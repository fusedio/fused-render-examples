"""Object preview for the S3 Browser.

Peeks at an object without downloading the whole thing. Text/CSV read a bounded
prefix via a ranged GET; Parquet reads only the footer (schema) plus a
column-projected slice of the first row group through a seekable range reader —
so a 500 MB file previews by pulling a few MB, not 500. All auth modes work
because every byte comes through the botocore client (which s3.py builds), not
through a filesystem layer with its own credential rules.

Returns one of:
  {"kind": "table",  "columns": [...], "rows": [[...]], "note": str|None}
  {"kind": "text",   "text": str, "truncated": bool}
  {"kind": "image",  "data_uri": str}            # small images, inlined
  {"kind": "unsupported", "reason": str, "size": int}
Errors use the same envelope as s3.py: {"error": {code, message, http_status}}.
"""
# /// script
# requires-python = ">=3.12"
# dependencies = ["botocore", "pandas", "pyarrow"]
# ///
import base64
import io

import s3lib

TEXT_EXT = {"txt", "log", "md", "json", "yaml", "yml", "xml", "html", "csv",
            "tsv", "geojson", "ndjson", "py", "js", "css", "sql", "sh", "toml", "ini", "cfg"}
IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "ico"}
IMAGE_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
              "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
              "svg": "image/svg+xml", "ico": "image/x-icon"}

TEXT_BYTES = 256 * 1024
IMAGE_BYTES = 3 * 1024 * 1024
ROWGROUP_LIMIT = 96 * 1024 * 1024  # skip row preview for huge first row groups


class _S3RangeReader(io.RawIOBase):
    """A seekable file-like backed by ranged GETs — enough for pyarrow."""

    def __init__(self, client, bucket, key, size):
        self._c, self._b, self._k, self._size, self._pos = client, bucket, key, size, 0

    def seek(self, off, whence=0):
        self._pos = off if whence == 0 else (self._pos + off if whence == 1 else self._size + off)
        return self._pos

    def tell(self):
        return self._pos

    def readable(self):
        return True

    def seekable(self):
        return True

    def read(self, n=-1):
        end = self._size - 1 if (n is None or n < 0) else min(self._pos + n, self._size) - 1
        if self._pos > end:
            return b""
        r = self._c.get_object(Bucket=self._b, Key=self._k, Range=f"bytes={self._pos}-{end}")
        data = r["Body"].read()
        self._pos += len(data)
        return data


def _ranged_bytes(client, bucket, key, n):
    r = client.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{n - 1}")
    return r["Body"].read()


def _json_safe(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def _preview_parquet(client, bucket, key, size, rows):
    import pyarrow.parquet as pq

    reader = _S3RangeReader(client, bucket, key, size)
    pf = pq.ParquetFile(reader)
    schema = pf.schema_arrow
    columns = [f.name for f in schema]
    meta = pf.metadata
    note = None
    data_rows = []
    if meta.num_row_groups and meta.row_group(0).total_byte_size <= ROWGROUP_LIMIT:
        proj = columns[:12]
        tbl = pf.read_row_group(0, columns=proj).slice(0, rows)
        recs = tbl.to_pylist()
        data_rows = [[_json_safe(rec.get(c)) for c in proj] for rec in recs]
        columns = proj
        if len(proj) < len(schema.names):
            note = f"showing {len(proj)} of {len(schema.names)} columns"
    else:
        note = "first row group too large to preview rows — showing schema only"
    return {
        "kind": "table",
        "columns": columns,
        "rows": data_rows,
        "num_rows": meta.num_rows,
        "num_columns": len(schema.names),
        "note": note,
    }


def _preview_csv(text, sep, rows):
    import pandas as pd

    if not text.endswith("\n") and "\n" in text:
        text = text[: text.rfind("\n")]  # drop the truncated final line
    df = pd.read_csv(io.StringIO(text), sep=sep, nrows=rows)
    return {
        "kind": "table",
        "columns": [str(c) for c in df.columns],
        "rows": [[_json_safe(v) for v in row] for row in df.itertuples(index=False, name=None)],
        "num_rows": None,
        "num_columns": len(df.columns),
        "note": "preview of first rows",
    }


def main(bucket: str = "", key: str = "", account_id: str = "", profile: str = "",
         region: str = "", anonymous: bool = False, endpoint_url: str = "", rows: int = 50):
    ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
    try:
        client = s3lib.client(s3lib.resolve(account_id, profile, region, anonymous, endpoint_url))
        head = client.head_object(Bucket=bucket, Key=key)
        size = head.get("ContentLength", 0)

        if ext == "parquet":
            return _preview_parquet(client, bucket, key, size, rows)

        if ext in IMAGE_EXT:
            if size > IMAGE_BYTES:
                return {"kind": "unsupported", "reason": "image too large to inline", "size": size}
            data = client.get_object(Bucket=bucket, Key=key)["Body"].read()
            mime = IMAGE_MIME.get(ext, "application/octet-stream")
            return {"kind": "image", "data_uri": f"data:{mime};base64," + base64.b64encode(data).decode()}

        if ext in TEXT_EXT or size <= TEXT_BYTES:
            raw = _ranged_bytes(client, bucket, key, min(size, TEXT_BYTES)) if size else b""
            text = raw.decode("utf-8", errors="replace")
            if ext in ("csv", "tsv"):
                return _preview_csv(text, "," if ext == "csv" else "\t", rows)
            return {"kind": "text", "text": text, "truncated": size > TEXT_BYTES}

        return {"kind": "unsupported", "reason": f".{ext or '(none)'} not previewable", "size": size}
    except Exception as e:  # noqa: BLE001
        if s3lib.is_botocore_error(e):
            return s3lib.envelope(e)
        raise
