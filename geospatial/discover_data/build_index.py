"""Build the local collection index -- one source per call, resumable.

Each call harvests one source for at most `budget` seconds, appends what it
got as a parquet part under ./data/index/parts, and returns a cursor when the
source isn't finished -- the page calls again with it until `done`. That keeps
every call safely under the 60 s runPython limit no matter how big the source.

Source spec grammar (one per line in the UI):

    https://host/stac                 a STAC API (/collections, paginated)
    static:https://.../catalog.json   a static STAC catalog -- crawled by
                                      following rel=child links; collections
                                      are harvested, their children are not
                                      descended into (Maxar's per-acquisition
                                      sub-collections stay out)
    cmr:https://cmr.earthdata.nasa.gov/stac
                                      NASA CMR-STAC -- expands the root's child
                                      providers, then harvests each provider
                                      like an API
    ...|kind=raster                   optional: kind for collections the
                                      classifier can't decide
"""

import json
import re
import time
from urllib.parse import urljoin, urlparse

import discover
import index_store as store


class _PartialCrawl(Exception):
    """A page fetch failed mid-source; carries what was harvested so far and
    the URL to resume from, so a transient upstream error costs one retry
    instead of the whole chunk."""

    def __init__(self, rows, resume_url, cause):
        super().__init__(str(cause))
        self.rows, self.resume_url, self.cause = rows, resume_url, cause

DEFAULT_SOURCES = discover.DEFAULT_APIS + [
    "static:https://maxar-opendata.s3.amazonaws.com/events/catalog.json|kind=raster",
    "collection:https://s3.us-west-2.amazonaws.com/umbra-open-data-catalog/stac/catalog.json|kind=raster",
    "cmr:https://cmr.earthdata.nasa.gov/stac",
]

_PAGE_LIMIT = 100      # per-page collection count (CMR clamps above this)
_MAX_FETCHES = 500     # cap on fetched documents per static-catalog source


def main(source: str = "", cursor: str = "", budget: float = 35.0, timeout: float = 12.0):
    """Harvest one source (resumably). Blank source returns the default list
    plus current index state, so the page can render the builder UI."""
    started = time.time()
    if not source.strip():
        return {"sources": DEFAULT_SOURCES, "meta": store.read_meta(),
                "index_dir": store.INDEX_DIR}

    spec = source.strip()
    kind_hint = ""
    if "|" in spec:
        spec, _, opts = spec.partition("|")
        spec = spec.strip()
        for opt in opts.split("|"):
            k, _, v = opt.partition("=")
            if k.strip() == "kind":
                kind_hint = v.strip().lower()
    if kind_hint not in ("", "raster", "vector"):
        raise ValueError(f"kind hint must be raster or vector, not {kind_hint!r}")

    slug = store.slugify(spec)
    fresh = not cursor.strip()
    if fresh:
        store.drop_source(slug)

    src_label = source.strip()

    def _begin(meta):
        entry = meta["sources"].setdefault(slug, {"source": src_label, "count": 0})
        if fresh:
            entry.update({"source": src_label, "count": 0, "error": None})
        entry["status"] = "building"
    store.update_meta(_begin)

    deadline = started + max(5.0, float(budget))
    ctx = dict(timeout=float(timeout), deadline=deadline, kind_hint=kind_hint,
               source=source.strip(), slug=slug,
               terms=discover.source_terms(store.source_url(spec)))
    try:
        if spec.startswith("collection:"):
            rows, next_cursor = _one_collection(spec[len("collection:"):], ctx)
        elif spec.startswith("static:"):
            rows, next_cursor = _crawl_static(spec[len("static:"):], cursor, ctx)
        elif spec.startswith("cmr:"):
            rows, next_cursor = _crawl_cmr(spec[len("cmr:"):], cursor, ctx)
        else:
            rows, next_cursor = _crawl_api(spec, cursor, ctx)
    except _PartialCrawl as p:
        # keep the harvest, hand back a resume point; the page retries
        rows, next_cursor = p.rows, p.resume_url
    except Exception as e:
        store.update_meta(lambda meta: meta["sources"][slug].update(
            {"status": "error", "error": f"{type(e).__name__}: {e}"}))
        raise RuntimeError(f"{spec}: {type(e).__name__}: {e}")

    # Don't index datasets with no data to show: whole providers of metadata-only
    # collections (FEDEO) are skipped upstream, but big providers like SCIOPS mix
    # in ~10k collections with zero granules -- drop those here so search only
    # ever returns collections you can actually pull items from.
    rows = [r for r in rows if r.get("has_items", True)]

    if rows:
        store.write_part(slug, rows)
    done = not next_cursor

    def _finish(meta):
        entry = meta["sources"][slug]
        entry["count"] = entry.get("count", 0) + len(rows)
        entry["status"] = "done" if done else "building"
        entry["updated"] = store.now_iso()
    meta = store.update_meta(_finish)
    entry = meta["sources"][slug]

    return {
        "source": source.strip(),
        "slug": slug,
        "added": len(rows),
        "count": entry["count"],
        "done": done,
        "next_cursor": next_cursor,
        "elapsed_ms": int((time.time() - started) * 1000),
        "meta": meta,
    }


# ---------- crawlers (each returns rows, next_cursor) ----------

def _crawl_api(base, cursor, ctx):
    base = base.rstrip("/")
    url = cursor or f"{base}/collections?limit={_PAGE_LIMIT}"
    rows = []
    while url and time.time() < ctx["deadline"]:
        try:
            payload = discover._get_json(url, ctx["timeout"])
        except Exception as e:
            if rows:  # keep this chunk's harvest; resume at the failed page
                raise _PartialCrawl(rows, url, e)
            raise
        if not isinstance(payload, dict):
            break
        page = [c for c in payload.get("collections", []) if isinstance(c, dict)]
        rows.extend(_row(col, base, "api", ctx) for col in page)
        nxt = next((l.get("href") for l in payload.get("links", [])
                    if l.get("rel") == "next" and l.get("href")), None)
        # guards against catalogs whose last page still links "next"
        if not page or not nxt or urljoin(url, nxt) == url:
            return rows, ""
        url = urljoin(url, nxt)
    return rows, url or ""


def _one_collection(url, ctx):
    """Index a whole STAC catalog/collection as a SINGLE dataset entry, instead
    of harvesting collections out of it. For catalogs that have no collection
    level at all -- Umbra partitions straight into Catalog > year > month > day >
    item, so the collection crawler walks thousands of date folders and finds
    nothing -- this is the right shape: one searchable "Umbra Open SAR Data"
    row whose captures load as items on demand (items.py crawls it as static)."""
    doc = discover._get_json(url, ctx["timeout"])
    return [_row(doc, url, "static", ctx, self_url=url)], ""


def _crawl_static(root, cursor, ctx):
    state = json.loads(cursor) if cursor else {"queue": [root], "fetched": 0, "seen": [], "title": ""}
    seen = set(state.get("seen", []))
    # the root catalog's own title is where a static catalog names its provider
    # ("Maxar ARD Open Data Catalog") -- carried across resumes in the cursor
    if state.get("title"):
        ctx["terms"] = discover.source_terms(root, state["title"])
    rows = []
    while state["queue"] and time.time() < ctx["deadline"] and state["fetched"] < _MAX_FETCHES:
        url = state["queue"].pop(0)
        if url in seen:  # child links can form diamonds or cycles
            continue
        seen.add(url)
        state["fetched"] += 1
        try:
            doc = discover._get_json(url, ctx["timeout"])
        except Exception:
            continue  # one broken child shouldn't sink the crawl
        if not isinstance(doc, dict):
            continue
        if doc.get("type") == "Collection" or ("extent" in doc and "links" in doc):
            _absolutize_links(doc, url)
            rows.append(_row(doc, root, "static", ctx, self_url=url))
        elif doc.get("type") == "Catalog" or "links" in doc:
            if url == root and not state.get("title"):
                state["title"] = doc.get("title") or doc.get("id") or ""
                ctx["terms"] = discover.source_terms(root, state["title"])
            for l in doc.get("links", []):
                if l.get("rel") == "child" and l.get("href"):
                    state["queue"].append(urljoin(url, l["href"]))
    state["seen"] = sorted(seen)
    next_cursor = json.dumps(state) if state["queue"] and state["fetched"] < _MAX_FETCHES else ""
    return rows, next_cursor


def _crawl_cmr(root, cursor, ctx):
    root = root.rstrip("/")
    if cursor:
        state = json.loads(cursor)
    else:
        landing = discover._get_json(root + "/", ctx["timeout"])
        providers = [l["href"].rstrip("/") for l in landing.get("links", [])
                     if l.get("rel") == "child" and l.get("href")]
        state = {"providers": providers, "url": ""}

    rows = []
    while time.time() < ctx["deadline"]:
        if not state["url"]:
            if not state["providers"]:
                break
            state["current"] = state["providers"].pop(0)
            state["url"] = f"{state['current']}/collections?limit={_PAGE_LIMIT}"
        # Each provider's granule-bearing collections, by concept id. Fetched once
        # per provider (memory only, never in the cursor). It does two jobs: an
        # empty set means a metadata-only provider (e.g. FEDEO -- all 346 of its
        # collections have zero granules, their STAC items always empty) which is
        # skipped whole; otherwise it flags each collection has_items so no-data
        # ones are dropped from the index. None = the check failed, so fall back
        # to trusting the items link (don't drop anything).
        if ctx.get("gset_for") != state["current"]:
            ctx["gset"] = _granule_set(root, state["current"], ctx["timeout"], ctx["deadline"])
            ctx["gset_for"] = state["current"]
        if ctx["gset"] is not None and not ctx["gset"]:
            state["url"] = ""          # metadata-only provider -- skip to the next
            continue
        # each CMR provider is its own sub-catalog (".../stac/LPCLOUD")
        ctx["terms"] = discover.source_terms(state["current"])
        try:
            got, nxt = _crawl_api(state["current"], state["url"], ctx)
        except _PartialCrawl as p:
            rows.extend(p.rows)
            state["url"] = p.resume_url  # retry this page on the next chunk
            break
        except Exception:
            nxt, got = "", []  # provider without a working /collections -- skip it
        rows.extend(got)
        state["url"] = nxt
    pending = state["url"] or state["providers"]
    return rows, (json.dumps(state) if pending else "")


_CONCEPT_RE = re.compile(r"/concepts/(C\d+-[^./?]+)")
_GSET_PAGES = 6          # cap: 6 x 2000 = up to 12k granule-bearing collections per provider


def _granule_set(root, provider_url, timeout, deadline):
    """The concept ids of a CMR provider's granule-bearing collections. CMR's
    STAC endpoint carries no granule signal, but its native collection search
    lists them with `has_granules=true`; the STAC collection's own links carry
    the matching concept id. Uses the compact `.json` representation (concept id
    is the entry `id`) rather than umm_json's full records. Returns the COMPLETE
    set on a fully-paginated success (empty = metadata-only provider), or None if
    the lookup was incomplete for any reason -- an error, the deadline, or more
    pages than the cap -- so the caller keeps the provider rather than dropping
    real datasets on a partial set."""
    name = provider_url.rstrip("/").split("/")[-1]
    u = urlparse(root)
    url = f"{u.scheme}://{u.netloc}/search/collections.json"
    ids = set()
    for page in range(1, _GSET_PAGES + 1):
        if time.time() >= deadline:
            return None            # out of budget -- don't trust a partial set
        try:
            payload = discover._get_json(url, timeout, params={
                "provider": name, "has_granules": "true", "page_size": 2000, "page_num": page})
        except Exception:
            return None            # any error -- keep the provider (fail-open)
        entries = (payload.get("feed") or {}).get("entry") or []
        for e in entries:
            cid = e.get("id")
            if cid:
                ids.add(cid)
        if len(entries) < 2000:
            return ids             # reached the last page -- the set is complete
    return None                    # more pages than the cap -- incomplete, fail-open


def _concept_id(col):
    for l in col.get("links", []) or []:
        m = _CONCEPT_RE.search(l.get("href", "") or "")
        if m:
            return m.group(1)
    return None


# ---------- row helpers ----------

def _row(col, base, access, ctx, self_url=None):
    norm = discover._normalize(col, base, ctx["terms"])
    norm["access"] = access
    if self_url:
        norm["self_href"] = self_url
    kind = store.classify_kind(col, hint=ctx["kind_hint"])
    return store.row_from_collection(norm, kind, ctx["source"], ctx["slug"],
                                     _has_items(col, norm, access, ctx.get("gset")))


def _has_items(col, norm, access, gset):
    """Whether the collection can actually yield items. Static catalogs are
    crawled, so True; a CMR collection is True only if it's in its provider's
    granule-bearing set; otherwise trust the advertised items endpoint."""
    if access == "static":
        return True
    if gset is not None:
        cid = _concept_id(col)
        if cid:
            return cid in gset
    return bool(norm.get("items_href"))


def _absolutize_links(doc, doc_url):
    for l in doc.get("links", []) or []:
        if l.get("href"):
            l["href"] = urljoin(doc_url, l["href"])


if __name__ == "__main__":
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SOURCES[0]
    cur = ""
    while True:
        r = main(source=src, cursor=cur)
        print(json.dumps({k: r[k] for k in ("slug", "added", "count", "done", "elapsed_ms")}))
        if r["done"]:
            break
        cur = r["next_cursor"]
