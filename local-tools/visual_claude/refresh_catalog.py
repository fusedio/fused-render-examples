"""Catalog refresh (preferences.md §5). Stdlib only.

Fetches Anthropic's settings reference markdown, parses the settings-key tables,
and updates the doc/default/minVersion half of settings_catalog.json IN PLACE —
keyed by each entry's `docKey` (falling back to `key`). The curated overlay
fields (label/group/control/options/docKey/unsetLabel) are preserved untouched.

`default` comes ONLY from the `**Default**: X` prose — the table's rightmost
column is an example value, never the default (§1 honesty rule). Undocumented
surfaced keys keep their existing doc/default and are reported as warnings.

Two triggers, one code path (preferences.md §5):
  - fused-callable action:  main() -> {ok, updated, total, undocumented[], error?}
    (never sys.exit/print for control flow — those crash or vanish under runPython).
    Explicit, user-triggered (the Preferences "Refresh catalog" button), never on load.
  - CLI:  python3 refresh_catalog.py  (the __main__ wrapper prints the same result).
"""
import json
import os
import re
import urllib.request
from typing import Any, Optional

SETTINGS_URL = "https://code.claude.com/docs/en/settings.md"
CATALOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings_catalog.json")
# Kept under fused-render's 30 s runPython cap (architecture.md §2) so a slow/hung
# fetch returns {ok:false, error} instead of tripping the subprocess timeout.
FETCH_TIMEOUT = 20

_ROW = re.compile(r"^\|\s*`([A-Za-z][A-Za-z0-9_.]*)`\s*\|(.*)\|([^|]*)\|\s*$")
_DEFAULT = re.compile(r"\*\*Default\*\*:\s*(`[^`]+`|[^\s.,;]+)")
_MINVER = re.compile(r"min-version:\s*([\d.]+)|Requires Claude Code v([\d.]+)")


def coerce_default(token: str) -> Any:
    tok = token.strip().strip("`").strip()
    try:
        return json.loads(tok)
    except ValueError:
        return tok.strip('"')


def extract_default(desc: str) -> Any:
    m = _DEFAULT.search(desc)
    return coerce_default(m.group(1)) if m else None


def extract_min_version(desc: str) -> Optional[str]:
    m = _MINVER.search(desc)
    return (m.group(1) or m.group(2)) if m else None


def clean_doc(desc: str) -> str:
    s = re.sub(r"\{/\*.*?\*/\}", " ", desc, flags=re.S)
    s = re.sub(r"^\s*\*\*Default\*\*:[^.]*\.\s*", "", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = s.replace("**", "").replace("`", "")
    s = re.sub(r"\s+", " ", s).strip()
    dot = s.find(". ")
    if dot != -1:
        s = s[: dot + 1]
    return s


def parse_settings_table(md: str) -> dict:
    start = md.find("### Available settings")
    if start == -1:
        raise ValueError("could not find '### Available settings' section")
    rest = md[start + 1 :]
    h2 = rest.find("\n## ")
    region = rest if h2 == -1 else rest[:h2]
    out = {}
    for line in region.split("\n"):
        m = _ROW.match(line)
        if not m:
            continue
        key, desc = m.group(1), m.group(2)  # group(3) = example col, unused
        if key in out:  # first occurrence wins
            continue
        out[key] = {"doc": clean_doc(desc), "default": extract_default(desc),
                    "minVersion": extract_min_version(desc)}
    return out


def main() -> dict:
    """fused-callable (preferences.md §5). Refresh in place; return a JSON result.
    On any failure the existing catalog is left untouched (§5 no-silent-truncation)
    and {ok:false, error} is returned — never raised — so the button surfaces it."""
    # A User-Agent is required — the docs host 403s the default urllib UA.
    req = urllib.request.Request(SETTINGS_URL, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
            md = r.read().decode("utf-8")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"fetch failed ({e}); kept existing catalog"}

    entries = parse_settings_table(md)
    # Sanity floor: docs document ~180 keys; a tiny result means the parser
    # missed the table — don't clobber the catalog.
    if len(entries) < 50:
        return {"ok": False,
                "error": f"parsed only {len(entries)} keys (expected ~180); docs shape changed"}

    with open(CATALOG_PATH, "r", encoding="utf-8") as f:
        catalog = json.load(f)

    updated, undocumented = 0, []
    for d in catalog:
        dk = d.get("docKey") or d["key"]
        e = entries.get(dk)
        if not e:
            undocumented.append(d["key"])
            continue
        d["doc"], d["default"], d["minVersion"] = e["doc"], e["default"], e["minVersion"]
        updated += 1

    with open(CATALOG_PATH, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)
        f.write("\n")

    return {"ok": True, "updated": updated, "total": len(catalog),
            "undocumented": undocumented}


if __name__ == "__main__":
    import sys
    res = main()
    if not res["ok"]:
        sys.exit(f"✗ {res['error']}")
    print(f"✓ refreshed {res['updated']}/{res['total']} catalog entries from docs")
    if res["undocumented"]:
        print(f"⚠ {len(res['undocumented'])} surfaced key(s) undocumented (kept existing): "
              f"{', '.join(res['undocumented'])}")
