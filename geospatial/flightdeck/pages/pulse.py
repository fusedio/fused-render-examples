"""Global pulse — data side of pulse.html (RadarBox/FR24-statistics-style dashboard).

Samples airplanes.live across 12 hub regions in parallel, dedupes by hex, and
aggregates a "state of the skies right now" snapshot: who's flying, what they're
flying, how high, and the current superlatives.

Runs on Fused's remote Lambda: self-contained, keyless APIs only, stdlib only.
Caches the whole aggregate to /tmp for 45s so refreshes are instant and polite.
"""

import fused


@fused.udf
def main() -> dict:
    import json
    import os
    import re
    import tempfile
    import time
    import urllib.error
    import urllib.request
    from collections import Counter
    from concurrent.futures import ThreadPoolExecutor

    AIRPLANES_LIVE = "https://api.airplanes.live/v2"
    RADIUS_NM = 250
    HTTP_TIMEOUT_S = 9
    CACHE_TTL_S = 45
    CACHE_PATH = os.path.join(tempfile.gettempdir(), "flightdeck_pulse.json")

    REGIONS = [
        ("Delhi", 28.6, 77.1),
        ("Mumbai", 19.1, 72.9),
        ("Gulf", 25.3, 55.4),
        ("Singapore", 1.36, 104.0),
        ("Hong Kong", 22.3, 114.0),
        ("Tokyo", 35.6, 140.0),
        ("London", 51.5, -0.5),
        ("Central Europe", 50.0, 8.6),
        ("New York", 40.6, -73.8),
        ("US West", 34.0, -118.4),
        ("South America", -23.4, -46.5),
        ("Australia", -33.9, 151.2),
    ]

    AIRLINES = {
        "AIC": "Air India", "IGO": "IndiGo", "AXB": "Air India Express",
        "UAE": "Emirates", "QTR": "Qatar Airways", "ETD": "Etihad",
        "SIA": "Singapore Airlines", "MAS": "Malaysia Airlines", "THA": "Thai Airways",
        "CPA": "Cathay Pacific", "JAL": "JAL", "ANA": "ANA",
        "KAL": "Korean Air", "AAR": "Asiana", "CES": "China Eastern",
        "CSN": "China Southern", "CCA": "Air China", "BAW": "British Airways",
        "DLH": "Lufthansa", "AFR": "Air France", "KLM": "KLM",
        "RYR": "Ryanair", "EZY": "easyJet", "WZZ": "Wizz Air",
        "THY": "Turkish Airlines", "UAL": "United", "AAL": "American",
        "DAL": "Delta", "SWA": "Southwest", "JBU": "JetBlue",
        "ACA": "Air Canada", "QFA": "Qantas", "VOZ": "Virgin Australia",
        "ANZ": "Air New Zealand", "GLO": "GOL", "TAM": "LATAM",
        "SVA": "Saudia", "MSR": "EgyptAir", "ETH": "Ethiopian",
        "SAA": "South African",
    }

    TYPES = {
        "A320": "Airbus A320", "A20N": "Airbus A320neo", "A21N": "Airbus A321neo",
        "B738": "Boeing 737-800", "B38M": "Boeing 737 MAX 8", "B77W": "Boeing 777-300ER",
        "B789": "Boeing 787-9", "A359": "Airbus A350-900", "A333": "Airbus A330-300",
        "A388": "Airbus A380", "B744": "Boeing 747-400", "AT76": "ATR 72-600",
        "E190": "Embraer E190", "B763": "Boeing 767-300", "B752": "Boeing 757-200",
        "DH8D": "Dash 8 Q400",
    }

    def fetch_json(url):
        req = urllib.request.Request(url, headers={"User-Agent": "flightdeck-pulse/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            return None

    # ---- serve from cache if fresh ----
    try:
        if os.path.exists(CACHE_PATH) and time.time() - os.path.getmtime(CACHE_PATH) < CACHE_TTL_S:
            with open(CACHE_PATH) as f:
                cached = json.load(f)
            cached["cached"] = True
            return cached
    except (json.JSONDecodeError, OSError):
        pass

    # ---- fan out over regions + global mil + emergency squawks (one wave) ----
    def scan(region):
        name, lat, lon = region
        data = fetch_json(f"{AIRPLANES_LIVE}/point/{lat:.4f}/{lon:.4f}/{RADIUS_NM}")
        return name, (data or {}).get("ac") or []

    with ThreadPoolExecutor(max_workers=8) as ex:
        region_futs = [ex.submit(scan, r) for r in REGIONS]
        mil_fut = ex.submit(fetch_json, f"{AIRPLANES_LIVE}/mil")
        sq_futs = {sq: ex.submit(fetch_json, f"{AIRPLANES_LIVE}/squawk/{sq}") for sq in ("7700", "7600")}
        results = [f.result() for f in region_futs]
        mil_ac = ((mil_fut.result() or {}).get("ac")) or []
        sq_data = {sq: ((f.result() or {}).get("ac") or []) for sq, f in sq_futs.items()}

    # ---- dedupe by hex, airborne only ----
    seen_hex = set()
    fleet = []  # (region, ac)
    region_counts = {name: 0 for name, _, _ in REGIONS}
    for name, acs in results:
        for ac in acs:
            hex_id = ac.get("hex")
            if not hex_id or hex_id in seen_hex:
                continue
            if ac.get("lat") is None:
                continue
            if ac.get("alt_baro") == "ground":
                continue
            seen_hex.add(hex_id)
            fleet.append((name, ac))
            region_counts[name] += 1

    total = len(fleet)

    airline_ctr = Counter()
    type_ctr = Counter()
    alt_bands = [0, 0, 0, 0, 0]  # <10k, 10-20k, 20-30k, 30-40k, >40k
    mil_count = 0
    fastest = None
    highest = None
    all_pts = []  # [lat, lon, alt] for the living world-map hero

    def alt_val(ac):
        a = ac.get("alt_baro")
        return a if isinstance(a, (int, float)) else None

    for region, ac in fleet:
        cs = (ac.get("flight") or "").strip().upper()
        prefix = re.sub(r"[^A-Z]", "", cs)[:3]
        if len(prefix) == 3 and prefix in AIRLINES:
            airline_ctr[prefix] += 1

        t = (ac.get("t") or "").strip().upper()
        if t:
            type_ctr[t] += 1

        alt = alt_val(ac)
        if alt is not None:
            if alt < 10000:
                alt_bands[0] += 1
            elif alt < 20000:
                alt_bands[1] += 1
            elif alt < 30000:
                alt_bands[2] += 1
            elif alt < 40000:
                alt_bands[3] += 1
            else:
                alt_bands[4] += 1

        lon_ac = ac.get("lon")
        if lon_ac is not None:
            all_pts.append([round(ac["lat"], 2), round(lon_ac, 2), int(alt) if alt is not None else 0])

        flags = ac.get("dbFlags")
        if isinstance(flags, int) and flags & 1:
            mil_count += 1

        gs = ac.get("gs")
        if isinstance(gs, (int, float)) and (fastest is None or gs > fastest["gs"]):
            fastest = {"cs": cs or "—", "type": t or "—", "gs": round(gs), "region": region}

        if alt is not None and (highest is None or alt > highest["alt"]):
            highest = {"cs": cs or "—", "type": t or "—", "alt": int(alt), "region": region}

    regions_out = sorted(
        [{"region": name, "n": region_counts[name]} for name, _, _ in REGIONS],
        key=lambda r: -r["n"],
    )
    airlines_out = [
        {"code": code, "name": AIRLINES[code], "n": n}
        for code, n in airline_ctr.most_common(12)
    ]
    types_out = [
        {"code": code, "name": TYPES.get(code, code), "n": n}
        for code, n in type_ctr.most_common(10)
    ]
    band_labels = ["<10k", "10–20k", "20–30k", "30–40k", ">40k"]
    alt_hist = [{"band": band_labels[i], "n": alt_bands[i]} for i in range(5)]

    # ---- downsample airborne positions for the world-map hero (stride keeps geo spread) ----
    MAX_PTS = 2500
    if len(all_pts) > MAX_PTS:
        stride = len(all_pts) / MAX_PTS
        points = [all_pts[int(i * stride)] for i in range(MAX_PTS)]
    else:
        points = all_pts

    # ---- global military snapshot (/v2/mil) ----
    mil_type_ctr = Counter()
    mil_positioned = []
    for ac in mil_ac:
        mt = (ac.get("t") or "").strip().upper()
        if mt:
            mil_type_ctr[mt] += 1
        if ac.get("lat") is not None and ac.get("lon") is not None:
            malt = alt_val(ac)
            mil_positioned.append({
                "cs": (ac.get("flight") or "").strip() or (ac.get("r") or "").strip() or (ac.get("hex") or "").upper(),
                "type": mt or "—",
                "lat": ac.get("lat"), "lon": ac.get("lon"),
                "alt": int(malt) if malt is not None else None,
                "hex": ac.get("hex"),
            })
    mil_positioned.sort(key=lambda a: a["alt"] if a["alt"] is not None else -1, reverse=True)
    military = {
        "count": len(mil_ac),
        "types": [{"code": c, "n": n} for c, n in mil_type_ctr.most_common(6)],
        "sample": mil_positioned[:5],
    }

    # ---- global emergency squawk sweep (7700/7600), deduped ----
    emergencies = []
    seen_emg = set()
    for sq in ("7700", "7600"):
        for ac in sq_data.get(sq, []):
            if ac.get("lat") is None or ac.get("lon") is None:
                continue
            hx = ac.get("hex")
            if hx in seen_emg:
                continue
            seen_emg.add(hx)
            emergencies.append({
                "squawk": sq,
                "cs": (ac.get("flight") or "").strip() or (ac.get("r") or "").strip() or (hx or "").upper(),
                "type": (ac.get("t") or "").strip().upper() or "—",
                "lat": ac.get("lat"), "lon": ac.get("lon"), "hex": hx,
            })

    out = {
        "ok": True,
        "ts": time.time(),
        "total": total,
        "regions": regions_out,
        "airlines": airlines_out,
        "types": types_out,
        "alt_hist": alt_hist,
        "fastest": fastest,
        "highest": highest,
        "emergencies": emergencies,
        "mil_count": mil_count,
        "military": military,
        "points": points,
        "sampled_note": "12 hub regions, 250 nm each — not full global coverage",
        "cached": False,
    }

    try:
        with open(CACHE_PATH, "w") as f:
            json.dump(out, f)
    except OSError:
        pass

    return out
