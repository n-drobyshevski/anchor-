"""The check-in flow, the streak, and the note step (plan sections 9, 13, 14).

The two tests that matter most here are
test_a_hard_pause_word_at_the_note_step_pauses_instead and its soft
twin: plan section 13 puts pause words before everything, `awaiting`
states included, and the note step is the only place in the codebase
where a plain text message means something other than "talk to me".
"""

from __future__ import annotations

import datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy import select

from app.config import Settings
from app.core import checkin, orders
from app.db.models import (
    Checkin,
    CheckinOrderResult,
    Job,
    Message,
    StandingOrder,
    StateChange,
    TelegramUpdate,
    UserState,
)
from app.tg import checkin as checkin_ui
from app.tg.router import build_router
from conftest import FakeLLMProvider, FakeSession

pytestmark = pytest.mark.asyncio

TEST_CHAT_ID = 555
TIMEZONE = "Europe/Paris"
MESSAGE_ID = 1


def _command_update(update_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": TEST_CHAT_ID, "type": "private"},
            "from": {"id": TEST_CHAT_ID, "is_bot": False, "first_name": "Test"},
            "text": text,
            "entities": [{"type": "bot_command", "offset": 0, "length": len(text.split(" ", 1)[0])}],
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


def _callback_update(update_id: int, data: str, *, message_id: int = MESSAGE_ID) -> dict:
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


def _build_dp(sessionmaker, settings=None, provider=None):
    fake = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake)
    dp = Dispatcher()
    dp.include_router(
        build_router(sessionmaker, settings or Settings(), provider or FakeLLMProvider(text="Принято."))
    )
    return dp, bot, fake


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


# --- the streak (plan section 9) ---


def _today() -> datetime.date:
    from app.core.clock import SystemClock
    from app.core.clock import local_date as clock_local_date

    return clock_local_date(SystemClock(), TIMEZONE)


async def _complete_checkin(sessionmaker, clock, *, rating=4):
    async with sessionmaker() as session:
        row = await checkin.start(session, clock, TIMEZONE)
        await checkin.set_rating(session, row.id, rating)
        await checkin.set_due_result(session, row.id, checkin.NONE)
        return await checkin.finish(session, clock, TIMEZONE)


async def test_first_checkin_starts_the_streak_at_one(sessionmaker, clock):
    await _seed(sessionmaker)
    _, streak = await _complete_checkin(sessionmaker, clock)
    assert streak == 1
    assert (await _state(sessionmaker)).streak == 1


async def test_a_checkin_the_day_after_increments(sessionmaker, clock):
    await _seed(sessionmaker, streak=1)
    async with sessionmaker() as session:
        session.add(Checkin(local_date=_today() - datetime.timedelta(days=1), day_rating=3))
        await session.commit()

    _, streak = await _complete_checkin(sessionmaker, clock)
    assert streak == 2


async def test_a_gap_resets_the_streak_to_one(sessionmaker, clock):
    await _seed(sessionmaker, streak=7)
    async with sessionmaker() as session:
        session.add(Checkin(local_date=_today() - datetime.timedelta(days=3), day_rating=3))
        await session.commit()

    _, streak = await _complete_checkin(sessionmaker, clock)
    assert streak == 1


async def test_a_same_day_recheckin_does_not_change_the_streak(sessionmaker, clock):
    """Plan section 9, and the reason finish() reads last_checkin_at
    rather than counting rows."""
    await _seed(sessionmaker, streak=1)
    async with sessionmaker() as session:
        session.add(Checkin(local_date=_today() - datetime.timedelta(days=1), day_rating=3))
        await session.commit()

    _, first = await _complete_checkin(sessionmaker, clock)
    _, second = await _complete_checkin(sessionmaker, clock)

    assert first == 2
    assert second == 2, "a redo of today must not bump the streak again"


async def test_finishing_writes_a_state_change_for_the_streak(sessionmaker, clock):
    await _seed(sessionmaker)
    await _complete_checkin(sessionmaker, clock)

    async with sessionmaker() as session:
        rows = (
            (await session.execute(select(StateChange).where(StateChange.field == "streak")))
            .scalars().all()
        )
    assert len(rows) == 1
    assert rows[0].new_value == "1"
    assert rows[0].source == "command"


async def test_a_second_checkin_the_same_day_overwrites_the_first(sessionmaker, clock):
    """`local_date` is unique, so starting again resets the day's row."""
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        first = await checkin.start(session, clock, TIMEZONE)
        await checkin.set_rating(session, first.id, 5)
        await checkin.set_note(session, first.id, "первая заметка")
        second = await checkin.start(session, clock, TIMEZONE)

    # A fresh session, deliberately: the writing session's identity map
    # would hand back the pre-upsert row and the assertion would pass
    # against stale objects rather than against the database.
    async with sessionmaker() as session:
        rows = (await session.execute(select(Checkin))).scalars().all()

    assert len(rows) == 1
    assert second.id == first.id
    assert second.day_rating is None, "start() returns the row as it now is"
    assert rows[0].day_rating is None, "answers are reset, not carried over"
    assert rows[0].note is None


# --- the synthetic line (plan section 9) ---


async def test_synthetic_line_format(sessionmaker):
    row = Checkin(local_date=_today(), day_rating=4, due_result="partial", note="устал")
    assert checkin.synthetic_line(row) == "[чек-ин] день 4/5 · действие: частично · «устал»"


async def test_synthetic_line_omits_a_missing_action_and_note(sessionmaker):
    row = Checkin(local_date=_today(), day_rating=2, due_result="none", note=None)
    assert checkin.synthetic_line(row) == "[чек-ин] день 2/5"


# --- the button flow ---


async def test_the_full_flow_edits_one_message(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3, 4, due_action="сдать отчёт")
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/checkin"))
    assert len(fake.sent) == 1
    assert fake.sent[0].text == checkin_ui.RATING_TEXT
    assert [b.text for r in fake.sent[0].reply_markup.inline_keyboard for b in r] == list("12345")

    await _feed(dp, bot, _callback_update(2, "c:r:4"))
    assert "сдать отчёт" in fake.edits[-1].text
    assert [b.text for r in fake.edits[-1].reply_markup.inline_keyboard for b in r] == [
        "Да", "Частично", "Нет"
    ]

    await _feed(dp, bot, _callback_update(3, "c:d:partial"))
    assert fake.edits[-1].text == checkin_ui.NOTE_TEXT

    await _feed(dp, bot, _text_update(4, "устал, но сделал"))

    assert len(fake.sent) == 2, "one keyboard message plus the in-character reply"
    async with sessionmaker() as session:
        row = (await session.execute(select(Checkin))).scalars().one()
    assert row.day_rating == 4
    assert row.due_result == "partial"
    assert row.note == "устал, но сделал"


async def test_no_due_action_skips_step_two(sessionmaker):
    """Plan section 9: record 'none' and go straight to the note."""
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/checkin"))
    await _feed(dp, bot, _callback_update(2, "c:r:3"))

    assert fake.edits[-1].text == checkin_ui.NOTE_TEXT
    async with sessionmaker() as session:
        row = (await session.execute(select(Checkin))).scalars().one()
    assert row.due_result == "none"


async def test_skip_finishes_without_a_note(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/checkin"))
    await _feed(dp, bot, _callback_update(2, "c:r:5"))
    await _feed(dp, bot, _callback_update(3, "c:n:skip"))

    assert "Серия: 1" in fake.edits[-1].text
    assert fake.edits[-1].reply_markup is None
    async with sessionmaker() as session:
        row = (await session.execute(select(Checkin))).scalars().one()
    assert row.note is None
    assert (await _state(sessionmaker)).streak == 1


async def test_a_second_skip_press_does_not_run_a_second_turn(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3, 4)
    provider = FakeLLMProvider(text="Принято.")
    dp, bot, fake = _build_dp(sessionmaker, provider=provider)

    await _feed(dp, bot, _command_update(1, "/checkin"))
    await _feed(dp, bot, _callback_update(2, "c:r:5"))
    await _feed(dp, bot, _callback_update(3, "c:n:skip"))
    await _feed(dp, bot, _callback_update(4, "c:n:skip"))

    assert provider.calls == 1


async def test_a_button_from_another_checkin_is_stale(sessionmaker):
    """Plan section 9's stale-button rule, which is why checkin carries
    a tg_message_id at all."""
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/checkin"))
    await _feed(dp, bot, _callback_update(2, "c:r:4", message_id=MESSAGE_ID + 999))

    assert fake.answered[-1].text == checkin_ui.STALE
    assert fake.edits == []
    async with sessionmaker() as session:
        row = (await session.execute(select(Checkin))).scalars().one()
    assert row.day_rating is None


# --- pause words beat the note step (plan section 13) ---


async def test_a_hard_pause_word_at_the_note_step_pauses_instead(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3)
    provider = FakeLLMProvider(text="не должно вызваться")
    dp, bot, fake = _build_dp(sessionmaker, provider=provider)

    await _feed(dp, bot, _command_update(1, "/checkin"))
    await _feed(dp, bot, _callback_update(2, "c:r:3"))
    await _feed(dp, bot, _text_update(3, "пурпурный"))

    state = await _state(sessionmaker)
    assert state.persona_active is False, "the pause word wins"
    assert state.awaiting is None, "and clears awaiting"
    assert provider.calls == 0

    async with sessionmaker() as session:
        row = (await session.execute(select(Checkin))).scalars().one()
    assert row.note is None, "the safeword must not be filed as a note"


async def test_a_soft_pause_word_at_the_note_step_also_wins(sessionmaker):
    """Section 9's "pause words always win" is unqualified, so жёлтый
    lowers intensity and carries on as an ordinary turn."""
    await _seed(sessionmaker, 1, 2, 3, intensity=4)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/checkin"))
    await _feed(dp, bot, _callback_update(2, "c:r:3"))
    await _feed(dp, bot, _text_update(3, "жёлтый, полегче"))

    state = await _state(sessionmaker)
    assert state.intensity == 3
    assert state.awaiting is None

    async with sessionmaker() as session:
        row = (await session.execute(select(Checkin))).scalars().one()
    assert row.note is None


async def test_a_slash_command_clears_awaiting(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/checkin"))
    await _feed(dp, bot, _callback_update(2, "c:r:3"))
    assert (await _state(sessionmaker)).awaiting == "checkin_note"

    await _feed(dp, bot, _command_update(3, "/state"))
    assert (await _state(sessionmaker)).awaiting is None


async def test_an_unknown_command_also_clears_awaiting(sessionmaker):
    """The middleware is outer, so it runs even when no handler matches."""
    await _seed(sessionmaker, 1, 2, 3)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/checkin"))
    await _feed(dp, bot, _callback_update(2, "c:r:3"))
    await _feed(dp, bot, _command_update(3, "/nosuchcommand"))

    assert (await _state(sessionmaker)).awaiting is None


async def test_a_note_step_left_open_overnight_does_not_swallow_the_next_day(sessionmaker):
    """Section 9 never says what happens to an unanswered note step.
    Left literal, tomorrow's first message becomes yesterday's note."""
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        stale = Checkin(local_date=_today() - datetime.timedelta(days=1), day_rating=3)
        session.add(stale)
        await session.commit()
        await session.refresh(stale)
        await checkin.set_awaiting_note(session, stale.id)

    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _text_update(1, "доброе утро, как дела"))

    state = await _state(sessionmaker)
    assert state.awaiting is None, "the stale step is cleared"
    async with sessionmaker() as session:
        row = await session.get(Checkin, stale.id)
        messages = (await session.execute(select(Message).where(Message.role == "user"))).scalars().all()

    assert row.note is None, "yesterday's check-in is untouched"
    assert [m.content for m in messages] == ["доброе утро, как дела"], "an ordinary message"
    assert messages[0].kind == "chat"


# --- the turn the check-in runs (plan section 9 step 4) ---


async def test_the_checkin_turn_stores_the_synthetic_line_and_runs_the_extractor(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3, 4, due_action="сдать отчёт")
    provider = FakeLLMProvider(text="Хорошо. Завтра — одна страница.")
    dp, bot, fake = _build_dp(sessionmaker, provider=provider)

    await _feed(dp, bot, _command_update(1, "/checkin"))
    await _feed(dp, bot, _callback_update(2, "c:r:4"))
    await _feed(dp, bot, _callback_update(3, "c:d:done"))
    await _feed(dp, bot, _text_update(4, "сделал всё"))

    async with sessionmaker() as session:
        stored = (
            (await session.execute(select(Message).where(Message.role == "user"))).scalars().all()
        )
        jobs = (await session.execute(select(Job))).scalars().all()

    assert len(stored) == 1, "the raw note is never a message of its own"
    assert stored[0].content == "[чек-ин] день 4/5 · действие: сделано · «сделал всё»"
    assert stored[0].kind == "checkin"
    assert [j.kind for j in jobs if j.kind == "extract"] == ["extract"]

    # The hidden flag reached the model.
    sent = "\n".join(m.content for m in provider.received_messages[0])
    assert "только что прошёл чек-ин" in sent


async def test_the_note_completion_retires_the_buttons(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/checkin"))
    await _feed(dp, bot, _callback_update(2, "c:r:3"))
    await _feed(dp, bot, _text_update(3, "нормально"))

    assert fake.edits[-1].reply_markup is None
    assert "Серия: 1" in fake.edits[-1].text


async def test_an_over_long_note_is_trimmed_to_the_column_limit(sessionmaker, clock):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        row = await checkin.start(session, clock, TIMEZONE)
        saved = await checkin.set_note(session, row.id, "я" * 600)
    assert len(saved.note) == checkin.NOTE_MAX


# --- 5c: standing orders in the check-in (plan section 7) ------------------


async def _add_order(sessionmaker, text: str, cadence: str = "daily", weekday: int | None = None) -> int:
    async with sessionmaker() as session:
        session.add(StandingOrder(text=text, cadence=cadence, weekday=weekday, status=orders.ACTIVE, source="user"))
        await session.commit()
        row = (await session.execute(select(StandingOrder).where(StandingOrder.text == text))).scalars().one()
        return row.id


async def test_order_step_appears_after_the_due_step(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3, due_action="сдать отчёт")
    order_id = await _add_order(sessionmaker, "пить воду")
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/checkin"))
    await _feed(dp, bot, _callback_update(2, "c:r:4"))
    await _feed(dp, bot, _callback_update(3, "c:d:done"))

    assert fake.edits[-1].text == "«пить воду» — сегодня выполнено?"
    labels = [b.text for r in fake.edits[-1].reply_markup.inline_keyboard for b in r]
    assert labels == ["Да", "Нет"]


async def test_order_step_appears_after_rating_with_no_due_action(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    await _add_order(sessionmaker, "читать")
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/checkin"))
    await _feed(dp, bot, _callback_update(2, "c:r:3"))

    assert "читать" in fake.edits[-1].text
    assert fake.edits[-1].reply_markup is not None


async def test_no_orders_goes_straight_to_the_note_step(sessionmaker):
    """No regression on 2d's own shape (tested above): with no active
    orders, the flow is exactly what it was before 5c."""
    await _seed(sessionmaker, 1, 2)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/checkin"))
    await _feed(dp, bot, _callback_update(2, "c:r:3"))

    assert fake.edits[-1].text == checkin_ui.NOTE_TEXT


async def test_at_most_three_orders_are_asked(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3, 4, 5)
    ids = [await _add_order(sessionmaker, f"дело {i}") for i in range(4)]
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/checkin"))
    await _feed(dp, bot, _callback_update(2, "c:r:3"))
    for update_id, order_id in zip((3, 4, 5), ids[:3]):
        assert "дело" in fake.edits[-1].text
        await _feed(dp, bot, _callback_update(update_id, f"c:o:{order_id}:d"))

    # The fourth active order is never asked (the cap), and the flow
    # moves on to the note step.
    assert fake.edits[-1].text == checkin_ui.NOTE_TEXT
    async with sessionmaker() as session:
        results = (await session.execute(select(CheckinOrderResult))).scalars().all()
    assert len(results) == 3


async def test_only_orders_due_today_are_asked(sessionmaker, clock):
    await _seed(sessionmaker, 1, 2)
    from app.core.clock import local_date as clock_local_date

    today = clock_local_date(clock, TIMEZONE)
    not_today_weekday = (today.isoweekday() % 7) + 1  # any other ISO weekday
    await _add_order(sessionmaker, "дело недели", cadence="weekly", weekday=not_today_weekday)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/checkin"))
    await _feed(dp, bot, _callback_update(2, "c:r:3"))

    assert fake.edits[-1].text == checkin_ui.NOTE_TEXT


async def test_order_results_are_stored_and_the_synthetic_line_lists_them(sessionmaker):
    await _seed(sessionmaker, 1, 2, 3, 4)
    order_id = await _add_order(sessionmaker, "пить воду")
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/checkin"))
    await _feed(dp, bot, _callback_update(2, "c:r:3"))
    await _feed(dp, bot, _callback_update(3, f"c:o:{order_id}:n"))
    await _feed(dp, bot, _callback_update(4, "c:n:skip"))

    async with sessionmaker() as session:
        result = await session.get(CheckinOrderResult, (1, order_id))
    assert result is not None
    assert result.result == "no"

    # Пропустить runs an in-character turn whose user-role content is
    # the synthetic line -- app/core/checkin.py's `synthetic_line` is
    # what this asserts the exact clause from.
    async with sessionmaker() as session:
        stored = (await session.execute(select(Message))).scalars().all()
    user_rows = [row for row in stored if row.role == "user"]
    assert any("договорённости: «пить воду» — нет" in row.content for row in user_rows)


# --- W4: the shared step rules and `submit` (app/core/checkin.py) ---


async def test_due_step_needed():
    assert checkin.due_step_needed("сдать отчёт") is True
    assert checkin.due_step_needed(None) is False
    assert checkin.due_step_needed("") is False


@pytest.mark.parametrize(
    "due_action,requested,expected",
    [
        (None, None, checkin.NONE),
        (None, checkin.DONE, checkin.NONE),
        ("", "partial", checkin.NONE),
        ("отчёт", checkin.DONE, checkin.DONE),
        ("отчёт", checkin.PARTIAL, checkin.PARTIAL),
        ("отчёт", checkin.NO, checkin.NO),
        ("отчёт", checkin.NONE, None),
        ("отчёт", None, None),
        ("отчёт", "maybe", None),
    ],
)
async def test_resolve_due_result(due_action, requested, expected):
    assert checkin.resolve_due_result(due_action, requested) == expected


async def test_telegram_rating_with_an_empty_due_action_still_records_none(sessionmaker):
    """W4 moved the rule into core (`due_step_needed`/`resolve_due_result`);
    an empty-string due action behaves exactly like none at all."""
    await _seed(sessionmaker, 1, 2, due_action="")
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/checkin"))
    await _feed(dp, bot, _callback_update(2, "c:r:3"))

    assert fake.edits[-1].text == checkin_ui.NOTE_TEXT
    async with sessionmaker() as session:
        row = (await session.execute(select(Checkin))).scalars().one()
    assert row.due_result == checkin.NONE


async def test_telegram_rating_step_asks_through_the_core_rule(sessionmaker, monkeypatch):
    await _seed(sessionmaker, 1, 2, due_action="сдать отчёт")
    seen = []
    real = checkin.due_step_needed

    def spy(due_action):
        seen.append(due_action)
        return real(due_action)

    monkeypatch.setattr(checkin, "due_step_needed", spy)
    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/checkin"))
    await _feed(dp, bot, _callback_update(2, "c:r:3"))
    assert seen == ["сдать отчёт"]
    assert "сдать отчёт" in fake.edits[-1].text


async def test_the_note_keyboard_uses_the_shared_skip_callback():
    (button,) = [b for row in checkin_ui.note_keyboard().inline_keyboard for b in row]
    assert button.callback_data == checkin_ui.SKIP_CALLBACK == "c:n:skip"


async def test_retire_for_web_drops_the_keyboard():
    fake = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake)
    await checkin_ui.retire_for_web(bot, TEST_CHAT_ID, 42)
    (edit,) = fake.edits
    assert (edit.chat_id, edit.message_id, edit.text) == (
        TEST_CHAT_ID,
        42,
        checkin_ui.WEB_TAKEOVER_TEXT,
    )
    assert edit.reply_markup is None
    assert fake.sent == []


async def test_retire_for_web_refuses_a_web_id_on_the_real_bot():
    fake = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake)
    await checkin_ui.retire_for_web(bot, TEST_CHAT_ID, -42)
    assert fake.edits == []


async def test_form_orders_are_todays_due_orders_capped(sessionmaker, clock):
    await _seed(sessionmaker)
    ids = [await _add_order(sessionmaker, f"дело {i}") for i in range(4)]
    today = _today()
    other_weekday = (today.isoweekday() % 7) + 1
    await _add_order(sessionmaker, "не сегодня", cadence="weekly", weekday=other_weekday)
    async with sessionmaker() as session:
        got = await checkin.form_orders(session, clock, TIMEZONE, 3)
    assert [o.id for o in got] == ids[:3]


async def test_submit_fills_every_step_without_opening_the_note_step(sessionmaker, clock):
    """Review fix: the web path never opens the global note step, which
    whatever queued row the worker claims next would read."""
    await _seed(sessionmaker)
    order_id = await _add_order(sessionmaker, "пить воду")
    async with sessionmaker() as session:
        row = await checkin.submit(
            session,
            clock,
            TIMEZONE,
            rating=4,
            due_result=checkin.PARTIAL,
            order_results=[(order_id, checkin.DONE)],
            note="заметка",
            message_id=-777,
        )
    assert (row.day_rating, row.due_result, row.note, row.tg_message_id) == (
        4, "partial", "заметка", -777,
    )
    assert row.local_date == _today()
    state = await _state(sessionmaker)
    assert (state.awaiting, state.awaiting_ref) == (None, None)
    assert state.streak == 0, "submit never finishes"
    async with sessionmaker() as session:
        assert await orders.results_for_checkin(session, row.id) == [("пить воду", "done")]


async def test_submit_on_the_same_day_overwrites(sessionmaker, clock):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        first = await checkin.submit(
            session, clock, TIMEZONE, rating=2, due_result=checkin.NONE,
            order_results=[], note="старое", message_id=-1,
        )
        second = await checkin.submit(
            session, clock, TIMEZONE, rating=5, due_result=checkin.NONE,
            order_results=[], note=None, message_id=-2,
        )
    assert second.id == first.id
    assert (second.day_rating, second.note, second.tg_message_id) == (5, None, -2)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rating": 0},
        {"rating": 6},
        {"rating": True},
        {"due_result": "maybe"},
        {"order_results": [(1, "partial")]},
    ],
)
async def test_submit_rejects_bad_values_before_writing(sessionmaker, clock, kwargs):
    await _seed(sessionmaker)
    args = {
        "rating": 3, "due_result": checkin.NONE, "order_results": [], "note": None, "message_id": -1,
    }
    args.update(kwargs)
    async with sessionmaker() as session:
        with pytest.raises(ValueError):
            await checkin.submit(session, clock, TIMEZONE, **args)
        assert (await session.execute(select(Checkin))).scalars().all() == []


async def test_finish_submitted_finishes_only_the_named_checkin_once(sessionmaker, clock):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        await checkin.submit(
            session, clock, TIMEZONE, rating=3, due_result=checkin.NONE,
            order_results=[], note=None, message_id=-5,
        )
        assert await checkin.finish_submitted(session, clock, TIMEZONE, -6) == (None, 0)
        row, streak = await checkin.finish_submitted(session, clock, TIMEZONE, -5)
        assert row is not None and streak == 1
        assert row.tg_message_id is None
        # A replayed completion finds nothing left to finish.
        assert await checkin.finish_submitted(session, clock, TIMEZONE, -5) == (None, 0)
    assert (await _state(sessionmaker)).last_checkin_at is not None


async def test_note_is_pause_word():
    assert checkin.note_is_pause_word("жёлтый") is True
    assert checkin.note_is_pause_word("пурпурный") is True
    assert checkin.note_is_pause_word("устал, но сделал") is False
    assert checkin.note_is_pause_word(None) is False


async def test_form_orders_on_a_redo_skip_answered_orders_and_respect_the_cap(sessionmaker, clock):
    """Review fix: a same-day redo keeps the day's order answers (and an
    answered `once` order is already retired), so the web form must ask
    exactly what Telegram's `next_due_order` walk would -- here nothing,
    since two answers already fill a cap of 2."""
    await _seed(sessionmaker)
    once = await _add_order(sessionmaker, "разово", cadence="once")
    daily_a = await _add_order(sessionmaker, "ежедневно а")
    daily_b = await _add_order(sessionmaker, "ежедневно б")
    async with sessionmaker() as session:
        assert [o.id for o in await checkin.form_orders(session, clock, TIMEZONE, 2)] == [once, daily_a]
        row = await checkin.submit(
            session, clock, TIMEZONE, rating=3, due_result=checkin.NONE,
            order_results=[(once, checkin.DONE), (daily_a, checkin.NO)], note=None, message_id=-1,
        )
        assert await checkin.form_orders(session, clock, TIMEZONE, 2) == []
        assert await orders.next_due_order(session, row.id, _today(), 2) is None
        # With room left under the cap, only the unanswered order is asked.
        assert [o.id for o in await checkin.form_orders(session, clock, TIMEZONE, 3)] == [daily_b]
        assert (await orders.next_due_order(session, row.id, _today(), 3)).id == daily_b


async def test_list_range_is_inclusive_and_ascending(sessionmaker):
    base = datetime.date(2026, 3, 10)
    async with sessionmaker() as session:
        for offset in (3, 0, 1, 5):
            session.add(Checkin(local_date=base + datetime.timedelta(days=offset)))
        await session.commit()
        rows = await checkin.list_range(
            session, base, base + datetime.timedelta(days=3)
        )
    assert [r.local_date.day for r in rows] == [10, 11, 13]


async def test_results_for_checkins_batches_with_the_same_ordering(sessionmaker):
    await _seed(sessionmaker)
    first = await _add_order(sessionmaker, "первое")
    second = await _add_order(sessionmaker, "второе")
    async with sessionmaker() as session:
        a = Checkin(local_date=datetime.date(2026, 3, 1))
        b = Checkin(local_date=datetime.date(2026, 3, 2))
        c = Checkin(local_date=datetime.date(2026, 3, 3))
        session.add_all([a, b, c])
        await session.commit()
        session.add_all(
            [
                CheckinOrderResult(checkin_id=a.id, order_id=second, result="no"),
                CheckinOrderResult(checkin_id=a.id, order_id=first, result="done"),
                CheckinOrderResult(checkin_id=b.id, order_id=first, result="no"),
            ]
        )
        await session.commit()
        batched = await orders.results_for_checkins(session, [a.id, b.id, c.id])
        singles = {i: await orders.results_for_checkin(session, i) for i in (a.id, b.id)}
        assert await orders.results_for_checkins(session, []) == {}
    assert batched == singles
    assert batched[a.id] == [("первое", "done"), ("второе", "no")]
    assert c.id not in batched


async def test_list_journal_is_newest_first_with_a_total(sessionmaker):
    from app.core import journal
    from app.db.models import Journal

    async with sessionmaker() as session:
        session.add_all(
            [
                Journal(local_date=datetime.date(2026, 3, 1), text="a"),
                Journal(local_date=datetime.date(2026, 3, 3), text="b"),
                Journal(local_date=datetime.date(2026, 3, 3), text="c"),
                Journal(local_date=datetime.date(2026, 3, 2), text="d"),
            ]
        )
        await session.commit()
        rows, total = await journal.list_journal(session, 0, 3)
        rest, total2 = await journal.list_journal(session, 3, 3)
    assert [r.text for r in rows] == ["c", "b", "d"]
    assert [r.text for r in rest] == ["a"]
    assert total == total2 == 4
