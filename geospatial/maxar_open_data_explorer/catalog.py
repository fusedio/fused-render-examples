"""runPython target for the Maxar Open Data explorer (fused-render).

Crawls the Maxar/Vantor Open Data STAC on S3 (no auth, CORS *):

  actions
  -------
  events                -> all 55+ events with bbox / temporal extent / acq count
  footprints event=<id> -> one row per acquisition (bbox, sensor, date, tile count)
  tiles event= acq=     -> one row per ARD tile of an acquisition (geometry +
                           absolute visual-COG URL) — the page streams the COGs
                           itself via HTTP range requests; nothing is downloaded.

30 s-timeout strategy (same as the vantor_data_fusion port): every fetch is
disk-cached under ./.cache, fan-outs are RESUMABLE (one cache file per unit) and
run against a time budget; when the budget runs out main() returns
{"ready": False, "done": n, "total": N} and the page polls again.
"""
# /// script
# dependencies = ["requests"]
# ///

import hashlib
import json
import os
import sys
import time

# The fused-render runner exec()s this file without __file__ (app >= Jul 2026);
# its preamble puts the script's directory at sys.path[0].
_HERE = (os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals()
         else os.path.abspath(sys.path[0]))
_CACHE = os.path.join(_HERE, ".cache")

ROOT = "https://maxar-opendata.s3.amazonaws.com/events/"
TIME_BUDGET_S = 14.0
REQ_TIMEOUT_S = 8
POOL_WORKERS = 24

_PLATFORMS = {
    "WV01": "WorldView-1", "WV02": "WorldView-2", "WV03": "WorldView-3",
    "WV04": "WorldView-4", "GE01": "GeoEye-1", "QB02": "QuickBird-2",
    "LG01": "WorldView Legion", "LG02": "WorldView Legion",
    "LG03": "WorldView Legion", "LG04": "WorldView Legion",
}


# ---------------------------------------------------------------- helpers

def _cpath(*parts):
    return os.path.join(_CACHE, *parts)


def _read_json(path):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return None
    return None


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)


def _num(v):
    try:
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def _hash(s):
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def _get(url):
    import requests
    r = requests.get(url, timeout=REQ_TIMEOUT_S)
    r.raise_for_status()
    return r.json()


def _resolve(base_url, href):
    import urllib.parse
    return urllib.parse.urljoin(base_url, href)


def _fanout(units, worker, deadline):
    """Resumable fan-out. units = [(key, cache_path, arg)]. Returns (rows, done, total, complete)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from concurrent.futures import TimeoutError as FuturesTimeout

    rows, todo = [], []
    for key, path, arg in units:
        c = _read_json(path)
        if c is not None:
            rows.append(c)
        else:
            todo.append((key, path, arg))
    total = len(units)
    if not todo:
        return rows, total, total, True

    timed_out = False
    with ThreadPoolExecutor(max_workers=POOL_WORKERS) as ex:
        futs = {}
        for key, path, arg in todo:
            if time.monotonic() > deadline:
                timed_out = True
                break
            futs[ex.submit(worker, arg)] = path
        try:
            for fut in as_completed(futs, timeout=max(0.5, deadline - time.monotonic() + 2 * REQ_TIMEOUT_S)):
                try:
                    row = fut.result()
                except Exception as err:
                    print(f"unit failed: {err}")
                    continue
                _write_json(futs[fut], row)
                rows.append(row)
                if time.monotonic() > deadline:
                    timed_out = True
                    break
        except FuturesTimeout:  # aliases builtin TimeoutError on 3.11+
            timed_out = True
        for fut in futs:
            fut.cancel()
    return rows, len(rows), total, (len(rows) == total and not timed_out)


# ---------------------------------------------------------------- events

def _event_ids():
    path = _cpath("event_ids.json")
    cached = _read_json(path)
    if cached is not None:
        return cached
    cat = _get(ROOT + "catalog.json")
    ids = [l["href"].split("/")[1] for l in cat.get("links", []) if l.get("rel") == "child"]
    _write_json(path, ids)
    return ids


def _event_meta(event_id):
    col = _get(f"{ROOT}{event_id}/collection.json")
    bbox = col["extent"]["spatial"]["bbox"][0][:4]
    interval = (col["extent"]["temporal"]["interval"] or [[None, None]])[0]
    n_acq = len([l for l in col.get("links", []) if l.get("rel") == "child"])
    return {
        "id": event_id,
        "title": col.get("title") or event_id.replace("-", " "),
        "bbox": [round(float(v), 4) for v in bbox],
        "start": (interval[0] or "")[:10] or None,
        "end": (interval[1] or "")[:10] or None,
        "n_acq": n_acq,
    }


def _events(deadline):
    ids = _event_ids()
    units = [(i, _cpath("events", _hash(i) + ".json"), i) for i in ids]
    rows, done, total, complete = _fanout(units, _event_meta, deadline)
    rows.sort(key=lambda r: (r["start"] or ""), reverse=True)
    return {"ready": complete, "done": done, "total": total,
            "events": rows if complete else []}


# ---------------------------------------------------------------- footprints

def _acq_urls(event):
    path = _cpath("fp", event, "_urls.json")
    cached = _read_json(path)
    if cached is not None:
        return cached
    base = f"{ROOT}{event}/collection.json"
    col = _get(base)
    urls = [_resolve(base, l["href"]) for l in col.get("links", []) if l.get("rel") == "child"]
    _write_json(path, urls)
    return urls


def _fetch_acq(acq_url):
    col = _get(acq_url)
    w, s, e, n = col["extent"]["spatial"]["bbox"][0][:4]
    item_hrefs = [_resolve(acq_url, l["href"]) for l in col.get("links", []) if l.get("rel") == "item"]

    props = {}
    if item_hrefs:
        try:
            props = _get(item_hrefs[0]).get("properties", {})
        except Exception as err:
            print(f"item metadata failed for {col.get('id')}: {err}")

    dt = str(props.get("datetime", ""))
    gsd = props.get("gsd")
    platform = props.get("platform", "")
    return {
        "acq_id": col.get("id", ""),
        "date": dt[:10] if dt else "unknown",
        "datetime": dt or None,
        "sensor": _PLATFORMS.get(platform, platform or "—"),
        "res_cm": round(gsd * 100) if gsd is not None else None,
        "off_nadir": _num(props.get("view:off_nadir")),
        "sun_elev": _num(props.get("view:sun_elevation")),
        "n_tiles": len(item_hrefs),
        "items": item_hrefs,
        "w": w, "s": s, "e": e, "n": n,
    }


def _footprints(event, deadline):
    urls = _acq_urls(event)
    units = [(u, _cpath("fp", event, _hash(u) + ".json"), u) for u in urls]
    rows, done, total, complete = _fanout(units, _fetch_acq, deadline)
    rows.sort(key=lambda r: (r["date"], r["acq_id"]))
    for r in rows:  # item URLs are re-served by the tiles action; keep payload lean
        r.pop("items", None)
    return {"ready": complete, "done": done, "total": total,
            "acquisitions": rows if complete else []}


# ---------------------------------------------------------------- tiles

def _fetch_tile(item_url):
    it = _get(item_url)
    props = it.get("properties", {})
    assets = it.get("assets", {})
    visual = assets.get("visual", {}).get("href")
    bbox = it.get("bbox", [None] * 4)[:4]
    return {
        "tile_id": f'{props.get("quadkey", "")}',
        "datetime": str(props.get("datetime", "")) or None,
        "clouds_pct": _num(props.get("tile:clouds_percent")),
        "epsg": props.get("proj:epsg"),
        "proj_bbox": props.get("proj:bbox"),
        "geometry": it.get("geometry"),
        "bbox": bbox,
        "visual": _resolve(item_url, visual) if visual else None,
    }


def _tiles(event, acq, deadline):
    urls = _acq_urls(event)
    match = None
    for u in urls:
        c = _read_json(_cpath("fp", event, _hash(u) + ".json"))
        if c and c.get("acq_id") == acq:
            match = u
            break
    if match is None:
        raise ValueError(f"acquisition {acq!r} not in cache — load footprints for {event!r} first")

    item_hrefs = _read_json(_cpath("fp", event, _hash(match) + ".json"))["items"]
    units = [(u, _cpath("tiles", event, _hash(u) + ".json"), u) for u in item_hrefs]
    rows, done, total, complete = _fanout(units, _fetch_tile, deadline)
    rows = [r for r in rows if r.get("visual")]
    rows.sort(key=lambda r: (r["clouds_pct"] if r["clouds_pct"] is not None else 999, r["tile_id"]))
    return {"ready": complete, "done": done, "total": total,
            "tiles": rows if complete else []}


# ---------------------------------------------------------------- entrypoint

def main(action: str = "events", event: str = "", acq: str = ""):
    deadline = time.monotonic() + TIME_BUDGET_S
    if action == "events":
        return _events(deadline)
    if action == "footprints":
        if not event:
            raise ValueError("footprints requires event=")
        return _footprints(event, deadline)
    if action == "tiles":
        if not event or not acq:
            raise ValueError("tiles requires event= and acq=")
        return _tiles(event, acq, deadline)
    raise ValueError(f"unknown action {action!r}")


# The fused-render runner (app >= Jul 2026) only invokes @fused.udf-registered
# entrypoints; a bare main() silently returns null. Register main via the shim.
try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass
