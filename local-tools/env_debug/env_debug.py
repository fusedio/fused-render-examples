def main(check_deps: str = ""):
    """System debug info for the current Python environment.

    Returns env var NAMES (never values), the Python version, whether we
    appear to be on a hosted Fused env vs. a local one, and which
    dependencies are importable.

    check_deps: comma-separated extra package names to probe, on top of the
                default set below.
    """
    import os
    import sys
    import platform
    import importlib
    import importlib.metadata as md

    # --- Python version ---------------------------------------------------
    python = {
        "version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "executable": sys.executable,
        "platform": platform.platform(),
    }

    # --- Env var NAMES only (never values) --------------------------------
    env_names = sorted(os.environ.keys())
    fused_env_names = [n for n in env_names if n.startswith("FUSED_")]

    # --- Hosted vs. local Fused env ---------------------------------------
    # On a hosted Fused runtime the system mount is present & writable, the
    # process usually runs inside AWS Lambda, and FUSED_* env vars are set.
    # Locally none of these hold.
    def _writable(p):
        try:
            return os.path.exists(p) and os.access(p, os.W_OK)
        except OSError:
            return False

    # Strong signals — any one of these means we're on a hosted Fused runtime.
    # (The /mount/fused-system writability check is what Fused's own
    # get_writable_dir() uses to decide it's running on the mounted drive.
    # OPENFUSED_DEPLOYED is the backend-injected signal other examples in this
    # repo gate on; in_realtime/in_batch are execution modes, not deployment
    # signals — local fused.runPython also runs in realtime, so they're not
    # used here.)
    strong = {
        "OPENFUSED_DEPLOYED set": bool(os.environ.get("OPENFUSED_DEPLOYED")),
        "/mount/fused-system writable": _writable("/mount/fused-system"),
        "AWS_LAMBDA_FUNCTION_NAME set": "AWS_LAMBDA_FUNCTION_NAME" in os.environ,
        "AWS_EXECUTION_ENV set": "AWS_EXECUTION_ENV" in os.environ,
    }

    # Weak/informational — presence alone does NOT imply hosted (a dev may set
    # FUSED_* vars locally, and in_realtime/in_batch reflect execution mode
    # rather than deployment), so these are shown but excluded from the verdict.
    weak = {
        "/mount exists": os.path.isdir("/mount"),
        "FUSED_* env var present": len(fused_env_names) > 0,
    }

    # Try the in-context signal if the fused package is importable.
    try:
        from fused.core._context import get_global_context

        ctx = get_global_context()
        weak["fused context: in_realtime"] = bool(getattr(ctx, "in_realtime", False))
        weak["fused context: in_batch"] = bool(getattr(ctx, "in_batch", False))
    except Exception:
        pass

    signals = {**strong, **weak}
    environment = {
        "verdict": "hosted" if any(strong.values()) else "local (non-hosted)",
        "signals": signals,
    }

    # --- Dependencies installed or not ------------------------------------
    default_deps = [
        "fused", "pandas", "geopandas", "shapely", "numpy", "pyarrow",
        "duckdb", "requests", "xarray", "rasterio", "matplotlib", "scipy",
    ]
    import re

    MAX_EXTRA_DEPS = 20
    valid_name = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
    extra = [d.strip() for d in check_deps.split(",") if d.strip()]
    extra = [d for d in extra if valid_name.match(d)][:MAX_EXTRA_DEPS]
    deps_to_check = default_deps + [d for d in extra if d not in default_deps]

    dependencies = []
    for name in deps_to_check:
        info = {"name": name, "installed": False, "version": None}
        try:
            info["version"] = md.version(name)
            info["installed"] = True
        except md.PackageNotFoundError:
            # Not installed as a distribution — fall back to import check.
            try:
                importlib.import_module(name)
                info["installed"] = True
                info["version"] = getattr(
                    importlib.import_module(name), "__version__", "unknown"
                )
            except Exception:
                info["installed"] = False
        dependencies.append(info)

    return {
        "python": python,
        "environment": environment,
        "env_var_names": env_names,
        "fused_env_var_names": fused_env_names,
        "dependencies": dependencies,
    }
