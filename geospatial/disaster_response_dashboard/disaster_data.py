"""runPython target for the Disaster Response Dashboard (fused-render).

Fuses public satellite-acquisition footprints, a storm best-track, and
building-damage timeseries into one JSON payload for the interactive globe.
Produces, as one JSON payload:

  - fc          acquisition footprints FeatureCollection (Maxar Open Data STAC)
  - days        the linear daily axis (image count + storm wind/category per day)
  - center      [lon, lat] centroid of all footprints
  - track       Melissa's IBTrACS best-track fixes (3-hourly points)
  - track_line  the track as a GeoJSON LineString feature
  - aois        damage AOIs + their SAM2 building-damage timeseries

Underlying open data: Maxar Open Data program (satellite acquisitions).

Sources (all public, no auth):
  - footprints: https://maxar-opendata.s3.amazonaws.com STAC (134 acquisition
    collections, 2 fetches each) — replaces `fused.run("footprints")`, which
    fanned out `acq_footprint` workers with udf.map.
  - track: NOAA IBTrACS last3years CSV (~9.4 MB) — replaces `fused.run("melissa_track")`.
  - aois: STATIC snapshot (copied verbatim from globe_html.py's AOI_DEF fallback).
    The canvas also read a live Modal Dict via fused.secrets; locally there are no
    MODAL_TOKEN_ID/MODAL_TOKEN_SECRET env vars and no `modal` package in the
    bundled runtime, so we degrade to the snapshot. The source UDF itself documents
    that the snapshot covers all three AOIs (santa_cruz ONLY exists as snapshot).

30s-timeout strategy: every expensive fetch is disk-cached under ./.cache
(fresh subprocess per call -> disk, not memory), the footprint fan-out is
RESUMABLE (one cache file per acquisition), and main() works against a time
budget: if it can't finish in time it returns {"ready": False, done, total}
and the page polls again — each poll continues where the last one stopped.
"""

# /// script
# dependencies = ["requests", "pandas"]
# ///

import hashlib
import json
import os
import sys
import tempfile
import time

_HERE = (os.path.dirname(os.path.abspath(__file__))
         if "__file__" in globals() else os.path.abspath(sys.path[0]))


def _is_hosted() -> bool:
    """True on the hosted serve runtime (which injects the `openfused` shim);
    locally the example runs in its own uv script-venv where it's absent. Same
    probe cog_overview_pyramid/overview_pyramid.py uses."""
    try:
        import openfused  # noqa: F401

        return True
    except ImportError:
        return False


_HOSTED = _is_hosted()

# Hosted the bundle is read-only, so ./.cache next to the script isn't writable —
# cache into a per-run temp dir instead. Cross-call it won't persist (per-call
# subprocess isolation), which is why the resumable poll loop is collapsed to a
# single call hosted; see main().
_CACHE = (
    os.path.join(tempfile.gettempdir(), "fr-disaster-response-dashboard-cache")
    if _HOSTED
    else os.path.join(_HERE, ".cache")
)

CATALOG_URL = "https://maxar-opendata.s3.amazonaws.com/events/{event}/collection.json"
IBTRACS_CSV = (
    "https://www.ncei.noaa.gov/data/"
    "international-best-track-archive-for-climate-stewardship-ibtracs/"
    "v04r01/access/csv/ibtracs.last3years.list.v04r01.csv"
)

# Bridge calls die at 30 s. Budget the fan-out at 14 s and cap each HTTP request
# at 6 s so the worst-case tail (budget + one in-flight task of 2 requests)
# stays under the timeout even on a cold cache.
TIME_BUDGET_S = 14.0
# Hosted has no cross-call cache (per-call isolation), so the resumable poll
# strategy can't accumulate — the fan-out must finish in ONE call. The hosted
# per-call budget is far larger than the local 30 s bridge (Lambda-class), so
# run the whole fan-out inline. Kept comfortably under a 300 s Lambda timeout.
HOSTED_BUDGET_S = 240.0
REQ_TIMEOUT_S = 6
POOL_WORKERS = 24

# Maxar platform codes -> human-readable sensor names.
_PLATFORMS = {
    "WV01": "WorldView-1", "WV02": "WorldView-2", "WV03": "WorldView-3",
    "WV04": "WorldView-4", "GE01": "GeoEye-1", "QB02": "QuickBird-2",
    "LG01": "WorldView Legion", "LG02": "WorldView Legion",
}

# STATIC snapshot of the SAM2 building-damage timeseries per AOI — copied verbatim
# from globe_html.py's AOI_DEF fallback (historical results; won't change). The live
# Modal Dict override is attempted only if tokens + the modal package exist.
AOI_DEF = [
    {"id": "black_river", "label": "Black River", "lon": -77.851, "lat": 18.0325, "zoom": 15.5,
     "series": [
        {"date": "2025-11-02", "intact": 1180, "damaged": 135, "destroyed": 4, "n": 1319, "off": 17.1, "cloud": 2},
        {"date": "2025-11-03", "intact": 1184, "damaged": 129, "destroyed": 6, "n": 1319, "off": 9.9, "cloud": 0}]},
    {"id": "santa_cruz", "label": "Santa Cruz", "lon": -77.703, "lat": 18.052, "zoom": 15.5,
     "series": [
        {"date": "2025-10-29", "intact": 1471, "damaged": 204, "destroyed": 8, "n": 1683, "off": 36.3, "cloud": 20},
        {"date": "2025-10-31", "intact": 1565, "damaged": 112, "destroyed": 6, "n": 1683, "off": 15.1, "cloud": 23},
        {"date": "2025-11-03", "intact": 488, "damaged": 59, "destroyed": 7, "n": 554, "off": 9.0, "cloud": 11}]},
    {"id": "st_elizabeth_n", "label": "St. Elizabeth (N)", "lon": -77.948, "lat": 18.308, "zoom": 15.5,
     "series": [
        {"date": "2025-11-03", "intact": 530, "damaged": 119, "destroyed": 4, "n": 653, "off": 11.3, "cloud": 3}]},
]


# ---------------------------------------------------------------- cache helpers

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
    os.replace(tmp, path)  # atomic — never a half-written cache file


def _num(v):
    """float or None (NaN/garbage -> None) — keeps the payload JSON-clean."""
    try:
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def _clean(v):
    """Like globe_html's clean(): NaN -> None, keep ints/strings as-is."""
    try:
        f = float(v)
        return None if f != f else (f if isinstance(v, float) else v)
    except (TypeError, ValueError):
        return v


# ---------------------------------------------------------------- storm track

def _track(name="MELISSA", season=2025):
    cached = _read_json(_cpath("track.json"))
    if cached is not None:
        return cached

    import io

    import pandas as pd
    import requests

    cols = ["SID", "SEASON", "NAME", "ISO_TIME", "LAT", "LON",
            "USA_WIND", "USA_PRES", "USA_SSHS",
            "USA_R34_NE", "USA_R34_SE", "USA_R34_SW", "USA_R34_NW"]
    r34c = ["USA_R34_NE", "USA_R34_SE", "USA_R34_SW", "USA_R34_NW"]

    t0 = time.monotonic()
    resp = requests.get(IBTRACS_CSV, timeout=25)
    resp.raise_for_status()
    print(f"IBTrACS downloaded: {len(resp.content) / 1e6:.1f} MB in {time.monotonic() - t0:.1f}s")

    df = pd.read_csv(io.BytesIO(resp.content), skiprows=[1], usecols=cols, low_memory=False)
    df = df[(df["NAME"].str.upper() == name) & (pd.to_numeric(df["SEASON"], errors="coerce") == season)].copy()
    for c in ["LAT", "LON", "USA_WIND", "USA_PRES", "USA_SSHS"] + r34c:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["LAT", "LON"]).sort_values("ISO_TIME").reset_index(drop=True)
    if not len(df):
        raise RuntimeError(f"no IBTrACS fixes for {name} {season}")

    r34 = df[r34c].mean(axis=1, skipna=True)
    tpts = [{
        "lon": round(float(r["LON"]), 2), "lat": round(float(r["LAT"]), 2),
        "dt": str(r["ISO_TIME"]), "date": str(r["ISO_TIME"])[:10],
        "sshs": _num(r["USA_SSHS"]), "wind": _num(r["USA_WIND"]), "pres": _num(r["USA_PRES"]),
        "r34": (round(v) if (v := _num(r34.iloc[i])) is not None else None),
    } for i, (_, r) in enumerate(df.iterrows())]

    _write_json(_cpath("track.json"), tpts)
    return tpts


# ---------------------------------------------------------------- footprints

def _catalog(event_name):
    path = _cpath(f"catalog_{event_name}.json")
    cached = _read_json(path)
    if cached is not None:
        return cached
    import requests
    cat = requests.get(CATALOG_URL.format(event=event_name), timeout=REQ_TIMEOUT_S * 2).json()
    base = CATALOG_URL.format(event=event_name).rsplit("/", 1)[0] + "/"
    urls = [base + link["href"].lstrip("./")
            for link in cat.get("links", []) if link.get("rel") == "child"]
    _write_json(path, urls)
    return urls


def _fetch_acq(acq_url):
    """One acquisition -> footprint bbox + sensor metadata (mirrors acq_footprint.py)."""
    import urllib.parse

    import requests

    col = requests.get(acq_url, timeout=REQ_TIMEOUT_S).json()
    w, s, e, n = col["extent"]["spatial"]["bbox"][0][:4]

    item_href = next((l["href"] for l in col.get("links", []) if l.get("rel") == "item"), None)
    props = {}
    if item_href:
        base = acq_url.rsplit("/", 1)[0] + "/"
        try:
            props = requests.get(urllib.parse.urljoin(base, item_href), timeout=REQ_TIMEOUT_S).json().get("properties", {})
        except Exception as err:  # metadata is best-effort, bbox is the essential part
            print(f"item metadata fetch failed for {col.get('id')}: {err}")

    dt = str(props.get("datetime", ""))
    gsd = props.get("gsd")
    platform = props.get("platform", "")
    return {
        "acq_id": col.get("id", ""),
        "date": dt[:10] if dt else "unknown",
        "datetime": dt or "—",
        "sensor": _PLATFORMS.get(platform, platform or "—"),
        "res_cm": round(gsd * 100) if gsd is not None else None,
        "off_nadir": _num(props.get("view:off_nadir")),
        "azimuth": _num(props.get("view:azimuth")),
        "sun_elev": _num(props.get("view:sun_elevation")),
        "clouds_pct": _num(props.get("tile:clouds_percent")),
        "w": w, "s": s, "e": e, "n": n,
    }


def _footprints(event_name, deadline):
    """Resumable fan-out: one cache file per acquisition. Returns (rows, done, total, complete)."""
    urls = _catalog(event_name)
    rows, todo = [], []
    for u in urls:
        p = _cpath("acq", hashlib.sha256(u.encode()).hexdigest()[:16] + ".json")
        c = _read_json(p)
        if c is not None:
            rows.append(c)
        else:
            todo.append((u, p))

    if not todo:
        return rows, len(rows), len(urls), True

    failed = 0
    timed_out = False
    if time.monotonic() < deadline - 2:
        from concurrent.futures import TimeoutError as FuturesTimeout
        from concurrent.futures import ThreadPoolExecutor, as_completed
        ex = ThreadPoolExecutor(max_workers=POOL_WORKERS)
        futs = {ex.submit(_fetch_acq, u): (u, p) for u, p in todo}
        try:
            for f in as_completed(futs, timeout=max(1.0, deadline - time.monotonic())):
                u, p = futs[f]
                try:
                    row = f.result()
                    _write_json(p, row)
                    rows.append(row)
                except Exception as err:  # transient: NOT cached, retried on the next poll
                    failed += 1
                    print(f"acq fetch failed ({u.rsplit('/', 1)[-1]}): {err}")
                if time.monotonic() > deadline:
                    timed_out = True
                    break
        except (TimeoutError, FuturesTimeout):  # 3.10 futures.TimeoutError != builtin
            timed_out = True
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
    else:
        timed_out = True

    done, total = len(rows), len(urls)
    # Complete when everything was at least ATTEMPTED this round (a handful of
    # permanently-failing acquisitions must not poll forever — mirror the source's
    # dropna and render with what we have).
    complete = not timed_out
    if complete and failed:
        print(f"proceeding with {done}/{total} footprints ({failed} failed fetches dropped)")
    return rows, done, total, complete


# ---------------------------------------------------------------- AOI timeseries

def _load_timeseries_live():
    """Live Modal Dict read — only possible with tokens in the env AND the modal
    package installed. Neither holds in the fused-render bundled runtime; the
    static AOI_DEF snapshot is the documented fallback."""
    tid = os.environ.get("MODAL_TOKEN_ID")
    tsec = os.environ.get("MODAL_TOKEN_SECRET")
    if not (tid and tsec):
        print("no MODAL_TOKEN_ID/MODAL_TOKEN_SECRET in env -> static AOI snapshot")
        return None
    try:
        import modal
    except ImportError:
        print("modal package not installed -> static AOI snapshot")
        return None
    try:
        tsd = modal.Dict.from_name("building-damage-timeseries", create_if_missing=True)
        s = {}
        for k in tsd.keys():
            r = tsd[k]
            s.setdefault(r["aoi"], []).append({
                "date": r["date"], "intact": r["n_intact"], "damaged": r["n_damaged"],
                "destroyed": r["n_destroyed"], "n": r["n"],
                "off": round(float(r.get("off_nadir", 0)), 1), "cloud": round(float(r.get("cloud", 0)))})
        return s
    except Exception as err:
        print("Modal Dict read failed:", err)
        return None


def _aois():
    import copy
    aois = copy.deepcopy(AOI_DEF)
    live = _load_timeseries_live()
    src = "static snapshot"
    if live:
        for a in aois:
            loaded = sorted(live.get(a["id"], []), key=lambda x: x["date"])
            if loaded:
                a["series"] = loaded
        src = "live Modal Dict"
    return [a for a in aois if a["series"]], src


# ---------------------------------------------------------------- assembly

def _assemble(rows, track, event_name):
    import datetime as dt

    meta = ["acq_id", "date", "datetime", "sensor", "res_cm",
            "off_nadir", "azimuth", "sun_elev", "clouds_pct"]
    rows = [r for r in rows
            if r.get("date") not in (None, "unknown")
            and None not in (r.get("w"), r.get("s"), r.get("e"), r.get("n"))]
    rows.sort(key=lambda r: r["date"])

    features = []
    for r in rows:
        w, s, e, n = r["w"], r["s"], r["e"], r["n"]
        features.append({
            "type": "Feature",
            "properties": {c: _clean(r.get(c)) for c in meta},
            "geometry": {"type": "Polygon",
                         "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]},
        })
    fc = {"type": "FeatureCollection", "features": features}

    img_count = {}
    for f in features:
        d = f["properties"]["date"]
        img_count[d] = img_count.get(d, 0) + 1

    minx = min(r["w"] for r in rows)
    maxx = max(r["e"] for r in rows)
    miny = min(r["s"] for r in rows)
    maxy = max(r["n"] for r in rows)
    center = [float((minx + maxx) / 2), float((miny + maxy) / 2)]

    track_line = {"type": "Feature", "properties": {},
                  "geometry": {"type": "LineString",
                               "coordinates": [[p["lon"], p["lat"]] for p in track]}}

    # Linear daily axis: first track fix -> last imagery date (mirrors globe_html).
    track_day = {}
    for p in track:
        e = track_day.setdefault(p["date"], {"wind": None, "sshs": None})
        if p["wind"] is not None:
            e["wind"] = max(e["wind"] or 0, p["wind"])
        if p["sshs"] is not None:
            e["sshs"] = max(e["sshs"] if e["sshs"] is not None else -99, p["sshs"])
    start = min(track_day) if track_day else min(img_count)
    last = max(list(img_count) + list(track_day))
    days = []
    cur = dt.date.fromisoformat(start)
    end = dt.date.fromisoformat(last)
    while cur <= end:
        ds = cur.isoformat()
        td = track_day.get(ds, {})
        days.append({"date": ds, "images": img_count.get(ds, 0),
                     "wind": td.get("wind"), "sshs": td.get("sshs")})
        cur += dt.timedelta(days=1)

    aois, aoi_src = _aois()
    build = ("render-port · AOIs(" + aoi_src + "): "
             + ", ".join(f"{a['id']}({len(a['series'])})" for a in aois))

    return {
        "ready": True,
        "fc": fc,
        "days": days,
        "center": center,
        "track": track,
        "track_line": track_line,
        "aois": aois,
        "build": build,
        "title": event_name.replace("-", " "),
    }


def main(event_name: str = "Hurricane-Melissa-Oct-2025") -> dict:
    # Hosted: one long call (no cross-call cache to resume from). Local: the 14 s
    # per-poll budget with the page polling until complete.
    deadline = time.monotonic() + (HOSTED_BUDGET_S if _HOSTED else TIME_BUDGET_S)

    track = _track()  # disk-cached after the first successful poll

    rows, done, total, complete = _footprints(event_name, deadline)
    if not complete:
        if not _HOSTED:
            # Local: resumable — the page polls and each call continues the fan-out.
            print(f"footprints partial: {done}/{total} — page will poll again")
            return {"ready": False, "stage": "acquisition footprints", "done": done, "total": total}
        # Hosted: no cross-call cache to resume from, so returning ready:False would
        # make the page re-poll and restart the fan-out from zero, never converging.
        # Render whatever completed within the budget (a partial map beats an
        # un-resumable poll); only fail if nothing came back at all.
        if not rows:
            raise RuntimeError("footprint fetch exceeded the hosted time budget "
                               "before any acquisition completed — reload to retry.")
        print(f"hosted: assembling partial {done}/{total} footprints (budget hit)")

    payload = _assemble(rows, track, event_name)
    print(f"payload ready: {len(payload['fc']['features'])} footprints, "
          f"{len(payload['track'])} track fixes, {len(payload['days'])} days, "
          f"{len(payload['aois'])} AOIs")
    return payload
