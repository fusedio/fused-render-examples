"""Backend tests for the S3 Browser dispatcher.

Read-op tests run against a public AWS Open Data bucket with no credentials.
Write-op tests use the `writable` fixture and skip unless S3_TEST_BUCKET is set.
"""
import urllib.parse
import uuid

import pytest
import requests

import s3


# ---- connection & errors -------------------------------------------------

def test_unknown_action_returns_envelope():
    res = s3.main(action="nope")
    assert res["error"]["code"] == "UnknownAction"


def test_nonexistent_bucket_returns_error_envelope():
    res = s3.main(action="list_objects", bucket="no-such-bucket-" + uuid.uuid4().hex,
                  region="us-east-1", anonymous=True)
    assert "error" in res
    assert res["error"]["code"] in ("NoSuchBucket", "AccessDenied", "404", "403")


# ---- listing -------------------------------------------------------------

def test_list_objects_top_level(public):
    res = s3.main(action="list_objects", **public)
    assert "error" not in res
    names = {f["name"] for f in res["folders"]}
    assert "release" in names
    assert res["delimiter"] == "/"


def test_list_objects_pagination(public):
    first = s3.main(action="list_objects", prefix="release/", max_keys=1,
                    delimiter="", **public)
    assert first["is_truncated"] is True
    assert first["next_token"]
    second = s3.main(action="list_objects", prefix="release/", max_keys=1,
                     delimiter="", token=first["next_token"], **public)
    assert "error" not in second
    if first["files"] and second["files"]:
        assert first["files"][0]["key"] != second["files"][0]["key"]


def test_list_keys_recursive(public):
    res = s3.main(action="list_keys",
                  prefix="release/2026-07-22.0/theme=divisions/type=division/", **public)
    assert "error" not in res
    assert res["keys"] and all(k.startswith("release/") for k in res["keys"])


def test_download_recursive_folder(tmp_path):
    import os

    import download

    dest = str(tmp_path / "dl")
    prefixes = ('["changelog/2024-06-13-beta.0/theme=admins/type=administrative_boundary/'
                'change_type=added/"]')
    plan = download.main(action="plan", bucket="overturemaps-us-west-2", region="us-west-2",
                         anonymous=True, prefixes=prefixes, dest_dir=dest)
    assert plan["total_files"] >= 1
    s = download.main(action="step", job=plan["job"])
    while s["status"] == "running":
        s = download.main(action="step", job=plan["job"])
    assert s["status"] == "done"
    assert s["done_files"] == plan["total_files"]
    assert not s["errors"]
    on_disk = sum(len(f) for _, _, f in os.walk(dest))
    assert on_disk == plan["total_files"]


def test_download_path_stays_inside_dest(tmp_path):
    import os

    import download

    dest = str(tmp_path / "d")
    os.makedirs(dest, exist_ok=True)
    dest_real = os.path.realpath(dest)
    for key in ("a/b/c.txt", "../../evil.txt", "..\\..\\evil.txt", "/etc/passwd", "x/../../../y.txt"):
        local = download._safe_local(dest, key)
        assert os.path.commonpath([dest_real, os.path.realpath(local)]) == dest_real


def test_list_objects_flat_mode_has_no_folders(public):
    res = s3.main(action="list_objects", prefix="release/", delimiter="",
                  max_keys=5, **public)
    assert res["folders"] == []
    assert res["files"]


def _first_file(public, prefix="release/"):
    res = s3.main(action="list_objects", prefix=prefix, delimiter="",
                  max_keys=5, **public)
    return res["files"][0]


# ---- head ----------------------------------------------------------------

def test_head_object_matches_listing(public):
    f = _first_file(public)
    head = s3.main(action="head_object", key=f["key"], **public)
    assert "error" not in head
    assert head["size"] == f["size"]
    assert head["etag"] == f["etag"]


# ---- presign -------------------------------------------------------------

def test_presign_anonymous_url_is_fetchable(public):
    f = _first_file(public)
    res = s3.main(action="presign", key=f["key"], method="get", **public)
    assert res["signed"] is False
    assert f["key"].split("/")[-1] in res["url"]   # key is URL-encoded in the path
    r = requests.get(res["url"], headers={"Range": "bytes=0-0"}, timeout=30)
    assert r.status_code in (200, 206)              # S3 decodes %XX back to the real key


def test_presign_anonymous_uses_endpoint_url():
    # S3-compatible public store: the link must use the endpoint (path-style),
    # not a bucket.s3.<region>.amazonaws.com host.
    res = s3.main(action="presign", bucket="b", key="a/b.txt", method="get",
                  anonymous=True, endpoint_url="https://s3.us-west-1.wasabisys.com")
    assert res["signed"] is False
    assert res["url"].startswith("https://s3.us-west-1.wasabisys.com/b/a/b.txt")


def test_expand_terminates_without_continuation_token():
    # A non-conforming endpoint can report IsTruncated with no NextContinuationToken;
    # _expand must stop instead of re-reading page 1 forever.
    import download

    class Stub:
        def list_objects_v2(self, **kw):
            return {"Contents": [{"Key": "x/1", "Size": 1}], "IsTruncated": True}

    items = download._expand(Stub(), "b", [], ["x/"])
    assert len(items) == 1


def test_bucket_region(public):
    res = s3.main(action="bucket_region", **public)
    assert res["region"] == "us-west-2"


def test_bucket_info_gathers_properties_independently(public):
    res = s3.main(action="bucket_info", **public)
    assert res["region"] == "us-west-2"
    # each sub-property resolves to a value/none/error dict, never a hard failure
    for k in ("versioning", "encryption", "public_access_block"):
        assert isinstance(res[k], dict)


def test_security_scan_runs(public):
    res = s3.main(action="security_scan", **public)
    assert "error" not in res
    assert isinstance(res["public"], bool)
    assert res["findings"] and all("level" in f and "title" in f for f in res["findings"])


def test_set_versioning_rejects_invalid():
    res = s3.main(action="set_versioning", bucket="x", status="Bogus", anonymous=True)
    assert res["error"]["code"] == "InvalidVersioning"


def test_bucket_config_rejects_bad_type():
    for act, extra in [("get_bucket_config", {}), ("put_bucket_config", {"config": "{}"}),
                       ("delete_bucket_config", {})]:
        res = s3.main(action=act, bucket="x", config_type="bogus", anonymous=True, **extra)
        assert res["error"]["code"] == "BadConfigType"


def test_presign_anonymous_version_url(public):
    res = s3.main(action="presign", key="some/key", method="get", version_id="V1", **public)
    assert res["signed"] is False
    assert "versionId=V1" in res["url"]


def test_presign_anonymous_encodes_key(public):
    res = s3.main(action="presign", key="data/my file?v2.csv", method="get", **public)
    assert " " not in res["url"]
    assert "my%20file" in res["url"] and "%3F" in res["url"]


def test_presign_signed_is_sigv4():
    from botocore.session import Session

    if "default" not in Session().available_profiles or Session(profile="default").get_credentials() is None:
        pytest.skip("no default AWS credentials for the signed-presign test")
    res = s3.main(action="presign", profile="default", region="us-east-1",
                  bucket="example-bucket", key="a/b.txt", method="get", expires=3600)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(res["url"]).query))
    assert res["signed"] is True
    assert "X-Amz-Signature" in q          # SigV4, not the deprecated SigV2
    assert q["X-Amz-Expires"] == "3600"


def test_presign_signed_with_version_id():
    from botocore.session import Session

    if "default" not in Session().available_profiles or Session(profile="default").get_credentials() is None:
        pytest.skip("no default AWS credentials for the signed-presign test")
    res = s3.main(action="presign", profile="default", region="us-east-1",
                  bucket="example-bucket", key="a/b.txt", method="get",
                  version_id="VER123", expires=600)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(res["url"]).query))
    assert q.get("versionId") == "VER123"


# ---- write ops (own bucket) ----------------------------------------------

def test_create_and_delete_folder(writable):
    name = "s3browser-test-" + uuid.uuid4().hex[:8]
    created = s3.main(action="create_folder", prefix="", name=name, **writable)
    assert created["created"] == name + "/"
    deleted = s3.main(action="delete_objects", keys='["%s/"]' % name, **writable)
    assert name + "/" in deleted["deleted"]


def test_upload_head_delete_roundtrip(writable):
    import base64
    key = "s3browser-test/%s.txt" % uuid.uuid4().hex[:8]
    payload = b"hello from the s3 browser test suite"
    up = s3.main(action="upload", key=key,
                 content_b64=base64.b64encode(payload).decode(),
                 content_type="text/plain", **writable)
    assert up["size"] == len(payload)
    head = s3.main(action="head_object", key=key, **writable)
    assert head["size"] == len(payload)
    assert head["content_type"] == "text/plain"
    s3.main(action="delete_objects", keys='["%s"]' % key, **writable)


def test_change_storage_class_rejects_invalid():
    res = s3.main(action="change_storage_class", bucket="x", key="y",
                  storage_class="BOGUS", anonymous=True)
    assert res["error"]["code"] == "InvalidStorageClass"


def test_change_storage_class(writable):
    import base64
    key = "s3browser-test/%s.txt" % uuid.uuid4().hex[:8]
    s3.main(action="upload", key=key, content_b64=base64.b64encode(b"x").decode(), **writable)
    res = s3.main(action="change_storage_class", key=key, storage_class="STANDARD_IA", **writable)
    assert res.get("storage_class") == "STANDARD_IA"
    head = s3.main(action="head_object", key=key, **writable)
    assert head["storage_class"] == "STANDARD_IA"
    s3.main(action="delete_objects", keys='["%s"]' % key, **writable)


def test_upload_unknown_action_returns_envelope():
    import upload

    res = upload.main(action="nope", bucket="x", anonymous=True)
    assert res["error"]["code"] == "UnknownAction"


def test_upload_missing_job_returns_envelope():
    import upload

    for act in ("part", "complete"):
        res = upload.main(action=act, job="does-not-exist", part_number=1, anonymous=True)
        assert res["error"]["code"] == "NoJob"


def test_multipart_upload_roundtrip(writable):
    import base64

    import upload

    key = "s3browser-test/mp-%s.bin" % uuid.uuid4().hex[:8]
    part1, part2 = b"A" * (5 * 1024 * 1024), b"B" * (1024 * 1024)   # 5 MB + 1 MB last part
    start = upload.main(action="start", key=key, content_type="application/octet-stream", **writable)
    job = start["job"]
    upload.main(action="part", job=job, part_number=1, content_b64=base64.b64encode(part1).decode(), **writable)
    r2 = upload.main(action="part", job=job, part_number=2, content_b64=base64.b64encode(part2).decode(), **writable)
    assert r2["done_parts"] == 2
    done = upload.main(action="complete", job=job, **writable)
    assert done["parts"] == 2
    head = s3.main(action="head_object", key=key, **writable)
    assert head["size"] == len(part1) + len(part2)
    s3.main(action="delete_objects", keys='["%s"]' % key, **writable)


def test_rename_object(writable):
    import base64
    src = "s3browser-test/%s-a.txt" % uuid.uuid4().hex[:8]
    dst = src.replace("-a.txt", "-b.txt")
    s3.main(action="upload", key=src, content_b64=base64.b64encode(b"x").decode(),
            **writable)
    res = s3.main(action="rename_object", key=dst, src_key=src, **writable)
    assert res["renamed"] == dst
    assert "error" not in s3.main(action="head_object", key=dst, **writable)
    gone = s3.main(action="head_object", key=src, **writable)
    assert "error" in gone
    s3.main(action="delete_objects", keys='["%s"]' % dst, **writable)
