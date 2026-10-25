"""The /menu flows end to end through the router (menu redesign, 2026-10-25):
prompts («✏️ Запомнить» -> a question -> the next message is the command's
argument), confirmations («⚠️ Выгрузить всё» -> «Точно?» -> «Да»), and the
buttons that lead into other messages' buttons.

What the pure side of the menu guarantees is in tests/test_menu.py; this
file drives `build_router` with real updates, against the throwaway
database, and reuses that file's fixtures. Each test names the rule it
pins:

- a prompt's reply runs the command and is never chat: no user `message`
  row, no model call, the prompt edited to «Ответ принят.»;
- a prompt ends on ✕ Отмена, on expiry (MENU_PROMPT_TTL_MIN), on a slash
  command, on a menu tap and on a pause word, after which text is chat;
- a replayed reply runs once;
- «Да» runs, «Нет» does not, and the card is one-shot: a second press
  (a double tap, two updates with two update ids) answers «Устарело.»;
- the sensitive acts work by button on Telegram and are inert when forged
  from the web.
"""

from __future__ import annotations

import asyncio
import datetime

import pytest
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select, text

from app.config import Settings
from app.core import menu_prompt
from app.core import orders as orders_core
from app.core import turn
from app.core.clock import FrozenClock
from app.db.models import (
    InterestTopic,
    Job,
    Memory,
    Message,
    OauthConnection,
    StandingOrder,
    StudyCard,
    StudyClip,
    StudyJob,
)
from app.tg import claude as claude_ui
from app.tg import data as data_ui
from app.tg import interests as interests_ui
from app.tg import lens as lens_ui
from app.tg import memory as memory_ui
from app.tg import menu
from app.tg import research as research_ui
from app.tg.router import DUE_CLEARED, FOCUS_ON, WEB_ONLY_REPLY
from app.vault import lens
from conftest import FakeLLMProvider, flatten_rich_message, make_bot, rich_callback_data
from test_menu import (
    CHAT_ID,
    TIMEZONE,
    _build,
    _callback_update,
    _command_update,
    _feed,
    _seed,
    _state,
)

NOW = datetime.datetime(2026, 10, 25, 10, 0, tzinfo=datetime.timezone.utc)
MENU_MESSAGE = 1  # FakeSession mints message id 1 for the /menu message


# --- helpers ----------------------------------------------------------------


def _settings(**extra) -> Settings:
    values = dict(_env_file=None, TZ_DEFAULT=TIMEZONE)
    values.update(extra)
    return Settings(**values)


def _last_id(fake) -> int:
    """The id of the newest message the fake Telegram minted."""
    return fake._next_message_id - 1


def _edits_of(fake, message_id: int) -> list:
    return [e for e in fake.edits if e.message_id == message_id]


def _texts(fake) -> list[str]:
    return [m.text for m in fake.sent]


async def _users(sessionmaker) -> list[Message]:
    async with sessionmaker() as session:
        result = await session.execute(select(Message).where(Message.role == "user"))
        return list(result.scalars())


async def _tap(dp, bot, update_id: int, data: str, *, message_id: int = MENU_MESSAGE) -> None:
    await _feed(dp, bot, _callback_update(update_id, data, message_id=message_id))


async def _say(dp, bot, update_id: int, line: str) -> None:
    """A plain message (or, starting with '/', a command)."""
    await _feed(dp, bot, _command_update(update_id, line))


async def _open_prompt(dp, bot, fake, update_id: int, key: str) -> int:
    """Open the menu and tap the prompt button `key`; returns the prompt
    message's id."""
    await _say(dp, bot, update_id, "/menu")
    await _tap(dp, bot, update_id + 1, f"mn:i:{key}")
    assert fake.sent[-1].text == menu.prompt_text(key)
    return _last_id(fake)


# --- prompt -> reply ------------------------------------------------------------


async def test_prompt_opens_a_step_with_a_cancel_button_and_a_clock(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build(sessionmaker)

    prompt_id = await _open_prompt(dp, bot, fake, 1, "tz")

    state = await _state(sessionmaker)
    assert state.awaiting == "menu:tz"
    assert state.awaiting_ref == prompt_id
    assert state.awaiting_at is not None
    markup = fake.sent[-1].reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    assert [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row] == [
        ("✕ Отмена", "mn:c")
    ]


async def test_prompt_reply_runs_the_command_with_no_user_row_and_no_model_call(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3)
    llm = FakeLLMProvider()
    dp, bot, fake = _build(sessionmaker, llm=llm)
    prompt_id = await _open_prompt(dp, bot, fake, 1, "tz")

    await _say(dp, bot, 3, "Europe/Berlin")

    state = await _state(sessionmaker)
    assert state.timezone == "Europe/Berlin"
    assert fake.sent[-1].text.startswith("Часовой пояс: Europe/Berlin.")
    assert llm.calls == 0
    assert await _users(sessionmaker) == []
    # The prompt is edited to «Ответ принят.» with no buttons, and the step is over.
    last = _edits_of(fake, prompt_id)[-1]
    assert last.text == menu.PROMPT_ANSWERED_TEXT
    assert last.reply_markup is None
    assert (state.awaiting, state.awaiting_ref, state.awaiting_at) == (None, None, None)


async def test_prompt_reply_for_remember_leads_to_the_kind_picker(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3)
    llm = FakeLLMProvider()
    dp, bot, fake = _build(sessionmaker, llm=llm)
    await _open_prompt(dp, bot, fake, 1, "remember")

    await _say(dp, bot, 3, "не пью кофе после 16")

    assert fake.sent[-1].text == memory_ui.REMEMBER_PROMPT.format(text="не пью кофе после 16")
    assert fake.sent[-1].reply_markup is not None  # the kind buttons
    assert llm.calls == 0
    assert await _users(sessionmaker) == []


async def test_prompt_reply_runs_exactly_the_typed_command_for_a_set_main_action(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3)
    dp, bot, fake = _build(sessionmaker)
    await _open_prompt(dp, bot, fake, 1, "due_set")

    await _say(dp, bot, 3, "дописать отчёт")

    assert (await _state(sessionmaker)).due_action == "дописать отчёт"


async def test_prompt_reply_is_stored_as_a_command_placeholder_never_as_the_users_text(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3)
    dp, bot, fake = _build(sessionmaker)
    await _open_prompt(dp, bot, fake, 1, "mind_add")

    await _say(dp, bot, 3, "заметка про сон")

    async with sessionmaker() as session:
        rows = list((await session.execute(select(Message).where(Message.update_id == 3))).scalars())
    assert rows and all(row.role == "assistant" for row in rows)


async def test_a_text_with_no_prompt_pending_is_ordinary_chat(sessionmaker):
    await _seed(sessionmaker, 1)
    llm = FakeLLMProvider()
    dp, bot, fake = _build(sessionmaker, llm=llm)

    await _say(dp, bot, 1, "Europe/Berlin")

    assert llm.calls == 1
    assert (await _state(sessionmaker)).timezone == TIMEZONE
    assert [m.content for m in await _users(sessionmaker)] == ["Europe/Berlin"]


# --- what ends a prompt ------------------------------------------------------------


async def test_cancel_ends_the_prompt_and_the_next_text_is_chat(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3, 4)
    llm = FakeLLMProvider()
    dp, bot, fake = _build(sessionmaker, llm=llm)
    prompt_id = await _open_prompt(dp, bot, fake, 1, "tz")

    await _tap(dp, bot, 3, "mn:c", message_id=prompt_id)

    assert _edits_of(fake, prompt_id)[-1].text == menu.CANCELLED_TEXT
    assert (await _state(sessionmaker)).awaiting is None
    await _say(dp, bot, 4, "Europe/Berlin")
    assert (await _state(sessionmaker)).timezone == TIMEZONE
    assert llm.calls == 1


async def test_a_prompt_expires_after_the_ttl_and_the_text_is_chat_again(sessionmaker):
    clock = FrozenClock(NOW)
    await _seed(sessionmaker, 1, 2, 3)
    llm = FakeLLMProvider()
    dp, bot, fake = _build(sessionmaker, settings=_settings(MENU_PROMPT_TTL_MIN=10), llm=llm, clock=clock)
    prompt_id = await _open_prompt(dp, bot, fake, 1, "tz")

    clock.advance(datetime.timedelta(minutes=10))
    await _say(dp, bot, 3, "Europe/Berlin")

    assert (await _state(sessionmaker)).timezone == TIMEZONE
    assert llm.calls == 1
    assert [m.content for m in await _users(sessionmaker)] == ["Europe/Berlin"]
    assert _edits_of(fake, prompt_id)[-1].text == menu.CANCELLED_TEXT
    state = await _state(sessionmaker)
    assert (state.awaiting, state.awaiting_at) == (None, None)


async def test_a_prompt_answered_just_inside_the_ttl_still_runs(sessionmaker):
    clock = FrozenClock(NOW)
    await _seed(sessionmaker, 1, 2, 3)
    llm = FakeLLMProvider()
    dp, bot, fake = _build(sessionmaker, settings=_settings(MENU_PROMPT_TTL_MIN=10), llm=llm, clock=clock)
    await _open_prompt(dp, bot, fake, 1, "tz")

    clock.advance(datetime.timedelta(minutes=9, seconds=59))
    await _say(dp, bot, 3, "Europe/Berlin")

    assert (await _state(sessionmaker)).timezone == "Europe/Berlin"
    assert llm.calls == 0


async def test_the_ttl_is_a_setting(sessionmaker):
    clock = FrozenClock(NOW)
    await _seed(sessionmaker, 1, 2, 3)
    llm = FakeLLMProvider()
    dp, bot, fake = _build(sessionmaker, settings=_settings(MENU_PROMPT_TTL_MIN=2), llm=llm, clock=clock)
    await _open_prompt(dp, bot, fake, 1, "tz")

    clock.advance(datetime.timedelta(minutes=3))
    await _say(dp, bot, 3, "Europe/Berlin")

    assert llm.calls == 1
    assert (await _state(sessionmaker)).timezone == TIMEZONE


async def test_a_slash_command_ends_the_prompt(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3, 4)
    llm = FakeLLMProvider()
    dp, bot, fake = _build(sessionmaker, llm=llm)
    prompt_id = await _open_prompt(dp, bot, fake, 1, "tz")

    await _say(dp, bot, 3, "/focus on")

    assert (await _state(sessionmaker)).focus_on is True
    assert fake.sent[-1].text == FOCUS_ON
    assert _edits_of(fake, prompt_id)[-1].text == menu.CANCELLED_TEXT
    assert (await _state(sessionmaker)).awaiting is None
    await _say(dp, bot, 4, "Europe/Berlin")
    assert (await _state(sessionmaker)).timezone == TIMEZONE
    assert llm.calls == 1


async def test_a_menu_tap_ends_the_prompt(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3, 4)
    llm = FakeLLMProvider()
    dp, bot, fake = _build(sessionmaker, llm=llm)
    prompt_id = await _open_prompt(dp, bot, fake, 1, "tz")

    await _tap(dp, bot, 3, "mn:s:quiet")

    assert _edits_of(fake, prompt_id)[-1].text == menu.CANCELLED_TEXT
    assert (await _state(sessionmaker)).awaiting is None
    await _say(dp, bot, 4, "Europe/Berlin")
    assert llm.calls == 1
    assert (await _state(sessionmaker)).timezone == TIMEZONE


async def test_a_new_prompt_replaces_the_old_one(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3, 4)
    dp, bot, fake = _build(sessionmaker)
    first = await _open_prompt(dp, bot, fake, 1, "tz")

    await _tap(dp, bot, 3, "mn:i:remember")

    assert _edits_of(fake, first)[-1].text == menu.CANCELLED_TEXT
    state = await _state(sessionmaker)
    assert state.awaiting == "menu:remember"
    assert state.awaiting_ref == _last_id(fake) != first
    await _say(dp, bot, 4, "не пью кофе")
    assert fake.sent[-1].text == memory_ui.REMEMBER_PROMPT.format(text="не пью кофе")


async def test_the_menu_button_ends_the_prompt(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3)
    dp, bot, fake = _build(sessionmaker)
    prompt_id = await _open_prompt(dp, bot, fake, 1, "tz")

    await _say(dp, bot, 3, menu.MENU_BUTTON_TEXT)

    assert _edits_of(fake, prompt_id)[-1].text == menu.CANCELLED_TEXT
    assert (await _state(sessionmaker)).awaiting is None
    assert (await _state(sessionmaker)).timezone == TIMEZONE


async def test_a_pause_word_ends_the_prompt_and_pauses(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3)
    dp, bot, fake = _build(sessionmaker)
    prompt_id = await _open_prompt(dp, bot, fake, 1, "tz")

    await _say(dp, bot, 3, "красный")

    state = await _state(sessionmaker)
    assert state.persona_active is False
    assert state.timezone == TIMEZONE
    assert (state.awaiting, state.awaiting_ref, state.awaiting_at) == (None, None, None)
    assert _edits_of(fake, prompt_id)[-1].text == menu.CANCELLED_TEXT


async def test_a_checkin_note_step_is_not_mistaken_for_a_menu_prompt(sessionmaker):
    from app.core import checkin as checkin_core

    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        await checkin_core.set_awaiting_note(session, 1)
    llm = FakeLLMProvider()
    dp, bot, fake = _build(sessionmaker, llm=llm)

    await _say(dp, bot, 1, "Europe/Berlin")

    assert (await _state(sessionmaker)).timezone == TIMEZONE
    assert fake.edits == []


# --- replay -----------------------------------------------------------------------


async def test_a_replayed_reply_runs_once(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3)
    llm = FakeLLMProvider()
    dp, bot, fake = _build(sessionmaker, llm=llm)
    await _open_prompt(dp, bot, fake, 1, "order_daily")

    await _say(dp, bot, 3, "проверять почту")
    sent = len(fake.sent)
    # The worker replays an update after a crash before complete().
    await _say(dp, bot, 3, "проверять почту")

    async with sessionmaker() as session:
        orders = list((await session.execute(select(StandingOrder))).scalars())
    assert len(orders) == 1
    assert len(fake.sent) == sent
    assert llm.calls == 0
    assert await _users(sessionmaker) == []


async def test_a_replayed_prompt_tap_asks_once(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build(sessionmaker)
    await _say(dp, bot, 1, "/menu")

    await _tap(dp, bot, 2, "mn:i:tz")
    await _tap(dp, bot, 2, "mn:i:tz")

    assert _texts(fake).count(menu.prompt_text("tz")) == 1


async def test_a_replayed_reply_after_the_step_was_cleared_is_not_chat(sessionmaker):
    """The recovery path: the reply ran, the step was cleared, and the
    update comes round again -- it must not fall through into a chat
    turn now that nothing is pending (turn.answered_without_turn)."""
    await _seed(sessionmaker, 1, 2, 3)
    llm = FakeLLMProvider()
    dp, bot, fake = _build(sessionmaker, llm=llm)
    await _open_prompt(dp, bot, fake, 1, "remember")
    await _say(dp, bot, 3, "не пью кофе")
    assert (await _state(sessionmaker)).awaiting is None

    await _say(dp, bot, 3, "не пью кофе")

    assert llm.calls == 0
    assert await _users(sessionmaker) == []
    assert _texts(fake).count(memory_ui.REMEMBER_PROMPT.format(text="не пью кофе")) == 1


# --- confirmation: Да runs, Нет does not ----------------------------------------------


async def _open_card(dp, bot, fake, update_id: int, key: str) -> int:
    """Open the menu, tap the confirm button `key`; returns the card's id."""
    await _say(dp, bot, update_id, "/menu")
    await _tap(dp, bot, update_id + 1, f"mn:a:{key}")
    assert fake.sent[-1].text == menu.confirm_text(key)
    return _last_id(fake)


async def test_confirm_card_asks_first_and_changes_nothing(sessionmaker):
    await _seed(sessionmaker, 1, 2, due_action="дописать отчёт")
    dp, bot, fake = _build(sessionmaker)

    await _open_card(dp, bot, fake, 1, "due_clear")

    markup = fake.sent[-1].reply_markup
    assert [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row] == [
        ("Да, снять", "mn:y:due_clear"),
        ("Нет", "mn:c"),
    ]
    assert (await _state(sessionmaker)).due_action == "дописать отчёт"


async def test_yes_runs_the_command_and_retires_the_card(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3, due_action="дописать отчёт")
    dp, bot, fake = _build(sessionmaker)
    card = await _open_card(dp, bot, fake, 1, "due_clear")

    await _tap(dp, bot, 3, "mn:y:due_clear", message_id=card)

    assert (await _state(sessionmaker)).due_action is None
    assert fake.sent[-1].text == DUE_CLEARED
    last = _edits_of(fake, card)[-1]
    assert (last.text, last.reply_markup) == (menu.DONE_TEXT, None)


async def test_no_does_not_run_the_command(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3, due_action="дописать отчёт")
    dp, bot, fake = _build(sessionmaker)
    card = await _open_card(dp, bot, fake, 1, "due_clear")
    sent = len(fake.sent)

    await _tap(dp, bot, 3, "mn:c", message_id=card)

    assert (await _state(sessionmaker)).due_action == "дописать отчёт"
    assert len(fake.sent) == sent
    assert _edits_of(fake, card)[-1].text == menu.CANCELLED_TEXT


async def test_yes_after_no_is_stale(sessionmaker):
    """«Нет» claims the card too: a «Да» already in flight cannot run
    after the user said no."""
    await _seed(sessionmaker, 1, 2, 3, 4, due_action="дописать отчёт")
    dp, bot, fake = _build(sessionmaker)
    card = await _open_card(dp, bot, fake, 1, "due_clear")
    await _tap(dp, bot, 3, "mn:c", message_id=card)

    await _tap(dp, bot, 4, "mn:y:due_clear", message_id=card)

    assert (await _state(sessionmaker)).due_action == "дописать отчёт"
    assert fake.answered[-1].text == "Устарело."


async def test_a_stale_confirm_card_after_the_flag_went_off_does_nothing(sessionmaker):
    """The card was drawn under a deploy that had the button; «Да» is
    checked against action_available again."""
    await _seed(sessionmaker, 1, 2, 3, due_action="x")
    dp, bot, fake = _build(sessionmaker, settings=_settings(PLANNER_ENABLED=True))
    await _say(dp, bot, 1, "/menu")
    await _tap(dp, bot, 2, "mn:a:planner_link")
    card = _last_id(fake)
    dp2, bot2, fake2 = _build(sessionmaker, settings=_settings(PLANNER_ENABLED=False))

    await _tap(dp2, bot2, 3, "mn:y:planner_link", message_id=card)

    assert fake2.sent == []
    assert fake2.answered[-1].text == "Устарело."


# --- one-shot: a double tap on «Да» ----------------------------------------------------


async def test_double_tapped_yes_runs_the_command_once(sessionmaker):
    """Two presses on the same card before the first is processed arrive
    as two updates with two update ids; only the first may run (two
    export files was the defect)."""
    await _seed(sessionmaker, 1, 2, 3, 4)
    dp, bot, fake = _build(sessionmaker)
    card = await _open_card(dp, bot, fake, 1, "export")

    await _tap(dp, bot, 3, "mn:y:export", message_id=card)
    await _tap(dp, bot, 4, "mn:y:export", message_id=card)

    assert len(fake.documents) == 1
    assert fake.answered[-1].text == "Устарело."
    assert [a.text for a in fake.answered].count("Устарело.") == 1


async def test_concurrent_yes_presses_run_the_command_once(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3, 4, 5)
    dp, bot, fake = _build(sessionmaker)
    card = await _open_card(dp, bot, fake, 1, "export")

    await asyncio.gather(
        _tap(dp, bot, 3, "mn:y:export", message_id=card),
        _tap(dp, bot, 4, "mn:y:export", message_id=card),
        _tap(dp, bot, 5, "mn:y:export", message_id=card),
    )

    assert len(fake.documents) == 1
    assert [a.text for a in fake.answered].count("Устарело.") == 2


async def test_a_replayed_yes_is_the_same_press_and_still_runs_once(sessionmaker):
    """The queue re-runs an update after a crash: the same update id is
    the same press (it may have died before the command ran), and the
    command's own replay gate keeps it to one file."""
    await _seed(sessionmaker, 1, 2, 3)
    dp, bot, fake = _build(sessionmaker)
    card = await _open_card(dp, bot, fake, 1, "export")

    await _tap(dp, bot, 3, "mn:y:export", message_id=card)
    await _tap(dp, bot, 3, "mn:y:export", message_id=card)

    assert len(fake.documents) == 1
    assert "Устарело." not in [a.text for a in fake.answered]


async def test_a_different_card_is_a_different_claim(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3, 4, 5, 6)
    dp, bot, fake = _build(sessionmaker)
    first = await _open_card(dp, bot, fake, 1, "export")
    second = await _open_card(dp, bot, fake, 3, "export")
    assert first != second

    await _tap(dp, bot, 5, "mn:y:export", message_id=first)
    await _tap(dp, bot, 6, "mn:y:export", message_id=second)

    assert len(fake.documents) == 2


async def test_claim_card_is_atomic_and_keyed_on_the_card(sessionmaker):
    async def claim(message_id, update_id):
        async with sessionmaker() as session:
            return await menu_prompt.claim_card(session, message_id, update_id)

    assert await claim(7, 100) is True
    assert await claim(7, 101) is False  # another press on the card
    assert await claim(7, 100) is True  # the same press, replayed
    assert await claim(8, 101) is True  # another card
    assert await claim(-7, 102) is True  # a web card (negative id) is another card

    results = await asyncio.gather(*(claim(9, 200 + i) for i in range(8)))
    assert results.count(True) == 1

    # Content-free: a kind, a key made of the card's message id only (no
    # chat id: debug.job shows dedup_key), and an update id.
    async with sessionmaker() as session:
        rows = list(
            (await session.execute(select(Job).where(Job.kind == menu_prompt.CARD_CLAIM_KIND))).scalars()
        )
    assert rows and all(row.status == "done" for row in rows)
    assert sorted(row.dedup_key for row in rows) == sorted(
        f"menu_card:{n}" for n in (7, 8, -7, 9)
    )
    assert all(str(CHAT_ID) not in row.dedup_key for row in rows)
    assert all(set(row.payload) == {"update_id"} for row in rows)


# --- export / delete / lens code on: Telegram vs a forged web press -------------------


async def test_export_by_button_sends_the_file_after_yes(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3)
    dp, bot, fake = _build(sessionmaker)
    card = await _open_card(dp, bot, fake, 1, "export")
    assert fake.documents == []

    await _tap(dp, bot, 3, "mn:y:export", message_id=card)

    assert len(fake.documents) == 1
    assert fake.documents[0].document.filename.endswith(".json")


async def test_delete_by_button_sends_the_wipe_card_and_deletes_nothing(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    settings = _settings()
    dp, bot, fake = _build(sessionmaker, settings=settings)
    await _say(dp, bot, 1, "/menu")

    await _tap(dp, bot, 2, "mn:a:delete")

    assert fake.sent[-1].text == data_ui.confirm_text(settings)
    buttons = [b.callback_data for row in fake.sent[-1].reply_markup.inline_keyboard for b in row]
    assert buttons[0].startswith("d:yes:") and buttons[1] == "d:no"
    assert await _state(sessionmaker) is not None


@pytest.fixture
async def nologin_after(sessionmaker):
    """The lens role is cluster-wide: whatever a test did, it ends NOLOGIN."""
    yield
    async with sessionmaker() as session:
        await session.execute(text("ALTER ROLE anchor_lens NOLOGIN"))
        await session.commit()


async def _lens_login(sessionmaker) -> bool | None:
    async with sessionmaker() as session:
        return await lens.code_access(session)


def _lens_settings(**extra) -> Settings:
    return _settings(
        VAULT_MODE="status", VAULT_API_TOKEN="x" * 32, LENS_ENABLED=True, **extra
    )


async def test_lens_code_on_by_button_opens_the_door_only_after_yes(sessionmaker, nologin_after):
    await _seed(sessionmaker, 1, 2, 3, 4)
    dp, bot, fake = _build(sessionmaker, settings=_lens_settings())
    await _say(dp, bot, 1, "/menu")
    await _tap(dp, bot, 2, "mn:a:lens_code_on")
    card = _last_id(fake)
    assert fake.sent[-1].text == menu.confirm_text("lens_code_on")
    assert await _lens_login(sessionmaker) is False

    await _tap(dp, bot, 3, "mn:y:lens_code_on", message_id=card)

    assert await _lens_login(sessionmaker) is True
    assert fake.sent[-1].text == lens_ui.CODE_SET_ON


async def test_lens_code_on_declined_leaves_the_door_shut(sessionmaker, nologin_after):
    await _seed(sessionmaker, 1, 2, 3)
    dp, bot, fake = _build(sessionmaker, settings=_lens_settings())
    await _say(dp, bot, 1, "/menu")
    await _tap(dp, bot, 2, "mn:a:lens_code_on")

    await _tap(dp, bot, 3, "mn:c", message_id=_last_id(fake))

    assert await _lens_login(sessionmaker) is False


async def test_lens_code_off_is_one_tap_and_rerenders_the_lens_card(sessionmaker, nologin_after):
    await _seed(sessionmaker, 1, 2)
    async with sessionmaker() as session:
        await lens.set_code_access(session, True)
    dp, bot, fake = _build(sessionmaker, settings=_lens_settings())
    await _say(dp, bot, 1, "/menu")

    await _tap(dp, bot, 2, "mn:a:lens_code_off")

    assert await _lens_login(sessionmaker) is False
    flat = flatten_rich_message(fake.edits[-1].rich_message)
    assert flat.startswith(menu.LENS_TITLE)
    assert "Доступ Claude Code: закрыт" in flat


FORGED_ON_THE_WEB = (
    "mn:a:export",
    "mn:y:export",
    "mn:a:delete",
    "mn:a:lens_code_on",
    "mn:y:lens_code_on",
    "mn:a:lens_undo",
    "mn:y:lens_undo",
    "mn:a:weblogout",
    "mn:y:weblogout",
    "mn:a:planner_link",
    "mn:y:planner_link",
    "mn:a:grok_open",
    "mn:i:claude_connect",
    "mn:y:claude_connect:ABCD-1234",
    "mn:y:claude_disconnect",
    "mn:a:interests_suggest",
    "mn:a:graph",
    "mn:a:hide_kb",
)


@pytest.mark.parametrize("data", FORGED_ON_THE_WEB)
async def test_a_forged_telegram_only_press_from_the_web_does_nothing(sessionmaker, nologin_after, data):
    await _seed(sessionmaker, 1, 2)
    bot, fake = make_bot()
    bot.is_web_sink = True
    settings = _lens_settings(
        PLANNER_ENABLED=True, RESEARCH_ENABLED=True, CLAUDE_ACCESS_ENABLED=True,
        GROK_ACCESS_ENABLED=True, WEB_UI_ENABLED=True,
    )
    dp, bot, fake = _build(sessionmaker, settings=settings, bot=bot, fake=fake)
    await _say(dp, bot, 1, "/menu")
    sent, edits = len(fake.sent), len(fake.edits)

    await _tap(dp, bot, 2, data)

    assert fake.documents == []
    assert len(fake.sent) == sent and len(fake.edits) == edits
    assert fake.answered[-1].text == "Устарело."
    assert await _lens_login(sessionmaker) is False
    state = await _state(sessionmaker)
    assert (state.awaiting, state.persona_active) == (None, True)
    async with sessionmaker() as session:
        assert (await session.execute(select(Job).where(Job.kind == "menu_card"))).first() is None
        assert (await session.execute(select(OauthConnection))).first() is None


async def test_the_same_presses_work_on_telegram(sessionmaker, nologin_after):
    """The contrast to the forged ones above: with the identical deploy,
    each of these buttons is accepted from Telegram."""
    await _seed(sessionmaker, *range(1, 12))
    settings = _lens_settings(PLANNER_ENABLED=True, RESEARCH_ENABLED=True, GROK_ACCESS_ENABLED=True)
    dp, bot, fake = _build(sessionmaker, settings=settings)
    await _say(dp, bot, 1, "/menu")
    stale = lambda: [a.text for a in fake.answered].count("Устарело.")  # noqa: E731

    for update_id, data in enumerate(
        ("mn:a:export", "mn:a:lens_code_on", "mn:a:planner_link", "mn:a:grok_open", "mn:a:delete"),
        start=2,
    ):
        await _tap(dp, bot, update_id, data)
    assert stale() == 0
    assert len(fake.sent) >= 5


async def test_a_telegram_only_prompt_answered_from_the_web_runs_nothing(sessionmaker):
    """The step is one global row: a message arriving over the web chat
    must never be handed to a Telegram-only command as its argument. It
    is refused and consumed, but the prompt -- still on the Telegram
    screen -- stays open, so the next Telegram line is its answer and
    not chat."""
    await _seed(sessionmaker, 1, 2, 3, 4)
    settings = _settings(CLAUDE_ACCESS_ENABLED=True, VAULT_MODE="status", VAULT_API_TOKEN="x" * 32)
    llm = FakeLLMProvider()
    tg_dp, tg_bot, tg_fake = _build(sessionmaker, settings=settings, llm=llm)
    prompt_id = await _open_prompt(tg_dp, tg_bot, tg_fake, 1, "claude_connect")
    web_bot, web_fake = make_bot()
    web_bot.is_web_sink = True
    web_dp, web_bot, web_fake = _build(sessionmaker, settings=settings, bot=web_bot, fake=web_fake, llm=llm)

    await _say(web_dp, web_bot, 3, "ABCD-1234")

    assert web_fake.sent[-1].text == WEB_ONLY_REPLY
    assert tg_fake.sent[-1].text == menu.prompt_text("claude_connect")  # no card was sent
    assert _edits_of(tg_fake, prompt_id) == []
    assert (await _state(sessionmaker)).awaiting == "menu:claude_connect"
    assert await _users(sessionmaker) == []

    await _say(tg_dp, tg_bot, 4, "ABCD-1234")

    assert tg_fake.sent[-1].text == menu.claude_connect_confirm_text("ABCD-1234")
    assert llm.calls == 0


async def test_prompts_work_on_the_web_for_commands_the_web_may_run(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3)
    bot, fake = make_bot()
    bot.is_web_sink = True
    llm = FakeLLMProvider()
    dp, bot, fake = _build(sessionmaker, bot=bot, fake=fake, llm=llm)
    await _open_prompt(dp, bot, fake, 1, "tz")

    await _say(dp, bot, 3, "Europe/Berlin")

    assert (await _state(sessionmaker)).timezone == "Europe/Berlin"
    assert llm.calls == 0


# --- forget: shows the record, then deletes on Да ------------------------------------------


async def _memory(sessionmaker, body: str = "не пью кофе после 16") -> int:
    async with sessionmaker() as session:
        row = Memory(kind="preference", text=body, source="user")
        session.add(row)
        await session.commit()
        return row.id


async def _memory_ids(sessionmaker) -> list[int]:
    async with sessionmaker() as session:
        return [row.id for row in (await session.execute(select(Memory))).scalars()]


async def test_forget_shows_the_record_then_deletes_on_yes(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3, 4)
    memory_id = await _memory(sessionmaker)
    dp, bot, fake = _build(sessionmaker)
    prompt_id = await _open_prompt(dp, bot, fake, 1, "forget")

    await _say(dp, bot, 3, str(memory_id))

    card = _last_id(fake)
    assert fake.sent[-1].text == menu.forget_confirm_text(memory_id, "не пью кофе после 16")
    assert [b.callback_data for row in fake.sent[-1].reply_markup.inline_keyboard for b in row] == [
        f"mn:y:forget:{memory_id}",
        "mn:c",
    ]
    assert await _memory_ids(sessionmaker) == [memory_id]  # nothing deleted yet
    assert _edits_of(fake, prompt_id)[-1].text == menu.PROMPT_ANSWERED_TEXT

    await _tap(dp, bot, 4, f"mn:y:forget:{memory_id}", message_id=card)

    assert await _memory_ids(sessionmaker) == []
    assert fake.sent[-1].text == memory_ui.FORGET_DONE.format(id=memory_id)
    assert _edits_of(fake, card)[-1].text == menu.DONE_TEXT


async def test_forget_no_keeps_the_record(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3, 4)
    memory_id = await _memory(sessionmaker)
    dp, bot, fake = _build(sessionmaker)
    await _open_prompt(dp, bot, fake, 1, "forget")
    await _say(dp, bot, 3, str(memory_id))

    await _tap(dp, bot, 4, "mn:c", message_id=_last_id(fake))

    assert await _memory_ids(sessionmaker) == [memory_id]


async def test_forget_double_tapped_yes_deletes_once(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3, 4, 5)
    memory_id = await _memory(sessionmaker)
    dp, bot, fake = _build(sessionmaker)
    await _open_prompt(dp, bot, fake, 1, "forget")
    await _say(dp, bot, 3, str(memory_id))
    card = _last_id(fake)

    await _tap(dp, bot, 4, f"mn:y:forget:{memory_id}", message_id=card)
    await _tap(dp, bot, 5, f"mn:y:forget:{memory_id}", message_id=card)

    assert _texts(fake).count(memory_ui.FORGET_DONE.format(id=memory_id)) == 1
    assert fake.answered[-1].text == "Устарело."


async def test_forget_with_a_missing_or_malformed_id_shows_no_card(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3, 4, 5)
    dp, bot, fake = _build(sessionmaker)
    await _open_prompt(dp, bot, fake, 1, "forget")
    await _say(dp, bot, 3, "999")
    assert fake.sent[-1].text == memory_ui.FORGET_MISSING

    await _tap(dp, bot, 4, "mn:i:forget")
    await _say(dp, bot, 5, "не число")
    assert fake.sent[-1].text == memory_ui.FORGET_USAGE
    assert not any("mn:y:forget" in str(m.reply_markup) for m in fake.sent)


async def test_forget_with_an_id_beyond_bigint_is_a_missing_record_not_an_error(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3)
    dp, bot, fake = _build(sessionmaker)
    await _open_prompt(dp, bot, fake, 1, "forget")

    await _say(dp, bot, 3, str(2**70))

    assert fake.sent[-1].text == memory_ui.FORGET_MISSING


async def test_pin_and_unpin_prompts_run_the_commands(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3, 4, 5)
    memory_id = await _memory(sessionmaker)
    dp, bot, fake = _build(sessionmaker)
    await _open_prompt(dp, bot, fake, 1, "pin")
    await _say(dp, bot, 3, str(memory_id))
    async with sessionmaker() as session:
        assert (await session.get(Memory, memory_id)).pinned is True

    await _tap(dp, bot, 4, "mn:i:unpin")
    await _say(dp, bot, 5, str(memory_id))
    async with sessionmaker() as session:
        assert (await session.get(Memory, memory_id)).pinned is False


# --- /claude connect: the code is shown back before it approves anything -----------------


def _claude_settings() -> Settings:
    return _settings(CLAUDE_ACCESS_ENABLED=True, VAULT_MODE="status", VAULT_API_TOKEN="x" * 32)


async def test_claude_connect_a_cyrillic_typed_code_still_asks_first(sessionmaker):
    """PendingStore.match reads Cyrillic look-alikes as Latin, so a code
    typed on a Russian layout is a real code: it must get the «Точно?»
    card too, showing the code as it will be looked up."""
    await _seed(sessionmaker, 1, 2, 3)
    dp, bot, fake = _build(sessionmaker, settings=_claude_settings())
    await _open_prompt(dp, bot, fake, 1, "claude_connect")

    await _say(dp, bot, 3, "АВС234")  # Cyrillic А, В, С

    assert fake.sent[-1].text == menu.claude_connect_confirm_text("ABC234")
    yes = fake.sent[-1].reply_markup.inline_keyboard[0][0]
    assert yes.callback_data == "mn:y:claude_connect:ABC234"


@pytest.mark.parametrize(
    ("line", "reply"),
    [("two words", claude_ui.USAGE), ("a" * 40, claude_ui.CODE_UNKNOWN), ("code;drop", claude_ui.CODE_UNKNOWN)],
)
async def test_claude_connect_text_that_is_no_code_is_answered_without_a_card(sessionmaker, line, reply):
    await _seed(sessionmaker, 1, 2, 3)
    dp, bot, fake = _build(sessionmaker, settings=_claude_settings())
    await _open_prompt(dp, bot, fake, 1, "claude_connect")

    await _say(dp, bot, 3, line)

    assert fake.sent[-1].text == reply
    assert fake.sent[-1].reply_markup is None
    assert (await _state(sessionmaker)).awaiting is None


async def test_claude_connect_shows_the_code_on_a_card_and_yes_runs_it_once(sessionmaker):
    await _seed(sessionmaker, *range(1, 8))
    settings = _settings(CLAUDE_ACCESS_ENABLED=True, VAULT_MODE="status", VAULT_API_TOKEN="x" * 32)
    dp, bot, fake = _build(sessionmaker, settings=settings)
    await _say(dp, bot, 1, "/menu")
    await _tap(dp, bot, 2, "mn:s:links")
    await _tap(dp, bot, 3, "mn:s:claude")
    await _tap(dp, bot, 4, "mn:i:claude_connect")
    await _say(dp, bot, 5, "ABCD-1234")

    assert fake.sent[-1].text == menu.claude_connect_confirm_text("ABCD-1234")
    yes = fake.sent[-1].reply_markup.inline_keyboard[0][0]
    assert yes.callback_data == "mn:y:claude_connect:ABCD-1234"
    card = _last_id(fake)
    sent = len(fake.sent)

    await _feed(dp, bot, _callback_update(6, yes.callback_data, message_id=card))
    answered = len(fake.sent)
    assert answered == sent + 1  # /claude connect's own reply (unknown code)
    await _feed(dp, bot, _callback_update(7, yes.callback_data, message_id=card))

    assert len(fake.sent) == answered  # the second tap never reached /claude
    assert fake.answered[-1].text == "Устарело."


# --- the cadence picker: order -> prompt -> /order -------------------------------------------


async def test_order_cadence_picker_to_prompt_to_a_created_order(sessionmaker):
    await _seed(sessionmaker, *range(1, 8))
    dp, bot, fake = _build(sessionmaker)
    await _say(dp, bot, 1, "/menu")
    await _tap(dp, bot, 2, "mn:s:deals")
    await _tap(dp, bot, 3, "mn:s:order_cadence")
    assert flatten_rich_message(fake.edits[-1].rich_message).startswith(menu.ORDER_CADENCE_TITLE)
    await _tap(dp, bot, 4, "mn:s:order_weekday")
    assert flatten_rich_message(fake.edits[-1].rich_message).startswith(menu.ORDER_WEEKDAY_TITLE)

    await _tap(dp, bot, 5, "mn:i:order_w3")
    assert fake.sent[-1].text == menu.prompt_text("order_w3")
    await _say(dp, bot, 6, "позвонить маме")

    async with sessionmaker() as session:
        orders = list((await session.execute(select(StandingOrder))).scalars())
    assert [(o.text, o.cadence, o.weekday, o.status) for o in orders] == [
        ("позвонить маме", "weekly", 3, "active")
    ]
    assert "позвонить маме" in fake.sent[-1].text


@pytest.mark.parametrize(
    "key,cadence,weekday",
    [("order_daily", "daily", None), ("order_weekdays", "weekdays", None), ("order_once", "once", None)],
)
async def test_every_simple_cadence_creates_its_order(sessionmaker, key, cadence, weekday):
    await _seed(sessionmaker, 1, 2, 3)
    dp, bot, fake = _build(sessionmaker)
    await _open_prompt(dp, bot, fake, 1, key)

    await _say(dp, bot, 3, "проверять почту")

    async with sessionmaker() as session:
        orders = list((await session.execute(select(StandingOrder))).scalars())
    assert [(o.cadence, o.weekday) for o in orders] == [(cadence, weekday)]


# --- the packet picker: interests add and study ----------------------------------------------


async def test_packet_picker_to_prompt_to_interests_add(sessionmaker):
    await _seed(sessionmaker, *range(1, 7))
    dp, bot, fake = _build(sessionmaker, settings=_settings(RESEARCH_ENABLED=True))
    await _say(dp, bot, 1, "/menu")
    await _tap(dp, bot, 2, "mn:s:research")
    await _tap(dp, bot, 3, "mn:s:packet_interest")
    flat = flatten_rich_message(fake.edits[-1].rich_message)
    assert flat.startswith(menu.PACKET_TITLE)
    assert rich_callback_data(fake.edits[-1].rich_message)[:2] == [
        "mn:i:interests_add_forums", "mn:i:interests_add_ref",
    ]

    await _tap(dp, bot, 4, "mn:i:interests_add_forums")
    assert fake.sent[-1].text == menu.prompt_text("interests_add_forums")
    await _say(dp, bot, 5, "бессонница")

    async with sessionmaker() as session:
        topics = list((await session.execute(select(InterestTopic))).scalars())
    assert [(t.packet, t.text) for t in topics] == [("forums", "бессонница")]
    assert "бессонница" in fake.sent[-1].text
    assert await _users(sessionmaker) == []


async def test_packet_picker_to_prompt_to_study(sessionmaker):
    await _seed(sessionmaker, *range(1, 7))
    dp, bot, fake = _build(sessionmaker, settings=_settings(RESEARCH_ENABLED=True))
    await _say(dp, bot, 1, "/menu")
    await _tap(dp, bot, 2, "mn:s:research")
    await _tap(dp, bot, 3, "mn:s:packet_study")

    await _tap(dp, bot, 4, "mn:i:study_ref")
    await _say(dp, bot, 5, "мелатонин")

    assert fake.sent[-1].text == research_ui.STUDY_ACCEPTED
    async with sessionmaker() as session:
        jobs = list((await session.execute(select(StudyJob))).scalars())
    assert [(j.kind, j.packet, j.query) for j in jobs] == [("study", "ref", "мелатонин")]


async def test_interests_run_by_number_prompt_runs_that_topic_now(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3, 4)
    dp, bot, fake = _build(sessionmaker, settings=_settings(RESEARCH_ENABLED=True))
    await _say(dp, bot, 1, "/interests add forums бессонница")
    async with sessionmaker() as session:
        topic = (await session.execute(select(InterestTopic))).scalar_one()
    await _tap(dp, bot, 2, "mn:i:interests_run_id")

    await _say(dp, bot, 4, str(topic.id))

    assert fake.sent[-1].text == interests_ui.RUN_ACCEPTED.format(text="бессонница", packet="forums")
    async with sessionmaker() as session:
        jobs = list((await session.execute(select(StudyJob))).scalars())
    assert [(j.kind, j.manual) for j in jobs] == [("study", True)]
    assert await _users(sessionmaker) == []


async def test_interests_run_by_number_with_an_unknown_number(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3)
    dp, bot, fake = _build(sessionmaker, settings=_settings(RESEARCH_ENABLED=True))
    await _open_prompt(dp, bot, fake, 1, "interests_run_id")

    await _say(dp, bot, 3, "999")

    assert fake.sent[-1].text in (interests_ui.RUN_TOPIC_MISSING, interests_ui.RUN_NO_TOPICS)
    assert (await _state(sessionmaker)).awaiting is None
    assert await _users(sessionmaker) == []


# --- /card: the card, then Принять / Отклонить -------------------------------------------------


async def _seed_card(sessionmaker, **overrides) -> StudyCard:
    async with sessionmaker() as session:
        job = StudyJob(kind="read", local_date=datetime.date(2026, 10, 25), status="done")
        session.add(job)
        await session.flush()
        clip = StudyClip(
            job_id=job.id, url="https://example.com/sleep", domain="example.com", text="текст"
        )
        session.add(clip)
        await session.flush()
        fields = dict(
            job_id=job.id,
            clip_id=clip.id,
            kind="technique",
            text="Ложиться спать в одно и то же время.",
            quote="Ложитесь спать в одно и то же время каждый день.",
            source_url=clip.url,
            risk_model="low",
            risk_rules="low",
            risk_final="low",
            status="pending",
        )
        fields.update(overrides)
        card = StudyCard(**fields)
        session.add(card)
        await session.commit()
        await session.refresh(card)
        return card


async def _card_status(sessionmaker, card_id: int) -> str:
    async with sessionmaker() as session:
        return (await session.get(StudyCard, card_id)).status


async def test_card_prompt_shows_the_card_with_adopt_and_reject_buttons(sessionmaker):
    card = await _seed_card(sessionmaker)
    await _seed(sessionmaker, 1, 2, 3)
    dp, bot, fake = _build(sessionmaker, settings=_settings(RESEARCH_ENABLED=True))
    await _open_prompt(dp, bot, fake, 1, "card")

    await _say(dp, bot, 3, str(card.id))

    assert fake.sent[-1].text == research_ui.render_card_detail(card)
    buttons = [b.callback_data for row in fake.sent[-1].reply_markup.inline_keyboard for b in row]
    assert buttons == [f"r:a:{card.id}", f"r:r:{card.id}"]


async def test_card_buttons_adopt_and_reject(sessionmaker):
    adopted = await _seed_card(sessionmaker)
    rejected = await _seed_card(sessionmaker, text="Другая карточка.")
    await _seed(sessionmaker, *range(1, 9))
    dp, bot, fake = _build(sessionmaker, settings=_settings(RESEARCH_ENABLED=True))
    await _open_prompt(dp, bot, fake, 1, "card")
    await _say(dp, bot, 3, str(adopted.id))
    first = _last_id(fake)
    await _tap(dp, bot, 4, "mn:i:card")
    await _say(dp, bot, 5, str(rejected.id))
    second = _last_id(fake)

    await _tap(dp, bot, 6, f"r:a:{adopted.id}", message_id=first)
    await _tap(dp, bot, 7, f"r:r:{rejected.id}", message_id=second)

    assert await _card_status(sessionmaker, adopted.id) == "adopted"
    assert await _card_status(sessionmaker, rejected.id) == "rejected"


async def test_card_prompt_for_a_decided_card_has_no_buttons(sessionmaker):
    card = await _seed_card(sessionmaker, status="rejected")
    await _seed(sessionmaker, 1, 2, 3)
    dp, bot, fake = _build(sessionmaker, settings=_settings(RESEARCH_ENABLED=True))
    await _open_prompt(dp, bot, fake, 1, "card")

    await _say(dp, bot, 3, str(card.id))

    assert fake.sent[-1].text == research_ui.render_card_detail(card)
    assert fake.sent[-1].reply_markup is None


async def test_card_prompt_with_a_missing_number(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3)
    dp, bot, fake = _build(sessionmaker, settings=_settings(RESEARCH_ENABLED=True))
    await _open_prompt(dp, bot, fake, 1, "card")

    await _say(dp, bot, 3, "999999")

    assert fake.sent[-1].text == research_ui.CARD_MISSING


# --- quiet keeps its way back ---------------------------------------------------------------------


async def test_quiet_from_settings_keeps_its_back_origin_through_a_preset(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3, 4)
    dp, bot, fake = _build(sessionmaker)
    await _say(dp, bot, 1, "/menu")
    await _tap(dp, bot, 2, "mn:s:settings")
    await _tap(dp, bot, 3, "mn:s:quiet:settings")
    data = rich_callback_data(fake.edits[-1].rich_message)
    assert data[-2:] == ["mn:s:settings", "mn:x"]
    assert "mn:a:quiet_2h:settings" in data

    await _tap(dp, bot, 4, "mn:a:quiet_2h:settings")

    flat = flatten_rich_message(fake.edits[-1].rich_message)
    assert flat.startswith(menu.QUIET_TITLE) and "Сейчас: до" in flat
    data = rich_callback_data(fake.edits[-1].rich_message)
    assert data[-2:] == ["mn:s:settings", "mn:x"]
    assert "mn:a:quiet_off:settings" in data
    assert (await _state(sessionmaker)).quiet_until is not None


async def test_quiet_from_main_goes_back_to_main(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3)
    dp, bot, fake = _build(sessionmaker)
    await _say(dp, bot, 1, "/menu")
    await _tap(dp, bot, 2, "mn:s:quiet")
    await _tap(dp, bot, 3, "mn:a:quiet_30m")

    data = rich_callback_data(fake.edits[-1].rich_message)
    assert data[-2:] == ["mn:s:main", "mn:x"]
    assert "mn:a:quiet_off" in data


# --- forged shapes ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        "mn:y:checkin",  # a run action has no card
        "mn:y:remember",  # a prompt without a card
        "mn:y:forget",  # a card-after-input key needs its argument
        "mn:y:export:12",  # and a plain card takes none
        "mn:a:remember",  # a prompt is opened with mn:i:
        "mn:a:forget",
        "mn:i:checkin",  # and a run action is not a prompt
        "mn:i:tz:extra",
        "mn:a:nope",
        "mn:y:nope",
        "mn:i:nope",
        "mn:z:checkin",
        "mn:a:",
    ],
)
async def test_forged_shapes_answer_stale_and_do_nothing(sessionmaker, data):
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build(sessionmaker)
    await _say(dp, bot, 1, "/menu")
    sent, edits = len(fake.sent), len(fake.edits)

    await _tap(dp, bot, 2, data)

    assert (len(fake.sent), len(fake.edits)) == (sent, edits)
    assert fake.answered[-1].text == "Устарело."
    state = await _state(sessionmaker)
    assert state.awaiting is None and state.timezone == TIMEZONE


async def test_a_prompt_whose_feature_went_off_is_ended_and_answered(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3)
    on = _settings(PLANNER_ENABLED=True)
    dp, bot, fake = _build(sessionmaker, settings=on)
    await _open_prompt(dp, bot, fake, 1, "task")
    off_dp, off_bot, off_fake = _build(sessionmaker, settings=_settings(PLANNER_ENABLED=False))

    await _say(off_dp, off_bot, 3, "купить хлеб")

    assert off_fake.sent[-1].text == menu.PROMPT_UNAVAILABLE
    assert (await _state(sessionmaker)).awaiting is None
    assert await _users(sessionmaker) == []


# --- review fixes (2026-10-25) ----------------------------------------------------


def _dated_update(update_id: int, line: str, when: datetime.datetime) -> dict:
    payload = _command_update(update_id, line)
    payload["message"]["date"] = int(when.timestamp())
    return payload


async def test_the_prompt_ttl_is_measured_at_the_messages_own_date(sessionmaker):
    """A reply sent inside the TTL but processed after it (a queue
    backlog, a redeploy) still answers the prompt: the clock is the
    Telegram message's `date`, not the moment the worker got to it."""
    clock = FrozenClock(NOW)
    await _seed(sessionmaker, 1, 2, 3)
    llm = FakeLLMProvider()
    dp, bot, fake = _build(sessionmaker, settings=_settings(MENU_PROMPT_TTL_MIN=10), llm=llm, clock=clock)
    await _open_prompt(dp, bot, fake, 1, "tz")

    clock.advance(datetime.timedelta(minutes=15))
    await _feed(dp, bot, _dated_update(3, "Europe/Berlin", NOW + datetime.timedelta(minutes=5)))

    assert (await _state(sessionmaker)).timezone == "Europe/Berlin"
    assert llm.calls == 0


async def test_a_message_dated_after_the_ttl_is_chat_even_if_processed_at_once(sessionmaker):
    clock = FrozenClock(NOW)
    await _seed(sessionmaker, 1, 2, 3)
    llm = FakeLLMProvider()
    dp, bot, fake = _build(sessionmaker, settings=_settings(MENU_PROMPT_TTL_MIN=10), llm=llm, clock=clock)
    await _open_prompt(dp, bot, fake, 1, "tz")

    await _feed(dp, bot, _dated_update(3, "Europe/Berlin", NOW + datetime.timedelta(minutes=11)))

    assert (await _state(sessionmaker)).timezone == TIMEZONE
    assert llm.calls == 1


async def test_a_replayed_prompt_tap_leaves_its_own_prompt_open(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3)
    dp, bot, fake = _build(sessionmaker)
    prompt_id = await _open_prompt(dp, bot, fake, 1, "tz")

    await _tap(dp, bot, 2, "mn:i:tz")  # the queue re-runs the tap

    assert (await _state(sessionmaker)).awaiting == "menu:tz"
    assert _edits_of(fake, prompt_id) == []
    await _say(dp, bot, 3, "Europe/Berlin")
    assert (await _state(sessionmaker)).timezone == "Europe/Berlin"


async def test_a_replayed_confirm_tap_clears_no_step(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3, 4)
    dp, bot, fake = _build(sessionmaker)
    await _open_card(dp, bot, fake, 1, "export")
    prompt_id = await _open_prompt(dp, bot, fake, 3, "tz")
    cards = _texts(fake).count(menu.confirm_text("export"))

    await _tap(dp, bot, 2, "mn:a:export")  # a replay of the card's own tap

    assert _texts(fake).count(menu.confirm_text("export")) == cards
    assert (await _state(sessionmaker)).awaiting == "menu:tz"
    assert _edits_of(fake, prompt_id) == []


class _Crash(RuntimeError):
    pass


def _crash_once_in_ensure_scene(monkeypatch):
    """The worker dies once, at the first `turn.ensure_scene` call."""
    real = turn.ensure_scene
    calls = {"n": 0}

    async def ensure_scene(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _Crash()
        return await real(*args, **kwargs)

    monkeypatch.setattr(turn, "ensure_scene", ensure_scene)


async def test_forget_card_is_recorded_as_sent_so_a_crash_replay_sends_no_second(sessionmaker, monkeypatch):
    await _seed(sessionmaker, 1, 2, 3)
    memory_id = await _memory(sessionmaker)
    dp, bot, fake = _build(sessionmaker)
    await _open_prompt(dp, bot, fake, 1, "forget")
    _crash_once_in_ensure_scene(monkeypatch)

    with pytest.raises(_Crash):
        await _say(dp, bot, 3, str(memory_id))
    await _say(dp, bot, 3, str(memory_id))  # the replay

    card = menu.forget_confirm_text(memory_id, "не пью кофе после 16")
    assert _texts(fake).count(card) == 1
    assert (await _state(sessionmaker)).awaiting is None


async def test_claude_connect_card_is_recorded_as_sent_so_a_crash_replay_sends_no_second(
    sessionmaker, monkeypatch
):
    await _seed(sessionmaker, 1, 2, 3)
    dp, bot, fake = _build(sessionmaker, settings=_claude_settings())
    await _open_prompt(dp, bot, fake, 1, "claude_connect")
    _crash_once_in_ensure_scene(monkeypatch)

    with pytest.raises(_Crash):
        await _say(dp, bot, 3, "ABC234")
    await _say(dp, bot, 3, "ABC234")

    assert _texts(fake).count(menu.claude_connect_confirm_text("ABC234")) == 1


async def test_the_orders_sweep_leaves_a_menu_prompt_alone(sessionmaker):
    """`awaiting_ref` is the prompt's message id under a menu prompt, not
    an order id: an expiring order that happens to share the number must
    not end the prompt."""
    await _seed(sessionmaker, 1, 2)
    async with sessionmaker() as session:
        order = await orders_core.propose(session, "плавать", orders_core.DAILY, None, source="anchor")
        row = await session.get(StandingOrder, order.id)
        row.created_at = NOW - datetime.timedelta(days=30)
        await session.commit()
    dp, bot, fake = _build(sessionmaker, clock=FrozenClock(NOW))
    await _open_prompt(dp, bot, fake, 1, "tz")
    async with sessionmaker() as session:
        await session.execute(text("update user_state set awaiting_ref = :id"), {"id": order.id})
        await session.commit()

    async with sessionmaker() as session:
        assert await orders_core.expire_stale(session, clock=FrozenClock(NOW)) == 1

    state = await _state(sessionmaker)
    assert (state.awaiting, state.awaiting_ref) == ("menu:tz", order.id)


async def test_lens_status_button_runs_bare_lens_even_with_the_lens_off(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3)
    settings = _settings(VAULT_MODE="status", VAULT_API_TOKEN="x" * 32, LENS_ENABLED=False)
    dp, bot, fake = _build(sessionmaker, settings=settings)
    await _say(dp, bot, 1, "/menu")
    await _tap(dp, bot, 2, "mn:s:lens")
    assert "mn:a:lens" in rich_callback_data(fake.edits[-1].rich_message)

    await _tap(dp, bot, 3, "mn:a:lens")

    assert "Устарело." not in [a.text for a in fake.answered]
    assert lens_ui.OFF_LINE in fake.sent[-1].text
