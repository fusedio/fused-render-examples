"""Detached worker for one download task: `python daemon.py <task-id>`.

Spawned by `downloads.py`'s `start` action with `start_new_session=True`, so it
is not bound to the 60 s `runPython` timeout and survives the page — and the
app — going away. It owns the task for as long as it holds the heartbeat in
`_downloads/meta/<id>.lock`, and it stops early when `pause` drops a `.stop`
sentinel. All progress goes to the task's metadata file, which is the only
channel the UI reads.

This is a script, not a `main()` data file: nothing here is meant to be called
through `fused.runPython`.
"""

import os
import sys

# Spawned with cwd set to this directory, which is also where downloads.py sits —
# so cwd is the right fallback for runners that don't define `__file__`.
_here = os.path.abspath(os.path.dirname(globals().get("__file__", "")) or ".")
sys.path.insert(0, _here)

import downloads

MAX_SECONDS = 24 * 3600  # a backstop, not an expected duration
MAX_ATTEMPTS = 8         # per stall; the budget resets whenever bytes arrive


def main():
    if len(sys.argv) < 2:
        print("usage: daemon.py <task-id>", file=sys.stderr)
        return 2
    task_id = sys.argv[1]
    try:
        meta = downloads._load(task_id)
    except (OSError, ValueError) as exc:
        print("cannot load task %s: %s" % (task_id, exc), file=sys.stderr)
        return 1

    if downloads._locked(task_id):
        print("task %s already has a live worker" % task_id, file=sys.stderr)
        return 0

    # Claim the task before doing anything slow: the spawning call is watching
    # for this heartbeat to confirm the hand-off succeeded.
    downloads._beat(task_id)

    try:
        result = downloads._run(meta, MAX_SECONDS, attempts=MAX_ATTEMPTS)
        print("finished %s: %s" % (task_id, result.get("status")), file=sys.stderr)
        return 0
    except Exception as exc:
        # Never leave a task stuck on "active" — the UI would show a download
        # that nothing is working on.
        import traceback
        traceback.print_exc()
        try:
            meta = downloads._load(task_id)
            meta["status"] = "error"
            meta["error"] = "%s: %s" % (type(exc).__name__, exc)
            downloads._save(meta)
        except Exception:
            pass
        return 1
    finally:
        downloads._unlink(downloads._side_path(task_id, ".lock"))


if __name__ == "__main__":
    sys.exit(main())
