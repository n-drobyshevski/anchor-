"""Application configuration loaded from environment variables.

All keys from the Phase 1 plan (section 4) are declared here, even
OPENROUTER_API_KEY/DAILY_USD_CAP/TRANSCRIPT_TURNS which 1a/1b did not
yet read, so that .env.example stays complete across milestones. 1c
read all of the vendor keys (app/llm/openrouter.py, added in 1e --
1c originally shipped against a different vendor), TRANSCRIPT_TURNS
(app/core/prompt.py) and DAILY_USD_CAP (app/core/spend.py's check_cap)
-- the daily cap ships with the persona turn rather than waiting for
1d's other safety features, per the decision recorded in the 1c plan
doc (the wallet guard must exist the moment the API key goes live).
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
    # 1e: vendor is OpenRouter (app/llm/openrouter.py), Chat Completions API.
    OPENROUTER_API_KEY: str = ""
    LLM_MODEL: str = "thedrummer/cydonia-24b-v4.1"
    LLM_MAX_TOKENS: int = 700
    LLM_TEMPERATURE: float = 0.9
    # "deny" restricts routing to providers that do not retain or train
    # on prompts. Cydonia's only provider is Parasail, so a mismatch
    # between this setting and what Parasail offers fails the request
    # outright rather than silently falling back to a provider that
    # would retain our prompts.
    LLM_DATA_COLLECTION: str = "deny"
    LLM_PRICE_IN: float = 0.30
    LLM_PRICE_CACHED: float = 0.15
    LLM_PRICE_OUT: float = 0.50
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


VALID_MODES = ("webhook", "polling")
# Credentials and connection details with no usable default. Every one
# of these is consumed by a constructor that rejects an empty string, so
# a missing value is a boot failure, not a degraded run.
REQUIRED_ALWAYS = ("TELEGRAM_BOT_TOKEN", "DATABASE_URL", "OPENROUTER_API_KEY")
# Only webhook mode serves HTTP: it verifies the secret on every request
# and registers PUBLIC_URL with Telegram. Polling needs neither.
REQUIRED_WEBHOOK = ("TELEGRAM_SECRET_TOKEN", "PUBLIC_URL")


def missing_required(settings: Settings) -> list[str]:
    """Names of settings that must be set before the app can run.

    Names only, never values -- this list is printed and logged.
    """
    missing = [name for name in REQUIRED_ALWAYS if not getattr(settings, name)]
    # 0 is the field default, and chat ids are never 0, so it reads as unset.
    if settings.ALLOWED_CHAT_ID == 0:
        missing.append("ALLOWED_CHAT_ID")
    if settings.MODE == "webhook":
        missing.extend(name for name in REQUIRED_WEBHOOK if not getattr(settings, name))
    return missing


def check_runtime_settings(settings: Settings) -> None:
    """Exit with a message naming what is missing, before anything is built.

    Deliberately NOT a pydantic validator: Settings() is constructed all
    over the test suite with a handful of relevant fields, and a
    validator would make every one of those raise.

    It exists because the constructors that actually fail do so with
    errors that point somewhere else. An unset OPENROUTER_API_KEY
    surfaces as the openai SDK's "Missing credentials ... set the
    OPENAI_API_KEY environment variable" -- a variable this app does not
    read, naming a vendor it does not call -- and an unset
    TELEGRAM_BOT_TOKEN as aiogram's bare "Token is invalid!". Both
    arrive as a traceback from inside a dependency, after the process
    has already died and taken the healthcheck with it.

    Every missing name is reported at once, so a misconfigured deploy
    takes one round trip to fix rather than one per variable.
    """
    if settings.MODE not in VALID_MODES:
        raise SystemExit(
            f"MODE must be one of {', '.join(VALID_MODES)}, got {settings.MODE!r}. "
            "Anything else silently starts the polling transport, which serves no "
            "HTTP and so can never answer a platform healthcheck."
        )

    missing = missing_required(settings)
    if missing:
        raise SystemExit(
            "Missing required environment variables: "
            + ", ".join(missing)
            + ". Set them in the deployment environment (or .env locally); "
            "see .env.example for the full list."
        )
