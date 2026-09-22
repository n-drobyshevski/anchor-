"""The study tables refuse, in SQL, what phase 4 says cannot exist.

Two of `study_card`'s constraints are not shape checks -- they are plan
section 12 invariants written where a bug in app/research/ cannot get
around them:

- a `risk_final='high'` card is never shown and never adoptable, so it
  must be `status='hidden'`;
- adopting a card writes exactly one memory, so an adopted card without
  a `memory_id` is a silent failure.

Milestones 4b and 4c write these rows. This file asserts the floor they
land on, because the code that would violate either does not exist yet
and the constraint should be proven before it does.
"""

from __future__ import annotations

import datetime

import pytest
import sqlalchemy
from sqlalchemy.exc import IntegrityError

from app.db.models import Memory, StudyCard, StudyClip, StudyJob


async def _job_and_clip(session) -> tuple[StudyJob, StudyClip]:
    job = StudyJob(kind="read", local_date=datetime.date(2026, 9, 22))
    session.add(job)
    await session.flush()
    clip = StudyClip(
        job_id=job.id, url="https://example.com/x", domain="example.com", text="текст"
    )
    session.add(clip)
    await session.flush()
    return job, clip


def _card(job, clip, **overrides) -> StudyCard:
    fields = dict(
        job_id=job.id,
        clip_id=clip.id,
        kind="technique",
        text="Ложиться в одно и то же время.",
        quote="Ложитесь спать в одно и то же время каждый день.",
        source_url="https://example.com/x",
        risk_model="low",
        risk_rules="low",
        risk_final="low",
    )
    fields.update(overrides)
    return StudyCard(**fields)


async def test_a_high_risk_card_cannot_be_pending(sessionmaker):
    async with sessionmaker() as session:
        job, clip = await _job_and_clip(session)
        session.add(_card(job, clip, risk_final="high", status="pending"))
        with pytest.raises(IntegrityError, match="ck_study_card_high_is_hidden"):
            await session.commit()


@pytest.mark.parametrize("status", ["pending", "adopted", "rejected", "expired"])
async def test_a_high_risk_card_is_hidden_or_it_is_nothing(sessionmaker, status):
    async with sessionmaker() as session:
        job, clip = await _job_and_clip(session)
        session.add(_card(job, clip, risk_final="high", status=status, memory_id=None))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_a_high_risk_card_stored_hidden_is_fine(sessionmaker):
    """Stored, not dropped: "the filter is working" should be observable
    in /export rather than inferred from an absence."""
    async with sessionmaker() as session:
        job, clip = await _job_and_clip(session)
        session.add(_card(job, clip, risk_model="low", risk_rules="high", risk_final="high", status="hidden"))
        await session.commit()


async def test_an_adopted_card_must_carry_the_memory_it_wrote(sessionmaker):
    async with sessionmaker() as session:
        job, clip = await _job_and_clip(session)
        session.add(_card(job, clip, status="adopted", memory_id=None))
        with pytest.raises(IntegrityError, match="ck_study_card_adopted_has_memory"):
            await session.commit()


async def test_an_adopted_card_with_its_memory_is_fine(sessionmaker):
    async with sessionmaker() as session:
        job, clip = await _job_and_clip(session)
        memory = Memory(kind="technique", text="Ложиться в одно и то же время.", source="adopt")
        session.add(memory)
        await session.flush()
        session.add(_card(job, clip, status="adopted", memory_id=memory.id))
        await session.commit()


async def test_deleting_a_job_takes_its_clips_and_cards(sessionmaker):
    """The cascade /delete's purge and a cancelled job both want. The
    alternative -- a card pointing at a job id that no longer exists --
    is a shape nothing in the plan has a use for."""
    async with sessionmaker() as session:
        job, clip = await _job_and_clip(session)
        session.add(_card(job, clip))
        await session.commit()
        job_id = job.id

        await session.execute(sqlalchemy.delete(StudyJob).where(StudyJob.id == job_id))
        await session.commit()

        for model in (StudyClip, StudyCard):
            remaining = await session.scalar(
                sqlalchemy.select(sqlalchemy.func.count()).select_from(model)
            )
            assert remaining == 0, f"{model.__tablename__} survived its job"


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "mantra"},
        {"risk_model": "extreme"},
        {"risk_rules": "unknown"},
        {"risk_final": "severe"},
        {"status": "maybe"},
        {"text": "x" * 301},
        {"quote": "ц" * 241},
    ],
    ids=["kind", "risk_model", "risk_rules", "risk_final", "status", "text", "quote"],
)
async def test_every_enum_and_length_is_constrained(sessionmaker, overrides):
    async with sessionmaker() as session:
        job, clip = await _job_and_clip(session)
        session.add(_card(job, clip, **overrides))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_lengths_are_counted_in_characters_not_bytes(sessionmaker):
    """300 Cyrillic characters is 600 bytes. char_length, not
    octet_length -- the same choice `memory` made."""
    async with sessionmaker() as session:
        job, clip = await _job_and_clip(session)
        session.add(_card(job, clip, text="ц" * 300, quote="ц" * 240))
        await session.commit()


async def test_rule_hits_defaults_to_an_empty_array(sessionmaker):
    """Rule ids only, never the matched text -- that text comes from a
    fetched page and /export dumps this table."""
    async with sessionmaker() as session:
        job, clip = await _job_and_clip(session)
        card = _card(job, clip)
        session.add(card)
        await session.commit()
        await session.refresh(card)
        assert card.rule_hits == []
