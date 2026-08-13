"""Tests for preview.py's localize behavior, read-only against the public bucket."""
import os

import preview
import s3

_PREFIX = ("changelog/2024-06-13-beta.0/theme=admins/type=administrative_boundary/"
           "change_type=added/")


def _first_key(public):
    listing = s3.main(action="list_objects", prefix=_PREFIX, delimiter="",
                      max_keys=1, **public)
    return listing["files"][0]["key"]


def test_localize_parquet_public(public):
    key = _first_key(public)
    res = preview.main(key=key, **public)
    assert "local_path" in res
    assert os.path.exists(res["local_path"])
    assert os.path.getsize(res["local_path"]) == res["size"]


def test_localize_too_large(public):
    key = _first_key(public)
    res = preview.main(key=key, max_bytes=1, force=False, **public)
    assert res["too_large"] is True
