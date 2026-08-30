"""Build the local evaluation dataset: Philadelphia's official building
footprints scored against eight Overture Maps releases.

runPython entrypoint:
    main(action="status")  -> progress snapshot for the UI
    main(action="start")   -> spawn the detached worker if not already running

Worker (python prepare.py --worker):
    1. download the city's bulk GeoJSON        -> philly_base.parquet
    2. fetch each release from the Fused mirror -> overture_<r>.parquet
    3. DuckDB spatial join per release          -> iou_<r>.parquet + summary
    4. wide tables for stats + tiles            -> stats.parquet, detail.parquet
    5. PMTiles for the city map, then one per release for Overture's own
       geometry                                 -> *.pmtiles
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# The detached worker below relaunches this file as `python prepare.py
# --worker`. Fused Render's packaged interpreter is built from a Windows
# embeddable distribution (a `._pth` file pins sys.path at start-up), and a
# venv built from it inherits that: the script's own directory is never
# auto-added, so the sibling import below would raise ModuleNotFoundError
# for a plain `python prepare.py` invocation. Harmless when it's already
# there (running through runPython's own sys.path setup).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from common import (
    BANDS,
    CACHE,
    MIRROR,
    MIRROR_LIST,
    PHILLY_BOUNDS,
    PHILLY_GEOJSON_URL,
    RELEASES,
    cache_path,
    connect_duckdb,
    connect_duckdb_remote,
    read_json,
    release_key,
    write_json_atomic,
)

PROGRESS = os.path.join(CACHE, "progress.json")
READY = os.path.join(CACHE, "ready.json")
LOG = os.path.join(CACHE, "prepare.log")

# Shared with index.html's fused.trackJob({id: JOB_ID, ...}) call so the page
# and this detached worker report into the SAME download-manager row instead
# of opening two for one build.
JOB_ID = "overture-footprint-scorecard"

_progress_lock = threading.Lock()
_progress = {}
_job = None


def main(action: str = "status"):
    if action == "status":
        return _status()
    if action == "start":
        return _start()
    raise ValueError(f"unknown action {action!r}")


def _status():
    import tiler

    ready = read_json(READY)
    if ready:
        return {"ready": True,
                "summary": dict(ready, outlines=tiler.available_outlines())}
    progress = read_json(PROGRESS) or {}
    running = bool(progress.get("pid")) and _pid_alive(progress.get("pid"))
    if progress and not running and not progress.get("error"):
        progress = dict(progress)
        progress["error"] = "the prepare worker stopped unexpectedly (see .cache/prepare.log)"
    return {"ready": False, "running": running, "progress": progress}


def _spawn_python():
    """Interpreter for the detached worker. On Windows prefer pythonw.exe
    (no console subsystem at all) beside whichever python is running this
    file, so the ~45-60 min download never flashes a terminal window —
    DETACHED_PROCESS alone isn't always enough in practice. Falls back to
    sys.executable everywhere else, and when there is no pythonw.exe."""
    exe = sys.executable
    if os.name == "nt" and exe:
        candidate = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(candidate):
            return candidate
    return exe


def _clean_env():
    """os.environ minus PYTHONHOME/PYTHONPATH, for spawning the worker: a
    bundle-scoped value here would leak the packaged app's own stdlib/site
    into the worker and shadow whichever interpreter's packages it should
    actually see."""
    return {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "PYTHONPATH")}


def _start():
    status = _status()
    if status.get("ready") or status.get("running"):
        return status
    os.makedirs(CACHE, exist_ok=True)
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    log = open(LOG, "a", encoding="utf-8")
    proc = subprocess.Popen(
        [_spawn_python(), os.path.abspath(__file__), "--worker"],
        stdout=log,
        stderr=log,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
        start_new_session=(os.name != "nt"),
        cwd=os.path.dirname(os.path.abspath(__file__)),
        env=_clean_env(),
    )
    log.close()
    _publish({"pid": proc.pid, "stage": "starting", "steps": _fresh_steps()})
    return {"ready": False, "running": True, "progress": read_json(PROGRESS) or {}}


def _pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- progress

def _fresh_steps():
    steps = [{"id": "philly", "label": "Philadelphia footprints", "pct": 0}]
    for release in RELEASES:
        steps.append({"id": f"fetch:{release}", "label": f"Overture {release}", "pct": 0})
    for release in RELEASES:
        steps.append({"id": f"score:{release}", "label": f"Score {release}", "pct": 0})
    steps.append({"id": "tiles", "label": "Map tiles", "pct": 0})
    return steps


def _job_progress():
    """(done, total, detail) across every step, for the download-manager row —
    fractional (a step half done counts as 0.5), so the bar moves smoothly
    instead of jumping once per completed step."""
    steps = _progress.get("steps") or []
    if not steps:
        return 0, 1, None
    done = sum(min(100.0, step.get("pct", 0)) for step in steps) / 100.0
    current = next((step for step in steps if 0 < step.get("pct", 0) < 100), None)
    return done, len(steps), current["label"] if current else None


def _publish(update=None):
    with _progress_lock:
        if update:
            _progress.update(update)
        _progress["updated_at"] = time.time()
        write_json_atomic(PROGRESS, _progress, best_effort=True)
    if _job:
        _job.update(*_job_progress())


def _step(step_id, pct, note=None):
    with _progress_lock:
        for step in _progress.get("steps", []):
            if step["id"] == step_id:
                step["pct"] = round(min(100.0, pct), 1)
                if note is not None:
                    step["note"] = note
        _progress["stage"] = step_id
        _progress["updated_at"] = time.time()
        write_json_atomic(PROGRESS, _progress, best_effort=True)
    if _job:
        _job.update(*_job_progress())


class _JobReport:
    """Best-effort progress report to the shell's download manager
    (fused-render-authoring skill's "Long-running work" pattern) — index.html
    opens the same row with fused.trackJob({id: JOB_ID, ...}) so it stays
    visible if the user browses to another file while this runs. Never
    raises: reporting must not break the build. Posts are rate-limited to
    ~1/s since _step()/_publish() fire far more often than that.
    """

    def __init__(self, job_id, title):
        origin = (os.environ.get("FUSED_RENDER_ORIGIN") or "").rstrip("/")
        self.url = origin + "/api/jobs"
        self.id = job_id
        self.enabled = origin.startswith("http")
        self._last_post = 0.0
        if self.enabled:
            self._post(title=title, kind="download", state="running", cancellable=False)

    def _post(self, **fields):
        if not self.enabled:
            return
        fields["id"] = self.id
        request = urllib.request.Request(
            self.url, data=json.dumps(fields).encode(),
            headers={"Content-Type": "application/json", "X-Fused": "1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=3):
                pass
        except (urllib.error.URLError, OSError, ValueError):
            pass

    def update(self, done, total, detail=None):
        now = time.time()
        if now - self._last_post < 1.0:
            return
        self._last_post = now
        self._post(done=done, total=total, detail=detail)

    def finish(self, detail=None):
        self._post(state="done", detail=detail)

    def fail(self, message):
        self._post(state="error", detail=message)


# ---------------------------------------------------------------- worker

def worker():
    global _job
    _progress.update({"pid": os.getpid(), "stage": "starting",
                      "steps": _fresh_steps(), "started_at": time.time(),
                      "error": None})
    _job = _JobReport(JOB_ID, "Overture vs Philadelphia dataset")
    _publish()
    try:
        _download_philly()
        _fetch_releases()
        ensure_bbox_columns()
        summaries = [_score_release(release) for release in RELEASES]
        _build_wide_tables()
        _build_tiles()
        _finish(summaries)
        _job.finish("Ready")
    except Exception as error:  # surfaced to the UI via progress.json
        import traceback
        traceback.print_exc()
        _publish({"error": f"{type(error).__name__}: {error}"})
        _job.fail(f"{type(error).__name__}: {error}")
        raise


def _download_philly():
    out = cache_path("philly_base.parquet")
    if os.path.exists(out):
        _step("philly", 100, "cached")
        return
    raw = cache_path("philly.geojson")
    if not os.path.exists(raw):
        tmp = raw + ".tmp"
        request = urllib.request.Request(PHILLY_GEOJSON_URL, headers={"User-Agent": "fused-render-example"})
        with urllib.request.urlopen(request, timeout=120) as response, open(tmp, "wb") as f:
            done = 0
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                _step("philly", min(60.0, done / (400 << 20) * 60), f"{done >> 20} MB")
        os.replace(tmp, raw)
    _step("philly", 65, "converting")
    con = connect_duckdb()
    columns = {row[0].lower(): row[0] for row in
               con.execute(f"DESCRIBE SELECT * FROM ST_Read('{_sql_path(raw)}')").fetchall()}

    def col(name, out_name, cast):
        source = columns.get(name)
        return (f'"{source}"::{cast} AS {out_name}' if source else
                f"NULL::{cast} AS {out_name}")

    con.execute(f"""
        COPY (
            SELECT
                row_number() OVER () AS fid,
                {col('objectid', 'objectid', 'BIGINT')},
                {col('address', 'address', 'VARCHAR')},
                {col('building_name', 'building_name', 'VARCHAR')},
                {col('approx_hgt', 'height', 'DOUBLE')},
                ST_AsWKB(geom_valid) AS wkb,
                ST_AsWKB(ST_Transform(geom_valid, 'EPSG:4326', 'EPSG:2272', always_xy := true)) AS wkb_2272,
                ST_Area(ST_Transform(geom_valid, 'EPSG:4326', 'EPSG:2272', always_xy := true)) AS area,
                ST_XMin(geom_valid) AS minx, ST_YMin(geom_valid) AS miny,
                ST_XMax(geom_valid) AS maxx, ST_YMax(geom_valid) AS maxy
            FROM (
                SELECT ST_MakeValid(geom) AS geom_valid, *
                FROM ST_Read('{_sql_path(raw)}')
                WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
            )
        ) TO '{_sql_path(out)}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    con.close()
    os.remove(raw)
    _step("philly", 100)


def _list_release_files(release):
    listing = cache_path(f"files_{release_key(release)}.json")
    cached = read_json(listing)
    if cached:
        return cached
    keys = []
    for part in range(6):
        prefix = urllib.parse.quote(f"overture/{release}/theme=buildings/type=building/part={part}/")
        token = None
        while True:
            url = f"{MIRROR_LIST}&prefix={prefix}&max-keys=1000"
            if token:
                url += "&continuation-token=" + urllib.parse.quote(token)
            request = urllib.request.Request(url, headers={"User-Agent": "fused-render-example"})
            with urllib.request.urlopen(request, timeout=60) as response:
                xml = response.read().decode()
            keys += re.findall(r"<Key>([^<]+)</Key>", xml)
            match = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", xml)
            if not match:
                break
            token = match.group(1)
    urls = [f"{MIRROR}/{key}" for key in keys if key.endswith(".parquet")]
    if not urls:
        raise RuntimeError(f"no parquet files found on the mirror for release {release}")
    write_json_atomic(listing, urls)
    return urls


def _fetch_release(release):
    out = cache_path(f"overture_{release_key(release)}.parquet")
    step = f"fetch:{release}"
    if os.path.exists(out):
        _step(step, 100, "cached")
        return
    urls = _list_release_files(release)
    _step(step, 5, f"{len(urls)} files")
    minx, miny, maxx, maxy = PHILLY_BOUNDS
    con = connect_duckdb_remote()
    tmp = out + ".tmp"
    con.execute(f"""
        COPY (
            SELECT
                id AS gers_id,
                ST_XMin(geom_valid) AS minx, ST_YMin(geom_valid) AS miny,
                ST_XMax(geom_valid) AS maxx, ST_YMax(geom_valid) AS maxy,
                ST_AsWKB(geom_valid) AS wkb,
                ST_AsWKB(ST_Transform(geom_valid, 'EPSG:4326', 'EPSG:2272', always_xy := true)) AS wkb_2272,
                ST_Area(ST_Transform(geom_valid, 'EPSG:4326', 'EPSG:2272', always_xy := true)) AS area
            FROM (
                SELECT id, ST_MakeValid(geometry) AS geom_valid
                FROM read_parquet($urls)
                WHERE bbox.xmin <= {maxx} AND bbox.xmax >= {minx}
                  AND bbox.ymin <= {maxy} AND bbox.ymax >= {miny}
            )
            WHERE geom_valid IS NOT NULL AND NOT ST_IsEmpty(geom_valid)
        ) TO '{_sql_path(tmp)}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """, {"urls": urls})
    con.close()
    os.replace(tmp, out)
    _step(step, 100)


def ensure_bbox_columns():
    """Older caches stored only the geometry; the viewer's per-view Overture
    query filters on plain numbers, so add the bounds if they are missing."""
    con = connect_duckdb()
    for release in RELEASES:
        path = cache_path(f"overture_{release_key(release)}.parquet")
        if not os.path.exists(path):
            continue
        columns = [row[0] for row in
                   con.execute(f"DESCRIBE SELECT * FROM read_parquet('{_sql_path(path)}')").fetchall()]
        if "minx" in columns:
            continue
        tmp = path + ".bbox.tmp"
        con.execute(f"""
            COPY (
                SELECT gers_id,
                       ST_XMin(geom) AS minx, ST_YMin(geom) AS miny,
                       ST_XMax(geom) AS maxx, ST_YMax(geom) AS maxy,
                       wkb, wkb_2272, area
                FROM (SELECT *, ST_GeomFromWKB(wkb) AS geom
                      FROM read_parquet('{_sql_path(path)}'))
            ) TO '{_sql_path(tmp)}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
        os.replace(tmp, path)
        print(f"added bbox columns to {release}", flush=True)
    con.close()


def _fetch_releases():
    pending = list(RELEASES)
    errors = []

    def run():
        while True:
            try:
                release = pending.pop(0)
            except IndexError:
                return
            try:
                _fetch_release(release)
            except Exception as error:
                errors.append((release, error))
                return

    threads = [threading.Thread(target=run, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    ticker = 0
    while any(thread.is_alive() for thread in threads):
        time.sleep(2)
        ticker += 1
        with _progress_lock:
            for step in _progress.get("steps", []):
                if step["id"].startswith("fetch:") and 0 < step["pct"] < 100:
                    step["pct"] = min(95.0, step["pct"] + 1.5)
        _publish()
    if errors:
        release, error = errors[0]
        raise RuntimeError(f"fetching {release} failed: {error}") from error


def _score_release(release):
    key = release_key(release)
    out = cache_path(f"iou_{key}.parquet")
    step = f"score:{release}"
    summary_path = cache_path(f"summary_{key}.json")
    if os.path.exists(out) and read_json(summary_path):
        _step(step, 100, "cached")
        return read_json(summary_path)
    _step(step, 10)
    con = connect_duckdb()
    con.execute(f"""
        CREATE TABLE p AS
        SELECT fid, area, ST_GeomFromWKB(wkb_2272) AS g
        FROM read_parquet('{_sql_path(cache_path("philly_base.parquet"))}')
    """)
    con.execute(f"""
        CREATE TABLE o AS
        SELECT gers_id, area, ST_GeomFromWKB(wkb_2272) AS g
        FROM read_parquet('{_sql_path(cache_path(f"overture_{key}.parquet"))}')
    """)
    overture_count = con.execute("SELECT count(*) FROM o").fetchone()[0]
    _step(step, 30)
    con.execute("""
        CREATE TABLE best AS
        SELECT fid, gers_id, iou FROM (
            SELECT fid, gers_id, iou,
                   row_number() OVER (PARTITION BY fid ORDER BY iou DESC) AS rank
            FROM (
                SELECT fid, gers_id,
                       inter / (area_p + area_o - inter) AS iou
                FROM (
                    SELECT p.fid, o.gers_id, p.area AS area_p, o.area AS area_o,
                           ST_Area(ST_Intersection(p.g, o.g)) AS inter
                    FROM p JOIN o ON ST_Intersects(p.g, o.g)
                )
            )
            WHERE iou > 0
        )
        WHERE rank = 1
    """)
    _step(step, 80)
    # Round the stored IoU so the release summary below, stats.parquet and the
    # map tiles all classify each building from the same value; otherwise a
    # footprint sitting on a band edge can be coloured on one side of it and
    # counted on the other.
    con.execute(f"""
        COPY (
            SELECT p.fid,
                   round(coalesce(best.iou, 0.0), 4) AS iou,
                   coalesce(best.gers_id, '') AS gers_id
            FROM p LEFT JOIN best USING (fid)
            ORDER BY p.fid
        ) TO '{_sql_path(out + ".tmp")}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    total, matched, mean_iou, median_iou = con.execute(f"""
        SELECT count(*),
               count(*) FILTER (iou > 0),
               coalesce(avg(iou) FILTER (iou > 0), 0),
               coalesce(median(iou) FILTER (iou > 0), 0)
        FROM read_parquet('{_sql_path(out + ".tmp")}')
    """).fetchone()
    bands = {}
    previous = None
    for name, floor in BANDS:
        upper = "" if previous is None else f" AND iou < {previous}"
        bands[name] = con.execute(
            f"SELECT count(*) FROM read_parquet('{_sql_path(out + '.tmp')}') "
            f"WHERE iou >= {floor}{upper}"
        ).fetchone()[0]
        previous = floor
    bands["missing"] = total - matched
    con.close()
    os.replace(out + ".tmp", out)
    summary = {
        "release": release,
        "philly_buildings": int(total),
        "overture_buildings": int(overture_count),
        "matched": int(matched),
        "match_rate": float(matched / total) if total else 0.0,
        "mean_iou": float(mean_iou),
        "median_iou": float(median_iou),
        "bands": {name: int(value) for name, value in bands.items()},
    }
    write_json_atomic(summary_path, summary)
    _step(step, 100)
    return summary


def _build_wide_tables():
    stats = cache_path("stats.parquet")
    detail = cache_path("detail.parquet")
    if os.path.exists(stats) and os.path.exists(detail):
        return
    con = connect_duckdb()
    iou_cols, gers_cols, joins = [], [], []
    for index, release in enumerate(RELEASES):
        key = release_key(release)
        path = _sql_path(cache_path(f"iou_{key}.parquet"))
        joins.append(f"LEFT JOIN read_parquet('{path}') r{index} USING (fid)")
        iou_cols.append(f"round(coalesce(r{index}.iou, 0), 4) AS i{index}")
        gers_cols.append(f"coalesce(r{index}.gers_id, '') AS g{index}")
    base = _sql_path(cache_path("philly_base.parquet"))
    con.execute(f"""
        COPY (
            SELECT b.fid, b.minx, b.miny, b.maxx, b.maxy, b.area,
                   {', '.join(iou_cols)}
            FROM read_parquet('{base}') b {' '.join(joins)}
        ) TO '{_sql_path(stats + ".tmp")}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    con.execute(f"""
        COPY (
            SELECT b.fid, b.objectid, b.address, b.building_name, b.height, b.area,
                   {', '.join(iou_cols)}, {', '.join(gers_cols)}
            FROM read_parquet('{base}') b {' '.join(joins)}
        ) TO '{_sql_path(detail + ".tmp")}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    con.close()
    os.replace(stats + ".tmp", stats)
    os.replace(detail + ".tmp", detail)


def _build_tiles():
    import tiler

    tiler.build_all(progress=lambda pct, note: _step("tiles", pct, note))


def _finish(summaries):
    ready = {
        "generated_at": time.time(),
        "bounds": list(PHILLY_BOUNDS),
        "releases": RELEASES,
        "summaries": summaries,
    }
    write_json_atomic(READY, ready)
    _publish({"stage": "done"})


def _sql_path(path):
    return path.replace("\\", "/").replace("'", "''")


if __name__ == "__main__":
    if "--worker" in sys.argv:
        worker()
    else:
        print(json.dumps(main(*sys.argv[1:]), indent=2))
