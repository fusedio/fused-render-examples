"""Airport activity board — data side of airport.html (FlightAware-style).

Modes:
  board  — live arrivals / departures / overhead / ground within 80 nm of an
           airport, plus current weather (default).
  route  — resolve a callsign's route via adsbdb (cached long; fills the
           origin/destination cell per row lazily). Copied from radar.py.

Runs on Fused's remote Lambda: self-contained, stdlib only, keyless APIs.
All helpers + the airport table live inside main() so the shipped source
carries everything it needs.
"""
import fused

@fused.udf
def main(iata: str = "DEL", mode: str = "board", callsign: str = "") -> dict:
    import concurrent.futures
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

    # ---- curated airport table: iata -> {icao,name,city,country_iso,lat,lon} ----
    AIRPORTS = json.loads(r'''{
      "DEL": {"icao": "VIDP", "name": "Indira Gandhi Intl", "city": "Delhi", "country_iso": "IN", "lat": 28.5665, "lon": 77.1031},
      "BOM": {"icao": "VABB", "name": "Chhatrapati Shivaji Maharaj Intl", "city": "Mumbai", "country_iso": "IN", "lat": 19.0887, "lon": 72.8679},
      "BLR": {"icao": "VOBL", "name": "Kempegowda Intl", "city": "Bengaluru", "country_iso": "IN", "lat": 13.1979, "lon": 77.7063},
      "MAA": {"icao": "VOMM", "name": "Chennai Intl", "city": "Chennai", "country_iso": "IN", "lat": 12.99, "lon": 80.1693},
      "CCU": {"icao": "VECC", "name": "Netaji Subhas Chandra Bose Intl", "city": "Kolkata", "country_iso": "IN", "lat": 22.6547, "lon": 88.4467},
      "HYD": {"icao": "VOHS", "name": "Rajiv Gandhi Intl", "city": "Hyderabad", "country_iso": "IN", "lat": 17.2403, "lon": 78.4294},
      "GOI": {"icao": "VOGO", "name": "Dabolim (Goa)", "city": "Goa", "country_iso": "IN", "lat": 15.3808, "lon": 73.8314},
      "PAT": {"icao": "VEPT", "name": "Jay Prakash Narayan Intl", "city": "Patna", "country_iso": "IN", "lat": 25.5913, "lon": 85.088},
      "AMD": {"icao": "VAAH", "name": "Sardar Vallabhbhai Patel Intl", "city": "Ahmedabad", "country_iso": "IN", "lat": 23.0772, "lon": 72.6347},
      "PNQ": {"icao": "VAPO", "name": "Pune", "city": "Pune", "country_iso": "IN", "lat": 18.5821, "lon": 73.9197},
      "COK": {"icao": "VOCI", "name": "Cochin Intl", "city": "Kochi", "country_iso": "IN", "lat": 10.152, "lon": 76.4019},
      "JAI": {"icao": "VIJP", "name": "Jaipur Intl", "city": "Jaipur", "country_iso": "IN", "lat": 26.8242, "lon": 75.8122},
      "LKO": {"icao": "VILK", "name": "Chaudhary Charan Singh Intl", "city": "Lucknow", "country_iso": "IN", "lat": 26.7606, "lon": 80.8893},
      "IXC": {"icao": "VICG", "name": "Chandigarh Intl", "city": "Chandigarh", "country_iso": "IN", "lat": 30.6735, "lon": 76.7885},
      "GAU": {"icao": "VEGT", "name": "Lokpriya Gopinath Bordoloi Intl", "city": "Guwahati", "country_iso": "IN", "lat": 26.1061, "lon": 91.5859},
      "DXB": {"icao": "OMDB", "name": "Dubai Intl", "city": "Dubai", "country_iso": "AE", "lat": 25.2528, "lon": 55.3644},
      "DOH": {"icao": "OTHH", "name": "Hamad Intl", "city": "Doha", "country_iso": "QA", "lat": 25.2731, "lon": 51.6081},
      "AUH": {"icao": "OMAA", "name": "Zayed Intl", "city": "Abu Dhabi", "country_iso": "AE", "lat": 24.433, "lon": 54.6511},
      "SIN": {"icao": "WSSS", "name": "Changi", "city": "Singapore", "country_iso": "SG", "lat": 1.3644, "lon": 103.9915},
      "KUL": {"icao": "WMKK", "name": "Kuala Lumpur Intl", "city": "Kuala Lumpur", "country_iso": "MY", "lat": 2.7456, "lon": 101.7099},
      "BKK": {"icao": "VTBS", "name": "Suvarnabhumi", "city": "Bangkok", "country_iso": "TH", "lat": 13.681, "lon": 100.7473},
      "HKG": {"icao": "VHHH", "name": "Hong Kong Intl", "city": "Hong Kong", "country_iso": "HK", "lat": 22.308, "lon": 113.9185},
      "NRT": {"icao": "RJAA", "name": "Narita Intl", "city": "Tokyo", "country_iso": "JP", "lat": 35.7647, "lon": 140.3864},
      "HND": {"icao": "RJTT", "name": "Haneda", "city": "Tokyo", "country_iso": "JP", "lat": 35.5494, "lon": 139.7798},
      "ICN": {"icao": "RKSI", "name": "Incheon Intl", "city": "Seoul", "country_iso": "KR", "lat": 37.4602, "lon": 126.4407},
      "PVG": {"icao": "ZSPD", "name": "Shanghai Pudong Intl", "city": "Shanghai", "country_iso": "CN", "lat": 31.1443, "lon": 121.8083},
      "PEK": {"icao": "ZBAA", "name": "Beijing Capital Intl", "city": "Beijing", "country_iso": "CN", "lat": 40.0801, "lon": 116.5846},
      "LHR": {"icao": "EGLL", "name": "Heathrow", "city": "London", "country_iso": "GB", "lat": 51.4706, "lon": -0.4619},
      "CDG": {"icao": "LFPG", "name": "Charles de Gaulle", "city": "Paris", "country_iso": "FR", "lat": 49.0097, "lon": 2.5479},
      "FRA": {"icao": "EDDF", "name": "Frankfurt", "city": "Frankfurt", "country_iso": "DE", "lat": 50.0379, "lon": 8.5622},
      "AMS": {"icao": "EHAM", "name": "Schiphol", "city": "Amsterdam", "country_iso": "NL", "lat": 52.3105, "lon": 4.7683},
      "IST": {"icao": "LTFM", "name": "Istanbul", "city": "Istanbul", "country_iso": "TR", "lat": 41.2753, "lon": 28.7519},
      "ZRH": {"icao": "LSZH", "name": "Zurich", "city": "Zurich", "country_iso": "CH", "lat": 47.4647, "lon": 8.5492},
      "MAD": {"icao": "LEMD", "name": "Adolfo Suarez Madrid-Barajas", "city": "Madrid", "country_iso": "ES", "lat": 40.4936, "lon": -3.5668},
      "FCO": {"icao": "LIRF", "name": "Leonardo da Vinci-Fiumicino", "city": "Rome", "country_iso": "IT", "lat": 41.8003, "lon": 12.2389},
      "JFK": {"icao": "KJFK", "name": "John F. Kennedy Intl", "city": "New York", "country_iso": "US", "lat": 40.6413, "lon": -73.7781},
      "EWR": {"icao": "KEWR", "name": "Newark Liberty Intl", "city": "Newark", "country_iso": "US", "lat": 40.6895, "lon": -74.1745},
      "LAX": {"icao": "KLAX", "name": "Los Angeles Intl", "city": "Los Angeles", "country_iso": "US", "lat": 33.9416, "lon": -118.4085},
      "ORD": {"icao": "KORD", "name": "O'Hare Intl", "city": "Chicago", "country_iso": "US", "lat": 41.9742, "lon": -87.9073},
      "SFO": {"icao": "KSFO", "name": "San Francisco Intl", "city": "San Francisco", "country_iso": "US", "lat": 37.6213, "lon": -122.379},
      "SEA": {"icao": "KSEA", "name": "Seattle-Tacoma Intl", "city": "Seattle", "country_iso": "US", "lat": 47.4502, "lon": -122.3088},
      "MIA": {"icao": "KMIA", "name": "Miami Intl", "city": "Miami", "country_iso": "US", "lat": 25.7959, "lon": -80.287},
      "ATL": {"icao": "KATL", "name": "Hartsfield-Jackson Atlanta Intl", "city": "Atlanta", "country_iso": "US", "lat": 33.6407, "lon": -84.4277},
      "DFW": {"icao": "KDFW", "name": "Dallas/Fort Worth Intl", "city": "Dallas", "country_iso": "US", "lat": 32.8998, "lon": -97.0403},
      "YYZ": {"icao": "CYYZ", "name": "Toronto Pearson Intl", "city": "Toronto", "country_iso": "CA", "lat": 43.6777, "lon": -79.6248},
      "SYD": {"icao": "YSSY", "name": "Sydney Kingsford Smith", "city": "Sydney", "country_iso": "AU", "lat": -33.9399, "lon": 151.1753},
      "MEL": {"icao": "YMML", "name": "Melbourne", "city": "Melbourne", "country_iso": "AU", "lat": -37.669, "lon": 144.841},
      "AKL": {"icao": "NZAA", "name": "Auckland", "city": "Auckland", "country_iso": "NZ", "lat": -37.0082, "lon": 174.792},
      "GRU": {"icao": "SBGR", "name": "Guarulhos - Governador Andre Franco Montoro Intl", "city": "Sao Paulo", "country_iso": "BR", "lat": -23.4356, "lon": -46.4731},
      "EZE": {"icao": "SAEZ", "name": "Ministro Pistarini Intl", "city": "Buenos Aires", "country_iso": "AR", "lat": -34.8222, "lon": -58.5358},
      "JNB": {"icao": "FAOR", "name": "O. R. Tambo Intl", "city": "Johannesburg", "country_iso": "ZA", "lat": -26.1367, "lon": 28.2411},
      "CAI": {"icao": "HECA", "name": "Cairo Intl", "city": "Cairo", "country_iso": "EG", "lat": 30.1219, "lon": 31.4056},
      "NBO": {"icao": "HKJK", "name": "Jomo Kenyatta Intl", "city": "Nairobi", "country_iso": "KE", "lat": -1.3192, "lon": 36.9278},
      "MEX": {"icao": "MMMX", "name": "Benito Juarez Intl", "city": "Mexico City", "country_iso": "MX", "lat": 19.4363, "lon": -99.0721}
    }''')

    # ---- runway layouts: iata -> [[ident_pair, low-end heading], ...] ----
    # Headings are the low ident number x 10 (e.g. "27" -> 270). Not survey-grade;
    # enough to compute headwind/crosswind against the reported surface wind.
    RUNWAYS = {
        "DEL": [["09/27", 90], ["10/28", 100], ["11R/29L", 110]],
        "BOM": [["09/27", 90], ["14/32", 140]],
        "BLR": [["09L/27R", 90], ["09R/27L", 90]],
        "MAA": [["07/25", 70], ["12/30", 120]],
        "CCU": [["01L/19R", 10], ["01R/19L", 10]],
        "HYD": [["09L/27R", 90], ["09R/27L", 90]],
        "GOI": [["08/26", 80]],
        "PAT": [["07/25", 70]],
        "AMD": [["05/23", 50]],
        "PNQ": [["10/28", 100]],
        "COK": [["09/27", 90]],
        "JAI": [["09/27", 90]],
        "LKO": [["09/27", 90], ["14/32", 140]],
        "IXC": [["11/29", 110]],
        "GAU": [["02/20", 20]],
        "DXB": [["12L/30R", 120], ["12R/30L", 120]],
        "DOH": [["16L/34R", 160], ["16R/34L", 160]],
        "AUH": [["13L/31R", 130], ["13R/31L", 130]],
        "SIN": [["02L/20R", 20], ["02C/20C", 20], ["02R/20L", 20]],
        "KUL": [["14L/32R", 140], ["14R/32L", 140], ["15/33", 150]],
        "BKK": [["01L/19R", 10], ["01R/19L", 10]],
        "HKG": [["07L/25R", 70], ["07R/25L", 70]],
        "NRT": [["16L/34R", 160], ["16R/34L", 160]],
        "HND": [["16L/34R", 160], ["04/22", 40], ["05/23", 50]],
        "ICN": [["15L/33R", 150], ["15R/33L", 150], ["16/34", 160]],
        "PVG": [["16L/34R", 160], ["17L/35R", 170]],
        "PEK": [["18L/36R", 180], ["18R/36L", 180], ["01/19", 10]],
        "LHR": [["09L/27R", 90], ["09R/27L", 90]],
        "CDG": [["09L/27R", 90], ["08R/26L", 80]],
        "FRA": [["07L/25R", 70], ["07R/25L", 70], ["18/36", 180]],
        "AMS": [["18R/36L", 180], ["06/24", 60], ["09/27", 90]],
        "IST": [["16L/34R", 160], ["17R/35L", 170], ["18/36", 180]],
        "ZRH": [["16/34", 160], ["14/32", 140], ["10/28", 100]],
        "MAD": [["18L/36R", 180], ["18R/36L", 180], ["14L/32R", 140]],
        "FCO": [["16C/34C", 160], ["16L/34R", 160], ["07/25", 70]],
        "JFK": [["04L/22R", 40], ["04R/22L", 40], ["13L/31R", 130], ["13R/31L", 130]],
        "EWR": [["04L/22R", 40], ["04R/22L", 40], ["11/29", 110]],
        "LAX": [["06L/24R", 60], ["06R/24L", 60], ["07R/25L", 70]],
        "ORD": [["10L/28R", 100], ["10C/28C", 100], ["09L/27R", 90]],
        "SFO": [["10L/28R", 100], ["10R/28L", 100], ["01R/19L", 10]],
        "SEA": [["16L/34R", 160], ["16C/34C", 160], ["16R/34L", 160]],
        "MIA": [["08L/26R", 80], ["09/27", 90], ["12/30", 120]],
        "ATL": [["09L/27R", 90], ["08R/26L", 80], ["10/28", 100]],
        "DFW": [["17C/35C", 170], ["18R/36L", 180], ["13R/31L", 130]],
        "YYZ": [["06L/24R", 60], ["05/23", 50], ["15L/33R", 150]],
        "SYD": [["16R/34L", 160], ["16L/34R", 160], ["07/25", 70]],
        "MEL": [["16/34", 160], ["09/27", 90]],
        "AKL": [["05/23", 50]],
        "GRU": [["09L/27R", 90], ["09R/27L", 90]],
        "EZE": [["11/29", 110], ["17/35", 170]],
        "JNB": [["03L/21R", 30], ["03R/21L", 30]],
        "CAI": [["05L/23R", 50], ["05R/23L", 50], ["16/34", 160]],
        "NBO": [["06/24", 60]],
        "MEX": [["05L/23R", 50], ["05R/23L", 50]],
    }

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

    # ============================ route mode ============================
    # (copied verbatim from radar.py so the HTML can lazily fill each row)
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

    # ============================ board mode ============================
    code = re.sub(r"[^A-Z0-9]", "", (iata or "").upper())
    ap = AIRPORTS.get(code)
    if not ap:
        return {
            "ok": False,
            "error": f"Unknown airport '{iata}'. Try one of the {len(AIRPORTS)} known IATA codes.",
            "known": sorted(AIRPORTS.keys()),
        }

    a_lat, a_lon = ap["lat"], ap["lon"]

    def haversine_nm(lat1, lon1, lat2, lon2):
        r_km = 6371.0
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
        h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return (2 * r_km * math.asin(math.sqrt(h))) / 1.852

    def bearing_deg(lat1, lon1, lat2, lon2):
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dl = math.radians(lon2 - lon1)
        y = math.sin(dl) * math.cos(p2)
        x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
        return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0

    def angle_diff(a, b):
        d = abs((a - b) % 360.0)
        return d if d <= 180 else 360.0 - d

    # ---- weather (open-meteo, keyless, 30 min cache) ----
    WMO = {
        0: "Clear skies", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
        45: "Fog", 48: "Depositing rime fog",
        51: "Light drizzle", 53: "Drizzle", 55: "Dense drizzle",
        56: "Freezing drizzle", 57: "Freezing drizzle",
        61: "Light rain", 63: "Rain", 65: "Heavy rain",
        66: "Freezing rain", 67: "Freezing rain",
        71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
        80: "Light showers", 81: "Showers", 82: "Violent showers",
        85: "Snow showers", 86: "Snow showers",
        95: "Thunderstorm", 96: "Thunderstorm, hail", 99: "Thunderstorm, heavy hail",
    }

    def weather_at(lat, lon, key):
        url = (
            "https://api.open-meteo.com/v1/forecast?latitude=%.4f&longitude=%.4f"
            "&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m,"
            "wind_direction_10m,surface_pressure,is_day"
            "&hourly=visibility&forecast_hours=1&wind_speed_unit=kn&timezone=auto" % (lat, lon)
        )
        data = cached_fetch("wx", key, url, ttl=1800)
        cur = (data or {}).get("current")
        if not cur:
            return None
        vis_km = None
        try:
            vis_km = round(data["hourly"]["visibility"][0] / 1000)
        except (KeyError, IndexError, TypeError):
            pass
        return {
            "temp_c": cur.get("temperature_2m"),
            "condition": WMO.get(cur.get("weather_code"), "—"),
            "is_day": cur.get("is_day"),
            "wind_kt": cur.get("wind_speed_10m"),
            "wind_dir": cur.get("wind_direction_10m"),
            "humidity_pct": cur.get("relative_humidity_2m"),
            "pressure_hpa": cur.get("surface_pressure"),
            "visibility_km": vis_km,
            "local_time": cur.get("time"),
        }

    # ---- real aviation weather (aviationweather.gov, keyless) ----
    def metar_at(icao, key):
        url = f"https://aviationweather.gov/api/data/metar?ids={icao}&format=json"
        arr = cached_fetch("metar", key, url, ttl=600)  # 10 min
        if not isinstance(arr, list) or not arr:
            return None
        m = arr[0]
        clouds = [
            {"cover": c.get("cover"), "base": c.get("base")}
            for c in (m.get("clouds") or [])
            if isinstance(c.get("base"), (int, float))
        ]
        ceiling = None
        for c in clouds:
            if c["cover"] in ("BKN", "OVC"):
                ceiling = c["base"] if ceiling is None else min(ceiling, c["base"])
        obs_age = None
        ot = m.get("obsTime")
        if isinstance(ot, (int, float)):
            obs_age = max(0, int(round((time.time() - ot) / 60)))
        vis = m.get("visib")
        vis_km = None
        if isinstance(vis, (int, float)):
            vis_km = round(vis * 1.609)
        elif isinstance(vis, str):
            mm = re.match(r"([0-9.]+)", vis)
            if mm:
                vis_km = round(float(mm.group(1)) * 1.609)
        wdir = m.get("wdir")
        if not isinstance(wdir, (int, float)):
            wdir = None
        return {
            "raw": m.get("rawOb"),
            "temp_c": m.get("temp"),
            "dewp_c": m.get("dewp"),
            "wind_dir": wdir,
            "wind_kt": m.get("wspd"),
            "visib_km": vis_km,
            "altim_hpa": round(m["altim"]) if isinstance(m.get("altim"), (int, float)) else None,
            "clouds": clouds,
            "ceiling_ft": ceiling,
            "wx": m.get("wxString"),
            "obs_age_min": obs_age,
        }

    def taf_at(icao, key):
        url = f"https://aviationweather.gov/api/data/taf?ids={icao}&format=json"
        arr = cached_fetch("taf", key, url, ttl=1800)  # 30 min
        if not isinstance(arr, list) or not arr:
            return None
        return arr[0].get("rawTAF")

    # ---- parallel fetch: open-meteo + METAR + TAF + traffic ----
    icao = ap["icao"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as _ex:
        _f_wx = _ex.submit(weather_at, a_lat, a_lon, code)
        _f_metar = _ex.submit(metar_at, icao, code)
        _f_taf = _ex.submit(taf_at, icao, code)
        _f_traffic = _ex.submit(fetch_json, f"{AIRPLANES_LIVE}/point/{a_lat:.4f}/{a_lon:.4f}/80")
        weather = _f_wx.result()
        metar = _f_metar.result()
        taf_raw = _f_taf.result()
        data = _f_traffic.result()

    # ---- runway wind rose: headwind/crosswind per runway from surface wind ----
    rw_dir = rw_kt = None
    if metar and isinstance(metar.get("wind_dir"), (int, float)) and isinstance(metar.get("wind_kt"), (int, float)):
        rw_dir, rw_kt = metar["wind_dir"], metar["wind_kt"]
    elif weather and isinstance(weather.get("wind_dir"), (int, float)) and isinstance(weather.get("wind_kt"), (int, float)):
        rw_dir, rw_kt = weather["wind_dir"], weather["wind_kt"]

    runways_out = []
    best_runway = None
    for pair, _base in RUNWAYS.get(code, []):
        best_end = None  # (headwind, dict)
        for end in pair.split("/"):
            mnum = re.match(r"(\d+)", end)
            if not mnum:
                continue
            hdg = (int(mnum.group(1)) * 10) % 360
            if rw_dir is None or rw_kt is None:
                head = cross = 0.0
            else:
                delta = math.radians(rw_dir - hdg)
                head = rw_kt * math.cos(delta)
                cross = abs(rw_kt * math.sin(delta))
            cand = {"ident": end, "hdg": hdg, "head_kt": round(head), "cross_kt": round(cross)}
            if best_end is None or head > best_end[0]:
                best_end = (head, cand)
        if best_end is not None:
            runways_out.append(best_end[1])
            if best_runway is None or best_end[1]["head_kt"] > best_runway["head_kt"]:
                best_runway = best_end[1]

    arrivals, departures = [], []
    overhead_count = ground_count = 0

    for ac in (data or {}).get("ac") or []:
        p_lat, p_lon = ac.get("lat"), ac.get("lon")
        if p_lat is None or p_lon is None:
            continue
        alt_raw = ac.get("alt_baro")
        on_ground = alt_raw == "ground"
        alt = 0 if on_ground else (alt_raw if isinstance(alt_raw, (int, float)) else None)
        gs = ac.get("gs")
        vr = ac.get("baro_rate")
        track = ac.get("track")
        dist_nm = round(haversine_nm(a_lat, a_lon, p_lat, p_lon), 1)

        row = {
            "hex": ac.get("hex"),
            "cs": (ac.get("flight") or "").strip(),
            "reg": ac.get("r"),
            "type": ac.get("t"),
            "desc": ac.get("desc"),
            "alt": alt,
            "gs": round(gs) if isinstance(gs, (int, float)) else None,
            "vr": vr if isinstance(vr, (int, float)) else None,
            "track": round(track) if isinstance(track, (int, float)) else None,
            "dist_nm": dist_nm,
            "squawk": ac.get("squawk"),
            "lat": p_lat,
            "lon": p_lon,
        }

        if on_ground:
            ground_count += 1
            continue

        # airborne classification
        classified = False
        if isinstance(alt, (int, float)) and alt < 20000 and isinstance(track, (int, float)):
            brng_to_field = bearing_deg(p_lat, p_lon, a_lat, a_lon)
            brng_from_field = bearing_deg(a_lat, a_lon, p_lat, p_lon)
            vrate = vr if isinstance(vr, (int, float)) else 0
            if vrate < 200 and angle_diff(track, brng_to_field) <= 55:
                spd = gs if isinstance(gs, (int, float)) and gs > 0 else 80
                row["eta_min"] = int(round(dist_nm / max(spd, 80) * 60))
                arrivals.append(row)
                classified = True
            elif vrate > 300 and angle_diff(track, brng_from_field) <= 70:
                departures.append(row)
                classified = True
        if not classified:
            overhead_count += 1

    arrivals.sort(key=lambda r: r.get("eta_min", 9999))
    departures.sort(key=lambda r: r["dist_nm"])

    return {
        "ok": True,
        "airport": {
            "iata": code, "icao": ap["icao"], "name": ap["name"], "city": ap["city"],
            "country_iso": ap["country_iso"], "lat": a_lat, "lon": a_lon,
        },
        "weather": weather,
        "metar": metar,
        "taf_raw": taf_raw,
        "runways": runways_out,
        "best_runway": best_runway,
        "wind_used": ({"dir": rw_dir, "kt": rw_kt} if rw_dir is not None and rw_kt is not None else None),
        "arrivals": arrivals,
        "departures": departures,
        "overhead_count": overhead_count,
        "ground_count": ground_count,
        "ts": time.time(),
    }
