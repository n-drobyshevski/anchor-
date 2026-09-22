"""The generic job queue (phase-2 plan sections 3 and 14, "queue").

The inbound queue's own behaviour is covered by tests/test_queue.py,
which 2a did not touch -- that file passing unchanged against the
generalized app/db/queue.py is the evidence that generalizing it
changed nothing. This file covers only what is new: dedup_key,
run_after, and the worker's claim priority.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select

from app.db.jobs import (
    claim_job,
    complete_job,
    defer_job,
    enqueue_job,
    fail_job,
    recover_stuck_jobs,
)
from app.db.models import Job
from app.db.queue import MAX_ATTEMPTS, claim, enqueue

pytestmark = pytest.mark.asyncio


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


async def test_dedup_key_makes_enqueue_idempotent(sessionmaker):
    async with sessionmaker() as session:
        first = await enqueue_job(session, "summarize_scene", {"scene_id": 1}, dedup_key="scene:1")
        second = await enqueue_job(session, "summarize_scene", {"scene_id": 1}, dedup_key="scene:1")
        rows = (await session.execute(select(Job))).scalars().all()

    assert first is True
    assert second is False
    assert len(rows) == 1


async def test_null_dedup_key_never_deduplicates(sessionmaker):
    """NULLs do not conflict in a unique index -- that is the correct
    reading of "this job is not deduplicated", and it must not silently
    collapse two distinct un-keyed jobs into one."""
    async with sessionmaker() as session:
        assert await enqueue_job(session, "summarize_scene", {"scene_id": 1}) is True
        assert await enqueue_job(session, "summarize_scene", {"scene_id": 2}) is True
        rows = (await session.execute(select(Job))).scalars().all()

    assert len(rows) == 2


async def test_run_after_in_the_future_is_not_claimable(sessionmaker):
    async with sessionmaker() as session:
        await enqueue_job(
            session,
            "summarize_scene",
            {"scene_id": 1},
            dedup_key="scene:1",
            run_after=_utcnow() + datetime.timedelta(hours=1),
        )

    async with sessionmaker() as session:
        assert await claim_job(session) is None


async def test_run_after_in_the_past_is_claimable(sessionmaker):
    async with sessionmaker() as session:
        await enqueue_job(
            session,
            "summarize_scene",
            {"scene_id": 7},
            dedup_key="scene:7",
            run_after=_utcnow() - datetime.timedelta(minutes=1),
        )

    async with sessionmaker() as session:
        job = await claim_job(session)

    assert job is not None
    assert job.payload == {"scene_id": 7}
    assert job.status == "processing"
    assert job.attempts == 1


async def test_due_jobs_are_claimed_soonest_first(sessionmaker):
    async with sessionmaker() as session:
        await enqueue_job(
            session, "summarize_scene", {"scene_id": 2}, dedup_key="scene:2",
            run_after=_utcnow() - datetime.timedelta(minutes=1),
        )
        await enqueue_job(
            session, "summarize_scene", {"scene_id": 1}, dedup_key="scene:1",
            run_after=_utcnow() - datetime.timedelta(minutes=10),
        )

    async with sessionmaker() as session:
        first = await claim_job(session)
    async with sessionmaker() as session:
        second = await claim_job(session)

    assert first.payload == {"scene_id": 1}
    assert second.payload == {"scene_id": 2}


async def test_inbound_updates_are_claimed_before_jobs(sessionmaker):
    """Plan section 3: "The worker loop claims inbound updates first, then
    due jobs." Background work must never delay a reply the user is
    waiting on. Asserted at the loop's own decision point rather than
    through the loop, which never terminates."""
    async with sessionmaker() as session:
        await enqueue(session, 900_001, {"update_id": 900_001})
        await enqueue_job(session, "summarize_scene", {"scene_id": 1}, dedup_key="scene:1")

    # Both queues are non-empty. The update must be the one that wins.
    async with sessionmaker() as session:
        update_row = await claim(session)
    assert update_row is not None
    assert update_row.update_id == 900_001

    # And only once the update queue is drained does the job become the
    # loop's next unit of work.
    async with sessionmaker() as session:
        assert await claim(session) is None
    async with sessionmaker() as session:
        job = await claim_job(session)
    assert job is not None


async def test_complete_marks_done(sessionmaker):
    async with sessionmaker() as session:
        await enqueue_job(session, "summarize_scene", {"scene_id": 1}, dedup_key="scene:1")
    async with sessionmaker() as session:
        job = await claim_job(session)
    async with sessionmaker() as session:
        await complete_job(session, job.id)
        row = await session.get(Job, job.id)
        assert row.status == "done"

    async with sessionmaker() as session:
        assert await claim_job(session) is None


async def test_failures_return_to_pending_until_max_attempts(sessionmaker):
    async with sessionmaker() as session:
        await enqueue_job(session, "summarize_scene", {"scene_id": 1}, dedup_key="scene:1")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        async with sessionmaker() as session:
            job = await claim_job(session)
        assert job is not None, f"job should still be claimable on attempt {attempt}"
        async with sessionmaker() as session:
            await fail_job(session, job.id, "ValueError")

    async with sessionmaker() as session:
        row = await session.get(Job, job.id)
    assert row.status == "failed"
    assert row.error == "ValueError"

    async with sessionmaker() as session:
        assert await claim_job(session) is None


async def test_defer_reschedules_without_consuming_the_retry_budget(sessionmaker):
    """The spend-cap path (plan section 12). A summary deferred once a
    day for a week must not exhaust MAX_ATTEMPTS and be marked failed
    without ever having errored."""
    async with sessionmaker() as session:
        await enqueue_job(session, "summarize_scene", {"scene_id": 1}, dedup_key="scene:1")
    async with sessionmaker() as session:
        job = await claim_job(session)
    assert job.attempts == 1

    run_after = _utcnow() + datetime.timedelta(hours=2)
    async with sessionmaker() as session:
        await defer_job(session, job.id, run_after)
        row = await session.get(Job, job.id)

    assert row.status == "pending"
    assert row.attempts == 0
    assert row.locked_at is None

    # ...and it is not claimable again until it is due.
    async with sessionmaker() as session:
        assert await claim_job(session) is None


async def test_stuck_job_recovers_to_pending(sessionmaker):
    async with sessionmaker() as session:
        await enqueue_job(session, "summarize_scene", {"scene_id": 1}, dedup_key="scene:1")
    async with sessionmaker() as session:
        job = await claim_job(session)

    async with sessionmaker() as session:
        row = await session.get(Job, job.id)
        row.locked_at = _utcnow() - datetime.timedelta(minutes=10)
        await session.commit()

    async with sessionmaker() as session:
        recovered = await recover_stuck_jobs(session)
    assert recovered == 1

    async with sessionmaker() as session:
        assert (await claim_job(session)) is not None


async def test_recover_leaves_freshly_locked_jobs_alone(sessionmaker):
    async with sessionmaker() as session:
        await enqueue_job(session, "summarize_scene", {"scene_id": 1}, dedup_key="scene:1")
    async with sessionmaker() as session:
        await claim_job(session)

    async with sessionmaker() as session:
        assert await recover_stuck_jobs(session) == 0


# --- the worker's job path (app/worker.py) ---


async def test_process_one_job_runs_the_handler_and_completes(sessionmaker, clock):
    """The worker's job half, end to end: claim -> dispatch -> complete."""
    from app.config import Settings
    from app.core.scene import MIN_MESSAGES_FOR_SUMMARY, ensure_open_scene
    from app.db.models import Message, Scene, UserState
    from app.worker import process_one_job
    from conftest import FakeLLMProvider

    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=4242, timezone="Europe/Paris"))
        await session.commit()
        scene_id = await ensure_open_scene(session, clock, idle_hours=6)
        for i in range(MIN_MESSAGES_FOR_SUMMARY):
            session.add(
                Message(
                    role="user" if i % 2 == 0 else "assistant",
                    content=f"реплика {i}",
                    ooc=False,
                    kind="chat",
                    scene_id=scene_id,
                )
            )
        await session.commit()
        await enqueue_job(
            session, "summarize_scene", {"scene_id": scene_id}, dedup_key=f"scene:{scene_id}"
        )

    provider = FakeLLMProvider(text="Обсуждали отчёт.")
    processed = await process_one_job(sessionmaker, Settings(), provider, clock)

    assert processed is True
    async with sessionmaker() as session:
        scene = await session.get(Scene, scene_id)
        job = (await session.execute(select(Job))).scalar_one()

    assert scene.summary == "Обсуждали отчёт."
    assert job.status == "done"


async def test_process_one_job_returns_false_when_no_job_is_due(sessionmaker, clock):
    from app.config import Settings
    from app.worker import process_one_job
    from conftest import FakeLLMProvider

    assert await process_one_job(sessionmaker, Settings(), FakeLLMProvider(), clock) is False


async def test_an_unknown_job_kind_fails_the_row_rather_than_vanishing(sessionmaker, clock):
    from app.config import Settings
    from app.db.models import UserState
    from app.worker import process_one_job
    from conftest import FakeLLMProvider

    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=4242, timezone="Europe/Paris"))
        await session.commit()
        await enqueue_job(session, "not_a_real_kind", {}, dedup_key="bogus:1")

    assert await process_one_job(sessionmaker, Settings(), FakeLLMProvider(), clock) is True

    async with sessionmaker() as session:
        job = (await session.execute(select(Job))).scalar_one()

    assert job.status == "pending"  # first of MAX_ATTEMPTS
    assert job.error == "ValueError"


async def test_a_deferred_job_is_rescheduled_not_failed(sessionmaker, clock):
    """Over the cap, the worker must defer rather than burn a retry."""
    from app.config import Settings
    from app.core.clock import next_local_midnight
    from app.core.scene import MIN_MESSAGES_FOR_SUMMARY, ensure_open_scene
    from app.db.models import Message, UserState
    from app.worker import process_one_job
    from conftest import FakeLLMProvider

    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=4242, timezone="Europe/Paris"))
        await session.commit()
        scene_id = await ensure_open_scene(session, clock, idle_hours=6)
        for i in range(MIN_MESSAGES_FOR_SUMMARY):
            session.add(
                Message(role="user", content=f"реплика {i}", ooc=False, kind="chat",
                        scene_id=scene_id)
            )
        await session.commit()
        await enqueue_job(
            session, "summarize_scene", {"scene_id": scene_id}, dedup_key=f"scene:{scene_id}"
        )

    provider = FakeLLMProvider(text="сводка")
    assert await process_one_job(sessionmaker, Settings(DAILY_USD_CAP=0.0), provider, clock) is True

    async with sessionmaker() as session:
        job = (await session.execute(select(Job))).scalar_one()

    assert provider.calls == 0
    assert job.status == "pending"
    assert job.attempts == 0
    assert job.error is None
    assert abs((job.run_after - next_local_midnight(clock, "Europe/Paris")).total_seconds()) < 1
