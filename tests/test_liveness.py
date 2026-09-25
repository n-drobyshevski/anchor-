"""Liveness (Phase 6 plan section 9.6; milestone 6e).

Two halves:
- /readyz's own staleness check lives in tests/test_webhook.py (it is a
  webhook-route test in every other respect);
- the in-process watchdog (app/worker.py) that actually kills the
  process when the heartbeat has gone stale for longer than Railway's
  own healthcheck would ever notice, tested here with an injected clock
  and an injected exit function (plan section 12's "watchdog exits only
  when stale").
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import update as sql_update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import Settings
from app.core.clock import FrozenClock
from app.db.models import HeartbeatState
from app.worker import _watchdog_loop, watchdog_is_stale

# No module-level pytest.mark.asyncio -- this file mixes the pure
# predicate's plain sync tests with the loop's async ones; see
# tests/test_backup.py's identical note (asyncio_mode = "auto" already
# detects the async tests on its own).

NOW = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)
FIVE_MIN = datetime.timedelta(minutes=5)


# --- the pure predicate --------------------------------------------------


def test_fresh_heartbeat_is_not_stale():
    assert watchdog_is_stale(NOW - datetime.timedelta(minutes=4), NOW, NOW - FIVE_MIN, FIVE_MIN) is False


def test_heartbeat_past_the_window_is_stale():
    assert (
        watchdog_is_stale(NOW - datetime.timedelta(minutes=5, seconds=1), NOW, NOW - FIVE_MIN, FIVE_MIN)
        is True
    )


def test_never_stamped_is_not_stale_during_the_startup_grace():
    """A None heartbeat_at right after boot must not trip the watchdog
    before the heartbeat loop's very first tick has had a chance to run."""
    started_at = NOW
    almost_there = NOW + datetime.timedelta(minutes=4, seconds=59)
    assert watchdog_is_stale(None, almost_there, started_at, FIVE_MIN) is False


def test_never_stamped_is_stale_once_the_startup_grace_has_passed():
    started_at = NOW
    past_grace = NOW + datetime.timedelta(minutes=5, seconds=1)
    assert watchdog_is_stale(None, past_grace, started_at, FIVE_MIN) is True


# --- the loop, with an injected clock and an injected exit function -----


async def _upsert_heartbeat_at(sessionmaker, heartbeat_at) -> None:
    async with sessionmaker() as session:
        await session.execute(
            pg_insert(HeartbeatState).values(id=1).on_conflict_do_nothing(index_elements=["id"])
        )
        await session.execute(
            sql_update(HeartbeatState).where(HeartbeatState.id == 1).values(heartbeat_at=heartbeat_at)
        )
        await session.commit()


class _FrozenTicker:
    """A clock whose `now_utc()` steps forward by a fixed amount on each
    call -- lets one test drive several `_watchdog_loop` iterations
    without a real `asyncio.sleep`."""

    def __init__(self, start: datetime.datetime, step: datetime.timedelta) -> None:
        self._now = start
        self._step = step

    def now_utc(self) -> datetime.datetime:
        current = self._now
        self._now = self._now + self._step
        return current


async def test_watchdog_loop_exits_when_heartbeat_is_stale(sessionmaker, monkeypatch):
    settings = Settings(LIVENESS_STALE_MIN=5)
    await _upsert_heartbeat_at(sessionmaker, NOW - datetime.timedelta(minutes=30))

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("app.worker.asyncio.sleep", _no_sleep)

    exit_calls: list[int] = []

    def _fake_exit(code: int) -> None:
        exit_calls.append(code)
        raise SystemExit(code)  # stop the (otherwise infinite) loop

    clock = _FrozenTicker(NOW, datetime.timedelta(seconds=1))
    with pytest.raises(SystemExit):
        await _watchdog_loop(
            sessionmaker, settings, clock, exit_fn=_fake_exit, started_at=NOW - datetime.timedelta(hours=1)
        )
    assert exit_calls == [1]


async def test_watchdog_loop_never_exits_while_heartbeat_stays_fresh(sessionmaker, monkeypatch):
    """A heartbeat that keeps advancing (a real heartbeat loop writing to
    it) must never trip the watchdog -- checked over several ticks."""
    settings = Settings(LIVENESS_STALE_MIN=5)
    await _upsert_heartbeat_at(sessionmaker, NOW)

    call_count = 0

    async def _no_sleep_then_stop(_seconds):
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            raise SystemExit(0)  # stop the loop after a few iterations

    monkeypatch.setattr("app.worker.asyncio.sleep", _no_sleep_then_stop)

    exit_calls: list[int] = []
    clock = FrozenClock(NOW)  # heartbeat never goes stale relative to this

    with pytest.raises(SystemExit):
        await _watchdog_loop(
            sessionmaker, settings, clock, exit_fn=lambda code: exit_calls.append(code), started_at=NOW
        )
    assert exit_calls == []


async def test_watchdog_loop_tolerates_a_check_failure(sessionmaker, monkeypatch):
    """A DB hiccup during the check must not be mistaken for staleness --
    it logs and waits for the next tick, exactly like the heartbeat
    loop's own broad except."""
    settings = Settings(LIVENESS_STALE_MIN=5)

    class _BrokenSessionmaker:
        def __call__(self):
            raise RuntimeError("boom")

    call_count = 0

    async def _no_sleep_then_stop(_seconds):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise SystemExit(0)

    monkeypatch.setattr("app.worker.asyncio.sleep", _no_sleep_then_stop)

    exit_calls: list[int] = []
    clock = FrozenClock(NOW)
    with pytest.raises(SystemExit):
        await _watchdog_loop(
            _BrokenSessionmaker(), settings, clock, exit_fn=lambda code: exit_calls.append(code), started_at=NOW
        )
    assert exit_calls == []
