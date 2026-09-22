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
    "LLM_MODEL_SAFETY",
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
    # Verified 2026-09-22 against OpenRouter's live model endpoint for
    # thedrummer/cydonia-24b-v4.1 (sole provider: Parasail). A fallback
    # only -- when OpenRouter reports usage.cost, that figure wins and
    # these are never consulted; spend_ledger.cost_source records which
    # of the two priced each row (H4).
    LLM_PRICE_IN: float = 0.30
    LLM_PRICE_CACHED: float = 0.15
    LLM_PRICE_OUT: float = 0.50
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
    # Same model as LLM_MODEL today, hence the same prices; verified
    # 2026-09-22.
    LLM_CHEAP_PRICE_IN: float = 0.30
    LLM_CHEAP_PRICE_CACHED: float = 0.15
    LLM_CHEAP_PRICE_OUT: float = 0.50
    # Background calls are bounded much tighter than a chat turn: a scene
    # summary is capped at 5 sentences by its prompt, so 400 tokens is
    # slack, not a target. Low temperature because none of the background
    # calls (summary now; extractor and welfare later) want invention.
    LLM_CHEAP_MAX_TOKENS: int = 400
    LLM_CHEAP_TEMPERATURE: float = 0.3

    # --- H2: the safety model (hardening pass) -------------------------
    #
    # The welfare classifier, the post-turn extractor and the tick
    # decision all ran on LLM_MODEL_CHEAP, which defaults to the same
    # Cydonia roleplay fine-tune as the persona. Three calls whose whole
    # job is to emit a strict JSON verdict were running on a model tuned
    # for prose, and a parse failure was indistinguishable from a clean
    # "nothing wrong" -- so the welfare check could be dead without
    # anything looking wrong.
    #
    # Scene summaries stay on the cheap model: a summary is prose, and it
    # is the one background call that wants the persona's own voice.
    #
    # Why gemini-2.5-flash-lite and not a cheaper nano-class model:
    # openai/gpt-5-nano is cheaper per token but accepts no `temperature`
    # on any of its endpoints, and OpenRouterProvider always sends one.
    # Combined with require_parameters (app/llm/openrouter.py), which is
    # already set for every json_schema call, that leaves zero eligible
    # endpoints -- the classifier would fail 100% of the time. Its
    # reasoning is also mandatory, which is a latency risk against
    # WELFARE_TIMEOUT_SECONDS below. Flash-Lite takes a temperature,
    # advertises structured_outputs, and makes reasoning optional.
    #
    # Prices verified on OpenRouter's live model endpoint 2026-09-22:
    # https://openrouter.ai/google/gemini-2.5-flash-lite
    LLM_MODEL_SAFETY: str = "google/gemini-2.5-flash-lite"
    LLM_SAFETY_PRICE_IN: float = 0.10
    LLM_SAFETY_PRICE_CACHED: float = 0.01
    LLM_SAFETY_PRICE_OUT: float = 0.40
    LLM_SAFETY_MAX_TOKENS: int = 400
    # Zero, not merely low. These three calls are classifications, and a
    # classifier that answers differently on a re-run cannot be reasoned
    # about -- least of all the one deciding whether someone is in real
    # distress.
    LLM_SAFETY_TEMPERATURE: float = 0.0

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
    #
    # H5 gave it a real default. Empty meant LLM_MODEL_CHEAP, which
    # defaults to the same Cydonia fine-tune as LLM_MODEL -- so out of
    # the box the harness had Cydonia grading Cydonia on "did it keep the
    # persona's voice" and "did it respect the boundaries", which is not
    # a weak check so much as no check. gpt-4.1-nano is a different lab
    # from both the persona model and the safety model, takes a
    # temperature, and supports strict json_schema, which the judge needs
    # (eval/judge.py fails closed on unusable output).
    #
    # Prices verified 2026-09-22 at https://openrouter.ai/openai/gpt-4.1-nano
    # ($0.10 in / $0.40 out per million). The harness makes ~13 judge
    # calls a run, so this is cents.
    #
    # eval/run.py refuses a blocking run when this resolves to LLM_MODEL
    # anyway, so setting it back to "" does not silently restore the old
    # behaviour -- it stops the run with exit code 3.
    LLM_MODEL_JUDGE: str = "openai/gpt-4.1-nano"

    # --- 5a: voice, mood and nicknames (phase-5 plan section 2) ---
    # Paths are resolved against the repo root (app/core/prompt.py's
    # REPO_ROOT), not the process's cwd, so a deploy that starts the
    # bot from a different working directory still finds them.
    #
    # An empty NICKNAMES_FILE means nicknames are never used -- the
    # plan states this explicitly, and app/core/voice.py's loader
    # treats a file with no lines exactly like a file with none that
    # pass the comment/blank filter, so no separate flag is needed.
    NICKNAMES_FILE: str = "persona/nicknames.txt"
    # Share of persona replies that carry a nickname. 0 means never,
    # 1 means every reply that has one to give -- both ends are valid
    # configurations, not errors, so the validator only rejects outside
    # [0, 1].
    NICKNAME_RATE: float = 0.5
    VOICE_FILE: str = "persona/voice.md"
    # How many lines of voice.md app/core/voice.py samples per scene.
    # Capped at the file's own length there, so this number is a
    # ceiling, not a promise.
    VOICE_PER_SCENE: int = 4

    @field_validator("NICKNAME_RATE")
    @classmethod
    def _rate_in_unit_interval(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"NICKNAME_RATE must be between 0 and 1, got {value}")
        return value

    @field_validator("VOICE_PER_SCENE")
    @classmethod
    def _voice_per_scene_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError(f"VOICE_PER_SCENE must be >= 0, got {value}")
        return value

    # --- 5b: the notebook (implementation plan §"Files") ---
    # Per-kind caps on *active* entries. All four validated >= 1: a cap
    # of 0 would mean "this kind can never hold an entry", which is a
    # different feature (turning a kind off) wearing a cap's clothes,
    # and app/core/notebook.py's per-kind cap assumes there is always
    # room for at least one entry once the oldest anchor-sourced one is
    # closed.
    NOTEBOOK_MAX_INTENTIONS: int = 4
    NOTEBOOK_MAX_OBSERVATIONS: int = 4
    NOTEBOOK_MAX_THREADS: int = 6
    # How long an open_thread may sit unresolved before the daily sweep
    # (app/core/notebook.py's run_notebook_expiry) closes it with
    # closed_by='expiry'. Intentions and observations have no TTL --
    # only a thread is "something to ask about later", which is exactly
    # the shape that goes stale.
    NOTEBOOK_THREAD_TTL_DAYS: int = 21

    @field_validator(
        "NOTEBOOK_MAX_INTENTIONS",
        "NOTEBOOK_MAX_OBSERVATIONS",
        "NOTEBOOK_MAX_THREADS",
        "NOTEBOOK_THREAD_TTL_DAYS",
    )
    @classmethod
    def _notebook_settings_at_least_one(cls, value: int, info) -> int:
        if value < 1:
            raise ValueError(f"{info.field_name} must be >= 1, got {value}")
        return value

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

    # --- 4a: the research loop (phase-4 plan section 3) ---
    #
    # The master switch. Everything in phase 4 no-ops when it is false,
    # and it stays false through milestones 4a-4c: the tables, the
    # fetcher and the commands all ship dark and get flipped on once
    # 4d's eval cases pass.
    RESEARCH_ENABLED: bool = False

    # Two quotas, because the two commands cost differently. A /study
    # job is one or two search calls plus two distills; a /read is a
    # single distill on a page the user already chose.
    RESEARCH_JOBS_PER_DAY: int = 1
    RESEARCH_READS_PER_DAY: int = 3
    # Per /study job. A search that finds nothing gets one reformulation
    # (plan section 6), so the floor for a useful job is 2.
    RESEARCH_MAX_SEARCHES: int = 4
    # Pages fetched per /study job. Two clips at ~5k input tokens each
    # is the bulk of a job's cost.
    RESEARCH_MAX_PINS: int = 2
    # Per job, and additionally counted against DAILY_USD_CAP -- a job
    # that hits either one stops and keeps the cards it already made
    # (plan section 12).
    RESEARCH_JOB_USD_CAP: float = 0.10

    RESEARCH_CARDS_MIN: int = 3
    RESEARCH_CARDS_MAX: int = 6
    # A pending card is a question waiting for an answer. After two
    # weeks the answer is "no" by default: the sweep marks it expired
    # rather than leaving /notes to accumulate forever.
    RESEARCH_CARD_TTL_DAYS: int = 14
    # Adopted techniques injected per turn. Kept small on purpose --
    # these compete with retrieved memories for the same attention, and
    # the persona is not a reference manual.
    RESEARCH_TECHNIQUES_IN_PROMPT: int = 2

    # The three /study packets. Comma-separated domains, parsed by the
    # validator below. GUIDES ships empty and /study guides refuses
    # until it is set -- picking those domains is the user's call, not a
    # default we invent.
    PACKET_FORUMS: Annotated[tuple[str, ...], NoDecode] = ("reddit.com",)
    PACKET_REF: Annotated[tuple[str, ...], NoDecode] = (
        "ru.wikipedia.org",
        "fr.wikipedia.org",
        "en.wikipedia.org",
    )
    PACKET_GUIDES: Annotated[tuple[str, ...], NoDecode] = ()

    # Fetcher limits (plan section 5). These are the numbers
    # app/research/fetch.py enforces; it takes them as arguments rather
    # than reading Settings, so the whole module stays testable without
    # an environment.
    FETCH_TIMEOUT_S: float = 10.0
    FETCH_MAX_BYTES: int = 2_000_000
    FETCH_MAX_REDIRECTS: int = 3
    # Characters of extracted text passed to distill. Roughly 5k tokens
    # of Russian, which is the input side of the cost estimate in plan
    # section 13.
    FETCH_MAX_CHARS: int = 15_000
    # Honest and identifiable, with no browser string anywhere in it.
    # Plan section 5.9 forbids spoofing this when a site blocks us.
    FETCH_USER_AGENT: str = (
        "AnchorBot/1.0 (personal, single-user; contact via repo owner)"
    )

    @field_validator("PACKET_FORUMS", "PACKET_REF", "PACKET_GUIDES", mode="before")
    @classmethod
    def _parse_packet(cls, value):
        """Accept "a.com,b.com" from the environment, and refuse junk loudly.

        Same NoDecode problem as TICK_HOURS: pydantic-settings wants
        JSON list syntax for a tuple-typed field read from env, and the
        plan writes these as bare comma-separated domains.

        Lowercased and deduped, order preserved, because
        app/research/addresses.py compares against these case-
        insensitively and a duplicate would silently shrink the packet.
        A scheme or a path in a packet entry is refused rather than
        stripped: `https://reddit.com/r/x` in a packet means somebody
        expected path filtering, and quietly turning it into
        `reddit.com` would admit the whole site instead.
        """
        if isinstance(value, str):
            parts = [part.strip() for part in value.split(",") if part.strip()]
        elif isinstance(value, (list, tuple)):
            parts = [str(part).strip() for part in value if str(part).strip()]
        else:
            return value
        domains: list[str] = []
        for part in parts:
            domain = part.lower().rstrip(".")
            if "/" in domain or ":" in domain:
                raise ValueError(
                    f"packet entries must be bare domains, got {part!r} -- "
                    "no scheme, port or path"
                )
            if "." not in domain:
                raise ValueError(f"packet entries must be domains, got {part!r}")
            if domain not in domains:
                domains.append(domain)
        return tuple(domains)

    @field_validator("PACKET_GUIDES")
    @classmethod
    def _cap_guides(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Plan section 3 caps this packet at five domains.

        Only this one: forums and ref are fixed lists this repo chose,
        while guides is the open slot the user fills, and an open slot
        with no ceiling is how a packet becomes "the web".
        """
        if len(value) > 5:
            raise ValueError(f"PACKET_GUIDES takes at most 5 domains, got {len(value)}")
        return value

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
