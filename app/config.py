"""Application configuration loaded from environment variables.

All keys from the Phase 1 plan (section 4) are declared here, even the ones
that are only read starting in later milestones (XAI_*, DAILY_USD_CAP,
TRANSCRIPT_TURNS), so that .env.example stays complete across milestones.
Nothing in 1a reads those extra keys.
"""

from __future__ import annotations

import re

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Telegram's own charset for the webhook secret token.
_SECRET_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- core / 1a ---
    MODE: str = "polling"  # "webhook" or "polling"
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_SECRET_TOKEN: str = ""
    ALLOWED_CHAT_ID: int = 0
    PUBLIC_URL: str = ""
    DATABASE_URL: str = ""
    LOG_LEVEL: str = "INFO"
    PORT: int = 8080

    # --- reserved for 1c/1d; declared now so .env.example is complete ---
    # TODO(phase-1c): read XAI_* in app/llm/xai.py
    XAI_API_KEY: str = ""
    XAI_MODEL: str = "grok-4.7"
    XAI_REASONING_EFFORT: str = "medium"
    XAI_PRICE_IN: float = 2.00
    XAI_PRICE_CACHED: float = 0.50
    XAI_PRICE_OUT: float = 6.00
    # TODO(phase-1d): read DAILY_USD_CAP in app/core/spend.py
    DAILY_USD_CAP: float = 1.00
    TZ_DEFAULT: str = "Europe/Paris"
    # TODO(phase-1c): read TRANSCRIPT_TURNS in app/core/prompt.py
    TRANSCRIPT_TURNS: int = 30

    @field_validator("DATABASE_URL")
    @classmethod
    def _rewrite_asyncpg_scheme(cls, value: str) -> str:
        """Railway hands us postgresql://; asyncpg needs postgresql+asyncpg://."""
        if value.startswith("postgresql://"):
            return "postgresql+asyncpg://" + value[len("postgresql://") :]
        return value

    @field_validator("TELEGRAM_SECRET_TOKEN")
    @classmethod
    def _validate_secret_token_charset(cls, value: str) -> str:
        if value and not _SECRET_TOKEN_RE.match(value):
            raise ValueError(
                "TELEGRAM_SECRET_TOKEN must match Telegram's charset ^[A-Za-z0-9_-]{1,256}$"
            )
        return value


def get_settings() -> Settings:
    """Build a fresh Settings instance from the current environment."""
    return Settings()
