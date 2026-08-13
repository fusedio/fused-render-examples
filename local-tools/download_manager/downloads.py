"""Multithreaded, resumable download engine.

Every `main()` call is a fresh subprocess killed at 60 s, so there is no
long-lived daemon here and no state in globals. All state lives on disk under
`_downloads/`:

    _downloads/meta/<id>.json   task metadata, incl. per-segment byte offsets
    _downloads/meta/<id>.lock   heartbeat proving exactly one live worker
    _downloads/meta/<id>.stop   sentinel written by `pause`
    _downloads/meta/<id>.log    daemon stderr, for when a spawn goes wrong
    _downloads/<name>.part      payload being filled by the worker threads
    _downloads/<name>           the finished file

`add` probes and registers a URL. There are then two ways to move bytes, and
they share `_run()`:

  start  spawns daemon.py detached (its own session), which downloads to
         completion and outlives both this call and the page. This is the
         normal path — the 60 s cap doesn't apply, and quitting the app no
         longer stops a download.
  pump   downloads inline for a bounded number of seconds and returns. The
         fallback for when spawning is unavailable; the page uses it only if
         `start` reports it could not hand off.

Either way, progress is persisted per segment as it happens, so an interrupted
download resumes instead of restarting — across a pause, a crash, or a reboot.
"""

import contextlib
import json
import os
import re
import threading
import time
import uuid
from urllib.parse import unquote, urlparse

CHUNK = 65536
LOCK_STALE = 8.0  # a heartbeat older than this means the worker died
MAX_THREADS = 16
SAMPLE_EVERY = 0.5     # seconds between speed samples
HISTORY_EVERY = 1.0    # seconds between points kept for the speed chart
HISTORY_MAX = 120      # points retained; bounds both the file and the poll payload
DEFAULT_DEST = "~/Downloads"


# ---------------------------------------------------------------- paths / io

def _root():
    """App-private state (metadata, locks, logs) — never the payload."""
    root = os.path.abspath("_downloads")
    os.makedirs(os.path.join(root, "meta"), exist_ok=True)
    return root


def _dest_dir(dest):
    """Resolve and validate a download folder, creating it if needed."""
    path = os.path.abspath(os.path.expanduser((dest or "").strip() or DEFAULT_DEST))
    if os.path.exists(path) and not os.path.isdir(path):
        raise ValueError("Not a folder: %s" % path)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        raise ValueError("Cannot create folder %s — %s" % (path, exc))
    if not os.access(path, os.W_OK):
        raise ValueError("Folder is not writable: %s" % path)
    return path


def _meta_dir():
    return os.path.join(_root(), "meta")


def _meta_path(task_id):
    return os.path.join(_meta_dir(), task_id + ".json")


def _side_path(task_id, ext):
    return os.path.join(_meta_dir(), task_id + ext)


def _load(task_id):
    with open(_meta_path(task_id), "r", encoding="utf-8") as fh:
        return json.load(fh)


def _save(meta):
    """Atomic write — a torn metadata file would lose a whole download."""
    path = _meta_path(meta["id"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(meta, fh)
    os.replace(tmp, path)


def _unlink(path):
    try:
        os.remove(path)
    except OSError:
        pass


# ------------------------------------------------- global bandwidth limit
# The limit has to hold across *processes*, since every task downloads in its own
# detached daemon. So the budget lives in one small file that all worker threads
# draw from under an advisory lock: a token bucket refilled at `rate` bytes/sec.
# Keeping `rate` inside that same file means a change applies to downloads that
# are already running, on their next chunk.

def _bucket_path():
    # Deliberately NOT in meta/ — everything named *.json in there is a task.
    return os.path.join(_root(), "bucket.json")


@contextlib.contextmanager
def _bucket():
    """Open the shared bucket exclusively; writes back whatever the body left."""
    fd = os.open(_bucket_path(), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        except Exception:
            # No advisory locking here (Windows). Threads within a process still
            # serialize on the GIL for these tiny updates, so the limit holds
            # approximately rather than exactly.
            pass
        try:
            state = json.loads(os.read(fd, 4096).decode("utf-8") or "{}")
        except ValueError:
            state = {}
        state.setdefault("rate", 0.0)
        state.setdefault("tokens", 0.0)
        state.setdefault("ts", time.time())
        yield state
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps(state).encode("utf-8"))
    finally:
        os.close(fd)  # releases the flock


def _burst(rate):
    """Bucket ceiling. At least one chunk, or _throttle could never grant."""
    return max(rate * 0.5, CHUNK)


def _current_rate():
    with _bucket() as state:
        return float(state.get("rate") or 0.0)


def _set_limit(rate):
    with _bucket() as state:
        state["rate"] = max(0.0, float(rate))
        state["tokens"] = min(state["tokens"], _burst(state["rate"]))
        current = state["rate"]
    return {"rate": current, "unlimited": current <= 0}


def _read_size(rate):
    """Smaller reads when throttled, so a slow limit means short sleeps.

    At 16 KB/s a full 64 KB chunk would mean sitting on the socket for four
    seconds at a time, which some servers hang up on.
    """
    if rate <= 0:
        return CHUNK
    return int(max(4096, min(CHUNK, rate // 4)))


def _throttle(nbytes, stop, deadline):
    """Wait until the shared bucket can pay for `nbytes`. No-op when unlimited."""
    while not stop.is_set() and time.time() < deadline:
        with _bucket() as state:
            rate = float(state.get("rate") or 0.0)
            if rate <= 0:
                return
            now = time.time()
            state["tokens"] = min(_burst(rate),
                                  state["tokens"] + max(0.0, now - state["ts"]) * rate)
            state["ts"] = now
            if state["tokens"] >= nbytes:
                state["tokens"] -= nbytes
                return
            wait = (nbytes - state["tokens"]) / rate
        # Short naps, so pausing stays responsive while throttled.
        time.sleep(min(0.25, max(0.01, wait)))


def _beat(task_id):
    """Refresh the liveness heartbeat for whoever is working this task."""
    with open(_side_path(task_id, ".lock"), "w", encoding="utf-8") as fh:
        json.dump({"pid": os.getpid(), "ts": time.time()}, fh)


def _locked(task_id):
    """True when another pump is alive and working this task."""
    try:
        with open(_side_path(task_id, ".lock"), "r", encoding="utf-8") as fh:
            return time.time() - json.load(fh).get("ts", 0) < LOCK_STALE
    except (OSError, ValueError):
        return False


# ------------------------------------------------------------------ metadata

def _safe_name(name):
    name = unquote(name or "").strip().replace("\\", "/").split("/")[-1]
    name = re.sub(r'[\x00-\x1f<>:"|?*]', "_", name)
    return name[:180] or "download"


def _name_from(url, headers):
    disp = headers.get("content-disposition") or ""
    m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", disp, re.I) or \
        re.search(r'filename="?([^";]+)"?', disp, re.I)
    if m:
        return _safe_name(m.group(1))
    return _safe_name(urlparse(url).path) or "download"


def _unique(path):
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 1
    while os.path.exists("%s (%d)%s" % (stem, n, ext)):
        n += 1
    return "%s (%d)%s" % (stem, n, ext)


def _segments(size, threads):
    """Split [0, size) into `threads` contiguous ranges."""
    if not size or threads <= 1:
        return [{"start": 0, "end": (size - 1) if size else None, "done": 0, "eof": False}]
    span = size // threads
    segs = []
    for i in range(threads):
        start = i * span
        end = size - 1 if i == threads - 1 else start + span - 1
        segs.append({"start": start, "end": end, "done": 0, "eof": False})
    return segs


def _seg_complete(seg):
    if seg["end"] is None:
        return bool(seg.get("eof"))
    return seg["start"] + seg["done"] > seg["end"]


def _progress(meta):
    downloaded = sum(s["done"] for s in meta["segments"])
    return {
        "id": meta["id"],
        "url": meta["url"],
        "name": meta["name"],
        "path": meta.get("path"),
        "dest": meta.get("dest"),
        "size": meta.get("size"),
        "threads": meta.get("threads"),
        "resumable": meta.get("resumable"),
        "status": meta.get("status"),
        "error": meta.get("error"),
        "created": meta.get("created"),
        "downloaded": downloaded,
        "speed": meta.get("speed") or 0.0,
        "peak": meta.get("peak") or 0.0,
        "active_seconds": meta.get("active_seconds") or 0.0,
        "history": meta.get("history") or [],
        "segments": [
            {
                "start": s["start"],
                "end": s["end"],
                "done": s["done"],
                "complete": _seg_complete(s),
            }
            for s in meta["segments"]
        ],
    }


# --------------------------------------------------------------- the pump

def _worker(meta, seg, deadline, stop, lock, part, errors, attempts):
    """Fill one segment from its current offset until it is complete or time is up.

    Reconnects on both kinds of interruption: an exception, and a body that
    simply ends early (a server closing mid-stream raises nothing). Because the
    retry budget resets whenever bytes actually arrive, a long download survives
    many transient faults while a genuinely dead URL still gives up.
    """
    import requests

    tries = 0
    while tries < attempts and not stop.is_set() and time.time() < deadline:
        if _seg_complete(seg):
            return
        start = seg["start"] + seg["done"]
        end = seg["end"]
        headers = {"Accept-Encoding": "identity"}
        if meta["resumable"]:
            headers["Range"] = "bytes=%d-%s" % (start, "" if end is None else end)
        moved = False
        try:
            with requests.get(meta["url"], headers=headers, stream=True, timeout=30) as r:
                r.raise_for_status()
                with open(part, "r+b") as fh:
                    fh.seek(start)
                    for chunk in r.iter_content(_read_size(_current_rate())):
                        if stop.is_set() or time.time() >= deadline:
                            return
                        if not chunk:
                            continue
                        # Pay for these bytes before keeping them. Sleeping here
                        # stops us draining the socket, so TCP backpressure slows
                        # the sender — that's what makes the cap real.
                        _throttle(len(chunk), stop, deadline)
                        if stop.is_set() or time.time() >= deadline:
                            return
                        if end is not None:  # a server that ignores Range must not overrun
                            room = end - (seg["start"] + seg["done"]) + 1
                            if room <= 0:
                                return
                            chunk = chunk[:room]
                        fh.write(chunk)
                        moved = True
                        with lock:
                            seg["done"] += len(chunk)
            if end is None:
                # Unknown total length: the stream ending is the only completion
                # signal there is, so we have to trust it.
                seg["eof"] = True
                return
            if _seg_complete(seg):
                return
            errors.append("stream ended early at byte %d" % (seg["start"] + seg["done"]))
        except Exception as exc:
            errors.append("%s: %s" % (type(exc).__name__, exc))
        tries = 0 if moved else tries + 1
        if tries:
            time.sleep(min(5.0, 0.5 * tries))  # back off only when nothing arrived


def _run(meta, seconds, attempts=3):
    """Download until done, interrupted, or `seconds` elapse.

    The bounded call (`pump`) and the detached daemon share this body — they
    differ only in how long they're allowed to run and how hard they retry.
    """
    task_id = meta["id"]
    part = meta["part"]
    if not os.path.exists(part):
        with open(part, "wb") as fh:
            if meta.get("size"):
                fh.truncate(meta["size"])

    pending = [s for s in meta["segments"] if not _seg_complete(s)]
    if not pending:
        return _finish(meta)

    if not meta["resumable"]:
        # No ranges means no resume: a restarted stream must rewrite from zero.
        for seg in pending:
            seg["done"] = 0

    meta["status"] = "active"
    _save(meta)
    _beat(task_id)  # claim the task before any thread starts, so `start` can confirm

    stop = threading.Event()
    lock = threading.Lock()
    errors = []
    deadline = time.time() + seconds
    threads = [
        threading.Thread(target=_worker,
                         args=(meta, s, deadline, stop, lock, part, errors, attempts),
                         daemon=True)
        for s in pending
    ]
    for t in threads:
        t.start()

    # Heartbeat the lock, sample the rate, and flush progress so the UI sees
    # movement. Speed is measured here rather than in the page because this loop
    # keeps running when no page is open — and because a refreshed page can then
    # show a real number immediately instead of waiting for two polls.
    sample_t = time.time()
    sample_b = sum(s["done"] for s in meta["segments"])
    history_t = sample_t
    while any(t.is_alive() for t in threads):
        if os.path.exists(_side_path(task_id, ".stop")):
            stop.set()
        _beat(task_id)
        with lock:
            now = time.time()
            elapsed = now - sample_t
            if elapsed >= SAMPLE_EVERY:
                bytes_now = sum(s["done"] for s in meta["segments"])
                inst = max(0.0, (bytes_now - sample_b) / elapsed)
                prev = meta.get("speed") or 0.0
                # Smoothed so the reading is legible, but seeded by the first
                # sample so it climbs immediately instead of ramping from zero.
                meta["speed"] = round(inst if prev <= 0 else prev * 0.6 + inst * 0.4, 1)
                meta["peak"] = round(max(meta.get("peak") or 0.0, meta["speed"]), 1)
                meta["active_seconds"] = round((meta.get("active_seconds") or 0.0) + elapsed, 2)
                sample_t, sample_b = now, bytes_now
            if now - history_t >= HISTORY_EVERY:
                # The series the UI charts. Kept here rather than in the page so
                # it covers the whole download, including time with no page open.
                hist = meta.setdefault("history", [])
                hist.append(int(meta.get("speed") or 0))
                del hist[:-HISTORY_MAX]
                history_t = now
            _save(meta)
        time.sleep(0.3)
    for t in threads:
        t.join()
    meta["speed"] = 0.0  # nothing is moving now; don't leave a stale rate on screen
    # Land the series at zero so a pause or a finish reads as a drop to idle
    # rather than as a chart that stops mid-air.
    hist = meta.setdefault("history", [])
    if hist and hist[-1] != 0:
        hist.append(0)
        del hist[:-HISTORY_MAX]
    _unlink(_side_path(task_id, ".lock"))

    if os.path.exists(_side_path(task_id, ".stop")):
        _unlink(_side_path(task_id, ".stop"))
        meta["status"] = "paused"
        _save(meta)
        return _progress(meta)

    if all(_seg_complete(s) for s in meta["segments"]):
        return _finish(meta)

    if errors and not any(s["done"] for s in pending):
        meta["status"] = "error"
        meta["error"] = errors[0]
    else:
        meta["status"] = "waiting"  # more to do; the page pumps again
        meta["error"] = None
    _save(meta)
    return _progress(meta)


def _finish(meta):
    """Trim the part file to the real size and move it into place."""
    part = meta["part"]
    if os.path.exists(part):
        if not meta.get("size"):
            meta["size"] = sum(s["done"] for s in meta["segments"])
        final = _unique(meta["path"])
        os.replace(part, final)
        meta["path"] = final
    meta["status"] = "done"
    meta["error"] = None
    _save(meta)
    return _progress(meta)


# --------------------------------------------------------------- actions

def _probe(url):
    """Ask the server what this URL is: name, size, and whether ranges work.

    A single 1-byte range request answers all three, and it's the only place the
    authoritative filename lives — `Content-Disposition` and the post-redirect
    URL are both invisible to the page.
    """
    import requests

    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        raise ValueError("Enter an http(s) URL")

    size, resumable = None, False
    try:
        r = requests.get(url, headers={"Range": "bytes=0-0", "Accept-Encoding": "identity"},
                         stream=True, timeout=20, allow_redirects=True)
        r.raise_for_status()
        headers = {k.lower(): v for k, v in r.headers.items()}
        final_url = r.url or url
        r.close()
    except Exception as exc:
        raise RuntimeError("Could not reach the URL — %s: %s" % (type(exc).__name__, exc))

    rng = headers.get("content-range") or ""
    m = re.search(r"/(\d+)\s*$", rng)
    if r.status_code == 206 and m:
        size, resumable = int(m.group(1)), True
    elif headers.get("content-length"):
        size = int(headers["content-length"])

    return {
        "url": url,
        # Redirects are followed, so the name comes from where we landed.
        "name": _name_from(final_url, headers),
        "size": size,
        "resumable": resumable,
        "content_type": (headers.get("content-type") or "").split(";")[0] or None,
        "max_threads": MAX_THREADS if (resumable and size) else 1,
    }


def _add(url, threads, name, dest):
    url = (url or "").strip()
    folder = _dest_dir(dest)  # validated before any network work
    info = _probe(url)
    size, resumable = info["size"], info["resumable"]

    threads = max(1, min(MAX_THREADS, int(threads or 4)))
    if not resumable or not size:
        threads = 1  # can't split what we can't range-request

    task_id = uuid.uuid4().hex[:12]
    fname = _safe_name(name) if name else info["name"]
    meta = {
        "id": task_id,
        "url": url,
        "name": fname,
        "dest": folder,
        # The part file sits beside its destination so finishing is a rename
        # within one filesystem — os.replace across devices would fail.
        "path": os.path.join(folder, fname),
        "part": os.path.join(folder, "%s.%s.part" % (fname, task_id)),
        "size": size,
        "resumable": resumable,
        "threads": threads,
        "status": "waiting",
        "error": None,
        "created": time.time(),
        "speed": 0.0,
        "peak": 0.0,
        "active_seconds": 0.0,
        "history": [],
        "segments": _segments(size, threads),
    }
    _save(meta)
    return _progress(meta)


def _start(meta):
    """Hand the task to a detached daemon and confirm it took ownership.

    `start_new_session` puts the child in its own process group, so it outlives
    both this 60 s-capped call and the page that asked for it — closing the app
    no longer stops a download. `__file__` does not exist under the runner, but
    the working directory is this script's directory, so daemon.py resolves
    from cwd.
    """
    import subprocess
    import sys

    task_id = meta["id"]
    _unlink(_side_path(task_id, ".stop"))
    meta["status"] = "active"
    meta["error"] = None
    _save(meta)

    log = open(_side_path(task_id, ".log"), "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath("daemon.py"), task_id],
            cwd=os.getcwd(), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, close_fds=True,
        )
    except Exception as exc:
        log.close()
        meta["status"] = "waiting"
        _save(meta)
        return dict(_progress(meta), spawned=False,
                    spawn_error="%s: %s" % (type(exc).__name__, exc))
    finally:
        log.close()

    # The daemon writes the heartbeat before starting any thread, so its
    # appearance is proof of a live worker — not merely a successful fork.
    for _ in range(40):
        if _locked(task_id):
            return dict(_progress(meta), spawned=True, pid=proc.pid)
        if proc.poll() is not None:
            break
        time.sleep(0.05)

    # Re-read: a daemon that ran and exited very quickly may already have written
    # a final status, and stamping our stale copy over it would lose the result.
    fresh = _load(task_id)
    if fresh["status"] == "active":
        fresh["status"] = "waiting"
        _save(fresh)
    tail = ""
    try:
        with open(_side_path(task_id, ".log"), "r", encoding="utf-8", errors="replace") as fh:
            tail = fh.read()[-400:].strip()
    except OSError:
        pass
    return dict(_progress(fresh), spawned=fresh["status"] == "done",
                spawn_error=tail or "daemon exited before claiming the task")


def _os_open(meta, reveal):
    """Hand a finished file to the desktop: reveal it, or launch it.

    The file is already on this machine, so the UI must not route it back
    through HTTP — that would make the browser save a second copy named after
    the endpoint instead of opening the real thing.
    """
    import subprocess
    import sys

    path = meta.get("path") or ""
    if meta.get("status") != "done":
        raise ValueError("Download is not finished yet")
    if not os.path.exists(path):
        raise ValueError("File is no longer there: %s" % path)

    if sys.platform == "darwin":
        cmd = ["open", "-R", path] if reveal else ["open", path]
    elif os.name == "nt":
        if not reveal:
            os.startfile(path)  # noqa: S606 - the user asked to open their own file
            return {"ok": True, "path": path}
        cmd = ["explorer", "/select,%s" % path]
    else:
        cmd = ["xdg-open", os.path.dirname(path) if reveal else path]

    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return {"ok": True, "path": path}


def _list():
    tasks = []
    for fn in os.listdir(_meta_dir()):
        if not fn.endswith(".json"):
            continue
        try:
            meta = _load(fn[:-5])
        except (OSError, ValueError):
            continue
        if "segments" not in meta:
            continue  # not a task file; never let a stray json break the list
        prog = _progress(meta)
        prog["busy"] = _locked(meta["id"])
        if not prog["busy"]:
            prog["speed"] = 0.0  # a dead worker's last rate is not current
            # A task marked active with no live worker was orphaned by a crash.
            if prog["status"] == "active":
                prog["status"] = "waiting"
        tasks.append(prog)
    tasks.sort(key=lambda t: t.get("created") or 0, reverse=True)
    return {"tasks": tasks}


def _remove(task_id, delete_file):
    try:
        meta = _load(task_id)
    except (OSError, ValueError):
        return {"ok": True}
    if _locked(task_id):
        # Ask the daemon to wind down first; deleting under a live writer would
        # just have it recreate the part file and the metadata.
        with open(_side_path(task_id, ".stop"), "w", encoding="utf-8") as fh:
            fh.write("1")
        for _ in range(40):
            if not _locked(task_id):
                break
            time.sleep(0.1)
    _unlink(meta["part"])
    if delete_file and meta.get("status") == "done":
        _unlink(meta["path"])
    for ext in (".json", ".lock", ".stop", ".log"):
        _unlink(_side_path(task_id, ext))
    return {"ok": True}


def main(action: str = "list", id: str = "", url: str = "", name: str = "",
         dest: str = "", threads: int = 4, seconds: float = 10.0,
         rate: float = -1.0, delete_file: bool = False):
    if action == "list":
        return _list()

    if action == "defaults":
        return {"dest": _dest_dir(dest), "max_threads": MAX_THREADS,
                "rate": _current_rate()}

    if action == "limit":
        # rate < 0 means "just tell me the current value"
        if rate < 0:
            current = _current_rate()
            return {"rate": current, "unlimited": current <= 0}
        return _set_limit(rate)

    if action == "probe":
        return _probe(url)

    if action == "add":
        return _add(url, threads, name, dest)

    if action == "remove":
        return _remove(id, delete_file)

    meta = _load(id)

    if action in ("reveal", "launch"):
        return _os_open(meta, reveal=action == "reveal")

    if action == "pause":
        if _locked(id):  # let the live pump wind itself down
            with open(_side_path(id, ".stop"), "w", encoding="utf-8") as fh:
                fh.write("1")
        else:
            meta["status"] = "paused"
            _save(meta)
        return _progress(meta)

    if action == "resume":
        _unlink(_side_path(id, ".stop"))
        if meta["status"] in ("paused", "error"):
            meta["status"] = "waiting"
            meta["error"] = None
            _save(meta)
        return _progress(meta)

    if action in ("start", "pump"):
        if meta["status"] in ("done", "paused"):
            return _progress(meta)
        if _locked(id):  # somebody already owns it
            return dict(_progress(meta), busy=True, spawned=True)
        if action == "start":
            return _start(meta)
        return _run(meta, max(1.0, min(45.0, float(seconds))))

    raise ValueError("Unknown action: %s" % action)
