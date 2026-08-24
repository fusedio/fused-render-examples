"""Interactive queries for the viewer.

main(action="area", bbox=[w, s, e, n]) -> per-release stats for the buildings
    whose footprint bbox intersects the drawn box.
main(action="detail", fid=123) -> one building's per-release scores.
main(action="compare", a=0, b=6) -> how agreement changed between two releases.
"""
from common import BANDS, RELEASES, cache_path, connect_plain


def main(action: str = "area", bbox=None, fid: int = 0, a: int = 0, b: int = 0):
    if action == "area":
        return area_stats([float(v) for v in bbox])
    if action == "detail":
        return detail(int(fid))
    if action == "compare":
        box = [float(v) for v in bbox] if bbox else None
        return compare(int(a), int(b), box)
    raise ValueError(f"unknown action {action!r}")


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
