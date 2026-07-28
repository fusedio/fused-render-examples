"""SKY view — Windy-style weather layers + live flights (data side of sky.html).

Modes:
  wind   — global 10-degree U/V wind grid in leaflet-velocity (GFS-json) format,
           built from open-meteo batched multi-location calls. Cached 45 min.
  frames — RainViewer animated precipitation radar frame list. Cached 5 min.

Flights come from radar.py (mode=scan) — sky.html reuses it directly.
Runs on Fused's remote Lambda: self-contained, keyless APIs only.
"""

import fused


@fused.udf
def main(mode: str = "wind", la1: float = 0.0, la2: float = 0.0,
         lo1: float = 0.0, lo2: float = 0.0) -> dict:
    import json
    import math
    import os
    import tempfile
    import time
    import urllib.error
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor

    CACHE_DIR = os.path.join(tempfile.gettempdir(), "flightdeck_cache")
    HTTP_TIMEOUT_S = 20

    def fetch_json(url):
        req = urllib.request.Request(url, headers={"User-Agent": "flightdeck/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            return None

    def cache_get(name, ttl):
        path = os.path.join(CACHE_DIR, name + ".json")
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < ttl:
            try:
                with open(path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return None

    def cache_put(name, data):
        os.makedirs(CACHE_DIR, exist_ok=True)
        try:
            with open(os.path.join(CACHE_DIR, name + ".json"), "w") as f:
                json.dump(data, f)
        except OSError:
            pass

    if mode == "frames":
        cached = cache_get("rainviewer", 300)
        if cached:
            return cached
        d = fetch_json("https://api.rainviewer.com/public/weather-maps.json")
        if not d:
            return {"ok": False, "error": "rainviewer unreachable"}
        frames = [
            {"path": f["path"], "time": f["time"]}
            for f in (d.get("radar", {}).get("past") or []) + (d.get("radar", {}).get("nowcast") or [])
        ]
        out = {"ok": True, "host": d.get("host"), "frames": frames}
        cache_put("rainviewer", out)
        return out

    def opensky_fetch(url):
        """OpenSky fetch, OAuth2-authed when credentials exist (4000 credits/day
        vs 400 anonymous). Returns (json, remaining_credits) — remaining comes
        from the X-Rate-Limit-Remaining header; None if header absent/unauthed
        failure, 0 on 429."""
        import urllib.parse
        token = None
        try:
            cid = fused.secrets["opensky_client_id"]
            csec = fused.secrets["opensky_client_secret"]
        except Exception:
            cid = csec = None
        if cid and csec:
            tok = cache_get("opensky_token", 25 * 60)  # tokens live 30 min
            token = (tok or {}).get("t")
            if not token:
                body = urllib.parse.urlencode({
                    "grant_type": "client_credentials",
                    "client_id": cid, "client_secret": csec,
                }).encode()
                req = urllib.request.Request(
                    "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token",
                    data=body)
                try:
                    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
                        token = json.loads(r.read().decode("utf-8")).get("access_token")
                    if token:
                        cache_put("opensky_token", {"t": token})
                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
                    token = None
        headers = {"User-Agent": "flightdeck/0.1"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                rem = resp.headers.get("X-Rate-Limit-Remaining")
                return (json.loads(resp.read().decode("utf-8")),
                        int(rem) if rem and rem.isdigit() else None)
        except urllib.error.HTTPError as e:
            return None, 0 if e.code == 429 else None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            return None, None

    if mode == "global":
        # full-planet snapshot via OpenSky — cached 120s (60s polling could burn
        # 5760 credits/day vs the 4000 authed allowance); authed 4000/day, anon 400
        cached = cache_get("opensky", 120)
        if cached:
            return cached
        d, quota = opensky_fetch("https://opensky-network.org/api/states/all")
        if not d or not d.get("states"):
            stale = cache_get("opensky", 30 * 60)  # serve stale over nothing
            if stale:
                stale["stale"] = True
                if quota is not None:
                    stale["quota"] = quota
                return stale
            return {"ok": False, "error": "opensky unreachable", "quota": quota}
        planes = []
        for s in d["states"]:
            # [0]icao24 [1]callsign [5]lon [6]lat [7]baro_alt_m [8]on_ground [9]vel_ms [10]track [11]vr_ms
            if s[5] is None or s[6] is None or s[8]:
                continue
            planes.append([
                round(s[6], 3), round(s[5], 3),
                int(s[10]) if s[10] is not None else 0,
                int(s[7] * 3.28084) if s[7] is not None else None,
                int(s[9] * 1.94384) if s[9] is not None else None,
                (s[1] or "").strip(),
                s[0],
                int(s[11] * 196.85) if s[11] is not None else 0,
            ])
        out = {"ok": True, "ts": d.get("time"), "count": len(planes), "planes": planes, "quota": quota}
        cache_put("opensky", out)
        return out

    # ---- mode == "wind": global 10-degree wind U/V grid ----
    # NOTE: open-meteo allows ~600 locations/min — this 504-point grid fits.
    cached = cache_get("windgrid10v2", 45 * 60)
    if cached:
        return cached

    LA1, LA2, LO1, LO2, STEP = 70, -60, -180, 170, 10
    lats_axis = list(range(LA1, LA2 - 1, -STEP))      # 70 .. -60, north to south
    lons_axis = list(range(LO1, LO2 + 1, STEP))       # -180 .. 170
    ny, nx = len(lats_axis), len(lons_axis)

    points = [(la, lo) for la in lats_axis for lo in lons_axis]  # row-major from NW

    def fetch_batch(batch):
        lats = ",".join(str(p[0]) for p in batch)
        lons = ",".join(str(p[1]) for p in batch)
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lats}&longitude={lons}"
            "&hourly=wind_speed_10m,wind_direction_10m&forecast_hours=1&wind_speed_unit=ms"
        )
        d = fetch_json(url)
        if d is None:
            return [None] * len(batch)
        if isinstance(d, dict):
            d = [d]
        out = []
        for item in d:
            try:
                spd = item["hourly"]["wind_speed_10m"][0]
                deg = item["hourly"]["wind_direction_10m"][0]
                out.append((spd, deg))
            except (KeyError, IndexError, TypeError):
                out.append(None)
        return out

    SIZE = 120
    batches = [points[i:i + SIZE] for i in range(0, len(points), SIZE)]
    results = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        for chunk in pool.map(fetch_batch, batches):
            results.extend(chunk)

    u_data, v_data = [], []
    got = 0
    for r in results:
        if r is None or r[0] is None:
            u_data.append(0.0)
            v_data.append(0.0)
            continue
        spd, deg = r
        rad = math.radians(deg)  # meteorological: direction wind comes FROM
        u_data.append(round(-spd * math.sin(rad), 2))
        v_data.append(round(-spd * math.cos(rad), 2))
        got += 1

    header_common = {
        "lo1": LO1, "la1": LA1, "lo2": LO2, "la2": LA2,
        "dx": STEP, "dy": STEP, "nx": nx, "ny": ny,
        "refTime": time.strftime("%Y-%m-%d %H:%M:%S"),
        "forecastTime": 0,
        "parameterUnit": "m.s-1",
        "parameterCategory": 2,
    }
    out = {
        "ok": True,
        "sampled": got,
        "grid": [
            {"header": dict(header_common, parameterNumber=2, parameterNumberName="eastward_wind"), "data": u_data},
            {"header": dict(header_common, parameterNumber=3, parameterNumberName="northward_wind"), "data": v_data},
        ],
        "max_ms": max((abs(u) + abs(v)) for u, v in zip(u_data, v_data)) if u_data else 0,
    }
    cache_put("windgrid10v2", out)
    return out
