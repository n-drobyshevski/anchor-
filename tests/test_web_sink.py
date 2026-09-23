"""app/web/sink.py tests (web-chat plan track 1).

- SendMessage, EditMessageText, EditMessageReplyMarkup and
  SendChatAction become hub events with negative ids
- the callback allowlist is replaced on edit and cleared when the
  markup is None
- unknown methods (SendDocument above all) raise NotImplementedError
- app/tg/send.py's edit_keyboard refuses a negative id against any bot
  that is not the web sink

Events are read back via `hub.subscribe(last_event_id=0)`, which replays
everything in the ring buffer -- the public API, rather than reaching
into WebHub's private buffer -- since every action here happens before
the assertions run.
"""

from __future__ import annotations

import pytest
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from app.tg.send import edit_keyboard
from app.web.hub import WebHub
from app.web.sink import make_web_bot
from conftest import make_bot

CHAT_ID = 111


def _replay(hub: WebHub) -> list:
    sub = hub.subscribe(last_event_id=0)
    sub.close()
    return sub.backlog


async def test_send_message_publishes_a_negative_id_message_event():
    hub = WebHub()
    bot = make_web_bot("123456:TEST", hub)

    message = await bot.send_message(CHAT_ID, "привет")

    assert message.message_id < 0
    events = _replay(hub)
    assert len(events) == 1
    event = events[0]
    assert event.event == "message"
    assert event.data["id"] == message.message_id
    assert event.data["role"] == "assistant"
    assert event.data["text"] == "привет"
    assert event.data["keyboard"] is None
    assert event.data["kind"] == "chat"

    await bot.session.close()


async def test_send_message_with_keyboard_registers_allowlist():
    hub = WebHub()
    bot = make_web_bot("123456:TEST", hub)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Я в порядке", callback_data="w:resume")],
            [InlineKeyboardButton(text="Остаюсь на паузе", callback_data="w:stay")],
        ]
    )

    message = await bot.send_message(CHAT_ID, "как ты?", reply_markup=markup)

    assert hub.allow_press(message.message_id, "w:resume") is True
    assert hub.allow_press(message.message_id, "w:stay") is True
    assert hub.allow_press(message.message_id, "w:bogus") is False

    event = _replay(hub)[0]
    assert event.data["kind"] == "welfare"  # inferred from the w: prefix
    assert event.data["keyboard"] == [
        [{"text": "Я в порядке", "data": "w:resume"}],
        [{"text": "Остаюсь на паузе", "data": "w:stay"}],
    ]

    await bot.session.close()


async def test_edit_message_text_publishes_an_edit_event_and_replaces_allowlist():
    hub = WebHub()
    bot = make_web_bot("123456:TEST", hub)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Да", callback_data="c:r:1")]]
    )
    sent = await bot.send_message(CHAT_ID, "исходный текст", reply_markup=markup)
    assert hub.allow_press(sent.message_id, "c:r:1") is True

    new_markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Ещё", callback_data="c:r:2")]]
    )
    await bot.edit_message_text(
        text="новый текст", chat_id=CHAT_ID, message_id=sent.message_id, reply_markup=new_markup
    )

    # Old data is no longer allowed; the edit replaced it.
    assert hub.allow_press(sent.message_id, "c:r:1") is False
    assert hub.allow_press(sent.message_id, "c:r:2") is True

    events = _replay(hub)
    assert [e.event for e in events] == ["message", "edit"]
    edit_event = events[-1]
    assert edit_event.data["id"] == sent.message_id
    assert edit_event.data["text"] == "новый текст"
    assert edit_event.data["keyboard"] == [[{"text": "Ещё", "data": "c:r:2"}]]

    await bot.session.close()


async def test_edit_message_reply_markup_none_clears_allowlist():
    hub = WebHub()
    bot = make_web_bot("123456:TEST", hub)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Да", callback_data="d:yes:1")]]
    )
    sent = await bot.send_message(CHAT_ID, "удалить всё?", reply_markup=markup)
    assert hub.allow_press(sent.message_id, "d:yes:1") is True

    await bot.edit_message_reply_markup(chat_id=CHAT_ID, message_id=sent.message_id, reply_markup=None)

    assert hub.allow_press(sent.message_id, "d:yes:1") is False
    events = _replay(hub)
    assert [e.event for e in events] == ["message", "edit"]
    edit_event = events[-1]
    assert edit_event.data["id"] == sent.message_id
    assert edit_event.data["keyboard"] is None
    assert "text" not in edit_event.data  # unchanged field is omitted, not null

    await bot.session.close()


async def test_edit_message_text_ignores_a_positive_message_id():
    """Medium-severity finding: WebSinkSession used to publish an edit
    (and touch the allowlist) for *any* message_id it was handed,
    including a positive one -- a real Telegram message_id, the shape
    app/tg/checkin.py's retire()/app/tg/proposals.py issue, reachable
    through the web sink whenever the *turn* that triggers the edit
    happens to have arrived over the web. The browser keys bubbles by
    DB message.id, an unrelated positive number that can coincide with
    this one by pure chance, so publishing would silently rewrite an
    unrelated bubble."""
    hub = WebHub()
    bot = make_web_bot("123456:TEST", hub)
    hub.register_keyboard(4812, [[{"text": "Да", "data": "c:r:1"}]])
    hub.register_message_text(4812, "какой-то несвязанный текст")

    result = await bot.edit_message_text(
        text="DONE", chat_id=CHAT_ID, message_id=4812
    )

    assert result.message_id == 4812
    assert result.text == "DONE"
    # Neither the allowlist nor the recorded text for this id moved.
    assert hub.allow_press(4812, "c:r:1") is True
    assert hub.text_for(4812) == "какой-то несвязанный текст"
    assert _replay(hub) == []  # no edit event published either

    await bot.session.close()


async def test_edit_message_reply_markup_ignores_a_positive_message_id():
    hub = WebHub()
    bot = make_web_bot("123456:TEST", hub)
    hub.register_keyboard(4812, [[{"text": "Да", "data": "c:r:1"}]])

    result = await bot.edit_message_reply_markup(chat_id=CHAT_ID, message_id=4812, reply_markup=None)

    assert result is True
    assert hub.allow_press(4812, "c:r:1") is True  # untouched
    assert _replay(hub) == []

    await bot.session.close()


async def test_send_message_and_edit_record_text_for_ingress_press():
    """Medium-severity finding: a synthetic callback_query's
    `message.text` used to be hardcoded "" -- app/tg/welfare.py's
    handle_callback appends its acknowledgement to that base text, so an
    empty base replaced the whole welfare reply. WebSinkSession now
    records the text alongside the keyboard so app/web/ingress.py's
    press() can look it up."""
    hub = WebHub()
    bot = make_web_bot("123456:TEST", hub)

    sent = await bot.send_message(CHAT_ID, "как ты?")
    assert hub.text_for(sent.message_id) == "как ты?"

    await bot.edit_message_text(text="как ты? (обновлено)", chat_id=CHAT_ID, message_id=sent.message_id)
    assert hub.text_for(sent.message_id) == "как ты? (обновлено)"

    # EditMessageReplyMarkup carries no text and must not blank it out.
    await bot.edit_message_reply_markup(chat_id=CHAT_ID, message_id=sent.message_id, reply_markup=None)
    assert hub.text_for(sent.message_id) == "как ты? (обновлено)"

    await bot.session.close()


async def test_send_chat_action_publishes_typing():
    hub = WebHub()
    bot = make_web_bot("123456:TEST", hub)

    await bot.send_chat_action(CHAT_ID, "typing")

    events = _replay(hub)
    assert len(events) == 1
    assert events[0].event == "typing"
    assert events[0].data == {}

    await bot.session.close()


async def test_answer_callback_query_with_text_publishes_toast():
    hub = WebHub()
    bot = make_web_bot("123456:TEST", hub)

    await bot.answer_callback_query("cb1", text="Устарело.")
    await bot.answer_callback_query("cb2")  # no text: no toast

    events = _replay(hub)
    assert len(events) == 1
    assert events[0].event == "toast"
    assert events[0].data == {"text": "Устарело."}

    await bot.session.close()


async def test_message_ids_do_not_collide_across_a_simulated_restart(monkeypatch):
    """High-severity finding: WebSinkSession used to start its id
    counter at a bare -1 on every process boot. An EventSource
    reconnects on its own after a redeploy, with no page reload and no
    client-side `rendered` map clear -- so ids reused across a restart
    collided with ids a tab had already rendered from the *previous*
    boot: the first reply after a restart was silently dropped as
    "already seen", and a later edit for the same reused id rewrote the
    wrong bubble. Seeding from a millisecond wall-clock timestamp
    instead means a later boot's ids never overlap an earlier boot's,
    as long as the wall clock does not run backwards across the
    restart."""
    from app.web import sink as sink_module

    monkeypatch.setattr(sink_module.time, "time_ns", lambda: 1_000_000_000_000)
    hub_a = WebHub()
    bot_a = make_web_bot("123456:TEST", hub_a)
    ids_a = [(await bot_a.send_message(CHAT_ID, f"boot A #{i}")).message_id for i in range(5)]
    await bot_a.session.close()

    # A later boot: strictly later wall-clock ms (by more than the
    # handful of ids boot A minted, so the ranges provably do not touch).
    monkeypatch.setattr(sink_module.time, "time_ns", lambda: 1_000_000_010_000_000)
    hub_b = WebHub()
    bot_b = make_web_bot("123456:TEST", hub_b)
    ids_b = [(await bot_b.send_message(CHAT_ID, f"boot B #{i}")).message_id for i in range(5)]
    await bot_b.session.close()

    assert all(a < 0 for a in ids_a) and all(b < 0 for b in ids_b)
    assert max(ids_b) < min(ids_a)  # boot B's ids are all lower (more negative) than boot A's


async def test_send_document_raises_not_implemented():
    """The sink's own defense against a bypassed /export: even if the
    router ever ran run_export against a web_bot, this is where it would
    fail loudly rather than silently succeed."""
    hub = WebHub()
    bot = make_web_bot("123456:TEST", hub)

    with pytest.raises(NotImplementedError):
        await bot.send_document(CHAT_ID, BufferedInputFile(b"data", filename="export.json"))

    await bot.session.close()


# --- app/tg/send.py's edit_keyboard negative-id guard ---


async def test_edit_keyboard_refuses_negative_id_against_the_real_bot():
    """The cross-transport fix: a real Bot must never be asked to edit a
    web-sink-issued (negative) message id."""
    bot, fake_session = make_bot()

    result = await edit_keyboard(bot, CHAT_ID, -42, "текст")

    assert result is False
    assert fake_session.edits == []  # no API call was made
    await bot.session.close()


async def test_edit_keyboard_allows_negative_id_against_the_web_sink():
    hub = WebHub()
    bot = make_web_bot("123456:TEST", hub)
    sent = await bot.send_message(CHAT_ID, "исходный текст")

    result = await edit_keyboard(bot, CHAT_ID, sent.message_id, "новый текст")

    assert result is True
    await bot.session.close()


async def test_edit_keyboard_still_works_for_a_positive_id():
    bot, fake_session = make_bot()

    result = await edit_keyboard(bot, CHAT_ID, 7, "текст")

    assert result is True
    assert len(fake_session.edits) == 1
    await bot.session.close()
