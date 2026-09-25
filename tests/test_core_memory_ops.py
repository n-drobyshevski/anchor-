"""app/core/memory.py's W3 extraction: `set_pinned_capped`, `forget` and
`list_active`'s new filters (plan step 1, "one set of rules").

Telegram parity for the same behavior is covered in
tests/test_memory_commands.py (run_set_pinned/run_forget through the
router); this file exercises the core functions directly, including
cases the Telegram surface never triggers on its own (e.g. pinning a
superseded row).
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select

from app.core import memory
from app.db.models import Memory, StateChange, StudyCard, StudyClip, StudyJob

pytestmark = pytest.mark.asyncio


async def _write(session, *, kind="identity", txt="факт", source="user", pinned=False, **kw):
    row = Memory(kind=kind, text=txt, source=source, pinned=pinned, **kw)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def _adopted_card(session, memory_id: int) -> StudyCard:
    """A StudyCard shaped exactly like app/core/cards.py's `adopt`
    leaves one: status='adopted', memory_id pointing at the given row.
    job_id/clip_id are real FKs (study_card_job_id_fkey/_clip_id_fkey),
    not just plausible-looking ints -- Postgres enforces them."""
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


# --- set_pinned_capped ---


async def test_set_pinned_capped_pins_under_the_cap(sessionmaker):
    async with sessionmaker() as session:
        row = await _write(session, txt="пользователь живёт в Лилле")
        outcome = await memory.set_pinned_capped(session, row.id, True, max_pinned=8)
    assert outcome == memory.PIN_OK
    async with sessionmaker() as session:
        assert (await session.get(Memory, row.id)).pinned is True


async def test_set_pinned_capped_missing_id(sessionmaker):
    async with sessionmaker() as session:
        outcome = await memory.set_pinned_capped(session, 999999, True, max_pinned=8)
    assert outcome == memory.PIN_MISSING


async def test_set_pinned_capped_over_cap_does_not_write(sessionmaker):
    async with sessionmaker() as session:
        pinned_row = await _write(session, txt="пользователь живёт в Лилле", pinned=True)
        extra = await _write(session, txt="по воскресеньям мы ездим к морю")
        outcome = await memory.set_pinned_capped(session, extra.id, True, max_pinned=1)
    assert outcome == memory.PIN_OVER_CAP
    async with sessionmaker() as session:
        assert (await session.get(Memory, extra.id)).pinned is False
        assert (await session.get(Memory, pinned_row.id)).pinned is True


async def test_set_pinned_capped_re_pinning_at_the_cap_is_allowed(sessionmaker):
    """The cap guards adding a *new* pin, not re-affirming one already
    pinned -- same case as
    tests/test_memory_commands.py::test_re_pinning_an_already_pinned_memory_at_the_cap_is_allowed."""
    async with sessionmaker() as session:
        row = await _write(session, txt="факт", pinned=True)
        outcome = await memory.set_pinned_capped(session, row.id, True, max_pinned=1)
    assert outcome == memory.PIN_OK


async def test_set_pinned_capped_unpin_is_never_capped(sessionmaker):
    async with sessionmaker() as session:
        row = await _write(session, txt="факт", pinned=True)
        outcome = await memory.set_pinned_capped(session, row.id, False, max_pinned=0)
    assert outcome == memory.PIN_OK
    async with sessionmaker() as session:
        assert (await session.get(Memory, row.id)).pinned is False


async def test_set_pinned_capped_on_a_superseded_row_is_missing(sessionmaker):
    async with sessionmaker() as session:
        old = await _write(session, txt="живёт в Лилле")
        await memory.write_memory(
            session, kind="identity", text="живёт в Руане", source="user", supersedes_id=old.id
        )
        outcome = await memory.set_pinned_capped(session, old.id, True, max_pinned=8)
    assert outcome == memory.PIN_MISSING


# --- forget ---


async def test_forget_deletes_and_writes_a_content_free_audit_row(sessionmaker):
    async with sessionmaker() as session:
        row = await _write(session, txt="пользователь живёт в Лилле")
        outcome = await memory.forget(session, row.id, source="web")
    assert outcome == memory.FORGET_OK

    async with sessionmaker() as session:
        assert await session.get(Memory, row.id) is None
        changes = (
            (await session.execute(select(StateChange).where(StateChange.field == "memory")))
            .scalars()
            .all()
        )
    assert len(changes) == 1
    audit = changes[0]
    assert audit.old_value == str(row.id)
    assert audit.new_value is None
    assert audit.source == "web"
    for value in (audit.field, audit.old_value, audit.new_value):
        assert value is None or "Лилл" not in value


async def test_forget_a_missing_id_returns_false_and_writes_no_audit(sessionmaker):
    async with sessionmaker() as session:
        outcome = await memory.forget(session, 999999, source="web")
    assert outcome == memory.FORGET_MISSING
    async with sessionmaker() as session:
        changes = (await session.execute(select(StateChange))).scalars().all()
    assert changes == []


async def test_forget_repairs_supersede_pointers_like_hard_delete(sessionmaker):
    """forget() is hard_delete plus the audit row; the chain-relinking
    behavior is hard_delete's own (see tests/test_memory.py), asserted
    here only to confirm forget() doesn't bypass it."""
    async with sessionmaker() as session:
        a = await _write(session, txt="a")
        b = await memory.write_memory(
            session, kind="identity", text="b", source="user", supersedes_id=a.id
        )
        c = await memory.write_memory(
            session, kind="identity", text="c", source="user", supersedes_id=b.id
        )
        outcome = await memory.forget(session, b.id, source="web")
    assert outcome == memory.FORGET_OK
    async with sessionmaker() as session:
        refreshed_a = await session.get(Memory, a.id)
    assert refreshed_a.superseded_by == c.id


# --- forget vs an adopted StudyCard (W3 finding) ---


async def test_forget_an_adopted_technique_is_protected_not_a_500(sessionmaker):
    """Seed a StudyCard exactly the way app/core/cards.py's `adopt` does
    (status='adopted', memory_id=M pointing at a fresh, chain-head
    technique memory) and forget M. Before the fix this raised an
    unhandled IntegrityError (study_card.memory_id is a bare FK with no
    ON DELETE, and ck_study_card_adopted_has_memory forbids NULL) --
    forget() must refuse instead, leaving both rows exactly as they
    were."""
    async with sessionmaker() as session:
        card_memory = await _write(session, kind="technique", txt="дыши перед сном", source="adopt")
        card = await _adopted_card(session, card_memory.id)
        card_id, memory_id = card.id, card_memory.id

        outcome = await memory.forget(session, memory_id, source="web")
    assert outcome == memory.FORGET_PROTECTED

    async with sessionmaker() as session:
        assert await session.get(Memory, memory_id) is not None
        refreshed_card = await session.get(StudyCard, card_id)
    assert refreshed_card.memory_id == memory_id
    async with sessionmaker() as session:
        changes = (await session.execute(select(StateChange))).scalars().all()
    assert changes == []  # refused before the audit row would be written


async def test_forget_relinks_a_study_card_when_a_successor_exists(sessionmaker):
    """The other half of the same finding: editing (superseding) an
    adopted technique and then forgetting the *old* row must not crash
    either -- hard_delete relinks study_card.memory_id to the successor
    the same way it relinks a predecessor's superseded_by."""
    async with sessionmaker() as session:
        card_memory = await _write(session, kind="technique", txt="дыши перед сном", source="adopt")
        card = await _adopted_card(session, card_memory.id)
        card_id, old_id = card.id, card_memory.id

        new = await memory.write_memory(
            session, kind="technique", text="дыши глубже перед сном", source="user",
            supersedes_id=old_id,
        )
        outcome = await memory.forget(session, old_id, source="web")
    assert outcome == memory.FORGET_OK

    async with sessionmaker() as session:
        assert await session.get(Memory, old_id) is None
        refreshed_card = await session.get(StudyCard, card_id)
    assert refreshed_card.memory_id == new.id


# --- has_predecessors ---


async def test_has_predecessors_true_only_for_chain_heads_with_a_predecessor(sessionmaker):
    async with sessionmaker() as session:
        old = await _write(session, txt="живёт в Лилле")
        new = await memory.write_memory(
            session, kind="identity", text="живёт в Руане", source="user", supersedes_id=old.id
        )
        lone = await _write(session, txt="не связан ни с чем")
        result = await memory.has_predecessors(session, [new.id, lone.id, old.id])
    assert result == {new.id}


# --- list_active filters ---


async def test_list_active_default_signature_is_unchanged(sessionmaker):
    async with sessionmaker() as session:
        await _write(session, txt="один")
        await _write(session, txt="два")
        rows, total = await memory.list_active(session, offset=0, limit=10)
    assert total == 2
    assert [r.text for r in rows] == ["один", "два"]


async def test_list_active_filters_by_kind(sessionmaker):
    async with sessionmaker() as session:
        await _write(session, kind="identity", txt="кто я")
        await _write(session, kind="rule", txt="правило")
        rows, total = await memory.list_active(session, offset=0, limit=10, kind="rule")
    assert total == 1
    assert [r.text for r in rows] == ["правило"]


async def test_list_active_filters_by_pinned(sessionmaker):
    async with sessionmaker() as session:
        await _write(session, txt="закреплено", pinned=True)
        await _write(session, txt="не закреплено", pinned=False)
        rows, total = await memory.list_active(session, offset=0, limit=10, pinned=True)
    assert total == 1
    assert [r.text for r in rows] == ["закреплено"]


async def test_list_active_kind_and_pinned_combine(sessionmaker):
    async with sessionmaker() as session:
        await _write(session, kind="rule", txt="правило закреплено", pinned=True)
        await _write(session, kind="rule", txt="правило не закреплено", pinned=False)
        await _write(session, kind="event", txt="событие закреплено", pinned=True)
        rows, total = await memory.list_active(
            session, offset=0, limit=10, kind="rule", pinned=True
        )
    assert total == 1
    assert [r.text for r in rows] == ["правило закреплено"]


async def test_list_active_order_web_is_pinned_then_newest_first(sessionmaker):
    """W3 finding: the web panel's list used to inherit `list_active`'s
    oldest-first default, so a same-second add/edit refetch (the
    invalidate every write triggers) could put the row the user just
    touched off the end of the page instead of at the top the client
    prepends new/edited rows to."""
    async with sessionmaker() as session:
        a = await _write(session, txt="первый")
        b = await _write(session, txt="второй", pinned=True)
        c = await _write(session, txt="третий")
        rows, total = await memory.list_active(session, offset=0, limit=10, order="web")
    assert total == 3
    assert [r.id for r in rows] == [b.id, c.id, a.id]


async def test_list_active_order_rejects_an_unknown_value(sessionmaker):
    async with sessionmaker() as session:
        with pytest.raises(ValueError):
            await memory.list_active(session, offset=0, limit=10, order="nonsense")


async def test_list_active_excludes_superseded_rows_under_a_filter(sessionmaker):
    async with sessionmaker() as session:
        old = await _write(session, kind="identity", txt="живёт в Лилле")
        await memory.write_memory(
            session, kind="identity", text="живёт в Руане", source="user", supersedes_id=old.id
        )
        rows, total = await memory.list_active(session, offset=0, limit=10, kind="identity")
    assert total == 1
    assert [r.text for r in rows] == ["живёт в Руане"]
