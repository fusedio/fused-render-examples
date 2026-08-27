"""Tests for discover.py — the federated STAC collection search.

Network is fully mocked: `discover._get_json` is monkeypatched with a fake
backend so tests are deterministic and offline. Run with:

    uv run --with pytest --with requests pytest test_discover.py -q
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import discover  # noqa: E402

CS = "https://api.stacspec.org/v1.0.0-rc.1/collection-search"
FT = "https://api.stacspec.org/v1.0.0-rc.1/collection-search#free-text"


# ----------------------------------------------------------------------------
# fake backend
# ----------------------------------------------------------------------------

def col(cid, title=None, description="", keywords=None, bbox=None, temporal=None,
        base="https://cat", extra_links=None):
    links = [
        {"rel": "self", "type": "application/json", "href": f"{base}/collections/{cid}"},
        {"rel": "items", "type": "application/geo+json", "href": f"{base}/collections/{cid}/items"},
    ]
    if extra_links:
        links += extra_links
    return {
        "id": cid, "type": "Collection",
        "title": title if title is not None else cid,
        "description": description,
        "keywords": keywords or [],
        "license": "CC0-1.0",
        "providers": [{"name": "Provider X"}],
        "extent": {
            "spatial": {"bbox": [bbox] if bbox else []},
            "temporal": {"interval": [temporal] if temporal else [[None, None]]},
        },
        "links": links,
    }


def install_backend(monkeypatch, catalogs):
    """catalogs: {base: {conformsTo, server, all, landing_error}}."""
    def _match(url):
        for base in catalogs:
            if url.startswith(base):
                return base
        raise AssertionError(f"unexpected url {url}")

    def fake_get_json(url, timeout, params=None):
        spec = catalogs[_match(url)]
        if "/collections" not in url:                      # landing page
            if spec.get("landing_error"):
                raise ConnectionError("landing unreachable")
            return {"conformsTo": spec.get("conformsTo", [])}
        if params and "q" in params:                       # server-side search
            srv = spec.get("server")
            if isinstance(srv, Exception):
                raise srv
            srv = srv or []
            return {"collections": srv, "numberMatched": len(srv), "links": []}
        return {"collections": spec.get("all", []), "links": []}  # fetch-all (local)

    monkeypatch.setattr(discover, "_get_json", fake_get_json)


# ----------------------------------------------------------------------------
# pure helpers
# ----------------------------------------------------------------------------

def test_tokenize():
    assert discover._tokenize(" Forest,  Biomass ") == ["forest", "biomass"]
    assert discover._tokenize("") == []
    assert discover._tokenize(None) == []


def test_score_tokens_weights():
    c = discover._normalize(col("x", title="Forest map", keywords=["biomass"],
                                description="about carbon"), "https://cat")
    # "forest" in title (3), "biomass" in keywords (2), "carbon" in desc (1)
    assert discover._score_tokens(c, ["forest"]) == 3
    assert discover._score_tokens(c, ["biomass"]) == 2
    assert discover._score_tokens(c, ["carbon"]) == 1
    assert discover._score_tokens(c, []) == 0


def test_score_phrase_bonus():
    c = discover._normalize(col("x", title="Sea Surface Temperature"), "https://cat")
    multi = discover._score_tokens(c, ["sea", "surface"])
    # both tokens hit the title (3+3) plus the verbatim phrase bonus (2)
    assert multi == 8


def test_score_field_is_max_not_sum():
    # a token in title AND keywords AND description scores from the best field
    # (title, 3), not their sum (6) -- stacking is what let partial matches win
    c = discover._normalize(col("x", title="reflectance", keywords=["reflectance"],
                                description="reflectance"), "https://cat")
    assert discover._score_tokens(c, ["reflectance"]) == 3


def test_full_query_match_beats_high_scoring_partial():
    # the reported bug: "sentinel-2 surface reflectance" returned a MODIS surface
    # reflectance collection above the real Sentinel-2 one. The MODIS title packs
    # both "surface" and "reflectance"; only Sentinel-2 also matches "sentinel-2".
    q = ["sentinel-2", "surface", "reflectance"]
    modis = discover._normalize(
        col("modis", title="MODIS Surface Reflectance Daily",
            keywords=["surface", "reflectance"]), "https://cat")
    s2 = discover._normalize(
        col("s2", title="Sentinel-2 Level-2A",
            description="Bottom-of-atmosphere surface reflectance."), "https://cat")
    assert discover._score_tokens(s2, q) > discover._score_tokens(modis, q)


def test_parse_bbox():
    assert discover._parse_bbox("-10, 20, 30, 40") == [-10.0, 20.0, 30.0, 40.0]
    assert discover._parse_bbox("1 2 3 4") == [1.0, 2.0, 3.0, 4.0]
    assert discover._parse_bbox("") is None
    assert discover._parse_bbox("1,2,3") is None
    assert discover._parse_bbox("a,b,c,d") is None


def test_bbox_overlap():
    q = [0, 0, 10, 10]
    assert discover._bbox_overlap(q, [5, 5, 15, 15]) is True
    assert discover._bbox_overlap(q, [20, 20, 30, 30]) is False
    assert discover._bbox_overlap(q, None) is True          # unknown extent isn't excluded
    assert discover._bbox_overlap(q, [1, 2]) is True


def test_bbox_overlap_across_the_antimeridian():
    # STAC encodes a crossing collection bbox with west > east (here: Fiji-ish,
    # 172.6E through 180 to -168.4E). A naive rectangle test sees west (172.6)
    # far east of a Pacific query box's east edge and wrongly excludes it.
    pacific = [172.6, -20, -168.4, -10]
    assert discover._bbox_overlap([170, -15, 175, -12], pacific) is True    # touches the west piece
    assert discover._bbox_overlap([-175, -15, -170, -12], pacific) is True  # touches the east piece
    assert discover._bbox_overlap([0, -15, 10, -12], pacific) is False      # nowhere near either piece
    # a crossing query box, symmetrically
    assert discover._bbox_overlap([170, -20, -170, -10], [172, -15, 178, -12]) is True


def test_interval_overlap():
    q = discover._parse_interval("2021-01-01/..")
    assert discover._interval_overlap(q, ["2020-01-01T00:00:00Z", None]) is True   # open end
    assert discover._interval_overlap(q, ["1990-01-01T00:00:00Z", "1991-01-01T00:00:00Z"]) is False
    assert discover._interval_overlap(q, None) is True
    assert discover._interval_overlap(("..", ".."), ["2000", "2001"]) is True


def test_to_ts():
    assert discover._to_ts("..") is None
    assert discover._to_ts("") is None
    assert discover._to_ts("2020-01-01") == discover._to_ts("2020-01-01T00:00:00Z")


def test_parse_apis():
    assert discover._parse_apis("https://a/, https://b/\nhttps://c") == \
        ["https://a", "https://b", "https://c"]
    assert discover._parse_apis("") == []


def test_host():
    assert discover._host("https://earth-search.aws.element84.com/v1") == "earth-search.aws.element84.com"
    assert discover._host("http://x.io/a/b") == "x.io"


def test_shorten():
    assert discover._shorten("a b   c", 100) == "a b c"
    out = discover._shorten("x" * 400, 50)
    assert len(out) <= 50 and out.endswith("…")


def test_normalize_extracts_fields():
    c = discover._normalize(
        col("cid", title="T", description="d " * 300, keywords=["k"],
            bbox=[1, 2, 3, 4], temporal=["2020-01-01T00:00:00Z", None], base="https://cat"),
        "https://cat")
    assert c["id"] == "cid" and c["title"] == "T"
    assert c["bbox"] == [1, 2, 3, 4]
    assert c["temporal"] == ["2020-01-01T00:00:00Z", None]
    assert c["items_href"] == "https://cat/collections/cid/items"
    assert c["self_href"] == "https://cat/collections/cid"
    assert c["api_host"] == "cat"
    assert len(c["description_short"]) <= 280


def test_normalize_html_href():
    with_html = discover._normalize(
        col("a", extra_links=[{"rel": "alternate", "type": "text/html", "href": "https://cat/a.html"}]),
        "https://cat")
    assert with_html["html_href"] == "https://cat/a.html"
    without = discover._normalize(col("b"), "https://cat")
    assert without["html_href"] is None


# ----------------------------------------------------------------------------
# main() — federation behaviour
# ----------------------------------------------------------------------------

def test_server_side_path(monkeypatch):
    base = "https://srv"
    install_backend(monkeypatch, {base: {
        "conformsTo": [CS, FT],
        "server": [col("a", title="foo one", base=base), col("b", title="foo two", base=base)],
    }})
    r = discover.main(q="foo", apis=base)
    assert r["total"] == 2
    src = r["sources"][0]
    assert src["ok"] and src["supports_q"] and src["returned"] == 2
    assert all(c["server_matched"] for c in r["collections"])


def test_local_filter_path(monkeypatch):
    base = "https://loc"
    install_backend(monkeypatch, {base: {
        "conformsTo": [],   # ignores q → fetch all, filter locally
        "all": [col("match", title="Foo dataset", base=base),
                col("nope", title="Unrelated", base=base)],
    }})
    r = discover.main(q="foo", apis=base)
    ids = [c["id"] for c in r["collections"]]
    assert ids == ["match"]
    assert r["sources"][0]["supports_q"] is False
    assert r["collections"][0]["server_matched"] is False


def test_server_failure_falls_back_to_local(monkeypatch):
    base = "https://flaky"
    install_backend(monkeypatch, {base: {
        "conformsTo": [CS, FT],                 # claims support...
        "server": RuntimeError("500 from free-text"),  # ...but server-side errors
        "all": [col("match", title="foo", base=base), col("x", title="bar", base=base)],
    }})
    r = discover.main(q="foo", apis=base)
    assert [c["id"] for c in r["collections"]] == ["match"]   # recovered via local path
    assert r["sources"][0]["ok"] is True
    assert r["sources"][0]["supports_q"] is True              # capability still reported
    assert r["collections"][0]["server_matched"] is False


def test_idf_reranks_distinctive_term_to_top(monkeypatch):
    # the reported bug, end to end: several collections carry the common terms
    # "surface reflectance"; only one carries the distinctive "sentinel-2" (and,
    # like the real collection, it never says "surface"). idf weighting must lift
    # the specific match above the generic ones.
    base = "https://srv"
    install_backend(monkeypatch, {base: {"conformsTo": [], "all": [
        col("modis-sr", title="MODIS Surface Reflectance", base=base),
        col("viirs-sr", title="VIIRS Surface Reflectance", base=base),
        col("landsat-sr", title="Landsat Surface Reflectance", base=base),
        col("s2", title="Sentinel-2 Level-2A", keywords=["reflectance"], base=base),
    ]}})
    r = discover.main(q="sentinel-2 surface reflectance", apis=base)
    assert r["collections"][0]["id"] == "s2"


def test_idf_weights_rare_over_common():
    cols = [discover._normalize(col("a", title="Surface Reflectance"), "https://c"),
            discover._normalize(col("b", title="Surface Reflectance"), "https://c"),
            discover._normalize(col("c", title="Sentinel-2 Reflectance"), "https://c")]
    w = discover.idf_weights(cols, ["sentinel-2", "surface", "reflectance"])
    assert w["sentinel-2"] > w["surface"] > w["reflectance"]


def test_strict_raises_on_failure(monkeypatch):
    install_backend(monkeypatch, {"https://down": {"landing_error": True}})
    with pytest.raises(RuntimeError):
        discover.main(q="foo", apis="https://down", strict=True)


def test_non_strict_skips_failure(monkeypatch):
    good, bad = "https://good", "https://down"
    install_backend(monkeypatch, {
        good: {"conformsTo": [], "all": [col("m", title="foo", base=good)]},
        bad: {"landing_error": True},
    })
    r = discover.main(q="foo", apis=f"{good},{bad}", strict=False)
    assert [c["id"] for c in r["collections"]] == ["m"]
    by_host = {s["host"]: s for s in r["sources"]}
    assert by_host["good"]["ok"] is True
    assert by_host["down"]["ok"] is False and by_host["down"]["error"]


def test_merge_and_rank_across_catalogs(monkeypatch):
    a, b = "https://a", "https://b"
    install_backend(monkeypatch, {
        a: {"conformsTo": [], "all": [col("low", title="unrelated", description="foo", base=a)]},
        b: {"conformsTo": [], "all": [col("high", title="foo foo", base=b)]},
    })
    r = discover.main(q="foo", apis=f"{a},{b}")
    assert [c["id"] for c in r["collections"]] == ["high", "low"]   # higher score first


def test_browse_mode_returns_all(monkeypatch):
    base = "https://loc"
    install_backend(monkeypatch, {base: {
        "conformsTo": [CS, FT],   # even a q-capable catalog is fetched-all when q is blank
        "all": [col("a", base=base), col("b", base=base)],
    }})
    r = discover.main(q="", apis=base)
    assert r["total"] == 2


def test_limit_caps_per_catalog(monkeypatch):
    base = "https://loc"
    install_backend(monkeypatch, {base: {
        "conformsTo": [],
        "all": [col(f"c{i}", title="foo", base=base) for i in range(5)],
    }})
    r = discover.main(q="foo", apis=base, limit=2)
    assert r["total"] == 2


def test_bbox_local_filter(monkeypatch):
    base = "https://loc"
    install_backend(monkeypatch, {base: {
        "conformsTo": [],
        "all": [col("in", bbox=[0, 0, 10, 10], base=base),
                col("out", bbox=[100, 100, 110, 110], base=base)],
    }})
    r = discover.main(q="", apis=base, bbox="5,5,6,6")
    assert [c["id"] for c in r["collections"]] == ["in"]


def test_datetime_local_filter(monkeypatch):
    base = "https://loc"
    install_backend(monkeypatch, {base: {
        "conformsTo": [],
        "all": [col("recent", temporal=["2020-01-01T00:00:00Z", None], base=base),
                col("old", temporal=["1990-01-01T00:00:00Z", "1991-01-01T00:00:00Z"], base=base)],
    }})
    r = discover.main(q="", apis=base, datetime="2021-01-01/..")
    assert [c["id"] for c in r["collections"]] == ["recent"]


def test_multiple_apis_from_param(monkeypatch):
    a, b = "https://a", "https://b"
    install_backend(monkeypatch, {
        a: {"conformsTo": [], "all": [col("fa", title="foo", base=a)]},
        b: {"conformsTo": [], "all": [col("fb", title="foo", base=b)]},
    })
    r = discover.main(q="foo", apis=f"{a}\n{b}")
    assert {s["host"] for s in r["sources"]} == {"a", "b"}
    assert {c["id"] for c in r["collections"]} == {"fa", "fb"}


def test_return_shape(monkeypatch):
    base = "https://loc"
    install_backend(monkeypatch, {base: {"conformsTo": [], "all": [col("a", title="foo", base=base)]}})
    r = discover.main(q="  foo  ", apis=base)
    assert r["q"] == "foo"
    assert r["place"] is None and r["bbox_used"] == ""
    assert isinstance(r["total"], int) and isinstance(r["elapsed_ms"], int)
    assert set(r) == {"q", "place", "bbox_used", "collections", "sources", "total", "elapsed_ms"}


# ----------------------------------------------------------------------------
# query understanding — stopwords, word boundaries, places
# ----------------------------------------------------------------------------

def test_word_boundary_no_substring_hits():
    c = discover._normalize(col("finland-dem", title="Finland Elevation Model",
                                description="A DEM covering Finland."), "https://cat")
    # "land" must not hit "Finland", "for" must not hit "forest"
    assert discover._score_tokens(c, ["land"]) == 0
    f = discover._normalize(col("forest", title="Forest biomass"), "https://cat")
    assert discover._score_tokens(f, ["for"]) == 0


def test_stemming_matches_plurals():
    c = discover._normalize(col("x", title="Flood extents"), "https://cat")
    assert discover._score_tokens(c, ["floods"]) == 3
    assert discover._score_tokens(c, ["extent"]) == 3


def test_hyphenated_words_match_parts():
    c = discover._normalize(col("s2", title="Sentinel-2 L2A"), "https://cat")
    assert discover._score_tokens(c, ["sentinel-2"]) == 3
    assert discover._score_tokens(c, ["sentinel"]) == 3


def test_extract_terms_stopwords_and_place():
    terms, place = discover._extract_terms("land cover dataset for india")
    assert terms == ["land", "cover"]
    assert place["name"] == "india" and len(place["bbox"]) == 4
    terms, place = discover._extract_terms("forest biomass")
    assert terms == ["forest", "biomass"] and place is None


def test_extract_terms_multiword_place():
    terms, place = discover._extract_terms("elevation over south asia")
    assert place["name"] == "south asia"
    assert terms == ["elevation"]


def test_stopwords_plus_place_becomes_pure_spatial():
    # "dataset for india" must not resurrect "dataset" as the only search term
    terms, place = discover._extract_terms("dataset for india")
    assert terms == [] and place["name"] == "india"


def test_place_only_query():
    terms, place = discover._extract_terms("india")
    assert terms == [] and place["name"] == "india"


def test_place_needs_preposition_or_trailing_position():
    # leading place words are part of the subject, not a location filter
    terms, place = discover._extract_terms("georgia land cover")
    assert place is None and terms == ["georgia", "land", "cover"]
    terms, place = discover._extract_terms("amazon rainforest biomass")
    assert place is None and "amazon" in terms
    # trailing still counts even without a preposition
    terms, place = discover._extract_terms("land cover india")
    assert place["name"] == "india" and terms == ["land", "cover"]


def test_source_terms_extracts_provider_drops_infra():
    t = discover.source_terms("https://maxar-opendata.s3.amazonaws.com/events/catalog.json",
                              "Maxar ARD Open Data Catalog")
    assert "maxar" in t and "opendata" in t and "ard" in t
    for noise in ("s3", "amazonaws", "com", "json"):
        assert noise not in t.split()
    assert "nasa" in discover.source_terms("https://cmr.earthdata.nasa.gov/stac/LPCLOUD")
    assert "lpcloud" in discover.source_terms("https://cmr.earthdata.nasa.gov/stac/LPCLOUD")


def test_provenance_makes_unnamed_collections_findable():
    # a Maxar event names the disaster, never its provider
    c = discover._normalize(col("India-Floods-Oct-2023", title="North India Floods"),
                            "https://maxar-opendata.s3.amazonaws.com/events/catalog.json",
                            discover.source_terms("https://maxar-opendata.s3.amazonaws.com/events",
                                                  "Maxar ARD Open Data Catalog"))
    assert discover._score_tokens(c, ["maxar"]) == 1     # findable...
    assert discover._score_tokens(c, ["floods"]) == 3    # ...but below its own text


def test_provenance_ranks_below_collection_text(monkeypatch):
    base = "https://maxar-opendata.s3.amazonaws.com"
    install_backend(monkeypatch, {base: {
        "conformsTo": [],
        "all": [col("event", title="Bay of Bengal Cyclone", base=base),
                col("about", title="MAXAR imagery of a flood", base=base)],
    }})
    r = discover.main(q="maxar", apis=base)
    ids = [c["id"] for c in r["collections"]]
    assert ids == ["about", "event"]   # both returned, real title match first


def test_flat_bbox_3d():
    assert discover._flat_bbox([-180, -90, 0, 180, 90, 8000]) == [-180, -90, 180, 90]
    assert discover._flat_bbox([1, 2, 3, 4]) == [1, 2, 3, 4]
    assert discover._flat_bbox([1, 2, 3]) is None
    assert discover._parse_bbox("-180,-90,0,180,90,8000") == [-180, -90, 180, 90]


def test_normalize_flattens_3d_bbox():
    c = discover._normalize(col("x", bbox=[-180, -90, 0, 180, 90, 8000]), "https://cat")
    assert c["bbox"] == [-180, -90, 180, 90]


def test_normalize_string_keywords():
    raw = col("x")
    raw["keywords"] = "land cover"
    c = discover._normalize(raw, "https://cat")
    assert c["keywords"] == ["land cover"]


def test_no_spatial_bonus_without_text_match(monkeypatch):
    base = "https://srv"
    zero = col("zero", title="Unrelated", bbox=[68, 8, 97, 35], base=base)
    hit = col("hit", title="Land Cover", bbox=None, base=base)
    install_backend(monkeypatch, {base: {"conformsTo": [CS, FT], "server": [zero, hit]}})
    r = discover.main(q="land cover for india", apis=base)
    by_id = {c["id"]: c for c in r["collections"]}
    assert by_id["zero"]["score"] == 0          # server-vouched but no text match
    assert by_id["hit"]["score"] > 0
    assert r["collections"][0]["id"] == "hit"


def test_spatial_bonus_prefers_regional():
    india = [68.0, 8.0, 97.0, 35.0]
    assert discover._spatial_bonus(india, india) > discover._spatial_bonus(india, [-180, -90, 180, 90])
    assert discover._spatial_bonus(india, [-125, 24, -66, 50]) == 0.0
    assert discover._spatial_bonus(None, india) == 0.0
    assert discover._spatial_bonus(india, None) == 0.0


def test_place_in_query_filters_spatially(monkeypatch):
    base = "https://loc"
    install_backend(monkeypatch, {base: {
        "conformsTo": [],
        "all": [col("india-lc", title="Land Cover India", bbox=[68, 8, 97, 35], base=base),
                col("us-lc", title="Land Cover US", bbox=[-125, 24, -66, 50], base=base)],
    }})
    r = discover.main(q="land cover for india", apis=base)
    assert [c["id"] for c in r["collections"]] == ["india-lc"]
    assert r["place"]["name"] == "india"
    assert r["bbox_used"]


def test_explicit_bbox_beats_place(monkeypatch):
    base = "https://loc"
    install_backend(monkeypatch, {base: {
        "conformsTo": [],
        "all": [col("us-lc", title="Land Cover US", bbox=[-125, 24, -66, 50], base=base)],
    }})
    r = discover.main(q="land cover for india", apis=base, bbox="-100,30,-90,40")
    assert [c["id"] for c in r["collections"]] == ["us-lc"]   # user's bbox wins
