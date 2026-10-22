"""Web settings from the environment (the repository's .env is read too). Defaults suit one server."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from litscout.config import ROOT, load_dotenv


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class WebSettings:
    repo_root: Path = ROOT
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("WEB_DATA_DIR", ROOT / "backend" / "data")))
    runs_dir: Path = field(default_factory=lambda: Path(os.environ.get("WEB_RUNS_DIR", ROOT / "runs")))
    frontend_dist: Path = field(default_factory=lambda: Path(os.environ.get("WEB_FRONTEND_DIST", ROOT / "frontend" / "dist")))
    python: str = sys.executable
    max_concurrent_jobs: int = field(default_factory=lambda: int(os.environ.get("WEB_MAX_CONCURRENT_JOBS", "2")))
    max_cost_usd: float = field(default_factory=lambda: float(os.environ.get("WEB_MAX_COST_USD", os.environ.get("MAX_COST_USD", "5"))))
    max_papers: int = field(default_factory=lambda: int(os.environ.get("WEB_MAX_PAPERS", os.environ.get("MAX_PAPERS", "20000"))))
    max_pdfs: int = field(default_factory=lambda: int(os.environ.get("WEB_MAX_PDFS", "20")))
    max_pdf_mb: int = field(default_factory=lambda: int(os.environ.get("WEB_MAX_PDF_MB", "25")))
    session_days: int = field(default_factory=lambda: int(os.environ.get("WEB_SESSION_DAYS", "7")))
    cookie_secure: bool = field(default_factory=lambda: _bool("WEB_COOKIE_SECURE", False))
    login_attempts_per_minute: int = 5

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'web.db'}"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"


def get_settings() -> WebSettings:
    load_dotenv(ROOT / ".env")
    s = WebSettings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.uploads_dir.mkdir(parents=True, exist_ok=True)
    s.runs_dir.mkdir(parents=True, exist_ok=True)
    return s
