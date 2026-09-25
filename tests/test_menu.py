"""Button menus (this milestone's spec, 2026-09-25): `/menu`'s inline hub,
`/start`'s persistent `☰ Меню` button, and the `mn:` callbacks.

`app/tg/menu.py` is pure -- no DB, no I/O -- so most of it is tested
directly, the same way tests/test_quiet_tz.py tests app/core/quiet.py's
parser before ever touching the router. The router-level tests below
follow that file's own Dispatcher pattern (also tests/test_interests_
commands.py's `_callback_update`).
"""

from __future__ import annotations

import datetime
import itertools

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from sqlalchemy import select

from app.config import Settings
from app.db.models import Message, TelegramUpdate, UserState
from app.tg import menu
from app.tg.router import BOT_COMMANDS, HIDE_KB_REPLY, START_TEXT, build_router
from conftest import FakeLLMProvider, FakeSession

CHAT_ID = 555
TIMEZONE = "Europe/Paris"

ALL_SECTIONS = ("main", "mem", "deals", "quiet", "mode", "data")

# Never emitted, forged or not (menu.ACTIONS' own docstring gives the
# reasoning for each).
EXCLUDED_ACTIONS = ("export", "delete", "grok", "planner_link", "due", "remember", "forget", "tz")


def _flag_combinations():
    """Every combination of the settings that gate a menu entry, plus web."""
    return itertools.product(
        (False, True),  # PLANNER_ENABLED
        (False, True),  # RESEARCH_ENABLED
        ("off", "status", "mirror", "sync"),  # VAULT_MODE
        (False, True),  # CLAUDE_ACCESS_ENABLED
        (False, True),  # web
    )


def _settings(planner: bool, research: bool, vault_mode: str, claude: bool) -> Settings:
    return Settings(
        _env_file=None,
        PLANNER_ENABLED=planner,
        RESEARCH_ENABLED=research,
        VAULT_MODE=vault_mode,
        VAULT_API_TOKEN="x" * 32,
        CLAUDE_ACCESS_ENABLED=claude,
    )


def _buttons(markup: InlineKeyboardMarkup) -> list:
    return [button for row in markup.inline_keyboard for button in row]


def _action_buttons(markup: InlineKeyboardMarkup) -> list:
    return [b for b in _buttons(markup) if b.callback_data.startswith(menu.ACTION_PREFIX)]


# --- pure: render / action_available ------------------------------------


def test_every_section_renders():
    settings = Settings(_env_file=None)
    for section in ALL_SECTIONS:
        for web in (False, True):
            rendered = menu.render(section, settings, web=web)
            assert rendered is not None
            text, markup = rendered
            assert text
            assert isinstance(markup, InlineKeyboardMarkup)
            assert markup.inline_keyboard


def test_unknown_section_is_none():
    assert menu.render("nope", Settings(_env_file=None), web=False) is None
    assert menu.render("", Settings(_env_file=None), web=True) is None


def test_every_callback_data_is_well_formed():
    for planner, research, vault_mode, claude, web in _flag_combinations():
        settings = _settings(planner, research, vault_mode, claude)
        for section in ALL_SECTIONS:
            _, markup = menu.render(section, settings, web=web)
            for button in _buttons(markup):
                data = button.callback_data
                assert data.startswith("mn:")
                assert len(data.encode("utf-8")) <= 64
    # The close button too, and the longest single action key.
    assert menu.CLOSE_CALLBACK.startswith("mn:")
    assert len(f"{menu.ACTION_PREFIX}quiet_30m".encode("utf-8")) <= 64


def test_plan_hidden_when_planner_off_shown_when_on():
    off = _settings(False, False, "off", False)
    on = _settings(True, False, "off", False)
    _, main_off = menu.render("main", off, web=False)
    _, main_on = menu.render("main", on, web=False)
    assert "plan" not in [b.callback_data for b in _action_buttons(main_off)]
    assert f"{menu.ACTION_PREFIX}plan" in [b.callback_data for b in _action_buttons(main_on)]


def test_notes_hidden_when_research_off_shown_when_on():
    off = _settings(False, False, "off", False)
    on = _settings(False, True, "off", False)
    _, mem_off = menu.render("mem", off, web=False)
    _, mem_on = menu.render("mem", on, web=False)
    assert f"{menu.ACTION_PREFIX}notes" not in [b.callback_data for b in _action_buttons(mem_off)]
    assert f"{menu.ACTION_PREFIX}notes" in [b.callback_data for b in _action_buttons(mem_on)]


def test_vault_hidden_when_mode_off():
    off = _settings(False, False, "off", False)
    on = _settings(False, False, "status", False)
    _, data_off = menu.render("data", off, web=False)
    _, data_on = menu.render("data", on, web=False)
    assert f"{menu.ACTION_PREFIX}vault" not in [b.callback_data for b in _action_buttons(data_off)]
    assert f"{menu.ACTION_PREFIX}vault" in [b.callback_data for b in _action_buttons(data_on)]


def test_claude_hidden_when_flag_off_or_web():
    on = _settings(False, False, "off", True)
    off = _settings(False, False, "off", False)
    _, data_flag_off = menu.render("data", off, web=False)
    _, data_flag_on_tg = menu.render("data", on, web=False)
    _, data_flag_on_web = menu.render("data", on, web=True)
    keys = lambda m: [b.callback_data for b in _action_buttons(m)]  # noqa: E731
    assert f"{menu.ACTION_PREFIX}claude" not in keys(data_flag_off)
    assert f"{menu.ACTION_PREFIX}claude" in keys(data_flag_on_tg)
    assert f"{menu.ACTION_PREFIX}claude" not in keys(data_flag_on_web)


def test_hide_kb_hidden_on_web():
    settings = Settings(_env_file=None)
    _, data_tg = menu.render("data", settings, web=False)
    _, data_web = menu.render("data", settings, web=True)
    keys = lambda m: [b.callback_data for b in _action_buttons(m)]  # noqa: E731
    assert f"{menu.ACTION_PREFIX}hide_kb" in keys(data_tg)
    assert f"{menu.ACTION_PREFIX}hide_kb" not in keys(data_web)


@pytest.mark.parametrize("action", EXCLUDED_ACTIONS + ("nonsense", "", "quiet"))
def test_action_available_is_false_for_excluded_and_garbage(action):
    settings = Settings(_env_file=None)
    assert menu.action_available(action, settings, web=False) is False
    assert menu.action_available(action, settings, web=True) is False


def test_every_rendered_action_button_satisfies_action_available():
    for planner, research, vault_mode, claude, web in _flag_combinations():
        settings = _settings(planner, research, vault_mode, claude)
        for section in ALL_SECTIONS:
            _, markup = menu.render(section, settings, web=web)
            for button in _action_buttons(markup):
                _, action = menu.parse_callback(button.callback_data)
                assert menu.action_available(action, settings, web=web), (
                    f"{section}/{action} rendered for web={web} but action_available said no"
                )


def test_parse_callback():
    assert menu.parse_callback("mn:x") == ("close", "")
    assert menu.parse_callback("mn:s:quiet") == ("section", "quiet")
    assert menu.parse_callback("mn:a:checkin") == ("action", "checkin")
    assert menu.parse_callback("m:k:identity:1") is None
    assert menu.parse_callback("garbage") is None


def test_reply_keyboard_has_one_persistent_button():
    kb = menu.reply_keyboard()
    assert isinstance(kb, ReplyKeyboardMarkup)
    assert kb.is_persistent is True
    assert kb.resize_keyboard is True
    assert [b.text for row in kb.keyboard for b in row] == [menu.MENU_BUTTON_TEXT]


def test_menu_is_registered_right_after_start():
    commands = [c.command for c in BOT_COMMANDS]
    assert commands.index("menu") == commands.index("start") + 1


# --- router-level ---------------------------------------------------------


def _command_update(update_id: int, text: str) -> dict:
    entities = []
    if text.startswith("/"):
        entities = [{"type": "bot_command", "offset": 0, "length": len(text.split(" ", 1)[0])}]
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": CHAT_ID, "type": "private"},
            "from": {"id": CHAT_ID, "is_bot": False, "first_name": "Test"},
            "text": text,
            "entities": entities,
        },
    }


def _callback_update(update_id: int, data: str, *, message_id: int) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "from": {"id": CHAT_ID, "is_bot": False, "first_name": "Test"},
            "chat_instance": "ci",
            "data": data,
            "message": {
                "message_id": message_id,
                "date": 0,
                "chat": {"id": CHAT_ID, "type": "private"},
                "text": "…",
            },
        },
    }


def _build(sessionmaker, settings=None, llm=None):
    fake = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake)
    dp = Dispatcher()
    dp.include_router(
        build_router(
            sessionmaker,
            settings or Settings(_env_file=None, TZ_DEFAULT=TIMEZONE),
            llm or FakeLLMProvider(),
        )
    )
    return dp, bot, fake


async def _seed(sessionmaker, *update_ids: int, **state) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone=TIMEZONE, **state))
        await session.commit()
        for update_id in update_ids:
            session.add(TelegramUpdate(update_id=update_id, payload={}))
        await session.commit()


async def _feed(dp, bot, payload: dict) -> None:
    await dp.feed_update(bot, Update.model_validate(payload, context={"bot": bot}))


async def _state(sessionmaker) -> UserState:
    async with sessionmaker() as session:
        return await session.get(UserState, 1)


async def test_start_reply_carries_the_menu_button(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/start"))

    assert fake.sent[0].text == START_TEXT
    markup = fake.sent[0].reply_markup
    assert isinstance(markup, ReplyKeyboardMarkup)
    assert [b.text for row in markup.keyboard for b in row] == [menu.MENU_BUTTON_TEXT]


async def test_menu_command_sends_one_message_with_an_inline_keyboard(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/menu"))

    assert len(fake.sent) == 1
    assert isinstance(fake.sent[0].reply_markup, InlineKeyboardMarkup)


async def test_menu_button_text_opens_the_menu_with_no_llm_call_and_no_stored_chat_message(
    sessionmaker,
):
    await _seed(sessionmaker, 1)
    llm = FakeLLMProvider()
    dp, bot, fake = _build(sessionmaker, llm=llm)

    await _feed(dp, bot, _command_update(1, menu.MENU_BUTTON_TEXT))

    assert len(fake.sent) == 1
    assert isinstance(fake.sent[0].reply_markup, InlineKeyboardMarkup)
    assert llm.calls == 0
    async with sessionmaker() as session:
        rows = list((await session.execute(select(Message))).scalars())
    assert not any(row.content == menu.MENU_BUTTON_TEXT for row in rows)


async def test_menu_button_clears_a_pending_checkin_awaiting_step(sessionmaker):
    from app.core import checkin as checkin_core

    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        await checkin_core.set_awaiting_note(session, 1)
    dp, bot, _ = _build(sessionmaker)

    await _feed(dp, bot, _command_update(1, menu.MENU_BUTTON_TEXT))

    state = await _state(sessionmaker)
    assert state.awaiting is None


async def test_section_callback_edits_the_message_in_place(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/menu"))
    # FakeSession mints message_id 1 for the /menu message just sent.

    await _feed(dp, bot, _callback_update(2, "mn:s:quiet", message_id=1))

    assert len(fake.edits) == 1
    assert fake.edits[0].text == menu.QUIET_TEXT
    assert len(fake.answered) == 1


async def test_unknown_section_answers_stale_and_edits_nothing(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:s:nope", message_id=1))

    assert fake.edits == []
    assert len(fake.answered) == 1
    assert fake.answered[0].text is not None


async def test_close_edits_to_the_closed_text_with_no_keyboard(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:x", message_id=1))

    assert fake.edits[-1].text == menu.CLOSED_TEXT
    assert fake.edits[-1].reply_markup is None


async def test_quiet_action_sets_quiet_until_two_hours_ahead(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:a:quiet_2h", message_id=1))

    state = await _state(sessionmaker)
    assert state.quiet_until is not None
    delta = state.quiet_until - datetime.datetime.now(datetime.timezone.utc)
    assert datetime.timedelta(hours=1, minutes=55) < delta < datetime.timedelta(hours=2, minutes=5)


async def test_quiet_off_action_clears_quiet_until(sessionmaker):
    await _seed(
        sessionmaker,
        1,
        2,
        quiet_until=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5),
    )
    dp, bot, fake = _build(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:a:quiet_off", message_id=1))

    state = await _state(sessionmaker)
    assert state.quiet_until is None


async def test_focus_on_and_off_flip_the_flag(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3)
    dp, bot, fake = _build(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:a:focus_on", message_id=1))
    assert (await _state(sessionmaker)).focus_on is True

    await _feed(dp, bot, _callback_update(3, "mn:a:focus_off", message_id=1))
    assert (await _state(sessionmaker)).focus_on is False


async def test_forged_export_and_delete_actions_do_nothing_but_answer(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3)
    dp, bot, fake = _build(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/menu"))
    sent_before = len(fake.sent)

    await _feed(dp, bot, _callback_update(2, "mn:a:export", message_id=1))
    await _feed(dp, bot, _callback_update(3, "mn:a:delete", message_id=1))

    assert fake.documents == []
    assert len(fake.sent) == sent_before
    assert len(fake.answered) == 2


async def test_hide_kb_action_sends_the_reply_keyboard_remove(sessionmaker):
    from aiogram.types import ReplyKeyboardRemove

    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:a:hide_kb", message_id=1))

    assert fake.sent[-1].text == HIDE_KB_REPLY
    assert isinstance(fake.sent[-1].reply_markup, ReplyKeyboardRemove)


async def test_state_action_sends_the_state_message(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:a:state", message_id=1))

    assert any("Персона:" in m.text for m in fake.sent)
