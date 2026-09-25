"""The `prebrief` idle kind: input, validation, storage for tomorrow,
its kind rule (after 19:00 local, morning disabled, note already
exists), and that only the morning outbound ever reads it (Phase 6 plan
section 6.4; milestone 6c's own test list)."""

from __future__ import annotations

import datetime
import decimal

import pytest
from sqlalchemy import select

from app.config import Settings
from app.core.clock import FrozenClock, to_local
from app.core.idle.gate import MORNING_DISABLED, NOTE_EXISTS, NOT_EVENING, config_from_settings, idle_gate
from app.core.idle.prebrief import NOTE_MAX_LEN, run_prebrief, validate
from app.db.models import BriefNote, IdleRun, StandingOrder, UserState
from conftest import FakeLLMProvider

TIMEZONE = "Europe/Paris"


def _evening_clock() -> FrozenClock:
    # 20:00 local (Europe/Paris, UTC+2 in September).
    return FrozenClock(datetime.datetime(2026, 9, 23, 18, 0, tzinfo=datetime.timezone.utc))


def _noon_clock() -> FrozenClock:
    return FrozenClock(datetime.datetime(2026, 9, 23, 12, 0, tzinfo=datetime.timezone.utc))


async def _seed_state(sessionmaker, **overrides) -> None:
    values = dict(id=1, chat_id=555, timezone=TIMEZONE, due_action="позвонить маме")
    values.update(overrides)
    async with sessionmaker() as session:
        session.add(UserState(**values))
        await session.commit()


# --- validate() -----------------------------------------------------------


def test_validate_keeps_up_to_three_short_screened_notes():
    payload = {"notes": ["Заметка раз", "Заметка два", "Заметка три"]}
    assert validate(payload) == ["Заметка раз", "Заметка два", "Заметка три"]


def test_validate_drops_a_fourth_note():
    payload = {"notes": [f"note {i}" for i in range(5)]}
    assert len(validate(payload)) == 3


def test_validate_drops_notes_over_the_length_cap():
    payload = {"notes": ["x" * (NOTE_MAX_LEN + 1), "ок"]}
    assert validate(payload) == ["ок"]


def test_validate_drops_notes_that_fail_screen():
    payload = {"notes": ["как убить себя", "нормальная заметка"]}
    assert validate(payload) == ["нормальная заметка"]


def test_validate_rejects_non_dict_and_non_list_shapes():
    assert validate({}) == []
    assert validate({"notes": "not a list"}) == []
    assert validate({"notes": [1, 2, None, "ок"]}) == ["ок"]


# --- kind rule --------------------------------------------------------


def test_kind_rule_before_1900_local():
    from app.core.idle.gate import IdleFacts

    config = config_from_settings(Settings())
    now = _noon_clock().now_utc()
    facts = IdleFacts(
        persona_active=True, local_now=to_local(now, TIMEZONE),
        daily_usd_cap=decimal.Decimal("1.00"),
    )
    assert idle_gate("prebrief", facts, now, config) == (False, NOT_EVENING)


def test_kind_rule_morning_disabled():
    from app.core.idle.gate import IdleFacts

    config = config_from_settings(Settings(OUTBOUND_ENABLED=False))
    now = _evening_clock().now_utc()
    facts = IdleFacts(
        persona_active=True, local_now=to_local(now, TIMEZONE),
        daily_usd_cap=decimal.Decimal("1.00"),
    )
    assert idle_gate("prebrief", facts, now, config) == (False, MORNING_DISABLED)


def test_kind_rule_note_already_exists():
    from app.core.idle.gate import IdleFacts

    config = config_from_settings(Settings())
    now = _evening_clock().now_utc()
    facts = IdleFacts(
        persona_active=True,
        local_now=to_local(now, TIMEZONE),
        prebrief_note_exists_tomorrow=True,
        daily_usd_cap=decimal.Decimal("1.00"),
    )
    assert idle_gate("prebrief", facts, now, config) == (False, NOTE_EXISTS)


def test_kind_rule_allows_after_1900_local_with_no_existing_note():
    from app.core.idle.gate import IdleFacts

    config = config_from_settings(Settings())
    now = _evening_clock().now_utc()
    facts = IdleFacts(
        persona_active=True, local_now=to_local(now, TIMEZONE),
        daily_usd_cap=decimal.Decimal("1.00"),
    )
    assert idle_gate("prebrief", facts, now, config) == (True, "ok")


# --- run_prebrief -------------------------------------------------------


async def test_run_prebrief_stores_notes_for_tomorrow(sessionmaker):
    clock = _evening_clock()
    await _seed_state(sessionmaker)
    async with sessionmaker() as session:
        session.add(
            StandingOrder(text="выпить воды", cadence="daily", status="active", source="user")
        )
        run = IdleRun(kind="prebrief", local_date=clock.now_utc().date(), status="queued")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id

    provider = FakeLLMProvider(text='{"notes": ["Вчера был тяжёлый день."]}')

    result = await run_prebrief(
        sessionmaker, Settings(), provider, clock,
        run_id=run_id, started_at=clock.now_utc(), timezone=TIMEZONE,
    )
    assert result.notes == ("Вчера был тяжёлый день.",)

    tomorrow = clock.now_utc().date() + datetime.timedelta(days=1)
    async with sessionmaker() as session:
        note = await session.get(BriefNote, tomorrow)
        assert note is not None
        assert note.notes == ["Вчера был тяжёлый день."]
        assert note.used_at is None


async def test_run_prebrief_never_writes_memory_or_notebook(sessionmaker):
    """Not reversible: no idle_change row, unlike consolidate/reflect."""
    clock = _evening_clock()
    await _seed_state(sessionmaker)
    async with sessionmaker() as session:
        run = IdleRun(kind="prebrief", local_date=clock.now_utc().date(), status="queued")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id

    provider = FakeLLMProvider(text='{"notes": []}')
    await run_prebrief(
        sessionmaker, Settings(), provider, clock,
        run_id=run_id, started_at=clock.now_utc(), timezone=TIMEZONE,
    )

    async with sessionmaker() as session:
        from app.db.models import IdleChange

        changes = (await session.execute(select(IdleChange))).scalars().all()
        assert changes == []


async def test_run_prebrief_does_not_overwrite_an_existing_tomorrow_note(sessionmaker):
    clock = _evening_clock()
    await _seed_state(sessionmaker)
    tomorrow = clock.now_utc().date() + datetime.timedelta(days=1)
    async with sessionmaker() as session:
        session.add(BriefNote(local_date=tomorrow, notes=["уже есть"]))
        run = IdleRun(kind="prebrief", local_date=clock.now_utc().date(), status="queued")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id

    provider = FakeLLMProvider(text='{"notes": ["новая заметка"]}')
    await run_prebrief(
        sessionmaker, Settings(), provider, clock,
        run_id=run_id, started_at=clock.now_utc(), timezone=TIMEZONE,
    )

    async with sessionmaker() as session:
        note = await session.get(BriefNote, tomorrow)
        assert note.notes == ["уже есть"]
