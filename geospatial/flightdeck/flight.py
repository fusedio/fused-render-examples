"""Flightdeck data resolver.

Resolves a flight number (IATA or ICAO callsign form) into a full flight
intelligence record: route, airline, airports, aircraft, amenities, live
position. Keyless sources: adsbdb.com (route/airline/aircraft) and
airplanes.live (live ADS-B position).

The UDF may execute on a remote runtime where only this function's source
exists, so everything -- helpers and the curated aircraft/airline data --
lives inside main(). Route lookups are cached in the temp dir.
Curated data is mirrored in data/aircraft.json and data/airlines.json for
editing; re-inline after changing those.
"""

import fused


@fused.udf
def main(flight: str = "", vs: str = "", live: bool = True) -> dict:
    import json
    import math
    import os
    import re
    import tempfile
    import time
    import urllib.error
    import urllib.request

    ADSBDB = "https://api.adsbdb.com/v0"
    AIRPLANES_LIVE = "https://api.airplanes.live/v2"
    CACHE_DIR = os.path.join(tempfile.gettempdir(), "flightdeck_cache")
    CACHE_TTL_S = 7 * 24 * 3600
    HTTP_TIMEOUT_S = 8
    JET_AVG_KMH = 780.0  # block-average incl. climb/descent
    TURBOPROP_AVG_KMH = 450.0

    AIRCRAFT = json.loads(r'''{"A20N": {"name": "Airbus A320neo", "manufacturer": "Airbus", "class": "narrowbody", "engines": 2, "engine_type": "CFM LEAP-1A / PW1100G", "length_m": 37.57, "wingspan_m": 35.8, "height_m": 11.76, "cruise_kmh": 833, "cruise_mach": 0.78, "range_km": 6300, "ceiling_ft": 39800, "typical_seats": "165–186"}, "A21N": {"name": "Airbus A321neo", "manufacturer": "Airbus", "class": "narrowbody", "engines": 2, "engine_type": "CFM LEAP-1A / PW1100G", "length_m": 44.51, "wingspan_m": 35.8, "height_m": 11.76, "cruise_kmh": 833, "cruise_mach": 0.78, "range_km": 7400, "ceiling_ft": 39800, "typical_seats": "180–232"}, "A320": {"name": "Airbus A320ceo", "manufacturer": "Airbus", "class": "narrowbody", "engines": 2, "engine_type": "CFM56 / IAE V2500", "length_m": 37.57, "wingspan_m": 35.8, "height_m": 11.76, "cruise_kmh": 828, "cruise_mach": 0.78, "range_km": 6100, "ceiling_ft": 39100, "typical_seats": "150–180"}, "A321": {"name": "Airbus A321ceo", "manufacturer": "Airbus", "class": "narrowbody", "engines": 2, "engine_type": "CFM56 / IAE V2500", "length_m": 44.51, "wingspan_m": 35.8, "height_m": 11.76, "cruise_kmh": 828, "cruise_mach": 0.78, "range_km": 5950, "ceiling_ft": 39100, "typical_seats": "185–220"}, "A319": {"name": "Airbus A319", "manufacturer": "Airbus", "class": "narrowbody", "engines": 2, "engine_type": "CFM56 / IAE V2500", "length_m": 33.84, "wingspan_m": 35.8, "height_m": 11.76, "cruise_kmh": 828, "cruise_mach": 0.78, "range_km": 6850, "ceiling_ft": 39100, "typical_seats": "124–156"}, "B738": {"name": "Boeing 737-800", "manufacturer": "Boeing", "class": "narrowbody", "engines": 2, "engine_type": "CFM56-7B", "length_m": 39.5, "wingspan_m": 35.8, "height_m": 12.5, "cruise_kmh": 842, "cruise_mach": 0.785, "range_km": 5765, "ceiling_ft": 41000, "typical_seats": "162–189"}, "B38M": {"name": "Boeing 737 MAX 8", "manufacturer": "Boeing", "class": "narrowbody", "engines": 2, "engine_type": "CFM LEAP-1B", "length_m": 39.52, "wingspan_m": 35.9, "height_m": 12.3, "cruise_kmh": 839, "cruise_mach": 0.79, "range_km": 6570, "ceiling_ft": 41000, "typical_seats": "162–210"}, "B39M": {"name": "Boeing 737 MAX 9", "manufacturer": "Boeing", "class": "narrowbody", "engines": 2, "engine_type": "CFM LEAP-1B", "length_m": 42.16, "wingspan_m": 35.9, "height_m": 12.3, "cruise_kmh": 839, "cruise_mach": 0.79, "range_km": 6570, "ceiling_ft": 41000, "typical_seats": "178–220"}, "B739": {"name": "Boeing 737-900", "manufacturer": "Boeing", "class": "narrowbody", "engines": 2, "engine_type": "CFM56-7B", "length_m": 42.1, "wingspan_m": 35.8, "height_m": 12.5, "cruise_kmh": 842, "cruise_mach": 0.785, "range_km": 5460, "ceiling_ft": 41000, "typical_seats": "177–215"}, "B788": {"name": "Boeing 787-8 Dreamliner", "manufacturer": "Boeing", "class": "widebody", "engines": 2, "engine_type": "GEnx-1B / Trent 1000", "length_m": 56.72, "wingspan_m": 60.12, "height_m": 16.92, "cruise_kmh": 903, "cruise_mach": 0.85, "range_km": 13530, "ceiling_ft": 43000, "typical_seats": "242–290"}, "B789": {"name": "Boeing 787-9 Dreamliner", "manufacturer": "Boeing", "class": "widebody", "engines": 2, "engine_type": "GEnx-1B / Trent 1000", "length_m": 62.81, "wingspan_m": 60.12, "height_m": 17.02, "cruise_kmh": 903, "cruise_mach": 0.85, "range_km": 14140, "ceiling_ft": 43000, "typical_seats": "280–296"}, "B78X": {"name": "Boeing 787-10 Dreamliner", "manufacturer": "Boeing", "class": "widebody", "engines": 2, "engine_type": "GEnx-1B / Trent 1000", "length_m": 68.28, "wingspan_m": 60.12, "height_m": 17.02, "cruise_kmh": 903, "cruise_mach": 0.85, "range_km": 11910, "ceiling_ft": 43000, "typical_seats": "318–336"}, "B77W": {"name": "Boeing 777-300ER", "manufacturer": "Boeing", "class": "widebody", "engines": 2, "engine_type": "GE90-115B", "length_m": 73.86, "wingspan_m": 64.8, "height_m": 18.5, "cruise_kmh": 892, "cruise_mach": 0.84, "range_km": 13650, "ceiling_ft": 43100, "typical_seats": "342–396"}, "B77L": {"name": "Boeing 777-200LR", "manufacturer": "Boeing", "class": "widebody", "engines": 2, "engine_type": "GE90-110B", "length_m": 63.73, "wingspan_m": 64.8, "height_m": 18.6, "cruise_kmh": 892, "cruise_mach": 0.84, "range_km": 15840, "ceiling_ft": 43100, "typical_seats": "238–301"}, "B772": {"name": "Boeing 777-200ER", "manufacturer": "Boeing", "class": "widebody", "engines": 2, "engine_type": "GE90 / Trent 800 / PW4000", "length_m": 63.73, "wingspan_m": 60.93, "height_m": 18.5, "cruise_kmh": 892, "cruise_mach": 0.84, "range_km": 13080, "ceiling_ft": 43100, "typical_seats": "280–320"}, "B773": {"name": "Boeing 777-300", "manufacturer": "Boeing", "class": "widebody", "engines": 2, "engine_type": "Trent 892 / PW4098", "length_m": 73.86, "wingspan_m": 60.93, "height_m": 18.5, "cruise_kmh": 892, "cruise_mach": 0.84, "range_km": 11120, "ceiling_ft": 43100, "typical_seats": "368–396"}, "A359": {"name": "Airbus A350-900", "manufacturer": "Airbus", "class": "widebody", "engines": 2, "engine_type": "Trent XWB-84", "length_m": 66.8, "wingspan_m": 64.75, "height_m": 17.05, "cruise_kmh": 903, "cruise_mach": 0.85, "range_km": 15000, "ceiling_ft": 41450, "typical_seats": "300–350"}, "A35K": {"name": "Airbus A350-1000", "manufacturer": "Airbus", "class": "widebody", "engines": 2, "engine_type": "Trent XWB-97", "length_m": 73.79, "wingspan_m": 64.75, "height_m": 17.08, "cruise_kmh": 903, "cruise_mach": 0.85, "range_km": 16100, "ceiling_ft": 41450, "typical_seats": "350–410"}, "A333": {"name": "Airbus A330-300", "manufacturer": "Airbus", "class": "widebody", "engines": 2, "engine_type": "Trent 700 / CF6 / PW4000", "length_m": 63.67, "wingspan_m": 60.3, "height_m": 16.79, "cruise_kmh": 871, "cruise_mach": 0.82, "range_km": 11750, "ceiling_ft": 41100, "typical_seats": "277–300"}, "A332": {"name": "Airbus A330-200", "manufacturer": "Airbus", "class": "widebody", "engines": 2, "engine_type": "Trent 700 / CF6 / PW4000", "length_m": 58.82, "wingspan_m": 60.3, "height_m": 17.39, "cruise_kmh": 871, "cruise_mach": 0.82, "range_km": 13450, "ceiling_ft": 41100, "typical_seats": "246–293"}, "A339": {"name": "Airbus A330-900neo", "manufacturer": "Airbus", "class": "widebody", "engines": 2, "engine_type": "Trent 7000", "length_m": 63.66, "wingspan_m": 64.0, "height_m": 16.79, "cruise_kmh": 871, "cruise_mach": 0.82, "range_km": 13334, "ceiling_ft": 41100, "typical_seats": "260–300"}, "B744": {"name": "Boeing 747-400", "manufacturer": "Boeing", "class": "jumbo", "engines": 4, "engine_type": "CF6 / PW4000 / RB211", "length_m": 70.66, "wingspan_m": 64.44, "height_m": 19.4, "cruise_kmh": 913, "cruise_mach": 0.855, "range_km": 13450, "ceiling_ft": 45100, "typical_seats": "400–420"}, "A388": {"name": "Airbus A380-800", "manufacturer": "Airbus", "class": "jumbo", "engines": 4, "engine_type": "Trent 900 / GP7200", "length_m": 72.72, "wingspan_m": 79.75, "height_m": 24.09, "cruise_kmh": 903, "cruise_mach": 0.85, "range_km": 14800, "ceiling_ft": 43100, "typical_seats": "489–615"}, "AT76": {"name": "ATR 72-600", "manufacturer": "ATR", "class": "turboprop", "engines": 2, "engine_type": "PW127M turboprop", "length_m": 27.17, "wingspan_m": 27.05, "height_m": 7.65, "cruise_kmh": 510, "cruise_mach": 0.45, "range_km": 1528, "ceiling_ft": 25000, "typical_seats": "70–78"}, "AT75": {"name": "ATR 72-500", "manufacturer": "ATR", "class": "turboprop", "engines": 2, "engine_type": "PW127F turboprop", "length_m": 27.17, "wingspan_m": 27.05, "height_m": 7.65, "cruise_kmh": 510, "cruise_mach": 0.45, "range_km": 1580, "ceiling_ft": 25000, "typical_seats": "68–74"}, "DH8D": {"name": "De Havilland Dash 8-400", "manufacturer": "De Havilland Canada", "class": "turboprop", "engines": 2, "engine_type": "PW150A turboprop", "length_m": 32.83, "wingspan_m": 28.42, "height_m": 8.34, "cruise_kmh": 667, "cruise_mach": 0.6, "range_km": 2040, "ceiling_ft": 27000, "typical_seats": "78–90"}, "E195": {"name": "Embraer E195", "manufacturer": "Embraer", "class": "regional", "engines": 2, "engine_type": "CF34-10E", "length_m": 38.65, "wingspan_m": 28.72, "height_m": 10.55, "cruise_kmh": 829, "cruise_mach": 0.78, "range_km": 4260, "ceiling_ft": 41000, "typical_seats": "100–124"}, "E190": {"name": "Embraer E190", "manufacturer": "Embraer", "class": "regional", "engines": 2, "engine_type": "CF34-10E", "length_m": 36.24, "wingspan_m": 28.72, "height_m": 10.55, "cruise_kmh": 829, "cruise_mach": 0.78, "range_km": 4537, "ceiling_ft": 41000, "typical_seats": "96–114"}, "B763": {"name": "Boeing 767-300ER", "manufacturer": "Boeing", "class": "widebody", "engines": 2, "engine_type": "CF6 / PW4000", "length_m": 54.94, "wingspan_m": 47.57, "height_m": 15.85, "cruise_kmh": 851, "cruise_mach": 0.8, "range_km": 11070, "ceiling_ft": 43100, "typical_seats": "218–269"}, "B752": {"name": "Boeing 757-200", "manufacturer": "Boeing", "class": "narrowbody", "engines": 2, "engine_type": "RB211 / PW2000", "length_m": 47.3, "wingspan_m": 38.05, "height_m": 13.56, "cruise_kmh": 850, "cruise_mach": 0.8, "range_km": 7250, "ceiling_ft": 42000, "typical_seats": "200–228"}}''')

    AIRLINES = json.loads(r'''{"AI": {"name": "Air India", "region": "India", "alliance": "Star Alliance", "loyalty": "Maharaja Club", "cabins": ["Economy", "Premium Economy", "Business", "First (select 777s)"], "wifi": "Wi-Fi on A350, B787-9 and select A321neo; rolling out fleet-wide", "ife": "Seatback screens on widebodies and new A350/A321neo; older narrowbodies vary", "power": "USB on most aircraft; AC power in premium cabins and new deliveries", "meals": "Complimentary hot meals and beverages on all sectors", "seat_pitch": {"economy": "30–33\"", "premium_economy": "38\"", "business": "flat beds on widebodies"}, "baggage": "15–25 kg checked (domestic, fare-dependent); 2×23 kg on most international", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 2800, "types": {"short": "A20N", "medium": "A21N", "long": "B788", "ultra_long": "B77W"}}}, "IX": {"name": "Air India Express", "region": "India", "alliance": null, "loyalty": "Maharaja Club", "cabins": ["Economy"], "wifi": "Not offered", "ife": "Streaming to own device on select MAX 8s (AiXconnect)", "power": "USB on newer 737 MAX 8", "meals": "Buy-on-board (Gourmair); free meal on select international fares", "seat_pitch": {"economy": "28–30\""}, "baggage": "15 kg checked (fare-dependent); 20–30 kg international", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 99999, "types": {"short": "B738", "medium": "B38M", "long": "B38M", "ultra_long": "B38M"}}}, "6E": {"name": "IndiGo", "region": "India", "alliance": null, "loyalty": "IndiGo BluChip", "cabins": ["Economy", "IndiGoStretch (select A321/A350 orders)"], "wifi": "Free basic Wi-Fi rolling out from 2025 (Starlink-backed) on select aircraft", "ife": "No seatback screens; streaming entertainment on select routes", "power": "USB on A321neo and newer deliveries", "meals": "Buy-on-board snacks; complimentary meal in IndiGoStretch", "seat_pitch": {"economy": "28–30\"", "business": "IndiGoStretch 38\" recliner"}, "baggage": "15 kg checked domestic (fare-dependent)", "fleet_rules": {"turboprop_below_km": 600, "narrowbody_below_km": 99999, "types": {"turboprop": "AT76", "short": "A20N", "medium": "A21N", "long": "A21N", "ultra_long": "B789"}}}, "QP": {"name": "Akasa Air", "region": "India", "alliance": null, "loyalty": null, "cabins": ["Economy"], "wifi": "Not offered", "ife": "No seatback screens", "power": "USB-A + USB-C at every seat", "meals": "Buy-on-board (Café Akasa)", "seat_pitch": {"economy": "28–31\""}, "baggage": "15 kg checked (fare-dependent)", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 99999, "types": {"short": "B38M", "medium": "B38M", "long": "B38M", "ultra_long": "B38M"}}}, "SG": {"name": "SpiceJet", "region": "India", "alliance": null, "loyalty": "SpiceClub", "cabins": ["Economy", "SpiceMax (extra legroom)"], "wifi": "Not offered", "ife": "No seatback screens", "power": "Not standard", "meals": "Buy-on-board; complimentary in SpiceMax", "seat_pitch": {"economy": "28–29\"", "premium_economy": "SpiceMax 32–34\""}, "baggage": "15 kg checked (fare-dependent)", "fleet_rules": {"turboprop_below_km": 600, "narrowbody_below_km": 99999, "types": {"turboprop": "DH8D", "short": "B738", "medium": "B738", "long": "B38M", "ultra_long": "B38M"}}}, "UK": {"name": "Vistara (merged into Air India)", "region": "India", "alliance": "Star Alliance", "loyalty": "Maharaja Club", "cabins": ["Economy", "Premium Economy", "Business"], "wifi": "On B787-9 and A321neo international", "ife": "Seatback on widebodies/A321neo; streaming on A320neo", "power": "USB across fleet; AC in premium cabins", "meals": "Complimentary meals all cabins", "seat_pitch": {"economy": "30–32\"", "premium_economy": "33–36\"", "business": "flat beds on 787/A321neo"}, "baggage": "15–25 kg checked (fare-dependent)", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 2800, "types": {"short": "A20N", "medium": "A21N", "long": "B789", "ultra_long": "B789"}}}, "EK": {"name": "Emirates", "region": "UAE", "alliance": null, "loyalty": "Skywards", "cabins": ["Economy", "Premium Economy", "Business", "First"], "wifi": "Free messaging for Skywards members; paid full access, free in premium tiers", "ife": "ice — 4K screens up to 13.3\" economy, industry-leading library", "power": "AC + USB at every seat", "meals": "Complimentary multi-course regional menus, all cabins", "seat_pitch": {"economy": "31–33\"", "premium_economy": "40\"", "business": "flat beds", "first": "suites with doors; shower spa on A380"}, "baggage": "25–35 kg economy (fare-dependent)", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 0, "types": {"short": "B77W", "medium": "B77W", "long": "A388", "ultra_long": "A388"}}}, "QR": {"name": "Qatar Airways", "region": "Qatar", "alliance": "oneworld", "loyalty": "Privilege Club", "cabins": ["Economy", "Business (Qsuite)", "First (A380)"], "wifi": "Super Wi-Fi (Starlink) free on much of the fleet", "ife": "Oryx One seatback across fleet", "power": "AC + USB at every seat", "meals": "Complimentary dining all cabins; dine-on-demand in Qsuite", "seat_pitch": {"economy": "31–32\"", "business": "Qsuite — doors, double beds, quad suites"}, "baggage": "25–30 kg economy (fare-dependent)", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 3000, "types": {"short": "A320", "medium": "B788", "long": "A359", "ultra_long": "A35K"}}}, "SQ": {"name": "Singapore Airlines", "region": "Singapore", "alliance": "Star Alliance", "loyalty": "KrisFlyer", "cabins": ["Economy", "Premium Economy", "Business", "Suites (A380)"], "wifi": "Free unlimited Wi-Fi for all passengers (KrisFlyer login)", "ife": "KrisWorld seatback across fleet", "power": "AC + USB at every seat", "meals": "Complimentary; Book the Cook pre-order in premium cabins", "seat_pitch": {"economy": "32\"", "premium_economy": "38\"", "business": "flat beds", "first": "Suites with separate bed"}, "baggage": "25–30 kg economy", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 1500, "types": {"short": "B738", "medium": "A359", "long": "A359", "ultra_long": "A359"}}}, "EY": {"name": "Etihad Airways", "region": "UAE", "alliance": null, "loyalty": "Etihad Guest", "cabins": ["Economy", "Business", "First (Apartments on A380)"], "wifi": "Paid tiers; free chat for Guest members on most aircraft", "ife": "E-BOX seatback across fleet", "power": "AC + USB at every seat", "meals": "Complimentary all cabins", "seat_pitch": {"economy": "31–33\"", "business": "Business Studios, flat beds", "first": "The Apartment / Residence on A380"}, "baggage": "23–35 kg economy (fare-dependent)", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 3000, "types": {"short": "A21N", "medium": "B789", "long": "B789", "ultra_long": "A35K"}}}, "BA": {"name": "British Airways", "region": "UK", "alliance": "oneworld", "loyalty": "Executive Club (Avios)", "cabins": ["Euro/World Traveller", "World Traveller Plus", "Club Suite", "First"], "wifi": "Paid Wi-Fi on most of fleet; free messaging for Club members", "ife": "Seatback on long-haul; none on short-haul", "power": "AC + USB on long-haul; USB on newer short-haul", "meals": "Complimentary long-haul; buy-on-board (M&S) short-haul economy", "seat_pitch": {"economy": "31\"", "premium_economy": "38\"", "business": "Club Suite with door"}, "baggage": "23 kg economy (fare-dependent)", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 3000, "types": {"short": "A320", "medium": "A21N", "long": "B789", "ultra_long": "A35K"}}}, "LH": {"name": "Lufthansa", "region": "Germany", "alliance": "Star Alliance", "loyalty": "Miles & More", "cabins": ["Economy", "Premium Economy", "Business (Allegris)", "First"], "wifi": "FlyNet — free messaging, paid tiers", "ife": "Seatback long-haul; streaming short-haul", "power": "AC + USB long-haul", "meals": "Complimentary long-haul; snack short-haul", "seat_pitch": {"economy": "31\"", "premium_economy": "38\"", "business": "Allegris suites rolling out"}, "baggage": "23 kg economy (fare-dependent)", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 3000, "types": {"short": "A20N", "medium": "A21N", "long": "A359", "ultra_long": "B744"}}}, "TK": {"name": "Turkish Airlines", "region": "Türkiye", "alliance": "Star Alliance", "loyalty": "Miles&Smiles", "cabins": ["Economy", "Business"], "wifi": "Free for Miles&Smiles members on wide-bodies", "ife": "Seatback on nearly all aircraft", "power": "AC + USB on most aircraft", "meals": "Complimentary hot meals even on short sectors; chef-onboard long-haul", "seat_pitch": {"economy": "31–32\"", "business": "flat beds on widebodies"}, "baggage": "20–30 kg economy (fare-dependent)", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 3500, "types": {"short": "A21N", "medium": "A21N", "long": "A359", "ultra_long": "B77W"}}}, "CX": {"name": "Cathay Pacific", "region": "Hong Kong", "alliance": "oneworld", "loyalty": "Cathay (Asia Miles)", "cabins": ["Economy", "Premium Economy", "Business (Aria Suite)", "First"], "wifi": "Paid; free for Diamond members", "ife": "Seatback across fleet", "power": "AC + USB at every seat", "meals": "Complimentary all cabins", "seat_pitch": {"economy": "32\"", "premium_economy": "40\"", "business": "Aria Suite with door"}, "baggage": "23–35 kg economy", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 3000, "types": {"short": "A21N", "medium": "A333", "long": "A359", "ultra_long": "B77W"}}}, "UA": {"name": "United Airlines", "region": "USA", "alliance": "Star Alliance", "loyalty": "MileagePlus", "cabins": ["Economy", "Economy Plus", "Premium Plus", "Polaris Business"], "wifi": "Starlink rolling out free; paid on legacy fits", "ife": "Seatback on most mainline; Bluetooth audio on new fits", "power": "AC + USB on most aircraft", "meals": "Complimentary long-haul; buy-on-board domestic economy", "seat_pitch": {"economy": "31\"", "premium_economy": "38\"", "business": "Polaris flat beds"}, "baggage": "Paid first bag domestic; 23 kg international", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 3500, "types": {"short": "B38M", "medium": "B39M", "long": "B789", "ultra_long": "B77W"}}}, "DL": {"name": "Delta Air Lines", "region": "USA", "alliance": "SkyTeam", "loyalty": "SkyMiles", "cabins": ["Main Cabin", "Comfort+", "Premium Select", "Delta One"], "wifi": "Free fleet-wide (SkyMiles login, T-Mobile partnership)", "ife": "Seatback on nearly all aircraft", "power": "AC + USB on most aircraft", "meals": "Complimentary long-haul; snacks domestic", "seat_pitch": {"economy": "31\"", "premium_economy": "38\"", "business": "Delta One suites with doors"}, "baggage": "Paid first bag domestic; 23 kg international", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 3500, "types": {"short": "A21N", "medium": "B739", "long": "A339", "ultra_long": "A35K"}}}, "AA": {"name": "American Airlines", "region": "USA", "alliance": "oneworld", "loyalty": "AAdvantage", "cabins": ["Main Cabin", "Premium Economy", "Flagship Business", "Flagship First (retiring)"], "wifi": "Paid; free ad-supported rolling out 2026", "ife": "Streaming on narrowbodies; seatback on widebodies", "power": "AC + USB on most aircraft", "meals": "Complimentary long-haul; buy-on-board domestic", "seat_pitch": {"economy": "30–31\"", "premium_economy": "38\"", "business": "Flagship Suites on 787-9P/777"}, "baggage": "Paid first bag domestic; 23 kg international", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 3500, "types": {"short": "A21N", "medium": "B38M", "long": "B789", "ultra_long": "B77W"}}}, "JL": {"name": "Japan Airlines", "region": "Japan", "alliance": "oneworld", "loyalty": "JAL Mileage Bank", "cabins": ["Economy", "Premium Economy", "Business", "First"], "wifi": "Free on domestic; paid tiers international", "ife": "Seatback across fleet", "power": "AC + USB at every seat", "meals": "Complimentary; renowned Japanese menus", "seat_pitch": {"economy": "33–34\" (widest in class)", "premium_economy": "42\"", "business": "flat beds"}, "baggage": "2×23 kg economy international", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 2000, "types": {"short": "B738", "medium": "B788", "long": "A359", "ultra_long": "A35K"}}}, "NH": {"name": "All Nippon Airways", "region": "Japan", "alliance": "Star Alliance", "loyalty": "ANA Mileage Club", "cabins": ["Economy", "Premium Economy", "Business (The Room)", "First (The Suite)"], "wifi": "Free on domestic; paid international", "ife": "Seatback across fleet", "power": "AC + USB at every seat", "meals": "Complimentary; Japanese + international menus", "seat_pitch": {"economy": "31–34\"", "premium_economy": "38\"", "business": "The Room — widest business seat flying"}, "baggage": "2×23 kg economy international", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 2000, "types": {"short": "A21N", "medium": "B788", "long": "B789", "ultra_long": "B77W"}}}, "AF": {"name": "Air France", "region": "France", "alliance": "SkyTeam", "loyalty": "Flying Blue", "cabins": ["Economy", "Premium Economy", "Business", "La Première"], "wifi": "Free messaging; paid tiers; free full for Flying Blue elite", "ife": "Seatback on long-haul", "power": "AC + USB long-haul", "meals": "Complimentary; champagne all cabins", "seat_pitch": {"economy": "31\"", "premium_economy": "38\"", "business": "flat beds with doors on new fits"}, "baggage": "23 kg economy (fare-dependent)", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 3000, "types": {"short": "A320", "medium": "A21N", "long": "B789", "ultra_long": "A35K"}}}, "QF": {"name": "Qantas", "region": "Australia", "alliance": "oneworld", "loyalty": "Qantas Frequent Flyer", "cabins": ["Economy", "Premium Economy", "Business", "First"], "wifi": "Free on domestic; rolling out international via Viasat", "ife": "Seatback on most aircraft", "power": "AC + USB on most aircraft", "meals": "Complimentary all cabins", "seat_pitch": {"economy": "31–32\"", "premium_economy": "38–42\"", "business": "Business Suites"}, "baggage": "23–32 kg economy", "fleet_rules": {"turboprop_below_km": 700, "narrowbody_below_km": 3500, "types": {"turboprop": "DH8D", "short": "B738", "medium": "A21N", "long": "B789", "ultra_long": "A35K"}}}, "MH": {"name": "Malaysia Airlines", "region": "Malaysia", "alliance": "oneworld", "loyalty": "Enrich", "cabins": ["Economy", "Business", "Business Suite (A350)"], "wifi": "Free messaging all guests; free full Wi-Fi for Enrich members on most widebodies", "ife": "Seatback on widebodies and 737 MAX; streaming on older 737-800", "power": "AC + USB on widebodies; USB on MAX", "meals": "Complimentary hot meals all cabins; satay ritual in Business", "seat_pitch": {"economy": "30–32\"", "business": "flat beds on widebodies", "first": "Business Suite 40+\""}, "baggage": "30 kg economy international", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 4200, "types": {"short": "B738", "medium": "B38M", "long": "A333", "ultra_long": "A359"}}}, "TG": {"name": "Thai Airways", "region": "Thailand", "alliance": "Star Alliance", "loyalty": "Royal Orchid Plus", "cabins": ["Economy", "Premium Economy (new fits)", "Royal Silk Business", "First (777-300ER)"], "wifi": "Paid tiers on widebodies; free messaging rolling out", "ife": "Seatback across most of fleet", "power": "AC + USB on widebodies", "meals": "Complimentary Thai + international menus all cabins", "seat_pitch": {"economy": "31–32\"", "business": "flat beds on widebodies"}, "baggage": "30 kg economy", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 2500, "types": {"short": "A320", "medium": "A359", "long": "A359", "ultra_long": "B77W"}}}, "VN": {"name": "Vietnam Airlines", "region": "Vietnam", "alliance": "SkyTeam", "loyalty": "Lotusmiles", "cabins": ["Economy", "Premium Economy", "Business"], "wifi": "Paid on A350/787 widebodies", "ife": "Seatback on widebodies; none on A321", "power": "AC + USB on widebodies", "meals": "Complimentary all cabins", "seat_pitch": {"economy": "31–32\"", "premium_economy": "38\"", "business": "flat beds on widebodies"}, "baggage": "23 kg economy", "fleet_rules": {"turboprop_below_km": 600, "narrowbody_below_km": 2500, "types": {"turboprop": "AT76", "short": "A321", "medium": "A321", "long": "B789", "ultra_long": "A359"}}}, "GA": {"name": "Garuda Indonesia", "region": "Indonesia", "alliance": "SkyTeam", "loyalty": "GarudaMiles", "cabins": ["Economy", "Business", "First (select 777-300ER)"], "wifi": "Paid on widebodies and 737 MAX", "ife": "Seatback across most of fleet", "power": "AC + USB on widebodies", "meals": "Complimentary Indonesian + international menus", "seat_pitch": {"economy": "31–32\"", "business": "flat beds on widebodies"}, "baggage": "23 kg economy", "fleet_rules": {"turboprop_below_km": 600, "narrowbody_below_km": 2500, "types": {"turboprop": "AT76", "short": "B738", "medium": "A333", "long": "A333", "ultra_long": "B77W"}}}, "SV": {"name": "Saudia", "region": "Saudi Arabia", "alliance": "SkyTeam", "loyalty": "AlFursan", "cabins": ["Guest (Economy)", "Business", "First (select 777)"], "wifi": "Free messaging; paid full tiers on most aircraft", "ife": "Seatback across fleet", "power": "AC + USB on most aircraft", "meals": "Complimentary; no alcohol served", "seat_pitch": {"economy": "31–32\"", "business": "flat beds on widebodies"}, "baggage": "23–30 kg economy", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 3000, "types": {"short": "A320", "medium": "A21N", "long": "B789", "ultra_long": "B77W"}}}, "KE": {"name": "Korean Air", "region": "South Korea", "alliance": "SkyTeam", "loyalty": "SKYPASS", "cabins": ["Economy", "Premium (new fits)", "Prestige Business", "First"], "wifi": "Free messaging; paid full on most widebodies", "ife": "Seatback across fleet", "power": "AC + USB at most seats", "meals": "Complimentary; bibimbap signature dish", "seat_pitch": {"economy": "33–34\"", "business": "Prestige Suites, flat beds", "first": "Kosmo Suites"}, "baggage": "23 kg economy (2×23 kg to/from Americas)", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 2000, "types": {"short": "B738", "medium": "A339", "long": "B789", "ultra_long": "B77W"}}}, "OZ": {"name": "Asiana Airlines (merging into Korean Air)", "region": "South Korea", "alliance": "Star Alliance", "loyalty": "Asiana Club", "cabins": ["Economy", "Business Smartium", "First (A380)"], "wifi": "Paid on A350/A380", "ife": "Seatback on widebodies", "power": "AC + USB on widebodies", "meals": "Complimentary Korean + international", "seat_pitch": {"economy": "32–33\"", "business": "flat beds on widebodies"}, "baggage": "23 kg economy", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 2000, "types": {"short": "A321", "medium": "A333", "long": "A359", "ultra_long": "A388"}}}, "CI": {"name": "China Airlines", "region": "Taiwan", "alliance": "SkyTeam", "loyalty": "Dynasty Flyer", "cabins": ["Economy", "Premium Economy", "Business"], "wifi": "Paid on A350/777/787", "ife": "Seatback across fleet", "power": "AC + USB on widebodies", "meals": "Complimentary all cabins", "seat_pitch": {"economy": "31–32\"", "premium_economy": "39\"", "business": "flat beds on widebodies"}, "baggage": "23–30 kg economy", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 2000, "types": {"short": "A21N", "medium": "A333", "long": "A359", "ultra_long": "B77W"}}}, "BR": {"name": "EVA Air", "region": "Taiwan", "alliance": "Star Alliance", "loyalty": "Infinity MileageLands", "cabins": ["Economy", "Premium Economy", "Royal Laurel Business"], "wifi": "Paid tiers on widebodies; free messaging for elites", "ife": "Seatback across fleet", "power": "AC + USB at every seat", "meals": "Complimentary; Din Tai Fung partnership on select routes", "seat_pitch": {"economy": "31–33\"", "premium_economy": "38\"", "business": "Royal Laurel flat beds"}, "baggage": "23–30 kg economy", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 2000, "types": {"short": "A321", "medium": "B789", "long": "B789", "ultra_long": "B77W"}}}, "CZ": {"name": "China Southern", "region": "China", "alliance": null, "loyalty": "Sky Pearl Club", "cabins": ["Economy", "Premium Economy", "Business"], "wifi": "Free basic on widebodies (registration required)", "ife": "Seatback on widebodies and newer narrowbodies", "power": "AC + USB on most aircraft", "meals": "Complimentary all cabins", "seat_pitch": {"economy": "31–32\"", "business": "flat beds on widebodies"}, "baggage": "23 kg economy", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 2500, "types": {"short": "A320", "medium": "A21N", "long": "B789", "ultra_long": "B77W"}}}, "MU": {"name": "China Eastern", "region": "China", "alliance": "SkyTeam", "loyalty": "Eastern Miles", "cabins": ["Economy", "Premium Economy", "Business"], "wifi": "Free basic on widebodies (registration required)", "ife": "Seatback on widebodies", "power": "AC + USB on widebodies", "meals": "Complimentary all cabins", "seat_pitch": {"economy": "31–32\"", "business": "flat beds on widebodies"}, "baggage": "23 kg economy", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 2500, "types": {"short": "A320", "medium": "A21N", "long": "A359", "ultra_long": "B77W"}}}, "CA": {"name": "Air China", "region": "China", "alliance": "Star Alliance", "loyalty": "PhoenixMiles", "cabins": ["Economy", "Premium Economy", "Business", "First (747-8/777)"], "wifi": "Free basic on widebodies (registration required)", "ife": "Seatback on widebodies", "power": "AC + USB on widebodies", "meals": "Complimentary all cabins", "seat_pitch": {"economy": "31–32\"", "business": "flat beds on widebodies"}, "baggage": "23 kg economy", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 2500, "types": {"short": "A320", "medium": "A21N", "long": "A359", "ultra_long": "B77W"}}}, "NZ": {"name": "Air New Zealand", "region": "New Zealand", "alliance": "Star Alliance", "loyalty": "Airpoints", "cabins": ["Economy", "Economy Skycouch", "Premium Economy", "Business Premier"], "wifi": "Free Wi-Fi on most jets", "ife": "Seatback across jet fleet", "power": "AC + USB on widebodies", "meals": "Complimentary on international; buy-on-board short domestic", "seat_pitch": {"economy": "31–33\"", "premium_economy": "41\"", "business": "flat beds"}, "baggage": "23 kg economy international", "fleet_rules": {"turboprop_below_km": 500, "narrowbody_below_km": 3500, "types": {"turboprop": "AT76", "short": "A20N", "medium": "A21N", "long": "B789", "ultra_long": "B789"}}}, "VA": {"name": "Virgin Australia", "region": "Australia", "alliance": null, "loyalty": "Velocity", "cabins": ["Economy", "Economy X", "Business"], "wifi": "Paid on most 737s; rolling out", "ife": "Streaming to own device", "power": "USB on most aircraft", "meals": "Complimentary snack; buy-on-board more", "seat_pitch": {"economy": "30–31\"", "business": "37\" recliner domestic"}, "baggage": "23 kg economy", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 99999, "types": {"short": "B738", "medium": "B738", "long": "B38M", "ultra_long": "B38M"}}}, "AK": {"name": "AirAsia", "region": "Malaysia", "alliance": null, "loyalty": "airasia rewards", "cabins": ["Economy", "Hot Seats (extra legroom)"], "wifi": "Paid (airasia WiFi) on select aircraft", "ife": "No seatback; app-based entertainment", "power": "Not standard", "meals": "Buy-on-board (Santan)", "seat_pitch": {"economy": "28–29\"", "premium_economy": "Hot Seats 31–32\""}, "baggage": "None free; 20–30 kg purchasable", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 99999, "types": {"short": "A320", "medium": "A21N", "long": "A21N", "ultra_long": "A21N"}}}, "D7": {"name": "AirAsia X", "region": "Malaysia", "alliance": null, "loyalty": "airasia rewards", "cabins": ["Economy", "Premium Flatbed"], "wifi": "Paid on select aircraft", "ife": "No seatback; app-based", "power": "USB in Premium", "meals": "Buy-on-board (Santan)", "seat_pitch": {"economy": "31–32\"", "business": "Premium Flatbed 60\""}, "baggage": "None free; purchasable", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 0, "types": {"short": "A333", "medium": "A333", "long": "A333", "ultra_long": "A333"}}}, "TR": {"name": "Scoot", "region": "Singapore", "alliance": null, "loyalty": "KrisFlyer (earn)", "cabins": ["Economy", "ScootPlus"], "wifi": "Paid on 787s; free tier rolling out", "ife": "No seatback; own device", "power": "USB/AC in ScootPlus and select rows", "meals": "Buy-on-board", "seat_pitch": {"economy": "28–31\"", "premium_economy": "ScootPlus 38\""}, "baggage": "None free; purchasable", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 3000, "types": {"short": "A20N", "medium": "A21N", "long": "B789", "ultra_long": "B789"}}}, "JQ": {"name": "Jetstar", "region": "Australia", "alliance": null, "loyalty": "Qantas FF (earn on bundles)", "cabins": ["Economy", "Business (787)"], "wifi": "Not offered on most", "ife": "Streaming/paid screens on 787", "power": "USB on 787/A321neo", "meals": "Buy-on-board", "seat_pitch": {"economy": "28–31\"", "business": "38\" recliner on 787"}, "baggage": "None free on basic fares", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 3500, "types": {"short": "A320", "medium": "A21N", "long": "B788", "ultra_long": "B788"}}}, "WN": {"name": "Southwest Airlines", "region": "USA", "alliance": null, "loyalty": "Rapid Rewards", "cabins": ["Economy (assigned seating from 2026)"], "wifi": "$8 flat; free messaging", "ife": "Streaming to own device", "power": "USB-A/C on MAX 8 retrofits", "meals": "Snacks + drinks complimentary", "seat_pitch": {"economy": "31–32\""}, "baggage": "First bag now paid (2025 policy change)", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 99999, "types": {"short": "B738", "medium": "B38M", "long": "B38M", "ultra_long": "B38M"}}}, "B6": {"name": "JetBlue", "region": "USA", "alliance": null, "loyalty": "TrueBlue", "cabins": ["Core", "Even More Space", "Mint (transcon/transatlantic)"], "wifi": "Free Fly-Fi fleet-wide", "ife": "Seatback on every aircraft", "power": "AC + USB at every seat", "meals": "Free snacks; buy-on-board fresh; full dining in Mint", "seat_pitch": {"economy": "32\"", "business": "Mint suites, flat beds"}, "baggage": "Paid first bag on basic fares", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 99999, "types": {"short": "A320", "medium": "A21N", "long": "A21N", "ultra_long": "A21N"}}}, "AS": {"name": "Alaska Airlines", "region": "USA", "alliance": "oneworld", "loyalty": "Mileage Plan", "cabins": ["Main", "Premium Class", "First"], "wifi": "Paid satellite; free messaging", "ife": "Streaming to own device", "power": "AC + USB on most aircraft", "meals": "Buy-on-board fresh; complimentary in First", "seat_pitch": {"economy": "31–32\"", "premium_economy": "35\"", "first": "recliner 41\""}, "baggage": "Paid first bag", "fleet_rules": {"turboprop_below_km": 500, "narrowbody_below_km": 99999, "types": {"turboprop": "DH8D", "short": "B738", "medium": "B39M", "long": "B39M", "ultra_long": "B39M"}}}, "FR": {"name": "Ryanair", "region": "Ireland", "alliance": null, "loyalty": null, "cabins": ["Economy"], "wifi": "Not offered", "ife": "None", "power": "None", "meals": "Buy-on-board", "seat_pitch": {"economy": "30\" (non-reclining)"}, "baggage": "Small bag only free; everything else paid", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 99999, "types": {"short": "B738", "medium": "B738", "long": "B38M", "ultra_long": "B38M"}}}, "U2": {"name": "easyJet", "region": "UK", "alliance": null, "loyalty": "easyJet Plus (perks)", "cabins": ["Economy"], "wifi": "Not offered", "ife": "None", "power": "None on most", "meals": "Buy-on-board", "seat_pitch": {"economy": "29\""}, "baggage": "Small bag free; cabin/hold paid", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 99999, "types": {"short": "A320", "medium": "A21N", "long": "A21N", "ultra_long": "A21N"}}}, "W6": {"name": "Wizz Air", "region": "Hungary", "alliance": null, "loyalty": "Wizz Discount Club", "cabins": ["Economy"], "wifi": "Not offered", "ife": "None", "power": "None", "meals": "Buy-on-board", "seat_pitch": {"economy": "28–30\""}, "baggage": "Small bag free; rest paid", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 99999, "types": {"short": "A320", "medium": "A21N", "long": "A21N", "ultra_long": "A21N"}}}, "LX": {"name": "SWISS", "region": "Switzerland", "alliance": "Star Alliance", "loyalty": "Miles & More", "cabins": ["Economy", "Premium Economy", "Business", "First"], "wifi": "Free messaging; paid tiers", "ife": "Seatback on long-haul", "power": "AC + USB long-haul", "meals": "Complimentary long-haul; Swiss chocolate always", "seat_pitch": {"economy": "31\"", "premium_economy": "39\"", "business": "flat beds"}, "baggage": "23 kg economy", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 3000, "types": {"short": "A20N", "medium": "A21N", "long": "A333", "ultra_long": "B77W"}}}, "KL": {"name": "KLM", "region": "Netherlands", "alliance": "SkyTeam", "loyalty": "Flying Blue", "cabins": ["Economy", "Premium Comfort", "World Business"], "wifi": "Free messaging; paid tiers", "ife": "Seatback on long-haul", "power": "AC + USB long-haul", "meals": "Complimentary long-haul; snack short-haul", "seat_pitch": {"economy": "31\"", "premium_economy": "38\"", "business": "flat beds"}, "baggage": "23 kg economy (fare-dependent)", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 3000, "types": {"short": "B738", "medium": "A21N", "long": "B789", "ultra_long": "B77W"}}}, "IB": {"name": "Iberia", "region": "Spain", "alliance": "oneworld", "loyalty": "Iberia Plus (Avios)", "cabins": ["Economy", "Premium Economy", "Business"], "wifi": "Paid; free messaging for elites", "ife": "Seatback on long-haul", "power": "AC + USB long-haul", "meals": "Complimentary long-haul; buy-on-board short-haul", "seat_pitch": {"economy": "31\"", "premium_economy": "37\"", "business": "flat beds"}, "baggage": "23 kg economy (fare-dependent)", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 3000, "types": {"short": "A320", "medium": "A21N", "long": "A359", "ultra_long": "A35K"}}}, "AY": {"name": "Finnair", "region": "Finland", "alliance": "oneworld", "loyalty": "Finnair Plus (Avios)", "cabins": ["Economy", "Premium Economy", "Business (AirLounge)"], "wifi": "Free messaging; paid tiers", "ife": "Seatback on long-haul", "power": "AC + USB long-haul", "meals": "Complimentary long-haul; blueberry juice signature", "seat_pitch": {"economy": "31\"", "premium_economy": "38\"", "business": "AirLounge sofa concept"}, "baggage": "23 kg economy (fare-dependent)", "fleet_rules": {"turboprop_below_km": 500, "narrowbody_below_km": 3000, "types": {"turboprop": "AT76", "short": "A320", "medium": "A321", "long": "A359", "ultra_long": "A359"}}}, "ET": {"name": "Ethiopian Airlines", "region": "Ethiopia", "alliance": "Star Alliance", "loyalty": "ShebaMiles", "cabins": ["Economy", "Cloud Nine Business"], "wifi": "Paid on widebodies", "ife": "Seatback on widebodies and MAX", "power": "AC + USB on widebodies", "meals": "Complimentary all cabins", "seat_pitch": {"economy": "31–32\"", "business": "flat beds on widebodies"}, "baggage": "2×23 kg economy on most international", "fleet_rules": {"turboprop_below_km": 700, "narrowbody_below_km": 3000, "types": {"turboprop": "DH8D", "short": "B738", "medium": "B38M", "long": "B789", "ultra_long": "A359"}}}, "MS": {"name": "EgyptAir", "region": "Egypt", "alliance": "Star Alliance", "loyalty": "EgyptAir Plus", "cabins": ["Economy", "Business"], "wifi": "Paid on widebodies", "ife": "Seatback on most aircraft", "power": "AC + USB on widebodies", "meals": "Complimentary; no alcohol served", "seat_pitch": {"economy": "31–32\"", "business": "flat beds on 787/A350"}, "baggage": "23 kg economy", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 3000, "types": {"short": "A320", "medium": "A21N", "long": "B789", "ultra_long": "B789"}}}, "WY": {"name": "Oman Air", "region": "Oman", "alliance": "oneworld", "loyalty": "Sindbad", "cabins": ["Economy", "Business", "Business Studio (787)"], "wifi": "Free messaging; paid tiers", "ife": "Seatback across fleet", "power": "AC + USB at every seat", "meals": "Complimentary all cabins", "seat_pitch": {"economy": "31–32\"", "business": "flat beds on widebodies"}, "baggage": "30 kg economy", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 3000, "types": {"short": "B38M", "medium": "B38M", "long": "B789", "ultra_long": "B789"}}}, "PK": {"name": "Pakistan International Airlines", "region": "Pakistan", "alliance": null, "loyalty": "PIA Awards+", "cabins": ["Economy", "Executive Economy", "Business"], "wifi": "Not offered", "ife": "Seatback on 777s (varies)", "power": "Limited", "meals": "Complimentary all cabins", "seat_pitch": {"economy": "31–34\"", "business": "recliner/angled flat on 777"}, "baggage": "30–40 kg economy international", "fleet_rules": {"turboprop_below_km": 500, "narrowbody_below_km": 2500, "types": {"turboprop": "AT76", "short": "A320", "medium": "A320", "long": "B772", "ultra_long": "B77L"}}}, "UL": {"name": "SriLankan Airlines", "region": "Sri Lanka", "alliance": "oneworld", "loyalty": "FlySmiLes", "cabins": ["Economy", "Business"], "wifi": "Paid on A330s", "ife": "Seatback on widebodies", "power": "AC + USB on widebodies", "meals": "Complimentary; Sri Lankan curries signature", "seat_pitch": {"economy": "31–32\"", "business": "flat beds on A330"}, "baggage": "23–30 kg economy", "fleet_rules": {"turboprop_below_km": 0, "narrowbody_below_km": 2500, "types": {"short": "A320", "medium": "A333", "long": "A333", "ultra_long": "A333"}}}, "BG": {"name": "Biman Bangladesh Airlines", "region": "Bangladesh", "alliance": null, "loyalty": "Biman Loyalty Club", "cabins": ["Economy", "Business"], "wifi": "Paid on 787s", "ife": "Seatback on 787s", "power": "AC + USB on 787s", "meals": "Complimentary all cabins", "seat_pitch": {"economy": "31–32\"", "business": "flat beds on 787"}, "baggage": "2×23 kg economy on most international", "fleet_rules": {"turboprop_below_km": 500, "narrowbody_below_km": 2000, "types": {"turboprop": "DH8D", "short": "B738", "medium": "B788", "long": "B789", "ultra_long": "B789"}}}}''')

    def fetch_json(url):
        req = urllib.request.Request(url, headers={"User-Agent": "flightdeck/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            return None

    def cached_fetch(kind, key, url, ttl=CACHE_TTL_S):
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

    def normalize(s):
        return re.sub(r"[^A-Z0-9]", "", s.upper())

    def haversine_km(lat1, lon1, lat2, lon2):
        r = 6371.0
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * r * math.asin(math.sqrt(a))

    def initial_bearing_deg(lat1, lon1, lat2, lon2):
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dl = math.radians(lon2 - lon1)
        y = math.sin(dl) * math.cos(p2)
        x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
        return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0

    def pick_estimated_type(airline, distance_km):
        rules = airline.get("fleet_rules")
        if not rules:
            return None
        types = rules.get("types", {})
        tp_below = rules.get("turboprop_below_km", 0)
        nb_below = rules.get("narrowbody_below_km", 0)
        if tp_below and distance_km < tp_below and "turboprop" in types:
            return types["turboprop"]
        if distance_km < 1200:
            return types.get("short")
        if distance_km < max(nb_below, 1200):
            return types.get("medium")
        if distance_km < 7000:
            return types.get("long")
        return types.get("ultra_long") or types.get("long")

    def airport(node):
        return {
            "iata": node.get("iata_code"),
            "icao": node.get("icao_code"),
            "name": node.get("name"),
            "city": node.get("municipality"),
            "country": node.get("country_name"),
            "country_iso": node.get("country_iso_name"),
            "lat": node.get("latitude"),
            "lon": node.get("longitude"),
            "elevation_ft": node.get("elevation"),
        }

    def live_position(callsigns):
        for cs in callsigns:
            if not cs:
                continue
            # short cache keeps repeat lookups consistent; retry once so a single
            # network hiccup doesn't silently flip "observed" back to "estimated"
            data = cached_fetch("live", cs, f"{AIRPLANES_LIVE}/callsign/{cs}", ttl=90)
            if data is None:
                time.sleep(0.5)
                data = fetch_json(f"{AIRPLANES_LIVE}/callsign/{cs}")
            if not data or not data.get("ac"):
                continue
            ac = data["ac"][0]
            if ac.get("lat") is None:
                continue
            alt = ac.get("alt_baro")
            return {
                "hex": ac.get("hex"),
                "lat": ac.get("lat"),
                "lon": ac.get("lon"),
                "alt_ft": alt if isinstance(alt, (int, float)) else 0,
                "on_ground": alt == "ground",
                "gs_kt": ac.get("gs"),
                "track_deg": ac.get("track"),
                "reg": ac.get("r"),
                "type_icao": ac.get("t"),
                "squawk": ac.get("squawk"),
                "seen_s": ac.get("seen"),
            }
        return None

    def opensky_fetch(url):
        """OpenSky fetch, OAuth2-authed when credentials exist (4000 credits/day
        vs 400 anon). Returns (json, remaining_credits from X-Rate-Limit-Remaining,
        0 on 429). Token cache file is shared with sky.py."""
        import urllib.parse
        token = None
        try:
            cid = fused.secrets["opensky_client_id"]
            csec = fused.secrets["opensky_client_secret"]
        except Exception:
            cid = csec = None
        tok_path = os.path.join(CACHE_DIR, "opensky_token.json")
        if cid and csec:
            if os.path.exists(tok_path) and time.time() - os.path.getmtime(tok_path) < 25 * 60:
                try:
                    with open(tok_path) as f:
                        token = json.load(f).get("t")
                except (json.JSONDecodeError, OSError):
                    pass
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
                        os.makedirs(CACHE_DIR, exist_ok=True)
                        with open(tok_path, "w") as f:
                            json.dump({"t": token}, f)
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

    def flown_track(hex_code):
        """Actual flown path so far (OpenSky tracks). Real flights rarely follow
        the great circle — airways, weather, airspace closures — so the drawn
        geodesic can sit 1000+ km from where the plane really is.
        Returns (path, quota_remaining)."""
        if not hex_code:
            return None, None
        os.makedirs(CACHE_DIR, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", f"trk_{hex_code}")
        cpath = os.path.join(CACHE_DIR, safe + ".json")
        data = quota = None
        if os.path.exists(cpath) and time.time() - os.path.getmtime(cpath) < 180:
            try:
                with open(cpath) as f:
                    data = json.load(f)
                quota = data.get("_quota")
            except (json.JSONDecodeError, OSError):
                data = None
        if data is None:
            data, quota = opensky_fetch(
                f"https://opensky-network.org/api/tracks/all?icao24={hex_code.lower()}&time=0")
            if data is not None:
                data["_quota"] = quota
                try:
                    with open(cpath, "w") as f:
                        json.dump(data, f)
                except OSError:
                    pass
        raw = (data or {}).get("path") or []
        pts = [[round(p[1], 3), round(p[2], 3)] for p in raw
               if isinstance(p, (list, tuple)) and len(p) >= 3
               and p[1] is not None and p[2] is not None]
        if len(pts) < 2:
            return None, quota
        step = max(1, len(pts) // 220)      # keep payload small on long-hauls
        thin = pts[::step]
        if thin[-1] != pts[-1]:
            thin.append(pts[-1])
        return thin, quota

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
        """Current conditions at an airport via open-meteo (keyless, 30 min cache)."""
        if lat is None or lon is None:
            return None
        url = (
            "https://api.open-meteo.com/v1/forecast?latitude=%.4f&longitude=%.4f"
            "&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m,surface_pressure,is_day"
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
            "humidity_pct": cur.get("relative_humidity_2m"),
            "pressure_hpa": cur.get("surface_pressure"),
            "visibility_km": vis_km,
            "local_time": cur.get("time"),
        }

    def resolve_one(query, want_live):
        q = normalize(query)
        if not q or len(q) < 3:
            return {"ok": False, "query": query, "error": "Enter a flight number like AI302 or 6E2134."}

        route_resp = cached_fetch("route", q, f"{ADSBDB}/callsign/{q}")
        fr = (route_resp or {}).get("response", {})
        fr = fr.get("flightroute") if isinstance(fr, dict) else None
        if not fr:
            return {
                "ok": False,
                "query": query,
                "error": f"No route found for {q}. Check the flight number -- some codeshare or seasonal flights are not in the route database.",
            }

        airline_raw = fr.get("airline") or {}
        origin = airport(fr.get("origin") or {})
        dest = airport(fr.get("destination") or {})

        distance_km = None
        duration_min = None
        if origin["lat"] is not None and dest["lat"] is not None:
            distance_km = haversine_km(origin["lat"], origin["lon"], dest["lat"], dest["lon"])

        iata = airline_raw.get("iata")
        amenities = AIRLINES.get(iata) if iata else None

        # live position + both weather lookups are independent network calls — run together
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=3) as pool:
            f_live = pool.submit(
                live_position, [fr.get("callsign_icao"), fr.get("callsign_iata"), q]
            ) if want_live else None
            f_wxo = pool.submit(weather_at, origin["lat"], origin["lon"], origin.get("iata") or "o")
            f_wxd = pool.submit(weather_at, dest["lat"], dest["lon"], dest.get("iata") or "d")
            live_data = f_live.result() if f_live else None
            weather_pair = {"origin": f_wxo.result(), "destination": f_wxd.result()}

        if live_data and not live_data.get("on_ground"):
            path, quota = flown_track(live_data.get("hex"))
            live_data["path"] = path
            live_data["opensky_quota"] = quota

        type_icao = None
        type_source = None
        reg = None
        if live_data and live_data.get("type_icao"):
            type_icao = live_data["type_icao"]
            type_source = "observed via live ADS-B"
            reg = live_data.get("reg")
        elif amenities and distance_km is not None:
            type_icao = pick_estimated_type(amenities, distance_km)
            type_source = "estimated from airline fleet + route length"
        if type_icao is None and distance_km is not None:
            # airline not in the curated set: fall back to a generic type by route length
            if distance_km < 1500:
                type_icao = "A320"
            elif distance_km < 3500:
                type_icao = "B738"
            elif distance_km < 9000:
                type_icao = "B789"
            else:
                type_icao = "B77W"
            type_source = "generic estimate by route length (airline fleet not curated)"

        aircraft = None
        if type_icao:
            spec = AIRCRAFT.get(type_icao)
            aircraft = {"type_icao": type_icao, "source": type_source, "registration": reg}
            if spec:
                aircraft.update(spec)
            else:
                aircraft["name"] = type_icao

        if distance_km is not None:
            avg = TURBOPROP_AVG_KMH if (aircraft and aircraft.get("class") == "turboprop") else JET_AVG_KMH
            duration_min = int(distance_km / avg * 60 + 25)

        if reg:
            reg_resp = cached_fetch("reg", reg, f"{ADSBDB}/aircraft/{reg}")
            node = (reg_resp or {}).get("response", {})
            node = node.get("aircraft") if isinstance(node, dict) else None
            if node and aircraft is not None:
                aircraft["registration_details"] = {
                    "registration": node.get("registration"),
                    "type": node.get("type"),
                    "manufacturer": node.get("manufacturer"),
                    "owner": node.get("registered_owner"),
                    "photo": node.get("url_photo"),
                    "photo_thumb": node.get("url_photo_thumbnail"),
                }

        bearing = None
        if distance_km is not None:
            bearing = initial_bearing_deg(origin["lat"], origin["lon"], dest["lat"], dest["lon"])

        return {
            "ok": True,
            "query": query,
            "callsign_icao": fr.get("callsign_icao"),
            "callsign_iata": fr.get("callsign_iata"),
            "airline": {
                "name": airline_raw.get("name"),
                "iata": airline_raw.get("iata"),
                "icao": airline_raw.get("icao"),
                "callsign": airline_raw.get("callsign"),
                "country": airline_raw.get("country"),
                "country_iso": airline_raw.get("country_iso"),
            },
            "origin": origin,
            "destination": dest,
            "distance_km": round(distance_km) if distance_km is not None else None,
            "duration_min": duration_min,
            "initial_bearing_deg": round(bearing, 1) if bearing is not None else None,
            "aircraft": aircraft,
            "amenities": amenities,
            "weather": weather_pair,
            "live": live_data,
            "sources": ["adsbdb.com (route, airline, registration)", "airplanes.live (live ADS-B)"]
            + (["opensky-network.org (flown track)"] if live_data and live_data.get("path") else [])
            + (["curated fleet/amenity data (typical, not booking-grade)"] if amenities else []),
        }

    def discover_airborne(limit=4):
        """Sample live ADS-B around big hubs, return flights airborne right now."""
        # ICAO telephony prefix -> IATA flight-number prefix (airlines we curate)
        icao2iata = {
            "AIC": "AI", "IGO": "6E", "AXB": "IX", "VTI": "UK", "SEJ": "SG", "AKJ": "QP",
            "UAE": "EK", "QTR": "QR", "ETD": "EY", "SIA": "SQ", "MAS": "MH", "THA": "TG",
            "CPA": "CX", "JAL": "JL", "ANA": "NH", "KAL": "KE", "BAW": "BA", "DLH": "LH",
            "AFR": "AF", "KLM": "KL", "UAL": "UA", "AAL": "AA", "DAL": "DL", "QFA": "QF",
            "GIA": "GA", "THY": "TK", "SVA": "SV", "PAL": "PR", "EVA": "BR", "CSN": "CZ",
        }
        hubs = [(28.56, 77.10), (19.09, 72.87), (25.25, 55.36), (1.36, 103.99)]  # DEL BOM DXB SIN
        cands, seen = [], set()
        for lat, lon in hubs:
            if len(cands) >= limit * 3:
                break
            data = fetch_json(f"{AIRPLANES_LIVE}/point/{lat}/{lon}/220")
            for ac in (data or {}).get("ac") or []:
                cs = (ac.get("flight") or "").strip().upper()
                alt = ac.get("alt_baro")
                # suffix-letter callsigns (IGO323E) can't be re-found from the plain
                # flight number on click-through — exact-form callsigns only
                m = re.match(r"^([A-Z]{3})(\d{1,4})$", cs)
                if not m or m.group(1) not in icao2iata:
                    continue
                if alt == "ground" or not isinstance(alt, (int, float)) or alt < 10000:
                    continue  # airborne, past initial climb, not about to land (hopefully)
                iata_flight = icao2iata[m.group(1)] + m.group(2)
                if iata_flight in seen:
                    continue
                seen.add(iata_flight)
                cands.append({
                    "flight": iata_flight,
                    "callsign": cs,
                    "type_icao": ac.get("t"),
                    "alt_ft": alt,
                })
                if len(cands) >= limit * 3:
                    break

        # only offer flights the click-through lookup will actually resolve:
        # adsbdb must know the route for this exact callsign
        from concurrent.futures import ThreadPoolExecutor as _TPE

        def route_of(c):
            resp = cached_fetch("route", c["callsign"], f"{ADSBDB}/callsign/{c['callsign']}")
            fr_ = (resp or {}).get("response", {})
            fr_ = fr_.get("flightroute") if isinstance(fr_, dict) else None
            if not fr_:
                return None
            o, d = fr_.get("origin") or {}, fr_.get("destination") or {}
            c["route"] = f"{o.get('iata_code', '?')} → {d.get('iata_code', '?')}"
            return c

        verified = []
        with _TPE(max_workers=6) as pool:
            for c in pool.map(route_of, cands):
                if c:
                    verified.append(c)
        return {"airborne": verified[:limit]}

    from concurrent.futures import ThreadPoolExecutor

    if not flight.strip() and not vs.strip():
        return discover_airborne()

    out = {"primary": None, "compare": None}
    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(resolve_one, flight, live) if flight.strip() else None
        f2 = pool.submit(resolve_one, vs, live) if vs.strip() else None
        if f1:
            out["primary"] = f1.result()
        if f2:
            out["compare"] = f2.result()
    return out
