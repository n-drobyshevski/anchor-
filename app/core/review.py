"""The weekly review (phase-5 plan sections 3 and 8; milestone 5d).

**What this is, in one sentence.** Once a week (or on demand via
`/review`), the safety model looks back at the local Monday-to-now
window and returns a short, validated analysis; the persona model then
turns it into a short message (generated and sent elsewhere -- see
below); and the analysis's own `intentions` and `proposals` become
notebook entries, standing-order proposals and persona-amendment
proposals, each through the module that already owns that write.

**Welfare never reaches this module's input.** `load_week()` excludes
any scene that contains a `kind='welfare'` message, the same filter
app/core/notebook.py's reflection job applies for the same reason (see
that module's own docstring) -- belt and braces, because a welfare
trigger's neighbouring turns can carry its content by implication.

**Why the persona message is generated elsewhere.** This module may
write only `WeeklyReview`, `ReviewProposal` and `SpendLedger`
(tests/test_autonomy_isolation.py's `OWN_TABLE_WRITES`), and it must
never import `app.core.outbound_send` or anything under `app.tg` (the
same `FORBIDDEN_IMPORTS`/`FORBIDDEN_PREFIXES` every other autonomy
module is checked against). Building and sending the actual persona
message needs both -- `outbound_send.build_outbound_messages` for the
prompt, `app.tg.review` for the Telegram card -- so `run_review()`
below does everything *except* that: it analyzes, validates, stores the
row, rotates this week's review intentions, and creates the proposal
rows (including, for a `standing_order` item, its own `StandingOrder`
row via `app/core/orders.py`). The caller -- `app/core/outbound_send.py`'s
`run_send_outbound` for the scheduled path, `app/tg/review.py`'s
`run_review_command` for `/review` -- generates the message, stores it,
and calls `set_message_id()` with the id it got back.

**Standing-order proposals have no cadence in the model's own output**
(the plan's analysis schema is `{"kind", "text", "reason"}`, with no
cadence field -- unlike the extractor's own `standing_order` proposal
item, which does carry one). `create_proposals()` below defaults every
review-authored order proposal to `daily`: the review only fires once a
week, so a `once` or `weekly` cadence would need the model to also name
a weekday it is never asked for, and `daily` is the safest reading of
"держать [it] as a habit" that a one-line proposal text usually means.
`orders.propose()` still screens it like any other model output, and
the user always sees the exact cadence before accepting.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging

from sqlalchemy import select
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core import clock as clock_module
from app.core import notebook as notebook_module
from app.core import orders as orders_module
from app.core import safety_events
from app.core.clock import Clock
from app.core.extract import parse_json
from app.core.screen import screen
from app.core.spend import check_cap, priced
from app.db.models import (
    Checkin,
    Journal,
    Message,
    PersonaAmendment,
    ReviewProposal,
    Scene,
    SpendLedger,
    StateChange,
    WeeklyReview,
)
from app.llm.provider import JSONSchema, LLMMessage, LLMProvider

logger = logging.getLogger(__name__)

# The daily sweep's job kind (plan section 8's "review_expiry" -- see
# app/core/scheduler.py's maybe_enqueue_review_expiry and
# app/worker.py's dispatch). Lives here, mirroring NOTEBOOK_EXPIRY and
# ORDERS_EXPIRY: the constant belongs with the job body.
REVIEW_EXPIRY = "review_expiry"

# Ledger categories (plan section 12's invariant list): the safety-model
# analysis call, and the persona-model message that follows it. Two
# categories, not one, because they are different models and different
# kinds of cost -- exactly the split "outbound" already has from "reflect"
# and "extractor".
REVIEW_CATEGORY = "review"
REVIEW_MSG_CATEGORY = "review_msg"

# Skip reason when the analysis could not be produced (cap, or an
# unparseable reply) -- app/core/outbound_send.py's WEEKLY_REVIEW branch
# marks the row `skipped` with this reason. There is no fallback
# message (implementation plan's "Send path").
REVIEW_UNAVAILABLE = "review_unavailable"

STANDING_ORDER = "standing_order"
PERSONA_NOTE = "persona_note"
PROPOSAL_KINDS = (STANDING_ORDER, PERSONA_NOTE)

PENDING = "pending"
ADOPTED = "adopted"
REJECTED = "rejected"
EXPIRED = "expired"
PROPOSAL_STATUSES = (PENDING, ADOPTED, REJECTED, EXPIRED)

# See this module's own docstring on why a review-proposed order always
# gets this cadence.
DEFAULT_ORDER_CADENCE = orders_module.DAILY

# How long a pending review_proposal survives before the daily sweep
# expires it (plan section 8's "A daily sweep (review_expiry) marks
# pending proposals older than 7 days as expired").
PROPOSAL_TTL_DAYS = 7

# --- validation limits (implementation plan's "Validation"), verbatim ----
WINS_MAX = 3
MISSES_MAX = 3
PATTERNS_MAX = 2
INTENTIONS_MAX = 3
PROPOSALS_MAX = 2
BULLET_TEXT_MAX = 160  # wins / misses / patterns
INTENTION_TEXT_MAX = 240
PROPOSAL_TEXT_MAX = 200
PROPOSAL_REASON_MAX = 160

REVIEW_SCHEMA = JSONSchema(
    name="anchor_weekly_review",
    strict=True,
    schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["wins", "misses", "patterns", "intentions", "proposals"],
        "properties": {
            "wins": {"type": "array", "items": {"type": "string"}},
            "misses": {"type": "array", "items": {"type": "string"}},
            "patterns": {"type": "array", "items": {"type": "string"}},
            "intentions": {"type": "array", "items": {"type": "string"}},
            "proposals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "text", "reason"],
                    "properties": {
                        "kind": {"type": "string", "enum": list(PROPOSAL_KINDS)},
                        "text": {"type": "string"},
                        "reason": {"type": ["string", "null"]},
                    },
                },
            },
        },
    },
)

# Plan section 8, verbatim.
REVIEW_ANALYSIS_PROMPT = (
    "Подведи неделю пользователя по данным. Только факты из данных. "
    "`intentions` — на чём Anchor стоит сосредоточиться на следующей неделе "
    "(формулировки о поддержке и ясности, не об ужесточении). `persona_note` — "
    "короткая поправка к стилю Anchor, которую подсказывает неделя (например, "
    "«меньше вопросов по утрам»). Запрещено: здоровье, кризисы, психологические "
    "ярлыки, повышение интенсивности, наказания."
)


@dataclasses.dataclass(frozen=True)
class Analysis:
    """Validated, ready-to-apply output of one weekly-review analysis call."""

    wins: list[str] = dataclasses.field(default_factory=list)
    misses: list[str] = dataclasses.field(default_factory=list)
    patterns: list[str] = dataclasses.field(default_factory=list)
    intentions: list[str] = dataclasses.field(default_factory=list)
    proposals: list[dict] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True)
class CreatedProposal:
    """One `review_proposal` row, plus the `standing_order` id it also
    created, when its kind is `standing_order` (else None). Handed back
    so app/tg/review.py can render each card without a second query --
    an `so:*` callback needs the *order's* id, not the proposal's, and
    an `am:*` callback needs the reverse.
    """

    proposal: ReviewProposal
    order_id: int | None = None


@dataclasses.dataclass(frozen=True)
class ReviewOutcome:
    """What `run_review()` produced, for the caller to generate and send
    the actual message from."""

    available: bool
    review_id: int | None = None
    week_start: datetime.date | None = None
    note: str = ""
    proposals: tuple[CreatedProposal, ...] = ()


def week_start_for(local_date: datetime.date) -> datetime.date:
    """The local Monday of the week `local_date` falls in (plan section 8:
    "The week: from the local Monday to now. week_start is that
    Monday."). `isoweekday()` is 1 for Monday, so subtracting
    `isoweekday() - 1` days lands on it from any day of the week.

    Deliberately re-derived here rather than imported from
    app/core/outbound.py's own `week_start_for` -- that module is not on
    this one's forbidden-import list, but importing it anyway would blur
    the isolation boundary this module's docstring exists to keep clean
    for one three-line function neither module can safely own for the
    other.
    """
    return local_date - datetime.timedelta(days=local_date.isoweekday() - 1)


def _clean(value, limit: int) -> str | None:
    """A non-empty string within `limit`, or None. Never truncates."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > limit:
        return None
    return text


def _clean_list(raw, *, limit_count: int, limit_len: int) -> list[str]:
    out: list[str] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if len(out) >= limit_count:
            break
        text = _clean(item, limit_len)
        if text is None:
            continue
        result = screen(text)
        if not result.ok:
            # Implementation plan's "Validation": an intensity or high
            # risk hit drops the item, not the whole payload.
            continue
        out.append(text)
    return out


def validate(payload: dict) -> Analysis:
    """Re-check every field of the model's output, trusting nothing.

    Lengths and counts per the implementation plan's limits; `screen()`
    (injection, redaction, the risk rules) on every string. A proposal
    whose `text` -- or whose `reason`, when given -- fails `screen()` is
    dropped whole, the same "drop, never trust" posture
    app/core/notebook.py's `validate()` takes for a reflection `add`.
    """
    wins = _clean_list(payload.get("wins"), limit_count=WINS_MAX, limit_len=BULLET_TEXT_MAX)
    misses = _clean_list(payload.get("misses"), limit_count=MISSES_MAX, limit_len=BULLET_TEXT_MAX)
    patterns = _clean_list(
        payload.get("patterns"), limit_count=PATTERNS_MAX, limit_len=BULLET_TEXT_MAX
    )
    intentions = _clean_list(
        payload.get("intentions"), limit_count=INTENTIONS_MAX, limit_len=INTENTION_TEXT_MAX
    )

    proposals: list[dict] = []
    raw_proposals = payload.get("proposals")
    if isinstance(raw_proposals, list):
        for item in raw_proposals:
            if len(proposals) >= PROPOSALS_MAX:
                break
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            if kind not in PROPOSAL_KINDS:
                continue
            text = _clean(item.get("text"), PROPOSAL_TEXT_MAX)
            if text is None:
                continue
            result = screen(text)
            if not result.ok:
                continue
            reason = _clean(item.get("reason"), PROPOSAL_REASON_MAX)
            if reason is not None:
                reason_result = screen(reason)
                if not reason_result.ok:
                    continue
            proposals.append({"kind": kind, "text": text, "reason": reason})

    return Analysis(
        wins=wins, misses=misses, patterns=patterns, intentions=intentions, proposals=proposals
    )


def render_note(analysis: Analysis) -> str:
    """The `{note}` the outbound send's hidden flag carries: wins, misses
    and patterns as bullets (implementation plan's "Send path" step 2)."""
    lines: list[str] = []
    for label, items in (
        ("Победы", analysis.wins),
        ("Промахи", analysis.misses),
        ("Паттерны", analysis.patterns),
    ):
        if items:
            lines.append(f"{label}:")
            lines.extend(f"- {item}" for item in items)
    return "\n".join(lines)


def _analysis_json(analysis: Analysis) -> dict:
    """The `weekly_review.analysis` jsonb payload."""
    return {
        "wins": list(analysis.wins),
        "misses": list(analysis.misses),
        "patterns": list(analysis.patterns),
        "intentions": list(analysis.intentions),
        "proposals": [dict(item) for item in analysis.proposals],
    }


# --- reading the week (analyze_week's own input) ---------------------------


async def _week_checkins(
    session: AsyncSession, week_start: datetime.date, today: datetime.date
) -> list[tuple[Checkin, list[tuple[str, str]]]]:
    result = await session.execute(
        select(Checkin)
        .where(Checkin.local_date >= week_start)
        .where(Checkin.local_date <= today)
        .order_by(Checkin.local_date)
    )
    rows = list(result.scalars().all())
    out = []
    for row in rows:
        results = await orders_module.results_for_checkin(session, row.id)
        out.append((row, results))
    return out


async def _week_journal(
    session: AsyncSession, week_start: datetime.date, today: datetime.date
) -> list[str]:
    result = await session.execute(
        select(Journal.text)
        .where(Journal.local_date >= week_start)
        .where(Journal.local_date <= today)
        .order_by(Journal.local_date, Journal.id)
    )
    return [row[0] for row in result.all()]


async def _week_scene_summaries(
    session: AsyncSession, *, start_at: datetime.datetime, end_at: datetime.datetime
) -> list[str]:
    """Closed scenes' summaries within the week, excluding any scene that
    contains a welfare message -- see the module docstring."""
    result = await session.execute(
        select(Scene.id, Scene.summary)
        .where(Scene.ended_at.is_not(None))
        .where(Scene.summary.is_not(None))
        .where(Scene.ended_at >= start_at)
        .where(Scene.ended_at < end_at)
        .order_by(Scene.ended_at)
    )
    rows = result.all()
    if not rows:
        return []
    scene_ids = [scene_id for scene_id, _ in rows]
    welfare_result = await session.execute(
        select(Message.scene_id)
        .where(Message.scene_id.in_(scene_ids))
        .where(Message.kind == "welfare")
        .distinct()
    )
    welfare_scene_ids = {row[0] for row in welfare_result.all()}
    return [summary for scene_id, summary in rows if scene_id not in welfare_scene_ids]


async def _week_streak_history(
    session: AsyncSession, *, start_at: datetime.datetime, end_at: datetime.datetime
) -> list[str]:
    result = await session.execute(
        select(StateChange.old_value, StateChange.new_value)
        .where(StateChange.field == "streak")
        .where(StateChange.created_at >= start_at)
        .where(StateChange.created_at < end_at)
        .order_by(StateChange.created_at)
    )
    return [f"{old or '0'} -> {new or '0'}" for old, new in result.all()]


async def _active_amendment_texts(session: AsyncSession) -> list[str]:
    """Active persona amendments, texts only -- a plain SELECT, never a
    write, so importing app/core/amendments.py (which would risk a
    cycle, since it imports this module for mark_proposal) is not
    needed."""
    result = await session.execute(
        select(PersonaAmendment.text)
        .where(PersonaAmendment.status == "active")
        .order_by(PersonaAmendment.id)
    )
    return [row[0] for row in result.all()]


def render_week_input(
    *,
    week_start: datetime.date,
    today: datetime.date,
    checkins: list[tuple[Checkin, list[tuple[str, str]]]],
    journal_lines: list[str],
    scene_summaries: list[str],
    streak_history: list[str],
    notebook_view: notebook_module.NotebookView,
    order_lines: list[str],
    amendment_texts: list[str],
) -> str:
    """The user-role message `analyze_week` sends -- every read-only
    input the implementation plan's "Analysis input" list names, welfare
    excluded (see the module docstring)."""
    lines = [f"## Неделя {week_start.isoformat()} — {today.isoformat()}"]

    lines.append("")
    lines.append("## Чек-ины и договорённости")
    if checkins:
        for row, results in checkins:
            parts = [row.local_date.isoformat()]
            if row.day_rating:
                parts.append(f"день {row.day_rating}/5")
            if row.due_result:
                parts.append(f"действие: {row.due_result}")
            if results:
                tail = "; ".join(f"«{text}» — {result}" for text, result in results)
                parts.append(f"договорённости: {tail}")
            lines.append("- " + " · ".join(parts))
    else:
        lines.append("(чек-инов не было)")

    lines.append("")
    lines.append("## Журнал")
    if journal_lines:
        lines.extend(f"- {line}" for line in journal_lines)
    else:
        lines.append("(пусто)")

    lines.append("")
    lines.append("## Сессии")
    if scene_summaries:
        lines.extend(f"- {line}" for line in scene_summaries)
    else:
        lines.append("(нет закрытых сессий)")

    lines.append("")
    lines.append("## Серия")
    lines.append(", ".join(streak_history) if streak_history else "(без изменений)")

    lines.append("")
    lines.append("## Активные заметки")
    entries = [*notebook_view.intentions, *notebook_view.observations, *notebook_view.threads]
    if entries:
        lines.extend(f"- {text}" for _, text, _ in entries)
    else:
        lines.append("(пусто)")

    lines.append("")
    lines.append("## Активные договорённости")
    if order_lines:
        lines.extend(f"- {line}" for line in order_lines)
    else:
        lines.append("(нет)")

    lines.append("")
    lines.append("## Активные поправки")
    if amendment_texts:
        lines.extend(f"- {line}" for line in amendment_texts)
    else:
        lines.append("(нет)")

    return "\n".join(lines)


async def load_week(session: AsyncSession, *, clock: Clock, timezone: str) -> str:
    """Assemble the current local week's analysis input, end to end."""
    today = clock_module.local_date(clock, timezone)
    week_start = week_start_for(today)
    start_at = clock_module.combine_local(week_start, datetime.time(0, 0), timezone)
    end_at = clock_module.combine_local(
        today + datetime.timedelta(days=1), datetime.time(0, 0), timezone
    )

    checkins = await _week_checkins(session, week_start, today)
    journal_lines = await _week_journal(session, week_start, today)
    scene_summaries = await _week_scene_summaries(session, start_at=start_at, end_at=end_at)
    streak_history = await _week_streak_history(session, start_at=start_at, end_at=end_at)
    notebook_view = await notebook_module.active_entries(session)
    order_rows = await orders_module.active_orders(session)
    order_lines = [
        f"«{row.text}» ({orders_module.cadence_label(row.cadence, row.weekday)})"
        for row in order_rows
    ]
    amendment_texts = await _active_amendment_texts(session)

    return render_week_input(
        week_start=week_start,
        today=today,
        checkins=checkins,
        journal_lines=journal_lines,
        scene_summaries=scene_summaries,
        streak_history=streak_history,
        notebook_view=notebook_view,
        order_lines=order_lines,
        amendment_texts=amendment_texts,
    )


# --- the analysis call -------------------------------------------------


async def analyze_week(
    session: AsyncSession,
    settings: Settings,
    provider: LLMProvider,
    *,
    clock: Clock,
    timezone: str,
) -> Analysis | None:
    """Step 1 of the implementation plan's "Send path": the cap check,
    then the safety-model call, then validation. Ledgered under
    REVIEW_CATEGORY and recorded as a SafetyEvent of kind `review`.
    Returns None on a cap refusal or an unparseable reply -- there is no
    fallback, per the plan (`run_send_outbound`'s WEEKLY_REVIEW branch
    turns a None here into a `skipped` row with reason
    REVIEW_UNAVAILABLE).
    """
    if await check_cap(session, settings, clock, timezone):
        logger.info("weekly review skipped, daily cap reached")
        return None

    week_start = week_start_for(clock_module.local_date(clock, timezone))
    input_text = await load_week(session, clock=clock, timezone=timezone)

    response = await provider.complete(
        [
            LLMMessage(role="system", content=REVIEW_ANALYSIS_PROMPT),
            LLMMessage(role="user", content=input_text),
        ],
        conversation_id=f"anchor-review-{week_start.isoformat()}",
        json_schema=REVIEW_SCHEMA,
    )

    cost = priced(response.usage, settings, model=response.model)
    session.add(
        SpendLedger(
            local_date=clock_module.local_date(clock, timezone),
            category=REVIEW_CATEGORY,
            model=response.model,
            tokens_in=response.usage.input_tokens,
            tokens_cached=response.usage.cached_tokens,
            tokens_out=response.usage.output_tokens,
            usd_cost=cost.usd,
            cost_source=cost.source,
        )
    )
    await session.commit()

    payload = parse_json(response.text)
    await safety_events.record_in(
        session,
        clock=clock,
        timezone=timezone,
        kind=safety_events.REVIEW,
        outcome=safety_events.PARSE_FAIL if payload is None else "ok",
        model=response.model,
    )
    await session.commit()
    if payload is None:
        logger.warning("weekly review returned unparseable output")
        return None

    return validate(payload)


# --- storing the row and its proposals ----------------------------------


async def store_review(
    session: AsyncSession,
    *,
    week_start: datetime.date,
    analysis: Analysis,
    clock: Clock,
    on_demand: bool = False,
) -> WeeklyReview:
    """Insert the `weekly_review` row, or -- on `/review`'s regenerate
    path only -- upsert it and expire the previous run's pending
    proposals (implementation plan's "/review": "an upsert on
    week_start, and the previous run's pending proposals become
    expired"). The scheduled path never hits the conflict branch: the
    outbound gate's own `weekly_review` kind rule already refuses to
    plan a second row for a week that has one.

    `message_id` is set later, via `set_message_id()`, once the caller
    has actually generated and stored the persona message this row
    describes.
    """
    if on_demand:
        existing = await session.execute(
            select(WeeklyReview).where(WeeklyReview.week_start == week_start)
        )
        row = existing.scalar_one_or_none()
        if row is not None:
            await session.execute(
                sql_update(ReviewProposal)
                .where(ReviewProposal.review_id == row.id)
                .where(ReviewProposal.status == PENDING)
                .values(status=EXPIRED, decided_at=clock.now_utc())
            )
            row.analysis = _analysis_json(analysis)
            row.message_id = None
            await session.commit()
            await session.refresh(row)
            logger.info("weekly review regenerated", extra={"review_id": row.id})
            return row

    row = WeeklyReview(week_start=week_start, analysis=_analysis_json(analysis))
    session.add(row)
    await session.commit()
    await session.refresh(row)
    logger.info("weekly review stored", extra={"review_id": row.id})
    return row


async def set_message_id(session: AsyncSession, review_id: int, message_id: int) -> None:
    await session.execute(
        sql_update(WeeklyReview).where(WeeklyReview.id == review_id).values(message_id=message_id)
    )
    await session.commit()


async def apply_intentions(session: AsyncSession, settings: Settings, analysis: Analysis, *, clock: Clock) -> None:
    """Rotate this week's review intentions -- notebook.py owns the
    table (implementation plan's "Intentions": "It lives in
    notebook.py, which owns the table")."""
    await notebook_module.replace_review_intentions(session, settings, analysis.intentions, clock=clock)


async def create_proposals(
    session: AsyncSession, *, review_id: int, analysis: Analysis
) -> list[CreatedProposal]:
    """Insert `review_proposal` rows for every validated proposal, and,
    for a `standing_order` item, its own `StandingOrder` row too
    (implementation plan's "Proposals": "A review_proposal row, plus
    orders.propose(..., source='review'), plus the 5c card")."""
    rows: list[CreatedProposal] = []
    for item in analysis.proposals:
        row = ReviewProposal(
            review_id=review_id, kind=item["kind"], text=item["text"], reason=item.get("reason")
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)

        order_id: int | None = None
        if item["kind"] == STANDING_ORDER:
            order = await orders_module.propose(
                session, item["text"], DEFAULT_ORDER_CADENCE, None, source="review"
            )
            if order is not None:
                await orders_module.link_review_proposal(session, order.id, row.id)
                order_id = order.id

        rows.append(CreatedProposal(proposal=row, order_id=order_id))

    logger.info("review proposals created", extra={"review_id": review_id, "count": len(rows)})
    return rows


async def mark_proposal(
    session: AsyncSession, proposal_id: int, status: str, *, clock: Clock
) -> ReviewProposal | None:
    """`adopted` or `rejected`, for a still-`pending` proposal only.
    Returns None otherwise -- the same idempotency shape
    app/core/proposal.py's `accept`/`reject` give a replayed button
    press. Called by app/tg/review.py's `am:a`/`am:r` callbacks
    (`persona_note`) and app/tg/orders.py's `so:a`/`so:r` callbacks, via
    `standing_order.review_proposal_id`, for a review-authored order.
    """
    if status not in (ADOPTED, REJECTED):
        raise ValueError(f"mark_proposal status must be adopted or rejected, got {status!r}")
    row = await session.get(ReviewProposal, proposal_id)
    if row is None or row.status != PENDING:
        return None
    row.status = status
    row.decided_at = clock.now_utc()
    await session.commit()
    await session.refresh(row)
    logger.info("review proposal decided", extra={"proposal_id": proposal_id})
    return row


async def expire_proposals(session: AsyncSession, *, clock: Clock) -> int:
    """Daily sweep (`review_expiry`): pending proposals older than
    PROPOSAL_TTL_DAYS become `expired`."""
    cutoff = clock.now_utc() - datetime.timedelta(days=PROPOSAL_TTL_DAYS)
    now = clock.now_utc()
    result = await session.execute(
        sql_update(ReviewProposal)
        .where(ReviewProposal.status == PENDING)
        .where(ReviewProposal.created_at < cutoff)
        .values(status=EXPIRED, decided_at=now)
        .returning(ReviewProposal.id)
    )
    expired_ids = result.fetchall()
    await session.commit()
    if expired_ids:
        logger.info("review proposals expired", extra={"count": len(expired_ids)})
    return len(expired_ids)


async def run_review_expiry(session: AsyncSession, settings: Settings, *, clock: Clock) -> None:
    """The `review_expiry` job body -- plain SQL housekeeping like
    NOTEBOOK_EXPIRY/ORDERS_EXPIRY, needing neither a provider nor a bot."""
    await expire_proposals(session, clock=clock)


# --- the shared entry point ---------------------------------------------


async def run_review(
    session: AsyncSession,
    settings: Settings,
    provider: LLMProvider,
    *,
    clock: Clock,
    timezone: str,
    on_demand: bool = False,
) -> ReviewOutcome:
    """Analyze the week, store it, rotate its intentions and create its
    proposals. Shared by the scheduled send path
    (app/core/outbound_send.py's WEEKLY_REVIEW branch) and `/review`
    (app/tg/review.py, `on_demand=True`) -- see the module docstring for
    why generating and sending the actual persona message is
    deliberately *not* part of this function.
    """
    analysis = await analyze_week(session, settings, provider, clock=clock, timezone=timezone)
    if analysis is None:
        return ReviewOutcome(available=False)

    week_start = week_start_for(clock_module.local_date(clock, timezone))
    row = await store_review(
        session, week_start=week_start, analysis=analysis, clock=clock, on_demand=on_demand
    )
    await apply_intentions(session, settings, analysis, clock=clock)
    proposals = await create_proposals(session, review_id=row.id, analysis=analysis)

    return ReviewOutcome(
        available=True,
        review_id=row.id,
        week_start=week_start,
        note=render_note(analysis),
        proposals=tuple(proposals),
    )


__all__ = [
    "ADOPTED",
    "Analysis",
    "BULLET_TEXT_MAX",
    "DEFAULT_ORDER_CADENCE",
    "EXPIRED",
    "INTENTIONS_MAX",
    "INTENTION_TEXT_MAX",
    "MISSES_MAX",
    "PATTERNS_MAX",
    "PENDING",
    "PERSONA_NOTE",
    "PROPOSALS_MAX",
    "PROPOSAL_KINDS",
    "PROPOSAL_REASON_MAX",
    "PROPOSAL_STATUSES",
    "PROPOSAL_TEXT_MAX",
    "PROPOSAL_TTL_DAYS",
    "REJECTED",
    "REVIEW_ANALYSIS_PROMPT",
    "REVIEW_CATEGORY",
    "REVIEW_EXPIRY",
    "REVIEW_MSG_CATEGORY",
    "REVIEW_SCHEMA",
    "REVIEW_UNAVAILABLE",
    "ReviewOutcome",
    "STANDING_ORDER",
    "WINS_MAX",
    "analyze_week",
    "apply_intentions",
    "create_proposals",
    "expire_proposals",
    "load_week",
    "mark_proposal",
    "render_note",
    "run_review",
    "run_review_expiry",
    "set_message_id",
    "store_review",
    "validate",
    "week_start_for",
]
