"""The unit tests never reach a real S3 store: the S3 tests run against moto or a local fake server."""

import os

import pytest


@pytest.fixture(autouse=True)
def no_aws_profile(monkeypatch):
    """No AWS profile, credentials or S3 endpoint of the machine's own (~/.aws, AWS_PROFILE, AWS_ENDPOINT_URL_S3):
    they would send the requests of these tests to a real store. Each test sets what it needs."""
    for name in ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"):
        monkeypatch.setenv(name, os.devnull)
    for name in ("AWS_PROFILE", "AWS_ENDPOINT_URL_S3"):
        monkeypatch.delenv(name, raising=False)
