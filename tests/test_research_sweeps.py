"""app/research/sweeps.py (phase-4 plan sections 4, 9, 14).

Plan section 14 names three cases explicitly: "card expiry sweep; clip
text nulled after 30 days; /delete purges and cancels jobs". The third
is tests/test_delete.py's job; this file covers the first two, plus the
scheduler and worker wiring that gets them to run at all.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select

from app.config import Settings
from app.core import scheduler
from app.db.models import Job, Memory, StudyCard, StudyClip, StudyJob, UserState
from app.research import sweeps
from app.worker import _run_job

pytestmark = pytest.mark.asyncio

TZ = "Europe/Paris"


async def _job(session, *, local_date=datetime.date(2026, 9, 22)) -> StudyJob:
    job = StudyJob(kind="read", local_date=local_date, status="done")
    session.add(job)
    await session.flush()
    return job


async def _clip(
    session, job: StudyJob, *, fetched_at=None, text: str | None = "исходный текст страницы"
) -> StudyClip:
    clip = StudyClip(
        job_id=job.id,
        url=f"https://example.com/{job.id}/{id(text)}",
        domain="example.com",
        title="Заголовок страницы",
        text=text,
        text_sha256="deadbeef",
        http_status=200,
        fetched_at=fetched_at,
    )
    session.add(clip)
    await session.flush()
    return clip


def _card(job: StudyJob, clip: StudyClip, *, created_at, status="pending", **overrides) -> StudyCard:
    fields = dict(
        job_id=job.id,
        clip_id=clip.id,
        kind="technique",
        text="Идея своими словами, взятая со страницы.",
        quote="Дословная цитата из текста страницы.",
        source_url=clip.url,
        risk_model="low",
        risk_rules="low",
        risk_final="low",
        status=status,
        created_at=created_at,
    )
    fields.update(overrides)
    return StudyCard(**fields)


async def _refresh(session, row):
    await session.refresh(row)
    return row


# --- expire_cards -------------------------------------------------------


async def test_a_pending_card_past_the_ttl_is_expired(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 22, 12, 0, tz=TZ)
    settings = Settings(RESEARCH_CARD_TTL_DAYS=14)

    async with sessionmaker() as session:
        job = await _job(session)
        clip = await _clip(session, job)
        old = _card(job, clip, created_at=clock.now_utc() - datetime.timedelta(days=15))
        session.add(old)
        await session.commit()

        count = await sweeps.expire_cards(session, settings, clock)
        assert count == 1
        await _refresh(session, old)
        assert old.status == "expired"


async def test_a_pending_card_inside_the_ttl_is_not_expired(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 22, 12, 0, tz=TZ)
    settings = Settings(RESEARCH_CARD_TTL_DAYS=14)

    async with sessionmaker() as session:
        job = await _job(session)
        clip = await _clip(session, job)
        fresh = _card(job, clip, created_at=clock.now_utc() - datetime.timedelta(days=5))
        session.add(fresh)
        await session.commit()

        count = await sweeps.expire_cards(session, settings, clock)
        assert count == 0
        await _refresh(session, fresh)
        assert fresh.status == "pending"


async def test_a_card_exactly_at_the_ttl_boundary_is_not_expired(sessionmaker, frozen_clock):
    """"Older than" the TTL, not "at least" it -- a card exactly
    RESEARCH_CARD_TTL_DAYS old is still inside its window, the same
    half-open convention the outbound grace windows use."""
    clock = frozen_clock(2026, 9, 22, 12, 0, tz=TZ)
    settings = Settings(RESEARCH_CARD_TTL_DAYS=14)

    async with sessionmaker() as session:
        job = await _job(session)
        clip = await _clip(session, job)
        boundary = _card(
            job, clip, created_at=clock.now_utc() - datetime.timedelta(days=14)
        )
        session.add(boundary)
        await session.commit()

        count = await sweeps.expire_cards(session, settings, clock)
        assert count == 0
        await _refresh(session, boundary)
        assert boundary.status == "pending"


async def test_adopted_rejected_and_hidden_cards_are_never_touched(sessionmaker, frozen_clock):
    """A decision the user already made must survive the sweep -- and a
    risk_final='high' hidden card must stay hidden, or
    ck_study_card_high_is_hidden would fail the whole sweep."""
    clock = frozen_clock(2026, 9, 22, 12, 0, tz=TZ)
    settings = Settings(RESEARCH_CARD_TTL_DAYS=14)
    ancient = clock.now_utc() - datetime.timedelta(days=400)

    async with sessionmaker() as session:
        job = await _job(session)
        clip = await _clip(session, job)

        memory = Memory(kind="technique", text="Приём, который приняли.", source="adopt")
        session.add(memory)
        await session.flush()

        adopted = _card(
            job, clip, created_at=ancient, status="adopted", memory_id=memory.id
        )
        rejected = _card(job, clip, created_at=ancient, status="rejected")
        hidden = _card(
            job, clip, created_at=ancient, status="hidden",
            risk_model="high", risk_rules="high", risk_final="high",
        )
        still_pending = _card(job, clip, created_at=ancient)
        session.add_all([adopted, rejected, hidden, still_pending])
        await session.commit()

        # No IntegrityError from ck_study_card_high_is_hidden: the sweep
        # only ever matches status='pending' rows, so the hidden card is
        # never a candidate for the UPDATE in the first place.
        count = await sweeps.expire_cards(session, settings, clock)
        assert count == 1

        for row in (adopted, rejected, hidden):
            await _refresh(session, row)
        assert adopted.status == "adopted"
        assert rejected.status == "rejected"
        assert hidden.status == "hidden"
        await _refresh(session, still_pending)
        assert still_pending.status == "expired"


async def test_expire_cards_is_idempotent(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 22, 12, 0, tz=TZ)
    settings = Settings(RESEARCH_CARD_TTL_DAYS=14)

    async with sessionmaker() as session:
        job = await _job(session)
        clip = await _clip(session, job)
        session.add(_card(job, clip, created_at=clock.now_utc() - datetime.timedelta(days=30)))
        await session.commit()

        first = await sweeps.expire_cards(session, settings, clock)
        second = await sweeps.expire_cards(session, settings, clock)
        assert first == 1
        assert second == 0


# --- forget_clip_text ----------------------------------------------------


async def test_clip_text_is_nulled_after_the_retention_window(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 22, 12, 0, tz=TZ)

    async with sessionmaker() as session:
        job = await _job(session)
        old_clip = await _clip(
            session, job, fetched_at=clock.now_utc() - datetime.timedelta(days=31)
        )
        await session.commit()

        count = await sweeps.forget_clip_text(session, clock)
        assert count == 1
        await _refresh(session, old_clip)
        assert old_clip.text is None


async def test_clip_text_is_kept_before_the_retention_window(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 22, 12, 0, tz=TZ)

    async with sessionmaker() as session:
        job = await _job(session)
        recent_clip = await _clip(
            session, job, fetched_at=clock.now_utc() - datetime.timedelta(days=10)
        )
        await session.commit()

        count = await sweeps.forget_clip_text(session, clock)
        assert count == 0
        await _refresh(session, recent_clip)
        assert recent_clip.text == "исходный текст страницы"


async def test_a_clip_with_no_fetched_at_is_untouched(sessionmaker, frozen_clock):
    """A failed fetch (app/research/fetch.py's FetchFailure path) never
    had text, and NULL < cutoff is SQL NULL, not true -- the explicit
    `fetched_at IS NOT NULL` filter is what actually guarantees this,
    not an accident of the comparison."""
    clock = frozen_clock(2026, 9, 22, 12, 0, tz=TZ)

    async with sessionmaker() as session:
        job = await _job(session)
        failed_clip = StudyClip(
            job_id=job.id,
            url="https://example.com/blocked",
            domain="example.com",
            text=None,
            fetch_error="blocked_private_ip",
            fetched_at=None,
        )
        session.add(failed_clip)
        await session.commit()

        # days=0 would otherwise catch everything; it must still spare
        # a clip that was never fetched.
        count = await sweeps.forget_clip_text(session, clock, days=0)
        assert count == 0
        await _refresh(session, failed_clip)
        assert failed_clip.text is None
        assert failed_clip.fetch_error == "blocked_private_ip"


async def test_forget_clip_text_is_idempotent(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 22, 12, 0, tz=TZ)

    async with sessionmaker() as session:
        job = await _job(session)
        await _clip(session, job, fetched_at=clock.now_utc() - datetime.timedelta(days=45))
        await session.commit()

        first = await sweeps.forget_clip_text(session, clock)
        second = await sweeps.forget_clip_text(session, clock)
        assert first == 1
        assert second == 0


async def test_forgetting_clip_text_keeps_metadata_and_an_adopted_cards_own_text(
    sessionmaker, frozen_clock
):
    """The clip's row survives with everything but its text; a card
    adopted from it keeps its own quote and text regardless, because
    those live on study_card, not on the clip (plan section 4)."""
    clock = frozen_clock(2026, 9, 22, 12, 0, tz=TZ)

    async with sessionmaker() as session:
        job = await _job(session)
        clip = await _clip(
            session, job, fetched_at=clock.now_utc() - datetime.timedelta(days=60)
        )
        memory = Memory(kind="technique", text="Ложиться в одно и то же время.", source="adopt")
        session.add(memory)
        await session.flush()
        card = _card(
            job, clip,
            created_at=clock.now_utc(),
            status="adopted",
            memory_id=memory.id,
            text="Ложиться в одно и то же время.",
            quote="исходный текст страницы",
        )
        session.add(card)
        await session.commit()

        count = await sweeps.forget_clip_text(session, clock)
        assert count == 1

        await _refresh(session, clip)
        assert clip.text is None
        assert clip.title == "Заголовок страницы"
        assert clip.domain == "example.com"
        assert clip.text_sha256 == "deadbeef"
        assert clip.http_status == 200
        assert clip.url.startswith("https://example.com/")

        await _refresh(session, card)
        assert card.text == "Ложиться в одно и то же время."
        assert card.quote == "исходный текст страницы"
        assert card.status == "adopted"


# --- run_daily_sweep: both, and their counts -----------------------------


async def test_run_daily_sweep_returns_both_counts(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 22, 12, 0, tz=TZ)
    settings = Settings(RESEARCH_CARD_TTL_DAYS=14)

    async with sessionmaker() as session:
        job = await _job(session)
        expiring_clip = await _clip(session, job)
        old_text_clip = await _clip(
            session, job, fetched_at=clock.now_utc() - datetime.timedelta(days=31)
        )
        session.add(
            _card(job, expiring_clip, created_at=clock.now_utc() - datetime.timedelta(days=20))
        )
        await session.commit()

        expired, forgotten = await sweeps.run_daily_sweep(session, settings, clock)
        assert expired == 1
        assert forgotten == 1


# --- scheduler wiring: once per local day, safe twice ---------------------


async def _seed_state(session, *, timezone=TZ) -> None:
    session.add(UserState(id=1, chat_id=555, timezone=timezone))
    await session.commit()


async def test_maybe_enqueue_research_sweep_queues_once_per_local_day(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 22, 3, 0, tz=TZ)

    async with sessionmaker() as session:
        first = await scheduler.maybe_enqueue_research_sweep(session, clock, TZ)
        second = await scheduler.maybe_enqueue_research_sweep(session, clock, TZ)
    assert first is True
    assert second is False

    async with sessionmaker() as session:
        rows = (
            await session.execute(select(Job).where(Job.kind == sweeps.RESEARCH_SWEEP))
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "pending"


async def test_maybe_enqueue_research_sweep_queues_again_the_next_local_day(
    sessionmaker, frozen_clock
):
    clock = frozen_clock(2026, 9, 22, 23, 59, tz=TZ)

    async with sessionmaker() as session:
        await scheduler.maybe_enqueue_research_sweep(session, clock, TZ)

    clock.advance(datetime.timedelta(minutes=2))  # rolls past local midnight

    async with sessionmaker() as session:
        enqueued_again = await scheduler.maybe_enqueue_research_sweep(session, clock, TZ)
    assert enqueued_again is True

    async with sessionmaker() as session:
        rows = (
            await session.execute(select(Job).where(Job.kind == sweeps.RESEARCH_SWEEP))
        ).scalars().all()
    assert len(rows) == 2


async def test_heartbeat_itself_never_enqueues_the_sweep(sessionmaker, frozen_clock):
    """`maybe_enqueue_research_sweep` is deliberately not called from
    inside `heartbeat()` (see the scheduler module docstring): several
    of tests/test_scheduler.py's, tests/test_outbound_send.py's and
    tests/test_tick.py's tests call `heartbeat()` directly and assert an
    exact `job` table state afterwards, including zero rows on a
    refused gate. This is the guard against a regression that would
    reintroduce exactly that breakage -- app/worker.py's `_heartbeat_
    loop` is what actually queues the sweep, as a sibling call."""
    clock = frozen_clock(2026, 9, 22, 15, 0, tz=TZ)
    settings = Settings()

    async with sessionmaker() as session:
        await _seed_state(session)

    async with sessionmaker() as session:
        await scheduler.heartbeat(session, settings, clock)

    async with sessionmaker() as session:
        rows = (await session.execute(select(Job))).scalars().all()
    assert rows == []


async def test_the_heartbeat_loops_own_composition_still_queues_the_sweep(
    sessionmaker, frozen_clock
):
    """What app/worker.py's `_heartbeat_loop` actually does each minute:
    `heartbeat()`, then `maybe_enqueue_research_sweep()`, in separate
    sessions. Exercised here as that same sequence, since the loop
    itself is an infinite `while True` with no seam to call once."""
    clock = frozen_clock(2026, 9, 22, 15, 0, tz=TZ)
    settings = Settings()

    async with sessionmaker() as session:
        await _seed_state(session, timezone=TZ)

    async with sessionmaker() as session:
        await scheduler.heartbeat(session, settings, clock)
    async with sessionmaker() as session:
        await scheduler.maybe_enqueue_research_sweep(session, clock, TZ)

    async with sessionmaker() as session:
        rows = (
            await session.execute(select(Job).where(Job.kind == sweeps.RESEARCH_SWEEP))
        ).scalars().all()
    assert len(rows) == 1


# --- worker dispatch -------------------------------------------------------


async def test_worker_dispatches_the_research_sweep_job(sessionmaker, frozen_clock, fake_llm_provider):
    """app/worker.py's _run_job routes RESEARCH_SWEEP to run_daily_sweep
    without needing a provider or a bot, unlike every other job kind."""
    clock = frozen_clock(2026, 9, 22, 12, 0, tz=TZ)
    settings = Settings(RESEARCH_CARD_TTL_DAYS=14)

    async with sessionmaker() as session:
        await _seed_state(session)
        job = await _job(session)
        clip = await _clip(session, job)
        session.add(_card(job, clip, created_at=clock.now_utc() - datetime.timedelta(days=20)))
        await session.commit()

        await _run_job(
            session, settings, None, fake_llm_provider, None, clock,
            sweeps.RESEARCH_SWEEP, {},
        )

        expired = (
            await session.execute(select(StudyCard).where(StudyCard.status == "expired"))
        ).scalars().all()
    assert len(expired) == 1
