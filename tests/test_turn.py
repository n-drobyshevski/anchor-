"""core/turn.py tests (plan section 8 / 16).

Every test uses FakeLLMProvider and FakeSession -- no test in this
module ever reaches the network. `LLMRetryableError(retry_after=0)` is
used throughout so turn.py's retry backoff sleeps are effectively
instant and the suite stays fast.
"""

from __future__ import annotations

import datetime
import decimal
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy import select

from app.config import Settings
from app.core import turn
from app.core.spend import local_date_for
from app.db.models import Message, SpendLedger, TelegramUpdate, UserState
from app.llm.provider import LLMError, LLMRetryableError, LLMUsage
from conftest import FakeLLMProvider, FakeSession

TEST_CHAT_ID = 4242
TIMEZONE = "Europe/Paris"


async def _seed(sessionmaker, *, chat_id: int = TEST_CHAT_ID, update_id: int, intensity: int = 3) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=chat_id, timezone=TIMEZONE, intensity=intensity))
        await session.commit()
        session.add(TelegramUpdate(update_id=update_id, payload={}))
        await session.commit()


def _bot() -> tuple[Bot, FakeSession]:
    fake_session = FakeSession()
    return Bot(token="123456:TESTTOKEN", session=fake_session), fake_session


async def _assistant_row(sessionmaker, update_id: int) -> Message | None:
    async with sessionmaker() as session:
        result = await session.execute(select(Message).where(Message.reply_to_update == update_id))
        return result.scalar_one_or_none()


async def _user_rows(sessionmaker, update_id: int) -> list[Message]:
    async with sessionmaker() as session:
        result = await session.execute(
            select(Message).where(Message.update_id == update_id, Message.role == "user")
        )
        return list(result.scalars().all())


async def _ledger_rows(sessionmaker) -> list[SpendLedger]:
    async with sessionmaker() as session:
        result = await session.execute(select(SpendLedger))
        return list(result.scalars().all())


async def test_successful_turn_stores_rows_sends_reply_and_writes_ledger(sessionmaker):
    update_id = 1
    await _seed(sessionmaker, update_id=update_id)
    bot, fake_session = _bot()
    usage = LLMUsage(input_tokens=120, cached_tokens=20, output_tokens=40, cost_usd=None)
    provider = FakeLLMProvider(text="Принято. Дальше.", usage=usage, model="grok-4.7-fake")

    await turn.run(
        sessionmaker,
        bot,
        Settings(),
        provider,
        chat_id=TEST_CHAT_ID,
        update_id=update_id,
        user_text="привет",
    )

    assert provider.calls == 1
    assert len(fake_session.sent) == 1
    assert fake_session.sent[0].text == "Принято. Дальше."
    assert fake_session.sent[0].chat_id == TEST_CHAT_ID

    user_rows = await _user_rows(sessionmaker, update_id)
    assert len(user_rows) == 1
    assert user_rows[0].content == "привет"

    assistant = await _assistant_row(sessionmaker, update_id)
    assert assistant is not None
    assert assistant.content == "Принято. Дальше."
    assert assistant.model == "grok-4.7-fake"
    assert assistant.tokens_in == 120
    assert assistant.tokens_cached == 20
    assert assistant.tokens_out == 40
    assert assistant.usd_cost is not None
    assert assistant.sent_at is not None

    ledger = await _ledger_rows(sessionmaker)
    assert len(ledger) == 1
    assert ledger[0].usd_cost == assistant.usd_cost
    assert ledger[0].local_date == local_date_for(TIMEZONE)
    assert ledger[0].category == "chat"

    await bot.session.close()


async def test_resend_when_sent_at_is_null(sessionmaker):
    """Simulates a crash between generating the reply and sending it:
    the assistant row exists with sent_at NULL. turn.run() must resend
    the stored content without calling the provider again.
    """
    update_id = 2
    await _seed(sessionmaker, update_id=update_id)
    async with sessionmaker() as session:
        session.add(
            Message(
                role="assistant",
                content="Уже сгенерированный ответ.",
                update_id=update_id,
                reply_to_update=update_id,
                sent_at=None,
                usd_cost=decimal.Decimal("0.001000"),
            )
        )
        await session.commit()

    bot, fake_session = _bot()
    provider = FakeLLMProvider()

    await turn.run(
        sessionmaker,
        bot,
        Settings(),
        provider,
        chat_id=TEST_CHAT_ID,
        update_id=update_id,
        user_text="привет ещё раз",
    )

    assert provider.calls == 0
    assert len(fake_session.sent) == 1
    assert fake_session.sent[0].text == "Уже сгенерированный ответ."

    assistant = await _assistant_row(sessionmaker, update_id)
    assert assistant.sent_at is not None

    await bot.session.close()


async def test_no_regeneration_when_assistant_row_already_sent(sessionmaker):
    update_id = 3
    await _seed(sessionmaker, update_id=update_id)
    sent_at = datetime.datetime.now(datetime.timezone.utc)
    async with sessionmaker() as session:
        session.add(
            Message(
                role="assistant",
                content="Уже отправлено.",
                update_id=update_id,
                reply_to_update=update_id,
                sent_at=sent_at,
                usd_cost=decimal.Decimal("0.001000"),
            )
        )
        await session.commit()

    bot, fake_session = _bot()
    provider = FakeLLMProvider()

    await turn.run(
        sessionmaker,
        bot,
        Settings(),
        provider,
        chat_id=TEST_CHAT_ID,
        update_id=update_id,
        user_text="привет снова",
    )

    assert provider.calls == 0
    assert fake_session.sent == []

    await bot.session.close()


async def test_over_cap_makes_zero_provider_calls_and_writes_no_ledger_row(sessionmaker):
    update_id = 4
    await _seed(sessionmaker, update_id=update_id)
    settings = Settings(DAILY_USD_CAP=0.50)
    today = local_date_for(TIMEZONE)
    async with sessionmaker() as session:
        session.add(SpendLedger(local_date=today, category="chat", usd_cost=decimal.Decimal("0.50")))
        await session.commit()

    bot, fake_session = _bot()
    provider = FakeLLMProvider()

    await turn.run(
        sessionmaker,
        bot,
        settings,
        provider,
        chat_id=TEST_CHAT_ID,
        update_id=update_id,
        user_text="ещё один вопрос",
    )

    assert provider.calls == 0
    assert len(fake_session.sent) == 1
    assert fake_session.sent[0].text == turn.CAP_REPLY_TEXT

    assistant = await _assistant_row(sessionmaker, update_id)
    assert assistant is not None
    assert assistant.usd_cost == decimal.Decimal("0")
    assert assistant.sent_at is not None

    # Still only the one ledger row seeded above -- the cap path must
    # never write a second one.
    ledger = await _ledger_rows(sessionmaker)
    assert len(ledger) == 1

    await bot.session.close()


async def test_provider_non_retryable_failure_stores_no_assistant_row(sessionmaker):
    update_id = 5
    await _seed(sessionmaker, update_id=update_id)
    bot, fake_session = _bot()
    provider = FakeLLMProvider(raises=[LLMError("BadRequestError")])

    await turn.run(
        sessionmaker,
        bot,
        Settings(),
        provider,
        chat_id=TEST_CHAT_ID,
        update_id=update_id,
        user_text="сообщение",
    )

    assert provider.calls == 1
    assert len(fake_session.sent) == 1
    assert fake_session.sent[0].text == turn.FAILURE_REPLY_TEXT

    assert await _assistant_row(sessionmaker, update_id) is None
    assert await _ledger_rows(sessionmaker) == []

    await bot.session.close()


async def test_provider_retries_exhausted_stores_no_assistant_row(sessionmaker):
    update_id = 6
    await _seed(sessionmaker, update_id=update_id)
    bot, fake_session = _bot()
    provider = FakeLLMProvider(
        raises=[
            LLMRetryableError(retry_after=0),
            LLMRetryableError(retry_after=0),
            LLMRetryableError(retry_after=0),
        ]
    )

    await turn.run(
        sessionmaker,
        bot,
        Settings(),
        provider,
        chat_id=TEST_CHAT_ID,
        update_id=update_id,
        user_text="сообщение",
    )

    # MAX_RETRIES=2 -> 3 total attempts before giving up.
    assert provider.calls == 3
    assert fake_session.sent[0].text == turn.FAILURE_REPLY_TEXT
    assert await _assistant_row(sessionmaker, update_id) is None

    await bot.session.close()


async def test_provider_retries_then_succeeds(sessionmaker):
    update_id = 7
    await _seed(sessionmaker, update_id=update_id)
    bot, fake_session = _bot()
    provider = FakeLLMProvider(
        text="Всё-таки получилось.",
        raises=[LLMRetryableError(retry_after=0), LLMRetryableError(retry_after=0)],
    )

    await turn.run(
        sessionmaker,
        bot,
        Settings(),
        provider,
        chat_id=TEST_CHAT_ID,
        update_id=update_id,
        user_text="сообщение",
    )

    assert provider.calls == 3
    assert fake_session.sent[0].text == "Всё-таки получилось."
    assistant = await _assistant_row(sessionmaker, update_id)
    assert assistant is not None
    assert assistant.sent_at is not None

    await bot.session.close()


async def test_no_double_user_message_across_two_turns(sessionmaker):
    """End-to-end regression: run two real turns and inspect exactly
    what was sent to the provider on the second call. The second
    call's transcript must contain the first turn's user+assistant
    messages exactly once each, and the second user's text must appear
    only as the final message, never duplicated via the transcript.
    """
    provider = FakeLLMProvider(text="Ответ номер один.")
    bot, fake_session = _bot()

    await _seed(sessionmaker, update_id=10)
    await turn.run(
        sessionmaker,
        bot,
        Settings(),
        provider,
        chat_id=TEST_CHAT_ID,
        update_id=10,
        user_text="первое сообщение",
    )

    async with sessionmaker() as session:
        session.add(TelegramUpdate(update_id=11, payload={}))
        await session.commit()

    provider.text = "Ответ номер два."
    await turn.run(
        sessionmaker,
        bot,
        Settings(),
        provider,
        chat_id=TEST_CHAT_ID,
        update_id=11,
        user_text="второе сообщение",
    )

    assert provider.calls == 2
    second_call_messages = provider.received_messages[1]
    contents = [m.content for m in second_call_messages]

    assert contents.count("первое сообщение") == 1
    assert contents.count("второе сообщение") == 1
    # The new user text must be the last message, not folded into the
    # transcript a second time.
    assert second_call_messages[-1].role == "user"
    assert second_call_messages[-1].content == "второе сообщение"

    await bot.session.close()
