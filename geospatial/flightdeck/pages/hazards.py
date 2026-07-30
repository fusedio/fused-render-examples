"""Earth Ops — data side of hazards.html (live planet hazards + live aviation).

Aggregates keyless global feeds into one JSON payload for a dark world map:
  - USGS earthquakes (mag ≥ 2.5, last day)
  - NASA EONET natural events (wildfires, severe storms, volcanoes, sea/lake ice)
  - open-meteo 250 hPa jet-stream winds sampled on a coarse global grid
  - airplanes.live ambient traffic over 6 hubs

Runs on Fused's remote Lambda: self-contained, keyless APIs only, stdlib only.
Each feed caches to /tmp on its own TTL so refreshes are cheap and polite.
Feeds are fetched in parallel (ThreadPoolExecutor) to stay well under 30s.
"""

import fused


@fused.udf
def main(mode: str = "all") -> dict:
    import json
    import os
    import tempfile
    import time
    import urllib.error
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor

    CACHE_DIR = os.path.join(tempfile.gettempdir(), "flightdeck_hazards")
    HTTP_TIMEOUT_S = 9

    USGS_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson"
    EONET_URL = "https://eonet.gsfc.nasa.gov/api/v3/events?status=open&limit=400"
    OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
    AIRPLANES_LIVE = "https://api.airplanes.live/v2"

    def fetch_json(url, ua="flightdeck-hazards/0.1"):
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            return None

    def cached(key, ttl, compute):
        """Serve `compute()`'s JSON result from /tmp if fresher than ttl seconds."""
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = os.path.join(CACHE_DIR, key + ".json")
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < ttl:
            try:
                with open(path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        val = compute()
        if val is not None:
            try:
                with open(path, "w") as f:
                    json.dump(val, f)
            except OSError:
                pass
        return val

    # ---------------- 1. earthquakes (USGS) ----------------
    def get_quakes():
        def compute():
            data = fetch_json(USGS_URL)
            if not data:
                return []
            now = time.time()
            out = []
            for f in data.get("features") or []:
                p = f.get("properties") or {}
                g = f.get("geometry") or {}
                mag = p.get("mag")
                coords = g.get("coordinates") or []
                if mag is None or mag < 2.5 or len(coords) < 2:
                    continue
                t_ms = p.get("time") or 0
                out.append({
                    "mag": round(float(mag), 1),
                    "place": p.get("place") or "",
                    "lat": coords[1],
                    "lon": coords[0],
                    "depth_km": round(coords[2], 1) if len(coords) > 2 and coords[2] is not None else None,
                    "age_hr": round(max(0.0, now - t_ms / 1000.0) / 3600.0, 1),
                    "url": p.get("url") or "",
                })
            out.sort(key=lambda q: -q["mag"])
            return out[:80]
        return cached("quakes", 600, compute) or []

    # ---------------- 2. natural events (NASA EONET) ----------------
    def get_events():
        def compute():
            data = fetch_json(EONET_URL)
            groups = {"wildfires": [], "storms": [], "volcanoes": [], "ice": []}
            if not data:
                return groups
            cat_map = {
                "Wildfires": "wildfires",
                "Severe Storms": "storms",
                "Volcanoes": "volcanoes",
                "Sea and Lake Ice": "ice",
            }
            for ev in data.get("events") or []:
                cats = ev.get("categories") or []
                title = cats[0].get("title") if cats else None
                key = cat_map.get(title)
                if not key:
                    continue
                geoms = ev.get("geometry") or []
                if not geoms:
                    continue
                geo = geoms[-1]  # last observed point of this event
                gtype = geo.get("type")
                coords = geo.get("coordinates")
                lat = lon = None
                try:
                    if gtype == "Point":
                        lon, lat = coords[0], coords[1]
                    elif gtype == "Polygon":
                        first = coords[0][0]  # first coord of first ring
                        lon, lat = first[0], first[1]
                except (TypeError, IndexError):
                    continue
                if lat is None or lon is None:
                    continue
                groups[key].append({
                    "title": ev.get("title") or "",
                    "lat": lat,
                    "lon": lon,
                    "category": title,
                })
            groups["wildfires"] = groups["wildfires"][:120]  # fires dominate — cap
            return groups
        return cached("eonet", 1800, compute) or {"wildfires": [], "storms": [], "volcanoes": [], "ice": []}

    # ---------------- 3. jet stream (open-meteo 250 hPa) ----------------
    def get_jet():
        def compute():
            lons = [-120, -90, -60, -30, 0, 30, 60, 90, 120, 150]
            north = [(la, lo) for la in [55, 45, 35, 25] for lo in lons]  # 40
            south = [(la, lo) for la in [-30, -40] for lo in lons]        # 20
            pts = north + south                                          # 60

            def fetch_batch(chunk):
                lat_s = ",".join(str(la) for la, _ in chunk)
                lon_s = ",".join(str(lo) for _, lo in chunk)
                url = (f"{OPEN_METEO}?latitude={lat_s}&longitude={lon_s}"
                       "&hourly=wind_speed_250hPa,wind_direction_250hPa"
                       "&forecast_hours=1&wind_speed_unit=kn")
                data = fetch_json(url)
                out = []
                if not data:
                    return out
                items = data if isinstance(data, list) else [data]
                for (la, lo), item in zip(chunk, items):
                    h = (item or {}).get("hourly") or {}
                    sp = h.get("wind_speed_250hPa") or []
                    di = h.get("wind_direction_250hPa") or []
                    if sp and di and sp[0] is not None and di[0] is not None:
                        out.append({"lat": la, "lon": lo, "kt": round(sp[0]), "dir": round(di[0])})
                return out

            chunks = [pts[i:i + 15] for i in range(0, len(pts), 15)]  # 4 batches
            res = []
            with ThreadPoolExecutor(max_workers=4) as ex:
                for r in ex.map(fetch_batch, chunks):
                    res.extend(r)
            return res
        return cached("jet", 3600, compute) or []

    # ---------------- 4. aviation layer (airplanes.live) ----------------
    def get_traffic():
        def compute():
            hubs = [(28.6, 77.1), (25.3, 55.4), (50.0, 8.6),
                    (40.6, -73.8), (35.6, 140.0), (1.36, 104.0)]

            def scan(h):
                lat, lon = h
                d = fetch_json(f"{AIRPLANES_LIVE}/point/{lat:.4f}/{lon:.4f}/250")
                return (d or {}).get("ac") or []

            seen = set()
            pts = []
            with ThreadPoolExecutor(max_workers=6) as ex:
                for acs in ex.map(scan, hubs):
                    for ac in acs:
                        hx = ac.get("hex")
                        lat = ac.get("lat")
                        if not hx or hx in seen or lat is None:
                            continue
                        seen.add(hx)
                        pts.append([round(lat, 2), round(ac.get("lon"), 2)])
            return pts[:1500]
        return cached("traffic", 60, compute) or []

    # ---------------- fan out over all four feeds ----------------
    with ThreadPoolExecutor(max_workers=4) as ex:
        fq = ex.submit(get_quakes)
        fe = ex.submit(get_events)
        fj = ex.submit(get_jet)
        ft = ex.submit(get_traffic)
        quakes = fq.result()
        events = fe.result()
        jet = fj.result()
        traffic = ft.result()

    return {
        "ok": True,
        "ts": time.time(),
        "quakes": quakes,
        "events": events,
        "jet": jet,
        "traffic": traffic,
        "counts": {
            "quakes": len(quakes),
            "wildfires": len(events["wildfires"]),
            "storms": len(events["storms"]),
            "volcanoes": len(events["volcanoes"]),
            "ice": len(events["ice"]),
            "jet": len(jet),
            "traffic": len(traffic),
        },
    }
