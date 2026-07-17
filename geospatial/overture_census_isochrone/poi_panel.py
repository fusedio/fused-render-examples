"""Panel 2: Overture Places POIs inside the isochrone + H3 counts.

Port of canvas UDFs Overture_Maps_Example + query_overture_pois +
pois_to_hex. The Overture query goes straight at the public STAC/S3 parquet
via DuckDB (no fused.load), and the hex aggregation uses the DuckDB h3
community extension instead of fused common.gdf_to_hex.
"""

import os
import sys

if "__file__" in globals():
    # Running as a plain script; the fused-render runner already puts the
    # script dir at sys.path[0] and exposes no __file__.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Hosted, the code runs with no __file__ (so the insert above is skipped) and the
# bundled sibling module (_common.py) lands under the project's assets/ dir, which
# isn't on sys.path. Add it so `from _common import …` (in main) resolves. Harmless
# locally: that dir doesn't exist there and _common is already importable above.
try:
    import openfused  # noqa: E402

    _assets_dir = os.path.join(openfused.project_root(), "assets")
    if os.path.isdir(_assets_dir):
        sys.path.insert(0, _assets_dir)
except ImportError:
    pass

_HERE = (os.path.dirname(os.path.abspath(__file__))
         if "__file__" in globals() else os.path.abspath(sys.path[0]))
_POI_PLOT_LIMIT = 800  # cap points shipped to the browser; counts stay exact


def main(
    address: str = "San Francisco, CA",
    travel_time_min: int = 15,
    transport_mode: str = "driving-car",
    poi_category: str = "cafe",
    step: str = "view",
) -> dict:
    from shapely.geometry import Point, shape
    from shapely.prepared import prep

    from _common import h3_connect, overture_pois_bbox, resolve_iso, warm_via_daemon

    lat, lon, label, geometry, area_km2, res = resolve_iso(
        address, travel_time_min, transport_mode
    )
    iso = shape(geometry)
    xmin, ymin, xmax, ymax = (round(v, 5) for v in iso.bounds)

    if step == "warm":
        # The Overture S3 scan can outlive the 30 s bridge budget on a cold
        # bbox — hand it to a detached warmer and let the page poll.
        target = overture_pois_bbox.cache_path(xmin, ymin, xmax, ymax, poi_category)
        code = (f"import sys; sys.path.insert(0, {_HERE!r}); "
                f"from _common import overture_pois_bbox; "
                f"overture_pois_bbox({xmin!r}, {ymin!r}, {xmax!r}, {ymax!r}, {poi_category!r})")
        return warm_via_daemon(f"poi_{poi_category}", target, code)

    raw = overture_pois_bbox(xmin, ymin, xmax, ymax, poi_category)  # slow, cached
    prepared = prep(iso)
    inside = [p for p in raw if prepared.contains(Point(p["lon"], p["lat"]))]

    # H3 aggregation (canvas pois_to_hex did this in DuckDB too)
    hex_counts = {}
    if inside:
        import pandas as pd

        df = pd.DataFrame(inside)[["lat", "lon"]]
        con = h3_connect()
        agg = con.execute(
            f"""
            SELECT h3_latlng_to_cell_string(lat, lon, {res}) AS hex,
                   COUNT(*) AS n
            FROM df GROUP BY 1
            """
        ).fetchall()
        con.close()
        hex_counts = {h: int(n) for h, n in agg}

    # Cap the points shipped to the browser with an even stride, not a head
    # slice — parquet row order is spatially clustered, so the first N points
    # would all sit in one corner of the isochrone.
    if len(inside) > _POI_PLOT_LIMIT:
        step = len(inside) / _POI_PLOT_LIMIT
        shipped = [inside[int(i * step)] for i in range(_POI_PLOT_LIMIT)]
    else:
        shipped = inside

    print(
        f"poi_panel: {poi_category!r} bbox_raw={len(raw)} inside={len(inside)} "
        f"hexes={len(hex_counts)} res={res}"
    )
    return {
        "poi_category": poi_category,
        "poi_count": len(inside),
        "pois": shipped,
        "hex_counts": hex_counts,
        "hex_res": res,
    }
