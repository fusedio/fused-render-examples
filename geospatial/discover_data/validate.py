"""Validate that user-entered URLs are usable STAC collection-search catalogs.

Called from the Catalogs panel before a new catalog list is applied. Uses plain
`requests` (bundled) rather than pystac-client (not bundled) -- but checks the
same things `pystac_client.Client.open` does: a landing page that declares
`stac_version` and a `conformsTo` list, plus a working `/collections` endpoint
(this app searches collections, so that endpoint is what actually matters).
"""

import os
import sys
import concurrent.futures as cf

# Resolve discover.py next to this file regardless of how the runner invokes us.
try:
    _HERE = os.path.dirname(os.path.abspath(__file__))
except NameError:
    _HERE = os.getcwd()
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import discover  # noqa: E402  -- reuse _get_json / _host / _parse_apis


def main(apis: str = "", timeout: float = 10.0):
    """Validate a comma/newline separated list of STAC API base URLs.

    Returns {"results": [{url, host, ok, message, stac_version,
                          n_collections, supports_q}, ...]} in input order.
    """
    urls = discover._parse_apis(apis)
    if not urls:
        return {"results": []}
    with cf.ThreadPoolExecutor(max_workers=min(8, len(urls))) as ex:
        results = list(ex.map(lambda u: _check_one(u, float(timeout)), urls))
    return {"results": results}


def _check_one(url, timeout):
    base = url.rstrip("/")
    out = {"url": url, "host": discover._host(base), "ok": False, "message": "",
           "stac_version": None, "n_collections": None, "supports_q": False}

    try:
        land = discover._get_json(base + "/", timeout)
    except Exception as e:
        out["message"] = f"unreachable ({type(e).__name__})"
        return out
    if not isinstance(land, dict):
        out["message"] = "did not return a JSON object"
        return out

    ver = land.get("stac_version")
    if not ver:
        out["message"] = "not a STAC endpoint (no stac_version)"
        return out
    out["stac_version"] = ver

    conf = land.get("conformsTo", []) or []
    out["supports_q"] = any("collection-search" in c for c in conf) and \
        any(("free-text" in c or "free_text" in c) for c in conf)

    try:
        cols = discover._get_json(base + "/collections", timeout, params={"limit": 1})
    except Exception as e:
        out["message"] = f"STAC {ver}, but /collections failed ({type(e).__name__}) -- collection search unsupported"
        return out
    if not isinstance(cols, dict) or "collections" not in cols:
        out["message"] = f"STAC {ver}, but /collections returned no collection list"
        return out

    out["n_collections"] = cols.get("numberMatched")
    out["ok"] = True
    mode = "server-side free-text" if out["supports_q"] else "filtered locally"
    out["message"] = f"STAC API {ver} - collection search OK - {mode}"
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(main(apis="https://stac.eoapi.dev, https://example.com"), indent=2))
