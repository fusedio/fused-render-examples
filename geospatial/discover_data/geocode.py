"""Geocode a place name to a point + bounding box, for the map's search box.

Runs server-side (via runPython) rather than a browser fetch, so it isn't
subject to the page's CORS/CSP and can set the User-Agent OpenStreetMap's
Nominatim usage policy asks for. Returns a short ranked list; the map jumps to
the first (its bbox if it has one, else a point).
"""

import requests

import discover

discover._utf8_stdio()   # force UTF-8 stdio (shared with discover, not re-implemented)

_URL = "https://nominatim.openstreetmap.org/search"
_HEADERS = {"User-Agent": "fused-render-discover-data", "Accept": "application/json"}


def main(q: str = "", limit: int = 5):
    q = q.strip()
    if not q:
        return {"results": []}
    r = requests.get(_URL, headers=_HEADERS, timeout=15, params={
        "q": q, "format": "jsonv2", "limit": max(1, min(int(limit), 10)), "addressdetails": 0})
    r.raise_for_status()
    results = []
    for g in r.json():
        try:
            lat, lon = float(g["lat"]), float(g["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        # Nominatim boundingbox is [south, north, west, east] as strings
        bb = g.get("boundingbox")
        bbox = None
        if isinstance(bb, list) and len(bb) == 4:
            try:
                bbox = [float(bb[2]), float(bb[0]), float(bb[3]), float(bb[1])]
            except (TypeError, ValueError):
                bbox = None
        results.append({"name": g.get("display_name", ""), "lat": lat, "lon": lon, "bbox": bbox})
    return {"results": results}


if __name__ == "__main__":
    import json
    print(json.dumps(main(q="hyderabad india"), indent=2)[:1500])
