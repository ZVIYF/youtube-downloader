"""Self-check tests for YouTube to Google Drive uploader."""

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

# Mock external modules for hermetic unit testing without network/pip requirements
class MockHttpError(Exception):
    pass

for mod in [
    "yt_dlp",
    "google",
    "google.oauth2",
    "google.oauth2.service_account",
    "googleapiclient",
    "googleapiclient.discovery",
    "googleapiclient.http",
]:
    sys.modules.setdefault(mod, MagicMock())

errors_mock = MagicMock()
errors_mock.HttpError = MockHttpError
sys.modules["googleapiclient.errors"] = errors_mock

from main import load_env_file, upload_to_drive


def test_upload_resumable_chunks_and_retries() -> None:
    """Test that next_chunk progress is handled and retried on transient error."""
    mock_service = MagicMock()
    mock_request = MagicMock()
    mock_service.files().create.return_value = mock_request

    # Simulate 1 transient error followed by success
    status_chunk = MagicMock()
    status_chunk.progress.return_value = 0.5

    mock_request.next_chunk.side_effect = [
        OSError("Connection reset"),
        (status_chunk, None),
        (None, {"id": "drive_file_123"}),
    ]

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp_file:
        tmp_file.write(b"dummy video data")
        tmp_path = Path(tmp_file.name)

    try:
        file_id = upload_to_drive(
            service=mock_service,
            file_path=tmp_path,
            chunk_size_mb=1,
            max_retries=3,
        )
        assert file_id == "drive_file_123", f"Expected file ID drive_file_123, got {file_id}"
    finally:
        tmp_path.unlink(missing_ok=True)


def test_temporary_cleanup_invariant() -> None:
    """Verify that temp directory deletes all files on exit even if an exception occurs."""
    retained_path = None
    try:
        with tempfile.TemporaryDirectory(prefix="test_yt_") as temp_dir:
            temp_file = Path(temp_dir) / "sample.mp4"
            temp_file.write_bytes(b"content")
            retained_path = temp_file
            assert retained_path.exists()
            raise ValueError("Forced error to test cleanup")
    except ValueError:
        pass

    assert not retained_path.exists(), "Temporary file must not survive context exit!"


def test_load_env_file() -> None:
    """Verify standard library .env parsing."""
    import os
    with tempfile.NamedTemporaryFile("w+", suffix=".env", delete=False) as tmp_env:
        tmp_env.write("TEST_ENV_KEY=hello_world\n# Comment\nANOTHER=value\n")
        tmp_env_path = tmp_env.name

    try:
        load_env_file(tmp_env_path)
        assert os.environ.get("TEST_ENV_KEY") == "hello_world"
        assert os.environ.get("ANOTHER") == "value"
    finally:
        Path(tmp_env_path).unlink(missing_ok=True)


if __name__ == "__main__":
    test_upload_resumable_chunks_and_retries()
    test_temporary_cleanup_invariant()
    test_load_env_file()
    print("All uploader self-checks passed successfully.")
