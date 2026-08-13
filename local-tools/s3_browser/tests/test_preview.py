"""Tests for preview.py. Parquet runs against the public bucket; text/CSV/image
round-trips need a writable bucket (S3_TEST_BUCKET) and otherwise skip."""
import base64
import uuid

import preview
import s3

# A 1x1 transparent PNG.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def test_preview_parquet_public(public):
    listing = s3.main(action="list_objects",
                      prefix="release/2026-07-22.0/theme=divisions/type=division/",
                      delimiter="", max_keys=1, **public)
    key = listing["files"][0]["key"]
    res = preview.main(key=key, rows=3, **public)
    assert res["kind"] == "table"
    assert "id" in res["columns"]
    assert len(res["rows"]) <= 3
    assert res["num_rows"] and res["num_rows"] > 0


def test_preview_csv_roundtrip(writable):
    key = "s3browser-test/preview-%s.csv" % uuid.uuid4().hex[:8]
    s3.main(action="upload", key=key, content_type="text/csv",
            content_b64=base64.b64encode(b"a,b,c\n1,2,3\n4,5,6\n").decode(), **writable)
    res = preview.main(key=key, rows=10, **writable)
    assert res["kind"] == "table"
    assert res["columns"] == ["a", "b", "c"]
    assert res["rows"][0][0] in (1, "1")
    s3.main(action="delete_objects", keys='["%s"]' % key, **writable)


def test_preview_text_roundtrip(writable):
    key = "s3browser-test/preview-%s.txt" % uuid.uuid4().hex[:8]
    s3.main(action="upload", key=key, content_type="text/plain",
            content_b64=base64.b64encode(b"hello\nworld\n").decode(), **writable)
    res = preview.main(key=key, **writable)
    assert res["kind"] == "text" and "hello" in res["text"]
    s3.main(action="delete_objects", keys='["%s"]' % key, **writable)


def test_preview_image_roundtrip(writable):
    key = "s3browser-test/preview-%s.png" % uuid.uuid4().hex[:8]
    s3.main(action="upload", key=key, content_type="image/png",
            content_b64=base64.b64encode(_PNG).decode(), **writable)
    res = preview.main(key=key, **writable)
    assert res["kind"] == "image"
    assert res["data_uri"].startswith("data:image/png;base64,")
    s3.main(action="delete_objects", keys='["%s"]' % key, **writable)
