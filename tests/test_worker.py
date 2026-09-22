"""Worker tests (plan section 16 / 6.3).

End-to-end persona turn: the worker claims a queued update, aiogram's
router hands it to turn.run(), and the outgoing SendMessage is captured
by FakeSession instead of hitting the network. FakeLLMProvider stands
in for xAI, so no real Telegram or LLM traffic runs in this suite.

Also covers the polling.py round trip: dumping an aiogram Update with
model_dump(mode="json", by_alias=True, exclude_none=True) and reloading
it with Update.model_validate must reproduce the original update.
"""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.types import Update

from app.config import Settings
from app.db.models import TelegramUpdate, UserState
from app.db.queue import enqueue
from app.tg.polling import dump_update
from app.tg.router import build_router
from app.worker import process_one_update
from conftest import FakeLLMProvider, FakeSession

TEST_CHAT_ID = 777


async def _seed_state(sessionmaker, chat_id: int = TEST_CHAT_ID) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=chat_id))
        await session.commit()


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


async def test_worker_runs_persona_turn_and_marks_row_done(sessionmaker, clock):
    await _seed_state(sessionmaker)
    fake_session = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake_session)
    provider = FakeLLMProvider(text="Принято.")
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, Settings(), provider))

    async with sessionmaker() as session:
        await enqueue(session, 200, _update_payload(200, "hello anchor"))

    processed = await process_one_update(sessionmaker, dp, bot, clock)
    assert processed is True

    assert len(fake_session.sent) == 1
    assert fake_session.sent[0].text == "Принято."
    assert fake_session.sent[0].chat_id == TEST_CHAT_ID
    assert provider.calls == 1

    async with sessionmaker() as session:
        row = await session.get(TelegramUpdate, 200)
        assert row.status == "done"

    await bot.session.close()


async def test_process_one_update_returns_false_when_queue_empty(sessionmaker, clock):
    fake_session = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake_session)
    provider = FakeLLMProvider()
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, Settings(), provider))

    processed = await process_one_update(sessionmaker, dp, bot, clock)
    assert processed is False
    assert fake_session.sent == []
    assert provider.calls == 0

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
