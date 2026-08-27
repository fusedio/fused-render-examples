"""Tests for items.py (per-collection items with resolved assets) and
download.py (stream one asset to disk). Network is fully mocked, same style as
test_discover.py."""

import json
import os
import sys
import time
from urllib.parse import quote

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import discover  # noqa: E402
import download  # noqa: E402
import items  # noqa: E402
import resolve_dir  # noqa: E402
import sign  # noqa: E402

B = chr(92)
Q = chr(34)

TIFF = "image/tiff; application=geotiff; profile=cloud-optimized"


def collection(access="api", base="https://cat"):
    return json.dumps({
        "id": "c1",
        "access": access,
        "items_href": f"{base}/collections/c1/items",
        "self_href": f"{base}/collections/c1" if access == "api"
        else "https://ex/events/E1/collection.json",
    })


def install(monkeypatch, docs):
    """docs: url -> document (or a callable receiving params). Records calls."""
    calls = []

    def fake_get_json(url, timeout, params=None):
        calls.append((url, params))
        doc = docs[url]
        return doc(params) if callable(doc) else doc

    monkeypatch.setattr(discover, "_get_json", fake_get_json)
    # default: no s3 bucket is probed as public (keeps tests off the network);
    # a test that wants the public-rewrite path overrides this after install().
    monkeypatch.setattr(items, "_probe_public", lambda url: False)
    items._S3_PUBLIC.clear()
    return calls


# ----------------------------------------------------------------------------
# api access
# ----------------------------------------------------------------------------

def api_feature():
    return {
        "id": "S1", "bbox": [1, 2, 3, 4],
        "properties": {"datetime": "2023-05-22T04:35:08Z"},
        "links": [{"rel": "self", "href": "https://cat/collections/c1/items/S1"}],
        "assets": {
            "visual": {"href": "https://data/abs-visual.tif", "type": TIFF, "roles": ["data"]},
            "relative": {"href": "./rel-visual.tif", "type": TIFF, "roles": ["data"]},
            "thumbnail": {"href": "https://data/thumb.jpg", "type": "image/jpeg", "roles": ["thumbnail"]},
            "cloud": {"href": "s3://bucket/cloud.jp2", "type": "image/jp2", "roles": ["data"]},
            "meta": {"href": "https://data/meta.xml", "type": "application/xml", "roles": ["metadata"]},
            "footprint": {"href": "https://data/fp.json", "type": "application/geo+json", "roles": ["data"]},
            "info": {"href": "https://data/info.json", "type": "application/json", "roles": ["data"]},
            "tiles": {"href": "https://data/t.pmtiles", "roles": ["data"]},
            "netcdf": {"href": "https://data/flood.nc", "type": "application/x-netcdf", "roles": ["data"]},
        },
    }


def test_api_items_passes_filters(monkeypatch):
    calls = install(monkeypatch, {
        "https://cat/collections/c1/items":
            {"features": [api_feature()], "numberMatched": 7},
    })
    r = items.main(collection_json=collection(), bbox="0,0,10,10",
                   datetime="2023-01-01T00:00:00Z/..", limit=5)
    assert calls[0][1] == {"limit": 5, "bbox": "0,0,10,10", "datetime": "2023-01-01T00:00:00Z/.."}
    assert r["access"] == "api" and r["count"] == 1 and r["matched"] == 7
    assert r["cursor"] == ""


def test_api_asset_resolution_and_viewability(monkeypatch):
    install(monkeypatch, {
        "https://cat/collections/c1/items": {"features": [api_feature()]},
    })
    r = items.main(collection_json=collection())
    it = r["items"][0]
    assert it["id"] == "S1" and it["bbox"] == [1, 2, 3, 4]
    assert it["datetime"] == "2023-05-22T04:35:08Z"
    a = {x["key"]: x for x in it["assets"]}
    # relative hrefs resolve against the item's own self link
    assert a["relative"]["href"] == "https://cat/collections/c1/items/rel-visual.tif"
    assert a["visual"]["viewable"] is True
    assert a["relative"]["viewable"] is True
    assert a["tiles"]["viewable"] is True
    assert a["thumbnail"]["viewable"] is False     # companion role, still listed
    assert a["cloud"]["viewable"] is False         # s3:// stays a link
    assert a["meta"]["viewable"] is False
    assert a["footprint"]["viewable"] is True      # .json only with geo+json type
    assert a["info"]["viewable"] is False
    assert a["netcdf"]["viewable"] is False        # remote NetCDF/HDF can't stream; download-only
    # the reason a blocked asset can't open is decided here (one source of truth)
    assert a["visual"]["reason"] == ""
    assert "not fetchable over HTTP" in a["cloud"]["reason"]      # s3://
    assert "download" in a["netcdf"]["reason"].lower()           # grid: download to open
    assert a["meta"]["reason"] == "the map template can't read this format"


# ----------------------------------------------------------------------------
# static access (Maxar-shaped: event -> acquisition children -> items)
# ----------------------------------------------------------------------------

ROOT = "https://ex/events/E1/collection.json"
CHILD_A = "https://ex/events/E1/ard/acquisition_collections/A_collection.json"
CHILD_B = "https://ex/events/E1/ard/acquisition_collections/B_collection.json"
ITEM_A1 = "https://ex/events/E1/ard/46/x/A1.json"
ITEM_A2 = "https://ex/events/E1/ard/46/x/A2.json"
ITEM_B1 = "https://ex/events/E1/ard/46/y/B1.json"


def static_docs():
    def item(iid, bbox, dt):
        return {
            "id": iid, "bbox": bbox,
            "properties": {"datetime": dt},
            "assets": {"visual": {"href": f"./{iid}-visual.tif", "type": TIFF, "roles": ["visual"]}},
        }
    return {
        ROOT: {"type": "Collection", "links": [
            {"rel": "child", "href": "./ard/acquisition_collections/A_collection.json"},
            {"rel": "child", "href": "./ard/acquisition_collections/B_collection.json"},
        ]},
        CHILD_A: {"type": "Collection",
                  "extent": {"spatial": {"bbox": [[0, 0, 10, 10]]}},
                  "links": [{"rel": "item", "href": "../46/x/A1.json"},
                            {"rel": "item", "href": "../46/x/A2.json"}]},
        CHILD_B: {"type": "Collection",
                  "extent": {"spatial": {"bbox": [[100, 40, 110, 50]]}},
                  "links": [{"rel": "item", "href": "../46/y/B1.json"}]},
        ITEM_A1: item("A1", [1, 1, 2, 2], "2023-05-22T04:35:08Z"),
        ITEM_A2: item("A2", [3, 3, 4, 4], "2023-05-23T04:35:08Z"),
        ITEM_B1: item("B1", [101, 41, 102, 42], "2023-05-24T04:35:08Z"),
    }


def test_public_s3_asset_is_rewritten_to_https_and_viewable(monkeypatch):
    feat = {
        "id": "M1", "bbox": [1, 2, 3, 4],
        "properties": {"datetime": "2022-06-27T00:00:00Z"},
        "links": [{"rel": "self", "href": "https://cat/collections/c1/items/M1"}],
        "assets": {"visual": {
            "href": "s3://maxar-opendata/events/x/10300100D4928800-visual.tif",
            "type": TIFF, "roles": ["data", "visual"]}},
    }
    install(monkeypatch, {"https://cat/collections/c1/items": {"features": [feat]}})
    monkeypatch.setattr(items, "_probe_public", lambda url: True)   # public bucket
    a = items.main(collection_json=collection())["items"][0]["assets"][0]
    assert a["href"] == "https://maxar-opendata.s3.amazonaws.com/events/x/10300100D4928800-visual.tif"
    assert a["auth"] == "none" and a["viewable"] is True and a["reason"] == ""


def test_private_s3_asset_stays_a_blocked_link(monkeypatch):
    feat = {
        "id": "V1", "bbox": [1, 2, 3, 4], "properties": {"datetime": "2025-01-01T00:00:00Z"},
        "links": [{"rel": "self", "href": "https://cat/collections/c1/items/V1"}],
        "assets": {"data": {"href": "s3://veda-data-store/x/y.tif", "type": TIFF, "roles": ["data"]}},
    }
    install(monkeypatch, {"https://cat/collections/c1/items": {"features": [feat]}})
    # _probe_public already returns False from install() -- private bucket
    a = items.main(collection_json=collection())["items"][0]["assets"][0]
    assert a["href"].startswith("s3://") and a["viewable"] is False
    assert "not fetchable over HTTP" in a["reason"]


def test_static_crawl_prunes_children_by_bbox(monkeypatch):
    calls = install(monkeypatch, static_docs())
    r = items.main(collection_json=collection("static"), bbox="0,0,5,5")
    assert sorted(i["id"] for i in r["items"]) == ["A1", "A2"]
    fetched = [u for u, _ in calls]
    assert CHILD_B in fetched          # its extent had to be read to prune it
    assert ITEM_B1 not in fetched      # ...but its items were never fetched


def test_static_crawl_stops_at_root_when_its_own_extent_misses_bbox(monkeypatch):
    # The root Collection carries an extent too (unlike the plain Catalog in
    # static_docs()) -- when the query bbox misses it, the whole tree is
    # provably empty, so no child should ever be fetched.
    docs = static_docs()
    docs[ROOT] = {"type": "Collection",
                  "extent": {"spatial": {"bbox": [[0, 0, 10, 10]]}},
                  "links": docs[ROOT]["links"]}
    calls = install(monkeypatch, docs)
    r = items.main(collection_json=collection("static"), bbox="100,40,110,50")
    assert r["items"] == [] and r["cursor"] == ""
    fetched = [u for u, _ in calls]
    assert fetched == [ROOT]           # neither child was ever fetched


def test_static_relative_assets_resolve_against_item_url(monkeypatch):
    install(monkeypatch, static_docs())
    r = items.main(collection_json=collection("static"))
    by_id = {i["id"]: i for i in r["items"]}
    assert by_id["A1"]["assets"][0]["href"] == "https://ex/events/E1/ard/46/x/A1-visual.tif"
    assert by_id["B1"]["assets"][0]["href"] == "https://ex/events/E1/ard/46/y/B1-visual.tif"
    assert all(a["viewable"] for i in r["items"] for a in i["assets"])


def test_static_pages_with_a_cursor(monkeypatch):
    install(monkeypatch, static_docs())
    # this fixture is smaller than one parallel wave, which would otherwise be
    # returned whole in page one -- shrink the wave so paging is observable
    monkeypatch.setattr(items, "_WAVE", 1)
    first = items.main(collection_json=collection("static"), limit=2)
    assert first["count"] == 2 and first["cursor"]
    assert [i["id"] for i in first["items"]] == ["A2", "A1"]   # datetime desc, per page

    second = items.main(collection_json=collection("static"), limit=2,
                        cursor=first["cursor"])
    assert [i["id"] for i in second["items"]] == ["B1"]
    assert second["cursor"] == ""      # nothing left to walk


def test_static_page_fetches_only_what_it_returns(monkeypatch):
    calls = install(monkeypatch, static_docs())
    monkeypatch.setattr(items, "_WAVE", 1)
    items.main(collection_json=collection("static"), limit=1)
    fetched = [u for u, _ in calls]
    assert ITEM_A1 in fetched
    assert ITEM_A2 not in fetched and ITEM_B1 not in fetched


def test_static_datetime_filter(monkeypatch):
    install(monkeypatch, static_docs())
    r = items.main(collection_json=collection("static"),
                   datetime="2023-05-23T00:00:00Z/..")
    assert sorted(i["id"] for i in r["items"]) == ["A2", "B1"]
    assert r["cursor"] == ""


def test_return_shape(monkeypatch):
    install(monkeypatch, {"https://cat/collections/c1/items": {"features": []}})
    r = items.main(collection_json=collection())
    assert set(r) == {"collection", "access", "items", "count", "matched",
                      "cursor", "elapsed_ms"}
    assert r["collection"] == "c1" and r["items"] == []


def test_items_payload_is_json_ascii_safe(monkeypatch):
    feat = api_feature()
    feat["assets"]["visual"]["title"] = "True colour → São Paulo ±0.5°"
    install(monkeypatch, {"https://cat/collections/c1/items": {"features": [feat]}})
    payload = json.dumps(items.main(collection_json=collection()))
    payload.encode("ascii")  # must not raise


# ----------------------------------------------------------------------------
# download.py
# ----------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, chunks, headers=None):
        self.chunks = chunks
        self.headers = headers or {}

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        return iter(self.chunks)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, chunks, headers=None):
        self.chunks = chunks
        self.headers = headers or {}
        self.calls = []

    def get(self, url, headers=None, stream=False, timeout=None):
        self.calls.append({"url": url, "stream": stream})
        return FakeResponse(self.chunks, self.headers)


def test_download_streams_to_disk(tmp_path, monkeypatch):
    session = FakeSession([b"abc", b"defg"])
    monkeypatch.setattr(discover, "_SESSION", session)
    r = download.main(url="https://data/scene%20one.tif", dest_dir=str(tmp_path))
    assert session.calls == [{"url": "https://data/scene%20one.tif", "stream": True}]
    assert r["name"] == "scene one.tif" and r["size"] == 7
    assert open(r["path"], "rb").read() == b"abcdefg"
    assert os.listdir(tmp_path) == ["scene one.tif"]   # no .part left behind


def test_download_explicit_name(tmp_path, monkeypatch):
    monkeypatch.setattr(discover, "_SESSION", FakeSession([b"x"]))
    r = download.main(url="https://data/a.tif", dest_dir=str(tmp_path), name="renamed.tif")
    assert r["name"] == "renamed.tif"
    assert os.path.basename(r["path"]) == "renamed.tif"


def test_mb_formats_sizes():
    assert download._mb(1536) == "1.5 KB"
    assert download._mb(int(2.5 * (1 << 20))) == "2.5 MB"
    assert download._mb(int(1.1 * (1 << 30))) == "1.1 GB"


def test_too_large_message_names_the_size_and_points_to_the_snippet():
    m = str(download._too_large(total=int(1.1 * (1 << 30)), done=12 * (1 << 20), el=3.0))
    assert "too large" in m and "GB" in m and "snippet" in m


def test_download_bails_early_when_throughput_cannot_finish_in_time(tmp_path, monkeypatch):
    # a huge Content-Length with a slow clock: after the sampling window the
    # projection sees it can't finish, so it stops instead of hitting the kill.
    monkeypatch.setattr(discover, "_SESSION",
                        FakeSession([b"x" * 10, b"y" * 10],
                                    headers={"Content-Length": str(5 * (1 << 30))}))
    clock = iter([0.0, 0.0, 4.0, 4.0])   # started, chunk1, chunk2 (el=4s > 3s sample)
    monkeypatch.setattr(download.time, "time", lambda: next(clock, 5.0))
    with pytest.raises(RuntimeError, match="too large"):
        download.main(url="https://data/huge.nc", dest_dir=str(tmp_path))
    assert os.listdir(tmp_path) == []   # temp + reservation both cleaned up


def test_download_rejects_non_http():
    with pytest.raises(ValueError):
        download.main(url="s3://bucket/key.tif")
    with pytest.raises(ValueError):
        download.main(url="")


def test_download_cleans_up_on_http_error_without_masking_it(tmp_path, monkeypatch):
    # The failure lands in raise_for_status(), before any bytes are written --
    # the temp file's handle is still open at that point. Cleanup must close it
    # first: on Windows, removing a still-open file raises PermissionError,
    # which used to replace this HTTPError instead of just cleaning up after it.
    class FailingResponse(FakeResponse):
        def raise_for_status(self):
            raise requests.HTTPError("404 Client Error")

    class FailingSession(FakeSession):
        def get(self, url, headers=None, stream=False, timeout=None):
            self.calls.append({"url": url, "stream": stream})
            return FailingResponse([], {})

    monkeypatch.setattr(discover, "_SESSION", FailingSession([]))
    with pytest.raises(requests.HTTPError, match="404"):
        download.main(url="https://data/missing.tif", dest_dir=str(tmp_path))
    assert os.listdir(tmp_path) == []   # temp + reservation both cleaned up


# ----------------------------------------------------------------------------
# sign.py (private storage the catalog signs anonymously)
# ----------------------------------------------------------------------------

AZURE = "https://hls2euwest.blob.core.windows.net/hls2/L30/B04.tif"
PUBLIC = "https://sentinel-cogs.s3.us-west-2.amazonaws.com/x/TCI.tif"


def test_scheme_for_keys_on_storage_host_not_catalog():
    assert sign.scheme_for(AZURE) == "azure-sas"
    assert sign.scheme_for(PUBLIC) == "none"
    # PC's own domain serves public tiles; it must not go to the signing API
    assert sign.scheme_for("https://planetarycomputer.microsoft.com/api/data/p.png") == "none"
    assert sign.scheme_for("s3://veda-data-store/x.tif") == ""


def test_sign_signs_private_storage(monkeypatch):
    calls = install(monkeypatch, {
        sign._PC_SIGN + quote(AZURE, safe=""):
            {"href": AZURE + "?se=2026&sig=abc", "msft:expiry": "2026-08-17T08:00:00Z"},
    })
    r = sign.main(url=AZURE)
    assert r["url"] == AZURE + "?se=2026&sig=abc"
    assert r["signed"] is True and r["expires"] == "2026-08-17T08:00:00Z"
    assert len(calls) == 1


def test_sign_leaves_public_urls_alone(monkeypatch):
    calls = install(monkeypatch, {})
    r = sign.main(url=PUBLIC)
    assert r["url"] == PUBLIC and r["signed"] is False and r["expires"] == ""
    assert calls == []                           # a public URL costs no request


def test_items_reports_auth_scheme_per_asset(monkeypatch):
    feat = api_feature()
    feat["assets"]["azure"] = {"href": AZURE, "type": TIFF, "roles": ["data"]}
    install(monkeypatch, {"https://cat/collections/c1/items": {"features": [feat]}})
    a = {x["key"]: x for x in items.main(collection_json=collection())["items"][0]["assets"]}
    assert a["azure"]["auth"] == "azure-sas" and a["azure"]["viewable"] is True
    assert a["visual"]["auth"] == "none"
    assert a["cloud"]["auth"] == ""              # s3:// - not fetchable at all


# ----------------------------------------------------------------------------
# download destination (user-chosen folder)
# ----------------------------------------------------------------------------

def test_download_defaults_to_project_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(discover, "_SESSION", FakeSession([b"x"]))
    monkeypatch.setattr(download, "DOWNLOAD_DIR", str(tmp_path / "proj"))
    r = download.main(url="https://data/a.tif")
    assert os.path.dirname(r["path"]) == str(tmp_path / "proj").replace("\\", "/")
    assert os.path.isdir(tmp_path / "proj")


def test_download_uses_chosen_folder_and_creates_it(tmp_path, monkeypatch):
    monkeypatch.setattr(discover, "_SESSION", FakeSession([b"x"]))
    chosen = tmp_path / "picked" / "nested"      # does not exist yet
    r = download.main(url="https://data/a.tif", dest_dir=str(chosen))
    assert os.path.dirname(r["path"]) == str(chosen).replace("\\", "/")
    assert open(r["path"], "rb").read() == b"x"


def test_download_expands_tilde_in_chosen_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(discover, "_SESSION", FakeSession([b"x"]))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))   # Windows
    monkeypatch.setenv("HOME", str(tmp_path))          # POSIX
    r = download.main(url="https://data/a.tif", dest_dir="~/Assets")
    assert "~" not in r["path"]
    assert os.path.isdir(tmp_path / "Assets")
    assert os.path.dirname(r["path"]) == str(tmp_path / "Assets").replace("\\", "/")


def test_download_blank_folder_falls_back_to_default(tmp_path, monkeypatch):
    monkeypatch.setattr(discover, "_SESSION", FakeSession([b"x"]))
    monkeypatch.setattr(download, "DOWNLOAD_DIR", str(tmp_path / "proj"))
    r = download.main(url="https://data/a.tif", dest_dir="   ")
    assert os.path.dirname(r["path"]) == str(tmp_path / "proj").replace("\\", "/")


def test_download_stays_inside_the_chosen_folder(tmp_path, monkeypatch):
    """A crafted href must not escape the folder the user picked."""
    monkeypatch.setattr(discover, "_SESSION", FakeSession([b"x"]))
    chosen = tmp_path / "picked"
    r = download.main(url="https://h/a/b%2F..%2F..%2Fevil.tif", dest_dir=str(chosen))
    assert os.path.dirname(r["path"]) == str(chosen).replace("\\", "/")
    assert os.listdir(chosen) == ["evil.tif"]
    assert not (tmp_path / "evil.tif").exists()


# ----------------------------------------------------------------------------
# pasted paths (download.clean_dir + resolve_dir.py)
# ----------------------------------------------------------------------------

BS = chr(92)     # backslash, built rather than escaped: these tests are about
DQ = chr(34)     # exactly the characters a paste carries


def test_clean_dir_accepts_paths_however_they_were_pasted(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    N = os.path.normpath
    win = "C:" + BS + "Users" + BS + "Admin" + BS + "My Downloads"

    # Windows "Copy as path" gives a quoted, backslashed path
    assert download.clean_dir(" " + DQ + win + DQ + "  ") == N(win)
    # single and smart quotes
    assert download.clean_dir("'/data/store'") == N("/data/store")
    assert download.clean_dir(chr(0x201c) + "C:/curly" + chr(0x201d)) == N("C:/curly")
    # a file:// URL, percent-escapes and all
    assert download.clean_dir(
        "file:///C:/Users/Admin/Pics%20and%20stuff") == N("C:/Users/Admin/Pics and stuff")
    # trailing separator, tilde, environment variable
    assert download.clean_dir("C:/Users/Admin/Docs/") == N("C:/Users/Admin/Docs")
    assert download.clean_dir("~/Assets") == N(os.path.join(str(tmp_path), "Assets"))
    assert download.clean_dir("$HOME/Assets") == N(os.path.join(str(tmp_path), "Assets"))
    # blank means "use the default", not "the current directory"
    assert download.clean_dir("   ") == ""
    assert download.clean_dir("") == ""
    assert download.clean_dir(None) == ""


def test_download_accepts_a_quoted_path(tmp_path, monkeypatch):
    monkeypatch.setattr(discover, "_SESSION", FakeSession([b"x"]))
    target = os.path.join(str(tmp_path), "Saved Assets")
    r = download.main(url="https://data/a.tif", dest_dir=DQ + target + DQ)
    assert DQ not in r["path"]
    assert os.path.isdir(target)
    assert os.path.dirname(r["path"]) == target.replace(BS, "/")


def test_resolve_dir_expands_what_the_page_cannot(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    want = os.path.join(str(tmp_path), "Assets").replace(chr(92), "/")
    assert resolve_dir.main(dest_dir="%USERPROFILE%/Assets")["dir"] == want
    assert resolve_dir.main(dest_dir="$HOME/Assets")["dir"] == want


def test_resolve_dir_blank_is_the_project_default():
    assert resolve_dir.main(dest_dir="")["dir"] == download.DOWNLOAD_DIR.replace(chr(92), "/")


def test_resolve_dir_create_makes_the_folder(tmp_path):
    target = tmp_path / "downloads" / "here"
    assert not target.exists()
    got = resolve_dir.main(dest_dir=str(target), create="1")["dir"]
    assert got == str(target).replace(chr(92), "/")
    assert target.is_dir()   # created so the explorer opens a real folder
    # default (no create) must not touch the filesystem
    resolve_dir.main(dest_dir=str(tmp_path / "untouched"))
    assert not (tmp_path / "untouched").exists()


def test_download_reserves_the_name_against_a_concurrent_call(tmp_path, monkeypatch):
    """Two assets sharing a basename must both survive. Checking existence and
    then writing loses this race; reserving the name atomically does not."""
    import threading

    class Slow:
        headers = {}

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            for _ in range(3):
                time.sleep(0.15)      # long enough for both calls to overlap
                yield b"x" * 100

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(discover, "_SESSION",
                        type("S", (), {"get": lambda self, u, **k: Slow()})())
    url = "https://data/{}/asset-visual.tif"
    got = {}

    def fetch(key, quadkey):
        got[key] = download.main(url=url.format(quadkey), dest_dir=str(tmp_path))

    threads = [threading.Thread(target=fetch, args=(k, q))
               for k, q in (("a", "133"), ("b", "311"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(os.listdir(tmp_path)) == ["asset-visual-2.tif", "asset-visual.tif"]
    assert {got["a"]["name"], got["b"]["name"]} == {"asset-visual.tif", "asset-visual-2.tif"}
    assert all(os.path.getsize(os.path.join(str(tmp_path), f)) == 300
               for f in os.listdir(tmp_path))


def test_download_neutralises_a_drive_qualified_name(tmp_path, monkeypatch):
    """os.path.join drops the folder for a name like "D:evil.tif", and a colon
    also opens an NTFS stream on Windows, so the colon must not survive."""
    monkeypatch.setattr(discover, "_SESSION", FakeSession([b"x"]))
    r = download.main(url="https://h/a/D:evil.tif", dest_dir=str(tmp_path))
    assert r["name"] == "D_evil.tif"
    assert os.path.dirname(r["path"]) == str(tmp_path).replace(chr(92), "/")
    assert os.listdir(tmp_path) == ["D_evil.tif"]
