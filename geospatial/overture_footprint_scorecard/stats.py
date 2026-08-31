"""Interactive queries for the viewer.

main(action="area", bbox=[w, s, e, n]) -> per-release stats for the buildings
    whose footprint bbox intersects the drawn box.
main(action="detail", fid=123) -> one building's per-release scores.
main(action="compare", a=0, b=6) -> how agreement changed between two releases.
main(action="method") -> where the data came from and how it was scored.
"""
from common import (
    BANDS,
    MIRROR,
    PHILLY_BOUNDS,
    PHILLY_GEOJSON_URL,
    RELEASES,
    cache_path,
    connect_plain,
)


def main(action: str = "area", bbox=None, fid: int = 0, a: int = 0, b: int = 0):
    if action == "area":
        return area_stats([float(v) for v in bbox])
    if action == "detail":
        return detail(int(fid))
    if action == "compare":
        box = [float(v) for v in bbox] if bbox else None
        return compare(int(a), int(b), box)
    if action == "method":
        return method()
    raise ValueError(f"unknown action {action!r}")


def method():
    """How the scores were produced, for the Ask AI panel to answer questions
    about the method rather than only about the numbers. Built from the same
    constants the pipeline runs on, so it cannot drift away from what the
    build actually did."""
    floors = dict(BANDS)
    return {
        "reference_layer": {
            "name": "LI_BUILDING_FOOTPRINTS",
            "publisher": "City of Philadelphia",
            "obtained_from": "ArcGIS Hub bulk GeoJSON download, " +
                             PHILLY_GEOJSON_URL.split("?")[0],
            "role": "treated as ground truth; every score is one of its buildings",
        },
        "overture_source": {
            "obtained_from": f"{MIRROR}/overture/<release>/theme=buildings/"
                             "type=building/, Fused's mirror of the Overture releases",
            "why_the_mirror": "the official Overture bucket keeps only the two most "
                              "recent releases, so the older ones are only on the mirror",
            "releases_scored": list(RELEASES),
            "clipped_to_bbox": list(PHILLY_BOUNDS),
        },
        "matching": [
            "Both layers are reprojected to EPSG:2272 (Pennsylvania South, US feet) "
            "so areas are measured in real units rather than in degrees.",
            "A DuckDB spatial join pairs each city building with every Overture "
            "footprint whose geometry intersects it.",
            "Each pair is scored IoU = shared area / (city area + Overture area - "
            "shared area). 1.0 is an identical footprint, 0 is no overlap.",
            "A city building keeps only its single best-scoring Overture match.",
            "IoU 0 means no Overture footprint overlaps that building at all.",
            "This is repeated independently for every release, so a building has "
            "one score per release.",
        ],
        "band_thresholds": {
            "close": f"IoU >= {floors['excellent']}",
            "partial": f"IoU {floors['good']} to {floors['excellent']}",
            "poor": f"IoU above 0 but below {floors['good']}",
            "absent": "no overlapping Overture footprint",
        },
        "reading_the_numbers": [
            "mean_iou and median_iou are averaged over MATCHED buildings only "
            "(IoU > 0); buildings Overture is missing are excluded from them and "
            "counted under absent instead.",
            "matched_pct is the share of city buildings with any overlapping "
            "Overture footprint.",
            "overture_footprints counts Overture buildings in the whole bounding "
            "box, which extends past the city limits, so it is expected to exceed "
            "the number of reference buildings.",
        ],
    }


# Change smaller than this is treated as the same footprint, not an edit.
MOVED = 0.05


def compare(a, b, bbox=None):
    """How every building's agreement changed between two releases."""
    con = connect_plain()
    path = cache_path("stats.parquet").replace("\\", "/")
    where = ""
    if bbox:
        west, south, east, north = bbox
        where = (f"WHERE maxx >= {west} AND minx <= {east} "
                 f"AND maxy >= {south} AND miny <= {north}")
    ia, ib = f"i{a}", f"i{b}"
    row = con.execute(f"""
        SELECT count(*) AS total,
               count(*) FILTER ({ia} = 0 AND {ib} > 0)  AS added,
               count(*) FILTER ({ia} > 0 AND {ib} = 0)  AS dropped,
               count(*) FILTER ({ia} > 0 AND {ib} > 0
                                AND {ib} - {ia} >= {MOVED})  AS better,
               count(*) FILTER ({ia} > 0 AND {ib} > 0
                                AND {ia} - {ib} >= {MOVED})  AS worse,
               count(*) FILTER (({ia} = 0 AND {ib} = 0)
                                OR ({ia} > 0 AND {ib} > 0
                                    AND abs({ib} - {ia}) < {MOVED})) AS same
        FROM read_parquet('{path}') {where}
    """).fetchone()
    con.close()
    keys = ["total", "added", "dropped", "better", "worse", "same"]
    counts = dict(zip(keys, (int(v) for v in row)))
    return {"a": a, "b": b, "moved": MOVED, "counts": counts}


def area_stats(bbox):
    west, south, east, north = bbox
    con = connect_plain()
    path = cache_path("stats.parquet").replace("\\", "/")
    selects = []
    for index in range(len(RELEASES)):
        column = f"i{index}"
        band_bits = []
        previous = None
        for name, floor in BANDS:
            upper = "" if previous is None else f" AND {column} < {previous}"
            band_bits.append(
                f"count(*) FILTER ({column} >= {floor}{upper}) AS {name}_{index}"
            )
            previous = floor
        selects.append(
            f"count(*) FILTER ({column} > 0) AS matched_{index}, "
            f"coalesce(avg({column}) FILTER ({column} > 0), 0) AS mean_{index}, "
            f"coalesce(median({column}) FILTER ({column} > 0), 0) AS median_{index}, "
            + ", ".join(band_bits)
        )
    row = con.execute(f"""
        SELECT count(*) AS total, {', '.join(selects)}
        FROM read_parquet('{path}')
        WHERE maxx >= {west} AND minx <= {east}
          AND maxy >= {south} AND miny <= {north}
    """).fetchone()
    names = [d[0] for d in con.description]
    con.close()
    values = dict(zip(names, row))
    total = values["total"]
    releases = []
    for index, release in enumerate(RELEASES):
        matched = values[f"matched_{index}"]
        bands = {name: int(values[f"{name}_{index}"]) for name, _floor in BANDS}
        bands["missing"] = int(total - matched)
        releases.append({
            "release": release,
            "matched": int(matched),
            "match_rate": (matched / total) if total else 0.0,
            "mean_iou": float(values[f"mean_{index}"]),
            "median_iou": float(values[f"median_{index}"]),
            "bands": bands,
        })
    return {"total": int(total), "bbox": bbox, "releases": releases}


def detail(fid):
    con = connect_plain()
    path = cache_path("detail.parquet").replace("\\", "/")
    row = con.execute(
        f"SELECT * FROM read_parquet('{path}') WHERE fid = {fid}"
    ).fetchone()
    if row is None:
        return {"found": False}
    names = [d[0] for d in con.description]
    con.close()
    values = dict(zip(names, row))
    return {
        "found": True,
        "fid": fid,
        "address": values.get("address") or "",
        "building_name": values.get("building_name") or "",
        "height": values.get("height"),
        "area_sqft": values.get("area"),
        "releases": [
            {
                "release": release,
                "iou": float(values[f"i{index}"]),
                "gers_id": values[f"g{index}"] or "",
            }
            for index, release in enumerate(RELEASES)
        ],
    }
