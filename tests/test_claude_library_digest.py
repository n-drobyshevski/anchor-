"""The library's once-a-day digest (connector plan section 9, C3).

app/core/scheduler.py's maybe_enqueue_library_digest queues at most one
job per local date, after CLAUDE_LIBRARY_DIGEST_TIME; app/tg/claude.py's
run_library_digest decides whether to send it, respecting may_report_now
and deferring rather than giving up. Modelled on tests/test_backup.py's
scheduling/dispatch section.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select

from app.config import Settings
from app.core.clock import FrozenClock
from app.core.grants import record_library_read
from app.core.scene import Deferred
from app.core.scheduler import (
    CLAUDE_LIBRARY_DIGEST,
    CLAUDE_LIBRARY_DIGEST_TIME,
    library_digest_dedup_key,
    maybe_enqueue_library_digest,
)
from app.db.models import Job, UserState
from app.tg import claude as claude_ui

# No module-level `pytestmark = pytest.mark.asyncio` (see
# tests/test_backup.py's own note): pyproject.toml's asyncio_mode =
# "auto" already runs async tests correctly, and this file has one
# plain sync test (the dedup key) that the marker would otherwise warn
# about.

NOW = datetime.datetime(2026, 9, 26, 12, 0, tzinfo=datetime.timezone.utc)


class FakeBot:
    """A minimal stand-in for aiogram's Bot: just enough for
    run_library_digest's one `send_message` call."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


async def _seed_state(sessionmaker, **overrides) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=555, timezone="UTC", **overrides))
        await session.commit()


# --- scheduling --------------------------------------------------------


async def test_maybe_enqueue_library_digest_only_after_digest_time(sessionmaker):
    settings = Settings(CLAUDE_ACCESS_ENABLED=True)
    before = FrozenClock(
        datetime.datetime.combine(datetime.date(2026, 1, 5), CLAUDE_LIBRARY_DIGEST_TIME)
        .replace(tzinfo=datetime.timezone.utc)
        - datetime.timedelta(minutes=1)
    )
    at_or_after = FrozenClock(
        datetime.datetime.combine(datetime.date(2026, 1, 5), CLAUDE_LIBRARY_DIGEST_TIME).replace(
            tzinfo=datetime.timezone.utc
        )
    )
    async with sessionmaker() as session:
        assert await maybe_enqueue_library_digest(session, settings, before, "UTC") is False
    async with sessionmaker() as session:
        assert await maybe_enqueue_library_digest(session, settings, at_or_after, "UTC") is True
    async with sessionmaker() as session:
        jobs = (
            (await session.execute(select(Job).where(Job.kind == CLAUDE_LIBRARY_DIGEST)))
            .scalars()
            .all()
        )
    assert len(jobs) == 1
    assert jobs[0].payload == {"local_date": "2026-01-05"}


async def test_maybe_enqueue_library_digest_disabled_never_enqueues(sessionmaker):
    settings = Settings(CLAUDE_ACCESS_ENABLED=False)
    clock = FrozenClock(NOW)
    async with sessionmaker() as session:
        assert await maybe_enqueue_library_digest(session, settings, clock, "UTC") is False


async def test_maybe_enqueue_library_digest_is_once_per_local_date(sessionmaker):
    settings = Settings(CLAUDE_ACCESS_ENABLED=True)
    clock = FrozenClock(datetime.datetime(2026, 9, 26, 21, 30, tzinfo=datetime.timezone.utc))
    async with sessionmaker() as session:
        first = await maybe_enqueue_library_digest(session, settings, clock, "UTC")
    async with sessionmaker() as session:
        second = await maybe_enqueue_library_digest(session, settings, clock, "UTC")
    assert first is True
    assert second is False


def test_library_digest_dedup_key_is_one_per_local_date():
    day = datetime.date(2026, 9, 26)
    assert library_digest_dedup_key(day) == library_digest_dedup_key(day)
    assert library_digest_dedup_key(day) != library_digest_dedup_key(day + datetime.timedelta(days=1))


# --- run_library_digest --------------------------------------------------


async def test_zero_reads_sends_nothing(sessionmaker):
    await _seed_state(sessionmaker)
    bot = FakeBot()
    clock = FrozenClock(NOW)
    async with sessionmaker() as session:
        await claude_ui.run_library_digest(
            session, Settings(), clock, bot, {"local_date": "2026-09-26"}
        )
    assert bot.sent == []


async def test_activity_sends_the_plural_correct_digest(sessionmaker):
    await _seed_state(sessionmaker)
    day = datetime.date(2026, 9, 26)
    async with sessionmaker() as session:
        for _ in range(3):
            await record_library_read(session, day)
    bot = FakeBot()
    clock = FrozenClock(NOW)
    async with sessionmaker() as session:
        await claude_ui.run_library_digest(
            session, Settings(), clock, bot, {"local_date": "2026-09-26"}
        )
    assert bot.sent == [(555, "Claude за сутки: библиотека — 3 запроса.")]


@pytest.mark.parametrize("n, word", [(1, "запрос"), (2, "запроса"), (5, "запросов"), (11, "запросов")])
async def test_reads_use_the_right_russian_plural(sessionmaker, n, word):
    await _seed_state(sessionmaker)
    day = datetime.date(2026, 9, 26)
    async with sessionmaker() as session:
        for _ in range(n):
            await record_library_read(session, day)
    bot = FakeBot()
    clock = FrozenClock(NOW)
    async with sessionmaker() as session:
        await claude_ui.run_library_digest(
            session, Settings(), clock, bot, {"local_date": "2026-09-26"}
        )
    assert bot.sent == [(555, f"Claude за сутки: библиотека — {n} {word}.")]


async def test_digest_defers_instead_of_sending_in_quiet_hours(sessionmaker):
    settings = Settings(QUIET_START=datetime.time(23, 0), QUIET_END=datetime.time(7, 0))
    await _seed_state(sessionmaker)
    day = datetime.date(2026, 9, 26)
    async with sessionmaker() as session:
        await record_library_read(session, day)
    # 21:00 UTC local (timezone UTC) falls inside 23:00-07:00? No -- pick
    # a clock that is inside the quiet window instead.
    clock = FrozenClock(datetime.datetime(2026, 9, 26, 23, 30, tzinfo=datetime.timezone.utc))
    bot = FakeBot()
    async with sessionmaker() as session:
        with pytest.raises(Deferred) as excinfo:
            await claude_ui.run_library_digest(
                session, settings, clock, bot, {"local_date": "2026-09-26"}
            )
    assert excinfo.value.run_after == clock.now_utc() + claude_ui.DIGEST_RETRY
    assert bot.sent == []


# --- worker dispatch ------------------------------------------------------


async def test_worker_dispatches_the_digest_job(sessionmaker):
    from app.worker import _run_job

    await _seed_state(sessionmaker)
    day = datetime.date(2026, 9, 26)
    async with sessionmaker() as session:
        await record_library_read(session, day)
    bot = FakeBot()
    clock = FrozenClock(NOW)
    async with sessionmaker() as session:
        await _run_job(
            session, Settings(), None, None, bot, clock,
            CLAUDE_LIBRARY_DIGEST, {"local_date": "2026-09-26"},
        )
    assert bot.sent == [(555, "Claude за сутки: библиотека — 1 запрос.")]


async def test_worker_skips_the_send_with_no_bot(sessionmaker):
    """Same "no bot, no send, still done" shape as every other
    Telegram-facing job in app/worker.py's _run_job."""
    from app.worker import _run_job

    await _seed_state(sessionmaker)
    day = datetime.date(2026, 9, 26)
    async with sessionmaker() as session:
        await record_library_read(session, day)
    clock = FrozenClock(NOW)
    async with sessionmaker() as session:
        outcome = await _run_job(
            session, Settings(), None, None, None, clock,
            CLAUDE_LIBRARY_DIGEST, {"local_date": "2026-09-26"},
        )
    assert outcome is not None
