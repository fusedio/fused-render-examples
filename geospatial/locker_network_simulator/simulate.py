"""Core scenario engine: baseline delivery tour vs. locker-optimized tour.

Given a seeded "day" of parcels, a set of hypothetical lockers, a capture
radius and a notification rule, it:
  1. builds the baseline tour (real OSRM road matrix, NN + 2-opt + or-opt),
  2. captures in-radius, rule-passing parcels to their nearest locker,
  3. re-solves the tour with captured home stops removed and each active
     locker inserted as ONE stop (seeded from the baseline order, so the
     optimized tour is never worse than the trivial reroute),
  4. returns both road geometries + before/after KPIs.
"""

import os
import sys

if "__file__" in globals():
    # The fused-render runner already puts the script dir at sys.path[0].
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402


@C.disk_cache
def _baseline_order(seed: int, coords):
    dur, _ = C.full_matrix(coords)
    return C.solve_tour(list(range(1, len(coords))), dur)


@C.disk_cache
def _after_order(scenario_key, coords, init):
    dur, _ = C.full_matrix(coords)
    order = C.two_opt(list(init), dur)
    order = C.or_opt(order, dur)
    # never worse than the trivial reroute we seeded it with
    if C.tour_cost(order, dur) > C.tour_cost(list(init), dur):
        order = list(init)
    return order


def _parse_lockers(lockers: str):
    out = []
    for part in (lockers or "").split(";"):
        part = part.strip()
        if not part:
            continue
        lat, lon = part.split(",")
        out.append({"lat": round(float(lat), 5), "lon": round(float(lon), 5)})
    return out


def _tour_stats(order, dur, dist):
    return (C.tour_cost(order, dist) / 1000.0, C.tour_cost(order, dur) / 60.0)


def main(
    seed: int = 7,
    lockers: str = "",
    radius_m: int = 400,
    email_only: bool = False,
) -> dict:
    parcels = C.make_parcels(seed)
    n = len(parcels)
    lks = _parse_lockers(lockers)

    # --- capture assignment -------------------------------------------------
    for p in parcels:
        p["captured_by"] = None
    for i, p in enumerate(parcels):
        if email_only and not p["has_email"]:
            continue
        best_d, best_l = None, None
        for li, lk in enumerate(lks):
            d = C.haversine_m(p["lat"], p["lon"], lk["lat"], lk["lon"])
            if d <= radius_m and (best_d is None or d < best_d):
                best_d, best_l = d, li
        if best_l is not None:
            p["captured_by"] = best_l

    counts = [0] * len(lks)
    failed_captured = 0
    for p in parcels:
        if p["captured_by"] is not None:
            counts[p["captured_by"]] += 1
            if p["status"] == "failed":
                failed_captured += 1

    shops = C.shop_candidates()
    for li, lk in enumerate(lks):
        lk["label"] = f"L{li + 1}"
        lk["captured"] = counts[li]
        host = min(
            shops,
            key=lambda s: C.haversine_m(s["lat"], s["lon"], lk["lat"], lk["lon"]),
            default=None,
        )
        if host and C.haversine_m(host["lat"], host["lon"], lk["lat"], lk["lon"]) <= 75:
            lk["host"] = host["name"]

    active = [li for li in range(len(lks)) if counts[li] > 0]

    # --- matrices & baseline tour --------------------------------------------
    base_coords = [[C.DEPOT["lat"], C.DEPOT["lon"]]] + [[p["lat"], p["lon"]] for p in parcels]
    coords = base_coords + [[lks[li]["lat"], lks[li]["lon"]] for li in active]
    dur, dist = C.full_matrix(coords)

    b_order = _baseline_order(int(seed), base_coords)
    for seq, node in enumerate(b_order):
        parcels[node - 1]["seq"] = seq + 1
    b_km, b_drive_min = _tour_stats(b_order, dur, dist)
    b_service_min = n * C.STOP_SERVICE_S / 60.0
    b_total_min = b_drive_min + b_service_min
    n_failed = sum(1 for p in parcels if p["status"] == "failed")

    def route_poly(order):
        pts = [coords[0]] + [coords[k] for k in order] + [coords[0]]
        return C.route_geometry(pts)["polyline"]

    baseline = {
        "distance_km": round(b_km, 2),
        "drive_min": round(b_drive_min, 1),
        "service_min": round(b_service_min, 1),
        "total_min": round(b_total_min, 1),
        "stops": n,
        "delivered": n - n_failed,
        "failed": n_failed,
        "polyline": route_poly(b_order),
    }

    result = {
        "seed": int(seed),
        "depot": C.DEPOT,
        "rule": {"radius_m": int(radius_m), "email_only": bool(email_only)},
        "parcels": parcels,
        "lockers": lks,
        "baseline": baseline,
        "optimized": None,
        "kpis": None,
    }
    if not active:
        return result

    # --- optimized tour -------------------------------------------------------
    captured_nodes = {i + 1 for i, p in enumerate(parcels) if p["captured_by"] is not None}
    init = [k for k in b_order if k not in captured_nodes]
    for pos, li in enumerate(active):
        init = C.cheapest_insertion(init, len(base_coords) + pos, dur)

    key = f"{int(seed)}|{';'.join(f'{lks[li]['lat']},{lks[li]['lon']}' for li in active)}|{int(radius_m)}|{bool(email_only)}"
    a_order = _after_order(key, coords, init)

    a_km, a_drive_min = _tour_stats(a_order, dur, dist)
    redirected = len(captured_nodes)
    home_stops = n - redirected
    a_service_min = (
        home_stops * C.STOP_SERVICE_S
        + sum(C.LOCKER_BASE_S + counts[li] * C.LOCKER_PER_PARCEL_S for li in active)
    ) / 60.0
    a_total_min = a_drive_min + a_service_min
    a_delivered = (n - n_failed - (redirected - failed_captured)) + redirected

    time_saved = b_total_min - a_total_min
    per_parcel_min = b_total_min / n
    optimized = {
        "distance_km": round(a_km, 2),
        "drive_min": round(a_drive_min, 1),
        "service_min": round(a_service_min, 1),
        "total_min": round(a_total_min, 1),
        "stops": home_stops + len(active),
        "delivered": a_delivered,
        "failed": n_failed - failed_captured,
        "polyline": route_poly(a_order),
    }
    result["optimized"] = optimized
    result["kpis"] = {
        "redirected": redirected,
        "failed_avoided": failed_captured,
        "distance_saved_km": round(b_km - a_km, 2),
        "time_saved_min": round(time_saved, 1),
        "stops_removed": n - (home_stops + len(active)),
        "productivity_before": round((n - n_failed) / (b_total_min / 10.0), 2),
        "productivity_after": round(a_delivered / (a_total_min / 10.0), 2),
        "extra_capacity": max(0, int(time_saved // per_parcel_min)),
    }
    print(
        f"seed={seed} lockers={len(lks)} active={len(active)} radius={radius_m} "
        f"redirected={redirected} saved={time_saved:.1f}min {b_km - a_km:.2f}km"
    )
    return result


# The fused-render runner (app >= Jul 2026) only invokes @fused.udf-registered
# entrypoints; a bare main() silently returns null. Register main via the shim.
try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass
