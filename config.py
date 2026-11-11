"""Runtime configuration. All paths are resolved relative to the project root."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=False)


def _path(env_name: str, default: Path) -> Path:
    raw = os.getenv(env_name)
    path = Path(raw) if raw else default
    if not path.is_absolute():
        path = BASE_DIR / path
    return path.resolve()


DATA_DIR = _path("MVP_DATA_DIR", BASE_DIR / "data")
DB_PATH = _path("MVP_DB_PATH", DATA_DIR / "contracts.duckdb")
PDF_DIR = _path("MVP_PDF_DIR", DATA_DIR / "pdfs")
TEMPLATE_DIR = BASE_DIR / "templates"

ADMIN_NAME = os.getenv("MVP_ADMIN_NAME", "Admin")
PDF_ENGINE = os.getenv("MVP_PDF_ENGINE", "auto").lower()
# auto -> dropbox_sign when an API key is present, otherwise mock
SIGNATURE_PROVIDER = os.getenv("MVP_SIGNATURE_PROVIDER", "auto").lower()
DROPBOX_SIGN_API_KEY = os.getenv("DROPBOX_SIGN_API_KEY", "")
DROPBOX_SIGN_TEST_MODE = os.getenv("DROPBOX_SIGN_TEST_MODE", "1").lower() not in ("0", "false", "no")


def init_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PDF_DIR.mkdir(parents=True, exist_ok=True)
