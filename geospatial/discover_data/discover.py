"""Self-federating STAC collection discovery.

`main()` fans a free-text query out to a list of STAC APIs in parallel and
merges their `/collections` results into one ranked list -- no server of our
own, no metadata store, always live against the upstream catalogs (the same
stateless, federated idea as developmentseed/stac-fastapi-collection-discovery,
reimplemented client-side so the list of catalogs is yours to edit).

Per catalog:
  * If its landing page advertises the Collection-Search + Free-Text
    conformance classes, the query is pushed down server-side (`?q=`), which
    matters for big catalogs (e.g. VEDA has ~250 collections).
  * Otherwise the catalog ignores `q`, so every collection is fetched
    (following `next` links) and filtered locally.
Either way every surviving collection is scored locally, so the merged list
has one consistent relevance order regardless of which path produced it.

Query understanding: stopwords ("dataset", "for", ...) are dropped, matching is
word-boundary (so "land" no longer hits "Finland"), and place names ("india",
"south asia") resolve against vendor/places.json into a bounding box that both
filters and spatially boosts results.
"""

import re
import json
import os
import sys
import time
import concurrent.futures as cf
from urllib.parse import urljoin

import requests


def _utf8_stdio():
    """Windows consoles and pipes default to cp1252, which can't encode text
    common in catalog metadata (arrows, degree signs, non-Latin names) -- a
    print would die with "'charmap' codec can't encode character". Force UTF-8
    with replacement so no print/traceback can crash an entrypoint. (Sources in
    this folder must themselves stay pure ASCII -- see test_index.py.)"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_utf8_stdio()

# Reliable public catalogs, spanning commercial cloud archives, NASA, and the
# eoAPI/MAAP reference deployments. Users can replace this list from the UI.
DEFAULT_APIS = [
    "https://planetarycomputer.microsoft.com/api/stac/v1",
    "https://earth-search.aws.element84.com/v1",
    "https://stac.maap-project.org",
    "https://stac.eoapi.dev",
    "https://openveda.cloud/api/stac",
]

_HEADERS = {"Accept": "application/json", "User-Agent": "fused-render-discover"}
_MAX_PAGES = 8  # cap pagination when fetching a whole catalog for local filtering

# Words that carry no signal in a dataset query ("land cover dataset for india"
# should match on "land cover", not on "dataset" or "for").
_STOPWORDS = {
    "a", "an", "and", "any", "are", "at", "be", "best", "by", "can", "data",
    "dataset", "datasets", "find", "for", "from", "get", "give", "i", "in",
    "is", "it", "looking", "me", "my", "need", "of", "on", "or", "over",
    "please", "show", "some", "that", "the", "this", "to", "want", "with",
}
_PREPS = {"for", "in", "of", "over", "near", "around", "at", "within", "across", "on"}

# Infrastructure words in a catalog URL that say nothing about the data.
_HOST_NOISE = {"com", "org", "net", "io", "gov", "edu", "co", "uk", "www", "s3",
               "amazonaws", "aws", "azure", "blob", "core", "windows", "api",
               "stac", "v1", "v2", "collections", "catalog", "json", "https", "http"}

_PLACES = None  # lazy-loaded vendor/places.json


def main(
    q: str = "",
    apis: str = "",
    bbox: str = "",
    datetime: str = "",
    limit: int = 40,
    per_api_timeout: float = 12.0,
    strict: bool = False,
):
    """Federated STAC collection search.

    q                free text; blank = browse every collection
    apis             comma/newline separated STAC API base URLs (blank = defaults)
    bbox             "west,south,east,north" spatial filter (optional)
    datetime         STAC interval, e.g. "2020-01-01/.." (optional)
    limit            max collections kept per catalog
    per_api_timeout  per-request timeout in seconds
    strict           if true, a failed upstream aborts with an error instead of
                     being skipped
    """
    started = time.time()
    bases = _parse_apis(apis) or list(DEFAULT_APIS)

    tokens, place, qbox, eff_bbox = resolve_query(q, bbox)
    # No re-injecting the raw query here: when a place consumed every term,
    # both code paths must do the same thing (a bbox browse), not one text
    # search and one browse.
    server_q = " ".join(tokens)
    qinterval = _parse_interval(datetime)

    args = dict(
        q=server_q,
        tokens=tokens,
        bbox=eff_bbox,
        datetime=datetime.strip(),
        qbox=qbox,
        qinterval=qinterval,
        limit=max(1, int(limit)),
        timeout=float(per_api_timeout),
    )

    collections = []
    sources = []
    with cf.ThreadPoolExecutor(max_workers=min(8, len(bases))) as ex:
        futures = {ex.submit(_search_one, base, args): base for base in bases}
        for fut in cf.as_completed(futures):
            base = futures[fut]
            try:
                cols, meta = fut.result()
            except Exception as e:  # a whole-catalog failure
                if strict:
                    raise RuntimeError(f"{base} failed: {type(e).__name__}: {e}")
                sources.append({"base": base, "host": _host(base), "ok": False,
                                "error": f"{type(e).__name__}: {e}"})
                continue
            collections.extend(cols)
            sources.append(meta)

    # One merged ranking. Re-score now that every catalog's results are pooled:
    # term weights (idf) can only be judged against the whole candidate set, and
    # they're what pull the collection matching the query's distinctive term to
    # the top. The spatial bonus computed per-catalog is preserved.
    if tokens:
        if collections:
            weights = idf_weights(collections, tokens)
            for c in collections:
                spatial = c["score"] - c["text_score"]
                c["text_score"] = _score_tokens(c, tokens, weights)
                c["score"] = c["text_score"] + spatial
        # collections that no field matched (text score 0) only survive if the
        # upstream server vouched for them.
        collections = [c for c in collections if c["text_score"] > 0 or c["server_matched"]]
    collections.sort(key=lambda c: (-c["score"], c["title"].lower()))

    sources.sort(key=lambda s: s["host"])
    return {
        "q": q.strip(),
        "place": place,
        "bbox_used": eff_bbox,
        "collections": collections,
        "sources": sources,
        "total": len(collections),
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def _search_one(base, args):
    base = base.rstrip("/")
    landing = _get_json(base + "/", args["timeout"])
    conf = landing.get("conformsTo", []) if isinstance(landing, dict) else []
    terms = source_terms(base, (landing.get("title") or landing.get("id") or "")
                         if isinstance(landing, dict) else "")
    supports_q = any("collection-search" in c for c in conf) and any(
        ("free-text" in c or "free_text" in c) for c in conf
    )

    number_matched = None
    raw = None
    server_matched = False
    if supports_q and args["q"]:
        params = {"q": args["q"], "limit": args["limit"]}
        if args["bbox"]:
            params["bbox"] = args["bbox"]
        if args["datetime"]:
            params["datetime"] = args["datetime"]
        try:
            payload = _get_json(base + "/collections", args["timeout"], params=params)
            raw = payload.get("collections", []) if isinstance(payload, dict) else []
            number_matched = payload.get("numberMatched") if isinstance(payload, dict) else None
            server_matched = True
        except Exception:
            raw = None  # server-side free-text choked -- fall back to local filtering

    if raw is None:
        # Catalog ignores q (or its free-text search failed) -- pull the whole
        # thing and filter locally.
        raw = _all_collections(base, args["timeout"], args["limit"])
        server_matched = False

    out = []
    for col in raw:
        if not isinstance(col, dict):
            continue
        norm = _normalize(col, base, terms)
        text_score = _score_tokens(norm, args["tokens"])
        if not server_matched:
            if args["tokens"] and text_score == 0:
                continue
            if args["qbox"] and not _bbox_overlap(args["qbox"], norm["bbox"]):
                continue
            if args["qinterval"] and not _interval_overlap(args["qinterval"], norm["temporal"]):
                continue
        norm["text_score"] = text_score
        # Geography refines text relevance; it must not rank a collection that
        # matched none of the query's words above one that did.
        bonus = _spatial_bonus(args["qbox"], norm["bbox"]) if (text_score or not args["tokens"]) else 0.0
        norm["score"] = text_score + bonus
        norm["server_matched"] = server_matched
        out.append(norm)

    # Keep the best `limit` by score, not the first `limit` in catalog order: a
    # catalog lists its collections in its own order, so capping as we go would
    # silently drop strong matches that happen to sit late in the listing (which
    # is exactly how "surface reflectance" searches lost the real datasets).
    if args["tokens"]:
        out.sort(key=lambda c: -c["score"])
    out = out[: args["limit"]]

    meta = {
        "base": base,
        "host": _host(base),
        "ok": True,
        "supports_q": supports_q,
        "returned": len(out),
        "number_matched": number_matched,
    }
    return out, meta


def _all_collections(base, timeout, limit):
    """Fetch every collection, following `next` links, bounded by _MAX_PAGES."""
    cols = []
    url = base + "/collections"
    params = {"limit": 1000}
    for _ in range(_MAX_PAGES):
        payload = _get_json(url, timeout, params=params)
        if not isinstance(payload, dict):
            break
        page = payload.get("collections", [])
        cols.extend(page)
        nxt = next((l.get("href") for l in payload.get("links", [])
                    if l.get("rel") == "next" and l.get("href")), None)
        # Enough to satisfy a browse (no query) -- matched filtering happens above.
        if not nxt or (not page) or len(cols) >= max(limit, 2000):
            break
        url, params = urljoin(url, nxt), None  # STAC allows relative next hrefs
    return cols


def source_terms(base, title=""):
    """Searchable identity of the catalog a collection came from.

    Many collections never name their own provider -- every Maxar Open Data
    event is titled after the disaster, so a search for "maxar" matched none of
    them. The provenance lives in the catalog URL and its root title, so make
    that searchable (at the weakest weight, below the collection's own text).
    """
    words = []
    m = re.match(r"https?://([^/]+)(/.*)?", base or "")
    host, path = (m.group(1), m.group(2) or "") if m else (base or "", "")
    for part in re.split(r"[^a-z0-9]+", (host + " " + path).lower()):
        if part and part not in _HOST_NOISE and not part.isdigit():
            words.append(part)
    for part in re.split(r"[^a-z0-9]+", (title or "").lower()):
        if part and part not in _HOST_NOISE:
            words.append(part)
    return " ".join(dict.fromkeys(words))


def _flat_bbox(b):
    """A STAC bbox is 4 numbers, or 6 with elevation: [w, s, zmin, e, n, zmax].
    Return the 2D [w, s, e, n] or None."""
    if not isinstance(b, list):
        return None
    if len(b) == 6:
        return [b[0], b[1], b[3], b[4]]
    return b if len(b) == 4 else None


def _normalize(col, base, terms=""):
    extent = col.get("extent", {}) or {}
    spatial = (extent.get("spatial", {}) or {}).get("bbox") or []
    temporal = (extent.get("temporal", {}) or {}).get("interval") or []
    bboxes = [f for f in (_flat_bbox(b) for b in spatial) if f][:24]
    bbox = bboxes[0] if bboxes else None
    interval = temporal[0] if temporal and isinstance(temporal[0], list) else [None, None]

    links = col.get("links", []) or []
    items_href = next((l.get("href") for l in links if l.get("rel") == "items"), None)
    self_href = next((l.get("href") for l in links if l.get("rel") == "self"), None)
    # Only a browser the source catalog itself advertises (first-party), if any.
    html_href = next((l.get("href") for l in links
                      if (l.get("type") or "").startswith("text/html")
                      and l.get("rel") in ("alternate", "via", "self")), None)

    desc = (col.get("description") or "").strip()
    keywords = col.get("keywords") or []
    if isinstance(keywords, str):  # a bare string would char-split downstream
        keywords = [keywords]
    return {
        "id": col.get("id", ""),
        "title": (col.get("title") or col.get("id") or "").strip(),
        "description": desc,
        "description_short": _shorten(desc, 280),
        "keywords": keywords,
        "license": col.get("license") or "",
        "providers": [p.get("name") for p in (col.get("providers") or []) if p.get("name")],
        "bbox": bbox,
        "bboxes": bboxes,
        "temporal": interval,
        "api": base,
        "api_host": _host(base),
        "source_terms": terms or source_terms(base),
        "access": "api",
        "items_href": items_href,
        "self_href": self_href or f"{base}/collections/{col.get('id', '')}",
        "html_href": html_href,
    }


# ---------- query understanding ----------

def _tokenize(q):
    return [t.strip(".,;:!?()[]\"'") for t in re.split(r"[\s,]+", (q or "").lower().strip())
            if t.strip(".,;:!?()[]\"'")]


def _places():
    global _PLACES
    if _PLACES is None:
        try:
            here = os.path.dirname(os.path.abspath(__file__))
        except NameError:  # some runners exec entrypoints without __file__
            here = os.getcwd()
        try:
            with open(os.path.join(here, "vendor", "places.json"), encoding="utf-8") as f:
                _PLACES = json.load(f)
        except (OSError, ValueError):
            _PLACES = {}
    return _PLACES


def _detect_place(tokens):
    """Longest place-name match in the token stream -> {name, bbox, span}."""
    places = _places()
    if not places:
        return None
    best = None
    for n in (4, 3, 2, 1):
        for i in range(0, len(tokens) - n + 1):
            name = " ".join(tokens[i:i + n])
            if name in places:
                best = {"name": name, "bbox": places[name], "span": (i, i + n)}
                break
        if best:
            break
    return best


def resolve_query(q, bbox):
    """One query-understanding step shared by the live and index paths:
    (tokens, place, qbox, bbox_used). An explicit bbox beats a detected place."""
    tokens, place = _extract_terms(q)
    qbox = _parse_bbox(bbox)
    if qbox is None and place:
        qbox = place["bbox"]
    return tokens, place, qbox, (",".join(str(float(v)) for v in qbox) if qbox else "")


def _extract_terms(q):
    """Query -> (scoring tokens, detected place). Drops the place words, a
    preposition in front of them ("for india"), and stopwords."""
    tokens = _tokenize(q)
    place = _detect_place(tokens)
    # Only trust a place that reads as one: after a preposition ("floods in
    # turkey") or trailing ("land cover india"). "georgia land cover" or
    # "amazon rainforest biomass" stay plain text queries.
    if place:
        i, j = place["span"]
        if not (j == len(tokens) or (i > 0 and tokens[i - 1] in _PREPS)):
            place = None
    if place:
        i, j = place["span"]
        if i > 0 and tokens[i - 1] in _PREPS:
            i -= 1
        tokens = tokens[:i] + tokens[j:]
    terms = [t for t in tokens if t not in _STOPWORDS]
    if not terms and not place:
        terms = tokens  # an all-stopword query still shouldn't match everything
    return terms, ({"name": place["name"], "bbox": place["bbox"]} if place else None)


# ---------- scoring & filtering ----------

def _stem(w):
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _words(text):
    """Word-boundary vocabulary of a text: whole tokens plus the alnum parts of
    hyphenated/underscored ones ("sentinel-2" yields sentinel-2, sentinel, 2)."""
    out = set()
    for w in re.split(r"[\s,;:()\[\]{}/|]+", (text or "").lower()):
        w = w.strip(".,;:!?\"'")
        if not w:
            continue
        out.add(_stem(w))
        for part in re.split(r"[^a-z0-9]+", w):
            if part:
                out.add(_stem(part))
    return out


def _score_tokens(norm, tokens, weights=None):
    if not tokens:
        return 0
    title_str = ((norm["title"] or "") + " " + (norm["id"] or "")).lower()
    title_words = _words(title_str)
    kw_words = _words(" ".join(norm["keywords"] or []))
    desc_words = _words(norm["description"] or "")
    # provenance: which catalog it came from, weakest signal of the four
    src_words = _words(norm.get("source_terms") or "")
    score = 0.0
    matched = 0
    for t in tokens:
        st = _stem(t)
        # each token scores from its single best field, not the sum of them: a
        # word repeated across title+keywords+description would otherwise pile
        # up 6 points and let a 2-of-3-terms match outrank a full-query one.
        if st in title_words:
            best = 3
        elif st in kw_words:
            best = 2
        elif st in desc_words:
            best = 1
        elif st in src_words:
            best = 1
        else:
            best = 0
        # main() passes per-term weights so a distinctive term (one that matches
        # few collections, e.g. "sentinel-2") counts for more than a common one.
        score += best * (weights.get(t, 1.0) if weights else 1.0)
        matched += best > 0
    # phrase bonus: the full query verbatim in the title
    phrase = " ".join(tokens)
    if len(tokens) > 1 and phrase in title_str:
        score += 2
    # coverage dominates: a collection matching every query term must beat one
    # racking up points on a subset ("surface reflectance" alone shouldn't beat
    # "sentinel-2 surface reflectance" matched in full). Squared so a 2-of-3
    # match (0.44x) falls clearly behind a complete one (1.0x).
    coverage = matched / len(tokens)
    return score * coverage * coverage


def idf_weights(collections, tokens):
    """Per-token weights from how rare a match is across the candidate set.

    A term matching few collections ("sentinel-2") is far more telling than one
    matching most of them ("surface", "reflectance"), so weighting by inverse
    match-frequency lets the single specific term outvote several generic ones --
    the fix for "sentinel-2 surface reflectance" returning MODIS first. Floored
    so a term present everywhere still contributes."""
    import math
    n = len(collections)
    df = {t: 0 for t in tokens}
    for c in collections:
        words = (_words((c["title"] or "") + " " + (c["id"] or ""))
                 | _words(" ".join(c["keywords"] or []))
                 | _words(c["description"] or "")
                 | _words(c.get("source_terms") or ""))
        for t in tokens:
            if _stem(t) in words:
                df[t] += 1
    return {t: max(0.3, math.log((n + 1) / (df[t] + 0.5))) for t in tokens}


def _lon_pieces(w, e):
    # STAC encodes an antimeridian-crossing bbox with west > east; split it
    # into its two non-wrapping pieces either side of 180, same idea as the
    # map's client-side splitAM (index.html), just for overlap/area math
    # instead of a draw.
    return [(w, 180.0), (-180.0, e)] if w > e else [(w, e)]


def _lon_span(w, e):
    return (e - w) if w <= e else (360.0 - w + e)


def _spatial_bonus(qbox, b):
    """How well a collection's extent fits the query box: up to +2 for covering
    it, plus up to +1 for being specific to it (a regional dataset outranks an
    equal-text global one)."""
    if not qbox or not b or len(b) < 4:
        return 0.0
    s, n = max(qbox[1], b[1]), min(qbox[3], b[3])
    if s >= n:
        return 0.0
    # Piece-pair sum, same idea as _bbox_overlap: the west/east pieces of a
    # crossing box never overlap each other, so summing each pair's overlap
    # can't double-count.
    lon_overlap = sum(max(0.0, min(qe, be) - max(qw, bw))
                      for qw, qe in _lon_pieces(qbox[0], qbox[2])
                      for bw, be in _lon_pieces(b[0], b[2]))
    if lon_overlap <= 0:
        return 0.0
    inter = lon_overlap * (n - s)
    qarea = max(1e-9, _lon_span(qbox[0], qbox[2]) * (qbox[3] - qbox[1]))
    carea = max(1e-9, _lon_span(b[0], b[2]) * (b[3] - b[1]))
    return round(2.0 * (inter / qarea) + min(1.0, qarea / carea), 3)


def _parse_bbox(bbox):
    if not bbox.strip():
        return None
    try:
        parts = [float(x) for x in re.split(r"[,\s]+", bbox.strip()) if x]
    except ValueError:
        return None
    return _flat_bbox(parts)


def _bbox_overlap(a, b):
    if not b or len(b) < 4:
        return True  # no spatial extent advertised -- don't exclude
    if a[3] < b[1] or a[1] > b[3]:
        return False  # latitude bands don't intersect
    return any(not (ae < bw or aw > be)
               for aw, ae in _lon_pieces(a[0], a[2])
               for bw, be in _lon_pieces(b[0], b[2]))


def _parse_interval(dt):
    if not dt.strip():
        return None
    s = dt.strip()
    if "/" in s:
        lo, hi = s.split("/", 1)
    else:
        lo, hi = s, s
    return (_to_ts(lo), _to_ts(hi))


def _interval_overlap(q, col):
    if not col:
        return True
    cs, ce = _to_ts(col[0]), _to_ts(col[1])
    qs, qe = q
    lo = max(x for x in (cs, qs) if x is not None) if (cs is not None or qs is not None) else None
    hi = min(x for x in (ce, qe) if x is not None) if (ce is not None or qe is not None) else None
    if lo is None or hi is None:
        return True  # an open-ended interval always overlaps
    return lo <= hi


def _to_ts(v):
    if not v or v in ("..", ""):
        return None
    from datetime import datetime as _dt, timezone as _tz
    txt = str(v).replace("Z", "+00:00")
    for cut in (txt, txt[:10]):
        try:
            d = _dt.fromisoformat(cut)
            if d.tzinfo is None:      # STAC datetimes are UTC; don't drift with local tz
                d = d.replace(tzinfo=_tz.utc)
            return d.timestamp()
        except ValueError:
            continue
    return None


# ---------- http & misc ----------

_SESSION = requests.Session()
# The default pool holds 10 connections per host, which every parallel fan-out
# here overruns -- and an evicted connection costs a fresh TLS handshake (~1.4 s
# to S3), so a crawl spends most of its time re-shaking hands with one host.
_SESSION.mount("https://", requests.adapters.HTTPAdapter(pool_maxsize=32))


def _get_json(url, timeout, params=None):
    r = _SESSION.get(url, headers=_HEADERS, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _parse_apis(apis):
    return [u.strip().rstrip("/") for u in re.split(r"[,\n]+", apis or "") if u.strip()]


def _host(url):
    m = re.match(r"https?://([^/]+)", url or "")
    return m.group(1) if m else url


def _shorten(text, n):
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "\u2026"


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(main(q="land cover dataset for india", limit=5), indent=2)[:3000])
