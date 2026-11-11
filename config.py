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
EXPORT_DIR = _path("MVP_EXPORT_DIR", DATA_DIR / "exports")
TEMPLATE_DIR = BASE_DIR / "templates"
COMPONENT_DIR = BASE_DIR / "ui" / "components"

ADMIN_NAME = os.getenv("MVP_ADMIN_NAME", "Admin")
PDF_ENGINE = os.getenv("MVP_PDF_ENGINE", "auto").lower()
# auto -> dropbox_sign when an API key is present, otherwise mock
SIGNATURE_PROVIDER = os.getenv("MVP_SIGNATURE_PROVIDER", "auto").lower()
DROPBOX_SIGN_API_KEY = os.getenv("DROPBOX_SIGN_API_KEY", "")
DROPBOX_SIGN_TEST_MODE = os.getenv("DROPBOX_SIGN_TEST_MODE", "1").lower() not in ("0", "false", "no")

# Review colours: every stakeholder gets one by default so annotations are told apart
# at a glance. Reviewers may pick any swatch when they write an annotation.
REVIEW_PALETTE = (
    "#2563eb",  # blue
    "#dc2626",  # red
    "#059669",  # green
    "#7c3aed",  # violet
    "#ea580c",  # orange
    "#0891b2",  # cyan
    "#db2777",  # pink
    "#65a30d",  # lime
)
ADMIN_COLOR = "#111827"
PALETTE_LABELS = {
    "#2563eb": "Blue",
    "#dc2626": "Red",
    "#059669": "Green",
    "#7c3aed": "Violet",
    "#ea580c": "Orange",
    "#0891b2": "Cyan",
    "#db2777": "Pink",
    "#65a30d": "Lime",
    "#111827": "Ink",
}


def init_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
