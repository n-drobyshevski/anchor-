"""Application configuration loaded from environment variables.

All keys from the Phase 1 plan (section 4) are declared here, even
XAI_API_KEY/DAILY_USD_CAP/TRANSCRIPT_TURNS which 1a/1b did not yet
read, so that .env.example stays complete across milestones. 1c reads
all of the XAI_* keys (app/llm/xai.py), TRANSCRIPT_TURNS (app/core/
prompt.py) and DAILY_USD_CAP (app/core/spend.py's check_cap) -- the
daily cap ships with the persona turn rather than waiting for 1d's
other safety features, per the decision recorded in the 1c plan doc
(the wallet guard must exist the moment the API key goes live).
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

    # --- 1c: LLM provider, prompt assembly, cost and the daily spend cap ---
    XAI_API_KEY: str = ""
    XAI_MODEL: str = "grok-4.7"
    XAI_REASONING_EFFORT: str = "medium"
    XAI_PRICE_IN: float = 2.00
    XAI_PRICE_CACHED: float = 0.50
    XAI_PRICE_OUT: float = 6.00
    DAILY_USD_CAP: float = 1.00
    TZ_DEFAULT: str = "Europe/Paris"
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
