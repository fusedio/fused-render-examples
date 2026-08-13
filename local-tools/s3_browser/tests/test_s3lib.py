"""Credential-resolution tests for s3lib — the security-critical path.

These verify that a saved account is merged correctly and that raw keys are only
ever read from the accounts file (never required as call params).
"""
import json

import s3lib


def test_resolve_direct_fields():
    c = s3lib.resolve(profile="p", region="us-east-1", anonymous=False, endpoint_url="https://x")
    assert c["profile"] == "p"
    assert c["region"] == "us-east-1"
    assert c["anonymous"] is False
    assert c["endpoint_url"] == "https://x"
    assert c["access_key"] == "" and c["secret_key"] == ""


def test_resolve_anonymous_flag():
    assert s3lib.resolve(anonymous=True)["anonymous"] is True


def test_resolve_account_with_keys(tmp_path, monkeypatch):
    acc = {"accounts": [{"id": "x", "auth": "keys", "access_key": "AK", "secret_key": "SK",
                         "region": "us-west-1", "endpoint_url": "https://ex"}]}
    p = tmp_path / "accounts.json"
    p.write_text(json.dumps(acc), encoding="utf-8")
    monkeypatch.setattr(s3lib, "ACCOUNTS_PATH", str(p))
    c = s3lib.resolve(account_id="x")
    assert c["access_key"] == "AK" and c["secret_key"] == "SK"
    assert c["region"] == "us-west-1" and c["endpoint_url"] == "https://ex"


def test_resolve_account_anonymous_auth(tmp_path, monkeypatch):
    acc = {"accounts": [{"id": "y", "auth": "anonymous", "region": "us-east-1"}]}
    p = tmp_path / "accounts.json"
    p.write_text(json.dumps(acc), encoding="utf-8")
    monkeypatch.setattr(s3lib, "ACCOUNTS_PATH", str(p))
    c = s3lib.resolve(account_id="y")
    assert c["anonymous"] is True and c["region"] == "us-east-1"


def test_resolve_explicit_region_overrides_account(tmp_path, monkeypatch):
    acc = {"accounts": [{"id": "z", "auth": "profile", "profile": "me", "region": "us-west-1"}]}
    p = tmp_path / "accounts.json"
    p.write_text(json.dumps(acc), encoding="utf-8")
    monkeypatch.setattr(s3lib, "ACCOUNTS_PATH", str(p))
    c = s3lib.resolve(account_id="z", region="eu-west-1")
    assert c["region"] == "eu-west-1" and c["profile"] == "me"


def test_resolve_missing_account_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(s3lib, "ACCOUNTS_PATH", str(tmp_path / "nope.json"))
    c = s3lib.resolve(account_id="does-not-exist", profile="fallback")
    assert c["profile"] == "fallback" and c["access_key"] == ""
