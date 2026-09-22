"""/delete (phase-2 plan sections 11 and 14, build rule 7).

Rule 7 is "/delete must really delete", and the two tests that enforce
it are not the behavioural ones -- they are the invariants at the bottom
of this file. A behavioural test proves today's tables get wiped; the
invariants fail the day someone adds a table or a column and forgets
this code exists, which is the only day it matters.
"""

from __future__ import annotations

import datetime
import decimal
import time

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy import func, select

from app.config import Settings
from app.core import purge
from app.db.models import (
    Base,
    Checkin,
    Job,
    Journal,
    Memory,
    Outbound,
    Message,
    PendingMemory,
    PersonaVersion,
    Proposal,
    Scene,
    SpendLedger,
    StateChange,
    TelegramUpdate,
    UserState,
)
from app.tg import data as data_ui
from app.tg.router import build_router
from conftest import FakeLLMProvider, FakeSession

pytestmark = pytest.mark.asyncio

CHAT_ID = 555
TIMEZONE = "Europe/Paris"


def _command_update(update_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": CHAT_ID, "type": "private"},
            "from": {"id": CHAT_ID, "is_bot": False, "first_name": "Test"},
            "text": text,
            "entities": [{"type": "bot_command", "offset": 0, "length": len(text)}],
        },
    }


def _callback_update(update_id: int, data: str, *, message_id: int = 1) -> dict:
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
                "text": data_ui.CONFIRM_TEXT,
            },
        },
    }


def _build_dp(sessionmaker):
    fake = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake)
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, Settings(), FakeLLMProvider(), FakeLLMProvider()))
    return dp, bot, fake


async def _seed_everything(sessionmaker, *update_ids: int) -> None:
    """One row in every purgeable table, plus a dirtied user_state."""
    now = datetime.datetime.now(datetime.timezone.utc)
    today = datetime.date.today()
    async with sessionmaker() as session:
        session.add(
            UserState(
                id=1, chat_id=CHAT_ID, timezone="America/New_York",
                persona_active=False, intensity=5, focus_on=True, focus_since=now,
                due_action="сдать отчёт", due_set_at=now, streak=9,
                last_checkin_at=now, awaiting="checkin_note", awaiting_ref=1,
            )
        )
        session.add(PersonaVersion(sha256="abc123", body="# Anchor"))
        await session.commit()
        for update_id in (1, *update_ids):
            session.add(TelegramUpdate(update_id=update_id, payload={}))
        await session.commit()
        scene = Scene(started_at=now, ended_at=now, summary="сводка")
        session.add(scene)
        await session.commit()
        await session.refresh(scene)
        session.add_all(
            [
                Message(role="user", content="текст", ooc=False, kind="chat",
                        update_id=1, scene_id=scene.id),
                Memory(kind="identity", text="факт", source="user"),
                PendingMemory(text="незавершённая заметка"),
                Checkin(local_date=today, day_rating=4),
                Proposal(field="due_action", value="что-то"),
                Journal(local_date=today, text="запись"),
                StateChange(field="intensity", old_value="3", new_value="5", source="command"),
                SpendLedger(local_date=today, category="chat", usd_cost=decimal.Decimal("0.01")),
                Job(kind="extract", payload={}, dedup_key="extract:1"),
                # 3a: a proactive message is a record of what the bot
                # said to this user, so "delete all my data" has to take
                # it too (phase-3 plan section 4).
                Outbound(
                    kind="morning",
                    local_date=today,
                    bucket=0,
                    planned_for=now,
                    status="sent",
                    sent_at=now,
                ),
            ]
        )
        await session.commit()


async def _counts(sessionmaker) -> dict[str, int]:
    async with sessionmaker() as session:
        out = {}
        for name, table in Base.metadata.tables.items():
            out[name] = (
                await session.execute(select(func.count()).select_from(table))
            ).scalar_one()
        return out


# --- the wipe ---


async def test_delete_wipes_every_purged_table(sessionmaker, clock):
    await _seed_everything(sessionmaker)
    before = await _counts(sessionmaker)
    assert all(before[name] > 0 for name in purge.PURGED_TABLES), "fixture must seed everything"

    async with sessionmaker() as session:
        await purge.delete_everything(session, Settings(), clock)

    after = await _counts(sessionmaker)
    for name in purge.PURGED_TABLES:
        if name == "state_change":
            # One deliberate audit row records that the wipe happened.
            assert after[name] == 1, name
        else:
            assert after[name] == 0, f"{name} survived the wipe"


async def test_pending_memory_is_wiped_though_the_plan_omits_it(sessionmaker, clock):
    """It holds text typed at /remember and never classified. Leaving it
    behind after "delete all my data" is the bug rule 7 names."""
    await _seed_everything(sessionmaker)
    async with sessionmaker() as session:
        await purge.delete_everything(session, Settings(), clock)
        rows = (await session.execute(select(PendingMemory))).scalars().all()
    assert rows == []


async def test_persona_version_survives(sessionmaker, clock):
    """Plan section 11: "Keep persona_version". It is a hash of a file in
    the repo, not user data."""
    await _seed_everything(sessionmaker)
    async with sessionmaker() as session:
        await purge.delete_everything(session, Settings(), clock)
        rows = (await session.execute(select(PersonaVersion))).scalars().all()
    assert len(rows) == 1


async def test_user_state_is_reset_but_keeps_chat_id(sessionmaker, clock):
    await _seed_everything(sessionmaker)
    settings = Settings(TZ_DEFAULT=TIMEZONE)

    async with sessionmaker() as session:
        await purge.delete_everything(session, settings, clock)

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)

    assert state is not None, "the row must be reset in place, never dropped"
    assert state.chat_id == CHAT_ID
    assert state.persona_active is True
    assert state.intensity == 3
    assert state.timezone == TIMEZONE
    assert state.focus_on is False and state.focus_since is None
    assert state.due_action is None and state.due_set_at is None
    assert state.streak == 0 and state.last_checkin_at is None
    assert state.awaiting is None and state.awaiting_ref is None


async def test_ids_restart_so_the_first_new_row_is_one(sessionmaker, clock):
    """After "delete everything", /memories showing #47 would be a lie."""
    await _seed_everything(sessionmaker)
    async with sessionmaker() as session:
        for i in range(5):
            session.add(Memory(kind="event", text=f"факт номер {i}", source="user"))
        await session.commit()
        await purge.delete_everything(session, Settings(), clock)

    async with sessionmaker() as session:
        row = Memory(kind="identity", text="первый новый факт", source="user")
        session.add(row)
        await session.commit()
        await session.refresh(row)
    assert row.id == 1


async def test_the_audit_row_records_the_fact_and_no_content(sessionmaker, clock):
    await _seed_everything(sessionmaker)
    async with sessionmaker() as session:
        await purge.delete_everything(session, Settings(), clock)
        rows = (await session.execute(select(StateChange))).scalars().all()

    assert len(rows) == 1
    audit = rows[0]
    assert audit.field == "data"
    assert audit.new_value == "deleted"
    assert audit.old_value is None
    for value in (audit.field, audit.old_value, audit.new_value, audit.source):
        assert value is None or "факт" not in value


async def test_the_bot_still_works_after_a_wipe(sessionmaker, clock):
    """get_state raises on a missing row, so a delete that dropped
    user_state would crash every later message."""
    from app.core.state import get_state

    await _seed_everything(sessionmaker)
    async with sessionmaker() as session:
        await purge.delete_everything(session, Settings(), clock)
    async with sessionmaker() as session:
        state = await get_state(session)
    assert state.chat_id == CHAT_ID


# --- the two-step confirmation ---


async def test_the_command_alone_deletes_nothing(sessionmaker):
    """Plan section 14: "the second step needs the button"."""
    await _seed_everything(sessionmaker, 2)
    dp, bot, fake = _build_dp(sessionmaker)

    await dp.feed_update(
        bot, Update.model_validate(_command_update(2, "/delete"), context={"bot": bot})
    )

    assert fake.sent[-1].text == data_ui.CONFIRM_TEXT
    labels = [b.text for r in fake.sent[-1].reply_markup.inline_keyboard for b in r]
    assert labels == [data_ui.CONFIRM_YES, data_ui.CONFIRM_NO]

    after = await _counts(sessionmaker)
    assert after["memory"] == 1, "nothing may be deleted before the button"


async def test_confirming_wipes_everything(sessionmaker):
    await _seed_everything(sessionmaker, 2, 3)
    dp, bot, fake = _build_dp(sessionmaker)

    await dp.feed_update(
        bot, Update.model_validate(_command_update(2, "/delete"), context={"bot": bot})
    )
    issued = int(time.time())
    await dp.feed_update(
        bot,
        Update.model_validate(_callback_update(3, f"d:yes:{issued}"), context={"bot": bot}),
    )

    after = await _counts(sessionmaker)
    assert after["memory"] == 0
    assert after["message"] == 0
    assert fake.edits[-1].text == data_ui.DELETED_TEXT
    assert fake.edits[-1].reply_markup is None


async def test_the_confirmation_names_the_real_provider(sessionmaker):
    """Plan section 11's text says copies sit with xAI for 30 days. The
    bot moved to OpenRouter in 1e and runs data_collection=deny, so both
    halves stopped being true."""
    assert "xAI" not in data_ui.DELETED_TEXT
    assert "30" not in data_ui.DELETED_TEXT
    assert "OpenRouter" in data_ui.DELETED_TEXT


async def test_cancelling_deletes_nothing(sessionmaker):
    await _seed_everything(sessionmaker, 2, 3)
    dp, bot, fake = _build_dp(sessionmaker)

    await dp.feed_update(
        bot, Update.model_validate(_command_update(2, "/delete"), context={"bot": bot})
    )
    await dp.feed_update(
        bot, Update.model_validate(_callback_update(3, "d:no"), context={"bot": bot})
    )

    after = await _counts(sessionmaker)
    assert after["memory"] == 1
    assert fake.edits[-1].text == data_ui.CANCELLED_TEXT
    assert fake.edits[-1].reply_markup is None


async def test_a_stale_button_deletes_nothing(sessionmaker):
    """A [Да, удалить] left in scrollback must not wipe everything when
    tapped by accident weeks later."""
    await _seed_everything(sessionmaker, 2)
    dp, bot, fake = _build_dp(sessionmaker)

    old = int(time.time()) - data_ui.CONFIRM_TTL - 1
    await dp.feed_update(
        bot, Update.model_validate(_callback_update(2, f"d:yes:{old}"), context={"bot": bot})
    )

    after = await _counts(sessionmaker)
    assert after["memory"] == 1
    assert fake.edits[-1].text == data_ui.STALE_TEXT
    assert len(fake.answered) == 1, "even a stale press is answered"


async def test_a_malformed_confirmation_deletes_nothing(sessionmaker):
    await _seed_everything(sessionmaker, 2)
    dp, bot, fake = _build_dp(sessionmaker)

    await dp.feed_update(
        bot, Update.model_validate(_callback_update(2, "d:yes:notanumber"), context={"bot": bot})
    )

    assert (await _counts(sessionmaker))["memory"] == 1
    assert fake.edits[-1].text == data_ui.STALE_TEXT


async def test_a_replayed_confirmation_is_harmless(sessionmaker):
    """The wipe destroys the message rows the usual replay gate reads,
    so idempotency comes from the wipe being repeatable instead."""
    await _seed_everything(sessionmaker, 2, 3)
    dp, bot, fake = _build_dp(sessionmaker)

    issued = int(time.time())
    for _ in range(2):
        await dp.feed_update(
            bot,
            Update.model_validate(_callback_update(3, f"d:yes:{issued}"), context={"bot": bot}),
        )

    after = await _counts(sessionmaker)
    assert after["memory"] == 0
    # Exactly one, not two: the second wipe truncates the first wipe's
    # audit row before writing its own. However many times it runs, the
    # log afterwards says "everything was deleted" once.
    assert after["state_change"] == 1


async def test_is_fresh_window():
    now = 1_000_000
    assert data_ui.is_fresh(now, now=now) is True
    assert data_ui.is_fresh(now - data_ui.CONFIRM_TTL + 1, now=now) is True
    assert data_ui.is_fresh(now - data_ui.CONFIRM_TTL - 1, now=now) is False
    # A button from the future is not fresh either -- that is a clock
    # problem, not a licence to wipe.
    assert data_ui.is_fresh(now + 60, now=now) is False


# --- THE invariants (build rule 7) ---


async def test_every_table_is_either_purged_or_deliberately_kept():
    """The one that matters. A table added in phase 3 must force a
    decision rather than silently surviving a delete."""
    covered = set(purge.PURGED_TABLES) | set(purge.KEPT_TABLES)
    assert set(Base.metadata.tables) == covered, (
        "a table is neither purged nor kept: "
        f"{set(Base.metadata.tables) ^ covered}"
    )


async def test_every_user_state_column_is_preserved_or_reset(clock):
    """A column added later must not silently keep its value through a
    wipe."""
    columns = {c.name for c in UserState.__table__.columns}
    handled = set(purge.PRESERVED_STATE_COLUMNS) | set(purge.reset_values(Settings(), clock))
    assert columns == handled, f"unhandled user_state columns: {columns ^ handled}"
