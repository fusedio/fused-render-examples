"""Panel 3: Census median household income inside the isochrone.

Port of canvas UDFs iso_census_intersect + iso_census_to_hex +
chart_data_income. The canvas read block-group geometries from
s3://fused-asset (needs Fused auth); this port substitutes public TIGERweb
geometries + the public census.gov B19013 table (see PORT_NOTES.md).
"""

import os
import sys

if "__file__" in globals():
    # fused-render runs this file as its real path both locally and hosted
    # (bundle v2), so the sibling _common.py is in this dir. The runner already
    # puts it on sys.path[0]; add it explicitly so `from _common import …` resolves.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main(
    address: str = "San Francisco, CA",
    travel_time_min: int = 15,
    transport_mode: str = "driving-car",
) -> dict:
    from shapely.geometry import shape
    from shapely.validation import make_valid

    from _common import (
        blockgroups_bbox,
        h3_connect,
        income_by_state,
        polygon_to_cells,
        resolve_iso,
    )

    lat, lon, label, geometry, area_km2, res = resolve_iso(
        address, travel_time_min, transport_mode
    )
    iso = shape(geometry)
    if not iso.is_valid:
        iso = make_valid(iso)
    xmin, ymin, xmax, ymax = (round(v, 5) for v in iso.bounds)

    bgs = blockgroups_bbox(xmin, ymin, xmax, ymax)  # cached

    # Income lookup for every state the bbox touches (cached per state)
    income = {}
    for st in sorted({bg["geoid"][:2] for bg in bgs}):
        income.update(income_by_state(st))

    # Intersect each block group with the isochrone, hexify the clipped part
    # carrying the block group's income, then average per hex (exactly what
    # iso_census_to_hex did via gdf_to_hex + AVG).
    con = h3_connect()
    sums, counts = {}, {}
    matched = 0
    for bg in bgs:
        inc = income.get(bg["geoid"])
        if inc is None:
            continue
        poly = shape(bg["geometry"])
        if not poly.is_valid:
            poly = make_valid(poly)
        clipped = poly.intersection(iso)
        if clipped.geom_type == "GeometryCollection":
            from shapely.ops import unary_union

            polys = [g for g in clipped.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
            if not polys:
                continue
            clipped = unary_union(polys)
        if clipped.is_empty or clipped.geom_type not in ("Polygon", "MultiPolygon"):
            continue
        matched += 1
        for cell in polygon_to_cells(con, clipped.__geo_interface__, res):
            sums[cell] = sums.get(cell, 0.0) + inc
            counts[cell] = counts.get(cell, 0) + 1
    con.close()

    hex_income = {h: round(sums[h] / counts[h]) for h in sums}
    values = sorted(hex_income.values())

    # Median of hex-level income (the canvas median_income widget computed
    # PERCENTILE_CONT(0.5) over the merged hex table).
    median_income = None
    if values:
        mid = len(values) // 2
        median_income = (
            values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
        )

    # 20 equal-width bins — same logic as the canvas chart_data_income UDF.
    histogram = {"labels": [], "counts": [], "edges": []}
    if values:
        lo, hi = float(values[0]), float(values[-1])
        span = (hi - lo) or 1.0
        n_bins = 20
        bins = [0] * n_bins
        for v in values:
            i = min(int((v - lo) / span * n_bins), n_bins - 1)
            bins[i] += 1
        edges = [lo + span * i / n_bins for i in range(n_bins + 1)]
        histogram = {
            "labels": [
                f"${int(edges[i]):,} - ${int(edges[i + 1]):,}" for i in range(n_bins)
            ],
            "counts": bins,
            "edges": [round(e) for e in edges],
        }

    print(
        f"census_panel: bgs={len(bgs)} matched={matched} hexes={len(hex_income)} "
        f"median={median_income} res={res}"
    )
    return {
        "hex_income": hex_income,
        "median_income": median_income,
        "bg_count": len(bgs),
        "bg_matched": matched,
        "histogram": histogram,
        "hex_res": res,
    }
