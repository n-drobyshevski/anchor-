"""app/core/scheduler.py tests (phase-3 plan sections 6 and 13).

The plan names five properties, and each has a test below:

- a simulated day produces morning ~09:00-09:15 and evening
  ~22:00-22:15, **exactly once** each;
- a restart at 10:30 still sends the morning message (grace), one at
  12:30 does not;
- the evening grace window ends at QUIET_START;
- priority ordering;
- a failed planning gate inserts no row.

"Exactly once" is the one worth stating plainly: the heartbeat runs
every 60 seconds, so over a 3-hour grace window it gets ~180 chances to
plan the same morning message. The unique constraint is what makes all
180 collapse into one, and `_already_exists` is what stops the other
179 from even trying.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.core.clock import FrozenClock, combine_local, local_date
from app.core.outbound_gate import EVENING_NAG, MORNING
from app.core.scheduler import heartbeat
from app.db.models import Checkin, Job, Outbound, UserState

TIMEZONE = "Europe/Paris"
CHAT_ID = 4242

# A plain Wednesday: no DST, nothing else load-bearing.
DAY = datetime.date(2026, 9, 23)


def at(hour: int, minute: int = 0, day: datetime.date = DAY) -> FrozenClock:
    """A clock reading `hour:minute` on the user's wall clock."""
    return FrozenClock(combine_local(day, datetime.time(hour, minute), TIMEZONE))


def settings(**overrides) -> Settings:
    base = dict(
        TZ_DEFAULT=TIMEZONE,
        JITTER_MAX_MIN=0,  # determinism; the jitter has its own test
        DAILY_USD_CAP=1.00,
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


async def _seed(sessionmaker, **fields) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone=TIMEZONE, **fields))
        await session.commit()


async def _rows(sessionmaker) -> list[Outbound]:
    async with sessionmaker() as session:
        result = await session.execute(select(Outbound).order_by(Outbound.id))
        return list(result.scalars().all())


async def _tick(sessionmaker, clock, cfg=None) -> int | None:
    async with sessionmaker() as session:
        return await heartbeat(session, cfg or settings(), clock)


# --- a simulated day ---------------------------------------------------


async def test_a_simulated_day_plans_morning_and_evening_exactly_once(sessionmaker):
    """Every minute from 07:00 to 23:00 local. 960 heartbeats, 2 rows."""
    await _seed(sessionmaker)
    cfg = settings(JITTER_MAX_MIN=15)

    clock = at(7, 0)
    for _ in range(16 * 60):
        await _tick(sessionmaker, clock, cfg)
        clock.advance(datetime.timedelta(minutes=1))

    rows = await _rows(sessionmaker)
    assert [row.kind for row in rows] == [MORNING, EVENING_NAG]

    morning, evening = rows
    assert morning.local_date == DAY and morning.bucket == 0
    assert evening.local_date == DAY and evening.bucket == 0

    # Planned inside the first jitter window after each target.
    for row, hour in ((morning, 9), (evening, 22)):
        target = combine_local(DAY, datetime.time(hour, 0), TIMEZONE)
        assert target <= row.planned_for <= target + datetime.timedelta(minutes=15)


async def test_each_planned_row_gets_exactly_one_send_job(sessionmaker):
    await _seed(sessionmaker)
    clock = at(9, 0)
    for _ in range(30):
        await _tick(sessionmaker, clock)
        clock.advance(datetime.timedelta(minutes=1))

    async with sessionmaker() as session:
        outbound_ids = list((await session.execute(select(Outbound.id))).scalars())
        jobs = list((await session.execute(select(Job))).scalars())

    assert len(outbound_ids) == 1
    assert [job.dedup_key for job in jobs] == [f"outbound:{outbound_ids[0]}"]
    assert jobs[0].kind == "send_outbound"
    assert jobs[0].payload == {"outbound_id": outbound_ids[0]}


async def test_a_second_heartbeat_in_the_same_minute_plans_nothing(sessionmaker):
    await _seed(sessionmaker)
    clock = at(9, 0)
    first = await _tick(sessionmaker, clock)
    second = await _tick(sessionmaker, clock)
    assert first is not None
    assert second is None
    assert len(await _rows(sessionmaker)) == 1


# --- the grace window --------------------------------------------------


async def test_a_restart_at_1030_still_plans_the_morning_message(sessionmaker):
    """SEND_GRACE_MIN is 180, so 09:00 + 3h means 10:30 is still inside."""
    await _seed(sessionmaker)
    assert await _tick(sessionmaker, at(10, 30)) is not None
    rows = await _rows(sessionmaker)
    assert [row.kind for row in rows] == [MORNING]


async def test_a_restart_at_1230_does_not(sessionmaker):
    await _seed(sessionmaker)
    assert await _tick(sessionmaker, at(12, 30)) is None
    assert await _rows(sessionmaker) == []


async def test_the_window_is_half_open_at_both_ends(sessionmaker):
    await _seed(sessionmaker)
    # One minute early: nothing.
    assert await _tick(sessionmaker, at(8, 59)) is None
    # Exactly on target: planned.
    assert await _tick(sessionmaker, at(9, 0)) is not None


async def test_exactly_at_the_grace_end_is_too_late(sessionmaker):
    await _seed(sessionmaker)
    assert await _tick(sessionmaker, at(12, 0)) is None
    assert await _rows(sessionmaker) == []


async def test_the_evening_grace_window_ends_at_quiet_start(sessionmaker):
    """EVENING_TIME 22:00 + SEND_GRACE_MIN 180 would reach 01:00, deep
    inside quiet hours. Plan section 2 clamps it to QUIET_START."""
    await _seed(sessionmaker)
    assert await _tick(sessionmaker, at(22, 29)) is not None
    assert [row.kind for row in await _rows(sessionmaker)] == [EVENING_NAG]


async def test_the_evening_nag_is_not_planned_once_quiet_hours_start(sessionmaker):
    await _seed(sessionmaker)
    assert await _tick(sessionmaker, at(22, 30)) is None
    assert await _rows(sessionmaker) == []


async def test_evening_jitter_cannot_push_a_send_into_quiet_hours(sessionmaker):
    """Planned at 22:29 with 15 minutes of jitter, the naive answer is
    22:44 -- which the send-time gate would refuse as quiet_hours, so
    the message would be silently skipped rather than sent late."""
    await _seed(sessionmaker)
    await _tick(sessionmaker, at(22, 29), settings(JITTER_MAX_MIN=15))
    (row,) = await _rows(sessionmaker)
    quiet_start = combine_local(DAY, datetime.time(22, 30), TIMEZONE)
    assert row.planned_for < quiet_start


# --- priority ----------------------------------------------------------


async def test_evening_outranks_morning_when_both_are_due(sessionmaker):
    """Only reachable by config, since the default windows never
    overlap -- but plan section 6 states the ordering, so it is tested
    rather than assumed."""
    await _seed(sessionmaker)
    cfg = settings(MORNING_TIME="21:00", EVENING_TIME="22:00", SEND_GRACE_MIN=180)
    await _tick(sessionmaker, at(22, 5), cfg)
    assert [row.kind for row in await _rows(sessionmaker)] == [EVENING_NAG]


async def test_only_one_intent_is_planned_per_heartbeat(sessionmaker):
    await _seed(sessionmaker)
    cfg = settings(MORNING_TIME="21:00", EVENING_TIME="22:00", SEND_GRACE_MIN=180)
    await _tick(sessionmaker, at(22, 5), cfg)
    assert len(await _rows(sessionmaker)) == 1
    # The loser is picked up on the next tick.
    await _tick(sessionmaker, at(22, 6), cfg)
    assert {row.kind for row in await _rows(sessionmaker)} == {MORNING, EVENING_NAG}


# --- a refused gate inserts nothing ------------------------------------


async def test_a_failed_planning_gate_inserts_no_row(sessionmaker):
    await _seed(sessionmaker, persona_active=False)
    assert await _tick(sessionmaker, at(9, 0)) is None
    assert await _rows(sessionmaker) == []
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(Job)) == 0


async def test_the_kill_switch_stops_planning(sessionmaker):
    await _seed(sessionmaker)
    assert await _tick(sessionmaker, at(9, 0), settings(OUTBOUND_ENABLED=False)) is None
    assert await _rows(sessionmaker) == []


async def test_quiet_lifted_inside_the_grace_window_still_gets_the_message(sessionmaker):
    """The reason a refused gate must not write a `skipped` row: plan
    section 6's worked example, /quiet expiring at 10:00."""
    quiet_until = combine_local(DAY, datetime.time(10, 0), TIMEZONE)
    await _seed(sessionmaker, quiet_until=quiet_until)

    assert await _tick(sessionmaker, at(9, 0)) is None
    assert await _tick(sessionmaker, at(9, 30)) is None
    assert await _rows(sessionmaker) == []

    assert await _tick(sessionmaker, at(10, 1)) is not None
    assert [row.kind for row in await _rows(sessionmaker)] == [MORNING]


async def test_a_completed_checkin_stops_the_evening_nag_being_planned(sessionmaker):
    clock = at(22, 0)
    await _seed(sessionmaker, last_checkin_at=clock.now_utc())
    assert await _tick(sessionmaker, clock) is None
    assert await _rows(sessionmaker) == []


async def test_an_abandoned_checkin_does_not_stop_the_nag(sessionmaker):
    """A /checkin row exists from the moment the command is typed, with
    every answer null. Suppressing the nag on that would silence
    exactly the reminder that was needed."""
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        session.add(Checkin(local_date=DAY))
        await session.commit()
    assert await _tick(sessionmaker, at(22, 0)) is not None
    assert [row.kind for row in await _rows(sessionmaker)] == [EVENING_NAG]


# --- a cancelled or sent row is not re-planned -------------------------


@pytest.mark.parametrize("status", ["cancelled", "sent", "skipped", "failed"])
async def test_a_row_in_any_status_blocks_re_planning_that_intent(sessionmaker, status):
    """A pause word at 09:05 cancels today's morning message. The
    heartbeat sixty seconds later must not put it straight back."""
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        session.add(
            Outbound(
                kind=MORNING,
                local_date=DAY,
                bucket=0,
                planned_for=combine_local(DAY, datetime.time(9, 0), TIMEZONE),
                status=status,
            )
        )
        await session.commit()

    assert await _tick(sessionmaker, at(9, 30)) is None
    assert len(await _rows(sessionmaker)) == 1


async def test_tomorrow_is_a_fresh_local_date(sessionmaker):
    await _seed(sessionmaker)
    await _tick(sessionmaker, at(9, 0))
    await _tick(sessionmaker, at(9, 0, DAY + datetime.timedelta(days=1)))
    rows = await _rows(sessionmaker)
    assert [row.local_date for row in rows] == [DAY, DAY + datetime.timedelta(days=1)]


async def test_the_local_date_is_the_users_not_utcs(sessionmaker):
    await _seed(sessionmaker)
    clock = at(9, 0)
    await _tick(sessionmaker, clock)
    (row,) = await _rows(sessionmaker)
    assert row.local_date == local_date(clock, TIMEZONE)
