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

# The fused-render runner (app >= Jul 2026) only invokes @fused.udf-registered
# entrypoints; a bare main() silently returns null. Register main via the shim.
try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass
