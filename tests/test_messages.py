"""Message storage tests (plan section 5 / 6.4 step 2 / 16).

- a user text message creates exactly one `message` row with the right
  update_id and ooc=false
- reprocessing the same update_id does not duplicate it
"""

from __future__ import annotations

from typing import AsyncGenerator

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.types import Message, Update
from sqlalchemy import select

from app.config import Settings
from app.db.models import Message as MessageRow
from app.db.queue import enqueue
from app.tg.router import build_router

TEST_CHAT_ID = 321


class FakeSession(BaseSession):
    """Captures outgoing methods instead of making real HTTP requests."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[SendMessage] = []
        self._next_message_id = 1

    async def close(self) -> None:
        pass

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None):
        if isinstance(method, SendMessage):
            self.sent.append(method)
            message_id = self._next_message_id
            self._next_message_id += 1
            return Message.model_validate(
                {
                    "message_id": message_id,
                    "date": 0,
                    "chat": {"id": method.chat_id, "type": "private"},
                    "text": method.text,
                },
                context={"bot": bot},
            )
        raise NotImplementedError(f"FakeSession cannot handle {method!r}")

    async def stream_content(
        self,
        url: str,
        headers: dict | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        raise NotImplementedError
        yield b""  # pragma: no cover


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


def _build_dp(sessionmaker) -> tuple[Dispatcher, Bot, FakeSession]:
    fake_session = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake_session)
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, Settings()))
    return dp, bot, fake_session


async def test_text_message_stored_once_with_role_and_update_id(sessionmaker):
    dp, bot, fake = _build_dp(sessionmaker)

    payload = _text_update(10, "hello anchor")
    async with sessionmaker() as session:
        await enqueue(session, 10, payload)

    update = Update.model_validate(payload, context={"bot": bot})
    await dp.feed_update(bot, update)

    async with sessionmaker() as session:
        result = await session.execute(select(MessageRow))
        rows = result.scalars().all()

    assert len(rows) == 1
    row = rows[0]
    assert row.role == "user"
    assert row.content == "hello anchor"
    assert row.update_id == 10
    assert row.ooc is False

    assert fake.sent[0].text == "hello anchor"
    await bot.session.close()


async def test_reprocessing_same_update_id_does_not_duplicate(sessionmaker):
    dp, bot, fake = _build_dp(sessionmaker)

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
    # The echo still fires each delivery (1c's idempotent turn replaces
    # this reply path; 1b only guards the stored message row).
    assert len(fake.sent) == 2

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
