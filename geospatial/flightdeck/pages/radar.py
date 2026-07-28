"""Live area radar — data side of radar.html (FlightRadar24-style view).

Modes:
  scan   — all aircraft within radius_nm of lat/lon (airplanes.live /point)
  route  — resolve a callsign's route via adsbdb (cached long; for the detail panel)

Runs on Fused's remote Lambda: self-contained, keyless APIs only.
"""

import fused


@fused.udf
def main(mode: str = "scan", lat: float = 28.56, lon: float = 77.10,
         radius_nm: int = 150, callsign: str = "") -> dict:
    import json
    import math
    import os
    import re
    import tempfile
    import time
    import urllib.error
    import urllib.request

    AIRPLANES_LIVE = "https://api.airplanes.live/v2"
    ADSBDB = "https://api.adsbdb.com/v0"
    CACHE_DIR = os.path.join(tempfile.gettempdir(), "flightdeck_cache")
    HTTP_TIMEOUT_S = 9

    def fetch_json(url):
        req = urllib.request.Request(url, headers={"User-Agent": "flightdeck/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            return None

    def cached_fetch(kind, key, url, ttl):
        os.makedirs(CACHE_DIR, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", f"{kind}_{key}")
        path = os.path.join(CACHE_DIR, safe + ".json")
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < ttl:
            try:
                with open(path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        data = fetch_json(url)
        if data is not None:
            try:
                with open(path, "w") as f:
                    json.dump(data, f)
            except OSError:
                pass
        return data

    if mode == "photo":
        # real photo of this exact airframe (planespotters, needs descriptive UA)
        hx = re.sub(r"[^a-f0-9]", "", callsign.lower())
        if len(hx) != 6:
            return {"ok": False}
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = os.path.join(CACHE_DIR, f"photo_{hx}.json")
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < 7 * 24 * 3600:
            try:
                with open(path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        req = urllib.request.Request(
            f"https://api.planespotters.net/pub/photos/hex/{hx}",
            headers={"User-Agent": "Flightdeck/0.1 (+mailto:at@fused.io)"})
        out = {"ok": False}
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            ph = (data.get("photos") or [None])[0]
            if ph:
                out = {
                    "ok": True,
                    "src": ph["thumbnail_large"]["src"],
                    "link": ph.get("link"),
                    "credit": ph.get("photographer"),
                }
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, KeyError):
            pass
        try:
            with open(path, "w") as f:
                json.dump(out, f)
        except OSError:
            pass
        return out

    if mode == "watch":
        # global emergency squawk sweep — tiny payload for the cross-page watch strip
        found = []
        for sq in ("7700", "7600"):
            data = fetch_json(f"{AIRPLANES_LIVE}/squawk/{sq}")
            for ac in (data or {}).get("ac") or []:
                if ac.get("lat") is None:
                    continue
                found.append({
                    "cs": (ac.get("flight") or "").strip() or ac.get("r") or ac.get("hex"),
                    "hex": ac.get("hex"),
                    "squawk": sq,
                    "lat": ac.get("lat"), "lon": ac.get("lon"),
                    "alt": ac.get("alt_baro"), "type": ac.get("t"),
                })
        return {"ok": True, "emergencies": found[:6]}

    if mode == "route":
        cs = re.sub(r"[^A-Z0-9]", "", callsign.upper())
        if len(cs) < 3:
            return {"ok": False, "error": "bad callsign"}
        resp = cached_fetch("route", cs, f"{ADSBDB}/callsign/{cs}", ttl=7 * 24 * 3600)
        fr = (resp or {}).get("response", {})
        fr = fr.get("flightroute") if isinstance(fr, dict) else None
        if not fr:
            # suffix-letter callsigns (IGO63YE) often resolve without the letter
            m = re.match(r"^([A-Z]{3}\d{1,4})[A-Z]$", cs)
            if m:
                resp = cached_fetch("route", m.group(1), f"{ADSBDB}/callsign/{m.group(1)}", ttl=7 * 24 * 3600)
                fr = (resp or {}).get("response", {})
                fr = fr.get("flightroute") if isinstance(fr, dict) else None
        if not fr:
            return {"ok": False, "error": "route unknown"}
        def apt(node):
            return {
                "iata": node.get("iata_code"), "icao": node.get("icao_code"),
                "name": node.get("name"), "city": node.get("municipality"),
                "country_iso": node.get("country_iso_name"),
                "lat": node.get("latitude"), "lon": node.get("longitude"),
            }
        al = fr.get("airline") or {}
        return {
            "ok": True,
            "airline": {"name": al.get("name"), "iata": al.get("iata"), "icao": al.get("icao")},
            "origin": apt(fr.get("origin") or {}),
            "destination": apt(fr.get("destination") or {}),
        }

    # ---- scan ----
    # airplanes.live caps /point at 250 nm — wide viewports are covered by a
    # parallel sweep of up to 9 sub-circles, deduplicated by hex
    r = max(10, min(1000, int(radius_nm)))
    if r <= 250:
        centers = [(lat, lon)]
        sub_r = r
    else:
        sub_r = 250
        step_deg = 250 / 60.0 * 1.55          # circle spacing that still overlaps
        n = min(3, max(2, int(r / 250)))      # 2x2 or 3x3 sweep
        offs = [(i - (n - 1) / 2) * step_deg for i in range(n)]
        coslat = max(0.2, abs(math.cos(math.radians(lat))))
        centers = [(lat + dy, lon + dx / coslat) for dy in offs for dx in offs]

    from concurrent.futures import ThreadPoolExecutor

    def one(c):
        return fetch_json(f"{AIRPLANES_LIVE}/point/{c[0]:.4f}/{c[1]:.4f}/{sub_r}")

    responses = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(one, centers))
    if all(d is None for d in responses):
        return {"ok": False, "error": "adsb feed unreachable"}
    seen_hex = set()
    merged = []
    for d in responses:
        for ac in (d or {}).get("ac") or []:
            hx = ac.get("hex")
            if hx in seen_hex:
                continue
            seen_hex.add(hx)
            merged.append(ac)
    out = []
    for ac in merged:
        if ac.get("lat") is None:
            continue
        alt = ac.get("alt_baro")
        out.append({
            "hex": ac.get("hex"),
            "cs": (ac.get("flight") or "").strip(),
            "reg": ac.get("r"),
            "type": ac.get("t"),
            "desc": ac.get("desc"),
            "lat": ac.get("lat"),
            "lon": ac.get("lon"),
            "alt": 0 if alt == "ground" else (alt if isinstance(alt, (int, float)) else None),
            "ground": alt == "ground",
            "gs": ac.get("gs"),
            "ias": ac.get("ias"),
            "mach": ac.get("mach"),
            "track": ac.get("track"),
            "vr": ac.get("baro_rate"),
            "squawk": ac.get("squawk"),
            "emergency": ac.get("emergency") if ac.get("emergency") not in (None, "none") else None,
            "cat": ac.get("category"),
            "wd": ac.get("wd"), "ws": ac.get("ws"), "oat": ac.get("oat"),
            "seen": ac.get("seen"),
            "mil": ac.get("dbFlags", 0) & 1 == 1 if isinstance(ac.get("dbFlags"), int) else False,
        })
    out.sort(key=lambda a: -(a["alt"] or 0))
    return {"ok": True, "ts": time.time(), "count": len(out), "aircraft": out}
