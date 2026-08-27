"""Download one asset to disk -- the plain case only.

Streams the URL in chunks to `dest_dir` (or the default folder) and returns the
local path and size. No clipping, no subsetting, no archives: a downloaded
file is a local path, so "Open in Map" works on it through the exact same
handoff as the remote URL.

The default folder is ~/.fused-render/downloads -- a central place alongside the
rest of fused-render's data, so downloads from any app land together and survive
moving the app folder, rather than scattering into each project's data/downloads.
"""

import os
import tempfile
import time
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

DOWNLOAD_DIR = os.path.expanduser(os.path.join("~", ".fused-render", "downloads"))

# This download streams through runPython, which the executor kills at ~60 s
# (DEFAULT_TIMEOUT). A big or slow asset would hit that ceiling and die with no
# explanation, so watch the transfer and stop early -- the moment throughput
# shows it can't finish in time -- with a message that says why. Stay under the
# real ceiling so the check, not the kill, is what ends it.
_CEILING = 52.0


def main(url: str = "", dest_dir: str = "", name: str = ""):
    # Imported here, not at module scope: resolve_dir.py pulls this module in
    # just for clean_dir, and requests costs ~0.9 s to import in a subprocess
    # that does no HTTP at all (measured: 0.40 s vs 1.26 s).
    import discover

    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"not a downloadable URL: {url!r}")
    folder = clean_dir(dest_dir) or DOWNLOAD_DIR
    os.makedirs(folder, exist_ok=True)
    path = _reserve(folder, _safe_name(name, url))
    # A private temp per call, not `path + ".part"`: two downloads of assets that
    # share a basename would otherwise stream into the same file.
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".dl-", suffix=".part")
    # Opened right away and closed in `finally` below, covering the request
    # itself: on Windows an open handle blocks the cleanup path's os.remove,
    # turning a plain request failure into a PermissionError that hides it.
    f = os.fdopen(fd, "wb")

    started = time.time()
    size = 0
    try:
        try:
            with discover._SESSION.get(url, headers=discover._HEADERS,
                                       stream=True, timeout=30) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length") or 0)
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
                    size += len(chunk)
                    el = time.time() - started
                    # once there's a throughput sample, project the full transfer
                    # and bail early if it would run past the runPython ceiling
                    if total and el > 3 and total * el / size > _CEILING:
                        raise _too_large(total, size, el)
                    if el > _CEILING:
                        raise _too_large(total or size, size, el)
        finally:
            f.close()
        os.replace(tmp, path)
    except Exception:
        for leftover in (tmp, path):     # path is our own reservation
            if os.path.exists(leftover):
                os.remove(leftover)
        raise

    return {
        "path": path.replace("\\", "/"),
        "name": os.path.basename(path),
        "size": size,
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def _mb(n):
    for div, unit in ((1 << 30, "GB"), (1 << 20, "MB"), (1 << 10, "KB")):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{int(n)} B"


def _too_large(total, done, el):
    rate = done / el if el else 0
    eta = total / rate if rate else 0
    return RuntimeError(
        f"too large to download in-app: {_mb(total)} at ~{_mb(rate)}/s would take "
        f"~{int(eta)} s, past the ~60 s limit. Use the code snippet below to fetch it.")


_QUOTES = ((chr(34), chr(34)), ("'", "'"),
           ("\u201c", "\u201d"), ("\u2018", "\u2019"))  # incl. smart quotes


def clean_dir(raw):
    """Take a folder path however it was pasted and make it usable.

    People paste what their file manager gave them, which is rarely a bare path:
    Windows "Copy as path" wraps it in double quotes and uses backslashes, a
    browser or Finder hands over a `file://` URL with percent-escapes, and a
    hand-typed path uses `~` or an environment variable. Any of those left
    as-is becomes a literal directory name instead of the folder meant.
    Returns "" for empty input, which callers read as "use the default".
    """
    s = (raw or "").strip()
    for lo, hi in _QUOTES:
        if len(s) >= 2 and s.startswith(lo) and s.endswith(hi):
            s = s[1:-1].strip()
            break
    if s.lower().startswith("file://"):
        s = url2pathname(urlsplit(s).path)
    if not s:
        return ""
    # normpath also settles the separator: on Windows it turns "/" into "\", and
    # on POSIX a backslash is a legal filename character and must be left alone.
    return os.path.normpath(os.path.expandvars(os.path.expanduser(s)))


def _safe_name(name, url):
    """Decode before splitting: a percent-encoded separator in a third-party
    asset href (`b%2F..%2F..%2Fevil.tif`) would otherwise survive a basename
    call and let the write land outside `folder`. The colon goes too: os.path
    .join drops the folder entirely for a drive-qualified name like
    `D:evil.tif`, and on Windows a colon otherwise opens an NTFS data stream."""
    raw = name.strip() or unquote(urlsplit(url).path)
    fname = raw.replace("\\", "/").rsplit("/", 1)[-1].strip().replace(":", "_")
    return fname if fname not in ("", ".", "..") else "asset.bin"


def _reserve(folder, fname):
    """Claim a free name atomically and return it, never overwriting. Maxar
    names every quadkey's asset after its acquisition, so two genuinely
    different items collide on one basename -- and checking existence before
    writing loses that race when both downloads are in flight at once."""
    stem, ext = os.path.splitext(fname)
    for n in range(1, 1000):
        path = os.path.join(folder, fname if n == 1 else f"{stem}-{n}{ext}")
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return path
        except FileExistsError:
            continue
    raise RuntimeError(f"too many files named like {fname!r} in {folder}")


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(main(url=sys.argv[1])))
