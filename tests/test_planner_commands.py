"""The `/task`, `/event`, `/done` commands and their buttons (P3).

Router-level tests, following tests/test_research_commands.py's and
tests/test_proposals.py's shape: real Update payloads through
build_router()'s dispatcher.
"""

from __future__ import annotations

import datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy import select

from app.config import Settings
from app.db.models import Job, Message, PlannerSnapshot, TelegramUpdate, UserState
from app.planner import actions as planner_actions
from app.planner import auth as planner_auth
from app.planner.jobs import PLANNER_WRITE
from app.tg import planner as planner_ui
from app.tg.router import BOT_COMMANDS, build_router
from conftest import FakeLLMProvider, FakeSession

pytestmark = pytest.mark.asyncio

TEST_CHAT_ID = 555
TZ = "Europe/Paris"


def _clock(frozen_clock):
    return frozen_clock(2026, 9, 23, 9, 0, tz=TZ)


def _command_update(update_id: int, text: str) -> dict:
    command_len = len(text.split(" ", 1)[0])
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": TEST_CHAT_ID, "type": "private"},
            "from": {"id": TEST_CHAT_ID, "is_bot": False, "first_name": "Test"},
            "text": text,
            "entities": [{"type": "bot_command", "offset": 0, "length": command_len}],
        },
    }


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
                "text": "…",
            },
        },
    }


def _build_dp(sessionmaker, settings: Settings, clock=None) -> tuple[Dispatcher, Bot, FakeSession]:
    fake = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake)
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, settings, FakeLLMProvider(), clock=clock))
    return dp, bot, fake


async def _seed(sessionmaker, *update_ids: int) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=TEST_CHAT_ID, timezone=TZ))
        await session.commit()
        for update_id in update_ids:
            session.add(TelegramUpdate(update_id=update_id, payload={}))
        await session.commit()


async def _feed(dp, bot, payload: dict) -> None:
    await dp.feed_update(bot, Update.model_validate(payload, context={"bot": bot}))


def _settings(**overrides) -> Settings:
    base = dict(PLANNER_ENABLED=True, PLANNER_MAX_WRITES_PER_DAY=20)
    base.update(overrides)
    return Settings(_env_file=None, **base)


async def test_the_planner_write_commands_are_registered() -> None:
    names = {c.command for c in BOT_COMMANDS}
    assert {"task", "event", "done"} <= names


@pytest.mark.parametrize("text", ["/task купить молоко", "/event встреча в 18:00", "/done"])
async def test_write_commands_refuse_when_planner_disabled(sessionmaker, text):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, _settings(PLANNER_ENABLED=False))
    await _feed(dp, bot, _command_update(1, text))
    assert fake.sent[0].text == planner_ui.DISABLED


# --- /task -------------------------------------------------------------


async def test_task_without_args_shows_usage(sessionmaker, frozen_clock):
    clock = _clock(frozen_clock)
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, _settings(), clock=clock)
    await _feed(dp, bot, _command_update(1, "/task"))
    assert fake.sent[0].text == planner_ui.TASK_USAGE


async def test_task_creates_a_pending_action_and_sends_a_confirm_card(sessionmaker, frozen_clock):
    clock = _clock(frozen_clock)
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, _settings(), clock=clock)

    await _feed(dp, bot, _command_update(1, "/task Купить молоко на завтра"))

    assert "Купить молоко" in fake.sent[-1].text
    assert fake.sent[-1].reply_markup is not None

    async with sessionmaker() as session:
        result = await session.execute(select(planner_actions.PlannerAction))
        rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].kind == planner_actions.CREATE_TASK
    assert rows[0].status == planner_actions.PENDING
    assert rows[0].payload["due_date"] == "2026-09-24"


async def test_tapping_yes_enqueues_exactly_one_write_job(sessionmaker, frozen_clock):
    clock = _clock(frozen_clock)
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        action = await planner_actions.create(
            session, clock, kind=planner_actions.CREATE_TASK,
            payload={"title": "Купить молоко", "due_date": None},
        )

    dp, bot, fake = _build_dp(sessionmaker, _settings(), clock=clock)
    await _feed(dp, bot, _callback_update(2, f"pa:y:{action.id}"))

    assert fake.answered  # the button was acknowledged
    assert planner_ui.ACTION_ACCEPTED in fake.edits[-1].text

    async with sessionmaker() as session:
        rows = (await session.execute(select(Job).where(Job.kind == PLANNER_WRITE))).scalars().all()
    assert len(rows) == 1
    assert rows[0].payload == {"planner_action_id": action.id}


async def test_a_replayed_yes_tap_enqueues_no_second_job(sessionmaker, frozen_clock):
    clock = _clock(frozen_clock)
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        action = await planner_actions.create(
            session, clock, kind=planner_actions.CREATE_TASK,
            payload={"title": "A", "due_date": None},
        )

    dp, bot, fake = _build_dp(sessionmaker, _settings(), clock=clock)
    await _feed(dp, bot, _callback_update(2, f"pa:y:{action.id}"))
    await _feed(dp, bot, _callback_update(3, f"pa:y:{action.id}"))

    assert planner_ui.ACTION_STALE in fake.edits[-1].text
    async with sessionmaker() as session:
        rows = (await session.execute(select(Job).where(Job.kind == PLANNER_WRITE))).scalars().all()
    assert len(rows) == 1


async def test_tapping_no_rejects_and_enqueues_nothing(sessionmaker, frozen_clock):
    clock = _clock(frozen_clock)
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        action = await planner_actions.create(
            session, clock, kind=planner_actions.CREATE_TASK,
            payload={"title": "A", "due_date": None},
        )

    dp, bot, fake = _build_dp(sessionmaker, _settings(), clock=clock)
    await _feed(dp, bot, _callback_update(2, f"pa:n:{action.id}"))

    assert planner_ui.ACTION_REJECTED in fake.edits[-1].text
    async with sessionmaker() as session:
        row = await planner_actions.get(session, action.id)
        rows = (await session.execute(select(Job).where(Job.kind == PLANNER_WRITE))).scalars().all()
    assert row.status == planner_actions.REJECTED
    assert rows == []


async def test_task_stops_at_the_daily_write_cap(sessionmaker, frozen_clock):
    clock = _clock(frozen_clock)
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        for i in range(2):
            await planner_actions.create(
                session, clock, kind=planner_actions.CREATE_TASK, payload={"title": str(i), "due_date": None}
            )

    dp, bot, fake = _build_dp(sessionmaker, _settings(PLANNER_MAX_WRITES_PER_DAY=2), clock=clock)
    await _feed(dp, bot, _command_update(1, "/task ещё одна задача"))

    assert "лимит" in fake.sent[-1].text.lower() or "планер" in fake.sent[-1].text.lower()
    async with sessionmaker() as session:
        rows = (await session.execute(select(planner_actions.PlannerAction))).scalars().all()
    assert len(rows) == 2  # the third was never created


# --- /event --------------------------------------------------------------


async def test_event_creates_a_pending_action_with_a_confirm_card(sessionmaker, frozen_clock):
    clock = _clock(frozen_clock)
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker, _settings(), clock=clock)

    await _feed(dp, bot, _command_update(1, "/event Встреча с Аней в 18:00 завтра"))

    assert "Встреча с Аней" in fake.sent[-1].text
    async with sessionmaker() as session:
        rows = (await session.execute(select(planner_actions.PlannerAction))).scalars().all()
    assert len(rows) == 1
    assert rows[0].kind == planner_actions.CREATE_EVENT


# --- /done -----------------------------------------------------------------


async def test_done_with_no_open_tasks_says_so(sessionmaker, frozen_clock):
    clock = _clock(frozen_clock)
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        session.add(
            PlannerSnapshot(id=1, fetched_at=clock.now_utc(), payload={"events": [], "tasks": []})
        )
        await session.commit()

    dp, bot, fake = _build_dp(sessionmaker, _settings(), clock=clock)
    await _feed(dp, bot, _command_update(1, "/done"))

    assert fake.sent[-1].text == planner_ui.DONE_EMPTY


async def test_done_lists_tasks_and_tapping_one_completes_it(sessionmaker, frozen_clock):
    clock = _clock(frozen_clock)
    await _seed(sessionmaker, 1, 2)
    async with sessionmaker() as session:
        session.add(
            PlannerSnapshot(
                id=1, fetched_at=clock.now_utc(),
                payload={"events": [], "tasks": [{"id": "abc", "title": "Купить молоко", "dueDate": None, "overdue": False, "status": "todo"}]},
            )
        )
        await session.commit()

    dp, bot, fake = _build_dp(sessionmaker, _settings(), clock=clock)
    await _feed(dp, bot, _command_update(1, "/done"))
    assert fake.sent[-1].text == planner_ui.DONE_HEADER
    assert fake.sent[-1].reply_markup is not None

    await _feed(dp, bot, _callback_update(2, "pl:d:abc"))
    assert "Купить молоко" in fake.edits[-1].text

    async with sessionmaker() as session:
        rows = (await session.execute(select(planner_actions.PlannerAction))).scalars().all()
        jobs = (await session.execute(select(Job).where(Job.kind == PLANNER_WRITE))).scalars().all()
    assert len(rows) == 1
    assert rows[0].kind == planner_actions.COMPLETE_TASK
    assert rows[0].status == planner_actions.ACCEPTED
    assert rows[0].payload == {"task_id": "abc", "title": "Купить молоко"}
    assert len(jobs) == 1


# --- /planner_link -------------------------------------------------------
#
# Security fix: the live OAuth authorize URL must reach Telegram directly
# (message.answer) and never be stored as a `message` row, since
# app/web/tail.py's live mirror and GET /api/history show every sent
# assistant row with no kind filter -- storing it there would leak a
# 10-minute, still-usable PKCE link to anyone holding a web session.


async def test_planner_link_sends_the_url_but_never_stores_it(
    sessionmaker, frozen_clock, monkeypatch
):
    clock = _clock(frozen_clock)
    await _seed(sessionmaker, 1)

    async def fake_link_url(settings, clock):
        return "https://planner.example/oauth/authorize?state=SECRET-STATE-VALUE"

    monkeypatch.setattr(planner_auth, "link_url", fake_link_url)

    dp, bot, fake = _build_dp(sessionmaker, _settings(), clock=clock)
    await _feed(dp, bot, _command_update(1, "/planner_link"))

    # The real reply, with the live URL, goes straight to Telegram.
    assert "authorize?state=SECRET-STATE-VALUE" in fake.sent[-1].text

    # But nothing in the `message` table -- what the web tail and
    # /api/history read from -- contains the URL, "authorize", or the
    # state value. Only an innocuous placeholder is stored.
    async with sessionmaker() as session:
        rows = (await session.execute(select(Message))).scalars().all()
    assert rows, "mark_update_handled should have written a row"
    for row in rows:
        assert "authorize" not in row.content
        assert "SECRET-STATE-VALUE" not in row.content
    assert any(row.content == "[/planner_link]" for row in rows)
    assert any(row.sent_at is not None for row in rows)
