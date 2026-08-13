"""Make the sibling s3.py importable and expose shared test fixtures."""
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
sys.path.insert(0, os.path.dirname(_here))

import pytest  # noqa: E402

# A public AWS Open Data bucket used for read-only tests — anonymous listing and
# HEAD both work here, so no credentials are needed.
PUBLIC_BUCKET = "overturemaps-us-west-2"
PUBLIC_REGION = "us-west-2"


@pytest.fixture
def public():
    """Connection kwargs for the anonymous public-bucket read tests."""
    return {"bucket": PUBLIC_BUCKET, "region": PUBLIC_REGION, "anonymous": True}


@pytest.fixture
def writable():
    """Connection kwargs for a bucket the runner owns and can mutate.

    Set S3_TEST_BUCKET (and optionally S3_TEST_REGION / AWS_PROFILE) to run the
    write-op tests against your own bucket; otherwise they skip.
    """
    bucket = os.environ.get("S3_TEST_BUCKET")
    if not bucket:
        pytest.skip("set S3_TEST_BUCKET to run write-op tests against your own bucket")
    return {
        "bucket": bucket,
        "region": os.environ.get("S3_TEST_REGION", ""),
        "profile": os.environ.get("AWS_PROFILE", ""),
    }
