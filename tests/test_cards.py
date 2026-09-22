"""app/core/cards.py: pending_page, get_card, adopt, reject (phase-4 plan sections 4, 9, 12).

Router-level command/callback tests live in tests/test_research_commands.py;
this file covers the domain layer in depth, the same split
tests/test_memory.py and tests/test_memory_commands.py use.
"""

from __future__ import annotations

import ast
import datetime
import inspect

import pytest
from sqlalchemy import select

from app.core import cards
from app.core.clock import FrozenClock
from app.db.models import (
    Memory,
    Outbound,
    Proposal,
    StateChange,
    StudyCard,
    StudyClip,
    StudyJob,
    UserState,
)

pytestmark = pytest.mark.asyncio

LOCAL_DATE = datetime.date(2026, 9, 22)


async def _job_and_clip(session, *, url: str = "https://example.com/sleep") -> tuple[StudyJob, StudyClip]:
    job = StudyJob(kind="read", local_date=LOCAL_DATE, status="done")
    session.add(job)
    await session.flush()
    clip = StudyClip(job_id=job.id, url=url, domain="example.com", text="страница о сне")
    session.add(clip)
    await session.flush()
    return job, clip


async def _add_card(session, job, clip, **overrides) -> StudyCard:
    fields = dict(
        job_id=job.id,
        clip_id=clip.id,
        kind="technique",
        text="Ложиться спать в одно и то же время.",
        quote="Ложитесь спать в одно и то же время каждый день, даже по выходным.",
        source_url=clip.url,
        risk_model="low",
        risk_rules="low",
        risk_final="low",
        status="pending",
    )
    fields.update(overrides)
    card = StudyCard(**fields)
    session.add(card)
    await session.commit()
    await session.refresh(card)
    return card


async def _hidden_card(session, job, clip, **overrides) -> StudyCard:
    fields = dict(risk_model="high", risk_rules="low", risk_final="high", status="hidden")
    fields.update(overrides)
    return await _add_card(session, job, clip, **fields)


def _clock() -> FrozenClock:
    return FrozenClock(datetime.datetime(2026, 9, 22, 12, 0, tzinfo=datetime.timezone.utc))


# --- isolation: the one-way valve (plan section 12) ------------------------


async def test_cards_module_has_no_name_that_writes_state_or_outbound():
    """Structural, not behavioural -- matches tests/test_extract.py's own
    check on app/core/extract.py and tests/test_research_isolation.py's
    on app/research/. A future edit could reintroduce the import without
    any behaviour test noticing until it was actually called."""
    tree = ast.parse(inspect.getsource(cards))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.body and isinstance(node.body[0], ast.Expr) and isinstance(
                node.body[0].value, ast.Constant
            ):
                node.body = node.body[1:] or [ast.Pass()]
    code = ast.unparse(tree)

    assert "update_state" not in code
    assert "cancel_outbound" not in code
    assert "load_state_summary" not in code
    # app.core.state itself is not forbidden -- record_change() is the
    # one sanctioned write this module makes (the assertion below pins
    # that it is record_change, not update_state, that gets imported).
    for node in ast.walk(ast.parse(inspect.getsource(cards))):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module not in (
                "app.core.outbound",
                "app.core.outbound_gate",
                "app.core.outbound_send",
                "app.core.prompt",
                "app.core.turn",
            ), f"cards.py imports {node.module}, the very thing plan section 12 forbids"
    assert "from app.core.state import record_change" in inspect.getsource(cards), (
        "record_change is the one state_change writer this module is allowed"
    )


# --- pending_page ------------------------------------------------------


async def test_pending_page_never_returns_a_hidden_card_even_when_newest(sessionmaker):
    async with sessionmaker() as session:
        job, clip = await _job_and_clip(session)
        await _add_card(session, job, clip, text="Видимая карточка.")
        await _hidden_card(session, job, clip, text="Скрытая карточка.")

        rows, total = await cards.pending_page(session, page=0)

    assert total == 1
    assert [row.text for row in rows] == ["Видимая карточка."]


async def test_pending_page_is_newest_first(sessionmaker):
    async with sessionmaker() as session:
        job, clip = await _job_and_clip(session)
        for i in range(3):
            await _add_card(session, job, clip, text=f"Карточка {i}.")

        rows, total = await cards.pending_page(session, page=0)

    assert total == 3
    assert [row.text for row in rows] == ["Карточка 2.", "Карточка 1.", "Карточка 0."]


async def test_pending_page_pages(sessionmaker):
    async with sessionmaker() as session:
        job, clip = await _job_and_clip(session)
        for i in range(7):
            await _add_card(session, job, clip, text=f"Карточка {i}.")

        first, total = await cards.pending_page(session, page=0)
        second, total_again = await cards.pending_page(session, page=1)

    assert total == total_again == 7
    assert len(first) == cards.PAGE_SIZE
    assert len(second) == 2
    assert {row.id for row in first} & {row.id for row in second} == set()


async def test_pending_page_excludes_adopted_and_rejected(sessionmaker):
    async with sessionmaker() as session:
        job, clip = await _job_and_clip(session)
        clock = _clock()
        pending = await _add_card(session, job, clip, text="Ещё в очереди.")
        adopted = await _add_card(session, job, clip, text="Уже приняли.")
        rejected = await _add_card(session, job, clip, text="Уже отклонили.")
        await cards.adopt(session, adopted.id, clock=clock)
        await cards.reject(session, rejected.id, clock=clock)

        rows, total = await cards.pending_page(session, page=0)

    assert total == 1
    assert rows[0].id == pending.id


# --- get_card ------------------------------------------------------------


async def test_get_card_returns_any_status_except_hidden(sessionmaker):
    async with sessionmaker() as session:
        job, clip = await _job_and_clip(session)
        clock = _clock()
        pending = await _add_card(session, job, clip, text="В ожидании.")
        adopted = await _add_card(session, job, clip, text="Принятая.")
        await cards.adopt(session, adopted.id, clock=clock)
        hidden = await _hidden_card(session, job, clip, text="Спрятанная.")

        assert (await cards.get_card(session, pending.id)).id == pending.id
        assert (await cards.get_card(session, adopted.id)).status == "adopted"
        assert await cards.get_card(session, hidden.id) is None


async def test_get_card_on_a_missing_id_is_none(sessionmaker):
    async with sessionmaker() as session:
        assert await cards.get_card(session, 999999) is None


# --- adopt -----------------------------------------------------------------


async def test_adopt_writes_one_technique_memory_and_links_it(sessionmaker):
    async with sessionmaker() as session:
        job, clip = await _job_and_clip(session)
        card = await _add_card(session, job, clip)
        clock = _clock()

        outcome = await cards.adopt(session, card.id, clock=clock)

        assert outcome == cards.ADOPTED
        await session.refresh(card)
        assert card.status == "adopted"
        assert card.decided_at == clock.now_utc()
        assert card.memory_id is not None

        memories = (await session.execute(select(Memory))).scalars().all()
        assert len(memories) == 1
        assert memories[0].id == card.memory_id
        assert memories[0].kind == "technique"
        assert memories[0].source == "adopt"
        assert memories[0].text == card.text


async def test_adopt_writes_nothing_but_memory_and_state_change(sessionmaker):
    """Plan section 12: 'Adopting writes only memory(kind=technique).
    Nothing in research writes persona.md, user_state, rules,
    commitments, or outbound.'"""
    async with sessionmaker() as session:
        job, clip = await _job_and_clip(session)
        card = await _add_card(session, job, clip)
        session.add(UserState(id=1, chat_id=1))
        await session.commit()
        before = (await session.execute(select(UserState))).scalar_one()
        before_updated_at = before.updated_at

        await cards.adopt(session, card.id, clock=_clock())

        after = (await session.execute(select(UserState))).scalar_one()
        assert after.updated_at == before_updated_at, "adopt must not touch user_state"

        assert (await session.execute(select(Outbound))).scalars().all() == []
        assert (await session.execute(select(Proposal))).scalars().all() == []

        changes = (await session.execute(select(StateChange))).scalars().all()
        assert len(changes) == 1
        assert changes[0].field == "study_card"
        assert changes[0].source == "command"
        # No card content in the audit row -- same rule as /forget's.
        for value in (changes[0].old_value, changes[0].new_value):
            assert value is None or "спать" not in value.lower()


async def test_adopt_is_idempotent_and_writes_exactly_one_memory(sessionmaker):
    async with sessionmaker() as session:
        job, clip = await _job_and_clip(session)
        card = await _add_card(session, job, clip)
        clock = _clock()

        first = await cards.adopt(session, card.id, clock=clock)
        second = await cards.adopt(session, card.id, clock=clock)

        assert first == cards.ADOPTED
        assert second == cards.ALREADY

        memories = (await session.execute(select(Memory))).scalars().all()
        assert len(memories) == 1
        changes = (await session.execute(select(StateChange))).scalars().all()
        assert len(changes) == 1, "the second call must write no audit row either"


async def test_adopting_a_near_duplicate_technique_links_the_existing_memory(sessionmaker):
    """write_memory() dedupes (app/core/memory.py, DEDUPE_MAX_SIMILARITY).
    A card whose text is a near-duplicate of an already-active memory
    must still end up adopted, pointing at that existing row -- an
    adopted card with memory_id=None would violate
    ck_study_card_adopted_has_memory, and a second near-identical memory
    row would be exactly the duplicate write_memory() exists to refuse."""
    async with sessionmaker() as session:
        job, clip = await _job_and_clip(session)
        existing = Memory(
            kind="technique",
            text="Ложиться спать в одно и то же время каждый вечер.",
            source="adopt",
        )
        session.add(existing)
        await session.commit()
        await session.refresh(existing)

        card = await _add_card(
            session, job, clip, text="Ложиться спать в одно и то же время каждый вечер."
        )

        outcome = await cards.adopt(session, card.id, clock=_clock())

        assert outcome == cards.ADOPTED
        await session.refresh(card)
        assert card.memory_id == existing.id

        memories = (await session.execute(select(Memory))).scalars().all()
        assert len(memories) == 1, "no second copy of the same technique"


async def test_a_replayed_adopt_after_a_crash_links_the_same_memory(sessionmaker):
    """Simulates the worker re-running a job after a crash between
    write_memory's commit and the card being marked adopted: the second
    run must land on the *same* memory row, not a duplicate."""
    async with sessionmaker() as session:
        job, clip = await _job_and_clip(session)
        card = await _add_card(session, job, clip)
        clock = _clock()

        from app.core.memory import write_memory

        written = await write_memory(session, kind="technique", text=card.text, source="adopt")
        # Simulate the crash: the memory exists, but the card was never
        # updated to point at it.

        outcome = await cards.adopt(session, card.id, clock=clock)

        assert outcome == cards.ADOPTED
        await session.refresh(card)
        assert card.memory_id == written.id
        memories = (await session.execute(select(Memory))).scalars().all()
        assert len(memories) == 1


async def test_adopt_on_a_hidden_card_is_forbidden(sessionmaker):
    async with sessionmaker() as session:
        job, clip = await _job_and_clip(session)
        card = await _hidden_card(session, job, clip)

        outcome = await cards.adopt(session, card.id, clock=_clock())

        assert outcome == cards.FORBIDDEN
        await session.refresh(card)
        assert card.status == "hidden"
        assert card.memory_id is None
        assert (await session.execute(select(Memory))).scalars().all() == []


async def test_adopt_on_a_missing_card_is_gone(sessionmaker):
    async with sessionmaker() as session:
        outcome = await cards.adopt(session, 999999, clock=_clock())
    assert outcome == cards.GONE


@pytest.mark.parametrize("status", ["rejected", "expired"])
async def test_adopt_on_a_rejected_or_expired_card_is_gone(sessionmaker, status):
    async with sessionmaker() as session:
        job, clip = await _job_and_clip(session)
        clock = _clock()
        card = await _add_card(session, job, clip, status=status, decided_at=clock.now_utc())

        outcome = await cards.adopt(session, card.id, clock=clock)

    assert outcome == cards.GONE


# --- reject ------------------------------------------------------------


async def test_reject_sets_status_and_decided_at(sessionmaker):
    async with sessionmaker() as session:
        job, clip = await _job_and_clip(session)
        card = await _add_card(session, job, clip)
        clock = _clock()

        outcome = await cards.reject(session, card.id, clock=clock)

        assert outcome == cards.REJECTED
        await session.refresh(card)
        assert card.status == "rejected"
        assert card.decided_at == clock.now_utc()
        assert card.memory_id is None
        assert (await session.execute(select(Memory))).scalars().all() == []


async def test_reject_is_idempotent(sessionmaker):
    async with sessionmaker() as session:
        job, clip = await _job_and_clip(session)
        card = await _add_card(session, job, clip)
        clock = _clock()

        first = await cards.reject(session, card.id, clock=clock)
        second = await cards.reject(session, card.id, clock=clock)

        assert first == cards.REJECTED
        assert second == cards.ALREADY
        changes = (await session.execute(select(StateChange))).scalars().all()
        assert len(changes) == 1


async def test_reject_on_a_hidden_card_is_forbidden(sessionmaker):
    async with sessionmaker() as session:
        job, clip = await _job_and_clip(session)
        card = await _hidden_card(session, job, clip)

        outcome = await cards.reject(session, card.id, clock=_clock())

        assert outcome == cards.FORBIDDEN
        await session.refresh(card)
        assert card.status == "hidden"


async def test_reject_on_an_adopted_card_is_gone(sessionmaker):
    """A decision already made the other way cannot be reversed by reject()."""
    async with sessionmaker() as session:
        job, clip = await _job_and_clip(session)
        card = await _add_card(session, job, clip)
        clock = _clock()
        await cards.adopt(session, card.id, clock=clock)

        outcome = await cards.reject(session, card.id, clock=clock)

        assert outcome == cards.GONE
        await session.refresh(card)
        assert card.status == "adopted"


async def test_reject_on_a_missing_card_is_gone(sessionmaker):
    async with sessionmaker() as session:
        outcome = await cards.reject(session, 999999, clock=_clock())
    assert outcome == cards.GONE
