#!/usr/bin/env python3
"""Validate the repo's Python and client JS (architecture.md §7).

Runs, in order, whatever is available on PATH:
  Python  — py_compile (syntax, stdlib) + pyright (types)
  JS      — node --check (syntax) + tsc -p jsconfig.json (types, via JSDoc)

Tools that aren't installed are SKIPPED with a note, not failed — so this stays
portable across machines (and other fused-render projects) where only some of
pyright/node/tsc exist. Exit code is non-zero iff a tool that DID run reported a
real problem. This is not a substitute for running the app in fused-render — it
cannot see behavior, only static defects.

Usage:  python3 validate.py
"""
import glob
import shutil
import subprocess
import sys
import py_compile

ROOT = __file__.rsplit("/", 1)[0] or "."


def _which(name):
    return shutil.which(name)


def _run(cmd, cwd=ROOT):
    """Run a command, streaming nothing; return (rc, combined output)."""
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def check_python():
    print("== Python ==")
    ok = True
    pys = sorted(glob.glob(f"{ROOT}/*.py"))
    # py_compile: syntax, always available (stdlib).
    bad = []
    for f in pys:
        try:
            py_compile.compile(f, doraise=True)
        except py_compile.PyCompileError as e:
            bad.append(str(e))
    if bad:
        ok = False
        print("  py_compile: FAIL")
        for b in bad:
            print("   ", b)
    else:
        print(f"  py_compile: OK ({len(pys)} files)")

    # Run pyright (type checking) if it is installed.
    if _which("pyright"):
        rc, out = _run(["pyright", *[f.split("/")[-1] for f in pys]])
        # pyright prints a summary line even on success; only fail on rc != 0.
        summary = out.splitlines()[-1] if out else ""
        if rc != 0:
            ok = False
            print("  pyright: FAIL")
            print("   ", "\n    ".join(out.splitlines()[-20:]))
        else:
            print(f"  pyright: OK ({summary})")
    else:
        print("  pyright: SKIP (not installed)")
    return ok


def check_js():
    print("== Client JS ==")
    ok = True
    script = f"{ROOT}/script.js"

    # node --check: syntax.
    if _which("node"):
        rc, out = _run(["node", "--check", script])
        if rc != 0:
            ok = False
            print("  node --check: FAIL")
            print("   ", out)
        else:
            print("  node --check: OK")
    else:
        print("  node --check: SKIP (node not installed)")

    # tsc -p jsconfig.json: semantic checking of the JSDoc-typed module.
    if _which("tsc"):
        rc, out = _run(["tsc", "-p", "jsconfig.json"])
        if rc != 0:
            ok = False
            print("  tsc --checkJs: FAIL")
            print("   ", "\n    ".join(out.splitlines()[:40]))
        else:
            print("  tsc --checkJs: OK")
    else:
        print("  tsc --checkJs: SKIP (tsc not installed)")
    return ok


def main():
    py_ok = check_python()
    js_ok = check_js()
    print()
    if py_ok and js_ok:
        print("validate: OK")
        return 0
    print("validate: FAILURES above")
    return 1


if __name__ == "__main__":
    sys.exit(main())
