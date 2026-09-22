"""Proposal buttons: accept, reject, idempotence (plan sections 8 and 14).

The accept path is the *only* code in the repo that writes due_action or
focus_on from a proposal, and it is reachable only from a button press.
These tests pin both halves: that the button works, and that nothing
else can do what it does.
"""

from __future__ import annotations

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy import select

from app.config import Settings
from app.core import proposal
from app.db.models import Memory, Proposal, StateChange, TelegramUpdate, UserState
from app.tg import proposals as proposals_ui
from app.tg.router import build_router
from conftest import FakeLLMProvider, FakeSession

pytestmark = pytest.mark.asyncio

TEST_CHAT_ID = 555


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
                "text": "Записать?",
            },
        },
    }


def _build_dp(sessionmaker) -> tuple[Dispatcher, Bot, FakeSession]:
    fake = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake)
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, Settings(), FakeLLMProvider()))
    return dp, bot, fake


async def _seed(sessionmaker, *update_ids: int) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=TEST_CHAT_ID, timezone="Europe/Paris"))
        await session.commit()
        for update_id in update_ids:
            session.add(TelegramUpdate(update_id=update_id, payload={}))
        await session.commit()


async def _make(sessionmaker, field: str, value: str) -> int:
    async with sessionmaker() as session:
        created, _ = await proposal.create(session, field=field, value=value, reason="потому что")
        return created.id


async def _feed(dp, bot, payload: dict) -> None:
    await dp.feed_update(bot, Update.model_validate(payload, context={"bot": bot}))


# --- accept ---


async def test_accepting_a_due_action_sets_the_state(sessionmaker):
    await _seed(sessionmaker, 1)
    pid = await _make(sessionmaker, "due_action", "сдать отчёт до пятницы")
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _callback_update(1, f"p:a:{pid}"))

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
        row = await session.get(Proposal, pid)

    assert state.due_action == "сдать отчёт до пятницы"
    assert state.due_set_at is not None
    assert row.status == "accepted"
    assert row.decided_at is not None
    assert len(fake.answered) == 1
    assert proposals_ui.ACCEPTED_TEXT in fake.edits[0].text
    assert fake.edits[0].reply_markup is None, "buttons must be removed"


async def test_accepting_focus_on_parses_the_value(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    on_id = await _make(sessionmaker, "focus_on", "on")
    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _callback_update(1, f"p:a:{on_id}"))

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
    assert state.focus_on is True
    assert state.focus_since is not None

    off_id = await _make(sessionmaker, "focus_on", "off")
    await _feed(dp, bot, _callback_update(2, f"p:a:{off_id}"))

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
    assert state.focus_on is False
    assert state.focus_since is None


async def test_an_ambiguous_focus_value_reads_as_off(sessionmaker):
    """Focus raises pressure, so an unparseable value must not switch it on."""
    await _seed(sessionmaker, 1)
    pid = await _make(sessionmaker, "focus_on", "может быть")
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _callback_update(1, f"p:a:{pid}"))

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
    assert state.focus_on is False


async def test_accepting_a_rule_writes_a_memory_with_source_user(sessionmaker):
    """Plan section 8: the user pressed the button, so the rule is theirs."""
    await _seed(sessionmaker, 1)
    pid = await _make(sessionmaker, "rule", "не работать по воскресеньям")
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _callback_update(1, f"p:a:{pid}"))

    async with sessionmaker() as session:
        rows = (await session.execute(select(Memory))).scalars().all()
    assert len(rows) == 1
    assert rows[0].kind == "rule"
    assert rows[0].source == "user"


async def test_accept_writes_state_change_with_source_button(sessionmaker):
    await _seed(sessionmaker, 1)
    pid = await _make(sessionmaker, "due_action", "сдать отчёт")
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _callback_update(1, f"p:a:{pid}"))

    async with sessionmaker() as session:
        changes = (
            (await session.execute(select(StateChange).where(StateChange.source == "button")))
            .scalars().all()
        )
    assert {c.field for c in changes} == {"due_action", "due_set_at"}


# --- reject ---


async def test_rejecting_changes_no_state(sessionmaker):
    await _seed(sessionmaker, 1)
    pid = await _make(sessionmaker, "due_action", "сдать отчёт до пятницы")
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _callback_update(1, f"p:r:{pid}"))

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
        row = await session.get(Proposal, pid)

    assert state.due_action is None
    assert row.status == "rejected"
    assert proposals_ui.REJECTED_TEXT in fake.edits[0].text


# --- idempotence and stale buttons ---


async def test_a_replayed_accept_applies_once(sessionmaker):
    await _seed(sessionmaker, 1)
    pid = await _make(sessionmaker, "rule", "не работать по воскресеньям")
    dp, bot, fake = _build_dp(sessionmaker)

    for _ in range(2):
        await _feed(dp, bot, _callback_update(1, f"p:a:{pid}"))

    async with sessionmaker() as session:
        rows = (await session.execute(select(Memory))).scalars().all()
    assert len(rows) == 1
    assert len(fake.answered) == 2, "every press is answered, even a stale one"


async def test_accepting_after_rejecting_does_nothing(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    pid = await _make(sessionmaker, "due_action", "сдать отчёт")
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _callback_update(1, f"p:r:{pid}"))
    await _feed(dp, bot, _callback_update(2, f"p:a:{pid}"))

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
        row = await session.get(Proposal, pid)

    assert state.due_action is None
    assert row.status == "rejected", "the first decision stands"


async def test_an_expired_proposal_cannot_be_accepted(sessionmaker):
    await _seed(sessionmaker, 1)
    first = await _make(sessionmaker, "due_action", "старое действие")
    await _make(sessionmaker, "due_action", "новое действие")  # expires `first`
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _callback_update(1, f"p:a:{first}"))

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
        row = await session.get(Proposal, first)

    assert state.due_action is None
    assert row.status == "expired"
    assert proposals_ui.STALE in fake.edits[0].text
    assert fake.edits[0].reply_markup is None


async def test_a_callback_for_a_missing_proposal_is_answered(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _callback_update(1, "p:a:999999"))

    assert len(fake.answered) == 1
    assert fake.edits[0].text == proposals_ui.STALE


async def test_a_malformed_callback_id_is_answered(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _callback_update(1, "p:a:notanumber"))

    assert len(fake.answered) == 1


# --- the confirmation message itself ---


async def test_the_confirmation_message_is_not_in_character(sessionmaker):
    """Plan section 8's wording: short, plain, not Anchor's voice."""
    text = proposals_ui.confirm_text("due_action", "сдать отчёт до пятницы")
    assert text == "Записать? Главное действие: «сдать отчёт до пятницы»"


async def test_send_proposal_records_the_message_id(sessionmaker):
    """Needed so an expired proposal's buttons can be edited away."""
    await _seed(sessionmaker)
    pid = await _make(sessionmaker, "due_action", "сдать отчёт")
    fake = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake)

    await proposals_ui.send_proposal(
        sessionmaker, bot, chat_id=TEST_CHAT_ID, proposal_id=pid
    )

    async with sessionmaker() as session:
        row = await session.get(Proposal, pid)
    assert row.tg_message_id is not None
    labels = [b.text for r in fake.sent[0].reply_markup.inline_keyboard for b in r]
    assert labels == [proposals_ui.ACCEPT, proposals_ui.REJECT]


async def test_sending_a_new_proposal_retires_the_old_ones_buttons(sessionmaker):
    """Plan section 8: "mark the old one expired and edit its buttons away"."""
    await _seed(sessionmaker)
    fake = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake)

    first = await _make(sessionmaker, "due_action", "старое действие")
    await proposals_ui.send_proposal(sessionmaker, bot, chat_id=TEST_CHAT_ID, proposal_id=first)

    async with sessionmaker() as session:
        second_row, expired = await proposal.create(
            session, field="due_action", value="новое действие", reason=None
        )
        second, expired_id = second_row.id, expired.id

    await proposals_ui.send_proposal(
        sessionmaker, bot, chat_id=TEST_CHAT_ID, proposal_id=second, expired_id=expired_id
    )

    assert len(fake.edits) == 1, "the old proposal's message was edited"
    assert fake.edits[0].reply_markup is None, "its buttons are gone"
    assert proposals_ui.STALE in fake.edits[0].text
