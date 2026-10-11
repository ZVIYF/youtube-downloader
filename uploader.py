"""YouTube downloader and Google Drive chunked resumable uploader module with progress tracking."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from google.oauth2 import service_account
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
import requests

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
    """Download video via Cobalt API directly into output_dir with streaming progress."""
    api_url = os.getenv("COBALT_API_URL", "https://api.cobalt.tools/api/json")
    api_key = os.getenv("COBALT_API_KEY")

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Api-Key {api_key}"

    payload = {"url": url}

    response = requests.post(api_url, headers=headers, json=payload, timeout=30)
    # If the default /api/json returned shutdown message (v7), attempt root endpoint fallback
    if response.status_code == 400 and "/api/json" in api_url:
        fallback_url = api_url.replace("/api/json", "/")
        fallback_resp = requests.post(fallback_url, headers=headers, json=payload, timeout=30)
        if fallback_resp.ok:
            response = fallback_resp

    if not response.ok:
        raise RuntimeError(f"Cobalt API request failed ({response.status_code}): {response.text}")

    data = response.json()
    direct_url = data.get("url")
    if not direct_url:
        status = data.get("status")
        detail = data.get("text") or data.get("error", {}).get("code")
        raise RuntimeError(f"Cobalt API did not return a video URL (status: {status}, detail: {detail})")

    filename = data.get("filename") or f"video_{int(time.time())}.mp4"
    if not filename.endswith(".mp4"):
        filename += ".mp4"
    target_path = output_dir / filename

    with requests.get(direct_url, stream=True, timeout=120) as stream_resp:
        stream_resp.raise_for_status()
        total_size = int(stream_resp.headers.get("content-length", 0))
        downloaded = 0

        with open(target_path, "wb") as f:
            for chunk in stream_resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0 and progress_callback:
                        pct = int(downloaded / total_size * 100)
                        progress_callback(pct)

    if progress_callback:
        progress_callback(100)

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
