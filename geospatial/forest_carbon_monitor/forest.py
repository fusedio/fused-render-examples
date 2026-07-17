"""Data backend for the Protected Forest Monitor dashboard.

Sources (all public, keyless):
- Park boundaries: OpenStreetMap protected-area relations, simplified to
  GeoJSON and shipped in ./boundaries/ (ODbL, © OpenStreetMap contributors).
- Annual tree-cover-loss + tree-cover-extent zonal statistics:
  Global Forest Watch Data API (data-api.globalforestwatch.org) queried
  on-the-fly against the shipped boundary polygon, using the API key that
  GFW's own public frontend embeds. Threshold: canopy density >= 30% — the
  same threshold as the tcd_30 map tiles, so map and chart tell one story.

Everything expensive is disk-cached under ./.cache (fused-render runs each
bridge call in a fresh subprocess, so the cache must live on disk). A cold
catalog is fetched by a detached warmer process the page polls.
"""
# /// script
# dependencies = ["requests"]
# ///

import functools
import hashlib
import json
import os
import sys
import tempfile

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
# subprocess isolation), but each hosted call recomputes inline within the larger
# serve budget; see _warm.
_CACHE_DIR = (
    os.path.join(tempfile.gettempdir(), "fr-forest-carbon-monitor-cache")
    if _HOSTED
    else os.path.join(_HERE, ".cache")
)

_GFW_API = "https://data-api.globalforestwatch.org"
# Public key embedded in globalforestwatch.org's own frontend bundle.
_GFW_KEY = "092d42cf-281d-4da1-a7c6-96a9ecff6cb4"
_THRESHOLD = 30  # canopy density %, matches the tcd_30 tile style

# Static metadata: official listed areas (UNESCO / national listings);
# area_boundary_ha (computed from the shipped OSM polygon) can differ — both
# are shown, exactly because the discrepancy is interesting.
PARKS = {
    "virunga_drc": {
        "name": "Virunga National Park",
        "designation": "National Park · World Heritage",
        "country": "DR Congo",
        "iucn": "II",
        "managed_by": "Institut Congolais pour la Conservation de la Nature",
        "area_listed_ha": 790000,
    },
    "manu_peru": {
        "name": "Manú National Park",
        "designation": "National Park · World Heritage",
        "country": "Peru",
        "iucn": "II",
        "managed_by": "SERNANP",
        "area_listed_ha": 1716295,
    },
    "gunung_leuser_id": {
        "name": "Gunung Leuser National Park",
        "designation": "National Park · World Heritage",
        "country": "Indonesia",
        "iucn": "II",
        "managed_by": "Ministry of Environment and Forestry",
        "area_listed_ha": 792700,
    },
    "bwindi_uganda": {
        "name": "Bwindi Impenetrable National Park",
        "designation": "National Park · World Heritage",
        "country": "Uganda",
        "iucn": "II",
        "managed_by": "Uganda Wildlife Authority",
        "area_listed_ha": 32092,
    },
    "corcovado_cr": {
        "name": "Corcovado National Park",
        "designation": "National Park",
        "country": "Costa Rica",
        "iucn": "II",
        "managed_by": "SINAC (Área de Conservación Osa)",
        "area_listed_ha": 42400,
    },
    "masoala_mg": {
        "name": "Masoala National Park",
        "designation": "National Park · World Heritage",
        "country": "Madagascar",
        "iucn": "II",
        "managed_by": "Madagascar National Parks",
        "area_listed_ha": 230000,
    },
}


def disk_cache(fn):
    """Memoize a JSON-returning function to disk, keyed by its args."""

    def cache_path(*args, **kwargs):
        key_src = json.dumps([fn.__name__, args, kwargs], sort_keys=True, default=str)
        key = hashlib.sha256(key_src.encode()).hexdigest()[:16]
        return os.path.join(_CACHE_DIR, f"{fn.__name__}_{key}.json")

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        path = cache_path(*args, **kwargs)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        result = fn(*args, **kwargs)
        os.makedirs(_CACHE_DIR, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(result, fh)
        os.replace(tmp, path)
        return result

    wrapper.cache_path = cache_path
    return wrapper


def _boundary(park: str) -> dict:
    # Hosted, the bundle is a read-only exec dir that holds only the entrypoint
    # code — sibling data like boundaries/ isn't next to __file__, so resolve it
    # through the bundle asset map instead (the files are bundled via the
    # _bundleBoundaries() literals in index.html). Locally read beside the script.
    if _HOSTED:
        import openfused

        path = openfused.asset_path("boundaries", f"{park}.json")
    else:
        path = os.path.join(_HERE, "boundaries", f"{park}.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _gfw_query(dataset: str, sql: str, geometry: dict) -> list:
    import requests

    r = requests.post(
        f"{_GFW_API}/dataset/{dataset}/latest/query/json",
        headers={"x-api-key": _GFW_KEY},
        json={"sql": sql, "geometry": geometry},
        timeout=60,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(f"GFW query failed: {str(body)[:300]}")
    return body["data"]


@disk_cache
def _park_stats(park: str, threshold: int) -> dict:
    """Annual loss series + tree-cover extent for one park boundary."""
    geom = _boundary(park)["geometry"]
    loss = _gfw_query(
        "umd_tree_cover_loss",
        "SELECT umd_tree_cover_loss__year AS year, SUM(area__ha) AS loss_ha "
        f"FROM results WHERE umd_tree_cover_density_2000__threshold >= {threshold} "
        "GROUP BY umd_tree_cover_loss__year ORDER BY umd_tree_cover_loss__year",
        geom,
    )
    extent = _gfw_query(
        "umd_tree_cover_density_2000",
        "SELECT SUM(area__ha) AS extent_ha FROM results "
        f"WHERE umd_tree_cover_density_2000__threshold >= {threshold}",
        geom,
    )
    by_year = {int(r["year"]): round(float(r["loss_ha"]), 2) for r in loss if r.get("year")}
    years = sorted(by_year)
    lo, hi = (years[0], years[-1]) if years else (2001, 2001)
    series = [{"year": y, "loss_ha": by_year.get(y, 0.0)} for y in range(min(lo, 2001), hi + 1)]
    extent_ha = round(float(extent[0]["extent_ha"] or 0), 2) if extent else 0.0
    return {"series": series, "extent_2000_ha": extent_ha}


# ------------------------------------------------------------ warm-up daemon
# A cold catalog is 12 GFW zonal queries — way past the 30 s bridge budget.
# step="warm" spawns a DETACHED process that fills the per-park disk cache;
# the page polls until every park is cached.

def _warm(threshold: int):
    import subprocess

    missing = [p for p in PARKS
               if not os.path.exists(_park_stats.cache_path(p, threshold))]
    if not missing:
        return {"ready": True, "done": len(PARKS), "total": len(PARKS)}

    if _HOSTED:
        # No detached daemon or cross-call cache hosted (per-call subprocess
        # isolation, read-only bundle) — the reason the page hung at "0/6 parks".
        # Skip the background warmer and report ready; step="catalog"/"detail"
        # run _park_stats inline within the larger hosted budget (the local ~30s
        # bridge is the only reason the daemon exists).
        return {"ready": True, "done": len(PARKS), "total": len(PARKS)}

    os.makedirs(_CACHE_DIR, exist_ok=True)
    lock = os.path.join(_CACHE_DIR, f"warm_{threshold}.pid")
    err = os.path.join(_CACHE_DIR, f"warm_{threshold}.err")
    state = {"ready": False, "done": len(PARKS) - len(missing), "total": len(PARKS)}
    if os.path.exists(lock):
        try:
            os.kill(int(open(lock, encoding="utf-8").read().strip()), 0)
            return state  # warmer alive, keep polling
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # stale lock: warmer died
        os.remove(lock)  # so the next call retries
        tail = ""
        if os.path.exists(err):
            tail = open(err, encoding="utf-8").read().strip()[-400:]
        if tail:
            raise RuntimeError(f"background data fetch failed: {tail}")

    code = (f"import sys; sys.path.insert(0, {_HERE!r}); import forest; "
            f"[forest._park_stats(p, {threshold}) for p in forest.PARKS]")
    with open(err, "w", encoding="utf-8") as errfh:
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=errfh,
        )
    with open(lock, "w", encoding="utf-8") as fh:
        fh.write(str(proc.pid))
    return state


def _kpis(park: str, series: list, extent_ha: float, y0: int, y1: int) -> dict:
    sel = [r for r in series if y0 <= r["year"] <= y1]
    cum = sum(r["loss_ha"] for r in sel)
    worst = max(sel, key=lambda r: r["loss_ha"]) if sel else None
    b = _boundary(park)
    return {
        "area_boundary_ha": b["area_ha"],
        "area_listed_ha": PARKS[park]["area_listed_ha"],
        "extent_2000_ha": extent_ha,
        "cumulative_loss_ha": round(cum, 1),
        "worst_year": worst["year"] if worst else None,
        "worst_year_loss_ha": round(worst["loss_ha"], 1) if worst else 0,
        "pct_extent_lost": round(100 * cum / extent_ha, 2) if extent_ha else None,
    }


def main(
    action: str = "detail",
    park: str = "virunga_drc",
    year_start: int = 2001,
    year_end: int = 2024,
    threshold: int = 30,
) -> dict:
    threshold = int(threshold)

    if action == "warm":
        return _warm(threshold)

    if action == "catalog":
        rows = []
        for pid, meta in PARKS.items():
            b = _boundary(pid)
            stats = _park_stats(pid, threshold)
            total = sum(r["loss_ha"] for r in stats["series"])
            rows.append({
                "id": pid,
                **meta,
                "area_boundary_ha": b["area_ha"],
                "extent_2000_ha": stats["extent_2000_ha"],
                "cumulative_loss_ha": round(total, 1),
                "pct_extent_lost": round(100 * total / stats["extent_2000_ha"], 2)
                if stats["extent_2000_ha"] else None,
            })
        return {"parks": rows, "threshold": threshold}

    if park not in PARKS:
        raise ValueError(f"unknown park {park!r}; one of {sorted(PARKS)}")

    b = _boundary(park)
    stats = _park_stats(park, threshold)
    series = stats["series"]
    data_years = [r["year"] for r in series]
    print(f"{park}: {len(series)} yrs, extent={stats['extent_2000_ha']:.0f} ha")
    return {
        "id": park,
        "meta": PARKS[park],
        "boundary": b["geometry"],
        "series": series,
        "data_year_min": min(data_years) if data_years else None,
        "data_year_max": max(data_years) if data_years else None,
        "threshold": threshold,
        "kpis": _kpis(park, series, stats["extent_2000_ha"], int(year_start), int(year_end)),
    }
