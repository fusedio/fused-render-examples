"""Suggest the best NEXT locker site.

Greedy: candidate sites are real shops/supermarkets (Overture places). Each is
scored by the parcels it would newly capture (excluding parcels already
captured by existing lockers, respecting the notification rule), weighted by
how tightly they cluster around the site — a pure captured-stop-density score,
no network calls, so it returns instantly. Adding the suggestion then runs the
full road-network simulation for the real KPI numbers.
"""

import os
import sys

if "__file__" in globals():
    # The fused-render runner already puts the script dir at sys.path[0].
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Hosted, the code runs with no __file__ (so the insert above is skipped) and
# bundled sibling modules land under the project's assets/ dir, which isn't on
# sys.path. Add it so `import _common` (and the shared helper below) resolve.
# Harmless locally: that dir doesn't exist there and both are already importable.
try:
    import openfused  # noqa: E402

    _assets_dir = os.path.join(openfused.project_root(), "assets")
    if os.path.isdir(_assets_dir):
        sys.path.insert(0, _assets_dir)
except ImportError:
    pass
import _common as C  # noqa: E402
from _common import _parse_lockers  # noqa: E402


def main(
    seed: int = 7,
    lockers: str = "",
    radius_m: int = 400,
    email_only: bool = False,
    top: int = 3,
) -> dict:
    parcels = C.make_parcels(seed)
    lks = _parse_lockers(lockers)
    shops = C.shop_candidates()

    # parcels still deliverable to a new locker
    free = []
    for p in parcels:
        if email_only and not p["has_email"]:
            continue
        if any(
            C.haversine_m(p["lat"], p["lon"], lk["lat"], lk["lon"]) <= radius_m
            for lk in lks
        ):
            continue
        free.append(p)

    scored = []
    for s in shops:
        captures, closeness = 0, 0.0
        for p in free:
            d = C.haversine_m(p["lat"], p["lon"], s["lat"], s["lon"])
            if d <= radius_m:
                captures += 1
                closeness += 1.0 - d / radius_m
        if captures:
            scored.append({**s, "captures": captures, "score": captures + 0.5 * closeness / max(captures, 1)})
    scored.sort(key=lambda x: -x["score"])

    # spatially de-duplicate: keep suggestions at least one radius apart
    picked = []
    for s in scored:
        if all(C.haversine_m(s["lat"], s["lon"], q["lat"], q["lon"]) > radius_m for q in picked):
            picked.append(s)
        if len(picked) >= int(top):
            break

    print(f"suggest: {len(free)} free parcels, {len(scored)} scoring shops")
    return {
        "suggestions": [
            {"lat": s["lat"], "lon": s["lon"], "name": s["name"],
             "category": s["category"], "captures": s["captures"]}
            for s in picked
        ]
    }
