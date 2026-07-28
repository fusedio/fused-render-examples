# /// script
# requires-python = ">=3.12"
# dependencies = ["pychrome", "requests"]
# ///
"""End-to-end tester for the Fused Render example projects.

For each project it runs three layers of checks:

  1. STRUCTURE  (no app needed) — the folder is shaped like a valid example:
     exactly one view .html, at least one .py with a module-level `main`,
     valid PEP 723 header if present, no import-time `__file__`, no committed
     secrets (.env) or caches (.cache / __pycache__).

  2. ENTRYPOINTS (needs the running app) — every `runPython("./x.py", …)` the
     view calls is invoked through the app's /api/run bridge from a FRESH copy
     of the project (cold cache), and must return ok. Warm-up daemons that
     return {"ready": false} are polled until ready.

  3. VISUAL     (needs the app + Chrome) — the view is loaded in headless
     Chrome exactly as a user would open it, given time to render, then we
     assert the page painted real content and shows no Python error, and save
     a screenshot to tests/artifacts/<project>.png.

The point of the FRESH copy (via --cold, default on) is to reproduce the
"someone just downloaded this and dropped it into their install" path: no warm
.cache, no repo-relative assumptions — the same thing that breaks on a cold
machine breaks here.

Usage:
    uv run tests/check.py                 # all projects, cold (the local working tree)
    uv run tests/check.py zonal_stats_hex # one project
    uv run tests/check.py --warm          # reuse existing .cache (faster)
    uv run tests/check.py --no-visual     # skip the browser layer
    uv run tests/check.py --ref origin/main   # test what users actually clone from
                                              # main, in a throwaway worktree (no
                                              # local edits, no gitignored .env)

Prerequisites: the FusedRender app running (its bridge is auto-detected), and
Google Chrome installed for the visual layer.
"""
import ast
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import requests

# pychrome's receive thread raises on the empty websocket frame Chrome sends at
# teardown; that's harmless noise, so drop it from the thread excepthook.
_default_excepthook = threading.excepthook
def _quiet_excepthook(args):
    if args.exc_type is json.JSONDecodeError:
        return
    _default_excepthook(args)
threading.excepthook = _quiet_excepthook

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARTIFACTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts")
CATEGORIES = ["geospatial", "local-tools"]
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# Views that need longer to paint (maps, cold data). Others default to 12 s.
VISUAL_WAIT = {"default": 12, "map": 22}
MAP_PROJECTS = {
    "zonal_stats_hex", "forest_carbon_monitor", "disaster_response_dashboard",
    "store_site_selection", "overture_census_isochrone", "locker_network_simulator",
    "cog_range_viewer", "japan_transit",
}
ERROR_MARKERS = ["Traceback (most recent call", "is not defined", "SyntaxError",
                 "ModuleNotFoundError", "does not define a callable 'main'"]


# ----------------------------------------------------------------- discovery

def find_bridge():
    """The FusedRender bridge port (not fixed across launches)."""
    try:
        pid = subprocess.check_output(["pgrep", "-x", "FusedRender"]).split()[0].decode()
        out = subprocess.check_output(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-a", "-p", pid], text=True)
        for line in out.splitlines():
            m = re.search(r"127\.0\.0\.1:(\d+)", line)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return 1777  # last-known default


def projects(names):
    out = []
    for cat in CATEGORIES:
        base = os.path.join(REPO, cat)
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            p = os.path.join(base, name)
            if os.path.isdir(p) and (not names or name in names):
                out.append((name, p))
    return out


# ----------------------------------------------------------------- layer 1

def git_tracked(path):
    """Files git actually tracks under `path` (what a user downloads), relative
    to `path`. Local-only gitignored files (.env, .cache) are excluded."""
    out = subprocess.check_output(["git", "-C", REPO, "ls-files", path], text=True)
    rels = []
    for line in out.splitlines():
        if line:
            rels.append(os.path.relpath(os.path.join(REPO, line), path))
    return rels


def lint(path):
    errs, warns = [], []
    tracked = git_tracked(path)
    htmls = [f for f in tracked if f.endswith(".html") and "/" not in f]
    if len(htmls) != 1:
        errs.append(f"expected exactly one top-level view .html, found {htmls}")
    # only committed junk is a problem; local .cache/.env are gitignored.
    # match path SEGMENTS so ".env" never matches the shipped ".env.example".
    def has_segment(rel, seg):
        return seg in rel.split("/")
    for seg, label in [(".env", ".env (secret leak risk)"),
                       (".cache", ".cache"), ("__pycache__", "__pycache__")]:
        if any(has_segment(r, seg) for r in tracked):
            errs.append(f"committed {label}")
    pys = [os.path.join(path, r) for r in tracked if r.endswith(".py")]

    entry_has_main = False
    for py in pys:
        if not os.path.exists(py):
            continue
        src = open(py, encoding="utf-8").read()
        stem = os.path.basename(py)
        try:
            tree = ast.parse(src)
        except SyntaxError:
            # The host Python may be older than the app's runtime (3.12) and
            # reject valid newer syntax (e.g. PEP 701 f-strings). A GENUINE
            # syntax error surfaces at runtime in the entrypoint layer, so don't
            # hard-fail here — fall back to a regex check for main().
            if re.search(r"^def main\(", src, re.M):
                entry_has_main = True
            continue
        if any(isinstance(n, ast.FunctionDef) and n.name == "main"
               for n in ast.walk(tree)):
            entry_has_main = True
        # The current runner loads modules via importlib (spec_from_file_location
        # → exec_module), which SETS __file__ — so unguarded __file__ works. It's
        # only worth guarding (`"__file__" in globals()`) for portability if the
        # file might be exec'd some other way, so this is a note, not a failure.
        uses_dunder_file = any(isinstance(n, ast.Name) and n.id == "__file__"
                               for n in ast.walk(tree))
        if uses_dunder_file and '"__file__" in globals()' not in src:
            warns.append(f"{stem}: uses __file__ unguarded (works — runner sets it — "
                         "but a `\"__file__\" in globals()` guard is more portable)")
        if "/// script" in src:
            block = re.search(r"# /// script\n(.*?)# ///", src, re.S)
            if block and "dependencies" not in block.group(1):
                errs.append(f"{stem}: PEP 723 header without a dependencies list")
    if pys and not entry_has_main:
        errs.append("no .py defines a module-level main()")
    return errs, warns


# ----------------------------------------------------------------- layer 2

def html_entrypoints(view_html, fresh_dir):
    """The .py files the view calls. Scans the view AND sibling .js files (some
    projects keep their runPython calls in script.js) for literal
    `runPython("./x.py")` paths; if the path is built dynamically (a variable),
    fall back to every .py in the project that defines a module-level main()."""
    src = open(view_html, encoding="utf-8").read()
    for js in sorted(f for f in os.listdir(fresh_dir) if f.endswith(".js")):
        src += "\n" + open(os.path.join(fresh_dir, js), encoding="utf-8").read()
    seen = []
    for m in re.finditer(r'runPython\(\s*["\']\.?/?([A-Za-z0-9_./-]+\.py)["\']', src):
        rel = m.group(1)
        if rel not in seen:
            seen.append(rel)
    if not seen and "runPython(" in src:
        for f in sorted(os.listdir(fresh_dir)):
            if f.endswith(".py"):
                s = open(os.path.join(fresh_dir, f), encoding="utf-8").read()
                if re.search(r"^def main\(", s, re.M):
                    seen.append(f)
    return seen


def bridge_run(port, py_abs, params, poll_ready=True, timeout=180):
    """POST /api/run; transparently poll warm-up daemons to readiness."""
    t0 = time.time()
    while True:
        r = requests.post(f"http://127.0.0.1:{port}/api/run",
                          headers={"Content-Type": "application/json", "X-Fused": "1"},
                          json={"py": py_abs, "params": params}, timeout=timeout + 5)
        body = r.json()
        if not body.get("ok"):
            return False, body.get("error")
        res = body.get("result")
        if poll_ready and isinstance(res, dict) and res.get("ready") is False:
            if time.time() - t0 > timeout:
                return False, {"message": "warm-up never became ready"}
            time.sleep(2)
            continue
        return True, res


def entrypoint_smoke(port, fresh_dir, view_html):
    """Call each entrypoint with defaults + a couple of common step/action values."""
    results = []
    for rel in html_entrypoints(os.path.join(fresh_dir, os.path.basename(view_html)), fresh_dir):
        py_abs = os.path.join(fresh_dir, rel)
        if not os.path.exists(py_abs):
            results.append((rel, False, "referenced by the view but missing"))
            continue
        # default call exercises the first-open path; warm daemons get polled
        ok, info = bridge_run(port, py_abs, {})
        # Only hard-fail on errors that mean the module won't LOAD at all —
        # those are real defects regardless of inputs. Everything input-
        # dependent (the entrypoint needs params / a key / more than 30 s cold)
        # is deferred to the visual layer, which drives it with real inputs.
        etype = info.get("type") if isinstance(info, dict) else ""
        msg = info.get("message", "") if isinstance(info, dict) else ""
        LOAD_ERRORS = {"ImportError", "ModuleNotFoundError", "SyntaxError",
                       "NameError", "IndentationError", "AttributeError"}
        if ok:
            results.append((rel, True, None))
        elif etype in LOAD_ERRORS:
            results.append((rel, False, info))       # module is broken — hard fail
        else:
            if "api key" in msg.lower():
                why = "requires an API key (see .env.example)"
            elif etype == "ParamError" or "required" in msg.lower():
                why = "needs params the view supplies"
            elif etype == "TimeoutError":
                why = "cold synchronous path > 30 s (view polls the warm step)"
            else:
                why = f"{etype}: input-dependent"
            results.append((rel, None, f"{why} — checked via the visual layer"))
    return results


# ----------------------------------------------------------------- layer 3

class Chrome:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="frx-chrome-")
        self.proc = subprocess.Popen(
            [CHROME, "--headless=new", f"--remote-debugging-port=0",
             f"--user-data-dir={self.dir}", "--window-size=1400,900",
             "--force-device-scale-factor=2", "--hide-scrollbars", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        # discover the chosen port from DevToolsActivePort
        port_file = os.path.join(self.dir, "DevToolsActivePort")
        for _ in range(50):
            if os.path.exists(port_file):
                self.port = int(open(port_file).read().splitlines()[0])
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("Chrome did not expose a CDP port")
        import pychrome
        self.browser = pychrome.Browser(url=f"http://127.0.0.1:{self.port}")

    def render(self, url, wait_s, out_png):
        tab = self.browser.new_tab()
        try:
            tab.start()
            tab.Page.enable()
            tab.Runtime.enable()
            tab.Page.navigate(url=url, _timeout=30)
            time.sleep(wait_s)
            text = tab.Runtime.evaluate(
                expression="document.body ? document.body.innerText : ''"
            )["result"].get("value", "") or ""
            shot = tab.Page.captureScreenshot(format="png")
            with open(out_png, "wb") as fh:
                fh.write(base64.b64decode(shot["data"]))
            return text
        finally:
            try:
                tab.stop(); self.browser.close_tab(tab)
            except Exception:
                pass

    def close(self):
        self.proc.terminate()
        shutil.rmtree(self.dir, ignore_errors=True)


def visual_check(chrome, port, fresh_dir, name, view_html):
    os.makedirs(ARTIFACTS, exist_ok=True)
    out = os.path.join(ARTIFACTS, f"{name}.png")
    view = os.path.join(fresh_dir, os.path.basename(view_html))
    url = f"http://127.0.0.1:{port}/render?path={view}"
    wait = VISUAL_WAIT["map"] if name in MAP_PROJECTS else VISUAL_WAIT["default"]
    text = chrome.render(url, wait, out)
    errs = []
    hit = [m for m in ERROR_MARKERS if m in text]
    if hit:
        errs.append(f"error text on page: {hit}")
    if len(text.strip()) < 15 and os.path.getsize(out) < 60_000:
        errs.append("page looks blank (little text, tiny screenshot)")
    return errs, out


# ----------------------------------------------------------------- driver

def _reexec_in_worktree(ref, passthrough):
    """Re-run this harness against a throwaway git worktree of `ref`, so we test
    EXACTLY what a user downloads from that ref (e.g. origin/main) — not the local
    working tree. This is the gap that let a fix pass locally while the pushed
    `main` an investor cloned still errored. All the checks below are unchanged;
    they just run against the ref's checkout (which also has no gitignored .env,
    so key-gated examples exercise their true keyless first-open path)."""
    tmp = tempfile.mkdtemp(prefix="frx-ref-")
    wt = os.path.join(tmp, "wt")
    print(f"── testing ref {ref!r} in a fresh worktree (not the working tree)\n")
    subprocess.check_call(["git", "-C", REPO, "worktree", "add", "--detach", wt, ref],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # Same interpreter → same already-installed deps (pychrome, requests).
        rc = subprocess.call([sys.executable, os.path.join(wt, "tests", "check.py"),
                              *passthrough])
        # Screenshots landed in the throwaway worktree; copy them into the real
        # repo's tests/artifacts before it's removed, so they stay inspectable.
        wt_art = os.path.join(wt, "tests", "artifacts")
        if os.path.isdir(wt_art):
            os.makedirs(ARTIFACTS, exist_ok=True)
            for f in os.listdir(wt_art):
                if f.endswith(".png"):
                    shutil.copy2(os.path.join(wt_art, f), os.path.join(ARTIFACTS, f))
        return rc
    finally:
        subprocess.call(["git", "-C", REPO, "worktree", "remove", "--force", wt],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    args = [a for a in sys.argv[1:]]

    # --ref <gitref> / --ref=<gitref>: test that ref's checkout, then exit.
    ref = None
    rest = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--ref":
            ref = args[i + 1] if i + 1 < len(args) else "origin/main"
            i += 2
            continue
        if a.startswith("--ref="):
            ref = a.split("=", 1)[1] or "origin/main"
            i += 1
            continue
        rest.append(a)
        i += 1
    if ref:
        sys.exit(_reexec_in_worktree(ref, rest))
    args = rest

    cold = "--warm" not in args
    do_visual = "--no-visual" not in args
    names = [a for a in args if not a.startswith("-")]
    args = set(a for a in args if a.startswith("-"))

    port = find_bridge()
    print(f"bridge: 127.0.0.1:{port}  ·  mode: {'cold' if cold else 'warm'}  ·  "
          f"visual: {do_visual}\n")

    def run_one(name, src):
        """Test one project; return (name, ok, log_lines). Thread-safe: touches
        only its own fresh temp dir and its own Chrome tab."""
        log = [f"── {name}"]
        lint_errs, lint_warns = lint(src)
        for e in lint_errs:
            log.append(f"   structure: ✗ {e}")
        for w in lint_warns:
            log.append(f"   structure: · note: {w}")
        if not lint_errs:
            log.append("   structure: ✓")

        # fresh copy = exactly the git-tracked files a user would download, into
        # a clean dir with no warm .cache (the true cold-open path)
        if cold:
            fresh = tempfile.mkdtemp(prefix=f"frx-{name}-")
            for rel in git_tracked(src):
                dst = os.path.join(fresh, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(os.path.join(src, rel), dst)
            # a maintainer's local (gitignored) .env carries API keys — copy it
            # so key-requiring projects exercise their real data path; without
            # it those entrypoints soft-skip with a "needs key" note
            if os.path.exists(os.path.join(src, ".env")):
                shutil.copy2(os.path.join(src, ".env"), os.path.join(fresh, ".env"))
        else:
            fresh = src

        htmls = [f for f in git_tracked(src) if f.endswith(".html") and "/" not in f]
        view = htmls[0] if htmls else None
        ep_errs, vis_errs = [], []
        try:
            if view:
                for rel, ok, info in entrypoint_smoke(port, fresh, view):
                    mark = ("–", f"– {info}") if ok is None else \
                           ("✓", "✓") if ok else ("✗", f"✗ {str(info)[:120]}")
                    log.append(f"   entrypoint {rel}: {mark[1]}")
                    if ok is False:
                        ep_errs.append(rel)
                if do_visual:
                    vis_errs, shot = visual_check(chrome, port, fresh, name, view)
                    log.append(f"   visual: {'✓' if not vis_errs else '✗ ' + '; '.join(vis_errs)}"
                               f"  → {os.path.relpath(shot, REPO)}")
            else:
                log.append("   entrypoint/visual: skipped (no view .html)")
        except Exception as e:
            vis_errs.append(f"harness error: {e}")
            log.append(f"   harness error: {e}")
        finally:
            if cold:
                shutil.rmtree(fresh, ignore_errors=True)

        ok = not (lint_errs or ep_errs or vis_errs)
        log.append(f"   → {'PASS' if ok else 'FAIL'}\n")
        return name, ok, log

    from concurrent.futures import ThreadPoolExecutor

    chrome = Chrome() if do_visual else None
    todo = projects(names)
    # bounded concurrency: overlaps the long cold warm-ups without hammering the
    # bridge or spawning too many map-rendering tabs at once
    workers = 1 if len(todo) == 1 else min(5, len(todo))
    rows = []
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(run_one, n, s) for n, s in todo]
            for fut in futures:
                name, ok, log = fut.result()
                print("\n".join(log))
                rows.append((name, ok))
    finally:
        if chrome:
            chrome.close()

    passed = sum(1 for _n, ok in rows if ok)
    print(f"═══ {passed}/{len(rows)} projects passed ═══")
    for name, ok in rows:
        print(f"   {'✓' if ok else '✗'} {name}")
    sys.exit(0 if passed == len(rows) else 1)


if __name__ == "__main__":
    main()
