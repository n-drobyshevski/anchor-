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

# Values that arrive by copy-paste and must not carry stray whitespace.
_STRIPPED_FIELDS = (
    "MODE",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_SECRET_TOKEN",
    "PUBLIC_URL",
    "DATABASE_URL",
    "OPENROUTER_API_KEY",
    "LLM_MODEL",
    "LLM_MODEL_CHEAP",
    "LLM_DATA_COLLECTION",
    "TZ_DEFAULT",
)


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
    # 1f: opt-in web search via OpenRouter's `web` plugin, triggered only
    # by the /search command (app/tg/router.py) -- never on an ordinary turn.
    LLM_WEB_SEARCH: bool = True
    LLM_WEB_SEARCH_MAX_RESULTS: int = 5
    # OpenRouter's docs do not say whether the Exa search fee ($0.007/req)
    # is already folded into the `usage.cost` OpenRouter reports, and
    # compute_cost (app/core/spend.py) prefers that vendor-reported cost
    # outright. Adding a non-zero price here on top of a cost that
    # already includes the fee would double-bill every searched turn.
    # Default to 0.0 (trust the vendor); scripts/smoke.py measures the
    # actual delta between a searched and unsearched call on live data --
    # set this to 0.007 only if that shows OpenRouter excludes the fee.
    LLM_WEB_SEARCH_PRICE_USD: float = 0.0
    DAILY_USD_CAP: float = 3.00
    TZ_DEFAULT: str = "Europe/Paris"
    TRANSCRIPT_TURNS: int = 30

    # --- 2a: the cheap model and scenes (phase-2 plan section 2) ---
    # The phase-2 plan names these XAI_MODEL_CHEAP / XAI_CHEAP_PRICE_*
    # and picks grok-4.3. Milestone 1e moved this bot off xAI entirely,
    # and the user's decision is to run the *same* model for the
    # background calls as for chat, so the plan's names are carried over
    # into this repo's post-1e LLM_* vocabulary and point at Cydonia.
    #
    # It is still a separate setting with its own price triple rather
    # than a reuse of LLM_MODEL, because compute_cost() is model-aware
    # (app/core/spend.py) and switching the background model later must
    # be one env var, not a code change. Defaulting it to the same model
    # and the same prices makes that seam free today.
    #
    # There is no LLM_CHEAP_REASONING_EFFORT: the plan asks for
    # reasoning_effort=low, but Cydonia's only provider (Parasail) does
    # not advertise that parameter, so the knob would be dead config.
    LLM_MODEL_CHEAP: str = "thedrummer/cydonia-24b-v4.1"
    LLM_CHEAP_PRICE_IN: float = 0.30
    LLM_CHEAP_PRICE_CACHED: float = 0.15
    LLM_CHEAP_PRICE_OUT: float = 0.50
    # Background calls are bounded much tighter than a chat turn: a scene
    # summary is capped at 5 sentences by its prompt, so 400 tokens is
    # slack, not a target. Low temperature because none of the background
    # calls (summary now; extractor and welfare later) want invention.
    LLM_CHEAP_MAX_TOKENS: int = 400
    LLM_CHEAP_TEMPERATURE: float = 0.3

    # Silence longer than this closes the open scene and opens a new one
    # (phase-2 plan section 5).
    SCENE_IDLE_HOURS: int = 6

    # --- 2b: memory (phase-2 plan sections 2, 6, 7) ---
    # How many memories each in-character prompt may carry. The
    # retrieval *thresholds* are deliberately not here but in
    # app/core/memory.py -- a deploy should not be able to set the
    # dedupe cutoff to 0 and start duplicating every fact.
    #
    # MEMORY_PINNED_MAX is enforced on write as well as on render
    # (app/tg/memory.py): section 7 caps the render at 8, so silently
    # accepting a ninth pin would drop a memory the user explicitly
    # asked to always be remembered, with no feedback.
    MEMORY_PINNED_MAX: int = 8
    MEMORY_RETRIEVED_MAX: int = 6

    @field_validator("DATABASE_URL")
    @classmethod
    def _rewrite_asyncpg_scheme(cls, value: str) -> str:
        """Railway hands us postgresql://; asyncpg needs postgresql+asyncpg://."""
        if value.startswith("postgresql://"):
            return "postgresql+asyncpg://" + value[len("postgresql://") :]
        return value

    @field_validator(*_STRIPPED_FIELDS, mode="before")
    @classmethod
    def _strip_surrounding_whitespace(cls, value):
        """Trim whitespace a dashboard or a shell added to a pasted value.

        `openssl rand -hex 32` ends in a newline, a dashboard field can
        keep a trailing space, and a Windows clipboard adds CR. None of
        those are part of the credential, but all of them travel with
        it, and the resulting failure is opaque: an API key with a
        trailing CR is rejected as unauthorized by the vendor, not as
        malformed by us.

        mode="before" so this runs ahead of the other validators on
        these fields -- notably the DATABASE_URL scheme rewrite, which
        does a prefix comparison.
        """
        return value.strip() if isinstance(value, str) else value


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

    # Checked here rather than as a validator on the field, because a
    # pydantic ValidationError renders the offending input_value into
    # the message -- which put a live webhook secret into the platform's
    # deploy logs, where anyone with read access to the project can see
    # it. Nothing that validates a credential may echo it.
    token = settings.TELEGRAM_SECRET_TOKEN
    if token and not _SECRET_TOKEN_RE.match(token):
        raise SystemExit(
            "TELEGRAM_SECRET_TOKEN must match Telegram's charset "
            "^[A-Za-z0-9_-]{1,256}$ (the value is deliberately not shown). "
            "Surrounding whitespace is stripped automatically, so this means "
            "the value itself contains a character outside that set -- most "
            "often a line break from a multi-line paste."
        )
