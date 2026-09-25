"""The daily check-in and the streak (plan sections 4 and 9).

The `checkin` row **is** the state machine. `/checkin` upserts today's
row with the answer fields nulled and each button fills one of them,
which is also why section 9's "a second check-in the same day overwrites
the first" needs no special handling: `local_date` is unique, so
starting again resets the day.

This module knows nothing about Telegram; app/tg/checkin.py owns the
keyboards and the Russian strings.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clock as clock_module
from app.core.clock import Clock
from app.core.state import STATE_ID, get_state, update_state
from app.db.models import Checkin, UserState

logger = logging.getLogger(__name__)

AWAITING_NOTE = "checkin_note"

NOTE_MAX = 500

DONE = "done"
PARTIAL = "partial"
NO = "no"
NONE = "none"
DUE_RESULTS = (DONE, PARTIAL, NO, NONE)

_DUE_LABELS = {DONE: "сделано", PARTIAL: "частично", NO: "не сделано", NONE: "нет действия"}
# Public for the vault's day file (phase-5 plan section 4.2), which says
# the same thing the synthetic line does.
DUE_LABELS_TEXT = _DUE_LABELS


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


def synthetic_line(row: Checkin) -> str:
    """Plan section 9's stored message: `[чек-ин] день 4/5 · действие: частично · «заметка»`.

    This, not the user's raw note, is what lands in `message` -- so the
    transcript and the scene summary see the whole check-in as one
    coherent turn rather than a bare sentence with no context.
    """
    parts = [f"день {row.day_rating}/5" if row.day_rating else "день не оценён"]
    if row.due_result and row.due_result != NONE:
        parts.append(f"действие: {_DUE_LABELS[row.due_result]}")
    if row.note:
        parts.append(f"«{row.note}»")
    return "[чек-ин] " + " · ".join(parts)


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
