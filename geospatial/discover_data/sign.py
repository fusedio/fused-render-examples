"""Turn a download's asset href into one that can actually be fetched.

Some catalogs publish assets that sit in private storage and hand out a
short-lived token on request, with no account and no credentials involved.
Planetary Computer is the case that matters here: every one of its assets lives
in an Azure container that answers an unsigned GET with

    HTTP 409 Public access is not permitted on this storage account.

and its SAS API signs any href anonymously, so nothing is asked of the user.

This is for **downloads only**. `download.py` fetches the bytes itself and has
no token machinery, so it needs a signature at the moment of the click. The map
handoff deliberately does not come through here: the map template signs Azure
containers with its own `blob_tokens`, which refreshes the token as it ages,
and pinning one here would only go stale while the tab stayed open.

Anything already public is returned untouched, so a caller can route every asset
through here without caring which kind it is.
"""

import time
from urllib.parse import quote, urlsplit

import discover

# Planetary Computer's anonymous signing endpoint (no key, no account).
_PC_SIGN = "https://planetarycomputer.microsoft.com/api/sas/v1/sign?href="
_AZURE_SUFFIX = ".blob.core.windows.net"


def main(url: str = ""):
    """Sign `url` if its host needs it, otherwise hand it straight back."""
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"not a fetchable URL: {url!r}")

    started = time.time()
    signed, expires = False, ""
    if scheme_for(url) == "azure-sas":
        payload = discover._get_json(_PC_SIGN + quote(url, safe=""), 20.0)
        if payload.get("href"):
            url, signed = payload["href"], True
            expires = payload.get("msft:expiry", "")
    return {"url": url, "signed": signed, "expires": expires,
            "elapsed_ms": int((time.time() - started) * 1000)}


def scheme_for(url):
    """Which authorization an asset needs: "azure-sas", "none", or "" for
    anything we cannot reach at all (an s3:// href, say). Keyed on the storage
    host, not the catalog: Planetary Computer also serves public tile endpoints
    off its own domain, and those must not be sent to the signing API."""
    if not url.lower().startswith(("http://", "https://")):
        return ""
    if urlsplit(url).netloc.lower().endswith(_AZURE_SUFFIX):
        return "azure-sas"
    return "none"


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(main(url=sys.argv[1]), indent=2))
