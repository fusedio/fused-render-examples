"""Warm-up entry point: fetches (and disk-caches) the Overture address pool and
shop candidates. The Overture S3 scan can outlive the 30 s bridge budget on a
cold run, so this spawns a DETACHED warmer process and the page polls it until
{"ready": True}. Once warm, the main simulate call never pays the Overture cost
inside its own 30 s window."""

import os
import sys

if "__file__" in globals():
    # fused-render runs this file as its real path both locally and hosted
    # (bundle v2), so the sibling _common.py is in this dir. The runner already
    # puts it on sys.path[0]; add it explicitly so `import _common` resolves.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

_HERE = (os.path.dirname(os.path.abspath(__file__))
         if "__file__" in globals() else os.path.abspath(sys.path[0]))


def main(seed: int = 7) -> dict:
    targets = [C.address_pool.cache_path(), C.shop_candidates.cache_path()]
    code = (f"import sys; sys.path.insert(0, {_HERE!r}); import _common as C; "
            f"C.address_pool(); C.shop_candidates()")
    # tour_data only WARMS; the real Overture fetch happens in the simulate call.
    # Locally warm_via_daemon fills the disk cache in a background daemon so the
    # simulate call is instant. Do NOT also fetch here: on a hosted page there is no
    # cross-call cache, so a fetch here would just scan Overture a second time that
    # simulate repeats — and the page reads only `ready` from this call, never the
    # address/shop counts.
    return C.warm_via_daemon("overture", targets, code)
