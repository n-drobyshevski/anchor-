"""`/order`, `/orders`, every `so:*` callback, and the counter-text path
through turn.run() (phase-5 plan sections 3 and 7, milestone 5c).

Router-level tests, same Dispatcher pattern as tests/test_notebook_
commands.py and tests/test_checkin.py: real Update payloads through
build_router()'s dispatcher.
"""

from __future__ import annotations

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy import select

from app.config import Settings
from app.core import orders
from app.db.models import StandingOrder, TelegramUpdate, UserState
from app.tg import orders as orders_ui
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


def _build_dp(sessionmaker, settings: Settings | None = None, provider=None):
    fake_session = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake_session)
    dp = Dispatcher()
    dp.include_router(
        build_router(sessionmaker, settings or Settings(), provider or FakeLLMProvider(text="Принято."))
    )
    return dp, bot, fake_session


async def _seed(sessionmaker, *update_ids: int, **state) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=TEST_CHAT_ID, timezone=TIMEZONE, **state))
        await session.commit()
        for update_id in update_ids:
            session.add(TelegramUpdate(update_id=update_id, payload={}))
        await session.commit()


async def _feed(dp, bot, payload: dict) -> None:
    await dp.feed_update(bot, Update.model_validate(payload, context={"bot": bot}))


async def _state(sessionmaker) -> UserState:
    async with sessionmaker() as session:
        return await session.get(UserState, 1)


async def _propose(sessionmaker, text: str = "пить воду по утрам", cadence: str = "daily") -> int:
    async with sessionmaker() as session:
        row = await orders.propose(session, text, cadence, None, source="anchor")
        return row.id


# --- /order ---------------------------------------------------------------


async def test_order_creates_an_active_order(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/order daily пить воду по утрам"))

    async with sessionmaker() as session:
        rows = (await session.execute(select(StandingOrder))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == orders.ACTIVE
    assert rows[0].cadence == orders.DAILY
    assert rows[0].source == "user"
    assert "пить воду по утрам" in fake.sent[0].text


async def test_order_with_no_args_explains_usage(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/order"))

    assert fake.sent[0].text == orders_ui.ORDER_USAGE


async def test_order_with_a_bad_cadence_explains_usage(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/order monthly раз в месяц"))

    assert fake.sent[0].text == orders_ui.ORDER_USAGE


async def test_order_daily_extreme_restriction_is_refused(sessionmaker):
    """Plan section 16's own acceptance line."""
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/order daily не есть до вечера"))

    assert fake.sent[0].text == orders.REFUSAL_TEXT
    async with sessionmaker() as session:
        rows = (await session.execute(select(StandingOrder))).scalars().all()
    assert rows == []


async def test_order_reports_the_cap(sessionmaker):
    settings = Settings(ORDERS_MAX_ACTIVE=1)
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build_dp(sessionmaker, settings)

    await _feed(dp, bot, _command_update(1, "/order daily первая"))
    await _feed(dp, bot, _command_update(2, "/order daily вторая"))

    assert fake.sent[1].text == orders.CAP_TEXT


# --- /orders ----------------------------------------------------------


async def test_orders_list_is_empty(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/orders"))

    assert fake.sent[0].text == orders_ui.ORDERS_LIST_EMPTY


async def test_orders_list_shows_active_orders_with_retire_buttons(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/order daily пить воду"))

    await _feed(dp, bot, _command_update(2, "/orders"))

    text = fake.sent[-1].text
    assert "«пить воду» (ежедневно)" in text
    buttons = [b.text for r in fake.sent[-1].reply_markup.inline_keyboard for b in r]
    assert len(buttons) == 1
    assert buttons[0].startswith(orders_ui.RETIRE)


async def test_orders_snyat_retires_and_rerenders(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/order daily пить воду"))
    await _feed(dp, bot, _command_update(2, "/orders"))

    async with sessionmaker() as session:
        row = (await session.execute(select(StandingOrder))).scalars().one()

    await _feed(dp, bot, _callback_update(3, f"so:x:{row.id}", message_id=len(fake.sent)))

    assert fake.edits[-1].text == orders_ui.ORDERS_LIST_EMPTY
    async with sessionmaker() as session:
        updated = await session.get(StandingOrder, row.id)
    assert updated.status == orders.RETIRED


async def test_orders_snyat_on_a_stale_id_is_answered(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _callback_update(1, "so:x:999999"))

    assert fake.edits[-1].text == orders_ui.STALE


# --- so:a / so:c / so:r on a proposal card ---------------------------------


async def test_so_a_accepts_a_proposal(sessionmaker):
    await _seed(sessionmaker, 1)
    order_id = await _propose(sessionmaker)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _callback_update(1, f"so:a:{order_id}"))

    assert orders_ui.ACCEPTED_TEXT in fake.edits[-1].text
    assert fake.edits[-1].reply_markup is None
    async with sessionmaker() as session:
        row = await session.get(StandingOrder, order_id)
    assert row.status == orders.ACTIVE


async def test_so_r_declines_a_proposal(sessionmaker):
    await _seed(sessionmaker, 1)
    order_id = await _propose(sessionmaker)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _callback_update(1, f"so:r:{order_id}"))

    assert orders_ui.DECLINED_TEXT in fake.edits[-1].text
    async with sessionmaker() as session:
        row = await session.get(StandingOrder, order_id)
    assert row.status == orders.DECLINED


async def test_so_a_reports_the_cap_and_keeps_the_buttons(sessionmaker):
    settings = Settings(ORDERS_MAX_ACTIVE=1)
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build_dp(sessionmaker, settings)
    await _feed(dp, bot, _command_update(1, "/order daily первая"))
    order_id = await _propose(sessionmaker, text="вторая")

    await _feed(dp, bot, _callback_update(2, f"so:a:{order_id}"))

    assert orders.CAP_TEXT in fake.edits[-1].text
    assert fake.edits[-1].reply_markup is not None
    async with sessionmaker() as session:
        row = await session.get(StandingOrder, order_id)
    assert row.status == orders.PROPOSED


async def test_so_a_on_a_stale_id_is_answered(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _callback_update(1, "so:a:999999"))

    assert fake.edits[-1].text == orders_ui.STALE


# --- so:c starts a counter, and the plain-text path finishes it ------------


async def test_so_c_then_counter_text_then_accept_mine(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3)
    order_id = await _propose(sessionmaker, text="бегать каждый день")
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _callback_update(1, f"so:c:{order_id}"))
    assert fake.edits[-1].text == orders.COUNTER_PROMPT_TEXT

    state = await _state(sessionmaker)
    assert state.awaiting == orders.AWAITING_SO_COUNTER
    assert state.awaiting_ref == order_id

    await _feed(dp, bot, _text_update(2, "weekdays бегать по будням"))

    async with sessionmaker() as session:
        original = await session.get(StandingOrder, order_id)
        counter = (
            await session.execute(select(StandingOrder).where(StandingOrder.counter_of == order_id))
        ).scalars().one()
    assert original.status == orders.DECLINED
    assert counter.status == orders.COUNTERED
    assert counter.cadence == orders.WEEKDAYS
    assert "Твой вариант" in fake.sent[-1].text

    state = await _state(sessionmaker)
    assert state.awaiting is None

    await _feed(dp, bot, _callback_update(3, f"so:a:{counter.id}", message_id=len(fake.sent)))
    async with sessionmaker() as session:
        row = await session.get(StandingOrder, counter.id)
    assert row.status == orders.ACTIVE


async def test_so_c_then_counter_text_then_cancel(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3)
    order_id = await _propose(sessionmaker, text="читать")
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _callback_update(1, f"so:c:{order_id}"))
    await _feed(dp, bot, _text_update(2, "читать по выходным"))

    async with sessionmaker() as session:
        counter = (
            await session.execute(select(StandingOrder).where(StandingOrder.counter_of == order_id))
        ).scalars().one()

    await _feed(dp, bot, _callback_update(3, f"so:r:{counter.id}", message_id=len(fake.sent)))
    async with sessionmaker() as session:
        row = await session.get(StandingOrder, counter.id)
    assert row.status == orders.DECLINED


async def test_a_counter_card_has_no_change_button(sessionmaker):
    """One round only: `so:c` on the counter itself is stale."""
    await _seed(sessionmaker, 1, 2, 3)
    order_id = await _propose(sessionmaker, text="медитировать")
    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _callback_update(1, f"so:c:{order_id}"))
    await _feed(dp, bot, _text_update(2, "медитировать по утрам"))
    async with sessionmaker() as session:
        counter = (
            await session.execute(select(StandingOrder).where(StandingOrder.counter_of == order_id))
        ).scalars().one()

    await _feed(dp, bot, _callback_update(3, f"so:c:{counter.id}", message_id=len(fake.sent)))

    assert fake.edits[-1].text == orders_ui.STALE


async def test_a_pause_word_during_so_counter_pauses_instead(sessionmaker):
    """Plan section 13: pause words always win and clear awaiting."""
    await _seed(sessionmaker, 1, 2)
    order_id = await _propose(sessionmaker, text="плавать")
    provider = FakeLLMProvider(text="не должно вызваться")
    dp, bot, fake = _build_dp(sessionmaker, provider=provider)

    await _feed(dp, bot, _callback_update(1, f"so:c:{order_id}"))
    await _feed(dp, bot, _text_update(2, "пурпурный"))

    state = await _state(sessionmaker)
    assert state.persona_active is False
    assert state.awaiting is None
    assert provider.calls == 0

    async with sessionmaker() as session:
        original = await session.get(StandingOrder, order_id)
    # The negotiation itself is left exactly where the pause word found
    # it -- a pause is not a decision about the order.
    assert original.status == orders.AWAITING_COUNTER


async def test_so_c_on_a_stale_id_is_answered(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _callback_update(1, "so:c:999999"))

    assert fake.edits[-1].text == orders_ui.STALE


async def test_a_high_risk_counter_declines_the_negotiation(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    order_id = await _propose(sessionmaker, text="бегать")
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _callback_update(1, f"so:c:{order_id}"))
    await _feed(dp, bot, _text_update(2, "не есть до вечера"))

    assert fake.sent[-1].text == orders.REFUSAL_TEXT
    async with sessionmaker() as session:
        original = await session.get(StandingOrder, order_id)
        counters = (
            await session.execute(select(StandingOrder).where(StandingOrder.counter_of == order_id))
        ).scalars().all()
    assert original.status == orders.DECLINED
    assert counters == []


async def test_replaying_the_counter_text_update_does_not_double_decide(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    order_id = await _propose(sessionmaker, text="писать дневник")
    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _callback_update(1, f"so:c:{order_id}"))

    for _ in range(2):
        await _feed(dp, bot, _text_update(2, "писать дневник по вечерам"))

    async with sessionmaker() as session:
        counters = (
            await session.execute(select(StandingOrder).where(StandingOrder.counter_of == order_id))
        ).scalars().all()
    assert len(counters) == 1


# --- registration --------------------------------------------------------


async def test_order_commands_are_registered_for_telegram():
    from app.tg.router import BOT_COMMANDS

    names = {command.command for command in BOT_COMMANDS}
    assert {"order", "orders"} <= names
