"""Worker tests (plan section 16 / 6.3).

End-to-end echo: the worker claims a queued update, aiogram's router
handles it, and the outgoing SendMessage is captured by a fake aiogram
session instead of hitting the network. No real Telegram traffic runs
in this suite.

Also covers the polling.py round trip: dumping an aiogram Update with
model_dump(mode="json", by_alias=True, exclude_none=True) and reloading
it with Update.model_validate must reproduce the original update.
"""

from __future__ import annotations

from typing import AsyncGenerator

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.types import Message, Update

from app.config import Settings
from app.db.models import TelegramUpdate
from app.db.queue import enqueue
from app.tg.polling import dump_update
from app.tg.router import build_router
from app.worker import process_one_update

TEST_CHAT_ID = 777


class FakeSession(BaseSession):
    """Captures outgoing methods instead of making real HTTP requests."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[SendMessage] = []
        self._next_message_id = 1

    async def close(self) -> None:
        pass

    async def make_request(
        self, bot: Bot, method: TelegramMethod, timeout: int | None = None
    ):
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


def _update_payload(update_id: int, text: str) -> dict:
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


async def test_worker_echoes_text_and_marks_row_done(sessionmaker):
    fake_session = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake_session)
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, Settings()))

    async with sessionmaker() as session:
        await enqueue(session, 200, _update_payload(200, "hello anchor"))

    processed = await process_one_update(sessionmaker, dp, bot)
    assert processed is True

    assert len(fake_session.sent) == 1
    assert fake_session.sent[0].text == "hello anchor"
    assert fake_session.sent[0].chat_id == TEST_CHAT_ID

    async with sessionmaker() as session:
        row = await session.get(TelegramUpdate, 200)
        assert row.status == "done"

    await bot.session.close()


async def test_process_one_update_returns_false_when_queue_empty(sessionmaker):
    fake_session = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake_session)
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, Settings()))

    processed = await process_one_update(sessionmaker, dp, bot)
    assert processed is False
    assert fake_session.sent == []

    await bot.session.close()


async def test_polling_dump_round_trips_through_model_validate():
    """by_alias=True matters: aiogram renames wire fields (from -> from_user)."""
    original = Update.model_validate(_update_payload(300, "round trip me"))
    dumped = dump_update(original)

    # The wire alias must be present, not the Python attribute name.
    assert "from" in dumped["message"]
    assert "from_user" not in dumped["message"]

    reloaded = Update.model_validate(dumped)

    assert reloaded.update_id == original.update_id
    assert reloaded.message.text == original.message.text
    assert reloaded.message.chat.id == original.message.chat.id
    assert reloaded.message.from_user.id == original.message.from_user.id
