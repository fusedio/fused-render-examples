"""Tests for the local parquet index — build_index.py / index_store.py /
query_index.py. Network is fully mocked (discover._get_json is patched);
parquet parts and meta.json go to a pytest tmp dir. Run with:

    uv run --with pytest --with requests --with pyarrow --with duckdb pytest test_index.py -q
"""

import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import discover        # noqa: E402
import index_store as store  # noqa: E402
import build_index     # noqa: E402
import query_index     # noqa: E402

from test_discover import col  # noqa: E402  — same collection factory


@pytest.fixture
def tmp_index(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "INDEX_DIR", str(tmp_path))
    monkeypatch.setattr(store, "PARTS_DIR", str(tmp_path / "parts"))
    monkeypatch.setattr(store, "META_PATH", str(tmp_path / "meta.json"))
    return tmp_path


def install_http(monkeypatch, routes):
    """routes: {url_prefix: payload or callable(url)}; longest prefix wins."""
    def fake_get_json(url, timeout, params=None):
        for prefix in sorted(routes, key=len, reverse=True):
            if url.startswith(prefix):
                payload = routes[prefix]
                return payload(url) if callable(payload) else payload
        raise AssertionError(f"unexpected url {url}")
    monkeypatch.setattr(discover, "_get_json", fake_get_json)


# ----------------------------------------------------------------------------
# build — API source
# ----------------------------------------------------------------------------

def test_build_api_paginates(tmp_index, monkeypatch):
    base = "https://api"
    page2 = f"{base}/collections?page=2"
    install_http(monkeypatch, {
        f"{base}/collections?page=2": {"collections": [col("b", base=base)], "links": []},
        f"{base}/collections": {"collections": [col("a", base=base)],
                                "links": [{"rel": "next", "href": page2}]},
    })
    r = build_index.main(source=base)
    assert r["done"] is True and r["count"] == 2
    meta = store.read_meta()
    assert meta["sources"][r["slug"]]["status"] == "done"


def test_build_api_cursor_resumes(tmp_index, monkeypatch):
    base = "https://api"
    page2 = f"{base}/collections?page=2"
    install_http(monkeypatch, {
        f"{base}/collections?page=2": {"collections": [col("b", base=base)], "links": []},
        f"{base}/collections": {"collections": [col("a", base=base)],
                                "links": [{"rel": "next", "href": page2}]},
    })
    r1 = build_index.main(source=base)
    assert r1["done"] is True
    # a resumed call (cursor set) must append, not wipe what's already built
    r2 = build_index.main(source=base, cursor=page2)
    assert r2["done"] is True
    assert r2["count"] == r1["count"] + 1


def test_build_api_stops_on_repeated_next_link(tmp_index, monkeypatch):
    base = "https://api"
    install_http(monkeypatch, {
        # last page still advertises next -> itself; the crawler must stop
        f"{base}/collections": lambda url: {
            "collections": [col("a", base=base)],
            "links": [{"rel": "next", "href": url}]},
    })
    r = build_index.main(source=base)
    assert r["done"] is True and r["count"] == 1


def test_build_api_partial_failure_keeps_harvest(tmp_index, monkeypatch):
    base = "https://api"
    page2 = f"{base}/collections?page=2"

    def flaky(url):
        raise ConnectionError("502")
    install_http(monkeypatch, {
        page2: flaky,
        f"{base}/collections": {"collections": [col("a", base=base)],
                                "links": [{"rel": "next", "href": page2}]},
    })
    r = build_index.main(source=base)
    assert r["done"] is False and r["added"] == 1     # page 1 kept
    assert r["next_cursor"] == page2                  # resume at the failed page


def test_kind_hint_normalized_and_validated(tmp_index, monkeypatch):
    base = "https://api"
    install_http(monkeypatch, {f"{base}/collections": {"collections": [col("a", base=base)], "links": []}})
    build_index.main(source=f"{base}|kind=Raster")    # case-insensitive
    r = query_index.main(kind="raster")
    assert [c["id"] for c in r["collections"]] == ["a"]
    with pytest.raises(ValueError):
        build_index.main(source=f"{base}|kind=rastr")


def test_part_files_slug_is_not_a_prefix_match(tmp_index, monkeypatch):
    a, b = "https://host/api/stac", "https://host/api/stac/v1"
    for src in (a, b):
        install_http(monkeypatch, {f"{src}/collections": {"collections": [col("x", base=src)], "links": []},
                                   src: {}})
        build_index.main(source=src)
    slug_a = store.slugify(a)
    assert len(store.part_files(slug_a)) == 1         # must not see v1's parts
    store.drop_source(slug_a)
    assert len(store.part_files(store.slugify(b))) == 1   # v1 untouched


def test_build_error_recorded(tmp_index, monkeypatch):
    def boom(url):
        raise ConnectionError("nope")
    install_http(monkeypatch, {"https://down": boom})
    with pytest.raises(RuntimeError):
        build_index.main(source="https://down")
    meta = store.read_meta()
    slug = store.slugify("https://down")
    assert meta["sources"][slug]["status"] == "error"


# ----------------------------------------------------------------------------
# build — static catalog (Maxar-shaped)
# ----------------------------------------------------------------------------

def _never(url):
    raise AssertionError(f"must not be fetched: {url}")


def _static_routes(root):
    event = col("event-1", title="North India Floods", bbox=[88.0, 27.1, 88.7, 28.0],
                temporal=["2022-03-07T04:53:47Z", "2023-10-06T05:04:41Z"])
    event["type"] = "Collection"
    # a static collection's child links point at per-acquisition sub-collections
    # that must NOT be crawled
    event["links"] = [{"rel": "child", "href": "./ard/acq1_collection.json"}]
    return {
        root: {"type": "Catalog", "links": [
            {"rel": "child", "href": "./event-1/collection.json"},
            {"rel": "child", "href": "./sub/catalog.json"},
        ]},
        "https://static.example/events/event-1/collection.json": event,
        "https://static.example/events/sub/catalog.json": {"type": "Catalog", "links": []},
        "https://static.example/events/event-1/ard/": _never,
    }


def test_build_static_harvests_collections_only(tmp_index, monkeypatch):
    root = "https://static.example/events/catalog.json"
    install_http(monkeypatch, _static_routes(root))
    r = build_index.main(source=f"static:{root}|kind=raster")
    assert r["done"] is True and r["count"] == 1
    q = query_index.main(q="", bbox="80,20,95,30")
    assert q["total"] == 1
    c = q["collections"][0]
    assert c["id"] == "event-1"
    assert c["kind"] == "raster"          # the |kind= hint
    assert c["access"] == "static"
    assert c["self_href"] == "https://static.example/events/event-1/collection.json"


# ----------------------------------------------------------------------------
# build — CMR-shaped (root of provider children)
# ----------------------------------------------------------------------------

def test_build_collection_indexes_catalog_as_one_entry(tmp_index, monkeypatch):
    # a catalog with no Collection level (Umbra-shaped: date subcatalogs only)
    # is indexed as a single searchable dataset, not crawled into nothing
    root = "https://static.example/umbra/catalog.json"
    install_http(monkeypatch, {
        root: {"type": "Catalog", "id": "umbra-sar-open-data", "title": "Umbra Open SAR Data",
               "links": [{"rel": "child", "href": "./2024/catalog.json"}]},
    })
    r = build_index.main(source=f"collection:{root}|kind=raster")
    assert r["done"] is True and r["count"] == 1
    q = query_index.main(q="umbra")
    assert q["collections"][0]["id"] == "umbra-sar-open-data"
    assert q["collections"][0]["access"] == "static"
    assert q["collections"][0]["self_href"] == root


def test_build_cmr_expands_providers(tmp_index, monkeypatch):
    root = "https://cmr.example/stac"
    install_http(monkeypatch, {
        f"{root}/P1/collections": {"collections": [col("c1", title="MODIS Land Cover")], "links": []},
        f"{root}/P2/collections": {"collections": [col("c2", title="GEDI Biomass")], "links": []},
        f"{root}/": {"links": [
            {"rel": "child", "href": f"{root}/P1"},
            {"rel": "child", "href": f"{root}/P2"},
        ]},
    })
    r = build_index.main(source=f"cmr:{root}")
    assert r["done"] is True and r["count"] == 2


def test_build_cmr_skips_metadata_only_providers(tmp_index, monkeypatch):
    # a provider whose collections have no granules (like CMR's FEDEO) is dropped
    # wholesale, so the index isn't padded with datasets that can never show items
    root = "https://cmr.example/stac"
    install_http(monkeypatch, {
        f"{root}/P1/collections": {"collections": [col("c1", title="Has data")], "links": []},
        f"{root}/P2/collections": {"collections": [col("c2", title="Metadata only")], "links": []},
        # native collection search reports no granule-bearing collections anywhere
        "https://cmr.example/search/collections.json": {"feed": {"entry": []}},
        f"{root}/": {"links": [
            {"rel": "child", "href": f"{root}/P1"},
            {"rel": "child", "href": f"{root}/P2"},
        ]},
    })
    r = build_index.main(source=f"cmr:{root}")
    assert r["count"] == 0   # both providers skipped -- nothing to index


def test_build_excludes_collections_without_items(tmp_index, monkeypatch):
    base = "https://api"
    has = col("has-items", title="Foo dataset", base=base)     # advertises rel=items
    no = col("no-items", title="Foo dataset", base=base)
    no["links"] = [l for l in no["links"] if l.get("rel") != "items"]   # no items endpoint
    install_http(monkeypatch, {f"{base}/collections": {"collections": [no, has], "links": []}})
    build_index.main(source=base)
    r = query_index.main(q="foo dataset")
    ids = [c["id"] for c in r["collections"]]
    assert ids == ["has-items"]   # the dead end isn't indexed at all


# ----------------------------------------------------------------------------
# query
# ----------------------------------------------------------------------------

def _seed(tmp_index, monkeypatch, cols, source="https://api", kind_hint=""):
    install_http(monkeypatch, {f"{source}/collections": {"collections": cols, "links": []},
                               source: {"collections": cols, "links": []}})
    spec = source + (f"|kind={kind_hint}" if kind_hint else "")
    return build_index.main(source=spec)


def test_query_bbox_and_text(tmp_index, monkeypatch):
    _seed(tmp_index, monkeypatch, [
        col("india-lc", title="Land Cover India", bbox=[68, 8, 97, 35]),
        col("us-lc", title="Land Cover US", bbox=[-125, 24, -66, 50]),
        col("india-dem", title="Elevation India", bbox=[68, 8, 97, 35]),
    ])
    r = query_index.main(q="land cover for india")
    assert [c["id"] for c in r["collections"]] == ["india-lc"]
    assert r["place"]["name"] == "india"
    assert r["built"] is True and r["indexed"] == 3


def test_query_kind_filter(tmp_index, monkeypatch):
    raster = col("r1", title="Imagery mosaic")
    raster["item_assets"] = {"visual": {"type": "image/tiff; application=geotiff"}}
    vector = col("v1", title="Building footprints")
    vector["item_assets"] = {"data": {"type": "application/geo+json"}}
    _seed(tmp_index, monkeypatch, [raster, vector])
    r = query_index.main(kind="raster")
    assert [c["id"] for c in r["collections"]] == ["r1"]
    r = query_index.main(kind="vector")
    assert [c["id"] for c in r["collections"]] == ["v1"]


def test_query_no_extent_not_excluded(tmp_index, monkeypatch):
    _seed(tmp_index, monkeypatch, [col("nobox", title="Land cover global", bbox=None)])
    r = query_index.main(q="land cover", bbox="0,0,10,10")
    assert [c["id"] for c in r["collections"]] == ["nobox"]


def test_query_unbuilt_index(tmp_index):
    r = query_index.main(q="anything")
    assert r["built"] is False and r["collections"] == []


def test_query_regional_outranks_global(tmp_index, monkeypatch):
    _seed(tmp_index, monkeypatch, [
        col("global-lc", title="Land Cover", bbox=[-180, -90, 180, 90]),
        col("india-lc", title="Land Cover", bbox=[68, 8, 97, 35]),
    ])
    r = query_index.main(q="land cover in india")
    assert [c["id"] for c in r["collections"]] == ["india-lc", "global-lc"]


# ----------------------------------------------------------------------------
# codecs — metadata is full of →, ±, ° and non-Latin scripts; none of it may
# crash on Windows' default cp1252
# ----------------------------------------------------------------------------

UNICODE_DESC = "Rain → flood mapping ±0.5° for São Paulo / 東京 🌍"


def test_unicode_roundtrip_through_parquet(tmp_index, monkeypatch):
    c = col("uni", title="Flood → damage", description=UNICODE_DESC,
            keywords=["über", "地図"], bbox=[0, 0, 1, 1])
    _seed(tmp_index, monkeypatch, [c])
    r = query_index.main(q="flood")
    out = r["collections"][0]
    assert out["description"] == UNICODE_DESC
    assert out["keywords"] == ["über", "地図"]
    assert out["title"] == "Flood → damage"


def test_update_meta_preserves_other_sources(tmp_index):
    # parallel builds each update their own source under the lock; one writer
    # must never drop another's entry
    store.update_meta(lambda m: m["sources"].setdefault("a", {"count": 1}))
    store.update_meta(lambda m: m["sources"].setdefault("b", {"count": 2}))
    meta = store.read_meta()
    assert meta["sources"]["a"]["count"] == 1
    assert meta["sources"]["b"]["count"] == 2
    assert not os.path.exists(store.META_PATH + ".lock")   # lock released


def test_update_meta_steals_stale_lock(tmp_index):
    import time
    os.makedirs(store.INDEX_DIR, exist_ok=True)
    open(store.META_PATH + ".lock", "w").close()
    os.utime(store.META_PATH + ".lock", (time.time() - 120, time.time() - 120))
    store.update_meta(lambda m: m["sources"].setdefault("a", {"count": 1}))
    assert store.read_meta()["sources"]["a"]["count"] == 1


def test_unicode_meta_json(tmp_index):
    meta = {"sources": {"s": {"source": "https://x", "error": "failed → retry"}}}
    store.write_meta(meta)
    assert store.read_meta() == meta
    # the file itself must be readable as UTF-8 regardless of locale
    with open(store.META_PATH, encoding="utf-8") as f:
        json.load(f)


def test_utf8_stdio_survives_cp1252_stream(monkeypatch):
    buf = io.BytesIO()
    cp1252 = io.TextIOWrapper(buf, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", cp1252)
    discover._utf8_stdio()
    print(UNICODE_DESC)          # would raise UnicodeEncodeError without the guard
    sys.stdout.flush()
    assert b"flood mapping" in buf.getvalue()


def test_runtime_sources_are_pure_ascii():
    """fused-render's execution backend writes page sources to disk with the
    locale codec (cp1252 on Windows) -- one → in a .py file and every run dies
    with "'charmap' codec can't encode character" before main() even starts.
    Keep all runPython-executed sources ASCII; use \\uXXXX escapes for unicode
    string literals (same runtime value, safe source bytes)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for name in os.listdir(here):
        if not name.endswith(".py") or name.startswith("test_"):
            continue
        data = open(os.path.join(here, name), encoding="utf-8").read()
        bad = [(i, ch) for i, ch in enumerate(data) if ord(ch) > 127]
        assert not bad, f"{name} has non-ASCII source chars: {bad[:5]}"


def test_payloads_are_json_ascii_safe(tmp_index, monkeypatch):
    """CLI blocks print json.dumps(...) — default ensure_ascii keeps that pure
    ASCII, so even an unguarded cp1252 console can print it."""
    _seed(tmp_index, monkeypatch, [col("uni", title="→", description=UNICODE_DESC)])
    payload = json.dumps(query_index.main(q=""))
    payload.encode("ascii")  # must not raise
