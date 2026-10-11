"""Multi-user Web Application for YouTube to Google Drive Uploads.

Provides user authentication, Google Drive folder onboarding, and asynchronous
background processing via FastAPI BackgroundTasks and SQLAlchemy SQLite storage.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Generator, Optional

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from uploader import get_service_account_email, process_video_background

# ---------------------------------------------------------------------------
# Configuration & Secret Management
# ---------------------------------------------------------------------------
def load_env(path: str = ".env") -> None:
    """Populate os.environ from a local .env file without external dependencies."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))

load_env()

SECRET_KEY: str = os.getenv("SECRET_KEY", "ponytail-insecure-secret-key-change-me")
DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./app.db")
CREDENTIALS_PATH: str = os.getenv("GDRIVE_SERVICE_ACCOUNT_FILE", "service_account.json")
CHUNK_SIZE_MB: int = int(os.getenv("CHUNK_SIZE_MB", "10"))

# ---------------------------------------------------------------------------
# Database Model & Session (SQLAlchemy SQLite)
# ---------------------------------------------------------------------------
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class User(Base):
    """User database record with optional Google Drive folder association."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    drive_folder_id = Column(String, nullable=True)


Base.metadata.create_all(bind=engine)


def get_db() -> Generator[Session, None, None]:
    """Dependency providing a transactional database session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Password Hashing & Cookie-Based Session Auth
# ---------------------------------------------------------------------------
import bcrypt


def hash_password(password: str) -> str:
    """Hash password directly using bcrypt."""
    pwd_bytes = password.encode("utf-8")[:72]
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(pwd_bytes, salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Verify plain password against bcrypt hash."""
    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("utf-8"))
    except Exception:
        return False



def create_session_token(user_id: int) -> str:
    """Generate an HMAC-SHA256 signed session cookie token."""
    sig = hmac.new(SECRET_KEY.encode(), str(user_id).encode(), hashlib.sha256).hexdigest()
    return f"{user_id}:{sig}"


def parse_session_token(token: str) -> Optional[int]:
    """Validate signed session token and return user_id, or None if invalid."""
    try:
        user_id_str, sig = token.split(":", 1)
        expected = hmac.new(SECRET_KEY.encode(), user_id_str.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, expected):
            return int(user_id_str)
    except Exception:
        return None
    return None


def get_current_user_optional(
    request: Request, db: Session = Depends(get_db)
) -> Optional[User]:
    """Extract authenticated User from signed session cookie, if present."""
    token = request.cookies.get("session")
    if not token:
        return None
    user_id = parse_session_token(token)
    if user_id is None:
        return None
    return db.query(User).filter(User.id == user_id).first()


def get_current_user(
    request: Request, db: Session = Depends(get_db)
) -> User:
    """Ensure authenticated User is present, raising redirect or 401."""
    user = get_current_user_optional(request, db)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/login"},
        )
    return user


# ---------------------------------------------------------------------------
# Web Application & Route Handlers
# ---------------------------------------------------------------------------
app = FastAPI(title="YouTube to Google Drive Uploader")
templates = Jinja2Templates(directory="templates")


@app.get("/register", response_class=HTMLResponse)
def register_form(request: Request) -> HTMLResponse:
    """Render account registration page."""
    return templates.TemplateResponse(request=request, name="register.html", context={"user": None})


@app.post("/register")
def register_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Create new user account, initialize session, and route to drive setup."""
    existing = db.query(User).filter(User.email == email.strip().lower()).first()
    if existing:
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={"user": None, "error": "An account with this email already exists."},
            status_code=400,
        )

    user = User(
        email=email.strip().lower(),
        password_hash=hash_password(password),
        drive_folder_id=None,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    response = RedirectResponse(url="/setup-drive", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(key="session", value=create_session_token(user.id), httponly=True, samesite="lax")
    return response


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    """Render login page."""
    return templates.TemplateResponse(request=request, name="login.html", context={"user": None})


@app.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Verify credentials and initiate user session."""
    user = db.query(User).filter(User.email == email.strip().lower()).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"user": None, "error": "Invalid email or password."},
            status_code=400,
        )

    target_url = "/setup-drive" if not user.drive_folder_id else "/"
    response = RedirectResponse(url=target_url, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(key="session", value=create_session_token(user.id), httponly=True, samesite="lax")
    return response


@app.get("/logout")
def logout() -> RedirectResponse:
    """Clear session cookie and redirect to login."""
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(key="session")
    return response


@app.get("/setup-drive", response_class=HTMLResponse)
def setup_drive_form(
    request: Request,
    user: User = Depends(get_current_user),
) -> HTMLResponse:
    """Render Google Drive folder onboarding page with service account email."""
    service_email = get_service_account_email(CREDENTIALS_PATH)
    return templates.TemplateResponse(
        request=request,
        name="setup_drive.html",
        context={
            "user": user,
            "service_account_email": service_email,
            "current_folder_id": user.drive_folder_id,
        },
    )


@app.post("/setup-drive")
def setup_drive_submit(
    request: Request,
    drive_folder_id: str = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Persist target Google Drive folder ID and redirect to dashboard."""
    folder_id = drive_folder_id.strip()
    if not folder_id:
        service_email = get_service_account_email(CREDENTIALS_PATH)
        return templates.TemplateResponse(
            request=request,
            name="setup_drive.html",
            context={
                "user": user,
                "service_account_email": service_email,
                "error": "Folder ID cannot be empty.",
            },
            status_code=400,
        )

    user.drive_folder_id = folder_id
    db.commit()
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: User = Depends(get_current_user),
) -> HTMLResponse:
    """Main dashboard to submit YouTube URLs; redirects to setup-drive if folder unconfigured."""
    if not user.drive_folder_id:
        return RedirectResponse(url="/setup-drive", status_code=status.HTTP_303_SEE_OTHER)

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"user": user, "message": None},
    )


@app.post("/upload", response_class=HTMLResponse)
def start_upload(
    request: Request,
    background_tasks: BackgroundTasks,
    url: str = Form(...),
    user: User = Depends(get_current_user),
) -> HTMLResponse:
    """Trigger background download and upload, returning immediate feedback."""
    if not user.drive_folder_id:
        return RedirectResponse(url="/setup-drive", status_code=status.HTTP_303_SEE_OTHER)

    clean_url = url.strip()
    background_tasks.add_task(
        process_video_background,
        url=clean_url,
        credentials_path=CREDENTIALS_PATH,
        folder_id=user.drive_folder_id,
        chunk_size_mb=CHUNK_SIZE_MB,
    )

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "user": user,
            "message": f"Downloading '{clean_url}' and uploading to Google Drive in the background.",
        },
    )


