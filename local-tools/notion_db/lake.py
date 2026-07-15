"""Lightweight parquet-snapshot docdb ("duckLake").

Each table is a folder under lake/ holding full-state parquet snapshots.
Every write produces a new timestamp-named snapshot; nothing is mutated
or deleted on disk. The current state is the lexicographically-largest
filename.

Parquet I/O prefers pyarrow and falls back to duckdb: the FusedRender
runtime that serves the UI bundles pyarrow but a broken duckdb (its
native module fails to import), while a typical local python has duckdb
but not pyarrow. Both backends produce plain parquet files, so the UI
and local tools (lakectl.py, Claude, scripts) can share one lake. Since
every read targets a single snapshot file, no cross-file schema
reconciliation (union_by_name) is needed — each snapshot already
carries the union of columns at its write time.
"""
import datetime
import json
import math
import os
import sys
import uuid

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    _BACKEND = "pyarrow"
except ImportError:
    import duckdb
    _BACKEND = "duckdb"

# The fused-render runner (app >= Jul 2026) strips __file__ from module
# globals, so resolve this file's directory defensively.
_HERE = (os.path.dirname(os.path.abspath(__file__))
         if "__file__" in globals() else os.path.abspath(sys.path[0]))
LAKE_DIR = os.path.join(_HERE, "lake")

RESERVED_COLUMNS = ("id", "body")


def _table_dir(table: str) -> str:
    if not table or "/" in table or table.startswith("."):
        raise ValueError(f"invalid table name: {table!r}")
    return os.path.join(LAKE_DIR, table)


def _timestamp() -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y%m%dT%H%M%S%fZ")


def _snapshot_files(table: str) -> list[str]:
    d = _table_dir(table)
    if not os.path.isdir(d):
        raise FileNotFoundError(f"no such table: {table}")
    return sorted(f for f in os.listdir(d) if f.endswith(".parquet"))


def _jsonable(v):
    if v is None or isinstance(v, (str, int, bool)):
        return v
    if isinstance(v, float):
        return None if math.isnan(v) else v
    return str(v)  # UUID, datetime, etc.


def _clean(rows: list[dict]) -> list[dict]:
    return [{k: _jsonable(v) for k, v in row.items()} for row in rows]


def _read_parquet(path: str) -> list[dict]:
    if _BACKEND == "pyarrow":
        rows = pq.read_table(path).to_pylist()
    else:
        rel = duckdb.sql(f"SELECT * FROM read_parquet('{path}')")
        cols = rel.columns
        rows = [dict(zip(cols, r)) for r in rel.fetchall()]
    return _clean(rows)


def _write_parquet(path: str, rows: list[dict]) -> None:
    if not rows:
        if _BACKEND == "pyarrow":
            pq.write_table(pa.table({"id": pa.array([], type=pa.string())}), path)
        else:
            duckdb.sql(f"COPY (SELECT CAST(NULL AS VARCHAR) AS id WHERE false) TO '{path}' (FORMAT parquet)")
        return
    # Give every row the union of keys so the writer sees a consistent schema.
    cols = []
    for row in rows:
        for k in row:
            if k not in cols:
                cols.append(k)
    normalized = [{c: row.get(c) for c in cols} for row in rows]
    if _BACKEND == "pyarrow":
        pq.write_table(pa.Table.from_pylist(normalized), path)
    else:
        tmp = path + ".json"
        with open(tmp, "w") as fh:
            json.dump(normalized, fh)
        try:
            duckdb.sql(f"COPY (SELECT * FROM read_json('{tmp}', format='array')) TO '{path}' (FORMAT parquet)")
        finally:
            os.remove(tmp)


def list_tables() -> list[str]:
    if not os.path.isdir(LAKE_DIR):
        return []
    return sorted(
        name for name in os.listdir(LAKE_DIR)
        if os.path.isdir(os.path.join(LAKE_DIR, name))
    )


def create_table(table: str) -> None:
    d = _table_dir(table)
    if os.path.isdir(d):
        raise FileExistsError(f"table already exists: {table}")
    os.makedirs(d)
    _write_parquet(os.path.join(d, f"{_timestamp()}.parquet"), [])


def rename_table(table: str, new_name: str) -> None:
    src = _table_dir(table)
    dst = _table_dir(new_name)
    if not os.path.isdir(src):
        raise FileNotFoundError(f"no such table: {table}")
    if os.path.isdir(dst):
        raise FileExistsError(f"table already exists: {new_name}")
    os.rename(src, dst)


def latest(table: str) -> list[dict]:
    files = _snapshot_files(table)
    if not files:
        return []
    return _read_parquet(os.path.join(_table_dir(table), files[-1]))


def write_snapshot(table: str, rows: list[dict]) -> str:
    d = _table_dir(table)
    if not os.path.isdir(d):
        raise FileNotFoundError(f"no such table: {table}")
    filename = f"{_timestamp()}.parquet"
    _write_parquet(os.path.join(d, filename), rows)
    return filename


def create_row(table: str, properties: dict) -> dict:
    rows = latest(table)
    row = dict(properties)
    row["id"] = str(uuid.uuid4())
    rows.append(row)
    write_snapshot(table, rows)
    return row


def update_row(table: str, row_id: str, properties: dict) -> dict:
    rows = latest(table)
    target = None
    for row in rows:
        if row.get("id") == row_id:
            row.update(properties)
            target = row
            break
    if target is None:
        raise KeyError(f"no row {row_id!r} in table {table!r}")
    write_snapshot(table, rows)
    return target


def delete_row(table: str, row_id: str) -> None:
    rows = latest(table)
    remaining = [r for r in rows if r.get("id") != row_id]
    if len(remaining) == len(rows):
        raise KeyError(f"no row {row_id!r} in table {table!r}")
    write_snapshot(table, remaining)


def bulk_create(table: str, rows: list[dict]) -> list[dict]:
    """Insert many rows in ONE snapshot. Each dict is a property map; ids are
    assigned. Returns the created rows."""
    current = latest(table)
    created = []
    for props in rows:
        row = dict(props)
        row["id"] = str(uuid.uuid4())
        created.append(row)
    write_snapshot(table, current + created)
    return created


def bulk_delete(table: str, ids: list[str]) -> int:
    """Remove many rows in ONE snapshot. Returns how many were removed."""
    rows = latest(table)
    drop = set(ids)
    remaining = [r for r in rows if r.get("id") not in drop]
    write_snapshot(table, remaining)
    return len(rows) - len(remaining)


def rename_column(table: str, old: str, new: str) -> None:
    if old in RESERVED_COLUMNS:
        raise ValueError(f"cannot rename reserved column {old!r}")
    rows = latest(table)
    for r in rows:
        if old in r:
            r[new] = r.pop(old)
    write_snapshot(table, rows)


def drop_column(table: str, column: str) -> None:
    if column in RESERVED_COLUMNS:
        raise ValueError(f"cannot drop reserved column {column!r}")
    rows = latest(table)
    for r in rows:
        r.pop(column, None)
    write_snapshot(table, rows)


def set_column(table: str, column: str, value) -> None:
    """Add a column (or overwrite it) with the same value on every row."""
    rows = latest(table)
    for r in rows:
        r[column] = value
    write_snapshot(table, rows)


def reorder_rows(table: str, ids: list[str]) -> list[dict]:
    """Rewrite the snapshot with rows ordered per `ids`; row order in the
    parquet file is the persisted order. Unlisted rows keep their relative
    order at the end."""
    rows = latest(table)
    pos = {row_id: i for i, row_id in enumerate(ids)}
    rows.sort(key=lambda r: pos.get(r.get("id"), len(pos)))
    write_snapshot(table, rows)
    return rows


def history(table: str) -> list[dict]:
    entries = []
    for f in reversed(_snapshot_files(table)):
        ts = f[: -len(".parquet")]
        entries.append({"filename": f, "timestamp": ts})
    return entries


def snapshot_at(table: str, filename: str) -> list[dict]:
    if "/" in filename or not filename.endswith(".parquet"):
        raise ValueError(f"invalid snapshot filename: {filename!r}")
    path = os.path.join(_table_dir(table), filename)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"no snapshot {filename!r} in table {table!r}")
    return _read_parquet(path)
