"""The daily check-in and the streak (plan sections 4 and 9).

The `checkin` row **is** the state machine. `/checkin` upserts today's
row with the answer fields nulled and each button fills one of them,
which is also why section 9's "a second check-in the same day overwrites
the first" needs no special handling: `local_date` is unique, so
starting again resets the day.

This module knows nothing about Telegram; app/tg/checkin.py owns the
keyboards and the Russian strings.

5c (phase-5 plan section 7) adds one step per active standing order due
today, between the due-action step and the note step: this module
imports app/core/orders.py -- never the reverse, which is what lets
`orders.due_today`/`next_due_order`/`record_result`/`yesterday_tally`
read a `local_date` and a `checkin_id` without app/core/orders.py ever
knowing what a check-in *is*. `synthetic_line`'s own `order_results`
parameter is the only place that dependency shows: it appends the day's
order answers to the stored check-in message, via
`orders.yesterday_line` for the actual formatting.

W4 (the web Check-in screen) moves the step rules both transports share
into this module -- `due_step_needed`, `resolve_due_result`,
`form_orders` -- and adds `submit`, which fills every step *including*
the note in one call. app/tg/checkin.py walks the same rules one button
at a time; app/web/panels/checkin.py calls `submit`. A web submission
never opens the global note step (`awaiting`): that flag is read by
whatever queued row the worker claims next, and opening it from an HTTP
handler would let an older, unrelated queued message be filed as this
check-in's note. Instead the web's one queued completion row names its
check-in by the minted message id, and `finish_submitted` finishes
exactly that check-in (app/tg/checkin.py's `finish_and_react`).
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clock as clock_module
from app.core import orders
from app.core import pause
from app.core.clock import Clock
from app.core.state import STATE_ID, get_state, update_state
from app.db.models import Checkin, StandingOrder, UserState

logger = logging.getLogger(__name__)

AWAITING_NOTE = "checkin_note"

NOTE_MAX = 500

DONE = "done"
PARTIAL = "partial"
NO = "no"
NONE = "none"
DUE_RESULTS = (DONE, PARTIAL, NO, NONE)

_DUE_LABELS = {DONE: "сделано", PARTIAL: "частично", NO: "не сделано", NONE: "нет действия"}


async def get_for_date(session: AsyncSession, local_date: datetime.date) -> Checkin | None:
    result = await session.execute(select(Checkin).where(Checkin.local_date == local_date))
    return result.scalars().first()


async def today(session: AsyncSession, clock: Clock, timezone: str) -> Checkin | None:
    return await get_for_date(session, clock_module.local_date(clock, timezone))


async def start(session: AsyncSession, clock: Clock, timezone: str) -> Checkin:
    """Upsert today's row with the answer fields cleared.

    Plan section 9: "A second check-in the same day overwrites the
    first." Resetting rather than reusing is what makes that true --
    otherwise a restarted check-in would keep yesterday's answers for
    any step the user does not reach this time.
    """
    local_date = clock_module.local_date(clock, timezone)
    stmt = (
        pg_insert(Checkin)
        .values(local_date=local_date)
        .on_conflict_do_update(
            index_elements=[Checkin.local_date],
            set_={"day_rating": None, "due_result": None, "note": None, "tg_message_id": None},
        )
        .returning(Checkin.id)
    )
    result = await session.execute(stmt)
    checkin_id = result.scalar_one()
    await session.commit()
    # populate_existing: the sessionmaker sets expire_on_commit=False, so
    # a plain get() would hand back whatever this session already had in
    # its identity map -- i.e. the row as it looked *before* the upsert
    # reset it, complete with the previous check-in's answers.
    row = await session.get(Checkin, checkin_id, populate_existing=True)
    logger.info("checkin started", extra={"checkin_id": row.id})
    return row


async def _set(session: AsyncSession, checkin_id: int, **values) -> Checkin | None:
    row = await session.get(Checkin, checkin_id)
    if row is None:
        return None
    for key, value in values.items():
        setattr(row, key, value)
    await session.commit()
    await session.refresh(row)
    return row


async def set_message_id(session: AsyncSession, checkin_id: int, message_id: int) -> None:
    await _set(session, checkin_id, tg_message_id=message_id)


async def set_rating(session: AsyncSession, checkin_id: int, rating: int) -> Checkin | None:
    if not 1 <= rating <= 5:
        return None
    return await _set(session, checkin_id, day_rating=rating)


async def set_due_result(session: AsyncSession, checkin_id: int, result: str) -> Checkin | None:
    if result not in DUE_RESULTS:
        return None
    return await _set(session, checkin_id, due_result=result)


async def set_note(session: AsyncSession, checkin_id: int, note: str | None) -> Checkin | None:
    if note is not None and len(note) > NOTE_MAX:
        note = note[:NOTE_MAX]
    return await _set(session, checkin_id, note=note)


def _local_date_of(moment: datetime.datetime | None, timezone: str) -> datetime.date | None:
    if moment is None:
        return None
    return clock_module.local_date_of(moment, timezone)


async def finish(
    session: AsyncSession, clock: Clock, timezone: str
) -> tuple[Checkin | None, int]:
    """Apply the streak and stamp last_checkin_at. Returns (row, streak).

    Plan section 9's three cases, decided from `last_checkin_at` -- which
    section 9 requires updating anyway, so no extra column is needed to
    tell "started again today" from "first time today":

    - last_checkin_at is already today  -> unchanged (a same-day redo)
    - a checkin exists for yesterday     -> streak + 1
    - otherwise                          -> streak = 1

    A first-ever check-in has no yesterday row, so it lands on 1; a
    two-day gap lands on 1 as well, which is the reset section 9 asks
    for.
    """
    local_date = clock_module.local_date(clock, timezone)
    row = await get_for_date(session, local_date)
    if row is None:
        return None, 0

    state = await get_state(session)
    previous = state.streak

    if _local_date_of(state.last_checkin_at, timezone) == local_date:
        streak = previous
    else:
        yesterday = local_date - datetime.timedelta(days=1)
        streak = previous + 1 if await get_for_date(session, yesterday) is not None else 1

    if streak != previous:
        await update_state(session, "streak", streak, "command")
    await update_state(session, "last_checkin_at", clock.now_utc(), "command")
    await clear_awaiting(session)

    logger.info("checkin finished", extra={"checkin_id": row.id, "count": streak})
    return row, streak


def synthetic_line(
    row: Checkin, order_results: list[tuple[str, str]] | None = None
) -> str:
    """Plan section 9's stored message: `[чек-ин] день 4/5 · действие: частично · «заметка»`.

    This, not the user's raw note, is what lands in `message` -- so the
    transcript and the scene summary see the whole check-in as one
    coherent turn rather than a bare sentence with no context.

    5c: `order_results` is `[(order_text, 'done'|'no'), ...]`, in the
    order the orders were asked. When given and non-empty, one more
    clause is appended -- « · договорённости: «x» — да; «y» — нет» --
    via `orders.yesterday_line` for the formatting.
    """
    parts = [f"день {row.day_rating}/5" if row.day_rating else "день не оценён"]
    if row.due_result and row.due_result != NONE:
        parts.append(f"действие: {_DUE_LABELS[row.due_result]}")
    if row.note:
        parts.append(f"«{row.note}»")
    tail = orders.yesterday_line(order_results or [])
    if tail:
        parts.append(f"договорённости: {tail}")
    return "[чек-ин] " + " · ".join(parts)


# --- shared step rules (W4: one set for Telegram and the web) ---
#
# app/tg/checkin.py walks these one button at a time; app/web/panels/
# checkin.py collects every answer in one form and calls `submit`. Both
# ask the same questions because both decide them here: whether the
# due-action step exists at all, what an absent due action records, and
# which standing orders a check-in asks about today.


def due_step_needed(due_action: str | None) -> bool:
    """Plan section 9: the due-action step only exists when there is a
    main action to report on; otherwise the check-in records NONE."""
    return bool(due_action)


def resolve_due_result(due_action: str | None, requested: str | None) -> str | None:
    """The due_result a check-in should record, or None if `requested`
    is not acceptable. With no due action the answer is always NONE,
    whatever was requested (there was no question to answer); with one,
    only DONE/PARTIAL/NO are -- NONE would claim there was no action."""
    if not due_step_needed(due_action):
        return NONE
    if requested in (DONE, PARTIAL, NO):
        return requested
    return None


async def form_orders(
    session: AsyncSession, clock: Clock, timezone: str, limit: int
) -> list[StandingOrder]:
    """The standing orders today's check-in asks about (5c): active,
    due today, oldest id first, not already answered by today's row,
    and within `limit` (settings.ORDERS_IN_CHECKIN_MAX) *counting* the
    answers that row already has -- `orders.remaining_due_orders`, the
    same rule app/tg/checkin.py's `_ask_order_or_note` walks one order
    at a time through `orders.next_due_order`. A same-day redo keeps the
    day's earlier order answers (core `start` resets only the check-in's
    own fields), so it asks exactly what a Telegram redo would."""
    local_date = clock_module.local_date(clock, timezone)
    row = await get_for_date(session, local_date)
    return await orders.remaining_due_orders(
        session, row.id if row is not None else None, local_date, limit
    )


def note_is_pause_word(note: str | None) -> bool:
    """Plan section 9: "Pause words always win" at the note step -- a
    note that is a pause word is not a note. app/core/turn.py's step 0c
    applies this to a typed Telegram note; the web panel applies it
    before `submit`, then queues the text as an ordinary message (so
    the pause itself runs in the worker, and the check-in stays
    unfinished, exactly as in Telegram)."""
    return note is not None and pause.match(note) is not None


async def submit(
    session: AsyncSession,
    clock: Clock,
    timezone: str,
    *,
    rating: int,
    due_result: str,
    order_results: list[tuple[int, str]],
    note: str | None,
    message_id: int,
) -> Checkin:
    """Every step of a check-in, in one call (W4's web form): start (a
    same-day redo overwrites, exactly as /checkin does) -> rating ->
    due result -> each order's answer -> the note -> `message_id`.

    The check-in is deliberately *not* finished here, and the global
    note step (`awaiting`) is deliberately *not* opened: the caller
    queues one completion row carrying `message_id`, and the worker
    finishes this exact check-in through `finish_submitted` in queue
    order (app/tg/checkin.py's `finish_and_react`). A pause-word note
    is the caller's to screen out first (`note_is_pause_word`).

    Raises ValueError for a rating outside 1..5 or a due_result outside
    DUE_RESULTS, before anything is written.
    """
    if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
        raise ValueError("rating")
    if due_result not in DUE_RESULTS:
        raise ValueError("due_result")
    for _order_id, result in order_results:
        if result not in (DONE, NO):
            raise ValueError("order_result")

    row = await start(session, clock, timezone)
    await set_rating(session, row.id, rating)
    await set_due_result(session, row.id, due_result)
    for order_id, result in order_results:
        await orders.record_result(session, row.id, order_id, result, clock=clock)
    await set_note(session, row.id, note)
    await set_message_id(session, row.id, message_id)
    return await session.get(Checkin, row.id, populate_existing=True)


async def finish_submitted(
    session: AsyncSession, clock: Clock, timezone: str, message_id: int
) -> tuple[Checkin | None, int]:
    """`finish` for a check-in filled by `submit`, named by the message
    id `submit` stored. Returns (None, 0) -- finishing nothing -- when
    today's row no longer carries that id: restarted since (a newer
    /checkin or web submit), or already finished by this same
    completion. The id is cleared on finish, which is this path's
    double-LLM guard: a replayed completion row finds nothing to finish,
    the way a replayed Пропустить finds the note step already closed.
    """
    row = await get_for_date(session, clock_module.local_date(clock, timezone))
    if row is None or row.tg_message_id is None or row.tg_message_id != message_id:
        return None, 0
    row, streak = await finish(session, clock, timezone)
    if row is not None:
        await _set(session, row.id, tg_message_id=None)
    return row, streak


async def list_range(
    session: AsyncSession, start_date: datetime.date, end_date: datetime.date
) -> list[Checkin]:
    """Every check-in row with `start_date <= local_date <= end_date`,
    ascending by local_date (the web's 30-day history and chart)."""
    result = await session.execute(
        select(Checkin)
        .where(Checkin.local_date >= start_date, Checkin.local_date <= end_date)
        .order_by(Checkin.local_date)
    )
    return list(result.scalars())


# --- the awaiting flag (plan sections 9 and 13) ---


async def set_awaiting_note(session: AsyncSession, checkin_id: int) -> None:
    await update_state(session, "awaiting", AWAITING_NOTE, "command")
    await update_state(session, "awaiting_ref", checkin_id, "command")


async def clear_awaiting(session: AsyncSession) -> None:
    """Idempotent: writes nothing (and no audit row) when already clear.

    Tolerates a missing user_state row rather than using get_state,
    which raises when startup has not run. The command middleware
    (app/tg/router.py) calls this on *every* command, including ones
    that arrive before the singleton exists -- and "no state row" plainly
    means "nothing is pending", not "fail the update".
    """
    result = await session.execute(select(UserState).where(UserState.id == STATE_ID))
    state = result.scalar_one_or_none()
    if state is None or (state.awaiting is None and state.awaiting_ref is None):
        return
    await update_state(session, "awaiting", None, "command")
    await update_state(session, "awaiting_ref", None, "command")


async def pending_note_checkin(
    session: AsyncSession, clock: Clock, timezone: str
) -> Checkin | None:
    """The check-in whose note step is open, or None.

    Returns None -- and clears the flag -- when the pending check-in is
    not today's. Section 9 never says what happens to an unanswered note
    step; left to persist, tomorrow's first message would be silently
    swallowed as yesterday's note. Expiring on the local-date boundary
    costs nothing and needs no timer, because the check happens exactly
    where the note would be consumed.
    """
    state = await get_state(session)
    if state.awaiting != AWAITING_NOTE or state.awaiting_ref is None:
        return None

    row = await session.get(Checkin, state.awaiting_ref)
    if row is None or row.local_date != clock_module.local_date(clock, timezone):
        logger.info("stale checkin note step cleared", extra={"checkin_id": state.awaiting_ref})
        await clear_awaiting(session)
        return None
    return row
