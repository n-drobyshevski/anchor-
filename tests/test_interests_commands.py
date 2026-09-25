"""`/interests`, `/interests add <forums|guides|ref> <тема>`, and the
`it:x:<id>` callback (Phase 6 plan section 7, milestone 6d). Router-level
tests, same Dispatcher pattern as tests/test_orders_commands.py.
"""

from __future__ import annotations

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy import select

from app.config import Settings
from app.core import interests
from app.db.models import InterestTopic, TelegramUpdate, UserState
from app.tg import interests as interests_ui
from app.tg.router import BOT_COMMANDS, build_router
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


def _build_dp(sessionmaker, settings: Settings | None = None):
    fake_session = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake_session)
    dp = Dispatcher()
    dp.include_router(
        build_router(sessionmaker, settings or Settings(), FakeLLMProvider(text="Принято."))
    )
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


async def test_interests_is_registered():
    assert any(c.command == "interests" for c in BOT_COMMANDS)


# --- /interests (list) -----------------------------------------------------


async def test_interests_list_is_empty(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/interests"))

    assert fake.sent[0].text == interests_ui.TOPICS_EMPTY


async def test_interests_list_shows_topics_with_remove_buttons(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    async with sessionmaker() as session:
        await interests.add_topic(session, Settings(), packet="forums", text="бессонница")
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(2, "/interests"))

    text = fake.sent[-1].text
    assert "[forums] бессонница" in text
    buttons = [b.text for r in fake.sent[-1].reply_markup.inline_keyboard for b in r]
    assert len(buttons) == 1
    assert buttons[0].startswith(interests_ui.REMOVE)


# --- /interests add ---------------------------------------------------------


async def test_interests_add_creates_a_topic(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/interests add forums бессонница"))

    async with sessionmaker() as session:
        rows = (await session.execute(select(InterestTopic))).scalars().all()
    assert len(rows) == 1
    assert rows[0].text == "бессонница"
    assert rows[0].packet == "forums"
    assert "бессонница" in fake.sent[0].text


async def test_interests_add_with_no_args_explains_usage(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/interests add"))

    assert fake.sent[0].text == interests_ui.ADD_USAGE
    async with sessionmaker() as session:
        assert (await session.execute(select(InterestTopic))).scalars().all() == []


async def test_interests_add_with_only_a_packet_explains_usage(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/interests add forums"))

    assert fake.sent[0].text == interests_ui.ADD_USAGE


async def test_interests_add_refuses_an_unknown_packet(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/interests add whatever бессонница"))

    assert fake.sent[0].text == interests_ui.UNKNOWN_PACKET_REPLY
    async with sessionmaker() as session:
        assert (await session.execute(select(InterestTopic))).scalars().all() == []


async def test_interests_add_refuses_a_high_risk_topic(sessionmaker):
    """Plan section 7's own acceptance line: a `high` hit gets «Такое не ищу.»."""
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/interests add forums дозировка мелатонина"))

    assert fake.sent[0].text == interests.REFUSAL_TEXT
    async with sessionmaker() as session:
        assert (await session.execute(select(InterestTopic))).scalars().all() == []


async def test_interests_add_reports_the_cap(sessionmaker):
    await _seed(sessionmaker, *range(1, interests.MAX_ACTIVE_TOPICS + 2))
    dp, bot, fake = _build_dp(sessionmaker)
    for i, update_id in enumerate(range(1, interests.MAX_ACTIVE_TOPICS + 1)):
        await _feed(dp, bot, _command_update(update_id, f"/interests add forums тема{i}"))

    await _feed(
        dp, bot, _command_update(interests.MAX_ACTIVE_TOPICS + 1, "/interests add forums лишняя")
    )

    assert fake.sent[-1].text == interests.CAP_TEXT


# --- it:x: (the [✖] button) -------------------------------------------------


async def test_it_x_removes_and_rerenders_the_list(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/interests add forums бессонница"))
    await _feed(dp, bot, _command_update(2, "/interests"))
    async with sessionmaker() as session:
        row = (await session.execute(select(InterestTopic))).scalars().one()

    await _feed(dp, bot, _callback_update(3, f"it:x:{row.id}", message_id=len(fake.sent)))

    assert fake.edits[-1].text == interests_ui.TOPICS_EMPTY
    async with sessionmaker() as session:
        updated = await session.get(InterestTopic, row.id)
    assert updated.active is False


async def test_it_x_on_a_stale_id_is_answered(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _callback_update(1, "it:x:999999"))

    assert fake.edits[-1].text == interests_ui.STALE


async def test_it_x_on_an_already_removed_topic_is_answered(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/interests add forums бессонница"))
    async with sessionmaker() as session:
        row = (await session.execute(select(InterestTopic))).scalars().one()

    await _feed(dp, bot, _callback_update(2, f"it:x:{row.id}", message_id=1))
    await _feed(dp, bot, _callback_update(3, f"it:x:{row.id}", message_id=2))

    assert fake.edits[-1].text == interests_ui.STALE
