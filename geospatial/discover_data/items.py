"""Items for one collection, with assets resolved to absolute URLs.

Two code paths, mirroring the `access` field the search results carry:

  * access == "api"    -- GET the collection's items_href with bbox / datetime /
                          limit; one standard /collections/{id}/items page.
  * access == "static" -- crawl self_href: rel=child links are walked to any
                          depth (a Maxar event is one level -- child acquisition
                          collections link rel=item directly; Umbra nests
                          Catalog > year > month > day > item), pruning a branch
                          against the bbox using its own extent, when it has one,
                          before descending into it.

Both paths return one page plus an opaque `cursor` to resume from. That matters
most for the static path: a Maxar event fans out to hundreds of items across its
acquisitions (433 for Cyclone Ditwah), and fetching them all to then keep `limit`
of them cost ~23 s. The crawl now walks children only until it has enough item
URLs to fill the page, fetches only those documents, and hands back the rest --
so the first page lands in a few seconds and later pages skip the walk entirely.
Ordering is per page, not global; a static catalog gives no way to sort hundreds
of items by date without fetching every one of them first.

Asset hrefs in static catalogs are relative ("./10300100E6747500-visual.tif"),
so every asset href is urljoin'd against the item's own resolved URL.

`viewable` marks assets the fused-render map template can open directly (its
geo_classify extension lists); companion assets (thumbnail, overview, metadata)
are still listed -- a thumbnail is a useful cheap preview -- just never viewable.
`auth` says what reaching the bytes takes: "none" for public HTTP, "azure-sas"
for storage the catalog will sign anonymously at click time (all of Planetary
Computer), and "" for an href no HTTP client can fetch at all, such as the
`s3://` URLs VEDA publishes. The UI needs the distinction to explain itself:
"needs signing" and "not fetchable over HTTP" are very different answers to
"why can't I open this?".
"""

import json
import time
import concurrent.futures as cf
from urllib.parse import urljoin

import discover
import index_store as store
import sign

_BUDGET = 40.0  # static crawls must stay under the 60 s runPython ceiling
# Acquisition collections are small (10-30 KB) and each fetch is latency-bound
# at ~0.4-0.7 s, and a batch runs concurrently -- so a batch costs the slowest
# member, not the sum, and every extra batch is another serial round trip. Start
# wide enough that a thin catalog does not round-trip, and double from there.
_CHILD_BATCH = 4
_WAVE = 8  # _fetch_all's worker count: the batch size that costs one round trip

# What the map template's geo_classify opens directly (its RASTER_EXT +
# VECTOR_EXT + tiled/columnar formats). NetCDF/HDF/GRIB and Zarr are left out on
# purpose: the template lists them, but a remote CF-NetCDF/HDF isn't a tiled
# georeferenced raster -- GDAL opens it with no CRS (HDF5 image driver) or falls
# back to the netCDF driver, which can't range-stream over HTTP and pulls the
# whole file. So they're non-viewable and offered as a download instead (see
# blockedReason in index.html). Bare .json is excluded here and only viewable
# with a geo+json media type -- most .json assets are metadata.
_VIEWABLE_EXT = (
    ".tif", ".tiff", ".cog", ".vrt", ".jp2", ".j2k", ".img", ".ntf",
    ".geojson", ".shp", ".gpkg", ".fgb", ".kml", ".gml",
    ".pmtiles", ".parquet", ".geoparquet",
)


def main(collection_json: str = "", bbox: str = "", datetime: str = "",
         limit: int = 12, cursor: str = ""):
    """Fetch one page of items for a collection (the search result object, as JSON)."""
    started = time.time()
    col = json.loads(collection_json)
    limit = max(1, min(int(limit), 200))
    qbox = discover._parse_bbox(bbox)
    qinterval = discover._parse_interval(datetime)
    resume = json.loads(cursor) if cursor.strip() else None

    if (col.get("access") or "api") == "static":
        items, matched, nxt = _static_items(
            col["self_href"], qbox, qinterval, limit, started + _BUDGET, resume)
    else:
        items, matched, nxt = _api_items(
            col["items_href"], bbox, datetime, limit, resume)

    return {
        "collection": col.get("id", ""),
        "access": col.get("access") or "api",
        "items": items,
        "count": len(items),
        "matched": matched,
        "cursor": json.dumps(nxt) if nxt else "",
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def _api_items(items_href, bbox, datetime, limit, resume):
    if resume:
        url, params = resume["next"], None
    else:
        url, params = items_href, {"limit": limit}
        if bbox.strip():
            params["bbox"] = bbox.strip()
        if datetime.strip():
            params["datetime"] = datetime.strip()
    payload = discover._get_json(url, 20.0, params=params)
    feats = payload.get("features", []) if isinstance(payload, dict) else []
    matched = payload.get("numberMatched") if isinstance(payload, dict) else None
    items = []
    # Keep every feature the server sent, even past `limit`: the cursor is its
    # own rel=next link, so truncating here would skip whatever a catalog that
    # ignores `limit` returned beyond it, with no way back to those items.
    for feat in feats:
        if not isinstance(feat, dict):
            continue
        self_url = next((l.get("href") for l in feat.get("links", [])
                         if l.get("rel") == "self" and l.get("href")), url)
        items.append(_item(feat, self_url))
    nxt = next((l["href"] for l in payload.get("links", []) or []
                if l.get("rel") == "next" and l.get("href")), "")
    return items, matched, {"next": urljoin(url, nxt)} if nxt else None


def _overlaps_bbox(doc, qbox):
    """True unless doc advertises its own extent and that extent provably
    misses qbox -- the same rule applied to every node in the tree, root
    included, so a query outside the whole catalog stops there instead of
    walking every child only to return nothing."""
    if not qbox:
        return True
    spatial = ((doc.get("extent") or {}).get("spatial") or {}).get("bbox") or []
    boxes = [b for b in (discover._flat_bbox(x) for x in spatial) if b]
    return not boxes or any(discover._bbox_overlap(qbox, b) for b in boxes)


def _static_items(self_href, qbox, qinterval, limit, deadline, resume):
    if resume:
        children, pending, seen = resume["children"], resume["items"], set(resume.get("seen", []))
    else:
        # Fetched directly, not through the fail-open _fetch_all below: a
        # timeout/HTTP error here means there is nothing to show at all, and
        # should surface as a real error rather than a silent empty page.
        doc = discover._get_json(self_href, 20.0)
        seen = {self_href}
        if not _overlaps_bbox(doc, qbox):
            return [], None, None   # the whole catalog's own extent misses qbox
        links = doc.get("links", []) or []
        # Deeper levels (rel=child of a child) are walked below, same as any
        # other child, so catalogs nested arbitrarily deep (Umbra: Catalog >
        # year > month > day > item) get expanded instead of assuming items
        # sit exactly one hop under self_href.
        children = [urljoin(self_href, l["href"]) for l in links
                    if l.get("rel") == "child" and l.get("href")]
        pending = [urljoin(self_href, l["href"]) for l in links
                   if l.get("rel") == "item" and l.get("href")]

    items, step = [], _CHILD_BATCH
    while len(items) < limit and time.time() < deadline:
        # walk the catalog tree only until the page can be filled
        while len(pending) < limit and children and time.time() < deadline:
            batch, children = children[:step], children[step:]
            step = min(step * 2, 16)
            batch = [u for u in batch if u not in seen]
            seen.update(batch)
            for url, child in _fetch_all(batch, deadline):
                if not _overlaps_bbox(child, qbox):
                    continue  # this branch never touches the query area
                links = child.get("links", []) or []
                pending.extend(urljoin(url, l["href"]) for l in links
                               if l.get("rel") == "item" and l.get("href"))
                children.extend(urljoin(url, l["href"]) for l in links
                                if l.get("rel") == "child" and l.get("href"))
        if not pending:
            break
        # Floor each batch at one full parallel wave. Asking for exactly the
        # shortfall degenerates into 12, 6, 3, 2, 1 ... separate round trips
        # once the filters start rejecting, and a wave of 8 costs the same wall
        # time as a wave of 1. Overshooting `limit` by less than a wave is fine;
        # dropping the surplus would lose items the cursor has already passed.
        take = min(max(limit - len(items), _WAVE), len(pending))
        batch, pending = pending[:take], pending[take:]
        for url, feat in _fetch_all(batch, deadline):
            it = _item(feat, url)
            if qbox and it["bbox"] and not discover._bbox_overlap(qbox, it["bbox"]):
                continue
            if qinterval and it["datetime"] and not discover._interval_overlap(
                    qinterval, [it["datetime"], it["datetime"]]):
                continue
            items.append(it)

    items.sort(key=lambda i: i["datetime"] or "", reverse=True)
    nxt = ({"children": children, "items": pending, "seen": sorted(seen)}
           if (children or pending) else None)
    return items, None, nxt


def _fetch_all(urls, deadline):
    """Fetch URLs in parallel, yielding (url, doc); a failed or slow fetch is
    dropped rather than sinking the whole call, and the deadline stops the
    batch early instead of blowing the runPython ceiling."""
    left = deadline - time.time()
    if not urls or left <= 0:
        return
    # Bound each fetch by the remaining budget, not a flat 15 s: shutdown()
    # below returns at once but the interpreter still joins running workers at
    # exit, so a straggler outliving the deadline would hold the subprocess open
    # past the 60 s ceiling and fail a call whose page had already been built.
    ex = cf.ThreadPoolExecutor(max_workers=min(_WAVE, len(urls)))
    futures = {ex.submit(discover._get_json, u, min(15.0, left)): u for u in urls}
    try:
        for fut in cf.as_completed(futures, timeout=left):
            try:
                doc = fut.result()
            except Exception:
                continue
            if isinstance(doc, dict):
                yield futures[fut], doc
    except cf.TimeoutError:
        pass
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


def _item(feat, item_url):
    props = feat.get("properties", {}) or {}
    assets = []
    for key, a in (feat.get("assets") or {}).items():
        if not isinstance(a, dict) or not a.get("href"):
            continue
        href = urljoin(item_url, a["href"])
        if href.lower().startswith("s3://") and _bucket_public(href):
            href = _s3_to_https(href)   # public bucket -> streamable https COG
        auth = sign.scheme_for(href)
        view = _viewable(key, a, href)
        assets.append({
            "key": key,
            "title": a.get("title") or "",
            "type": a.get("type") or "",
            "roles": list(a.get("roles") or []),
            "href": href,
            "size": a.get("file:size"),
            "auth": auth,
            "viewable": view,
            "reason": _block_reason(href, a.get("type") or "", auth, view),
        })
    return {
        "id": feat.get("id", ""),
        "bbox": discover._flat_bbox(feat.get("bbox")),
        "datetime": props.get("datetime") or props.get("start_datetime") or "",
        "assets": assets,
    }


def _viewable(key, asset, href):
    if not href.lower().startswith(("http://", "https://")):
        return False  # the map template streams http(s) only (s3:// stays a link)
    if key.lower() in store._SKIP_ASSETS or set(asset.get("roles") or []) & store._SKIP_ROLES:
        return False
    path = href.split("?", 1)[0].lower()
    if path.endswith(".json"):
        return "geo+json" in (asset.get("type") or "").lower()
    return path.endswith(_VIEWABLE_EXT)


# Some catalogs publish s3:// hrefs for PUBLIC buckets (Maxar Open Data's
# maxar-opendata, the Sentinel/Landsat open buckets). Those objects stream fine
# over https, so rewrite them to the virtual-hosted URL and let them open on the
# map like any COG. A private bucket (VEDA's veda-data-store) answers the same
# probe with 403 and stays an unfetchable s3:// link. Probed once per bucket.
_S3_PUBLIC = {}


def _s3_to_https(href):
    bucket, _, key = href[len("s3://"):].partition("/")
    return f"https://{bucket}.s3.amazonaws.com/{key}"


def _bucket_public(href):
    bucket = href[len("s3://"):].partition("/")[0]
    if bucket not in _S3_PUBLIC:
        _S3_PUBLIC[bucket] = _probe_public(_s3_to_https(href))
    return _S3_PUBLIC[bucket]


def _probe_public(url):
    import requests
    try:
        return requests.get(url, headers={"Range": "bytes=0-0"}, timeout=6).status_code in (200, 206)
    except requests.RequestException:
        return False


# Gridded/multidimensional formats (NetCDF/HDF/GRIB) are download-to-open-locally,
# not map-streamable: a remote one isn't a tiled georeferenced raster (GDAL opens
# it with no CRS, or the netCDF driver pulls the whole file). Listed next to
# _VIEWABLE_EXT so the format rules live in one place.
_GRID_EXT = (".nc", ".nc4", ".cdf", ".hdf", ".hdf5", ".h5", ".he5",
             ".grb", ".grb2", ".grib", ".grib2")


def _is_grid(href, media_type):
    path = href.split("?", 1)[0].lower()
    return path.endswith(_GRID_EXT) or any(
        k in media_type.lower() for k in ("netcdf", "x-hdf", "hdf5", "grib"))


def _block_reason(href, media_type, auth, viewable):
    """Why an asset can't open on the map (empty string if it can). Returned to
    the UI verbatim, so it lives here next to the viewability rules rather than
    being re-derived on the client from a parallel format list."""
    if auth == "":
        scheme = href.split(":", 1)[0]
        return (f"not fetchable over HTTP ({scheme}://) \u2014 "
                "the catalog keeps this one private")
    if viewable:
        return ""
    if _is_grid(href, media_type):
        return ("NetCDF/HDF can't be streamed into the map \u2014 open it by "
                "downloading the whole file (\u2193), which may be large")
    return "the map template can't read this format"


if __name__ == "__main__":
    col = {"id": "demo", "access": "static", "items_href": None,
           "self_href": "https://maxar-opendata.s3.amazonaws.com/events/"
                        "BayofBengal-Cyclone-Mocha-May-23/collection.json"}
    r = main(collection_json=json.dumps(col), limit=5)
    print(json.dumps({k: r[k] for k in ("count", "matched", "elapsed_ms")}),
          "more" if r["cursor"] else "end")
    for it in r["items"]:
        print(it["id"], it["datetime"], sum(a["viewable"] for a in it["assets"]), "viewable")
