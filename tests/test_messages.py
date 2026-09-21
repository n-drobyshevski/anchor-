"""Message storage tests (plan section 5 / 6.4 step 2 / 16).

- a user text message creates exactly one `message` row with the right
  update_id and ooc=false, plus one assistant row from the turn
- reprocessing the same update_id does not duplicate the user row, and
  (1c's idempotency) sends nothing and makes no second provider call
  the second time -- turn.run() resends the stored reply only when it
  was never marked sent, and here it already was
"""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy import select

from app.config import Settings
from app.db.models import Message as MessageRow
from app.db.models import UserState
from app.db.queue import enqueue
from app.tg.router import build_router
from conftest import FakeLLMProvider, FakeSession

TEST_CHAT_ID = 321


async def _seed_state(sessionmaker, chat_id: int = TEST_CHAT_ID) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=chat_id))
        await session.commit()


def _text_update(update_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": TEST_CHAT_ID, "type": "private"},
            "from": {"id": TEST_CHAT_ID, "is_bot": False, "first_name": "Test"},
            "text": text,
        },
    }


def _build_dp(sessionmaker, provider=None) -> tuple[Dispatcher, Bot, FakeSession]:
    fake_session = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake_session)
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, Settings(), provider or FakeLLMProvider()))
    return dp, bot, fake_session


async def test_text_message_stored_once_with_role_and_update_id(sessionmaker):
    await _seed_state(sessionmaker)
    provider = FakeLLMProvider(text="Принято.")
    dp, bot, fake = _build_dp(sessionmaker, provider)

    payload = _text_update(10, "hello anchor")
    async with sessionmaker() as session:
        await enqueue(session, 10, payload)

    update = Update.model_validate(payload, context={"bot": bot})
    await dp.feed_update(bot, update)

    async with sessionmaker() as session:
        result = await session.execute(select(MessageRow).where(MessageRow.role == "user"))
        rows = result.scalars().all()

    assert len(rows) == 1
    row = rows[0]
    assert row.role == "user"
    assert row.content == "hello anchor"
    assert row.update_id == 10
    assert row.ooc is False

    assert fake.sent[0].text == "Принято."
    await bot.session.close()


async def test_reprocessing_same_update_id_does_not_duplicate(sessionmaker):
    await _seed_state(sessionmaker)
    provider = FakeLLMProvider(text="Принято.")
    dp, bot, fake = _build_dp(sessionmaker, provider)

    payload = _text_update(20, "same update twice")
    async with sessionmaker() as session:
        await enqueue(session, 20, payload)

    update = Update.model_validate(payload, context={"bot": bot})

    # First delivery.
    await dp.feed_update(bot, update)
    # Simulated replay (e.g. worker.recover_stuck resetting a crashed row).
    await dp.feed_update(bot, update)

    async with sessionmaker() as session:
        result = await session.execute(
            select(MessageRow).where(MessageRow.update_id == 20, MessageRow.role == "user")
        )
        rows = result.scalars().all()

    assert len(rows) == 1
    # 1c's idempotent turn: the assistant row was already stored and
    # sent_at was set on the first delivery, so the second delivery's
    # step 2 does nothing at all -- no resend, no second provider call.
    assert len(fake.sent) == 1
    assert provider.calls == 1

    await bot.session.close()


async def test_non_text_message_gets_fixed_reply_and_stores_nothing(sessionmaker):
    dp, bot, fake = _build_dp(sessionmaker)

    payload = {
        "update_id": 30,
        "message": {
            "message_id": 30,
            "date": 0,
            "chat": {"id": TEST_CHAT_ID, "type": "private"},
            "from": {"id": TEST_CHAT_ID, "is_bot": False, "first_name": "Test"},
            "sticker": {
                "file_id": "abc",
                "file_unique_id": "abc-1",
                "type": "regular",
                "width": 100,
                "height": 100,
                "is_animated": False,
                "is_video": False,
            },
        },
    }
    update = Update.model_validate(payload, context={"bot": bot})
    await dp.feed_update(bot, update)

    assert fake.sent[0].text == "Пока только текст."

    async with sessionmaker() as session:
        result = await session.execute(select(MessageRow))
        assert result.scalars().all() == []

    await bot.session.close()
