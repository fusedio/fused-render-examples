"""Build the example's PMTiles archives with DuckDB's native MVT encoder.

philly_iou.pmtiles  z10-15, one feature per city building, carrying
                    fid + i0..i6 (per-release IoU) so switching release never
                    refetches tiles.
"""
import math
import os
from concurrent.futures import ThreadPoolExecutor

from common import (
    PHILLY_BOUNDS,
    RELEASES,
    cache_path,
    connect_duckdb,
    release_key,
)
from pmtiles_writer import write_pmtiles

EXTENT = 4096
BUFFER = 64
PHILLY_ZOOMS = range(10, 16)
# Overture geometry is an inspection layer: z15 only, overzoomed above.
OVERTURE_ZOOMS = range(15, 16)
# Zoomed out, every building that ever mismatched is kept - those are the whole
# point - and the largest footprints fill in the city's shape around them.
RANK_CAPS = {10: 12000, 11: 30000, 12: 90000, 13: 260000}
PROBLEM_IOU = 0.5

ORIGIN = 20037508.342789244


def _tile_bounds_3857(z, x, y):
    span = 2 * ORIGIN / (1 << z)
    return (
        -ORIGIN + x * span,
        ORIGIN - (y + 1) * span,
        -ORIGIN + (x + 1) * span,
        ORIGIN - y * span,
    )


def _tile_bounds_4326(z, x, y):
    n = 1 << z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return west, south, east, north


def _lonlat_to_tile(lon, lat, z):
    n = 1 << z
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def _tiles_for(bounds, zooms):
    minx, miny, maxx, maxy = bounds
    for z in zooms:
        x0, y0 = _lonlat_to_tile(minx, maxy, z)
        x1, y1 = _lonlat_to_tile(maxx, miny, z)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                yield z, x, y


def _render_tiles(con, table, columns, zooms, use_rank_caps, lonlat=False):
    tiles = {}
    todo = list(_tiles_for(PHILLY_BOUNDS, zooms))

    def render(zxy):
        z, x, y = zxy
        minx, miny, maxx, maxy = _tile_bounds_3857(z, x, y)
        pad = (maxx - minx) * BUFFER / EXTENT
        cap = RANK_CAPS.get(z) if use_rank_caps else None
        rank_filter = f"AND (problem OR area_rank <= {cap})" if cap else ""
        if lonlat:
            w, s_, e, n = _tile_bounds_4326(z, x, y)
            dx = (e - w) * BUFFER / EXTENT
            dy = (n - s_) * BUFFER / EXTENT
            where = (f"lon1 >= {w - dx} AND lon0 <= {e + dx} "
                     f"AND lat1 >= {s_ - dy} AND lat0 <= {n + dy}")
        else:
            where = (f"bmaxx >= {minx - pad} AND bminx <= {maxx + pad} "
                     f"AND bmaxy >= {miny - pad} AND bminy <= {maxy + pad}")
        cursor = con.cursor()
        row = cursor.execute(f"""
            SELECT ST_AsMVT(t, 'buildings') FROM (
                SELECT {columns},
                       ST_AsMVTGeom(
                           g,
                           ST_Extent(ST_TileEnvelope({z}, {x}, {y})),
                           {EXTENT}, {BUFFER}, true
                       ) AS geom
                FROM {table}
                WHERE {where} {rank_filter}
            ) t WHERE geom IS NOT NULL
        """).fetchone()
        cursor.close()
        if row and row[0] and len(row[0]) > 20:
            tiles[(z, x, y)] = bytes(row[0])

    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 4)) as pool:
        list(pool.map(render, todo))
    return tiles


def _materialize_philly(con):
    stats = cache_path("stats.parquet")
    iou_cols = ", ".join(f"i{index}" for index in range(len(RELEASES)))
    least_iou = "least(" + ", ".join(f"i{i}" for i in range(len(RELEASES))) + ")"
    con.execute(f"""
        CREATE TABLE philly AS
        SELECT s.fid, {iou_cols},
               {least_iou} < {PROBLEM_IOU} AS problem,
               ST_Transform(ST_GeomFromWKB(b.wkb), 'EPSG:4326', 'EPSG:3857', always_xy := true) AS g,
               row_number() OVER (ORDER BY s.area DESC) AS area_rank
        FROM read_parquet('{_sql(stats)}') s
        JOIN read_parquet('{_sql(cache_path("philly_base.parquet"))}') b USING (fid)
    """)
    con.execute("""
        ALTER TABLE philly ADD COLUMN bminx DOUBLE;
        ALTER TABLE philly ADD COLUMN bminy DOUBLE;
        ALTER TABLE philly ADD COLUMN bmaxx DOUBLE;
        ALTER TABLE philly ADD COLUMN bmaxy DOUBLE;
        UPDATE philly SET bminx = ST_XMin(g), bminy = ST_YMin(g),
                          bmaxx = ST_XMax(g), bmaxy = ST_YMax(g);
    """)


def build_philly(progress=None):
    out = cache_path("philly_iou.pmtiles")
    if os.path.exists(out):
        return
    con = connect_duckdb()
    _materialize_philly(con)
    if progress:
        progress(10, "city tiles")
    iou_cols = ", ".join(
        f"i{index}::DOUBLE AS i{index}" for index in range(len(RELEASES))
    )
    tiles = _render_tiles(
        con, "philly", f"fid::BIGINT AS fid, {iou_cols}",
        PHILLY_ZOOMS, use_rank_caps=True,
    )
    con.close()
    fields = {"fid": "Number"}
    fields.update({f"i{index}": "Number" for index in range(len(RELEASES))})
    write_pmtiles(
        out + ".tmp", tiles, bounds=PHILLY_BOUNDS,
        minzoom=min(PHILLY_ZOOMS), maxzoom=max(PHILLY_ZOOMS),
        metadata={"vector_layers": [{
            "id": "buildings", "fields": fields,
            "minzoom": min(PHILLY_ZOOMS), "maxzoom": max(PHILLY_ZOOMS),
        }]},
    )
    os.replace(out + ".tmp", out)


def build_overture(release):
    """One release's own footprints, z15 only (the viewer draws them from street
    level and MapLibre overzooms above that). The parquet already carries each
    footprint's bounds, so tiles filter on plain numbers and DuckDB skips whole
    row groups instead of decoding geometry it will throw away."""
    out = cache_path(f"overture_{release_key(release)}.pmtiles")
    if os.path.exists(out):
        return False
    parquet = _sql(cache_path(f"overture_{release_key(release)}.parquet"))
    con = connect_duckdb()
    con.execute(f"""
        CREATE TABLE ov AS
        SELECT gers_id,
               ST_Transform(ST_GeomFromWKB(wkb), 'EPSG:4326', 'EPSG:3857',
                            always_xy := true) AS g,
               ST_XMin(ST_GeomFromWKB(wkb)) AS lon0,
               ST_XMax(ST_GeomFromWKB(wkb)) AS lon1,
               ST_YMin(ST_GeomFromWKB(wkb)) AS lat0,
               ST_YMax(ST_GeomFromWKB(wkb)) AS lat1
        FROM read_parquet('{parquet}')
        WHERE maxx >= {PHILLY_BOUNDS[0]} AND minx <= {PHILLY_BOUNDS[2]}
          AND maxy >= {PHILLY_BOUNDS[1]} AND miny <= {PHILLY_BOUNDS[3]}
    """)
    tiles = _render_tiles(con, "ov", "gers_id::VARCHAR AS gers_id",
                          OVERTURE_ZOOMS, use_rank_caps=False, lonlat=True)
    con.close()
    write_pmtiles(
        out + ".tmp", tiles, bounds=PHILLY_BOUNDS,
        minzoom=min(OVERTURE_ZOOMS), maxzoom=max(OVERTURE_ZOOMS),
        metadata={"vector_layers": [{
            "id": "buildings", "fields": {"gers_id": "String"},
            "minzoom": min(OVERTURE_ZOOMS), "maxzoom": max(OVERTURE_ZOOMS),
        }]},
    )
    os.replace(out + ".tmp", out)
    return True


def available_outlines():
    return [
        release for release in RELEASES
        if os.path.exists(cache_path(f"overture_{release_key(release)}.pmtiles"))
    ]


def build_all(progress=None):
    """The city scorecard tileset is what the app needs to open. The per-release
    Overture tilesets are an inspection extra, so they come last - the caller
    can report them separately."""
    if progress:
        progress(5, "city scorecard tiles")
    build_philly(progress)
    total = len(RELEASES)
    for index, release in enumerate(RELEASES):
        build_overture(release)
        if progress:
            progress(20 + (index + 1) / total * 80, f"Overture geometry {release}")
    if progress:
        progress(100, "ready")


def _sql(path):
    return path.replace("\\", "/").replace("'", "''")


if __name__ == "__main__":
    build_all(progress=lambda pct, note: print(f"{pct:5.1f}%  {note}", flush=True))
