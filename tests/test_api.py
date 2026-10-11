import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from typing import Generator
from unittest.mock import MagicMock, patch

# Hermetic mocking: ensure no actual network or Google/yt-dlp calls can occur
for mod in [
    "yt_dlp",
    "google",
    "google.oauth2",
    "google.oauth2.service_account",
    "googleapiclient",
    "googleapiclient.discovery",
    "googleapiclient.errors",
    "googleapiclient.http",
]:
    sys.modules.setdefault(mod, MagicMock())

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool

from app import app, Base, get_db, User

# In-memory test database with StaticPool
SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"
test_engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)



@pytest.fixture(scope="session", autouse=True)
def setup_test_db() -> Generator[None, None, None]:
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)


def override_get_db() -> Generator[Session, None, None]:
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as test_client:
        yield test_client


def test_auth_registration_and_login_flow(client: TestClient) -> None:
    # 1. Access register page
    resp = client.get("/register")
    assert resp.status_code == 200
    assert "Create an Account" in resp.text

    # 2. Register user
    email = "testuser@example.com"
    resp = client.post(
        "/register",
        data={"email": email, "password": "securepassword123"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/setup-drive"
    assert "session" in resp.cookies

    # 3. Duplicate registration check
    resp_dup = client.post(
        "/register",
        data={"email": email, "password": "anotherpassword"},
        follow_redirects=False,
    )
    assert resp_dup.status_code == 400
    assert "already exists" in resp_dup.text

    # 4. Login flow
    login_fail = client.post(
        "/login",
        data={"email": email, "password": "wrongpassword"},
        follow_redirects=False,
    )
    assert login_fail.status_code == 400
    assert "Invalid email or password" in login_fail.text

    login_success = client.post(
        "/login",
        data={"email": email, "password": "securepassword123"},
        follow_redirects=False,
    )
    assert login_success.status_code == 303
    assert "session" in login_success.cookies


def test_setup_drive_flow(client: TestClient) -> None:
    # Register a new user
    user_email = "driveuser@example.com"
    reg_resp = client.post(
        "/register",
        data={"email": user_email, "password": "pass"},
        follow_redirects=False,
    )
    session_cookie = reg_resp.cookies["session"]
    client.cookies.set("session", session_cookie)

    # Access setup-drive page
    resp = client.get("/setup-drive")
    assert resp.status_code == 200
    # Assert exact Hebrew instruction presence
    assert "ברוך הבא! כדי שהמערכת תוכל להעלות סרטונים ישירות לדרייב שלך" in resp.text
    assert "Google Drive Folder ID" in resp.text

    # Submit drive folder id
    folder_id = "test_gdrive_folder_abc123"
    post_resp = client.post(
        "/setup-drive",
        data={"drive_folder_id": folder_id},
        follow_redirects=False,
    )
    assert post_resp.status_code == 303
    assert post_resp.headers["location"] == "/"

    # Verify user record updated in database
    db = TestingSessionLocal()
    user = db.query(User).filter(User.email == user_email).first()
    assert user is not None
    assert user.drive_folder_id == folder_id
    db.close()


def test_dashboard_and_background_upload_trigger(client: TestClient) -> None:
    # Setup user with drive folder id
    email = "uploaduser@example.com"
    reg = client.post(
        "/register",
        data={"email": email, "password": "pass"},
        follow_redirects=False,
    )
    client.cookies.set("session", reg.cookies["session"])

    client.post(
        "/setup-drive",
        data={"drive_folder_id": "target_folder_999"},
        follow_redirects=False,
    )

    # 1. Access dashboard
    dash_resp = client.get("/")
    assert dash_resp.status_code == 200
    assert "Upload YouTube Video" in dash_resp.text

    # 2. Trigger upload with background task mock
    target_yt_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    with patch("app.process_video_background") as mock_bg_task:
        upload_resp = client.post(
            "/upload",
            data={"url": target_yt_url},
            follow_redirects=False,
        )
        assert upload_resp.status_code == 200
        assert "Process Started" in upload_resp.text
        assert target_yt_url in upload_resp.text

        # Verify background task was scheduled with user's drive folder
        mock_bg_task.assert_called_once()
        call_kwargs = mock_bg_task.call_args.kwargs
        assert call_kwargs["url"] == target_yt_url
        assert call_kwargs["folder_id"] == "target_folder_999"


def test_logout_flow(client: TestClient) -> None:
    client.cookies.set("session", "some_session_value")
    resp = client.get("/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
