"""YouTube downloader and Google Drive chunked resumable uploader module with progress tracking."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

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


def download_video(
    url: str,
    output_dir: Path,
    progress_callback: Optional[Callable[[int], None]] = None,
) -> Path:
    """Download video via yt-dlp to output_dir with optional progress callback."""
    outtmpl = str(output_dir / "%(title)s [%(id)s].%(ext)s")

    def progress_hook(d: dict) -> None:
        if d.get("status") == "downloading" and progress_callback:
            percent_str = d.get("_percent_str", "").replace("%", "").strip()
            clean_str = re.sub(r"\x1b\[[0-9;]*m", "", percent_str)
            try:
                progress_callback(int(float(clean_str)))
            except (ValueError, TypeError):
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes") or 0
                if total > 0:
                    progress_callback(int(downloaded / total * 100))

    cookiefile: Optional[str] = None
    env_cookie_file = os.getenv("YT_COOKIES_FILE")
    if env_cookie_file and os.path.exists(env_cookie_file):
        dest_path = Path(tempfile.gettempdir()) / "youtube_cookies.txt"
        with open(env_cookie_file, "r", encoding="utf-8", errors="replace") as src:
            content = src.read().replace("\r\n", "\n")
        with open(dest_path, "w", encoding="utf-8", newline="\n") as dst:
            dst.write(content)
        print(f"DEBUG: Cookies sanitized and written. Size: {os.path.getsize(dest_path)} bytes")
        cookiefile = str(dest_path)

    ydl_opts = {
        "outtmpl": outtmpl,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "quiet": False,
        "no_warnings": True,
        "extractor_args": {"youtube": ["player_client=tv,ios"]},
        "cookiefile": cookiefile,
        "progress_hooks": [progress_hook] if progress_callback else [],
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
    progress_callback: Optional[Callable[[int], None]] = None,
) -> str:
    """Stream file to Google Drive using chunked resumable upload with retry backoff and progress callback."""
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
                    pct = int(status.progress() * 100)
                    print(f"[{file_path.name}] Uploading: {pct}%")
                    if progress_callback:
                        progress_callback(pct)
                break
            except (HttpError, socket.error, OSError) as error:
                if attempt == max_retries - 1:
                    raise RuntimeError(f"Upload failed after {max_retries} retries: {error}") from error
                backoff = 2**attempt
                print(f"Transient error: {error}. Retrying in {backoff}s...")
                time.sleep(backoff)

    return response.get("id", "")


def process_video_background(
    task_id: str,
    url: str,
    credentials_path: str,
    folder_id: Optional[str] = None,
    chunk_size_mb: int = 10,
    progress_store: Optional[dict[str, dict]] = None,
) -> None:
    """Execute download and upload in background; update progress_store and delete temporary files."""
    try:
        if progress_store is not None:
            progress_store[task_id] = {"phase": "downloading", "progress": 0, "file_id": ""}

        service = get_drive_service(credentials_path)
        with tempfile.TemporaryDirectory(prefix="yt_gdrive_") as temp_dir:
            temp_path = Path(temp_dir)
            print(f"[{task_id}] Starting download: {url}")

            def on_download(pct: int) -> None:
                if progress_store is not None:
                    progress_store[task_id] = {"phase": "downloading", "progress": pct, "file_id": ""}

            local_file = download_video(url, temp_path, progress_callback=on_download)
            print(f"[{task_id}] Downloaded: {local_file.name}. Starting Drive upload to folder '{folder_id}'...")

            if progress_store is not None:
                progress_store[task_id] = {"phase": "uploading", "progress": 0, "file_id": ""}

            def on_upload(pct: int) -> None:
                if progress_store is not None:
                    progress_store[task_id] = {"phase": "uploading", "progress": pct, "file_id": ""}

            file_id = upload_to_drive(
                service=service,
                file_path=local_file,
                folder_id=folder_id,
                chunk_size_mb=chunk_size_mb,
                progress_callback=on_upload,
            )
            print(f"[{task_id}] Background upload completed. Drive File ID: {file_id}")
            if progress_store is not None:
                progress_store[task_id] = {"phase": "done", "progress": 100, "file_id": file_id}
    except Exception as exc:
        print(f"[{task_id}] Background processing error for {url}: {exc}")
        if progress_store is not None:
            progress_store[task_id] = {"phase": "error", "progress": 0, "file_id": "", "error": str(exc)}
