"""Settings: secrets and deployment values from the environment (.env), tuning from YAML."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

ROOT = Path(__file__).resolve().parents[2]


def load_dotenv(path: Path) -> None:
    """Read KEY=VALUE lines; variables already set in the environment win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env(name: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    value = os.environ.get(name) or default
    if required and not value:
        raise SystemExit(f"Missing environment variable {name}; set it in .env (see .env.example).")
    return value


@dataclass
class Settings:
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    embedding_model: str
    seed_pool_max: int
    openalex_api_key: Optional[str]
    s2_api_key: Optional[str]
    github_token: Optional[str]
    contact_email: Optional[str]
    max_papers: int
    max_cost_usd: float
    tuning: dict[str, Any] = field(default_factory=dict)
    runs_dir: Path = ROOT / "runs"

    def t(self, *keys: str, default: Any = None) -> Any:
        """Nested lookup in the YAML tuning, e.g. s.t('monitor', 'yield', 'window')."""
        node: Any = self.tuning
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node


def load_settings(config_path: Optional[str] = None) -> Settings:
    load_dotenv(ROOT / ".env")
    path = Path(config_path) if config_path else ROOT / "config" / "default.yaml"
    tuning = yaml.safe_load(path.read_text()) or {}
    return Settings(
        llm_base_url=_env("LLM_BASE_URL", required=True).rstrip("/"),
        llm_api_key=_env("LLM_API_KEY", required=True),
        llm_model=_env("LLM_MODEL", "gpt-5.6-luna"),
        embedding_model=_env("EMBEDDING_MODEL", "qwen/qwen3-embedding-8b"),
        seed_pool_max=int(_env("SEED_POOL_MAX", "200")),
        openalex_api_key=_env("OPENALEX_API_KEY"),
        s2_api_key=_env("S2_API_KEY"),
        github_token=_env("GITHUB_TOKEN"),
        contact_email=_env("CONTACT_EMAIL"),
        max_papers=int(_env("MAX_PAPERS", "100000")),
        max_cost_usd=float(_env("MAX_COST_USD", "20")),
        tuning=tuning,
    )
