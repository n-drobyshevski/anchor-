"""Scarce attention: Anchor may be busy (phase 5, spec 2026-09-25, slice 3).

`user_state.attention` is 'present' or 'short'. 'short' adds one flag to
the "## Сейчас" block («режим: коротко…») and makes mood занята; that is
all it does. **An inbound turn is always answered**: there is no
'silent' state here at all, because a Telegram user who gets no reply
thinks the bot died. Skipping a proactive tick is the outbound gate's
business, not this module's.

It is computed in code, never by the model:

1. an unexpired 'short' stays short (until `attention_until`);
2. `MAX_SUBSTANTIVE_REPLIES` in-character replies within the last hour
   -> short for a deterministic 20-40 minutes;
3. the first inbound of a quiet-hours stretch -> short, the same way:
   a two-sentence reply and a deferred order, not a new topic at night;
4. otherwise present. Once `attention_until` has passed, the next user
   message lands here, which is the "reset after a pause". `/in` resets
   explicitly (`reset`).

Replies are counted only since the last short stretch ended, so the
replies sent *during* it cannot put Anchor straight back into it.

`refresh()` is the only writer: a targeted UPDATE of these two columns,
the same pattern as app/core/voice.py's remember_nickname. It is not an
audited state_change: attention is bookkeeping about Anchor, not a
decision about the user. tests/test_autonomy_isolation.py limits this
module to exactly these two columns.
"""

from __future__ import annotations

import dataclasses
import datetime
import zlib

from sqlalchemy import func, select
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core import clock as clock_module
from app.core.clock import Clock, within_window
from app.db.models import Message, UserState

PRESENT = "present"
SHORT = "short"

_STATE_ID = 1
_REPLY_KINDS = ("chat", "checkin")


def short_minutes(local_date: datetime.date, reply_count: int, low: int, high: int) -> int:
    """How long a short stretch lasts. Pure and stable across processes.

    crc32, not hash(): Python salts str hashing per process, which would
    make the same inputs give different lengths after a restart.
    """
    span = max(high - low, 0) + 1
    return low + zlib.crc32(f"{local_date.isoformat()}:{reply_count}".encode()) % span


@dataclasses.dataclass(frozen=True)
class Inputs:
    replies_since: int
    first_inbound_in_quiet: bool


def compute(
    now: datetime.datetime,
    *,
    current: str,
    until: datetime.datetime | None,
    inputs: Inputs,
    max_replies: int,
    minutes: int,
) -> tuple[str, datetime.datetime | None]:
    """The four rules of the module docstring, in order. Pure."""
    if current == SHORT and until is not None and until > now:
        return SHORT, until
    if inputs.replies_since >= max_replies or inputs.first_inbound_in_quiet:
        return SHORT, now + datetime.timedelta(minutes=minutes)
    return PRESENT, None


def _quiet_length(settings: Settings) -> datetime.timedelta:
    start = datetime.datetime.combine(datetime.date(2000, 1, 1), settings.QUIET_START)
    end = datetime.datetime.combine(datetime.date(2000, 1, 1), settings.QUIET_END)
    if end <= start:
        end += datetime.timedelta(days=1)
    return end - start


async def load_inputs(
    session: AsyncSession,
    settings: Settings,
    clock: Clock,
    state: UserState,
    *,
    exclude_update_id: int | None,
) -> Inputs:
    """Read-only. Counts replies and looks at the previous user message."""
    now = clock.now_utc()
    since = now - datetime.timedelta(hours=1)
    if state.attention_until is not None and state.attention_until > since:
        since = state.attention_until
    replies = await session.execute(
        select(func.count())
        .select_from(Message)
        .where(
            Message.role == "assistant",
            Message.ooc.is_(False),
            Message.kind.in_(_REPLY_KINDS),
            Message.created_at >= since,
        )
    )
    replies_since = int(replies.scalar_one())

    first_in_quiet = False
    local_now = clock_module.now_local(clock, state.timezone)
    if within_window(local_now.time(), settings.QUIET_START, settings.QUIET_END):
        # The same exclusion app/core/mood.py uses: the current turn's
        # own message is already stored by the time this runs.
        stmt = select(Message.created_at).where(Message.role == "user")
        if exclude_update_id is not None:
            stmt = stmt.where(Message.update_id.is_distinct_from(exclude_update_id))
        previous = (
            await session.execute(stmt.order_by(Message.id.desc()).limit(1))
        ).scalar_one_or_none()
        if previous is not None:
            previous_local = clock_module.to_local(previous, state.timezone)
            same_stretch = now - previous < _quiet_length(settings) and within_window(
                previous_local.time(), settings.QUIET_START, settings.QUIET_END
            )
            first_in_quiet = not same_stretch
    return Inputs(replies_since=replies_since, first_inbound_in_quiet=first_in_quiet)


async def refresh(
    session: AsyncSession,
    settings: Settings,
    clock: Clock,
    state: UserState,
    *,
    exclude_update_id: int | None,
) -> tuple[str, datetime.datetime | None]:
    """Recompute and store attention for this turn. Commits.

    Also sets the new values on `state` itself, so the caller's
    in-memory row (which mood and the prompt read next) agrees with the
    database without a second read.
    """
    inputs = await load_inputs(
        session, settings, clock, state, exclude_update_id=exclude_update_id
    )
    now = clock.now_utc()
    minutes = short_minutes(
        clock_module.local_date(clock, state.timezone),
        inputs.replies_since,
        settings.ATTENTION_SHORT_MIN_LOW,
        settings.ATTENTION_SHORT_MIN_HIGH,
    )
    attention, until = compute(
        now,
        current=state.attention,
        until=state.attention_until,
        inputs=inputs,
        max_replies=settings.MAX_SUBSTANTIVE_REPLIES,
        minutes=minutes,
    )
    if (attention, until) != (state.attention, state.attention_until):
        await session.execute(
            sql_update(UserState)
            .where(UserState.id == _STATE_ID)
            .values(attention=attention, attention_until=until)
        )
        await session.commit()
        state.attention = attention
        state.attention_until = until
    return attention, until


async def reset(session: AsyncSession) -> None:
    """`/in`: back to present. Commits."""
    await session.execute(
        sql_update(UserState)
        .where(UserState.id == _STATE_ID)
        .values(attention=PRESENT, attention_until=None)
    )
    await session.commit()


def is_short(state: UserState, now: datetime.datetime) -> bool:
    """Pure: is `state` in an unexpired short stretch right now?"""
    until = getattr(state, "attention_until", None)
    return getattr(state, "attention", PRESENT) == SHORT and until is not None and until > now
