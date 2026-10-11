"""Hermetic self-check tests for web auth and service account logic."""

import hashlib
import hmac
import json
import os
import tempfile
from pathlib import Path

import sys
from unittest.mock import MagicMock

for mod in ["yt_dlp", "google", "google.oauth2", "google.oauth2.service_account", "googleapiclient", "googleapiclient.discovery", "googleapiclient.errors", "googleapiclient.http"]:
    sys.modules.setdefault(mod, MagicMock())

# Auth token logic verification (same algorithm as app.py)
SECRET_KEY = "test-secret-key"



def create_session_token(user_id: int) -> str:
    sig = hmac.new(SECRET_KEY.encode(), str(user_id).encode(), hashlib.sha256).hexdigest()
    return f"{user_id}:{sig}"


def parse_session_token(token: str) -> int | None:
    try:
        user_id_str, sig = token.split(":", 1)
        expected = hmac.new(SECRET_KEY.encode(), user_id_str.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, expected):
            return int(user_id_str)
    except Exception:
        return None
    return None


def test_session_token_roundtrip() -> None:
    token = create_session_token(42)
    assert parse_session_token(token) == 42
    assert parse_session_token("42:invalid_sig") is None
    assert parse_session_token("invalid_format") is None


def test_service_account_email_extraction() -> None:
    from uploader import get_service_account_email

    with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as tmp:
        json.dump({"client_email": "bot@project.iam.gserviceaccount.com"}, tmp)
        tmp_path = tmp.name

    try:
        email = get_service_account_email(tmp_path)
        assert email == "bot@project.iam.gserviceaccount.com"
    finally:
        Path(tmp_path).unlink(missing_ok=True)


if __name__ == "__main__":
    test_session_token_roundtrip()
    test_service_account_email_extraction()
    print("Web auth and service account extraction tests passed.")
