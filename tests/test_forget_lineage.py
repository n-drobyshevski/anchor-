"""app/core/memory.py's `forget_lineage` (phase-8 plan sections 7.2 and
18.1): forgetting any id in a supersede chain forgets the whole chain,
the same as deleting a fact's file in the vault does.

`forget()`'s own delegation to this is covered where `forget()` already
had tests (tests/test_core_memory_ops.py, tests/test_memory.py); this
file exercises `forget_lineage` directly, including `commit=False`,
which nothing calls through `forget()` today.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select

from app.core import memory
from app.db.models import Memory, StateChange, StudyCard, StudyClip, StudyJob

pytestmark = pytest.mark.asyncio


async def _write(session, *, kind="identity", txt="факт", source="user", **kw):
    row = Memory(kind=kind, text=txt, source=source, **kw)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def _chain(session):
    """a -> b -> c, c the head."""
    a = await _write(session, txt="факт А")
    b = await _write(session, txt="факт Б")
    c = await _write(session, txt="факт В")
    a.superseded_by = b.id
    b.superseded_by = c.id
    await session.commit()
    return a, b, c


async def _adopted_card(session, memory_id: int) -> StudyCard:
    """A StudyCard shaped like app/core/cards.py's `adopt` leaves one."""
    job = StudyJob(kind="read", local_date=datetime.date(2026, 1, 1), status="done")
    session.add(job)
    await session.flush()
    clip = StudyClip(job_id=job.id, url="https://example.test/sleep", domain="example.test", text="т")
    session.add(clip)
    await session.flush()
    card = StudyCard(
        job_id=job.id,
        clip_id=clip.id,
        kind="technique",
        text="дыши перед сном",
        quote="q",
        source_url=clip.url,
        risk_model="low",
        risk_rules="low",
        risk_final="low",
        status="adopted",
        memory_id=memory_id,
    )
    session.add(card)
    await session.commit()
    await session.refresh(card)
    return card


async def test_forget_lineage_removes_the_whole_chain(sessionmaker):
    async with sessionmaker() as session:
        a, b, c = await _chain(session)
        outcome = await memory.forget_lineage(session, c.id, source="vault")
    assert outcome == memory.FORGET_OK
    async with sessionmaker() as session:
        remaining = (await session.execute(select(Memory))).scalars().all()
    assert remaining == []


async def test_forget_lineage_from_a_non_head_id_forgets_the_whole_lineage(sessionmaker):
    """Called with `a` -- the oldest, most-superseded row -- rather than
    the head `c`, forget_lineage still resolves forward and forgets all
    three, not just `a`."""
    async with sessionmaker() as session:
        a, b, c = await _chain(session)
        outcome = await memory.forget_lineage(session, a.id, source="vault")
    assert outcome == memory.FORGET_OK
    async with sessionmaker() as session:
        for row_id in (a.id, b.id, c.id):
            assert await session.get(Memory, row_id) is None


async def test_forget_lineage_protected_by_a_real_adopted_card_changes_nothing(sessionmaker):
    """An adopted card pointing at a *predecessor* still protects the
    whole lineage: once the chain is forgotten there is nothing left
    outside it to relink the card to, unlike a plain hard_delete."""
    async with sessionmaker() as session:
        old = await _write(session, kind="technique", txt="дыши перед сном", source="adopt")
        card = await _adopted_card(session, old.id)
        new = await memory.write_memory(
            session, kind="technique", text="дыши глубже перед сном", source="user",
            supersedes_id=old.id,
        )
        outcome = await memory.forget_lineage(session, old.id, source="vault")
    assert outcome == memory.FORGET_PROTECTED

    async with sessionmaker() as session:
        assert await session.get(Memory, old.id) is not None
        assert await session.get(Memory, new.id) is not None
        refreshed_card = await session.get(StudyCard, card.id)
        assert (refreshed_card.status, refreshed_card.memory_id) == ("adopted", old.id)
        changes = (await session.execute(select(StateChange))).scalars().all()
    assert changes == []


async def test_forget_lineage_writes_exactly_one_content_free_audit_row(sessionmaker):
    async with sessionmaker() as session:
        a, b, c = await _chain(session)
        outcome = await memory.forget_lineage(session, b.id, source="vault")
    assert outcome == memory.FORGET_OK

    async with sessionmaker() as session:
        changes = (await session.execute(select(StateChange))).scalars().all()
    assert len(changes) == 1
    audit = changes[0]
    assert audit.field == "memory"
    assert audit.old_value == str(c.id), "the head's id, even though b.id was passed"
    assert audit.new_value is None
    assert audit.source == "vault"
    for value in (audit.field, audit.old_value, audit.new_value):
        assert value is None or "факт" not in value


async def test_forget_lineage_missing_id_returns_missing(sessionmaker):
    async with sessionmaker() as session:
        outcome = await memory.forget_lineage(session, 999_999, source="vault")
    assert outcome == memory.FORGET_MISSING
    async with sessionmaker() as session:
        changes = (await session.execute(select(StateChange))).scalars().all()
    assert changes == []


async def test_forget_lineage_commit_false_does_not_commit(sessionmaker):
    async with sessionmaker() as session:
        row = await _write(session, txt="факт")
        row_id = row.id

    async with sessionmaker() as session:
        outcome = await memory.forget_lineage(session, row_id, source="vault", commit=False)
        assert outcome == memory.FORGET_OK
        await session.rollback()

    async with sessionmaker() as session:
        assert await session.get(Memory, row_id) is not None
        changes = (await session.execute(select(StateChange))).scalars().all()
    assert changes == []
