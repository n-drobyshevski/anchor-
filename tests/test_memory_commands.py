"""The memory commands and callbacks (phase-2 plan sections 11 and 14).

Router-level tests: they feed real Update payloads through
build_router()'s dispatcher, so the command filters, the callback
filters and the replay gates are all exercised as they actually run.
core/memory.py's behaviour is covered in depth in tests/test_memory.py.
"""

from __future__ import annotations

import datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy import select

from app.config import Settings
from app.core import memory
from app.db.models import (
    Memory,
    PendingMemory,
    StateChange,
    StudyCard,
    StudyClip,
    StudyJob,
    TelegramUpdate,
    UserState,
)
from app.tg import memory as memory_ui
from app.tg.router import build_router
from conftest import FakeLLMProvider, FakeSession

pytestmark = pytest.mark.asyncio

TEST_CHAT_ID = 555
TIMEZONE = "Europe/Paris"


def _command_update(update_id: int, text: str) -> dict:
    """A command update whose bot_command entity covers only the verb,
    exactly as a real Telegram client marks it up, so CommandObject.args
    parses out the rest."""
    command_len = len(text.split(" ", 1)[0])
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": TEST_CHAT_ID, "type": "private"},
            "from": {"id": TEST_CHAT_ID, "is_bot": False, "first_name": "Test"},
            "text": text,
            "entities": [{"type": "bot_command", "offset": 0, "length": command_len}],
        },
    }


def _callback_update(update_id: int, data: str, *, message_id: int = 900) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "from": {"id": TEST_CHAT_ID, "is_bot": False, "first_name": "Test"},
            "chat_instance": "ci",
            "data": data,
            "message": {
                "message_id": message_id,
                "date": 0,
                "chat": {"id": TEST_CHAT_ID, "type": "private"},
                "text": "…",
            },
        },
    }


def _build_dp(sessionmaker, settings: Settings) -> tuple[Dispatcher, Bot, FakeSession]:
    fake_session = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake_session)
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, settings, FakeLLMProvider()))
    return dp, bot, fake_session


async def _seed(sessionmaker, *update_ids: int) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=TEST_CHAT_ID, timezone=TIMEZONE))
        await session.commit()
        for update_id in update_ids:
            session.add(TelegramUpdate(update_id=update_id, payload={}))
        await session.commit()


async def _feed(dp, bot, payload: dict) -> None:
    await dp.feed_update(bot, Update.model_validate(payload, context={"bot": bot}))


# --- /remember ---


async def test_remember_parks_text_and_offers_kind_buttons(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings())

    await _feed(dp, bot, _command_update(1, "/remember я живу в Лилле"))

    assert len(fake.sent) == 1
    assert "я живу в Лилле" in fake.sent[0].text
    labels = [
        button.text
        for row in fake.sent[0].reply_markup.inline_keyboard
        for button in row
    ]
    assert labels == ["Обо мне", "Предпочтение", "Правило", "Событие"]

    async with sessionmaker() as session:
        pending = (await session.execute(select(PendingMemory))).scalars().all()
    assert [row.text for row in pending] == ["я живу в Лилле"]


async def test_remember_with_no_text_explains_usage(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings())

    await _feed(dp, bot, _command_update(1, "/remember"))

    assert fake.sent[0].text == memory_ui.REMEMBER_USAGE
    async with sessionmaker() as session:
        assert (await session.execute(select(PendingMemory))).scalars().all() == []


async def test_remember_refuses_text_over_the_column_limit(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings())

    await _feed(dp, bot, _command_update(1, "/remember " + "я" * 301))

    assert "301" not in fake.sent[0].text
    assert fake.sent[0].text.startswith("Слишком длинно")
    async with sessionmaker() as session:
        assert (await session.execute(select(PendingMemory))).scalars().all() == []


async def test_a_replayed_remember_parks_one_row_and_sends_one_keyboard(sessionmaker):
    """The worker re-runs an update after a crash or the stuck sweep."""
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings())

    for _ in range(2):
        await _feed(dp, bot, _command_update(1, "/remember я живу в Лилле"))

    assert len(fake.sent) == 1
    async with sessionmaker() as session:
        pending = (await session.execute(select(PendingMemory))).scalars().all()
    assert len(pending) == 1


# --- the kind callback ---


async def test_kind_button_writes_the_memory_and_edits_in_place(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build_dp(sessionmaker, Settings())

    await _feed(dp, bot, _command_update(1, "/remember я живу в Лилле"))
    async with sessionmaker() as session:
        pending_id = (await session.execute(select(PendingMemory.id))).scalar_one()

    await _feed(dp, bot, _callback_update(2, f"m:k:identity:{pending_id}"))

    assert len(fake.answered) == 1, "the button must always be answered"
    assert len(fake.edits) == 1, "the result edits the prompt, it does not send a new message"
    assert len(fake.sent) == 1, "still just the original keyboard message"

    async with sessionmaker() as session:
        rows = (await session.execute(select(Memory))).scalars().all()
    assert len(rows) == 1
    assert rows[0].text == "я живу в Лилле"
    assert rows[0].kind == "identity"
    assert rows[0].source == "user"


async def test_a_replayed_kind_button_writes_one_memory(sessionmaker):
    """Idempotent through take_pending's DELETE ... RETURNING."""
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build_dp(sessionmaker, Settings())

    await _feed(dp, bot, _command_update(1, "/remember я живу в Лилле"))
    async with sessionmaker() as session:
        pending_id = (await session.execute(select(PendingMemory.id))).scalar_one()

    for _ in range(2):
        await _feed(dp, bot, _callback_update(2, f"m:k:identity:{pending_id}"))

    async with sessionmaker() as session:
        rows = (await session.execute(select(Memory))).scalars().all()
    assert len(rows) == 1
    assert fake.edits[-1].text == memory_ui.STALE


async def test_a_stale_kind_button_is_answered_not_ignored(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings())

    await _feed(dp, bot, _callback_update(1, "m:k:identity:999999"))

    assert len(fake.answered) == 1
    assert fake.edits[0].text == memory_ui.STALE


async def test_kind_button_reports_a_duplicate_instead_of_writing(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build_dp(sessionmaker, Settings())

    async with sessionmaker() as session:
        await memory.write_memory(
            session, kind="identity", text="я живу в Лилле", source="user"
        )

    await _feed(dp, bot, _command_update(1, "/remember я живу в Лилле"))
    async with sessionmaker() as session:
        pending_id = (await session.execute(select(PendingMemory.id))).scalar_one()
    await _feed(dp, bot, _callback_update(2, f"m:k:identity:{pending_id}"))

    assert fake.edits[0].text == memory_ui.REMEMBER_DUPLICATE
    async with sessionmaker() as session:
        rows = (await session.execute(select(Memory))).scalars().all()
    assert len(rows) == 1


# --- /memories ---


async def test_memories_empty(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings())

    await _feed(dp, bot, _command_update(1, "/memories"))

    assert fake.sent[0].text == memory_ui.MEMORIES_EMPTY
    assert fake.sent[0].reply_markup is None


async def test_memories_lists_id_kind_pin_and_text(sessionmaker):
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        row = await memory.write_memory(
            session, kind="identity", text="я живу в Лилле", source="user"
        )
        await memory.set_pinned(session, row.id, True)

    dp, bot, fake = _build_dp(sessionmaker, Settings())
    await _feed(dp, bot, _command_update(1, "/memories"))

    assert f"#{row.id} [identity] 📌 я живу в Лилле" in fake.sent[0].text


async def test_memories_trims_long_text(sessionmaker):
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        await memory.write_memory(
            session, kind="event", text="и" * 200, source="user"
        )

    dp, bot, fake = _build_dp(sessionmaker, Settings())
    await _feed(dp, bot, _command_update(1, "/memories"))

    assert "…" in fake.sent[0].text
    assert "и" * 200 not in fake.sent[0].text


async def test_memories_pages_and_the_first_page_has_no_back_arrow(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    async with sessionmaker() as session:
        for i in range(25):
            session.add(Memory(kind="event", text=f"факт {i}", source="user"))
        await session.commit()

    dp, bot, fake = _build_dp(sessionmaker, Settings())
    await _feed(dp, bot, _command_update(1, "/memories"))

    arrows = [b.text for row in fake.sent[0].reply_markup.inline_keyboard for b in row]
    assert arrows == ["›"], "no back arrow on the first page"

    await _feed(dp, bot, _callback_update(2, "m:p:20"))

    assert len(fake.edits) == 1, "paging edits, it does not send a new message"
    assert len(fake.sent) == 1
    arrows = [b.text for row in fake.edits[0].reply_markup.inline_keyboard for b in row]
    assert arrows == ["‹"], "no forward arrow on the last page"


async def test_a_replayed_page_callback_is_harmless(sessionmaker):
    """edit_keyboard swallows Telegram's "message is not modified"."""
    await _seed(sessionmaker, 1, 2)
    async with sessionmaker() as session:
        for i in range(25):
            session.add(Memory(kind="event", text=f"факт {i}", source="user"))
        await session.commit()

    dp, bot, fake = _build_dp(sessionmaker, Settings())
    await _feed(dp, bot, _command_update(1, "/memories"))
    for _ in range(2):
        await _feed(dp, bot, _callback_update(2, "m:p:20"))

    assert len(fake.answered) == 2
    assert len(fake.sent) == 1


# --- /forget ---


async def test_forget_deletes_and_audits_without_storing_text(sessionmaker):
    """Plan section 11: "state_change records `memory <id> deleted` with
    no text"."""
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        row = await memory.write_memory(
            session, kind="identity", text="я живу в Лилле", source="user"
        )

    dp, bot, fake = _build_dp(sessionmaker, Settings())
    await _feed(dp, bot, _command_update(1, f"/forget {row.id}"))

    assert fake.sent[0].text == memory_ui.FORGET_DONE.format(id=row.id)

    async with sessionmaker() as session:
        assert (await session.execute(select(Memory))).scalars().all() == []
        changes = (
            (await session.execute(select(StateChange).where(StateChange.field == "memory")))
            .scalars()
            .all()
        )

    assert len(changes) == 1
    audit = changes[0]
    assert audit.old_value == str(row.id)
    # The text must appear in no column at all -- old_value is exactly
    # where a future maintainer would helpfully put it.
    for value in (audit.field, audit.old_value, audit.new_value, audit.source):
        assert value is None or "Лилл" not in value


async def test_forget_accepts_a_hash_prefixed_id(sessionmaker):
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        row = await memory.write_memory(session, kind="event", text="факт", source="user")

    dp, bot, fake = _build_dp(sessionmaker, Settings())
    await _feed(dp, bot, _command_update(1, f"/forget #{row.id}"))

    assert fake.sent[0].text == memory_ui.FORGET_DONE.format(id=row.id)


async def test_forget_without_an_id_explains_usage(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings())

    await _feed(dp, bot, _command_update(1, "/forget"))

    assert fake.sent[0].text == memory_ui.FORGET_USAGE


async def test_forget_a_missing_id_says_so(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings())

    await _feed(dp, bot, _command_update(1, "/forget 999999"))

    assert fake.sent[0].text == memory_ui.FORGET_MISSING


async def test_forget_an_adopted_technique_is_protected_not_a_crash(sessionmaker):
    """W3 finding: forgetting the head of a chain a StudyCard still
    points at used to raise an unhandled IntegrityError -- this is the
    Telegram side of the same bug the web panel hits behind its own
    «Забыть» button, pre-existing (app.core.memory.forget's parity with
    Telegram is exact) but never exercised before W3 made it reachable
    from a UI button."""
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        row = await memory.write_memory(
            session, kind="technique", text="дыши перед сном", source="adopt"
        )
        job = StudyJob(kind="read", local_date=datetime.date(2026, 1, 1), status="done")
        session.add(job)
        await session.flush()
        clip = StudyClip(job_id=job.id, url="https://example.test/sleep", domain="example.test", text="т")
        session.add(clip)
        await session.flush()
        session.add(
            StudyCard(
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
                memory_id=row.id,
            )
        )
        await session.commit()
        memory_id = row.id

    dp, bot, fake = _build_dp(sessionmaker, Settings())
    await _feed(dp, bot, _command_update(1, f"/forget {memory_id}"))

    assert fake.sent[0].text == memory_ui.FORGET_PROTECTED
    async with sessionmaker() as session:
        assert await session.get(Memory, memory_id) is not None


async def test_a_replayed_forget_writes_one_audit_row(sessionmaker):
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        row = await memory.write_memory(session, kind="event", text="факт", source="user")

    dp, bot, fake = _build_dp(sessionmaker, Settings())
    for _ in range(2):
        await _feed(dp, bot, _command_update(1, f"/forget {row.id}"))

    async with sessionmaker() as session:
        changes = (
            (await session.execute(select(StateChange).where(StateChange.field == "memory")))
            .scalars()
            .all()
        )
    assert len(changes) == 1
    assert len(fake.sent) == 1


# --- /pin and /unpin ---


async def test_pin_and_unpin(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    async with sessionmaker() as session:
        row = await memory.write_memory(session, kind="event", text="факт", source="user")

    dp, bot, fake = _build_dp(sessionmaker, Settings())
    await _feed(dp, bot, _command_update(1, f"/pin {row.id}"))
    async with sessionmaker() as session:
        assert (await session.get(Memory, row.id)).pinned is True

    await _feed(dp, bot, _command_update(2, f"/unpin {row.id}"))
    async with sessionmaker() as session:
        assert (await session.get(Memory, row.id)).pinned is False

    assert fake.sent[0].text == memory_ui.PIN_DONE.format(id=row.id)
    assert fake.sent[1].text == memory_ui.UNPIN_DONE.format(id=row.id)


async def test_pin_refuses_past_the_cap(sessionmaker):
    """Section 7 caps the *render* at MEMORY_PINNED_MAX, so silently
    accepting one more would drop a memory the user explicitly asked to
    always be remembered, with no feedback."""
    settings = Settings(MEMORY_PINNED_MAX=2)
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        # Deliberately unrelated texts: near-duplicates would be deduped
        # on write and there would be nothing to pin.
        for phrase in ("пользователь живёт в Лилле", "пользователь пьёт чай без сахара"):
            row = await memory.write_memory(
                session, kind="event", text=phrase, source="user"
            )
            await memory.set_pinned(session, row.id, True)
        extra = await memory.write_memory(
            session, kind="event", text="по воскресеньям мы ездим к морю", source="user"
        )

    dp, bot, fake = _build_dp(sessionmaker, settings)
    await _feed(dp, bot, _command_update(1, f"/pin {extra.id}"))

    assert fake.sent[0].text == memory_ui.PIN_OVER_CAP.format(max=2)
    async with sessionmaker() as session:
        assert (await session.get(Memory, extra.id)).pinned is False


async def test_re_pinning_an_already_pinned_memory_at_the_cap_is_allowed(sessionmaker):
    """The cap guards adding a new pin, not re-affirming an existing one."""
    settings = Settings(MEMORY_PINNED_MAX=1)
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        row = await memory.write_memory(session, kind="event", text="факт", source="user")
        await memory.set_pinned(session, row.id, True)

    dp, bot, fake = _build_dp(sessionmaker, settings)
    await _feed(dp, bot, _command_update(1, f"/pin {row.id}"))

    assert fake.sent[0].text == memory_ui.PIN_DONE.format(id=row.id)


async def test_pin_a_missing_id_says_so(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings())

    await _feed(dp, bot, _command_update(1, "/pin 999999"))

    assert fake.sent[0].text == memory_ui.PIN_MISSING


# --- callback hygiene ---


async def test_an_unknown_callback_is_still_answered(sessionmaker):
    """An unanswered button spins in the client until Telegram times it out."""
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, Settings())

    await _feed(dp, bot, _callback_update(1, "totally:unknown"))

    assert len(fake.answered) == 1


async def test_memory_commands_are_registered_for_telegram():
    from app.tg.router import BOT_COMMANDS

    names = {command.command for command in BOT_COMMANDS}
    assert {"remember", "memories", "forget", "pin", "unpin"} <= names
