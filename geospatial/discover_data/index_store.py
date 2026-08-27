"""Shared plumbing for the local collection index.

The index is a folder of parquet part-files (one or more per source) under
./data/index/parts, plus meta.json describing each source's build state.
Parquet instead of CSV/TSV so the query side (query_index.py) can run real
predicates -- bbox intersection, kind filter, token prefilter -- via duckdb
without parsing anything.
"""

import json
import os
import re
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_DIR = os.path.join(_HERE, "data", "index")
PARTS_DIR = os.path.join(INDEX_DIR, "parts")
META_PATH = os.path.join(INDEX_DIR, "meta.json")

# Media-type / wording signals for raster vs vector collections.
_RASTER_MEDIA = ("tiff", "geotiff", "cog", "jp2", "jpeg2000", "netcdf", "hdf",
                 "zarr", "grib", "cloud-optimized", "image/")
_VECTOR_MEDIA = ("geojson", "parquet", "flatgeobuf", "shapefile", "geopackage",
                 "gpkg", "mvt", "vnd.pmtiles", "csv")
# preview/companion assets say nothing about the data itself (every Planetary
# Computer collection carries an image/png thumbnail)
_SKIP_ASSETS = {"thumbnail", "rendered_preview", "preview", "tilejson", "overview"}
_SKIP_ROLES = {"thumbnail", "overview", "preview", "metadata", "legend"}
_RASTER_WORDS = {"imagery", "raster", "satellite", "radar", "sar", "dem", "dsm",
                 "dtm", "elevation", "multispectral", "hyperspectral", "landsat",
                 "sentinel", "modis", "viirs", "reflectance", "backscatter",
                 "mosaic", "grid", "gridded", "band", "bands", "optical", "ard"}
_VECTOR_WORDS = {"vector", "boundaries", "boundary", "buildings", "footprints",
                 "points", "polygons", "roads", "admin", "administrative",
                 "parcels", "places", "addresses", "network", "osm"}


def source_url(spec):
    """A source spec ("static:https://...|kind=raster") without prefix/options."""
    return re.sub(r"^(static|cmr|collection):", "", spec.split("|")[0].strip())


def slugify(source):
    s = re.sub(r"^https?://", "", source_url(source)).strip("/")
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:80] or "source"


# ---------- meta ----------

def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_meta():
    try:
        with open(META_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"sources": {}}


def write_meta(meta):
    os.makedirs(INDEX_DIR, exist_ok=True)
    tmp = META_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)
    os.replace(tmp, META_PATH)


# Derived from META_PATH at call time (not frozen at import), so it tracks any
# reassignment of META_PATH -- tests monkeypatch it, and it must never point the
# lock at a different meta.json than the one being written.
def _lock_path():
    return META_PATH + ".lock"


def _lock(timeout=20.0, stale=30.0):
    """Cross-process advisory lock on meta.json via an exclusively-created lock
    file. Parallel index builds each read-modify-write meta.json for their own
    source; without this, two writers racing lose each other's entry. Steals a
    lock older than `stale` seconds so a crashed builder can't wedge the index."""
    os.makedirs(INDEX_DIR, exist_ok=True)
    path = _lock_path()
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > stale:
                    os.remove(path)
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                raise TimeoutError("timed out waiting for the index meta lock")
            time.sleep(0.05)


def _unlock():
    try:
        os.remove(_lock_path())
    except OSError:
        pass


def update_meta(mutate):
    """Read meta, apply `mutate(meta)` in place, and write it back atomically
    under the cross-process lock; returns the mutated meta. This is the only
    safe way to touch meta.json while builds run in parallel."""
    _lock()
    try:
        meta = read_meta()
        mutate(meta)
        write_meta(meta)
        return meta
    finally:
        _unlock()


def part_files(slug=None):
    if not os.path.isdir(PARTS_DIR):
        return []
    names = sorted(os.listdir(PARTS_DIR))
    if slug is not None:
        # exact slug + numeric seq -- a bare prefix test would also match a
        # source whose slug merely extends this one (".../stac" vs ".../stac/v1")
        pat = re.compile(re.escape(slug) + r"-\d+\.parquet$")
        return [os.path.join(PARTS_DIR, n) for n in names if pat.fullmatch(n)]
    return [os.path.join(PARTS_DIR, n) for n in names if n.endswith(".parquet")]


def drop_source(slug):
    for p in part_files(slug):
        os.remove(p)


def write_part(slug, rows):
    """Append one parquet part for a source; returns the file name."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    os.makedirs(PARTS_DIR, exist_ok=True)
    seq = len(part_files(slug))
    name = f"{slug}-{seq:04d}.parquet"
    path = os.path.join(PARTS_DIR, name)
    table = pa.Table.from_pylist(rows, schema=_schema())
    tmp = path + ".tmp"
    pq.write_table(table, tmp)
    os.replace(tmp, path)
    return name


def _schema():
    import pyarrow as pa
    return pa.schema([
        ("id", pa.string()),
        ("title", pa.string()),
        ("description", pa.string()),
        ("keywords", pa.list_(pa.string())),
        ("license", pa.string()),
        ("providers", pa.list_(pa.string())),
        ("west", pa.float64()),
        ("south", pa.float64()),
        ("east", pa.float64()),
        ("north", pa.float64()),
        ("bboxes_json", pa.string()),
        ("t_start", pa.string()),
        ("t_end", pa.string()),
        ("api", pa.string()),
        ("api_host", pa.string()),
        ("source_terms", pa.string()),
        ("access", pa.string()),
        ("items_href", pa.string()),
        ("self_href", pa.string()),
        ("html_href", pa.string()),
        ("kind", pa.string()),
        ("source", pa.string()),
        ("slug", pa.string()),
        ("indexed_at", pa.string()),
    ])


# ---------- rows ----------

def classify_kind(col, hint=""):
    """raster / vector / unknown, from asset media types first, wording second."""
    types = []
    for assets in (col.get("item_assets"), col.get("assets")):
        if isinstance(assets, dict):
            for name, a in assets.items():
                if not isinstance(a, dict) or not a.get("type"):
                    continue
                if name.lower() in _SKIP_ASSETS or set(a.get("roles") or []) & _SKIP_ROLES:
                    continue
                types.append(str(a["type"]).lower())
    blob = " ".join(types)
    if any(m in blob for m in _RASTER_MEDIA):
        return "raster"
    if any(m in blob for m in _VECTOR_MEDIA):
        return "vector"
    if hint:
        return hint  # a stated source-level kind beats guessing from wording

    words = set(re.split(r"[^a-z0-9]+", " ".join([
        col.get("title") or "", col.get("description") or "",
        " ".join(col.get("keywords") or [])]).lower()))
    r = len(words & _RASTER_WORDS)
    v = len(words & _VECTOR_WORDS)
    if r > v:
        return "raster"
    if v > r:
        return "vector"
    return "unknown"


def row_from_collection(norm, kind, source, slug, has_items=True):
    """Flatten a discover.py-normalized collection into a parquet row. `has_items`
    is a transient build-time signal (build_index drops no-data rows before
    writing); it rides on the dict but is not part of the parquet schema."""
    b = norm["bbox"] or [None, None, None, None]
    t = norm["temporal"] or [None, None]
    return {
        "id": norm["id"],
        "title": norm["title"],
        "description": norm["description"],
        "keywords": [str(k) for k in norm["keywords"]],
        "license": norm["license"],
        "providers": norm["providers"],
        "west": _f(b[0]), "south": _f(b[1]), "east": _f(b[2]), "north": _f(b[3]),
        "bboxes_json": json.dumps(norm.get("bboxes") or []),
        "t_start": str(t[0]) if t[0] else None,
        "t_end": str(t[1]) if t[1] else None,
        "api": norm["api"],
        "api_host": norm["api_host"],
        "source_terms": norm.get("source_terms", ""),
        "access": norm.get("access", "api"),
        "items_href": norm["items_href"],
        "self_href": norm["self_href"],
        "html_href": norm["html_href"],
        "kind": kind,
        "source": source,
        "slug": slug,
        "has_items": bool(has_items),
        "indexed_at": now_iso(),
    }


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def collection_from_row(row):
    """Parquet row -> the same normalized shape discover.py returns."""
    from discover import _shorten
    bbox = None
    if row["west"] is not None and row["east"] is not None:
        bbox = [row["west"], row["south"], row["east"], row["north"]]
    bboxes = json.loads(row["bboxes_json"] or "[]")
    desc = row["description"] or ""
    return {
        "id": row["id"],
        "title": row["title"],
        "description": desc,
        "description_short": _shorten(desc, 280),
        "keywords": list(row["keywords"] or []),
        "license": row["license"] or "",
        "providers": list(row["providers"] or []),
        "bbox": bbox,
        "bboxes": bboxes or ([bbox] if bbox else []),
        "temporal": [row["t_start"], row["t_end"]],
        "api": row["api"],
        "api_host": row["api_host"],
        "source_terms": row.get("source_terms") or "",
        "access": row["access"] or "api",
        "items_href": row["items_href"],
        "self_href": row["self_href"],
        "html_href": row["html_href"],
        "kind": row["kind"] or "unknown",
    }
