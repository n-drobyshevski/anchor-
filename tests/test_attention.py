"""Scarce attention (phase 5, spec 2026-09-25, slice 3): app/core/attention.py.

The pure rules, the jitter, the database inputs (reply count, the first
inbound of a quiet stretch), the narrow writer, `/in`'s reset, and the
flag it puts into the persona context.
"""

from __future__ import annotations

import datetime
import random

import pytest
from sqlalchemy import select

from app.config import Settings
from app.core import attention
from app.core import persona_context
from app.db.models import Message, TelegramUpdate, UserState

pytestmark = pytest.mark.asyncio

TIMEZONE = "Europe/Paris"
NOW = datetime.datetime(2026, 9, 25, 12, 0, tzinfo=datetime.timezone.utc)
QUIET = attention.Inputs(replies_since=0, first_inbound_in_quiet=False)


# --- the pure rules ---------------------------------------------------------


def test_short_minutes_is_in_range_and_stable():
    day = datetime.date(2026, 9, 25)
    values = {attention.short_minutes(day, n, 20, 40) for n in range(200)}
    assert min(values) >= 20 and max(values) <= 40
    assert len(values) > 5
    assert attention.short_minutes(day, 12, 20, 40) == attention.short_minutes(day, 12, 20, 40)


def test_compute_defaults_to_present():
    assert attention.compute(
        NOW, current="present", until=None, inputs=QUIET, max_replies=12, minutes=30
    ) == ("present", None)


def test_compute_goes_short_after_max_replies():
    inputs = attention.Inputs(replies_since=12, first_inbound_in_quiet=False)
    state, until = attention.compute(
        NOW, current="present", until=None, inputs=inputs, max_replies=12, minutes=25
    )
    assert state == "short"
    assert until == NOW + datetime.timedelta(minutes=25)


def test_compute_goes_short_on_the_first_inbound_in_quiet_hours():
    inputs = attention.Inputs(replies_since=0, first_inbound_in_quiet=True)
    state, _ = attention.compute(
        NOW, current="present", until=None, inputs=inputs, max_replies=12, minutes=25
    )
    assert state == "short"


def test_an_unexpired_short_stretch_is_kept_as_is():
    until = NOW + datetime.timedelta(minutes=10)
    assert attention.compute(
        NOW, current="short", until=until, inputs=QUIET, max_replies=12, minutes=40
    ) == ("short", until)


def test_an_expired_short_stretch_resets_to_present():
    until = NOW - datetime.timedelta(minutes=1)
    assert attention.compute(
        NOW, current="short", until=until, inputs=QUIET, max_replies=12, minutes=40
    ) == ("present", None)


def test_there_is_no_silent_state():
    """Never silent on an inbound turn: the module only knows two states."""
    assert {attention.PRESENT, attention.SHORT} == {"present", "short"}
    assert not hasattr(attention, "SILENT")


# --- the database side --------------------------------------------------------


async def _seed_state(sessionmaker, **overrides) -> UserState:
    async with sessionmaker() as session:
        state = UserState(id=1, chat_id=1, timezone=TIMEZONE, **overrides)
        session.add(state)
        await session.commit()
        await session.refresh(state)
        return state


async def _add(sessionmaker, *, role, created_at, kind="chat", ooc=False, update_id=None):
    async with sessionmaker() as session:
        if update_id is not None:
            session.add(TelegramUpdate(update_id=update_id, payload={}))
            await session.commit()
        row = Message(role=role, content="…", kind=kind, ooc=ooc, update_id=update_id)
        session.add(row)
        await session.commit()
        row.created_at = created_at
        await session.commit()


async def _state(sessionmaker) -> UserState:
    async with sessionmaker() as session:
        return (await session.execute(select(UserState))).scalar_one()


async def test_refresh_counts_only_in_character_replies_in_the_last_hour(
    sessionmaker, frozen_clock
):
    clock = frozen_clock(2026, 9, 25, 15, 0, tz=TIMEZONE)
    settings = Settings(MAX_SUBSTANTIVE_REPLIES=3)
    state = await _seed_state(sessionmaker)
    now = clock.now_utc()
    for minutes in (5, 10):
        await _add(sessionmaker, role="assistant", created_at=now - datetime.timedelta(minutes=minutes))
    # None of these count: out of character, canned, too old.
    await _add(sessionmaker, role="assistant", created_at=now, ooc=True)
    await _add(sessionmaker, role="assistant", created_at=now, kind="canned")
    await _add(sessionmaker, role="assistant", created_at=now - datetime.timedelta(hours=2))

    async with sessionmaker() as session:
        assert await attention.refresh(
            session, settings, clock, state, exclude_update_id=None
        ) == ("present", None)

    await _add(sessionmaker, role="assistant", created_at=now - datetime.timedelta(minutes=1))
    async with sessionmaker() as session:
        result, until = await attention.refresh(
            session, settings, clock, state, exclude_update_id=None
        )
    assert result == "short"
    stored = await _state(sessionmaker)
    assert (stored.attention, stored.attention_until) == ("short", until)
    assert state.attention == "short"


async def test_replies_during_a_short_stretch_do_not_restart_it(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 25, 15, 0, tz=TIMEZONE)
    settings = Settings(MAX_SUBSTANTIVE_REPLIES=3)
    now = clock.now_utc()
    ended = now - datetime.timedelta(minutes=5)
    state = await _seed_state(sessionmaker, attention="short", attention_until=ended)
    # Three replies in the last hour, but all before the stretch ended.
    for minutes in (20, 30, 40):
        await _add(sessionmaker, role="assistant", created_at=now - datetime.timedelta(minutes=minutes))

    async with sessionmaker() as session:
        assert await attention.refresh(
            session, settings, clock, state, exclude_update_id=None
        ) == ("present", None)


async def test_the_first_inbound_of_a_quiet_night_goes_short(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 25, 23, 30, tz=TIMEZONE)
    now = clock.now_utc()
    state = await _seed_state(sessionmaker)
    await _add(sessionmaker, role="user", created_at=now - datetime.timedelta(hours=3), update_id=1)
    await _add(sessionmaker, role="user", created_at=now, update_id=2)

    async with sessionmaker() as session:
        inputs = await attention.load_inputs(
            session, Settings(), clock, state, exclude_update_id=2
        )
    assert inputs.first_inbound_in_quiet is True


async def test_a_second_message_in_the_same_quiet_stretch_is_not_first(
    sessionmaker, frozen_clock
):
    clock = frozen_clock(2026, 9, 25, 23, 30, tz=TIMEZONE)
    now = clock.now_utc()
    state = await _seed_state(sessionmaker)
    await _add(sessionmaker, role="user", created_at=now - datetime.timedelta(minutes=20), update_id=1)
    await _add(sessionmaker, role="user", created_at=now, update_id=2)

    async with sessionmaker() as session:
        inputs = await attention.load_inputs(
            session, Settings(), clock, state, exclude_update_id=2
        )
    assert inputs.first_inbound_in_quiet is False


async def test_daytime_is_never_a_quiet_first_inbound(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 25, 14, 0, tz=TIMEZONE)
    state = await _seed_state(sessionmaker)
    await _add(sessionmaker, role="user", created_at=clock.now_utc() - datetime.timedelta(days=2), update_id=1)

    async with sessionmaker() as session:
        inputs = await attention.load_inputs(
            session, Settings(), clock, state, exclude_update_id=None
        )
    assert inputs.first_inbound_in_quiet is False


async def test_reset_returns_to_present(sessionmaker):
    await _seed_state(
        sessionmaker, attention="short", attention_until=NOW + datetime.timedelta(minutes=30)
    )
    async with sessionmaker() as session:
        await attention.reset(session)
    stored = await _state(sessionmaker)
    assert (stored.attention, stored.attention_until) == ("present", None)


# --- the persona context --------------------------------------------------------


async def test_gather_adds_the_short_flag_only_while_short(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 25, 15, 0, tz=TIMEZONE)
    until = clock.now_utc() + datetime.timedelta(minutes=20)
    state = await _seed_state(sessionmaker, attention="short", attention_until=until)

    async with sessionmaker() as session:
        ctx = await persona_context.gather(
            session, Settings(), state, clock,
            scene_id=None, exclude_update_id=None, rng=random.Random(0),
        )
    assert persona_context.SHORT_FLAG in ctx.flags
    assert ctx.mood == "занята"

    state.attention_until = clock.now_utc() - datetime.timedelta(minutes=1)
    async with sessionmaker() as session:
        ctx = await persona_context.gather(
            session, Settings(), state, clock,
            scene_id=None, exclude_update_id=None, rng=random.Random(0),
        )
    assert persona_context.SHORT_FLAG not in ctx.flags
