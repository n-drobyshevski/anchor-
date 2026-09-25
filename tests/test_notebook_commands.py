"""`/mind`, `/mind add` and the `nb:x:<id>` close button (phase-5 plan
section 6, milestone 5b).

Router-level tests, same Dispatcher pattern as tests/test_memory_commands.py:
real Update payloads through build_router()'s dispatcher, so the command
filters, the callback filter and the replay gates are all exercised as
they actually run.
"""

from __future__ import annotations

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy import select

from app.config import Settings
from app.core import notebook
from app.db.models import NotebookEntry, TelegramUpdate, UserState
from app.tg import notebook as notebook_ui
from app.tg.router import build_router
from conftest import FakeLLMProvider, FakeSession

pytestmark = pytest.mark.asyncio

TEST_CHAT_ID = 555
TIMEZONE = "Europe/Paris"


def _command_update(update_id: int, text: str) -> dict:
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


def _build_dp(sessionmaker, settings: Settings | None = None) -> tuple[Dispatcher, Bot, FakeSession]:
    fake_session = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake_session)
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, settings or Settings(), FakeLLMProvider()))
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


# --- /mind (listing) -------------------------------------------------------


async def test_mind_is_empty(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/mind"))

    assert fake.sent[0].text == notebook_ui.MIND_EMPTY
    assert fake.sent[0].reply_markup is None


async def test_mind_lists_entries_grouped_by_kind_with_close_buttons(sessionmaker):
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        session.add(NotebookEntry(kind="intention", text="меньше сидеть в телефоне", source="user"))
        session.add(NotebookEntry(kind="observation", text="пишет по вечерам", source="anchor"))
        session.add(NotebookEntry(kind="open_thread", text="обещал разобрать почту", source="anchor"))
        await session.commit()
        rows = (await session.execute(select(NotebookEntry))).scalars().all()
    ids_by_text = {row.text: row.id for row in rows}

    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/mind"))

    text = fake.sent[0].text
    assert "Намерения:" in text
    assert "Наблюдения:" in text
    assert "Незакрытое:" in text
    assert f"#{ids_by_text['меньше сидеть в телефоне']} меньше сидеть в телефоне" in text

    buttons = [
        button.text
        for row in fake.sent[0].reply_markup.inline_keyboard
        for button in row
    ]
    assert len(buttons) == 3
    assert all(b.startswith("✖ #") for b in buttons)


# --- the close callback -----------------------------------------------------


async def test_close_button_closes_an_anchor_entry(sessionmaker):
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        entry = NotebookEntry(kind="observation", text="пишет по вечерам", source="anchor")
        session.add(entry)
        await session.commit()
        await session.refresh(entry)

    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _callback_update(1, f"nb:x:{entry.id}"))

    assert len(fake.answered) == 1
    assert len(fake.edits) == 1
    assert fake.edits[0].text == notebook_ui.MIND_EMPTY

    async with sessionmaker() as session:
        row = await session.get(NotebookEntry, entry.id)
    assert row.active is False
    assert row.closed_by == "user"


async def test_close_button_closes_a_user_entry(sessionmaker):
    """The user can close any entry, including their own."""
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        entry = NotebookEntry(kind="intention", text="бросить курить", source="user")
        session.add(entry)
        await session.commit()
        await session.refresh(entry)

    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _callback_update(1, f"nb:x:{entry.id}"))

    async with sessionmaker() as session:
        row = await session.get(NotebookEntry, entry.id)
    assert row.active is False
    assert row.closed_by == "user"


async def test_close_button_closes_a_review_entry(sessionmaker):
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        entry = NotebookEntry(kind="observation", text="из ревью", source="review")
        session.add(entry)
        await session.commit()
        await session.refresh(entry)

    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _callback_update(1, f"nb:x:{entry.id}"))

    async with sessionmaker() as session:
        row = await session.get(NotebookEntry, entry.id)
    assert row.active is False
    assert row.closed_by == "user"


async def test_a_stale_close_button_is_answered_not_ignored(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _callback_update(1, "nb:x:999999"))

    assert len(fake.answered) == 1
    assert fake.edits[0].text == notebook_ui.STALE


async def test_a_replayed_close_button_answers_stale_the_second_time(sessionmaker):
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        entry = NotebookEntry(kind="observation", text="что-то", source="anchor")
        session.add(entry)
        await session.commit()
        await session.refresh(entry)

    dp, bot, fake = _build_dp(sessionmaker)
    for _ in range(2):
        await _feed(dp, bot, _callback_update(1, f"nb:x:{entry.id}", message_id=901))

    assert len(fake.answered) == 2
    async with sessionmaker() as session:
        row = await session.get(NotebookEntry, entry.id)
    assert row.active is False


# --- /mind add ---------------------------------------------------------


async def test_mind_add_ok(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/mind add быть добрее к себе"))

    assert fake.sent[0].text == notebook_ui.ADD_REPLIES["ok"]
    async with sessionmaker() as session:
        rows = (await session.execute(select(NotebookEntry))).scalars().all()
    assert len(rows) == 1
    assert rows[0].kind == "intention"
    assert rows[0].source == "user"


async def test_mind_add_with_no_text_explains_usage(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/mind add"))

    assert fake.sent[0].text == notebook_ui.MIND_ADD_USAGE


async def test_mind_add_refuses_a_high_risk_text(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/mind add принимать 500 мг мелатонина"))

    assert fake.sent[0].text == notebook_ui.ADD_REPLIES["refused"]
    async with sessionmaker() as session:
        rows = (await session.execute(select(NotebookEntry))).scalars().all()
    assert rows == []


async def test_mind_add_reports_the_cap(sessionmaker):
    settings = Settings(NOTEBOOK_MAX_INTENTIONS=1)
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build_dp(sessionmaker, settings)

    await _feed(dp, bot, _command_update(1, "/mind add первое намерение"))
    await _feed(dp, bot, _command_update(2, "/mind add совсем другое про сон и режим"))

    assert fake.sent[1].text == notebook_ui.ADD_REPLIES["cap"]


async def test_mind_add_reports_too_long(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/mind add " + "и" * 241))

    assert fake.sent[0].text == notebook_ui.ADD_REPLIES["too_long"]


async def test_mind_add_reports_a_duplicate(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/mind add быть добрее к себе"))
    await _feed(dp, bot, _command_update(2, "/mind add быть добрее к себе"))

    assert fake.sent[1].text == notebook_ui.ADD_REPLIES["duplicate"]


async def test_a_replayed_mind_add_writes_one_entry(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    for _ in range(2):
        await _feed(dp, bot, _command_update(1, "/mind add быть добрее к себе"))

    async with sessionmaker() as session:
        rows = (await session.execute(select(NotebookEntry))).scalars().all()
    assert len(rows) == 1
    assert len(fake.sent) == 1


async def test_mind_commands_are_registered_for_telegram():
    from app.tg.router import BOT_COMMANDS

    names = {command.command for command in BOT_COMMANDS}
    assert "mind" in names
