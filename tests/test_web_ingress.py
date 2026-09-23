"""app/web/ingress.py tests (web-chat plan track 1).

- the synthetic message Update payload validates as an aiogram Update
- ids are negative and increasing (origin is the sign of update_id --
  app/db/models.py's TelegramUpdate/WebUpdate)
- a repeated client_key stores only one row (idempotent retry)
- /delete, /export, /delete@x, /EXPORT (case) and `d:` callbacks are
  all rejected before anything is queued
- a press whose data is not on the hub's allowlist is rejected
"""

from __future__ import annotations

import pytest
from aiogram.types import Update
from sqlalchemy import select

from app.config import Settings
from app.db.models import TelegramUpdate, WebUpdate
from app.web.hub import WebHub
from app.web.ingress import (
    BlockedCommand,
    PressRejected,
    build_callback_update,
    build_message_update,
    checkin_complete,
    is_blocked_command,
    press,
    send_text,
)

ALLOWED_CHAT_ID = 4242


def _settings() -> Settings:
    return Settings(ALLOWED_CHAT_ID=ALLOWED_CHAT_ID)


# --- payload shape ---


def test_build_message_update_validates_as_aiogram_update():
    payload = build_message_update(-1, "привет", _settings())
    update = Update.model_validate(payload)
    assert update.update_id == -1
    assert update.message.text == "привет"
    assert update.message.chat.id == ALLOWED_CHAT_ID
    assert update.message.from_user.id == ALLOWED_CHAT_ID
    # No `entities` field: the design's grounded claim is that aiogram's
    # Command filter reads only message.text, never entities.
    assert update.message.entities is None


def test_build_callback_update_validates_as_aiogram_update():
    payload = build_callback_update(-2, message_id=-1, data="w:resume", settings=_settings())
    update = Update.model_validate(payload)
    assert update.update_id == -2
    assert update.callback_query.data == "w:resume"
    assert update.callback_query.message.message_id == -1
    assert update.callback_query.from_user.id == ALLOWED_CHAT_ID
    assert update.callback_query.message.text == ""  # default: no text given


def test_build_callback_update_carries_the_message_text():
    """Medium-severity finding: the synthetic callback_query's message
    text used to be hardcoded "" unconditionally. app/tg/welfare.py's
    handle_callback appends its acknowledgement to
    `callback.message.text`, so an empty base replaced a whole welfare
    reply -- including any crisis-support text -- with just the ack."""
    payload = build_callback_update(
        -2, message_id=-1, data="w:resume", settings=_settings(), text="как ты?"
    )
    update = Update.model_validate(payload)
    assert update.callback_query.message.text == "как ты?"


# --- is_blocked_command: exact tokenization, no drift from the router ---


@pytest.mark.parametrize(
    "text",
    [
        "/delete",
        "/export",
        "/DELETE",
        "/Export",
        "/delete@AnchorBot",
        "/export@AnchorBot",
        "/delete extra args",
        "/export with nbsp",  # split(maxsplit=1) treats any whitespace run
        "/export ",
        "  /delete  ",
    ],
)
def test_blocked_commands_are_rejected(text):
    assert is_blocked_command(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "hello",
        "/out",
        "/in",
        "/state",
        "/checkin",
        "экспорт",  # Russian word, not the command
        "delete",  # no "/" prefix
        "",
        "   ",
        "/",
    ],
)
def test_ordinary_text_is_not_blocked(text):
    assert is_blocked_command(text) is False


# --- send_text: end to end through enqueue_web ---


async def test_send_text_blocks_export_and_delete_before_queueing(sessionmaker):
    async with sessionmaker() as session:
        with pytest.raises(BlockedCommand):
            await send_text(session, settings=_settings(), text="/export", client_key="k1")
        with pytest.raises(BlockedCommand):
            await send_text(session, settings=_settings(), text="/delete@x", client_key="k2")

    async with sessionmaker() as session:
        count = (await session.execute(select(TelegramUpdate))).scalars().all()
    assert count == []


async def test_send_text_enqueues_a_negative_increasing_web_row(sessionmaker):
    async with sessionmaker() as session:
        id1 = await send_text(session, settings=_settings(), text="привет", client_key="a")
        id2 = await send_text(session, settings=_settings(), text="ещё", client_key="b")

    assert id1 < 0
    assert id2 < 0
    assert id2 > id1  # web ids increase over time (design section 2)

    async with sessionmaker() as session:
        row1 = await session.get(TelegramUpdate, id1)
        row2 = await session.get(TelegramUpdate, id2)
        web1 = await session.get(WebUpdate, id1)
        web2 = await session.get(WebUpdate, id2)
    assert row1 is not None
    assert web1.client_key == "a"
    assert row2 is not None
    assert web2.client_key == "b"


async def test_send_text_retry_with_same_client_key_is_idempotent(sessionmaker):
    async with sessionmaker() as session:
        first = await send_text(session, settings=_settings(), text="hi", client_key="dup")
    async with sessionmaker() as session:
        second = await send_text(session, settings=_settings(), text="hi", client_key="dup")

    assert first == second
    async with sessionmaker() as session:
        rows = (
            await session.execute(select(WebUpdate).where(WebUpdate.client_key == "dup"))
        ).scalars().all()
    assert len(rows) == 1
    # No wasted telegram_update row for the losing side of the race.
    async with sessionmaker() as session:
        tg_rows = (await session.execute(select(TelegramUpdate))).scalars().all()
    assert len(tg_rows) == 1


# --- press: allowlist + blocked callback prefix ---


async def test_press_rejects_delete_callback_prefix(sessionmaker):
    hub = WebHub()
    hub.register_keyboard(-1, [[{"text": "Да, удалить", "data": "d:yes:1"}]])
    async with sessionmaker() as session:
        with pytest.raises(BlockedCommand):
            await press(session, hub, settings=_settings(), message_id=-1, data="d:yes:1")


async def test_press_rejects_data_not_on_allowlist(sessionmaker):
    hub = WebHub()
    hub.register_keyboard(-1, [[{"text": "Я в порядке", "data": "w:resume"}]])
    async with sessionmaker() as session:
        with pytest.raises(PressRejected):
            await press(session, hub, settings=_settings(), message_id=-1, data="w:stay")
        # A different message_id entirely is just as rejected.
        with pytest.raises(PressRejected):
            await press(session, hub, settings=_settings(), message_id=-2, data="w:resume")


async def test_press_accepts_allowlisted_data_and_enqueues(sessionmaker):
    hub = WebHub()
    hub.register_keyboard(-1, [[{"text": "Я в порядке", "data": "w:resume"}]])
    async with sessionmaker() as session:
        update_id = await press(session, hub, settings=_settings(), message_id=-1, data="w:resume")
    assert update_id < 0

    async with sessionmaker() as session:
        row = await session.get(TelegramUpdate, update_id)
        web_row = await session.get(WebUpdate, update_id)
    assert row.update_id < 0
    assert web_row.client_key is None
    assert row.payload["callback_query"]["data"] == "w:resume"
    assert row.payload["callback_query"]["message"]["message_id"] == -1


async def test_press_carries_the_hub_recorded_text_into_the_synthetic_update(sessionmaker):
    hub = WebHub()
    hub.register_keyboard(-1, [[{"text": "Я в порядке", "data": "w:resume"}]])
    hub.register_message_text(-1, "как ты? Постоянно думаю о тебе.")

    async with sessionmaker() as session:
        update_id = await press(session, hub, settings=_settings(), message_id=-1, data="w:resume")

    async with sessionmaker() as session:
        row = await session.get(TelegramUpdate, update_id)
    assert row.payload["callback_query"]["message"]["text"] == "как ты? Постоянно думаю о тебе."


async def test_press_never_dedupes_across_calls(sessionmaker):
    """No client_key on /api/press (the HTTP contract carries none): two
    presses of the same button, even with identical data, are two rows."""
    hub = WebHub()
    hub.register_keyboard(-1, [[{"text": "Я в порядке", "data": "w:resume"}]])
    async with sessionmaker() as session:
        first = await press(session, hub, settings=_settings(), message_id=-1, data="w:resume")
        second = await press(session, hub, settings=_settings(), message_id=-1, data="w:resume")
    assert first != second


# --- W4: reserve_web_id and checkin_complete ---


async def test_reserve_web_id_is_negative_distinct_and_shares_the_update_id_space(sessionmaker):
    from app.db import queue

    async with sessionmaker() as session:
        first = await queue.reserve_web_id(session)
        second = await queue.reserve_web_id(session)
        update_id = await send_text(session, settings=_settings(), text="привет", client_key="k-r")
    assert first < 0 and second < 0
    assert len({first, second, update_id}) == 3
    assert first < second < update_id, "one increasing sequence behind all three"


async def test_checkin_complete_enqueues_the_web_submit_callback_for_the_given_message_id(sessionmaker):
    hub = WebHub()
    async with sessionmaker() as session:
        update_id = await checkin_complete(session, settings=_settings(), message_id=-12345)
        rows = list((await session.execute(select(TelegramUpdate))).scalars())
        web_rows = list((await session.execute(select(WebUpdate))).scalars())

    assert len(rows) == 1 and rows[0].update_id == update_id < 0
    assert web_rows[0].client_key is None
    update = Update.model_validate(rows[0].payload)
    assert update.callback_query.data == "c:n:web"
    assert update.callback_query.message.message_id == -12345
    assert update.callback_query.from_user.id == ALLOWED_CHAT_ID
    assert update.callback_query.message.chat.id == ALLOWED_CHAT_ID
    # Server-issued, so it needs (and has) no hub allowlist entry.
    assert hub.allow_press(-12345, "c:n:web") is False


async def test_checkin_complete_never_dedupes(sessionmaker):
    async with sessionmaker() as session:
        a = await checkin_complete(session, settings=_settings(), message_id=-1)
        b = await checkin_complete(session, settings=_settings(), message_id=-1)
    assert a != b
