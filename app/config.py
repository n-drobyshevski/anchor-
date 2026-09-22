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

import datetime
import re

from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

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
    "LLM_MODEL_JUDGE",
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
    #
    # H3: defaults to False. /search was never in a plan -- it arrived with
    # milestone 1f and defaulted on -- and it is the only path in this bot
    # that sends the user's words to a third party (Exa, via the plugin).
    # The feature stays in the tree for phase 4; it is simply not live
    # until someone turns it on deliberately. When it is False the handler
    # answers SEARCH_DISABLED_REPLY_TEXT and makes no model call at all.
    LLM_WEB_SEARCH: bool = False
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
    # Lowered from 3.00 to 1.00 in 3a, by decision: phase-3 plan
    # section 12 budgets the whole proactive day (morning + evening
    # nag + at most one tick, plus ~6 tick decisions) at about
    # $0.03-0.05 and reasons throughout against a $1 cap. The cap is
    # the gate's row 5 as well as the chat guard now, so it is the
    # first thing that silences unsolicited messages on a runaway
    # day -- which is the right order: the user asked for none of
    # them.
    DAILY_USD_CAP: float = 1.00
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

    # --- 3e: the eval harness's rubric judge (phase-3 plan section 9) ---
    # Empty means "use LLM_MODEL_CHEAP", which is exactly what section 9
    # specifies -- so out of the box nothing changes.
    #
    # It exists as its own knob because the cheap model is the same
    # Cydonia fine-tune the harness is grading, and the rubric items it
    # scores (persona voice, respected boundaries) are what block a
    # persona.md change from shipping. A judge that shares a family
    # with the candidate is a weak judge, and swapping it should be one
    # env var rather than a code change. Read only by eval/, never by
    # the bot.
    LLM_MODEL_JUDGE: str = ""

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

    # --- 2c: the post-turn extractor (phase-2 plan sections 2 and 8) ---
    # Confidence at or above which an extracted identity/preference/event
    # memory is written without asking. Rule memories are never
    # auto-written at any confidence -- they become proposals.
    MEMORY_AUTOWRITE_MIN_CONF: float = 0.8
    # Whether to send a strict json_schema response_format on extractor
    # calls. OpenRouter reports Cydonia's provider supports it, but that
    # is a declared capability, not a verified one. Turning this off
    # falls back to asking for JSON in the prompt alone; app/core/
    # extract.py parses and validates identically either way, so this
    # changes how often the model returns something usable, never
    # whether bad output could be applied.
    LLM_STRUCTURED_OUTPUTS: bool = True

    # --- 2e: the welfare check (phase-2 plan sections 2 and 10) ---
    # Confidence at or above which a `real` verdict drops the persona.
    # 0.6 is deliberately low: the classifier prompt already tells the
    # model to choose `real` when torn, and the cost of a false positive
    # (a warm out-of-character message the user waves away with a
    # button) is far smaller than the cost of a false negative.
    WELFARE_MIN_CONF: float = 0.6
    # The classifier runs beside the main generation, so this is the
    # extra latency ceiling it can add, not a total. On timeout the
    # normal reply goes out -- the check fails open for chat.
    WELFARE_TIMEOUT_SECONDS: float = 8.0

    # --- 3a: proactive outbound (phase-3 plan section 2) ---
    # The global kill switch. False stops every unsolicited message at
    # the first gate check (app/core/outbound_gate.py), planning
    # included, without touching any other setting -- so turning the
    # whole feature off is one env var and a restart, not a rollback.
    OUTBOUND_ENABLED: bool = True

    # The two fixed intents. Wall-clock times in user_state.timezone,
    # not UTC: 09:00 means what the user's phone says, on both DST days.
    MORNING_TIME: datetime.time = datetime.time(9, 0)
    EVENING_TIME: datetime.time = datetime.time(22, 0)
    # How late a fixed intent may still fire -- after a redeploy, a
    # crash, or a /quiet that expires mid-morning. The evening nag's
    # effective window is shorter: it is clamped to QUIET_START, since
    # a nag that arrives during quiet hours is exactly what quiet hours
    # are for (plan section 2).
    SEND_GRACE_MIN: int = 180

    # Quiet hours, local wall clock, wrapping past midnight. Compared
    # as clock faces rather than instants (see app/core/clock.py's
    # within_window) -- "nothing after half ten at night" is a
    # statement about the user's clock, and stays true across DST with
    # no special handling.
    QUIET_START: datetime.time = datetime.time(22, 30)
    QUIET_END: datetime.time = datetime.time(8, 0)

    # The budget: at most this many unsolicited messages per local day,
    # all kinds together.
    MAX_UNSOLICITED_PER_DAY: int = 3
    # If the last unsolicited message is still unanswered, wait at
    # least this long before sending another.
    MIN_GAP_UNANSWERED_H: int = 8
    # After this many unanswered in a row, go silent until the user
    # writes -- fixed intents included (plan section 11). With
    # MAX_UNSOLICITED_PER_DAY at 3 this is roughly one day of being
    # ignored, which is the intent: the bot notices and stops.
    MAX_IGNORED_IN_ROW: int = 3
    # Silence longer than this, with focus on, earns one calm nudge.
    SILENCE_NUDGE_H: int = 48

    # Local hours at which the optional tick is *considered*. Being in
    # this list buys a model call to decide, not a message.
    # NoDecode because pydantic-settings JSON-decodes any complex-typed
    # env value *before* field validators run, so without it
    # `TICK_HOURS=10,12,14` dies in json.loads and the validator below
    # never sees the string.
    TICK_HOURS: Annotated[tuple[int, ...], NoDecode] = (10, 12, 14, 16, 18, 20)
    TICK_MAX_PER_DAY: int = 1
    # The user wrote this recently -> there is nothing to re-open.
    TICK_SKIP_IF_ACTIVE_H: int = 2

    # Planned sends are scattered across this many minutes so the bot
    # does not arrive at exactly 09:00:00 every single day. Set to 0
    # for manual phone testing, where predictability beats texture.
    JITTER_MAX_MIN: int = 15

    # After a welfare trigger, the two discretionary kinds (silence,
    # tick) stay off this long. Morning and evening are part of the
    # agreed routine and resume with the persona.
    WELFARE_COOLDOWN_H: int = 24
    # The ceiling on a single /quiet, so a fat-fingered "/quiet 30d"
    # cannot mute the bot for a month.
    QUIET_MAX_DAYS: int = 7

    @field_validator("TICK_HOURS", mode="before")
    @classmethod
    def _parse_tick_hours(cls, value):
        """Accept "10,12,14" from the environment.

        pydantic-settings expects JSON list syntax for a tuple-typed
        field read from env, so `TICK_HOURS=10,12,14` would otherwise
        fail to parse -- and the plan writes it exactly that way.
        Sorted and deduped so the heartbeat can trust the order, and
        range-checked because an hour of 25 is a typo that would
        silently disable a tick slot forever.
        """
        if isinstance(value, str):
            parts = [part.strip() for part in value.split(",") if part.strip()]
            try:
                hours = [int(part) for part in parts]
            except ValueError as exc:
                raise ValueError(
                    "TICK_HOURS must be comma-separated local hours, e.g. 10,12,14"
                ) from exc
        elif isinstance(value, (list, tuple)):
            hours = [int(part) for part in value]
        else:
            return value
        for hour in hours:
            if not 0 <= hour <= 23:
                raise ValueError(f"TICK_HOURS entries must be 0-23, got {hour}")
        return tuple(sorted(set(hours)))

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
