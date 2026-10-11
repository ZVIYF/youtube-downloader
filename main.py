"""YouTube to Google Drive Uploader.

Downloads videos via yt-dlp to a temporary directory and streams them
to Google Drive using chunked resumable uploads with exponential backoff.
Local files are guaranteed to be cleaned up upon completion or failure.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

def load_env_file(path: str = ".env") -> None:
    """Populate os.environ from a local .env file without external dependencies."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))

from google.oauth2 import service_account
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
import yt_dlp

SCOPES: list[str] = ["https://www.googleapis.com/auth/drive.file"]


def get_drive_service(credentials_path: str) -> Resource:
    """Build and return an authenticated Google Drive API service resource."""
    if not os.path.exists(credentials_path):
        raise FileNotFoundError(f"Service account file not found: {credentials_path}")

    creds = service_account.Credentials.from_service_account_file(
        credentials_path, scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def download_video(url: str, output_dir: Path) -> Path:
    """Download a video via yt-dlp into output_dir and return its local file path."""
    outtmpl = str(output_dir / "%(title)s [%(id)s].%(ext)s")
    ydl_opts = {
        "outtmpl": outtmpl,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "quiet": False,
        "no_warnings": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if not info:
            raise RuntimeError("Failed to extract video information.")
        filename = ydl.prepare_filename(info)

    target_path = Path(filename).with_suffix(".mp4")
    if not target_path.exists():
        target_path = Path(filename)
    if not target_path.exists():
        raise FileNotFoundError(f"Expected download file missing: {target_path}")

    return target_path


def upload_to_drive(
    service: Resource,
    file_path: Path,
    folder_id: Optional[str] = None,
    chunk_size_mb: int = 10,
    max_retries: int = 5,
) -> str:
    """Upload a local file to Google Drive using chunked resumable upload with retry."""
    file_metadata: dict[str, str | list[str]] = {"name": file_path.name}
    if folder_id:
        file_metadata["parents"] = [folder_id]

    media = MediaFileUpload(
        str(file_path),
        mimetype="video/mp4",
        chunksize=chunk_size_mb * 1024 * 1024,
        resumable=True,
    )

    request = service.files().create(body=file_metadata, media_body=media, fields="id")
    response: Optional[dict[str, str]] = None

    while response is None:
        for attempt in range(max_retries):
            try:
                status, response = request.next_chunk()
                if status:
                    print(f"Uploading: {int(status.progress() * 100)}% completed.")
                break
            except (HttpError, socket.error, OSError) as error:
                if attempt == max_retries - 1:
                    raise RuntimeError(f"Upload failed after {max_retries} attempts: {error}") from error
                backoff = 2**attempt
                print(f"Transient upload error ({error}). Retrying in {backoff}s...")
                time.sleep(backoff)

    return response.get("id", "")


def process_video(
    url: str,
    credentials_path: str,
    folder_id: Optional[str] = None,
    chunk_size_mb: int = 10,
) -> str:
    """Download and upload video; temporary files are strictly wiped on exit."""
    service = get_drive_service(credentials_path)

    # TemporaryDirectory guarantees automatic cleanup on exit or exception
    with tempfile.TemporaryDirectory(prefix="yt_gdrive_") as temp_dir:
        temp_path = Path(temp_dir)
        print(f"Downloading video from: {url}")
        local_file = download_video(url, temp_path)
        print(f"Downloaded to temporary file: {local_file.name} ({local_file.stat().st_size / (1024*1024):.1f} MB)")

        print("Initiating Google Drive resumable upload...")
        file_id = upload_to_drive(
            service=service,
            file_path=local_file,
            folder_id=folder_id,
            chunk_size_mb=chunk_size_mb,
        )
        print(f"Upload complete. Drive File ID: {file_id}")
        return file_id


def main() -> None:
    """CLI entrypoint."""
    load_env_file()

    parser = argparse.ArgumentParser(description="Upload YouTube video to Google Drive.")
    parser.add_argument("url", help="YouTube video URL")
    parser.add_argument(
        "--credentials",
        default=os.getenv("GDRIVE_SERVICE_ACCOUNT_FILE", "service_account.json"),
        help="Path to Google Drive service account JSON file",
    )
    parser.add_argument(
        "--folder-id",
        default=os.getenv("GDRIVE_FOLDER_ID") or None,
        help="Optional Google Drive parent folder ID",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=int(os.getenv("CHUNK_SIZE_MB", "10")),
        help="Resumable upload chunk size in MB (default: 10)",
    )

    args = parser.parse_args()

    try:
        file_id = process_video(
            url=args.url,
            credentials_path=args.credentials,
            folder_id=args.folder_id,
            chunk_size_mb=args.chunk_size,
        )
        print(f"Success! Video uploaded with ID: {file_id}")
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
