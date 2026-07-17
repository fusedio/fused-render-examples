"""Warm-up entry point: fetches (and disk-caches) the Overture address pool and
shop candidates. The Overture S3 scan can outlive the 30 s bridge budget on a
cold run, so this spawns a DETACHED warmer process and the page polls it until
{"ready": True}. Once warm, the main simulate call never pays the Overture cost
inside its own 30 s window."""

import os
import sys

if "__file__" in globals():
    # The fused-render runner already puts the script dir at sys.path[0].
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Hosted, the code runs with no __file__ (so the insert above is skipped) and a
# bundled sibling module (_common.py) lands under the project's assets/ dir, which
# isn't on sys.path. Add it so `import _common` resolves. Harmless locally: that
# dir doesn't exist there and _common is already importable via the insert above.
try:
    import openfused  # noqa: E402

    _assets_dir = os.path.join(openfused.project_root(), "assets")
    if os.path.isdir(_assets_dir):
        sys.path.insert(0, _assets_dir)
except ImportError:
    pass
import _common as C  # noqa: E402

_HERE = (os.path.dirname(os.path.abspath(__file__))
         if "__file__" in globals() else os.path.abspath(sys.path[0]))


def main(seed: int = 7) -> dict:
    targets = [C.address_pool.cache_path(), C.shop_candidates.cache_path()]
    code = (f"import sys; sys.path.insert(0, {_HERE!r}); import _common as C; "
            f"C.address_pool(); C.shop_candidates()")
    status = C.warm_via_daemon("overture", targets, code)
    if not status.get("ready"):
        return status
    pool = C.address_pool()
    shops = C.shop_candidates()
    parcels = C.make_parcels(seed)
    print(f"address pool={len(pool)} shops={len(shops)} parcels={len(parcels)}")
    return {"ready": True, "pool": len(pool), "shops": len(shops), "parcels": len(parcels)}
