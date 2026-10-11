"""YouTube downloader and Google Drive chunked resumable uploader module."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import time
from pathlib import Path
from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
import yt_dlp

SCOPES: list[str] = ["https://www.googleapis.com/auth/drive.file"]


def get_service_account_email(credentials_path: str) -> str:
    """Extract client_email from the service account JSON or fallback to env."""
    if os.path.exists(credentials_path):
        try:
            with open(credentials_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                email = data.get("client_email")
                if email:
                    return str(email)
        except Exception:
            pass
    return os.getenv("SERVICE_ACCOUNT_EMAIL", "uploader-service@project.iam.gserviceaccount.com")


def get_drive_service(credentials_path: str) -> Resource:
    """Build and return an authenticated Google Drive API service resource."""
    if not os.path.exists(credentials_path):
        raise FileNotFoundError(f"Service account file not found: {credentials_path}")

    creds = service_account.Credentials.from_service_account_file(
        credentials_path, scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def download_video(url: str, output_dir: Path) -> Path:
    """Download video via yt-dlp to output_dir and return file path."""
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
    """Stream file to Google Drive using chunked resumable upload with retry backoff."""
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
                    print(f"[{file_path.name}] Uploading: {int(status.progress() * 100)}%")
                break
            except (HttpError, socket.error, OSError) as error:
                if attempt == max_retries - 1:
                    raise RuntimeError(f"Upload failed after {max_retries} retries: {error}") from error
                backoff = 2**attempt
                print(f"Transient error: {error}. Retrying in {backoff}s...")
                time.sleep(backoff)

    return response.get("id", "")


def process_video_background(
    url: str,
    credentials_path: str,
    folder_id: Optional[str] = None,
    chunk_size_mb: int = 10,
) -> None:
    """Execute download and upload in background; guarantee temporary file deletion."""
    try:
        service = get_drive_service(credentials_path)
        with tempfile.TemporaryDirectory(prefix="yt_gdrive_") as temp_dir:
            temp_path = Path(temp_dir)
            print(f"Starting download: {url}")
            local_file = download_video(url, temp_path)
            print(f"Downloaded: {local_file.name}. Starting Drive upload to folder '{folder_id}'...")

            file_id = upload_to_drive(
                service=service,
                file_path=local_file,
                folder_id=folder_id,
                chunk_size_mb=chunk_size_mb,
            )
            print(f"Background upload completed successfully. Drive File ID: {file_id}")
    except Exception as exc:
        print(f"Background processing error for {url}: {exc}")

