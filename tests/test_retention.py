"""app/core/retention.py -- the daily retention sweeps (Phase 6 plan
section 9.4; milestone 6e; plan section 12's "retention" test list).
"""

from __future__ import annotations

import datetime

from sqlalchemy import select

from app.config import Settings
from app.core import retention
from app.core.clock import FrozenClock
from app.db.models import Job, Message, Outbound, Scene, TelegramUpdate, WeeklyReview

# No module-level pytest.mark.asyncio: this file mixes async DB tests
# with a couple of plain sync ones (the dedup key), and pyproject.toml's
# asyncio_mode = "auto" already detects the async ones on its own -- see
# tests/test_backup.py's identical note.

NOW = datetime.datetime(2026, 1, 30, 12, 0, tzinfo=datetime.timezone.utc)


def _settings(**overrides) -> Settings:
    return Settings(**overrides)


# --- telegram_update.payload -------------------------------------------


async def test_forget_update_payloads_nulls_old_payload_only(sessionmaker):
    settings = _settings(UPDATE_PAYLOAD_RETENTION_DAYS=30)
    old = NOW - datetime.timedelta(days=31)
    fresh = NOW - datetime.timedelta(days=1)
    async with sessionmaker() as session:
        session.add(TelegramUpdate(update_id=1, payload={"a": 1}, created_at=old))
        session.add(TelegramUpdate(update_id=2, payload={"b": 2}, created_at=fresh))
        await session.commit()

        count = await retention.forget_update_payloads(session, settings, FrozenClock(NOW))
    assert count == 1

    async with sessionmaker() as session:
        rows = {row.update_id: row.payload for row in (await session.execute(select(TelegramUpdate))).scalars()}
    assert rows[1] is None
    assert rows[2] == {"b": 2}


async def test_forget_update_payloads_is_idempotent(sessionmaker):
    settings = _settings(UPDATE_PAYLOAD_RETENTION_DAYS=30)
    old = NOW - datetime.timedelta(days=31)
    async with sessionmaker() as session:
        session.add(TelegramUpdate(update_id=1, payload={"a": 1}, created_at=old))
        await session.commit()
        first = await retention.forget_update_payloads(session, settings, FrozenClock(NOW))
        second = await retention.forget_update_payloads(session, settings, FrozenClock(NOW))
    assert first == 1
    assert second == 0


# --- terminal job rows ---------------------------------------------------


async def test_forget_terminal_jobs_deletes_only_done_and_failed_past_the_window(sessionmaker):
    settings = _settings(JOB_RETENTION_DAYS=30)
    old = NOW - datetime.timedelta(days=31)
    fresh = NOW - datetime.timedelta(days=1)
    async with sessionmaker() as session:
        session.add(Job(kind="extract", payload={}, status="done", created_at=old))
        session.add(Job(kind="extract", payload={}, status="failed", created_at=old))
        session.add(Job(kind="extract", payload={}, status="done", created_at=fresh))
        session.add(Job(kind="extract", payload={}, status="pending", created_at=old))
        await session.commit()

        count = await retention.forget_terminal_jobs(session, settings, FrozenClock(NOW))
    assert count == 2

    async with sessionmaker() as session:
        remaining = {row.status for row in (await session.execute(select(Job))).scalars()}
    assert remaining == {"done", "pending"}  # the fresh done row and the pending row survive


# --- old messages, only when the scene is summarized ----------------------


async def _scene(session, *, summary: str | None) -> Scene:
    scene = Scene(started_at=NOW - datetime.timedelta(days=60), summary=summary)
    session.add(scene)
    await session.flush()
    return scene


async def test_forget_old_messages_noop_when_setting_is_zero(sessionmaker):
    settings = _settings(MESSAGE_RETENTION_DAYS=0)
    async with sessionmaker() as session:
        scene = await _scene(session, summary="итог сцены")
        session.add(
            Message(
                role="user", content="привет", scene_id=scene.id,
                created_at=NOW - datetime.timedelta(days=999),
            )
        )
        await session.commit()
        count = await retention.forget_old_messages(session, settings, FrozenClock(NOW))
    assert count == 0

    async with sessionmaker() as session:
        assert (await session.execute(select(Message))).scalars().all()


async def test_forget_old_messages_only_when_scene_is_summarized(sessionmaker):
    settings = _settings(MESSAGE_RETENTION_DAYS=30)
    old = NOW - datetime.timedelta(days=60)
    async with sessionmaker() as session:
        summarized = await _scene(session, summary="итог")
        unsummarized = await _scene(session, summary=None)
        session.add(Message(role="user", content="a", scene_id=summarized.id, created_at=old))
        session.add(Message(role="user", content="b", scene_id=unsummarized.id, created_at=old))
        await session.commit()

        count = await retention.forget_old_messages(session, settings, FrozenClock(NOW))
    assert count == 1

    async with sessionmaker() as session:
        remaining = [row.content for row in (await session.execute(select(Message))).scalars()]
    assert remaining == ["b"]


async def test_forget_old_messages_respects_the_cutoff(sessionmaker):
    settings = _settings(MESSAGE_RETENTION_DAYS=30)
    async with sessionmaker() as session:
        scene = await _scene(session, summary="итог")
        session.add(
            Message(
                role="user", content="fresh", scene_id=scene.id,
                created_at=NOW - datetime.timedelta(days=1),
            )
        )
        await session.commit()
        count = await retention.forget_old_messages(session, settings, FrozenClock(NOW))
    assert count == 0


async def test_forget_old_messages_never_deletes_a_message_outbound_points_at(sessionmaker):
    """FK safety: outbound.message_id -> message.id has no ON DELETE
    action -- deleting a referenced message would raise, so the sweep
    must exclude it rather than crash."""
    settings = _settings(MESSAGE_RETENTION_DAYS=30)
    old = NOW - datetime.timedelta(days=60)
    async with sessionmaker() as session:
        scene = await _scene(session, summary="итог")
        message = Message(role="assistant", content="привет", scene_id=scene.id, created_at=old)
        session.add(message)
        await session.flush()
        session.add(
            Outbound(
                kind="morning", local_date=datetime.date(2026, 1, 1), planned_for=old,
                status="sent", message_id=message.id,
            )
        )
        await session.commit()

        count = await retention.forget_old_messages(session, settings, FrozenClock(NOW))
    assert count == 0

    async with sessionmaker() as session:
        assert (await session.execute(select(Message))).scalars().all()


async def test_forget_old_messages_never_deletes_a_message_weekly_review_points_at(sessionmaker):
    """Same FK-safety guarantee for weekly_review.message_id."""
    settings = _settings(MESSAGE_RETENTION_DAYS=30)
    old = NOW - datetime.timedelta(days=60)
    async with sessionmaker() as session:
        scene = await _scene(session, summary="итог")
        message = Message(role="assistant", content="итог недели", scene_id=scene.id, created_at=old)
        session.add(message)
        await session.flush()
        session.add(
            WeeklyReview(
                week_start=datetime.date(2026, 1, 5), analysis={}, message_id=message.id
            )
        )
        await session.commit()

        count = await retention.forget_old_messages(session, settings, FrozenClock(NOW))
    assert count == 0


# --- the combined job and dedup key --------------------------------------


async def test_run_retention_sweep_runs_all_three_rules(sessionmaker):
    settings = _settings(
        UPDATE_PAYLOAD_RETENTION_DAYS=30, JOB_RETENTION_DAYS=30, MESSAGE_RETENTION_DAYS=30
    )
    old = NOW - datetime.timedelta(days=60)
    async with sessionmaker() as session:
        session.add(TelegramUpdate(update_id=1, payload={"a": 1}, created_at=old))
        session.add(Job(kind="extract", payload={}, status="done", created_at=old))
        scene = await _scene(session, summary="итог")
        session.add(Message(role="user", content="старое", scene_id=scene.id, created_at=old))
        await session.commit()

        await retention.run_retention_sweep(session, settings, FrozenClock(NOW))

    async with sessionmaker() as session:
        update = (await session.execute(select(TelegramUpdate))).scalars().one()
        assert update.payload is None
        assert (await session.execute(select(Job))).scalars().all() == []
        assert (await session.execute(select(Message))).scalars().all() == []


def test_retention_sweep_dedup_key_is_one_per_local_date():
    d = datetime.date(2026, 1, 30)
    assert retention.retention_sweep_dedup_key(d) == retention.retention_sweep_dedup_key(d)
    assert retention.retention_sweep_dedup_key(d) != retention.retention_sweep_dedup_key(
        datetime.date(2026, 1, 31)
    )


async def test_maybe_enqueue_retention_sweep_is_once_per_local_date(sessionmaker):
    from app.core.scheduler import maybe_enqueue_retention_sweep
    from app.db.models import Job

    clock = FrozenClock(NOW)
    async with sessionmaker() as session:
        first = await maybe_enqueue_retention_sweep(session, clock, "UTC")
    async with sessionmaker() as session:
        second = await maybe_enqueue_retention_sweep(session, clock, "UTC")
    assert first is True
    assert second is False

    async with sessionmaker() as session:
        jobs = (
            (await session.execute(select(Job).where(Job.kind == "retention_sweep")))
            .scalars()
            .all()
        )
    assert len(jobs) == 1


# --- worker dispatch -------------------------------------------------------


async def test_worker_dispatches_the_retention_sweep_job(sessionmaker):
    """app/worker.py's _run_job routes `retention_sweep` to
    run_retention_sweep without needing a provider or a bot."""
    from app.db.models import UserState
    from app.worker import _run_job

    settings = _settings(
        UPDATE_PAYLOAD_RETENTION_DAYS=30, JOB_RETENTION_DAYS=30, MESSAGE_RETENTION_DAYS=0
    )
    clock = FrozenClock(NOW)
    old = NOW - datetime.timedelta(days=60)

    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=555, timezone="UTC"))
        session.add(TelegramUpdate(update_id=1, payload={"a": 1}, created_at=old))
        await session.commit()
        await _run_job(
            session, settings, None, None, None, clock, retention.RETENTION_SWEEP, {}
        )

    async with sessionmaker() as session:
        update = (await session.execute(select(TelegramUpdate))).scalars().one()
    assert update.payload is None
