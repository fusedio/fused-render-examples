"""Tests for geocode.py -- the map's place search. Network is mocked.

    uv run --with pytest --with requests pytest test_geocode.py -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import geocode  # noqa: E402


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_geocode_reorders_bbox_to_wsen(monkeypatch):
    # Nominatim boundingbox is [south, north, west, east]; we want [w, s, e, n]
    monkeypatch.setattr(geocode.requests, "get", lambda *a, **k: _Resp([
        {"display_name": "Hyderabad, India", "lat": "17.36", "lon": "78.47",
         "boundingbox": ["17.29", "17.56", "78.24", "78.62"]},
    ]))
    g = geocode.main(q="hyderabad")["results"][0]
    assert g["lat"] == 17.36 and g["lon"] == 78.47
    assert g["bbox"] == [78.24, 17.29, 78.62, 17.56]


def test_geocode_tolerates_missing_bbox(monkeypatch):
    monkeypatch.setattr(geocode.requests, "get", lambda *a, **k: _Resp([
        {"display_name": "Somewhere", "lat": "1.0", "lon": "2.0"},
    ]))
    g = geocode.main(q="somewhere")["results"][0]
    assert g["bbox"] is None and g["lat"] == 1.0


def test_geocode_empty_query_skips_network():
    assert geocode.main(q="   ")["results"] == []
