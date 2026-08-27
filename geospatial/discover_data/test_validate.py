"""Tests for validate.py — catalog URL validation.

Network is mocked by patching `discover._get_json` (validate reuses it). Run:

    uv run --with pytest --with requests pytest test_validate.py -q
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import discover  # noqa: E402
import validate  # noqa: E402

CS = "https://api.stacspec.org/v1.0.0-rc.1/collection-search"
FT = "https://api.stacspec.org/v1.0.0-rc.1/collection-search#free-text"


def landing(stac_version="1.0.0", conf=None):
    return {"stac_version": stac_version, "conformsTo": conf or [], "links": []}


def collections(number_matched=5):
    return {"collections": [{"id": "x"}], "numberMatched": number_matched, "links": []}


def install(monkeypatch, specs):
    """specs: {base: {"landing": dict|Exception|other, "collections": dict|Exception}}."""
    def fake(url, timeout, params=None):
        for base, spec in specs.items():
            if url.startswith(base.rstrip("/")):
                if "/collections" in url:
                    c = spec.get("collections")
                    if isinstance(c, Exception):
                        raise c
                    return c
                land = spec.get("landing")
                if isinstance(land, Exception):
                    raise land
                return land
        raise AssertionError(f"unexpected url {url}")
    monkeypatch.setattr(discover, "_get_json", fake)


def one(monkeypatch, base, spec):
    install(monkeypatch, {base: spec})
    return validate.main(apis=base)["results"][0]


def test_valid_server_side(monkeypatch):
    r = one(monkeypatch, "https://good",
            {"landing": landing(conf=[CS, FT]), "collections": collections(41)})
    assert r["ok"] is True
    assert r["supports_q"] is True
    assert r["stac_version"] == "1.0.0"
    assert r["n_collections"] == 41
    assert "server-side free-text" in r["message"]


def test_valid_local_only(monkeypatch):
    r = one(monkeypatch, "https://plain",
            {"landing": landing(conf=[]), "collections": collections()})
    assert r["ok"] is True
    assert r["supports_q"] is False
    assert "filtered locally" in r["message"]


def test_collections_endpoint_fails(monkeypatch):
    r = one(monkeypatch, "https://halfstac",
            {"landing": landing(), "collections": ConnectionError("boom")})
    assert r["ok"] is False
    assert r["stac_version"] == "1.0.0"
    assert "collections failed" in r["message"]


def test_collections_no_list(monkeypatch):
    r = one(monkeypatch, "https://weird",
            {"landing": landing(), "collections": {"not": "collections"}})
    assert r["ok"] is False
    assert "no collection list" in r["message"]


def test_not_json_object(monkeypatch):
    r = one(monkeypatch, "https://listy", {"landing": ["a", "b"]})
    assert r["ok"] is False
    assert "JSON object" in r["message"]


def test_no_stac_version(monkeypatch):
    r = one(monkeypatch, "https://random", {"landing": {"hello": "world"}})
    assert r["ok"] is False
    assert "not a STAC endpoint" in r["message"]


def test_unreachable(monkeypatch):
    r = one(monkeypatch, "https://down", {"landing": ConnectionError("no route")})
    assert r["ok"] is False
    assert "unreachable" in r["message"]


def test_empty_apis_returns_no_results():
    assert validate.main(apis="")["results"] == []
    assert validate.main(apis="  , \n ")["results"] == []


def test_multiple_urls_in_order(monkeypatch):
    install(monkeypatch, {
        "https://a": {"landing": landing(conf=[CS, FT]), "collections": collections(3)},
        "https://b": {"landing": {"nope": 1}},
    })
    results = validate.main(apis="https://a\nhttps://b")["results"]
    assert [r["host"] for r in results] == ["a", "b"]
    assert results[0]["ok"] is True and results[1]["ok"] is False
