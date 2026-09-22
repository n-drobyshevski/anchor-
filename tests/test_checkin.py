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
from app.core import checkin
from app.db.models import Checkin, Job, Message, StateChange, TelegramUpdate, UserState
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
    from app.core.spend import local_date_for

    return local_date_for(TIMEZONE)


async def _complete_checkin(sessionmaker, *, rating=4):
    async with sessionmaker() as session:
        row = await checkin.start(session, TIMEZONE)
        await checkin.set_rating(session, row.id, rating)
        await checkin.set_due_result(session, row.id, checkin.NONE)
        return await checkin.finish(session, TIMEZONE)


async def test_first_checkin_starts_the_streak_at_one(sessionmaker):
    await _seed(sessionmaker)
    _, streak = await _complete_checkin(sessionmaker)
    assert streak == 1
    assert (await _state(sessionmaker)).streak == 1


async def test_a_checkin_the_day_after_increments(sessionmaker):
    await _seed(sessionmaker, streak=1)
    async with sessionmaker() as session:
        session.add(Checkin(local_date=_today() - datetime.timedelta(days=1), day_rating=3))
        await session.commit()

    _, streak = await _complete_checkin(sessionmaker)
    assert streak == 2


async def test_a_gap_resets_the_streak_to_one(sessionmaker):
    await _seed(sessionmaker, streak=7)
    async with sessionmaker() as session:
        session.add(Checkin(local_date=_today() - datetime.timedelta(days=3), day_rating=3))
        await session.commit()

    _, streak = await _complete_checkin(sessionmaker)
    assert streak == 1


async def test_a_same_day_recheckin_does_not_change_the_streak(sessionmaker):
    """Plan section 9, and the reason finish() reads last_checkin_at
    rather than counting rows."""
    await _seed(sessionmaker, streak=1)
    async with sessionmaker() as session:
        session.add(Checkin(local_date=_today() - datetime.timedelta(days=1), day_rating=3))
        await session.commit()

    _, first = await _complete_checkin(sessionmaker)
    _, second = await _complete_checkin(sessionmaker)

    assert first == 2
    assert second == 2, "a redo of today must not bump the streak again"


async def test_finishing_writes_a_state_change_for_the_streak(sessionmaker):
    await _seed(sessionmaker)
    await _complete_checkin(sessionmaker)

    async with sessionmaker() as session:
        rows = (
            (await session.execute(select(StateChange).where(StateChange.field == "streak")))
            .scalars().all()
        )
    assert len(rows) == 1
    assert rows[0].new_value == "1"
    assert rows[0].source == "command"


async def test_a_second_checkin_the_same_day_overwrites_the_first(sessionmaker):
    """`local_date` is unique, so starting again resets the day's row."""
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        first = await checkin.start(session, TIMEZONE)
        await checkin.set_rating(session, first.id, 5)
        await checkin.set_note(session, first.id, "первая заметка")
        second = await checkin.start(session, TIMEZONE)

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


async def test_an_over_long_note_is_trimmed_to_the_column_limit(sessionmaker):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        row = await checkin.start(session, TIMEZONE)
        saved = await checkin.set_note(session, row.id, "я" * 600)
    assert len(saved.note) == checkin.NOTE_MAX
