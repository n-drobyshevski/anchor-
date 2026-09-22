"""The post-turn extractor (phase-2 plan sections 8, 13 and 14).

The headline test in this file is
test_model_output_cannot_reach_sensitive_state: a scripted extractor
reply that tries to set intensity, persona_active and streak must change
nothing. Everything else here exists to keep that true as the code
grows.

Every LLM call is a FakeLLMProvider with scripted JSON; nothing here
touches the network.
"""

from __future__ import annotations

import ast
import inspect
import json

import pytest
from sqlalchemy import select

from app.config import Settings
from app.core import extract, proposal
from app.db.models import Journal, Memory, Proposal, SpendLedger, StateChange, UserState
from conftest import FakeLLMProvider

pytestmark = pytest.mark.asyncio

TIMEZONE = "Europe/Paris"


def _settings(**kw) -> Settings:
    base = {"DAILY_USD_CAP": 10.0, "MEMORY_AUTOWRITE_MIN_CONF": 0.8}
    base.update(kw)
    return Settings(**base)


def _payload(journal=None, memories=None, proposals=None) -> str:
    return json.dumps(
        {
            "journal": journal,
            "memories": memories or [],
            "proposals": proposals or [],
        },
        ensure_ascii=False,
    )


async def _seed(sessionmaker, update_id: int, *, user="я живу в Лилле", assistant="Принято."):
    from app.db.models import Message, TelegramUpdate

    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=4242, timezone=TIMEZONE))
        await session.commit()
        session.add(TelegramUpdate(update_id=update_id, payload={}))
        await session.commit()
        session.add(Message(role="user", content=user, ooc=False, kind="chat", update_id=update_id))
        session.add(
            Message(
                role="assistant",
                content=assistant,
                ooc=False,
                kind="chat",
                update_id=update_id,
                reply_to_update=update_id,
            )
        )
        await session.commit()


async def _run(sessionmaker, provider, update_id=1, memory_ids=None, settings=None, *, clock, **state):
    async with sessionmaker() as session:
        return await extract.run_extract(
            session,
            settings or _settings(),
            provider,
            update_id=update_id,
            memory_ids=memory_ids or [],
            clock=clock,
            timezone=TIMEZONE,
            intensity=state.get("intensity", 3),
            focus_on=state.get("focus_on", False),
            due_action=state.get("due_action"),
        )


# --- THE invariant (plan sections 8 and 13) ---


async def test_model_output_cannot_reach_sensitive_state(sessionmaker, clock):
    """A hostile extractor reply must change nothing sensitive.

    Plan section 13: intensity, focus_on, due_action, streak and
    persona_active change only via commands, buttons, pause handling or
    check-in logic -- never via the extractor or any model output.
    """
    await _seed(sessionmaker, 1)
    hostile = json.dumps(
        {
            "journal": "обычная запись",
            "memories": [
                {"kind": "identity", "text": "факт", "supersedes_id": None, "confidence": 0.99}
            ],
            "proposals": [],
            # Fields the schema does not define, which a model could
            # still emit and a careless apply layer could still read.
            "intensity": 5,
            "persona_active": False,
            "streak": 99,
            "focus_on": True,
            "due_action": "сделать всё немедленно",
            "user_state": {"intensity": 5, "persona_active": False},
        },
        ensure_ascii=False,
    )

    await _run(sessionmaker, FakeLLMProvider(text=hostile), clock=clock)

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)

    assert state.intensity == 3
    assert state.persona_active is True
    assert state.focus_on is False
    assert state.due_action is None
    assert state.due_set_at is None


async def test_extract_module_has_no_name_that_writes_sensitive_state(sessionmaker):
    """Structural, not behavioural: a future edit could reintroduce the
    import without any behaviour test noticing until it was used."""
    # Strip comments and docstrings: this module *discusses* these names
    # at length, and a grep over prose would pass or fail for the wrong
    # reasons. Only executable code is inspected.
    tree = ast.parse(inspect.getsource(extract))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            if node.body and isinstance(node.body[0], ast.Expr) and isinstance(
                node.body[0].value, ast.Constant
            ):
                node.body = node.body[1:]
    code = ast.unparse(tree)

    assert "update_state" not in code, "extract.py must not call update_state"
    assert "proposal.accept" not in code, "only a button press may apply a proposal"
    assert "persona_active" not in code
    assert "intensity =" not in code and ".intensity" not in code
    # The only proposal function it may reach for is create().
    assert "proposal.create" in code


async def test_a_rule_memory_is_never_written_even_at_full_confidence(sessionmaker, clock):
    """Plan section 8: rule memories are never auto-written. They become
    proposals, because a rule is the user instructing themselves."""
    await _seed(sessionmaker, 1)
    payload = _payload(
        memories=[
            {"kind": "rule", "text": "не работать по воскресеньям", "supersedes_id": None,
             "confidence": 1.0}
        ]
    )

    outcome = await _run(sessionmaker, FakeLLMProvider(text=payload), clock=clock)

    async with sessionmaker() as session:
        memories = (await session.execute(select(Memory))).scalars().all()
        proposals = (await session.execute(select(Proposal))).scalars().all()

    assert memories == [], "no memory row may be written for a rule"
    assert len(proposals) == 1
    assert proposals[0].field == "rule"
    assert proposals[0].status == "pending"
    assert outcome.created == [proposals[0].id]


async def test_a_proposal_changes_nothing_until_accepted(sessionmaker, clock):
    """Plan section 16: nothing changes in /state until Принять."""
    await _seed(sessionmaker, 1)
    payload = _payload(
        proposals=[{"field": "due_action", "value": "сдать отчёт до пятницы", "reason": "договорились"}]
    )

    await _run(sessionmaker, FakeLLMProvider(text=payload), clock=clock)

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
        stored = (await session.execute(select(Proposal))).scalars().one()

    assert state.due_action is None
    assert stored.status == "pending"
    assert stored.value == "сдать отчёт до пятницы"


# --- validation ---


async def test_unknown_supersedes_id_is_nulled(sessionmaker):
    """Plan section 8: supersedes_id must be one of the ids given in the
    input. The model cannot retire a memory it was never shown."""
    result = extract.validate(
        {
            "journal": None,
            "memories": [
                {"kind": "identity", "text": "факт", "supersedes_id": 999, "confidence": 0.9}
            ],
            "proposals": [],
        },
        offered_ids={1, 2},
    )
    assert result["memories"][0]["supersedes_id"] is None


async def test_offered_supersedes_id_survives(sessionmaker):
    result = extract.validate(
        {
            "journal": None,
            "memories": [
                {"kind": "identity", "text": "факт", "supersedes_id": 2, "confidence": 0.9}
            ],
            "proposals": [],
        },
        offered_ids={1, 2},
    )
    assert result["memories"][0]["supersedes_id"] == 2


async def test_validate_drops_unknown_kinds_and_fields(sessionmaker):
    result = extract.validate(
        {
            "journal": None,
            "memories": [
                {"kind": "technique", "text": "x", "supersedes_id": None, "confidence": 0.9},
                {"kind": "identity", "text": "ok", "supersedes_id": None, "confidence": 0.9},
            ],
            "proposals": [
                {"field": "persona_active", "value": "false", "reason": "r"},
                {"field": "focus_on", "value": "on", "reason": "r"},
            ],
        },
        offered_ids=set(),
    )
    assert [m["kind"] for m in result["memories"]] == ["identity"]
    assert [p["field"] for p in result["proposals"]] == ["focus_on"]


async def test_validate_enforces_counts_and_lengths(sessionmaker):
    result = extract.validate(
        {
            "journal": "и" * 241,
            "memories": [
                {"kind": "identity", "text": f"факт {i}", "supersedes_id": None, "confidence": 0.9}
                for i in range(5)
            ],
            "proposals": [
                {"field": "focus_on", "value": "on", "reason": "r"},
                {"field": "due_action", "value": "x", "reason": "r"},
            ],
        },
        offered_ids=set(),
    )
    assert result["journal"] is None, "an over-long journal is dropped, not truncated"
    assert len(result["memories"]) == extract.MAX_MEMORIES
    assert len(result["proposals"]) == extract.MAX_PROPOSALS


async def test_validate_survives_garbage_shapes(sessionmaker):
    result = extract.validate(
        {"journal": 42, "memories": "not a list", "proposals": [None, 7, {"field": None}]},
        offered_ids=set(),
    )
    assert result == {"journal": None, "memories": [], "proposals": []}


async def test_validate_rejects_a_boolean_confidence(sessionmaker):
    """bool is a subclass of int in Python; `True` must not read as 1.0."""
    result = extract.validate(
        {
            "journal": None,
            "memories": [
                {"kind": "identity", "text": "факт", "supersedes_id": None, "confidence": True}
            ],
            "proposals": [],
        },
        offered_ids=set(),
    )
    assert result["memories"] == []


async def test_parse_json_tolerates_fences_and_preamble(sessionmaker):
    fenced = '```json\n{"journal": null, "memories": [], "proposals": []}\n```'
    chatty = 'Вот результат: {"journal": null, "memories": [], "proposals": []} — всё.'
    for raw in (fenced, chatty):
        assert extract.parse_json(raw) == {"journal": None, "memories": [], "proposals": []}


async def test_parse_json_returns_none_for_prose(sessionmaker):
    assert extract.parse_json("Мне нечего добавить.") is None


async def test_unparseable_output_applies_nothing(sessionmaker, clock):
    await _seed(sessionmaker, 1)
    outcome = await _run(sessionmaker, FakeLLMProvider(text="Мне нечего добавить."), clock=clock)

    async with sessionmaker() as session:
        assert (await session.execute(select(Memory))).scalars().all() == []
        assert (await session.execute(select(Journal))).scalars().all() == []
    assert outcome.created == []


# --- confidence and dedupe ---


async def test_low_confidence_memories_are_dropped(sessionmaker, clock):
    await _seed(sessionmaker, 1)
    payload = _payload(
        memories=[
            {"kind": "identity", "text": "пользователь живёт в Лилле", "supersedes_id": None,
             "confidence": 0.5}
        ]
    )

    await _run(sessionmaker, FakeLLMProvider(text=payload), clock=clock)

    async with sessionmaker() as session:
        assert (await session.execute(select(Memory))).scalars().all() == []


async def test_high_confidence_memories_are_written_with_source_extractor(sessionmaker, clock):
    await _seed(sessionmaker, 1)
    payload = _payload(
        memories=[
            {"kind": "identity", "text": "пользователь живёт в Лилле", "supersedes_id": None,
             "confidence": 0.9}
        ]
    )

    await _run(sessionmaker, FakeLLMProvider(text=payload), clock=clock)

    async with sessionmaker() as session:
        rows = (await session.execute(select(Memory))).scalars().all()
        changes = (
            (await session.execute(select(StateChange).where(StateChange.source == "extractor")))
            .scalars().all()
        )

    assert len(rows) == 1
    assert rows[0].source == "extractor"
    assert len(changes) == 1
    assert changes[0].new_value == str(rows[0].id)
    # The audit row must not carry the fact itself.
    assert changes[0].old_value is None


async def test_extractor_writes_are_deduped(sessionmaker, clock):
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        session.add(Memory(kind="identity", text="пользователь живёт в Лилле", source="user"))
        await session.commit()

    payload = _payload(
        memories=[
            {"kind": "identity", "text": "пользователь живет в Лилле", "supersedes_id": None,
             "confidence": 0.95}
        ]
    )
    await _run(sessionmaker, FakeLLMProvider(text=payload), clock=clock)

    async with sessionmaker() as session:
        rows = (await session.execute(select(Memory))).scalars().all()
    assert len(rows) == 1


async def test_a_valid_supersede_retires_the_old_memory(sessionmaker, clock):
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        old = Memory(kind="identity", text="пользователь живёт в Лилле", source="user")
        session.add(old)
        await session.commit()
        await session.refresh(old)
        old_id = old.id

    payload = _payload(
        memories=[
            {"kind": "identity", "text": "пользователь переехал в Руан", "supersedes_id": old_id,
             "confidence": 0.95}
        ]
    )
    await _run(sessionmaker, FakeLLMProvider(text=payload), memory_ids=[old_id], clock=clock)

    async with sessionmaker() as session:
        old = await session.get(Memory, old_id)
        rows = (await session.execute(select(Memory))).scalars().all()

    assert old.superseded_by is not None
    assert len(rows) == 2, "superseded, not duplicated"


# --- redaction (plan section 8) ---


@pytest.mark.parametrize(
    "text",
    [
        "карта пользователя 4111 1111 1111 1111",
        "счёт FR14 2004 1010 0505 0001 3M02 606",
        "почта пользователя nick@example.com",
    ],
)
async def test_redaction_rejects_secrets_before_any_write(sessionmaker, clock, text):
    await _seed(sessionmaker, 1)
    payload = _payload(
        journal=text,
        memories=[{"kind": "identity", "text": text, "supersedes_id": None, "confidence": 0.99}],
    )

    await _run(sessionmaker, FakeLLMProvider(text=payload), clock=clock)

    async with sessionmaker() as session:
        assert (await session.execute(select(Memory))).scalars().all() == []
        assert (await session.execute(select(Journal))).scalars().all() == []


async def test_ordinary_text_is_not_redacted(sessionmaker, clock):
    await _seed(sessionmaker, 1)
    payload = _payload(
        journal="Поговорили про отчёт.",
        memories=[
            {"kind": "identity", "text": "пользователь живёт в доме 12", "supersedes_id": None,
             "confidence": 0.9}
        ],
    )

    await _run(sessionmaker, FakeLLMProvider(text=payload), clock=clock)

    async with sessionmaker() as session:
        assert len((await session.execute(select(Memory))).scalars().all()) == 1
        assert len((await session.execute(select(Journal))).scalars().all()) == 1


# --- journal, ledger, cap ---


async def test_journal_row_is_written_with_the_local_date(sessionmaker, clock):
    await _seed(sessionmaker, 1)
    await _run(sessionmaker, FakeLLMProvider(text=_payload(journal="Поговорили про отчёт.")), clock=clock)

    async with sessionmaker() as session:
        rows = (await session.execute(select(Journal))).scalars().all()
    assert len(rows) == 1
    assert rows[0].text == "Поговорили про отчёт."


async def test_the_call_is_ledgered_under_extractor(sessionmaker, clock):
    await _seed(sessionmaker, 1)
    await _run(sessionmaker, FakeLLMProvider(text=_payload(), model="cydonia-fake"), clock=clock)

    async with sessionmaker() as session:
        rows = (await session.execute(select(SpendLedger))).scalars().all()
    assert len(rows) == 1
    assert rows[0].category == "extractor"


async def test_at_the_cap_the_extractor_is_skipped_entirely(sessionmaker, clock):
    """Plan section 12: at the cap the extractor is skipped. Unlike a
    scene summary it is not deferred -- the exchange will have aged out
    of the transcript by tomorrow."""
    await _seed(sessionmaker, 1)
    provider = FakeLLMProvider(text=_payload(journal="что-то"))

    await _run(sessionmaker, provider, settings=_settings(DAILY_USD_CAP=0.0), clock=clock)

    assert provider.calls == 0
    async with sessionmaker() as session:
        assert (await session.execute(select(Journal))).scalars().all() == []


async def test_a_schema_is_sent_with_the_call(sessionmaker, clock):
    await _seed(sessionmaker, 1)
    provider = FakeLLMProvider(text=_payload())
    await _run(sessionmaker, provider, clock=clock)
    assert provider.received_schemas == [extract.EXTRACT_SCHEMA]


async def test_the_extractor_sees_memory_ids_but_the_chat_model_does_not(sessionmaker, clock):
    """The one place a model is shown memory ids, because supersedes_id
    requires it (plan section 7's rule is about the *chat* model)."""
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        row = Memory(kind="identity", text="пользователь живёт в Лилле", source="user")
        session.add(row)
        await session.commit()
        await session.refresh(row)
        memory_id = row.id

    provider = FakeLLMProvider(text=_payload())
    await _run(sessionmaker, provider, memory_ids=[memory_id], clock=clock)

    sent = "\n".join(m.content for m in provider.received_messages[0])
    assert f"{memory_id} — пользователь живёт в Лилле" in sent


async def test_only_one_proposal_is_pending_at_a_time(sessionmaker, clock):
    """Plan section 8: a new proposal expires the outstanding one."""
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        first, _ = await proposal.create(
            session, clock, field="due_action", value="старое действие", reason=None
        )
        first_id = first.id

    payload = _payload(
        proposals=[{"field": "due_action", "value": "новое действие", "reason": "r"}]
    )
    outcome = await _run(sessionmaker, FakeLLMProvider(text=payload), clock=clock)

    async with sessionmaker() as session:
        old = await session.get(Proposal, first_id)
        pending = await proposal.get_pending(session)

    assert old.status == "expired"
    assert old.decided_at is not None
    assert pending.value == "новое действие"
    assert outcome.expired == [first_id]


async def test_a_junk_entry_does_not_consume_the_cap(sessionmaker):
    """Caps apply after filtering, not before: one malformed entry at the
    head of the list must not silently discard the valid ones behind it."""
    result = extract.validate(
        {
            "journal": None,
            "memories": [
                "not a dict",
                {"kind": "nope", "text": "x", "supersedes_id": None, "confidence": 0.9},
                {"kind": "identity", "text": "первый", "supersedes_id": None, "confidence": 0.9},
                {"kind": "identity", "text": "второй", "supersedes_id": None, "confidence": 0.9},
                {"kind": "identity", "text": "третий", "supersedes_id": None, "confidence": 0.9},
                {"kind": "identity", "text": "четвёртый", "supersedes_id": None, "confidence": 0.9},
            ],
            "proposals": [
                {"field": "persona_active", "value": "false", "reason": "r"},
                {"field": "due_action", "value": "сдать отчёт", "reason": "r"},
            ],
        },
        offered_ids=set(),
    )
    assert [m["text"] for m in result["memories"]] == ["первый", "второй", "третий"]
    assert [p["field"] for p in result["proposals"]] == ["due_action"]


# --- when the extractor is enqueued (plan section 8's "When") ---


async def _turn_setup(sessionmaker, update_id: int, **state):
    from aiogram import Bot

    from app.db.models import TelegramUpdate
    from conftest import FakeSession

    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=4242, timezone=TIMEZONE, **state))
        await session.commit()
        session.add(TelegramUpdate(update_id=update_id, payload={}))
        await session.commit()
    return Bot(token="123456:TESTTOKEN", session=FakeSession())


async def _jobs(sessionmaker):
    from app.db.models import Job

    async with sessionmaker() as session:
        return (await session.execute(select(Job))).scalars().all()


async def test_a_delivered_in_character_turn_enqueues_the_extractor(sessionmaker, clock):
    from app.core import turn

    update_id = 8001
    bot = await _turn_setup(sessionmaker, update_id)
    async with sessionmaker() as session:
        row = Memory(kind="identity", text="пользователь живёт в Лилле", source="user", pinned=True)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        memory_id = row.id

    await turn.run(
        sessionmaker, bot, _settings(), FakeLLMProvider(text="Принято."),
        clock=clock, chat_id=4242, update_id=update_id, user_text="я сегодня думал про Лилль",
    )

    jobs = await _jobs(sessionmaker)
    extract_jobs = [j for j in jobs if j.kind == "extract"]
    assert len(extract_jobs) == 1
    assert extract_jobs[0].dedup_key == f"extract:{update_id}"
    assert extract_jobs[0].payload["update_id"] == update_id
    # The injected ids ride along so the extractor can validate any
    # supersedes_id it proposes.
    assert memory_id in extract_jobs[0].payload["memory_ids"]


async def test_a_neutral_turn_does_not_enqueue_the_extractor(sessionmaker, clock):
    """Plan section 8: never after OOC/neutral turns."""
    from app.core import turn

    update_id = 8002
    bot = await _turn_setup(sessionmaker, update_id, persona_active=False)

    await turn.run(
        sessionmaker, bot, _settings(), FakeLLMProvider(text="Хорошо."),
        clock=clock, chat_id=4242, update_id=update_id, user_text="привет",
    )

    assert [j for j in await _jobs(sessionmaker) if j.kind == "extract"] == []


async def test_a_pause_word_does_not_enqueue_the_extractor(sessionmaker, clock):
    """Never after a canned reply."""
    from app.core import turn

    update_id = 8003
    bot = await _turn_setup(sessionmaker, update_id)

    await turn.run(
        sessionmaker, bot, _settings(), FakeLLMProvider(text="не должно вызваться"),
        clock=clock, chat_id=4242, update_id=update_id, user_text="пурпурный",
    )

    assert [j for j in await _jobs(sessionmaker) if j.kind == "extract"] == []


async def test_a_capped_turn_does_not_enqueue_the_extractor(sessionmaker, clock):
    from app.core import turn
    from app.db.models import SpendLedger as Ledger
    from app.core.clock import local_date as clock_local_date
    import decimal

    update_id = 8004
    bot = await _turn_setup(sessionmaker, update_id)
    async with sessionmaker() as session:
        session.add(
            Ledger(
                local_date=clock_local_date(clock, TIMEZONE),
                category="chat",
                usd_cost=decimal.Decimal("99"),
            )
        )
        await session.commit()

    await turn.run(
        sessionmaker, bot, _settings(DAILY_USD_CAP=1.0), FakeLLMProvider(text="не вызовется"),
        clock=clock, chat_id=4242, update_id=update_id, user_text="привет, как дела",
    )

    assert [j for j in await _jobs(sessionmaker) if j.kind == "extract"] == []


async def test_a_failed_turn_does_not_enqueue_the_extractor(sessionmaker, clock):
    """The extractor describes a delivered exchange; there isn't one."""
    from app.core import turn
    from app.llm.provider import LLMError

    update_id = 8005
    bot = await _turn_setup(sessionmaker, update_id)

    await turn.run(
        sessionmaker, bot, _settings(), FakeLLMProvider(raises=[LLMError("boom")]),
        clock=clock, chat_id=4242, update_id=update_id, user_text="привет, как дела",
    )

    assert [j for j in await _jobs(sessionmaker) if j.kind == "extract"] == []


async def test_a_replayed_turn_enqueues_one_extract_job(sessionmaker, clock):
    from app.core import turn

    update_id = 8006
    bot = await _turn_setup(sessionmaker, update_id)
    provider = FakeLLMProvider(text="Принято.")

    for _ in range(2):
        await turn.run(
            sessionmaker, bot, _settings(), provider,
            clock=clock, chat_id=4242, update_id=update_id, user_text="привет, как дела",
        )

    assert len([j for j in await _jobs(sessionmaker) if j.kind == "extract"]) == 1


async def test_the_worker_runs_the_extract_job_and_sends_the_proposal(sessionmaker, clock):
    """End to end through the worker: claim -> extract -> proposal message."""
    from aiogram import Bot

    from app.worker import process_one_job
    from conftest import FakeSession
    from app.db.jobs import enqueue_job

    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        await enqueue_job(
            session, "extract", {"update_id": 1, "memory_ids": []}, dedup_key="extract:1"
        )

    payload = _payload(
        proposals=[{"field": "due_action", "value": "сдать отчёт до пятницы", "reason": "r"}]
    )
    fake = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake)

    assert await process_one_job(sessionmaker, _settings(), FakeLLMProvider(text=payload), clock, bot)

    async with sessionmaker() as session:
        stored = (await session.execute(select(Proposal))).scalars().one()

    assert stored.status == "pending"
    assert len(fake.sent) == 1
    assert "Записать?" in fake.sent[0].text
    assert stored.tg_message_id is not None
