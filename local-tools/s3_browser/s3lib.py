"""Shared S3 plumbing: credential resolution, client construction, error envelope.

Both s3.py and preview.py build clients the same way, so the logic lives here.

Credential rule: a request may carry a saved-account id (`account_id`) OR the
non-secret connection fields directly (`profile`/`region`/`anonymous`/`endpoint`).
Raw access keys are NEVER passed as call params — they would land in the call
log. Instead they live in a git-ignored `accounts.json` next to this file, and
the backend reads them by id here. So the page passes only an id; the secret
never leaves the disk except into the botocore client.
"""
import json
import os

# Guard __file__ so the module also resolves when exec'd without it set.
HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
ACCOUNTS_PATH = os.path.join(HERE, "accounts.json")


def load_account(account_id: str):
    if not account_id:
        return None
    try:
        with open(ACCOUNTS_PATH, encoding="utf-8") as f:
            for a in json.load(f).get("accounts", []):
                if a.get("id") == account_id:
                    return a
    except (FileNotFoundError, ValueError):
        return None
    return None


def resolve(account_id="", profile="", region="", anonymous=False, endpoint_url="",
            access_key="", secret_key="", session_token=""):
    """Merge a saved account (if any) with explicit overrides into one conn dict.

    Explicit params win over the stored account (so you can browse a different
    region/bucket under the same credentials)."""
    a = load_account(account_id)
    if a:
        auth = a.get("auth", "profile")
        profile = profile or a.get("profile", "")
        endpoint_url = endpoint_url or a.get("endpoint_url", "")
        region = region or a.get("region", "")
        if auth == "anonymous":
            anonymous = True
        elif auth == "keys":
            access_key = access_key or a.get("access_key", "")
            secret_key = secret_key or a.get("secret_key", "")
            session_token = session_token or a.get("session_token", "")
    return {
        "profile": profile,
        "region": region,
        "anonymous": bool(anonymous),
        "endpoint_url": endpoint_url,
        "access_key": access_key,
        "secret_key": secret_key,
        "session_token": session_token,
    }


def client(conn):
    from botocore import UNSIGNED
    from botocore.config import Config
    from botocore.session import Session

    cfg = {"retries": {"max_attempts": 3, "mode": "standard"}}
    if conn["region"]:
        cfg["region_name"] = conn["region"]
    # SigV4 for signed clients so presigned URLs work in every region (the
    # legacy SigV2 default for us-east-1 is deprecated); UNSIGNED for public.
    cfg["signature_version"] = UNSIGNED if conn["anonymous"] else "s3v4"
    if conn["endpoint_url"]:
        cfg["s3"] = {"addressing_style": "path"}
    session = Session(profile=conn["profile"] or None)
    kw = {"config": Config(**cfg), "endpoint_url": conn["endpoint_url"] or None}
    if conn["access_key"] and conn["secret_key"]:
        kw["aws_access_key_id"] = conn["access_key"]
        kw["aws_secret_access_key"] = conn["secret_key"]
        if conn["session_token"]:
            kw["aws_session_token"] = conn["session_token"]
    return session.create_client("s3", **kw)


def envelope(e):
    from botocore.exceptions import ClientError

    if isinstance(e, ClientError):
        r = e.response
        return {"error": {
            "code": r.get("Error", {}).get("Code"),
            "message": r.get("Error", {}).get("Message"),
            "http_status": r.get("ResponseMetadata", {}).get("HTTPStatusCode"),
        }}
    return {"error": {"code": type(e).__name__, "message": str(e)}}


def is_botocore_error(e):
    from botocore.exceptions import BotoCoreError, ClientError

    return isinstance(e, (ClientError, BotoCoreError))


def available_profiles():
    from botocore.session import Session

    try:
        return sorted(Session().available_profiles)
    except Exception:
        return []
