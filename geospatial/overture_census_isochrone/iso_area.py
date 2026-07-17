"""Panel 1: geocode + ORS isochrone + isochrone hex universe.

Port of canvas UDFs geocode_point, latlng_isochrone_simplified,
isochrone_chosen_site_overture and isochrone_to_hex_overture, collapsed into
one fast bridge call (everything network-bound is disk-cached in _common).
"""

import os
import sys

if "__file__" in globals():
    # Running as a plain script; the fused-render runner already puts the
    # script dir at sys.path[0] and exposes no __file__.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Hosted, the code runs with no __file__ (so the insert above is skipped) and the
# bundled sibling module (_common.py) lands under the project's assets/ dir, which
# isn't on sys.path. Add it so `from _common import …` (in main) resolves. Harmless
# locally: that dir doesn't exist there and _common is already importable above.
try:
    import openfused  # noqa: E402

    _assets_dir = os.path.join(openfused.project_root(), "assets")
    if os.path.isdir(_assets_dir):
        sys.path.insert(0, _assets_dir)
except ImportError:
    pass


def main(
    address: str = "San Francisco, CA",
    travel_time_min: int = 15,
    transport_mode: str = "driving-car",
) -> dict:
    from _common import OVERTURE_RELEASE, h3_connect, polygon_to_cells, resolve_iso

    lat, lon, label, geometry, area_km2, res = resolve_iso(
        address, travel_time_min, transport_mode
    )

    con = h3_connect()
    iso_hexes = polygon_to_cells(con, geometry, res)
    con.close()

    print(
        f"iso_area: {address!r} -> ({lat:.4f},{lon:.4f}) "
        f"area={area_km2:.1f} km2 res={res} hexes={len(iso_hexes)}"
    )
    return {
        "address": address,
        "label": label,
        "center": [lat, lon],
        "geometry": geometry,
        "area_km2": round(area_km2, 2),
        "range_min": int(travel_time_min),
        "profile": transport_mode,
        "hex_res": res,
        "iso_hexes": iso_hexes,
        "overture_release": OVERTURE_RELEASE,
    }
