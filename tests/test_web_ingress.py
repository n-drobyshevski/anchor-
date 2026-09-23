"""app/web/ingress.py tests (web-chat plan track 1).

- the synthetic message Update payload validates as an aiogram Update
- ids are negative and increasing; the DB's source/sign CHECK holds
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
from app.db.models import TelegramUpdate
from app.web.hub import WebHub
from app.web.ingress import (
    BlockedCommand,
    PressRejected,
    build_callback_update,
    build_message_update,
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
    assert row1.source == "web"
    assert row1.client_key == "a"
    assert row2.source == "web"
    assert row2.client_key == "b"


async def test_send_text_retry_with_same_client_key_is_idempotent(sessionmaker):
    async with sessionmaker() as session:
        first = await send_text(session, settings=_settings(), text="hi", client_key="dup")
    async with sessionmaker() as session:
        second = await send_text(session, settings=_settings(), text="hi", client_key="dup")

    assert first == second
    async with sessionmaker() as session:
        rows = (
            await session.execute(select(TelegramUpdate).where(TelegramUpdate.client_key == "dup"))
        ).scalars().all()
    assert len(rows) == 1


async def test_source_sign_check_rejects_a_web_row_with_a_positive_id(sessionmaker):
    """The migration's CHECK ((source = 'web') = (update_id < 0)) is the
    backstop under this whole design -- prove it actually fires."""
    from sqlalchemy.exc import IntegrityError

    async with sessionmaker() as session:
        session.add(
            TelegramUpdate(update_id=999, payload={}, source="web", status="pending", attempts=0)
        )
        with pytest.raises(IntegrityError):
            await session.commit()


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
    assert row.source == "web"
    assert row.client_key is None
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
