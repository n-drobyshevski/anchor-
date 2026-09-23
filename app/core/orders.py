"""Negotiated standing orders (phase-5 plan sections 3 and 7; milestone 5c).

**What this is, in one sentence.** The extractor (and, from 5d, the
weekly review) may propose a recurring commitment; the user accepts it,
counters it exactly once, or declines it; the user can also author one
directly with `/order`; an active order shows up in the prompt and gets
one step in the evening check-in; a miss is only ever *mentioned*, in
the "now" block, as `Договорённости вчера: x/y` -- there is no penalty
logic anywhere in this file, and there never will be.

**An autonomy module, like app/core/mood.py, voice.py, persona_context.py
and notebook.py before it.** tests/test_autonomy_isolation.py enforces
this structurally: this module must never import `app.core.state`,
`app.core.checkin`, `app.core.outbound_gate`, `app.core.scheduler`,
`app.core.outbound_send`, `app.core.pause`, `app.core.welfare`,
`app.core.quiet`, or anything under `app.tg`. It writes exactly three
things -- `StandingOrder`, `CheckinOrderResult`, and two `UserState`
columns (`awaiting`/`awaiting_ref`) through a targeted `UPDATE`, the
same shape app/core/voice.py's `remember_nickname` uses for
`nickname_last` rather than routing through `update_state()`. The check-
in imports this module -- never the reverse -- which is what lets
`due_today`/`record_result`/`yesterday_tally` read the check-in's
world (a `local_date`, a `checkin_id`) without this module ever knowing
what a check-in *is*.

**Orders never touch `streak`, `intensity`, `focus_on`, `due_action`,
`persona_active`, or an outbound gate.** A miss is mentioned, never
enforced -- there is no code path anywhere in this module that could
lower a number or flip a flag because an order went unanswered.

**Screened regardless of author.** `app/core/screen.py` runs on every
order text -- a proposal from the extractor or the review, the user's
own `/order`, and the user's own counter text alike -- because Anchor
will *remind* the user about an active order, and a reminder must never
be able to nag toward something harmful, even if the user asked for it.
`risk_high`, `injection` and `unsafe_to_store` are refused for every
author with `REFUSAL_TEXT`. A `risk_intensity` hit is different: it
**drops** a proposal written by `anchor` or `review` (those are model
output, so the same "drop the whole item" posture app/core/notebook.py's
`validate()` takes for a reflection add), but is **allowed** on the
user's own `/order` and counter text -- the same carve-out
`notebook.add_user_intention` gives `/mind add`: "быть строже к себе"
as the user's own commitment is their call, not Anchor's to refuse.

**One negotiation round, enforced by the data as well as by code.**
`counter_of` is set only on a counter row, and `start_counter` refuses
(`"stale"`) a row that already has one -- so a counter can never itself
be countered. The counter card also simply has no «Изменить» button
(app/tg/orders.py), which is the second, belt-and-braces half of the
same rule.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import func, select
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.clock import Clock
from app.core.screen import RISK_INTENSITY, screen
from app.db.models import CheckinOrderResult, StandingOrder, UserState

logger = logging.getLogger(__name__)

# --- statuses ----------------------------------------------------------

PROPOSED = "proposed"
AWAITING_COUNTER = "awaiting_counter"
COUNTERED = "countered"
ACTIVE = "active"
DECLINED = "declined"
RETIRED = "retired"
EXPIRED = "expired"
STATUSES = (PROPOSED, AWAITING_COUNTER, COUNTERED, ACTIVE, DECLINED, RETIRED, EXPIRED)

# Statuses an unanswered proposal or an open counter may still be
# decided from -- `accept`/`decline` both work from either, since
# "Принять"/"Отклонить" on the original card and "Принять мой
# вариант"/"Отмена" on the counter card are the same two actions on
# whichever row is currently awaiting a decision.
DECIDABLE = (PROPOSED, COUNTERED)
# Statuses `expire_stale` sweeps after PROPOSAL_TTL_DAYS -- an
# unanswered proposal, an original mid-counter, or an unanswered
# counter, all alike: nobody decided, so nobody is bound.
STALE_AFTER_TTL = (PROPOSED, AWAITING_COUNTER, COUNTERED)

# --- cadences ------------------------------------------------------------

DAILY = "daily"
WEEKDAYS = "weekdays"
WEEKLY = "weekly"
ONCE = "once"
CADENCES = (DAILY, WEEKDAYS, WEEKLY, ONCE)

_WEEKDAY_NAMES = {
    1: "понедельникам",
    2: "вторникам",
    3: "средам",
    4: "четвергам",
    5: "пятницам",
    6: "субботам",
    7: "воскресеньям",
}

# Plan section 7's cadence labels for the proposal/counter cards.
CADENCE_LABELS = {DAILY: "ежедневно", WEEKDAYS: "по будням", ONCE: "один раз"}


def cadence_label(cadence: str, weekday: int | None) -> str:
    """`ежедневно` / `по будням` / `по средам` / `один раз`."""
    if cadence == WEEKLY and weekday in _WEEKDAY_NAMES:
        return f"по {_WEEKDAY_NAMES[weekday]}"
    return CADENCE_LABELS.get(cadence, cadence)


# --- user_state.awaiting -------------------------------------------------

AWAITING_SO_COUNTER = "so_counter"

# The daily expiry sweep's job kind (plan section 7's "Expiry"). Lives
# here rather than app/core/scheduler.py, mirroring app/core/notebook.py's
# own NOTEBOOK_EXPIRY -- the constant belongs with the job body, and
# app/core/scheduler.py imports it, one direction only, same as it
# already does for NOTEBOOK_EXPIRY.
ORDERS_EXPIRY = "orders_expiry"

# --- limits and the model-proposal expiry ---------------------------------

TEXT_MAX = 200
# A plain constant, not a Settings field: the implementation plan's
# config list does not name it, unlike ORDERS_MAX_ACTIVE and
# ORDERS_IN_CHECKIN_MAX, which do have their own field_validators in
# app/config.py.
PROPOSAL_TTL_DAYS = 7

# --- the Russian strings, verbatim from the plan --------------------------

REFUSAL_TEXT = "Такое не записываю — это не ко мне."
CAP_TEXT = "Сначала сними одну из договорённостей."
COUNTER_PROMPT_TEXT = "Напиши свой вариант одним сообщением."
PROPOSAL_TEXT = "Предлагаю договорённость: «{text}» ({cadence})"
COUNTER_CARD_TEXT = "Твой вариант: «{text}»"
CHECKIN_STEP_TEXT = "«{text}» — сегодня выполнено?"


def _order_result_label(result: str) -> str:
    return "да" if result == "done" else "нет"


def yesterday_line(results: list[tuple[str, str]]) -> str | None:
    """The check-in synthetic line's order tail, or None when nothing was asked.

    `« «x» — да; «y» — нет»`, one clause per answered order, in the order
    they were asked. app/core/checkin.py's `synthetic_line` appends this
    verbatim (with its own leading ` · договорённости: ` separator) to
    the day's stored check-in message.
    """
    if not results:
        return None
    return "; ".join(f"«{text}» — {_order_result_label(result)}" for text, result in results)


def yesterday_tally_text(done: int, asked: int) -> str:
    return f"{done}/{asked}"


# --- reading -----------------------------------------------------------


async def set_message_id(session: AsyncSession, order_id: int, message_id: int) -> None:
    """Remember a card's message id, mirroring app/core/proposal.py's own
    `set_message_id` -- used so a later edit (accept/decline/counter)
    lands on the right message. A targeted UPDATE of `StandingOrder`,
    the table this module already owns."""
    await session.execute(
        sql_update(StandingOrder).where(StandingOrder.id == order_id).values(tg_message_id=message_id)
    )
    await session.commit()


async def active_orders(session: AsyncSession) -> list[StandingOrder]:
    """Every active order, oldest first -- the prompt's own order and
    `/orders`' listing both read it this way."""
    result = await session.execute(
        select(StandingOrder).where(StandingOrder.status == ACTIVE).order_by(StandingOrder.id)
    )
    return list(result.scalars().all())


async def _count_active(session: AsyncSession) -> int:
    result = await session.execute(
        select(func.count()).select_from(StandingOrder).where(StandingOrder.status == ACTIVE)
    )
    return result.scalar_one()


def _is_due(order: StandingOrder, local_date: datetime.date) -> bool:
    if order.cadence == DAILY:
        return True
    if order.cadence == WEEKDAYS:
        return local_date.isoweekday() <= 5
    if order.cadence == WEEKLY:
        return local_date.isoweekday() == order.weekday
    if order.cadence == ONCE:
        # An active `once` order always has no result yet: record_result
        # retires it the moment it gets one (see that function), so by
        # the time an order is read back here as `active` and `once`, it
        # has nothing to compare a date against -- it is simply due.
        return True
    return False


async def due_today(
    session: AsyncSession, local_date: datetime.date, limit: int
) -> list[StandingOrder]:
    """Up to `limit` active orders due on `local_date`, oldest id first."""
    orders = await active_orders(session)
    due = [order for order in orders if _is_due(order, local_date)]
    return due[:limit]


async def answered_order_ids(session: AsyncSession, checkin_id: int) -> set[int]:
    """Every order id this check-in already has a result for."""
    result = await session.execute(
        select(CheckinOrderResult.order_id).where(CheckinOrderResult.checkin_id == checkin_id)
    )
    return {row[0] for row in result.all()}


async def results_for_checkin(
    session: AsyncSession, checkin_id: int
) -> list[tuple[str, str]]:
    """`[(order_text, 'done'|'no'), ...]` for this check-in, in the order
    the orders were asked (StandingOrder.id ascending, which is the same
    order `next_due_order` hands them out in). Feeds app/core/checkin.py's
    `synthetic_line`."""
    result = await session.execute(
        select(StandingOrder.text, CheckinOrderResult.result)
        .join(CheckinOrderResult, CheckinOrderResult.order_id == StandingOrder.id)
        .where(CheckinOrderResult.checkin_id == checkin_id)
        .order_by(StandingOrder.id)
    )
    return [(text, result_value) for text, result_value in result.all()]


async def next_due_order(
    session: AsyncSession, checkin_id: int, local_date: datetime.date, limit: int
) -> StandingOrder | None:
    """The next order this check-in should ask about, or None (plan
    section 7's check-in extension: "the first order due today, ordered
    by id, that has no checkin_order_result for this check-in", capped
    at `limit`)."""
    answered = await answered_order_ids(session, checkin_id)
    if len(answered) >= limit:
        return None
    for order in await due_today(session, local_date, limit):
        if order.id not in answered:
            return order
    return None


async def record_result(
    session: AsyncSession, checkin_id: int, order_id: int, result: str, *, clock: Clock
) -> None:
    """Upsert this check-in's answer for one order, retiring a `once`
    order the moment it gets its first result (plan section 7: "A
    `once` order is retired after it gets its first result")."""
    existing = await session.get(CheckinOrderResult, (checkin_id, order_id))
    if existing is None:
        session.add(CheckinOrderResult(checkin_id=checkin_id, order_id=order_id, result=result))
    else:
        existing.result = result

    order = await session.get(StandingOrder, order_id)
    if order is not None and order.cadence == ONCE and order.status == ACTIVE:
        order.status = RETIRED
        order.retired_at = clock.now_utc()

    await session.commit()
    logger.info("order result recorded", extra={"order_id": order_id})


async def yesterday_tally(session: AsyncSession, local_date: datetime.date) -> str | None:
    """`Договорённости вчера: x/y`'s value, or None when nothing was asked
    that day (plan section 7's "now" block line, omitted when empty)."""
    from app.db.models import Checkin  # local: avoids a module-level

    # dependency on the check-in's own table shape, matching this
    # module's read-only, one-way relationship with app/core/checkin.py.
    checkin_row = (
        await session.execute(select(Checkin).where(Checkin.local_date == local_date))
    ).scalar_one_or_none()
    if checkin_row is None:
        return None
    result = await session.execute(
        select(CheckinOrderResult.result).where(CheckinOrderResult.checkin_id == checkin_row.id)
    )
    results = [row[0] for row in result.all()]
    if not results:
        return None
    done = sum(1 for value in results if value == "done")
    return yesterday_tally_text(done, len(results))


# --- parsing ---------------------------------------------------------------


def parse_cadence(token: str) -> tuple[str, int | None] | None:
    """`daily` | `weekdays` | `weekly:<1-7>` | `once` -> (cadence, weekday).

    None on anything else, including a bare `weekly` with no weekday or
    an out-of-range one -- there is no cadence this function will guess
    a weekday for.
    """
    token = token.strip().lower()
    if token in (DAILY, WEEKDAYS, ONCE):
        return token, None
    if token.startswith(f"{WEEKLY}:"):
        raw = token[len(WEEKLY) + 1 :]
        if raw.isdigit():
            weekday = int(raw)
            if 1 <= weekday <= 7:
                return WEEKLY, weekday
    return None


# --- writing -----------------------------------------------------------


def _clean_text(text: str) -> str | None:
    cleaned = text.strip()
    if not cleaned or len(cleaned) > TEXT_MAX:
        return None
    return cleaned


async def _set_awaiting(session: AsyncSession, awaiting: str | None, awaiting_ref: int | None) -> None:
    """The one write this module makes to `user_state`: a targeted
    UPDATE of `awaiting`/`awaiting_ref` only -- never through
    `update_state()`, which this module may not import. Mirrors
    app/core/voice.py's `remember_nickname`. Callers commit."""
    await session.execute(
        sql_update(UserState).where(UserState.id == 1).values(
            awaiting=awaiting, awaiting_ref=awaiting_ref
        )
    )


async def propose(
    session: AsyncSession, text: str, cadence: str, weekday: int | None, *, source: str
) -> StandingOrder | None:
    """A model-authored proposal (extractor or, from 5d, the review).

    Screened as model output: any screen failure -- including a bare
    `risk_intensity` hit -- drops the whole proposal silently, the same
    posture app/core/notebook.py's `validate()` takes for a reflection
    `add`. Returns the inserted row, or None when nothing was written.
    """
    if source not in ("anchor", "review"):
        raise ValueError(f"propose() source must be anchor or review, got {source!r}")
    cleaned = _clean_text(text)
    if cleaned is None or cadence not in CADENCES:
        return None
    result = screen(cleaned)
    if not result.ok:
        logger.info("order proposal dropped", extra={"event": result.reason})
        return None

    row = StandingOrder(text=cleaned, cadence=cadence, weekday=weekday, status=PROPOSED, source=source)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    logger.info("order proposed", extra={"order_id": row.id})
    return row


CreateResult = str  # "ok" | "refused" | "cap"


async def create_active(
    session: AsyncSession,
    settings: Settings,
    text: str,
    cadence: str,
    weekday: int | None,
    *,
    source: str = "user",
    clock: Clock,
) -> CreateResult:
    """`/order <каденция> <текст>`: created directly as `active`.

    A `risk_intensity` hit is allowed here -- the user's own commitment,
    same carve-out `notebook.add_user_intention` gives `/mind add`.
    """
    cleaned = _clean_text(text)
    if cleaned is None or cadence not in CADENCES:
        return "refused"
    result = screen(cleaned)
    if not result.ok and result.reason != RISK_INTENSITY:
        return "refused"

    if await _count_active(session) >= settings.ORDERS_MAX_ACTIVE:
        return "cap"

    session.add(
        StandingOrder(
            text=cleaned,
            cadence=cadence,
            weekday=weekday,
            status=ACTIVE,
            source=source,
            decided_at=clock.now_utc(),
        )
    )
    await session.commit()
    logger.info("order created active", extra={"event": "order_create"})
    return "ok"


DecisionResult = str  # "ok" | "cap" | "stale"


async def accept(session: AsyncSession, settings: Settings, order_id: int, *, clock: Clock) -> DecisionResult:
    """«Принять» / «Принять мой вариант». Works on a `proposed` row (the
    original card) or a `countered` one (the counter card) alike -- the
    same two statuses `DECIDABLE` names.

    On `cap` the row keeps its status (plan section 7): a proposal or an
    open counter that could not be accepted must still be answerable
    later, once the user has room.
    """
    order = await session.get(StandingOrder, order_id)
    if order is None or order.status not in DECIDABLE:
        return "stale"
    if await _count_active(session) >= settings.ORDERS_MAX_ACTIVE:
        return "cap"

    order.status = ACTIVE
    order.decided_at = clock.now_utc()
    await session.commit()
    logger.info("order accepted", extra={"order_id": order_id})
    return "ok"


async def decline(session: AsyncSession, order_id: int, *, clock: Clock) -> DecisionResult:
    """«Отклонить» / «Отмена». Same two decidable statuses as `accept`."""
    order = await session.get(StandingOrder, order_id)
    if order is None or order.status not in DECIDABLE:
        return "stale"

    order.status = DECLINED
    order.decided_at = clock.now_utc()
    await session.commit()
    logger.info("order declined", extra={"order_id": order_id})
    return "ok"


async def retire(session: AsyncSession, order_id: int, *, clock: Clock) -> DecisionResult:
    """`/orders`' [Снять]: retire an active order. Never a penalty --
    the user removing their own commitment, at any time, for any reason."""
    order = await session.get(StandingOrder, order_id)
    if order is None or order.status != ACTIVE:
        return "stale"

    order.status = RETIRED
    order.retired_at = clock.now_utc()
    await session.commit()
    logger.info("order retired", extra={"order_id": order_id})
    return "ok"


async def start_counter(session: AsyncSession, order_id: int) -> DecisionResult:
    """«Изменить»: one round only.

    Refused (`"stale"`) for anything but a fresh, uncountered proposal:
    a row that already carries `counter_of` (it is itself a counter) or
    that is not `proposed` (already decided, already awaiting a counter,
    or already a counter's own target) cannot be countered again. That
    is the data half of "Anchor never counters the counter, and neither
    does a second press"; app/tg/orders.py's counter card carrying no
    «Изменить» button is the other half.
    """
    order = await session.get(StandingOrder, order_id)
    if order is None or order.status != PROPOSED or order.counter_of is not None:
        return "stale"

    order.status = AWAITING_COUNTER
    await _set_awaiting(session, AWAITING_SO_COUNTER, order_id)
    await session.commit()
    logger.info("order counter started", extra={"order_id": order_id})
    return "ok"


class CounterOutcome:
    """`submit_counter`'s result: `status` is `"ok"`, `"refused"` or
    `"stale"`; `order` is the new counter row on `"ok"`, else None."""

    __slots__ = ("status", "order")

    def __init__(self, status: str, order: StandingOrder | None = None) -> None:
        self.status = status
        self.order = order


async def submit_counter(
    session: AsyncSession, order_id: int, text: str, *, clock: Clock
) -> CounterOutcome:
    """The plain text that follows «Изменить» (app/core/turn.py step 0c).

    `order_id` is `user_state.awaiting_ref`. A leading cadence token
    (`daily`, `weekdays`, `weekly:<1-7>` or `once`) is parsed out of the
    text and used for the counter; otherwise the counter inherits the
    original's own cadence and weekday.

    Screened as the user's own text (a `risk_intensity` hit is allowed,
    same carve-out as `create_active`). A screen failure ends the
    negotiation rather than leaving it stranded: the original is marked
    `declined` and `awaiting` is cleared, so a refused counter cannot
    leave a card with a dead «Изменить» behind it.
    """
    original = await session.get(StandingOrder, order_id)
    if original is None or original.status != AWAITING_COUNTER:
        await _set_awaiting(session, None, None)
        await session.commit()
        return CounterOutcome("stale")

    parts = text.strip().split(maxsplit=1)
    parsed = parse_cadence(parts[0]) if parts else None
    if parsed is not None and len(parts) > 1:
        cadence, weekday = parsed
        body = parts[1]
    else:
        cadence, weekday = original.cadence, original.weekday
        body = text

    cleaned = _clean_text(body)
    result = screen(cleaned) if cleaned is not None else None

    if cleaned is None or (not result.ok and result.reason != RISK_INTENSITY):
        original.status = DECLINED
        original.decided_at = clock.now_utc()
        await _set_awaiting(session, None, None)
        await session.commit()
        logger.info("order counter refused", extra={"order_id": order_id})
        return CounterOutcome("refused")

    counter = StandingOrder(
        text=cleaned,
        cadence=cadence,
        weekday=weekday,
        status=COUNTERED,
        source="user",
        counter_of=original.id,
    )
    session.add(counter)
    original.status = DECLINED
    original.decided_at = clock.now_utc()
    await _set_awaiting(session, None, None)
    await session.commit()
    await session.refresh(counter)
    logger.info("order countered", extra={"order_id": counter.id})
    return CounterOutcome("ok", counter)


async def expire_stale(session: AsyncSession, *, clock: Clock) -> int:
    """Daily sweep: `proposed`/`awaiting_counter`/`countered` rows older
    than `PROPOSAL_TTL_DAYS` become `expired` (plan section 7). If
    `awaiting_ref` points at a row this sweep just expired, `awaiting`
    is cleared too, so a stale «Изменить» can never strand the next
    plain-text message as a phantom counter.
    """
    cutoff = clock.now_utc() - datetime.timedelta(days=PROPOSAL_TTL_DAYS)
    now = clock.now_utc()
    result = await session.execute(
        select(StandingOrder.id)
        .where(StandingOrder.status.in_(STALE_AFTER_TTL))
        .where(StandingOrder.created_at < cutoff)
    )
    expired_ids = [row[0] for row in result.all()]
    if not expired_ids:
        return 0

    await session.execute(
        sql_update(StandingOrder)
        .where(StandingOrder.id.in_(expired_ids))
        .values(status=EXPIRED, decided_at=now)
    )

    state = (await session.execute(select(UserState).where(UserState.id == 1))).scalar_one_or_none()
    if state is not None and state.awaiting_ref in expired_ids:
        await _set_awaiting(session, None, None)

    await session.commit()
    logger.info("orders expired", extra={"count": len(expired_ids)})
    return len(expired_ids)


__all__ = [
    "ACTIVE",
    "AWAITING_COUNTER",
    "AWAITING_SO_COUNTER",
    "ORDERS_EXPIRY",
    "CADENCES",
    "CADENCE_LABELS",
    "CAP_TEXT",
    "CHECKIN_STEP_TEXT",
    "COUNTER_CARD_TEXT",
    "COUNTER_PROMPT_TEXT",
    "COUNTERED",
    "CounterOutcome",
    "DAILY",
    "DECIDABLE",
    "DECLINED",
    "EXPIRED",
    "ONCE",
    "PROPOSAL_TTL_DAYS",
    "PROPOSAL_TEXT",
    "PROPOSED",
    "REFUSAL_TEXT",
    "RETIRED",
    "STATUSES",
    "TEXT_MAX",
    "WEEKDAYS",
    "WEEKLY",
    "accept",
    "active_orders",
    "answered_order_ids",
    "cadence_label",
    "create_active",
    "decline",
    "due_today",
    "expire_stale",
    "next_due_order",
    "parse_cadence",
    "propose",
    "record_result",
    "results_for_checkin",
    "retire",
    "set_message_id",
    "start_counter",
    "submit_counter",
    "yesterday_line",
    "yesterday_tally",
    "yesterday_tally_text",
]
