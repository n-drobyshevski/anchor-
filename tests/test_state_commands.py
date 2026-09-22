"""/due, /focus and the extended /state (plan sections 9 and 11).

/due and /focus are the direct counterparts to the proposals 2c can only
suggest: the same two user_state fields, written immediately because the
user typed them rather than because a model proposed them.
"""

from __future__ import annotations

import datetime
import decimal

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy import select

from app.config import Settings
from app.core import memory, proposal
from app.db.models import Proposal, SpendLedger, StateChange, TelegramUpdate, UserState
from app.tg import proposals as proposals_ui
from app.tg.router import DUE_CLEARED, DUE_SET, FOCUS_OFF, FOCUS_ON, FOCUS_USAGE, build_router
from conftest import FakeLLMProvider, FakeSession

pytestmark = pytest.mark.asyncio

TEST_CHAT_ID = 555
TIMEZONE = "Europe/Paris"


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


def _build_dp(sessionmaker, settings=None):
    fake = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake)
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, settings or Settings(), FakeLLMProvider()))
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


# --- /due ---


async def test_due_sets_the_action_and_timestamp(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/due сдать отчёт до пятницы"))

    state = await _state(sessionmaker)
    assert state.due_action == "сдать отчёт до пятницы"
    assert state.due_set_at is not None
    assert fake.sent[0].text == DUE_SET.format(text="сдать отчёт до пятницы")


async def test_due_with_no_text_clears(sessionmaker):
    await _seed(
        sessionmaker, 1,
        due_action="старое", due_set_at=datetime.datetime.now(datetime.timezone.utc),
    )
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/due"))

    state = await _state(sessionmaker)
    assert state.due_action is None
    assert state.due_set_at is None
    assert fake.sent[0].text == DUE_CLEARED


async def test_due_writes_state_change_with_source_command(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/due сдать отчёт"))

    async with sessionmaker() as session:
        rows = (
            (await session.execute(select(StateChange).where(StateChange.source == "command")))
            .scalars().all()
        )
    assert {r.field for r in rows} >= {"due_action", "due_set_at"}


async def test_due_expires_a_pending_proposal_for_the_same_field(sessionmaker):
    """A direct command outranks an outstanding suggestion -- otherwise a
    live Принять would later overwrite what the user just typed."""
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        created, _ = await proposal.create(
            session, field="due_action", value="что-то другое", reason=None
        )
        proposal_id = created.id
        await proposal.set_message_id(session, proposal_id, 900)

    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/due сдать отчёт"))

    async with sessionmaker() as session:
        row = await session.get(Proposal, proposal_id)
    assert row.status == "expired"
    assert any(proposals_ui.STALE in edit.text for edit in fake.edits)
    assert fake.edits[0].reply_markup is None


async def test_due_leaves_a_proposal_for_a_different_field_alone(sessionmaker):
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        created, _ = await proposal.create(session, field="focus_on", value="on", reason=None)
        proposal_id = created.id

    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/due сдать отчёт"))

    async with sessionmaker() as session:
        row = await session.get(Proposal, proposal_id)
    assert row.status == "pending"


# --- /focus ---


@pytest.mark.parametrize("arg", ["on", "вкл"])
async def test_focus_on(sessionmaker, arg):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, f"/focus {arg}"))

    state = await _state(sessionmaker)
    assert state.focus_on is True
    assert state.focus_since is not None
    assert fake.sent[0].text == FOCUS_ON


async def test_focus_off_clears_focus_since(sessionmaker):
    await _seed(
        sessionmaker, 1,
        focus_on=True, focus_since=datetime.datetime.now(datetime.timezone.utc),
    )
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/focus off"))

    state = await _state(sessionmaker)
    assert state.focus_on is False
    assert state.focus_since is None
    assert fake.sent[0].text == FOCUS_OFF


async def test_focus_without_an_argument_explains(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/focus"))

    assert fake.sent[0].text == FOCUS_USAGE
    assert (await _state(sessionmaker)).focus_on is False


async def test_focus_with_nonsense_explains_rather_than_guessing(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/focus может быть"))

    assert fake.sent[0].text == FOCUS_USAGE
    assert (await _state(sessionmaker)).focus_on is False


async def test_focus_expires_a_pending_focus_proposal(sessionmaker):
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        created, _ = await proposal.create(session, field="focus_on", value="on", reason=None)
        proposal_id = created.id
        await proposal.set_message_id(session, proposal_id, 900)

    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/focus off"))

    async with sessionmaker() as session:
        row = await session.get(Proposal, proposal_id)
    assert row.status == "expired"


# --- /state (plan section 11) ---


async def test_state_shows_every_phase_two_field(sessionmaker):
    now = datetime.datetime.now(datetime.timezone.utc)
    await _seed(
        sessionmaker, 1,
        intensity=4, focus_on=True, streak=6,
        last_checkin_at=now - datetime.timedelta(days=1),
        due_action="сдать отчёт", due_set_at=now - datetime.timedelta(days=2),
    )
    async with sessionmaker() as session:
        await memory.write_memory(
            session, kind="identity", text="пользователь живёт в Лилле", source="user"
        )
        session.add(
            SpendLedger(
                local_date=datetime.datetime.now(datetime.timezone.utc).date(),
                category="chat",
                usd_cost=decimal.Decimal("0.020000"),
            )
        )
        session.add(
            SpendLedger(
                local_date=datetime.datetime.now(datetime.timezone.utc).date(),
                category="extractor",
                usd_cost=decimal.Decimal("0.010000"),
            )
        )
        await session.commit()

    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/state"))
    text = fake.sent[0].text

    # Phase 1's fields still there -- tests/test_commands.py depends on these.
    assert "вкл" in text
    assert "4/5" in text
    assert TIMEZONE in text
    # Plan section 11's additions.
    assert "Фокус: вкл" in text
    assert "Серия: 6 дн." in text
    assert "вчера" in text
    assert "«сдать отчёт»" in text
    assert "Помню: 1 записей" in text
    assert "chat 0.02" in text
    assert "extractor 0.01" in text


async def test_state_with_nothing_set_reads_cleanly(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/state"))
    text = fake.sent[0].text

    assert "Серия: 0 дн." in text
    assert "Последний чек-ин: давно" in text
    assert "Главное действие: нет" in text
    assert "Помню: 0 записей" in text


async def test_spend_by_category_sums_per_category(sessionmaker):
    from app.core.spend import today_by_category
    from app.core.spend import local_date_for

    await _seed(sessionmaker)
    async with sessionmaker() as session:
        for category, cost in (("chat", "0.02"), ("chat", "0.03"), ("summary", "0.01")):
            session.add(
                SpendLedger(
                    local_date=local_date_for(TIMEZONE),
                    category=category,
                    usd_cost=decimal.Decimal(cost),
                )
            )
        await session.commit()
        totals = await today_by_category(session, TIMEZONE)

    assert totals == {"chat": decimal.Decimal("0.050000"), "summary": decimal.Decimal("0.010000")}
    assert list(totals) == ["chat", "summary"], "largest first"
