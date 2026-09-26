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
from app.core import memory, proposal, safety_events
from app.core.clock import SystemClock
from app.core.clock import local_date as clock_local_date
from app.db.models import (
    IdleRun,
    Proposal,
    SafetyEvent,
    SpendLedger,
    StateChange,
    TelegramUpdate,
    UserState,
)
from app.tg import proposals as proposals_ui
from app.tg.router import DUE_CLEARED, DUE_SET, FOCUS_OFF, FOCUS_ON, FOCUS_USAGE, build_router
from conftest import FakeLLMProvider, FakeSession, flatten_rich_message

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


def _state_text(fake: FakeSession) -> str:
    """/state now sends a rich message (app/tg/state_view.py) rather
    than a plain one -- flatten it back to text so these assertions,
    lifted from before that change, keep reading the same substrings."""
    return flatten_rich_message(fake.rich[-1].rich_message)


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


async def test_due_expires_a_pending_proposal_for_the_same_field(sessionmaker, clock):
    """A direct command outranks an outstanding suggestion -- otherwise a
    live Принять would later overwrite what the user just typed."""
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        created, _ = await proposal.create(
            session, clock, field="due_action", value="что-то другое", reason=None
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


async def test_due_leaves_a_proposal_for_a_different_field_alone(sessionmaker, clock):
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        created, _ = await proposal.create(session, clock, field="focus_on", value="on", reason=None)
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


async def test_focus_expires_a_pending_focus_proposal(sessionmaker, clock):
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        created, _ = await proposal.create(session, clock, field="focus_on", value="on", reason=None)
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
    # /state sums spend for the user's local day; seeding the UTC date put
    # these rows on "yesterday" from 22:00 UTC (Paris is UTC+2).
    today = clock_local_date(SystemClock(), TIMEZONE)
    async with sessionmaker() as session:
        await memory.write_memory(
            session, kind="identity", text="пользователь живёт в Лилле", source="user"
        )
        session.add(
            SpendLedger(
                local_date=today,
                category="chat",
                usd_cost=decimal.Decimal("0.020000"),
            )
        )
        session.add(
            SpendLedger(
                local_date=today,
                category="extractor",
                usd_cost=decimal.Decimal("0.010000"),
            )
        )
        await session.commit()

    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/state"))
    text = _state_text(fake)

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


async def test_state_shows_the_welfare_check_health(sessionmaker):
    """H2's line. The point of it is the failure count: a welfare check
    that has silently stopped working is the one failure mode nothing
    else on this screen would show."""
    today = datetime.datetime.now(datetime.timezone.utc).date()
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        session.add_all(
            [
                SafetyEvent(local_date=today, kind="welfare", outcome="ok"),
                SafetyEvent(local_date=today, kind="welfare", outcome="ok"),
                SafetyEvent(local_date=today, kind="welfare", outcome="ok"),
                SafetyEvent(local_date=today, kind="welfare", outcome="timeout"),
                SafetyEvent(local_date=today, kind="welfare", outcome="parse_fail"),
                # Counted as neither: the backstop worked.
                SafetyEvent(local_date=today, kind="welfare", outcome="fallback_hit"),
                # A different check entirely.
                SafetyEvent(local_date=today, kind="tick", outcome="error"),
            ]
        )
        await session.commit()

    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/state"))
    assert "Проверка благополучия (7 дн.): ok 3 · сбои 2" in _state_text(fake)


async def test_state_reads_zero_when_no_check_has_run(sessionmaker):
    """A fresh install shows the line rather than hiding it -- an absent
    line and a healthy one would be indistinguishable."""
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/state"))
    assert "Проверка благополучия (7 дн.): ok 0 · сбои 0" in _state_text(fake)


async def test_state_with_nothing_set_reads_cleanly(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/state"))
    text = _state_text(fake)

    assert "Серия: 0 дн." in text
    assert "Последний чек-ин: давно" in text
    assert "Главное действие: нет" in text
    assert "Помню: 0 записей" in text


async def test_spend_by_category_sums_per_category(sessionmaker, clock):
    from app.core.spend import today_by_category
    from app.core.clock import SystemClock
    from app.core.clock import local_date as clock_local_date

    await _seed(sessionmaker)
    async with sessionmaker() as session:
        for category, cost in (("chat", "0.02"), ("chat", "0.03"), ("summary", "0.01")):
            session.add(
                SpendLedger(
                    local_date=clock_local_date(SystemClock(), TIMEZONE),
                    category=category,
                    usd_cost=decimal.Decimal(cost),
                )
            )
        await session.commit()
        totals = await today_by_category(session, clock, TIMEZONE)

    assert totals == {"chat": decimal.Decimal("0.050000"), "summary": decimal.Decimal("0.010000")}
    assert list(totals) == ["chat", "summary"], "largest first"


# --- the research line (4d fixes) ---------------------------------------


async def _record(sessionmaker, clock, kind: str, outcome: str, times: int = 1) -> None:
    for _ in range(times):
        await safety_events.record(
            sessionmaker, clock=clock, timezone=TIMEZONE, kind=kind, outcome=outcome
        )


async def test_state_has_no_research_line_before_any_research_ran(sessionmaker, clock):
    """Unlike the welfare line: the welfare check runs on ordinary turns,
    so zeroes there mean it has stopped. Research only runs when asked,
    so a permanent «0 · 0» would be noise for someone who never uses it."""
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/state"))

    assert "Исследования" not in _state_text(fake)


async def test_state_shows_the_research_line_once_there_is_something_to_show(
    sessionmaker, clock
):
    """The number that matters is the failures. A distiller returning
    unparseable JSON makes `done` jobs with no cards, which reads as a
    quiet week of unhelpful pages until this line says otherwise."""
    await _seed(sessionmaker, 1)
    await _record(sessionmaker, clock, safety_events.DISTILL, "ok", times=3)
    await _record(sessionmaker, clock, safety_events.DISTILL, safety_events.PARSE_FAIL, times=2)
    await _record(sessionmaker, clock, safety_events.SEARCH, "ok")
    await _record(sessionmaker, clock, safety_events.SEARCH, "error", times=4)

    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/state"))

    line = next(
        row for row in _state_text(fake).splitlines() if row.startswith("Исследования")
    )
    assert "разбор ok 3 · сбои 2" in line
    assert "поиск ok 1 · сбои 4" in line


async def test_the_research_line_does_not_disturb_the_welfare_line(sessionmaker, clock):
    """Two separate rollups over the same table; neither may count the
    other's rows."""
    await _seed(sessionmaker, 1)
    await _record(sessionmaker, clock, safety_events.DISTILL, safety_events.PARSE_FAIL, times=5)

    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/state"))

    welfare = next(
        row for row in _state_text(fake).splitlines()
        if row.startswith("Проверка благополучия")
    )
    assert "ok 0 · сбои 0" in welfare


# --- 6c: /state's canary line -------------------------------------------


async def test_state_shows_no_canary_line_before_any_canary_ran(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/state"))
    assert "Канарейка" not in _state_text(fake)


async def test_state_shows_the_latest_canary_ok(sessionmaker):
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        session.add(
            IdleRun(
                kind="canary", local_date=datetime.date(2026, 9, 23), status="done",
                summary={"cases": {"01": True}, "passed": True},
            )
        )
        await session.commit()
    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/state"))
    assert "Канарейка: 2026-09-23 ок" in _state_text(fake)


async def test_state_shows_the_latest_canary_regression(sessionmaker):
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        session.add(
            IdleRun(
                kind="canary", local_date=datetime.date(2026, 9, 23), status="done",
                summary={"cases": {"01": False}, "passed": False},
            )
        )
        await session.commit()
    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/state"))
    assert "Канарейка: 2026-09-23 ⚠️" in _state_text(fake)


async def test_state_shows_persona_version_idle_and_backup(sessionmaker):
    """Package A: after a deploy, /state is how the user checks parity --
    the persona file's version, the idle line and the backup line."""
    from app.core.prompt import load_persona, persona_path_for
    from app.db.models import BackupLog

    await _seed(sessionmaker, 1, streak=2)
    async with sessionmaker() as session:
        now = datetime.datetime.now(datetime.timezone.utc)
        session.add(
            BackupLog(started_at=now, finished_at=now, status="failed", error_code="not_configured")
        )
        await session.commit()

    dp, bot, fake = _build_dp(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/state"))
    text = _state_text(fake)

    sha = load_persona(persona_path_for(Settings()))[1][:8]
    assert f"Персона: вкл · v{sha}" in text
    assert "Серия: 2 дн." in text
    assert "Потрачено сегодня:" in text
    assert "Фон: 0.00 /" in text
    assert "Бэкап: ⚠️ ошибка" in text
