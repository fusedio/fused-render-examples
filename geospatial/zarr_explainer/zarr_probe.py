"""Zarr structure-probe daemon for the "What is Zarr" explainer.

Serves the interactive explainer page: opens any Zarr store (local, s3://,
https://, or a FusedRender rclone mount) with zarr-python 3 and exposes its
STRUCTURE — the group tree, every array's shape/chunk grid/codecs, the raw
metadata JSON, real chunk keys — plus instrumented reads: every /slice and
/probe reports exactly which chunk objects were fetched and what they cost
(requests, bytes, ms). All store I/O goes through the same counting +
caching MeterStore as templates/zarr_aoi/tile_server.py.

Endpoints (GET, CORS *):
  /ping /quit
  /tree?file=            -> group tree, arrays w/ chunk grids + sample keys,
                            suggested default var, consolidated-metadata flag
  /rawmeta?file=&path=   -> raw zarr.json / .zarray / .zattrs / .zmetadata text
  /slice?file=&var=&plane=d0,d1&idx=i0,..&r0=&r1=&c0=&c1=
                         -> read a 2D window of any plane orientation; returns
                            downsampled values + touched chunk keys + cost
  /probe?file=&var=&coords=i0,i1,..  -> one value + its chunk key + cost
  /stats?file=[&reset=1] -> live counters + recent op log
  /ls?file=              -> every file + size of a LOCAL store directory
                            (the mock store's real tree; refuses mounts)
  /clearcache?file=      -> forget the store: drops its open handle, LRU
                            chunk cache and meter, so the next read is COLD
                            (the explainer uses this to keep the "real S3
                            read" honest on every run)

Caps: a /slice may span at most 64 chunk objects and 4M cells — the error
message says so honestly (that's part of the lesson).
"""
# /// script
# dependencies = ["numpy", "zarr>=3.0.8", "s3fs", "crc32c"]
# ///

import hashlib
import json
import math
import os
import sys
import threading
import time

STATE = os.path.expanduser("~/.cache/fused-render-zarrsteps/daemon.json")
DAEMON_VENV = os.path.expanduser("~/.cache/fused-render-zarrsteps/venv")
SHARED_VENV = os.path.expanduser("~/.cache/fused-render-zarraoi/venv")
DAEMON_DEPS = ["numpy", "zarr>=3.0.8", "s3fs", "crc32c"]
IDLE_EXIT_S = 30 * 60
CACHE_CAP = 512 * 1024 * 1024
SLICE_CELL_CAP = 4_000_000
SLICE_OBJ_CAP = 64
MAX_NODES = 400


def _me():
    if "__file__" in globals():
        return os.path.abspath(__file__)
    return os.path.join(os.path.abspath(sys.path[0]), "zarr_probe.py")


def _daemon_python():
    # the zarr_aoi template builds an identical venv — reuse it when present
    for venv in (SHARED_VENV, DAEMON_VENV):
        vp = os.path.join(venv, "bin", "python")
        if os.path.exists(vp):
            return vp
    import shutil
    import subprocess
    uv = shutil.which("uv") or os.path.expanduser("~/.local/bin/uv")
    vp = os.path.join(DAEMON_VENV, "bin", "python")
    if os.path.exists(uv):
        try:
            os.makedirs(os.path.dirname(DAEMON_VENV), exist_ok=True)
            subprocess.run([uv, "venv", "--python", "3.12", DAEMON_VENV],
                           check=True, capture_output=True, timeout=120)
            subprocess.run([uv, "pip", "install", "-p", vp] + DAEMON_DEPS,
                           check=True, capture_output=True, timeout=600)
            return vp
        except Exception:
            import shutil as _sh
            _sh.rmtree(DAEMON_VENV, ignore_errors=True)
    return sys.executable


def _version():
    try:
        h = hashlib.sha256(open(_me(), "rb").read()).hexdigest()[:12]
    except OSError:
        h = "0"
    return h + "|" + _daemon_python()


def _alive(port, version):
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping",
                                    timeout=2) as r:
            d = json.load(r)
        return d.get("ok") and d.get("version") == version
    except Exception:
        return False


def main(action: str = "ensure"):
    import subprocess
    version = _version()
    try:
        with open(STATE) as f:
            st = json.load(f)
        if _alive(st.get("port"), version):
            return {"port": st["port"], "reused": True}
        try:
            import urllib.request
            urllib.request.urlopen(
                f"http://127.0.0.1:{st.get('port')}/quit", timeout=1).read()
        except Exception:
            pass
    except (OSError, ValueError):
        pass
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    log = os.path.join(os.path.dirname(STATE), "daemon.log")
    with open(log, "ab") as lf:
        subprocess.Popen([_daemon_python(), _me(), "--serve"],
                         stdout=lf, stderr=lf,
                         start_new_session=True, cwd=os.path.dirname(_me()))
    for _ in range(600):              # venv build on first run can take a while
        time.sleep(0.1)
        try:
            with open(STATE) as f:
                st = json.load(f)
            if st.get("version") == version and _alive(st.get("port"), version):
                return {"port": st["port"], "reused": False}
        except (OSError, ValueError):
            continue
    return {"error": f"zarr probe daemon did not start — see {log}"}


try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass


# ================================================================ daemon
def _serve():
    import numpy as np
    import zarr
    from collections import OrderedDict, deque
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs
    from zarr.storage import WrapperStore

    VERSION = _version()
    last_hit = [time.time()]

    # ---------------- counting + caching store ----------------
    class MeterStore(WrapperStore):
        def __init__(self, store, meter=None):
            super().__init__(store)
            self.meter = meter if meter is not None else new_meter()

        def with_read_only(self, read_only=False):
            return MeterStore(self._store.with_read_only(read_only), self.meter)

        def _cached(self, key, byte_range):
            ck = (key, repr(byte_range))
            m = self.meter
            with m["lock"]:
                hit = m["cache"].get(ck)
                if hit is not None:
                    m["cache"].move_to_end(ck)
                    m["cache_hits"] += 1
                    m["cache_saved"] += len(hit)
                    m["keys"].append({"key": key, "bytes": len(hit),
                                      "cached": True, "ms": 0.0,
                                      "range": _rng(byte_range)})
            return ck, hit

        def _cache_put(self, ck, data):
            m = self.meter
            with m["lock"]:
                m["cache"][ck] = data
                m["cache_bytes"] += len(data)
                while m["cache_bytes"] > CACHE_CAP and m["cache"]:
                    _, old = m["cache"].popitem(last=False)
                    m["cache_bytes"] -= len(old)

        async def get(self, key, prototype, byte_range=None):
            ck, hit = self._cached(key, byte_range)
            if hit is not None:
                return prototype.buffer.from_bytes(hit)
            m = self.meter
            t0 = time.perf_counter()
            out = await self._store_get(key, prototype, byte_range)
            ms = (time.perf_counter() - t0) * 1000
            n = len(out) if out is not None else 0
            with m["lock"]:
                m["requests"] += 1
                m["net_bytes"] += n
                m["net_ms"] += ms
                if out is None:
                    m["missing"] += 1
                m["keys"].append({"key": key, "bytes": n, "cached": False,
                                  "ms": round(ms, 1), "missing": out is None,
                                  "range": _rng(byte_range)})
            if out is not None:
                self._cache_put(ck, out.to_bytes())
            return out

        async def _store_get(self, key, prototype, byte_range):
            # big whole-chunk objects: parallel range fetch (single-stream
            # S3 GETs crawl on 26+ MB chunks, e.g. MUR SST)
            if byte_range is None:
                try:
                    import asyncio
                    fs = getattr(self._store, "fs", None)
                    root = getattr(self._store, "path", None)
                    if fs is not None and root:
                        full = f"{root}/{key}"
                        size = int((await fs._info(full)).get("size") or 0)
                        if size > 8 * 1024 * 1024:
                            n = 8
                            step = (size + n - 1) // n

                            async def part(i):
                                d = await fs._cat_file(
                                    full, start=i * step,
                                    end=min(size, (i + 1) * step))
                                # live progress for /stats pollers: bytes of
                                # the in-flight object, counted per part
                                with self.meter["lock"]:
                                    self.meter["inflight"] += len(d)
                                return d

                            parts = await asyncio.gather(
                                *(part(i) for i in range(n)))
                            with self.meter["lock"]:
                                self.meter["requests"] += n - 1
                                self.meter["inflight"] = 0
                            return prototype.buffer.from_bytes(b"".join(parts))
                except FileNotFoundError:
                    return None
                except Exception:
                    pass    # fall through to the plain single GET
            return await super().get(key, prototype, byte_range)

        async def get_partial_values(self, prototype, key_ranges):
            import asyncio
            return await asyncio.gather(
                *(self.get(k, prototype, r) for k, r in key_ranges))

        async def list_dir(self, prefix):
            with self.meter["lock"]:
                self.meter["lists"] += 1
            async for k in super().list_dir(prefix):
                yield k

    def _rng(byte_range):
        if byte_range is None:
            return None
        try:
            return [int(getattr(byte_range, "start", 0) or 0),
                    int(getattr(byte_range, "end", 0) or 0)] \
                if not isinstance(byte_range, (tuple, list)) \
                else [int(byte_range[0] or 0), int(byte_range[1] or 0)]
        except (TypeError, ValueError):
            return str(byte_range)

    def new_meter():
        return {"lock": threading.Lock(), "requests": 0, "net_bytes": 0,
                "inflight": 0, "net_ms": 0.0, "missing": 0, "lists": 0,
                "cache": OrderedDict(), "cache_bytes": 0,
                "cache_hits": 0, "cache_saved": 0,
                "keys": deque(maxlen=2000),
                "ops": deque(maxlen=40), "opened": time.time()}

    # ---------------- source resolution (same as zarr_aoi) ----------------
    def rclone_conf():
        out, sec = {}, None
        for p in (os.path.expanduser("~/.config/rclone/rclone.conf"),):
            try:
                for ln in open(p):
                    ln = ln.strip()
                    if ln.startswith("[") and ln.endswith("]"):
                        sec = ln[1:-1]
                        out[sec] = {}
                    elif "=" in ln and sec:
                        k, _, v = ln.partition("=")
                        out[sec][k.strip()] = v.strip()
            except OSError:
                pass
        return out

    def resolve_source(path):
        S3_POOL = {"config_kwargs": {"max_pool_connections": 48}}
        if path.startswith("s3://"):
            return {"kind": "s3", "url": path,
                    "storage_options": {"anon": True, **S3_POOL},
                    "label": path + " (anonymous S3)"}
        if path.startswith(("http://", "https://")):
            return {"kind": "http", "url": path, "storage_options": {},
                    "label": path}
        path = os.path.abspath(os.path.expanduser(path))
        mroot = os.path.expanduser("~/.fused-render/mounts") + os.sep
        if path.startswith(mroot):
            rel = path[len(mroot):]
            name, _, rest = rel.partition(os.sep)
            try:
                mounts = json.load(open(os.path.expanduser(
                    "~/.fused-render/mounts.json")))
            except (OSError, ValueError):
                mounts = []
            ent = next((m for m in mounts if m.get("name") == name), None)
            if ent and ":" in ent.get("remote", ""):
                rname, _, rpath = ent["remote"].partition(":")
                key = "/".join(s for s in (rpath.strip("/"),
                                           rest.replace(os.sep, "/")) if s)
                cfg = rclone_conf().get(rname, {})
                if cfg.get("type") == "s3":
                    so = dict(S3_POOL)
                    anon = cfg.get("env_auth", "false") != "true" and \
                        not cfg.get("access_key_id")
                    if anon:
                        so["anon"] = True
                    ck = {}
                    if cfg.get("region"):
                        ck["region_name"] = cfg["region"]
                    if cfg.get("endpoint"):
                        so["endpoint_url"] = cfg["endpoint"]
                    if ck:
                        so["client_kwargs"] = ck
                    return {"kind": "s3", "url": "s3://" + key,
                            "storage_options": so,
                            "label": f"mount '{name}' → s3://{key}"
                                     + (" (anonymous)" if anon else "")}
        return {"kind": "local", "url": path, "label": path + " (local)"}

    # ---------------- store open + tree walk ----------------
    datasets = {}
    ds_lock = threading.Lock()

    def dims_of(arr):
        md = arr.metadata.to_dict()
        d = md.get("dimension_names")
        if not d:
            d = dict(arr.attrs).get("_ARRAY_DIMENSIONS")
        return [str(x) for x in d] if d else \
            [f"dim_{i}" for i in range(arr.ndim)]

    def chunk_info(arr):
        md = arr.metadata.to_dict()
        info = {"chunks": None, "inner": None, "codecs": [],
                "zarr_format": md.get("zarr_format"), "sep": ".",
                "prefix": ""}
        if md.get("zarr_format") == 3:
            info["chunks"] = list(md["chunk_grid"]["configuration"]["chunk_shape"])
            cke = md.get("chunk_key_encoding") or {}
            info["sep"] = (cke.get("configuration") or {}).get("separator", "/")
            info["prefix"] = "c" if cke.get("name", "default") == "default" else ""
            for c in md.get("codecs", []):
                if c.get("name") == "sharding_indexed":
                    cfg = c["configuration"]
                    info["inner"] = list(cfg["chunk_shape"])
                    info["codecs"] += [cc.get("name") for cc in
                                       cfg.get("codecs", [])
                                       if cc.get("name") != "bytes"]
                elif c.get("name") != "bytes":
                    info["codecs"].append(c.get("name"))
        else:
            info["chunks"] = list(md.get("chunks") or [])
            info["sep"] = md.get("dimension_separator") or "."
            comp = md.get("compressor")
            if comp:
                info["codecs"] = [comp.get("id", "?")]
        return info

    def chunk_key(path, ci, coords):
        """The real store key for chunk grid coords (v2 and v3)."""
        sep = ci["sep"]
        if ci["zarr_format"] == 3:
            tail = sep.join([ci["prefix"]] + [str(c) for c in coords]) \
                if ci["prefix"] else sep.join(str(c) for c in coords)
        else:
            tail = sep.join(str(c) for c in coords)
        return f"{path}/{tail}" if path else tail

    def fill_of(arr):
        at = dict(arr.attrs)
        fv = arr.fill_value
        for k in ("_FillValue", "missing_value"):
            if k in at:
                try:
                    fv = float(np.asarray(at[k]).ravel()[0])
                except (TypeError, ValueError):
                    pass
        try:
            fv = None if fv is None else float(fv)
        except (TypeError, ValueError):
            return None
        return None if fv is not None and math.isnan(fv) else fv

    def decode_of(arr):
        at = dict(arr.attrs)

        def num(k):
            v = at.get(k)
            if v is None:
                return None
            try:
                return float(np.asarray(v).ravel()[0])
            except (TypeError, ValueError):
                return None
        return {"sf": num("scale_factor"), "ao": num("add_offset"),
                "fill": fill_of(arr), "units": str(at.get("units", ""))}

    def open_dataset(path):
        with ds_lock:
            ds = datasets.get(path)
        if ds is not None:
            return ds
        src = resolve_source(path)
        meter = new_meter()
        if src["kind"] == "local":
            inner = zarr.storage.LocalStore(src["url"], read_only=True)
        else:
            inner = zarr.storage.FsspecStore.from_url(
                src["url"], read_only=True,
                storage_options=src["storage_options"])
        store = MeterStore(inner, meter)
        root = zarr.open_group(store=store, mode="r")
        consolidated = getattr(root.metadata, "consolidated_metadata",
                               None) is not None

        arrays, groups = {}, {}

        def walk(g, prefix, depth):
            try:
                for k, a in g.arrays():
                    if len(arrays) < MAX_NODES:
                        arrays[f"{prefix}{k}"] = a
                subs = list(g.groups())
            except Exception:
                subs = []
            if depth >= 4:
                return
            for k, sg in subs:
                if len(arrays) >= MAX_NODES or len(groups) >= MAX_NODES:
                    break
                groups[f"{prefix}{k}"] = sg
                walk(sg, f"{prefix}{k}/", depth + 1)
        walk(root, "", 0)
        # OME-NGFF: level arrays are unlistable over plain http — take the
        # paths straight from the multiscales metadata
        ms = dict(root.attrs).get("multiscales")
        if isinstance(ms, list) and ms and isinstance(ms[0], dict):
            for ent in ms[0].get("datasets") or []:
                p = str(ent.get("path"))
                if p and p not in arrays:
                    try:
                        arrays[p] = root[p]
                    except (KeyError, FileNotFoundError):
                        pass

        ds = {"path": path, "src": src, "store": store, "root": root,
              "meter": meter, "arrays": arrays, "groups": groups,
              "consolidated": consolidated,
              "read_lock": threading.Lock()}
        with ds_lock:
            datasets[path] = ds
            while len(datasets) > 4:
                datasets.pop(next(iter(datasets)))
        return ds

    def metered(ds, kind, fn, **log):
        m = ds["meter"]
        got = ds["read_lock"].acquire(timeout=0.25)
        try:
            with m["lock"]:
                r0, b0, h0 = m["requests"], m["net_bytes"], m["cache_hits"]
                k0 = len(m["keys"])
            t0 = time.perf_counter()
            out = fn()
            ms = (time.perf_counter() - t0) * 1000
            with m["lock"]:
                keys = list(m["keys"])[k0:]
                ent = {"t": time.time(), "kind": kind, "ms": round(ms, 1),
                       "requests": m["requests"] - r0,
                       "net_bytes": m["net_bytes"] - b0,
                       "cache_hits": m["cache_hits"] - h0, **log}
                if not got:
                    ent["approx"] = True
                m["ops"].appendleft(ent)
        finally:
            if got:
                ds["read_lock"].release()
        return out, ent, keys

    # ---------------- endpoints ----------------
    def q1(q, k, dflt=None):
        v = q.get(k)
        return v[0] if v else dflt

    def arr_of(q, ds):
        var = q1(q, "var", "")
        a = ds["arrays"].get(var)
        if a is None:
            raise ValueError(f"unknown array {var!r}")
        return var, a

    def arr_node(path, a):
        ci = chunk_info(a)
        shape = [int(s) for s in a.shape]
        ch = ci["chunks"] or shape
        grid = [max(1, math.ceil(s / c)) for s, c in zip(shape, ch)] \
            if ch else []
        itemsize = np.dtype(a.dtype).itemsize
        node = {"path": path, "type": "array",
                "dims": dims_of(a), "shape": shape,
                "dtype": str(a.dtype), "itemsize": itemsize,
                "zarr_format": ci["zarr_format"],
                "chunks": ci["chunks"], "inner_chunks": ci["inner"],
                "chunk_grid": grid,
                "n_chunks": int(np.prod(grid)) if grid else 0,
                "codecs": ci["codecs"],
                "separator": ci["sep"],
                "fill_value": fill_of(a),
                "units": str(dict(a.attrs).get("units", "")),
                "logical_bytes": int(np.prod(shape)) * itemsize if shape
                else itemsize,
                "chunk_logical_bytes":
                    int(np.prod(ch)) * itemsize if ch else itemsize,
                "key_first": chunk_key(path, ci, [0] * len(shape)),
                "key_last": chunk_key(path, ci, [g - 1 for g in grid]),
                "attrs": {str(k): str(v)[:200]
                          for k, v in list(dict(a.attrs).items())[:12]}}
        if ci["inner"]:
            node["inner_grid"] = [max(1, math.ceil(c / i))
                                  for c, i in zip(ch, ci["inner"])]
        return node

    def do_tree(q):
        ds = open_dataset(q1(q, "file"))
        nodes = [arr_node(p, a) for p, a in
                 sorted(ds["arrays"].items())[:MAX_NODES]]
        gnodes = [{"path": p, "type": "group",
                   "attrs": {str(k): str(v)[:200]
                             for k, v in list(dict(g.attrs).items())[:12]}}
                  for p, g in sorted(ds["groups"].items())[:100]]
        # suggested default: biggest spatial grid, floats > ints on ties
        data_arrs = [n for n in nodes if len(n["shape"]) >= 2 and
                     not n["path"].lower().endswith(("_bnds", "_bounds",
                                                     "bnds"))]
        aux = ("mask", "quality", "condition", "angle", "footprint",
               "classification", "probability")
        default = max(data_arrs, key=lambda n: (
            not any(t in n["path"].lower() for t in aux),
            int(np.prod(n["shape"][-2:])),
            n["dtype"].startswith("float"),
            len(n["shape"])))["path"] if data_arrs else None
        root_attrs = dict(ds["root"].attrs)
        out = {"file": ds["path"], "source": ds["src"]["label"],
               "kind": ds["src"]["kind"],
               "zarr_format": ds["root"].metadata.zarr_format,
               "consolidated": ds["consolidated"],
               "n_arrays": len(ds["arrays"]), "n_groups": len(ds["groups"]),
               "default_var": default,
               "multiscales": bool(root_attrs.get("multiscales")),
               "attrs": {str(k): str(v)[:400]
                         for k, v in list(root_attrs.items())[:24]},
               "groups": gnodes, "arrays": nodes}
        return 200, json.dumps(out, default=str).encode(), "application/json"

    def do_rawmeta(q):
        ds = open_dataset(q1(q, "file"))
        node = q1(q, "path", "") or ""
        store = ds["store"]
        from zarr.core.buffer import default_buffer_prototype
        from zarr.core.sync import sync
        proto = default_buffer_prototype()

        def fetch(key):
            try:
                buf = sync(store.get(key, proto))
                return buf.to_bytes().decode("utf-8", "replace") \
                    if buf is not None else None
            except Exception:
                return None
        docs = {}
        cands = ([f"{node}/zarr.json" if node else "zarr.json"]
                 + [f"{node}/{s}" if node else s
                    for s in (".zarray", ".zgroup", ".zattrs")])
        if not node:
            cands.append(".zmetadata")
        for key in cands:
            txt = fetch(key)
            if txt is not None:
                docs[key] = txt[:20000]
        return 200, json.dumps({"path": node, "docs": docs}).encode(), \
            "application/json"

    def plan_objects(node_shape, ci, plane, idx, r0, r1, c0, c1):
        """Chunk-grid coords a selection touches + their store keys."""
        ch = ci["chunks"]
        d0, d1 = plane
        spans = []
        for d, (size, csz) in enumerate(zip(node_shape, ch)):
            if d == d0:
                lo, hi = r0 // csz, (r1 - 1) // csz
            elif d == d1:
                lo, hi = c0 // csz, (c1 - 1) // csz
            else:
                i = min(max(idx[d], 0), size - 1)
                lo = hi = i // csz
            spans.append(range(lo, hi + 1))
        total = 1
        for s in spans:
            total *= len(s)
        return spans, total

    def do_slice(q):
        ds = open_dataset(q1(q, "file"))
        var, a = arr_of(q, ds)
        nd = a.ndim
        shape = [int(s) for s in a.shape]
        if nd < 2:
            return do_slice_1d(q, ds, var, a)
        try:
            plane = [int(x) for x in (q1(q, "plane") or
                                      f"{nd-2},{nd-1}").split(",")]
        except ValueError:
            plane = [nd - 2, nd - 1]
        d0, d1 = plane
        if not (0 <= d0 < nd and 0 <= d1 < nd and d0 != d1):
            return 400, json.dumps({"error": "bad plane dims"}).encode(), \
                "application/json"
        idx = [0] * nd
        for i, tok in enumerate((q1(q, "idx") or "").split(",")[:nd]):
            try:
                idx[i] = int(tok)
            except ValueError:
                pass
        H, W = shape[d0], shape[d1]
        r0 = min(max(int(q1(q, "r0", "0")), 0), H - 1)
        c0 = min(max(int(q1(q, "c0", "0")), 0), W - 1)
        r1 = min(max(int(q1(q, "r1", str(H))), r0 + 1), H)
        c1 = min(max(int(q1(q, "c1", str(W))), c0 + 1), W)

        ci = chunk_info(a)
        if not ci["chunks"]:
            ci["chunks"] = shape
        spans, n_outer = plan_objects(shape, ci, (d0, d1), idx, r0, r1, c0, c1)
        # objects actually fetched = inner chunks when sharded
        n_obj = n_outer
        if ci["inner"]:
            ratio = 1
            for d in (d0, d1):
                ratio *= math.ceil(min(ci["chunks"][d], (r1 - r0) if d == d0
                                       else (c1 - c0)) / ci["inner"][d])
            n_obj = n_outer * max(1, ratio)
        cells = (r1 - r0) * (c1 - c0)
        if cells > SLICE_CELL_CAP or n_obj > SLICE_OBJ_CAP:
            return 413, json.dumps({
                "error": f"selection spans ~{n_obj} chunk objects / "
                         f"{cells:,} cells (caps: {SLICE_OBJ_CAP} objects, "
                         f"{SLICE_CELL_CAP:,} cells) — shrink the window",
                "objects": n_obj, "cells": cells}).encode(), \
                "application/json"

        sel = []
        for d in range(nd):
            if d == d0:
                sel.append(slice(r0, r1))
            elif d == d1:
                sel.append(slice(c0, c1))
            else:
                sel.append(min(max(idx[d], 0), shape[d] - 1))

        data, ent, keys = metered(
            ds, "slice", lambda: np.asarray(a[tuple(sel)]),
            var=var, plane=[d0, d1], window=[r1 - r0, c1 - c0])
        if d0 > d1:                      # keep rows=d0, cols=d1 orientation
            data = data.T

        dec = decode_of(a)
        planned = [chunk_key(var, ci, coords)
                   for coords in _iter_spans(spans)][:200]
        out = {
            "var": var, "plane": [d0, d1], "idx": idx,
            "window": [r0, r1, c0, c1], "shape": shape,
            "read_cells": cells,
            "logical_bytes": cells * np.dtype(a.dtype).itemsize,
            "planned_chunks": planned, "n_planned": n_outer,
            "touched": [k for k in keys if not k.get("missing")][:200],
            "n_missing": sum(1 for k in keys if k.get("missing")),
            "cost": {"requests": ent["requests"],
                     "net_bytes": ent["net_bytes"],
                     "cache_hits": ent["cache_hits"], "ms": ent["ms"]},
            "units": dec["units"],
            **_display(data, dec),
        }
        return 200, json.dumps(out, default=str).encode(), "application/json"

    def _display(data, dec):
        """Downsampled display payload; handles non-numeric dtypes too."""
        if data.dtype.kind not in "biufc":       # strings/bytes/objects
            sy = max(1, math.ceil(data.shape[0] / 160))
            sx = max(1, math.ceil(data.shape[1] / 160))
            disp = data[::sy, ::sx]
            return {"numeric": False, "vmin": None, "vmax": None,
                    "p2": None, "p98": None, "step": [sy, sx],
                    "values": [[str(v) for v in row] for row in disp]}
        vals = data.astype("float64")
        if dec["fill"] is not None:
            vals = np.where(vals == dec["fill"], np.nan, vals)
        if dec["sf"] is not None or dec["ao"] is not None:
            vals = vals * (dec["sf"] or 1.0) + (dec["ao"] or 0.0)
        sh, sw = vals.shape
        sy = max(1, math.ceil(sh / 160))
        sx = max(1, math.ceil(sw / 160))
        disp = vals[::sy, ::sx]
        fin = vals[np.isfinite(vals)]
        return {"numeric": True,
                "vmin": float(fin.min()) if fin.size else None,
                "vmax": float(fin.max()) if fin.size else None,
                "p2": float(np.percentile(fin, 2)) if fin.size else None,
                "p98": float(np.percentile(fin, 98)) if fin.size else None,
                "step": [sy, sx],
                "values": np.where(np.isfinite(disp), np.round(disp, 4),
                                   None).tolist()}

    def do_slice_1d(q, ds, var, a):
        """1-D arrays (usually coordinates): window on the single dim."""
        H = int(a.shape[0]) if a.ndim else 1
        c0 = min(max(int(q1(q, "c0", "0")), 0), max(H - 1, 0))
        c1 = min(max(int(q1(q, "c1", str(H))), c0 + 1), H)
        ci = chunk_info(a)
        if not ci["chunks"]:
            ci["chunks"] = [H]
        cs = max(1, int(ci["chunks"][0]))
        lo, hi = c0 // cs, (c1 - 1) // cs
        if hi - lo + 1 > SLICE_OBJ_CAP or (c1 - c0) > SLICE_CELL_CAP:
            return 413, json.dumps({
                "error": f"selection spans {hi - lo + 1} chunk objects / "
                         f"{c1 - c0:,} cells — shrink the window"}).encode(), \
                "application/json"
        data, ent, keys = metered(
            ds, "slice", lambda: np.asarray(a[c0:c1]).reshape(1, -1),
            var=var, window=[1, c1 - c0])
        dec = decode_of(a)
        out = {
            "var": var, "plane": [-1, 0], "idx": [0],
            "window": [0, 1, c0, c1], "shape": [H],
            "read_cells": c1 - c0,
            "logical_bytes": (c1 - c0) * np.dtype(a.dtype).itemsize,
            "planned_chunks": [chunk_key(var, ci, [i])
                               for i in range(lo, hi + 1)][:200],
            "n_planned": hi - lo + 1,
            "touched": [k for k in keys if not k.get("missing")][:200],
            "n_missing": sum(1 for k in keys if k.get("missing")),
            "cost": {"requests": ent["requests"],
                     "net_bytes": ent["net_bytes"],
                     "cache_hits": ent["cache_hits"], "ms": ent["ms"]},
            "units": dec["units"],
            **_display(data, dec),
        }
        return 200, json.dumps(out, default=str).encode(), "application/json"

    def _iter_spans(spans):
        import itertools
        return itertools.product(*spans)

    def do_probe(q):
        ds = open_dataset(q1(q, "file"))
        var, a = arr_of(q, ds)
        nd = a.ndim
        shape = [int(s) for s in a.shape]
        coords = [0] * nd
        for i, tok in enumerate((q1(q, "coords") or "").split(",")[:nd]):
            try:
                coords[i] = min(max(int(tok), 0), shape[i] - 1)
            except ValueError:
                pass
        data, ent, keys = metered(
            ds, "probe", lambda: np.asarray(a[tuple(coords)]),
            var=var, coords=coords)
        ci = chunk_info(a)
        ch = ci["chunks"] or shape
        ccoords = [c // s for c, s in zip(coords, ch)]
        dec = decode_of(a)
        if data.dtype.kind not in "biufc":
            out = {"var": var, "coords": coords, "chunk_coords": ccoords,
                   "chunk_key": chunk_key(var, ci, ccoords),
                   "value": str(data.ravel()[0]), "units": dec["units"],
                   "touched": [k for k in keys if not k.get("missing")][:40],
                   "cost": {"requests": ent["requests"],
                            "net_bytes": ent["net_bytes"],
                            "cache_hits": ent["cache_hits"], "ms": ent["ms"]}}
            return 200, json.dumps(out).encode(), "application/json"
        try:
            v = float(data.ravel()[0])
            if dec["fill"] is not None and v == dec["fill"]:
                v = float("nan")
            if dec["sf"] is not None or dec["ao"] is not None:
                v = v * (dec["sf"] or 1.0) + (dec["ao"] or 0.0)
        except (TypeError, ValueError):
            v = float("nan")
        out = {"var": var, "coords": coords, "chunk_coords": ccoords,
               "chunk_key": chunk_key(var, ci, ccoords),
               "value": None if math.isnan(v) else v,
               "units": dec["units"],
               "touched": [k for k in keys if not k.get("missing")][:40],
               "cost": {"requests": ent["requests"],
                        "net_bytes": ent["net_bytes"],
                        "cache_hits": ent["cache_hits"], "ms": ent["ms"]}}
        return 200, json.dumps(out).encode(), "application/json"

    def do_stats(q):
        ds = open_dataset(q1(q, "file"))
        m = ds["meter"]
        with m["lock"]:
            if q1(q, "reset") == "1":
                m.update({"requests": 0, "net_bytes": 0, "inflight": 0,
                          "net_ms": 0.0, "missing": 0, "lists": 0,
                          "cache_hits": 0, "cache_saved": 0,
                          "opened": time.time()})
                m["ops"].clear()
                m["keys"].clear()
            out = {k: m[k] for k in ("requests", "net_bytes", "inflight",
                                     "net_ms", "missing", "lists",
                                     "cache_hits", "cache_saved",
                                     "cache_bytes", "opened")}
            out["ops"] = list(m["ops"])
            out["recent_keys"] = list(m["keys"])[-60:]
        return 200, json.dumps(out).encode(), "application/json"

    def do_clearcache(q):
        """Drop a store's open handle + LRU cache + meter entirely, so the
        next read against it is genuinely cold."""
        raw = (q1(q, "file") or "").strip()
        with ds_lock:
            ds = datasets.pop(raw, None)
        return 200, json.dumps(
            {"cleared": ds is not None, "file": raw}).encode(), \
            "application/json"

    def do_ls(q):
        """List every file of a LOCAL zarr store directory, with sizes.
        Feeds the explainer's file-tree step (mock store only — refuses
        remote URLs and FUSE mounts, which must never be walked)."""
        raw = (q1(q, "file") or "").strip()
        if raw.startswith(("s3://", "http://", "https://")):
            return 400, json.dumps(
                {"error": "/ls only lists local directories"}).encode(), \
                "application/json"
        path = os.path.abspath(os.path.expanduser(raw))
        if path.startswith(os.path.expanduser("~/.fused-render")):
            return 400, json.dumps(
                {"error": "refusing to walk mounted storage"}).encode(), \
                "application/json"
        if not os.path.isdir(path):
            return 400, json.dumps(
                {"error": f"not a local directory: {path}"}).encode(), \
                "application/json"
        files, total, count = [], 0, 0
        for root, dirs, names in os.walk(path):
            dirs.sort()
            for n in sorted(names):
                if n == ".DS_Store":
                    continue
                fp = os.path.join(root, n)
                try:
                    sz = os.stat(fp).st_size
                except OSError:
                    continue
                total += sz
                count += 1
                if len(files) < 1200:
                    files.append({"path": os.path.relpath(fp, path)
                                  .replace(os.sep, "/"), "bytes": sz})
        return 200, json.dumps(
            {"root": path, "n_files": count, "total_bytes": total,
             "files": files}).encode(), "application/json"

    def do_head(q):
        """HEAD one object (https/s3/local) -> its exact size in bytes.
        Lets the page quote real object sizes without downloading them."""
        import urllib.error
        import urllib.request
        target = (q1(q, "url") or "").strip()
        if not target:
            return 400, json.dumps({"error": "url= required"}).encode(), \
                "application/json"
        if target.startswith("s3://"):
            bucket, _, key = target[5:].partition("/")
            target = f"https://{bucket}.s3.amazonaws.com/{key}"
        if target.startswith("http"):
            def _h(u):
                req = urllib.request.Request(u, method="HEAD")
                return urllib.request.urlopen(req, timeout=25)
            t0 = time.time()
            try:
                r = _h(target)
            except urllib.error.HTTPError as e:
                reg = e.headers.get("x-amz-bucket-region")
                if reg and ".s3.amazonaws.com/" in target:
                    target = target.replace(
                        ".s3.amazonaws.com/", f".s3.{reg}.amazonaws.com/")
                    r = _h(target)
                else:
                    raise
            with r:
                size = int(r.headers.get("Content-Length") or 0)
                code = r.status
            return 200, json.dumps(
                {"url": target, "bytes": size, "status": code,
                 "ms": round((time.time() - t0) * 1000, 1)}).encode(), \
                "application/json"
        st_ = os.stat(target)
        return 200, json.dumps(
            {"url": target, "bytes": st_.st_size, "status": 200,
             "ms": 0}).encode(), "application/json"

    # ---------------- HTTP ----------------
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            last_hit[0] = time.time()
            u = urlparse(self.path)
            q = parse_qs(u.query)
            try:
                if u.path == "/ping":
                    code, body, ct = 200, json.dumps(
                        {"ok": True, "version": VERSION}).encode(), \
                        "application/json"
                elif u.path == "/quit":
                    self._send(200, b"bye", "text/plain")
                    threading.Thread(target=srv.shutdown,
                                     daemon=True).start()
                    return
                elif u.path == "/tree":
                    code, body, ct = do_tree(q)
                elif u.path == "/rawmeta":
                    code, body, ct = do_rawmeta(q)
                elif u.path == "/slice":
                    code, body, ct = do_slice(q)
                elif u.path == "/probe":
                    code, body, ct = do_probe(q)
                elif u.path == "/stats":
                    code, body, ct = do_stats(q)
                elif u.path == "/head":
                    code, body, ct = do_head(q)
                elif u.path == "/ls":
                    code, body, ct = do_ls(q)
                elif u.path == "/clearcache":
                    code, body, ct = do_clearcache(q)
                else:
                    code, body, ct = 404, b"not found", "text/plain"
            except Exception as e:
                import traceback
                traceback.print_exc()
                code, body, ct = 500, json.dumps(
                    {"error": str(e)}).encode(), "application/json"
            self._send(code, body, ct)

        def _send(self, code, body, ct):
            self.send_response(code)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as fh:
        json.dump({"port": port, "pid": os.getpid(), "version": VERSION}, fh)

    def reaper():
        while True:
            time.sleep(60)
            if time.time() - last_hit[0] > IDLE_EXIT_S:
                srv.shutdown()
                return
    threading.Thread(target=reaper, daemon=True).start()
    print(f"zarr probe daemon on 127.0.0.1:{port} (v{VERSION})", flush=True)
    srv.serve_forever()


if __name__ == "__main__" and "--serve" in sys.argv:
    _serve()
