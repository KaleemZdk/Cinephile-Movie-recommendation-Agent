"""Application settings, read once from the environment and the project .env file.

Every other module asks for settings through get_settings(); nothing else reads
environment variables directly.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import certifi
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_MODEL = "gemini-3.5-flash-lite"
DEFAULT_FALLBACK_MODELS = ("gemini-3.1-flash-lite", "gemini-3.5-flash")


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    library_db: Path
    checkpoint_db: Path
    frontend_dir: Path
    gemini_api_key: str | None
    gemini_model: str
    gemini_fallback_models: tuple[str, ...]
    llm_timeout: float
    tmdb_credential: str | None
    tmdb_region: str
    log_level: str


def _env_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if not raw:
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")
    # Some Windows machines lack an up-to-date certificate store; use certifi unless told otherwise.
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())

    data_dir = Path(os.getenv("CINEPHILE_DATA_DIR") or PROJECT_ROOT / "data")
    return Settings(
        data_dir=data_dir,
        library_db=Path(os.getenv("MOVIES_DB_PATH") or data_dir / "movies.db"),
        checkpoint_db=data_dir / "checkpoints.db",
        frontend_dir=PROJECT_ROOT / "frontend",
        gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
        gemini_model=(os.getenv("GEMINI_MODEL") or DEFAULT_MODEL).strip(),
        gemini_fallback_models=_env_list("GEMINI_FALLBACK_MODELS", DEFAULT_FALLBACK_MODELS),
        llm_timeout=_env_float("LLM_TIMEOUT", 40.0),
        # The v4 read-access token is preferred: it travels in a header, not in the URL.
        tmdb_credential=os.getenv("TMDB_API_Read_Access_Token")  # noqa: SIM112 - existing .env key name
        or os.getenv("TMDB_API_KEY")
        or None,
        tmdb_region=(os.getenv("TMDB_REGION") or "US").upper(),
        log_level=(os.getenv("LOG_LEVEL") or "INFO").upper(),
    )


def configure_logging(level: str | None = None) -> None:
    """Log to stderr (stdout is the MCP protocol channel in the tool server process)."""
    logging.basicConfig(level=level or get_settings().log_level, format="%(levelname)s %(name)s: %(message)s")
    # httpx logs full URLs, which can contain a v3 TMDB key; mcp logs every request at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.WARNING)
