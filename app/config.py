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
import urllib.parse

from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Telegram's own charset for the webhook secret token.
_SECRET_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")

# Web-chat plan track 2: the exact shape scripts/web_passphrase.py emits
# -- "scrypt$17$8$1$<salt_b64>$<hash_b64>", where 17/8/1 are the fixed
# N-exponent/r/p cost parameters (N=2**17, OWASP-acceptable) and both
# base64 segments are URL-safe, unpadded. This is a format check only,
# independent of app/web/auth.py's own parser -- the same
# belt-and-suspenders instinct as the two independent /delete/ /export
# guards (app/web/ingress.py and app/tg/router.py): a boot-time check
# that can never be skipped by a bug in the request-time one, and vice
# versa.
_PASSPHRASE_HASH_RE = re.compile(r"^scrypt\$17\$8\$1\$[A-Za-z0-9_-]+\$[A-Za-z0-9_-]+$")

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
    "WEB_PASSPHRASE_HASH",
    # 6e: credentials and endpoints that arrive by copy-paste from a
    # Railway variables panel or an offline `age-keygen`, same reasoning
    # as OPENROUTER_API_KEY above.
    "BACKUP_AGE_RECIPIENT",
    "BACKUP_S3_ENDPOINT",
    "BACKUP_S3_BUCKET",
    "BACKUP_S3_REGION",
    "BACKUP_S3_ACCESS_KEY_ID",
    "BACKUP_S3_SECRET_ACCESS_KEY",
    "BACKUP_PG_DUMP",
    "PLANNER_MCP_URL",
    "PLANNER_SUPABASE_URL",
    "PLANNER_OAUTH_CLIENT_ID",
    "PLANNER_OAUTH_REDIRECT_URI",
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
    # Phase 5 (spec 2026-09-25): which persona file the system prompt is
    # read from, relative to the repo root like VOICE_FILE. Swapping it
    # by env keeps a private overlay out of the default branch; the code
    # stays theme-agnostic either way.
    PERSONA_FILE: str = "persona/persona.md"
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

    # --- 5c: standing orders (implementation plan §"Files") ---
    # ORDERS_MAX_ACTIVE caps how many active orders can exist at once
    # (checked on accept() and on /order); ORDERS_IN_CHECKIN_MAX caps how
    # many are asked about in a single check-in. Both >= 1 for the same
    # reason as the notebook caps above: 0 would be "the feature is off"
    # wearing a cap's clothes. The 7-day proposal expiry is a plain
    # constant (PROPOSAL_TTL_DAYS in app/core/orders.py), not a setting,
    # because the plan's config list does not name it.
    ORDERS_MAX_ACTIVE: int = 5
    ORDERS_IN_CHECKIN_MAX: int = 3

    # Phase 5 (spec 2026-09-25): the debt queue (app/core/obligations.py,
    # whose MAX_OPEN caps it at 5 open debts). DEBT_IN_PROMPT is how many
    # of the oldest the "## Долг" section shows.
    DEBT_IN_PROMPT: int = 3
    # Scarce attention (app/core/attention.py): after this many
    # in-character replies within a rolling hour, Anchor goes 'short'
    # for a deterministic LOW..HIGH minutes.
    MAX_SUBSTANTIVE_REPLIES: int = 12
    ATTENTION_SHORT_MIN_LOW: int = 20
    ATTENTION_SHORT_MIN_HIGH: int = 40

    @field_validator(
        "DEBT_IN_PROMPT",
        "MAX_SUBSTANTIVE_REPLIES",
        "ATTENTION_SHORT_MIN_LOW",
    )
    @classmethod
    def _phase5_counts_at_least_one(cls, value: int, info) -> int:
        if value < 1:
            raise ValueError(f"{info.field_name} must be >= 1, got {value}")
        return value

    @field_validator("ATTENTION_SHORT_MIN_HIGH")
    @classmethod
    def _attention_high_not_below_low(cls, value: int, info) -> int:
        low = info.data.get("ATTENTION_SHORT_MIN_LOW", 1)
        if value < low:
            raise ValueError(f"ATTENTION_SHORT_MIN_HIGH must be >= ATTENTION_SHORT_MIN_LOW ({low}), got {value}")
        return value

    @field_validator("ORDERS_MAX_ACTIVE", "ORDERS_IN_CHECKIN_MAX")
    @classmethod
    def _orders_settings_at_least_one(cls, value: int, info) -> int:
        if value < 1:
            raise ValueError(f"{info.field_name} must be >= 1, got {value}")
        return value

    # --- 5d: weekly review and persona amendments (implementation plan
    # §"Config") ---
    # ISO weekday (1=Monday .. 7=Sunday) the review is planned on, local
    # to user_state.timezone -- matches StandingOrder.weekday's own
    # convention rather than Python's Monday=0.
    REVIEW_DOW: int = 7
    REVIEW_TIME: datetime.time = datetime.time(19, 0)
    # The cap on `active` plus `trial` amendments together (plan's
    # "Adopt": "The cap counts active plus trial rows against
    # AMENDMENTS_MAX_ACTIVE"). >= 1 for the same reason as the notebook
    # and orders caps above: 0 would be "the feature is off" wearing a
    # cap's clothes.
    AMENDMENTS_MAX_ACTIVE: int = 10

    @field_validator("REVIEW_DOW")
    @classmethod
    def _review_dow_in_range(cls, value: int) -> int:
        if not 1 <= value <= 7:
            raise ValueError(f"REVIEW_DOW must be between 1 and 7, got {value}")
        return value

    @field_validator("AMENDMENTS_MAX_ACTIVE")
    @classmethod
    def _amendments_max_active_at_least_one(cls, value: int) -> int:
        if value < 1:
            raise ValueError(f"AMENDMENTS_MAX_ACTIVE must be >= 1, got {value}")
        return value

    # --- 5e: callbacks (phase-5 plan section 11a) ---
    # A candidate `event` memory must be at least this old before it is
    # eligible for "## Можно вспомнить" -- a callback to something the
    # user said an hour ago would read as the bot parroting the last
    # message back, not as it remembering. And it must not have been
    # used (delivered as a callback) in the last CALLBACK_UNUSED_DAYS,
    # so the same memory does not get recycled every few days. Both
    # >= 1 for the same reason as the notebook/orders/amendments caps
    # above: 0 would mean "any memory, however fresh, however recently
    # used", which is a different feature (no cooldown at all) wearing
    # a threshold's clothes.
    CALLBACK_MIN_AGE_DAYS: int = 7
    CALLBACK_UNUSED_DAYS: int = 14

    @field_validator("CALLBACK_MIN_AGE_DAYS", "CALLBACK_UNUSED_DAYS")
    @classmethod
    def _callback_settings_at_least_one(cls, value: int, info) -> int:
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

    # --- Grok access (docs/grok-access.md) ---
    #
    # Opt-in read access for an outside assistant (grok.com's custom MCP
    # connector). Ships off: with this false the /mcp route does not
    # exist and /grok refuses. Even when on, nothing is readable until
    # the user presses [Разрешить] on a /grok grant, and every grant
    # expires on its own.
    GROK_ACCESS_ENABLED: bool = False
    # The ceiling on one grant's lifetime, whatever the keyboard offers.
    GROK_GRANT_MAX_HOURS: int = 168
    # Per grant, a sliding one-minute window on the MCP endpoint.
    GROK_MAX_CALLS_PER_MINUTE: int = 30

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

    # --- web-chat plan track 2: the browser front end -------------------
    #
    # Default False, and nothing else about this section takes effect
    # until it is true -- app/main.py only builds the WebHub, the web
    # sink Bot and setup_web()'s routes when WEB_UI_ENABLED is set, so a
    # deploy that never opts in behaves exactly as it did before this
    # feature existed (design section 9).
    WEB_UI_ENABLED: bool = False
    # scrypt$17$8$1$<salt_b64>$<hash_b64>, generated by
    # `uv run python scripts/web_passphrase.py`. Required, and format-
    # checked against _PASSPHRASE_HASH_RE, only when WEB_UI_ENABLED is
    # true -- check_runtime_settings below never echoes the value,
    # matching TELEGRAM_SECRET_TOKEN's own discipline.
    WEB_PASSPHRASE_HASH: str = ""
    # Idle timeout and absolute ceiling for a web_session row
    # (app/web/auth.py). Both server-enforced at request time, not by a
    # database CHECK -- see app/db/models.py's WebSession docstring for
    # why a CHECK constraint cannot express "relative to now".
    WEB_SESSION_IDLE_HOURS: int = 72
    WEB_SESSION_MAX_DAYS: int = 14
    # TTL for the Telegram-delivered login code (design section 4).
    WEB_LOGIN_CODE_TTL_S: int = 300
    # --- planner P2: read path + OAuth link -----------------------------
    #
    # The master switch. False (the default) keeps every planner code
    # path dark -- no job kind is dispatched, no command does anything
    # but say "off", and build_now_block(planner=None) stays
    # byte-identical to today (app/core/prompt.py). Off through this
    # milestone's ship the same way RESEARCH_ENABLED was through 4a-4c.
    PLANNER_ENABLED: bool = False
    # The planner's MCP endpoint, e.g. https://planner.example.com/api/mcp.
    PLANNER_MCP_URL: str = ""
    # The planner's Supabase project URL. Its OAuth authorization server
    # is `${PLANNER_SUPABASE_URL}/auth/v1` (see app/planner/auth.py,
    # matching lib/mcp/env.ts's getSupabaseAuthIssuer() in the planner
    # repo) -- Anchor discovers the actual authorize/token endpoints
    # from that issuer's RFC 8414 metadata rather than guessing paths.
    PLANNER_SUPABASE_URL: str = ""
    # A public OAuth client id registered against the planner's Supabase
    # project (dynamic client registration, done once, out of band --
    # see docs/README for the exact steps). PKCE-only; no client secret.
    PLANNER_OAUTH_CLIENT_ID: str = ""
    # Anchor's own callback: https://<railway-host>/planner/oauth/callback.
    # Its host must be added, in full, to the planner's
    # MCP_ALLOWED_REDIRECT_HOSTS -- a bare "up.railway.app" there would
    # allow any Railway app (design review, table 1).
    PLANNER_OAUTH_REDIRECT_URI: str = ""
    # A snapshot older than this is treated as absent by
    # app/planner/snapshot.py's render_lines() -- the plan section
    # "degradation" requirement is that the plan section of the now-
    # block simply disappears rather than showing stale data.
    PLANNER_SNAPSHOT_MAX_AGE_MIN: int = 30
    # How often the heartbeat re-queues PLANNER_SYNC, in minutes (plus
    # one extra run ~10 minutes before MORNING_TIME, so the morning
    # message reflects a fresh agenda -- app/core/scheduler.py's
    # maybe_enqueue_planner_sync).
    PLANNER_SYNC_EVERY_MIN: int = 15
    # P3: items Anchor creates on the planner are private by default,
    # so they do not appear on the partner's calendar without opt-in
    # (design review, your decision in section 0). Read now so the
    # setting exists ahead of the write path that consumes it.
    PLANNER_WRITE_PRIVATE: bool = True
    # P4: writes proposed from ordinary chat, behind their own flag.
    # False until 4d-equivalent evals exist for this feature.
    PLANNER_INTENT: bool = False
    # P4: the safety-model intent call runs beside welfare.classify in
    # the same asyncio.gather (app/core/turn.py), so it shares that
    # call's fail-open discipline -- same shape as WELFARE_TIMEOUT_SECONDS.
    PLANNER_INTENT_TIMEOUT_SECONDS: float = 8.0
    # P3: a daily ceiling on planner writes, independent of DAILY_USD_CAP
    # -- a loop that kept proposing writes would otherwise be bounded
    # only by spend, and a stray planner_action is a calendar entry, not
    # a few cents.
    PLANNER_MAX_WRITES_PER_DAY: int = 20
    # A pending planner_action (a /task, /event or chat-proposed card
    # nobody has tapped) older than this is expired rather than shown
    # forever: without a cutoff, a card from weeks ago could still be
    # accepted and write an event in the past (design review finding 10).
    PLANNER_PENDING_TTL_HOURS: int = 24
    # Anchor plan, "Anchor" section: gates both the one extra
    # `get_health` MCP call each PLANNER_SYNC makes and the one health
    # line render_lines() adds to the now-block. Off by default --
    # sleep and heart metrics reach OpenRouter only when this is true
    # (README privacy note), same discipline as every other planner flag.
    PLANNER_HEALTH: bool = False

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

    # --- 6a: idle framework (Phase 6 plan section 2; milestone 6a) ---
    # The global kill switch, same shape as OUTBOUND_ENABLED: false stops
    # planning at the first gate row (`disabled`), with nothing else
    # touched. Idle stays under its own switch rather than OUTBOUND_
    # ENABLED's because idle never sends a message at all -- the two
    # features do not overlap.
    IDLE_ENABLED: bool = True
    # How long the user must have been silent before idle work may run.
    IDLE_AFTER_H: int = 3
    # Idle's own daily spend ceiling, separate from and inside
    # DAILY_USD_CAP -- see IDLE_RESERVE_USD below for how the two relate.
    IDLE_USD_CAP: float = 0.25
    # Always kept free for live chat: idle runs only if
    # spent_today + IDLE_JOB_USD_CAP <= DAILY_USD_CAP - IDLE_RESERVE_USD.
    # Idle can never consume the reserve, by construction of that check
    # (app/core/idle/gate.py's row 9), not by a promise here.
    IDLE_RESERVE_USD: float = 0.50
    # Per-job ceiling. A job that would cross it raises JobCapHit and
    # stops, keeping whatever it already committed.
    IDLE_JOB_USD_CAP: float = 0.05
    IDLE_MAX_JOBS_PER_DAY: int = 8
    # Local hours idle may run, "HH:MM-HH:MM", wrapping past midnight
    # exactly like QUIET_START/QUIET_END (app/core/clock.py's
    # within_window). The default admits the whole day.
    IDLE_WINDOW: str = "00:00-23:59"
    # How long a reversible, done idle_run stays undoable through /digest.
    IDLE_UNDO_DAYS: int = 7
    # How many recent persona replies the `critique` kind (6c) scores per
    # run. Read only from 6c on; 6a defines it because .env.example and
    # Settings should be complete across milestones, matching this
    # file's own convention (see the module docstring).
    CRITIQUE_SAMPLE: int = 5
    # ISO weekday (1=Monday..7=Sunday) the weekly regression canary (6c)
    # runs on, matching StandingOrder.weekday's and REVIEW_DOW's own
    # convention rather than Python's Monday=0.
    CANARY_DOW: int = 3

    @field_validator("IDLE_AFTER_H", "IDLE_MAX_JOBS_PER_DAY", "IDLE_UNDO_DAYS", "CRITIQUE_SAMPLE")
    @classmethod
    def _idle_int_settings_at_least_one(cls, value: int, info) -> int:
        if value < 1:
            raise ValueError(f"{info.field_name} must be >= 1, got {value}")
        return value

    @field_validator("IDLE_USD_CAP", "IDLE_RESERVE_USD", "IDLE_JOB_USD_CAP")
    @classmethod
    def _idle_usd_settings_positive(cls, value: float, info) -> float:
        if value < 0:
            raise ValueError(f"{info.field_name} must be >= 0, got {value}")
        return value

    @field_validator("CANARY_DOW")
    @classmethod
    def _canary_dow_in_range(cls, value: int) -> int:
        if not 1 <= value <= 7:
            raise ValueError(f"CANARY_DOW must be between 1 and 7, got {value}")
        return value

    # --- 6e: hardening (Phase 6 plan section 2; milestone 6e) -----------
    #
    # Encrypted nightly backups (§9.1). BACKUP_ENABLED is the same shape
    # as OUTBOUND_ENABLED/IDLE_ENABLED: false stops the heartbeat from
    # ever enqueueing a backup job, nothing else touched. Unlike idle,
    # the backup job itself is never gated on budget, persona state or
    # the idle window -- app/ops/backup.py's own docstring says why.
    BACKUP_ENABLED: bool = True
    BACKUP_TIME: datetime.time = datetime.time(4, 0)
    BACKUP_KEEP_DAILY: int = 14
    BACKUP_KEEP_WEEKLY: int = 8
    # Public age recipient only (age1...); the matching private key never
    # touches the server. Empty by default -- until the user generates a
    # keypair offline and sets this, app/ops/backup.py records
    # backup_log.status='failed', error_code='not_configured' every
    # night rather than crashing the heartbeat.
    BACKUP_AGE_RECIPIENT: str = ""
    BACKUP_S3_ENDPOINT: str = ""
    BACKUP_S3_BUCKET: str = ""
    # "auto" is Tigris's (Railway bucket) own convention for "the
    # endpoint decides"; a real S3-compatible provider that requires a
    # specific region can still set this explicitly.
    BACKUP_S3_REGION: str = "auto"
    BACKUP_S3_ACCESS_KEY_ID: str = ""
    BACKUP_S3_SECRET_ACCESS_KEY: str = ""
    # PATH by default. A setting rather than a hardcoded path so the
    # Dockerfile's postgresql-client-18 install can be found regardless
    # of where a given base image happens to put it, without a code
    # change -- and so a test can point it at a specific pg_dump binary
    # (e.g. /usr/lib/postgresql/18/bin/pg_dump) without touching PATH.
    BACKUP_PG_DUMP: str = "pg_dump"

    UPDATE_PAYLOAD_RETENTION_DAYS: int = 30
    JOB_RETENTION_DAYS: int = 30
    # 0 means "keep forever" -- not a threshold of zero days, an off
    # switch. app/core/retention.py's own sweep treats it that way
    # explicitly rather than the validator forbidding it, because the
    # plan's own default is 0.
    MESSAGE_RETENTION_DAYS: int = 0

    # How long the heartbeat may go stale before /readyz fails and the
    # in-process watchdog (app/worker.py) kills the process so Railway's
    # restart policy brings it back (§9.6).
    LIVENESS_STALE_MIN: int = 5

    @field_validator("BACKUP_KEEP_DAILY", "BACKUP_KEEP_WEEKLY", "LIVENESS_STALE_MIN")
    @classmethod
    def _backup_int_settings_at_least_one(cls, value: int, info) -> int:
        if value < 1:
            raise ValueError(f"{info.field_name} must be >= 1, got {value}")
        return value

    @field_validator("UPDATE_PAYLOAD_RETENTION_DAYS", "JOB_RETENTION_DAYS")
    @classmethod
    def _retention_settings_at_least_one(cls, value: int, info) -> int:
        # These two are genuinely mandatory sweeps (the plan gives them
        # no "0 = off" meaning, unlike MESSAGE_RETENTION_DAYS below), so
        # 0 is rejected the same way the notebook/orders caps reject it.
        if value < 1:
            raise ValueError(f"{info.field_name} must be >= 1, got {value}")
        return value

    @field_validator("MESSAGE_RETENTION_DAYS")
    @classmethod
    def _message_retention_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError(f"MESSAGE_RETENTION_DAYS must be >= 0, got {value}")
        return value

    @field_validator("IDLE_WINDOW")
    @classmethod
    def _idle_window_format(cls, value: str) -> str:
        """`HH:MM-HH:MM`, both halves real wall-clock times.

        Validated here (fail loudly at boot) and parsed again by
        app/core/idle/gate.py's `parse_window` (the pure function the
        gate actually calls) -- the same "settings shape is checked at
        boot, business logic re-derives it" split TICK_HOURS and the
        PACKET_* fields already use in this file.
        """
        import re as _re

        match = _re.match(r"^(\d{2}):(\d{2})-(\d{2}):(\d{2})$", value)
        if not match:
            raise ValueError(f"IDLE_WINDOW must be HH:MM-HH:MM, got {value!r}")
        sh, sm, eh, em = (int(part) for part in match.groups())
        if not (0 <= sh <= 23 and 0 <= sm <= 59 and 0 <= eh <= 23 and 0 <= em <= 59):
            raise ValueError(f"IDLE_WINDOW has an out-of-range time component: {value!r}")
        return value

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
# Only required when PLANNER_ENABLED -- the whole feature is off by
# default and must not block boot for a deployment that never sets it.
REQUIRED_PLANNER = (
    "PLANNER_MCP_URL",
    "PLANNER_SUPABASE_URL",
    "PLANNER_OAUTH_CLIENT_ID",
    "PLANNER_OAUTH_REDIRECT_URI",
)


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
    if settings.PLANNER_ENABLED:
        missing.extend(name for name in REQUIRED_PLANNER if not getattr(settings, name))
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

    # Web-chat plan track 2 (design section 9). Three checks, all fatal,
    # all reported without ever printing WEB_PASSPHRASE_HASH: a web UI
    # exposed to the public internet with no passphrase, over plaintext
    # HTTP, or attached to a transport that serves no HTTP at all, is a
    # deploy mistake worth dying loudly over rather than starting dark.
    if settings.WEB_UI_ENABLED:
        if settings.MODE != "webhook":
            raise SystemExit(
                "WEB_UI_ENABLED requires MODE=webhook. Polling mode serves no "
                "HTTP (app/main.py's _run_polling_mode never builds a web.Application), "
                "so there is nothing for the web chat to attach to."
            )
        if not _web_ui_origin_is_permitted(settings.PUBLIC_URL):
            raise SystemExit(
                "WEB_UI_ENABLED requires PUBLIC_URL to be an https:// URL "
                "(http://localhost or http://127.0.0.1 is allowed for local "
                "development only). The web login cookie is Secure and the "
                "CSRF Origin check compares against this URL's origin, so an "
                "http:// deploy target is refused rather than silently serving "
                "the login page over plaintext."
            )
        if not _PASSPHRASE_HASH_RE.match(settings.WEB_PASSPHRASE_HASH):
            raise SystemExit(
                "WEB_PASSPHRASE_HASH is missing or malformed (the value is "
                "deliberately not shown). Generate one with "
                "`uv run python scripts/web_passphrase.py` and set it in the "
                "deployment environment; the expected shape is "
                "scrypt$17$8$1$<salt>$<hash>."
            )


def _web_ui_origin_is_permitted(public_url: str) -> bool:
    """https://, or http://localhost[:port] / http://127.0.0.1[:port] for dev.

    Parsed with urllib.parse rather than a string prefix check: a naive
    `.startswith("http://localhost")` would also accept
    "http://localhost.evil.example", which is not the same host at all.
    """
    parsed = urllib.parse.urlparse(public_url.strip())
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")
