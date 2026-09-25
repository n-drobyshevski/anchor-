"""The debt queue (phase 5, spec 2026-09-25, slice 4).

Domain: app/core/obligations.py (cap, idempotent close, /due's focus
debt, the prompt lines, the missed-check-in sweep), the hooks that open
and close debts (commands.set_due, proposal.accept, checkin.finish),
and the Telegram side: `/paid`, `/paid N` and the `ob:*` buttons.
"""

from __future__ import annotations

import datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy import select

from app.config import Settings
from app.core import checkin, commands, obligations, proposal
from app.core import clock as clock_module
from app.db.models import Checkin, Obligation, Proposal, TelegramUpdate, UserState
from app.tg import obligations as obligations_ui
from app.tg import proposals as proposals_ui
from app.tg.router import BOT_COMMANDS, build_router
from conftest import FakeLLMProvider, FakeSession

pytestmark = pytest.mark.asyncio

TEST_CHAT_ID = 555
TIMEZONE = "Europe/Paris"


async def _seed_state(sessionmaker, *update_ids: int, **state) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=TEST_CHAT_ID, timezone=TIMEZONE, **state))
        await session.commit()
        for update_id in update_ids:
            session.add(TelegramUpdate(update_id=update_id, payload={}))
        await session.commit()


async def _open(sessionmaker, text: str, **kwargs) -> Obligation | None:
    async with sessionmaker() as session:
        return await obligations.open_(
            session,
            text=text,
            kind=kwargs.pop("kind", "promised"),
            source=kwargs.pop("source", "user"),
            **kwargs,
        )


async def _all(sessionmaker) -> list[Obligation]:
    async with sessionmaker() as session:
        result = await session.execute(select(Obligation).order_by(Obligation.id))
        return list(result.scalars().all())


# --- the domain -------------------------------------------------------------


async def test_open_caps_at_five_open_debts(sessionmaker):
    for n in range(obligations.MAX_OPEN):
        assert await _open(sessionmaker, f"долг {n}") is not None

    assert await _open(sessionmaker, "шестой") is None
    assert len(await _all(sessionmaker)) == obligations.MAX_OPEN


async def test_closing_one_frees_a_slot(sessionmaker, clock):
    rows = [await _open(sessionmaker, f"долг {n}") for n in range(obligations.MAX_OPEN)]
    async with sessionmaker() as session:
        await obligations.close(session, clock, rows[0].id)

    assert await _open(sessionmaker, "новый") is not None


async def test_open_rejects_empty_text_and_squashes_whitespace(sessionmaker):
    assert await _open(sessionmaker, "   ") is None
    row = await _open(sessionmaker, "  прислать \n отчёт  ")
    assert row.text == "прислать отчёт"


async def test_close_is_idempotent(sessionmaker, clock):
    row = await _open(sessionmaker, "прислать отчёт")
    async with sessionmaker() as session:
        first = await obligations.close(session, clock, row.id, obligations.DONE)
        second = await obligations.close(session, clock, row.id, obligations.DROPPED)

    assert first is not None and first.status == obligations.DONE
    assert first.closed_at is not None
    assert second is None
    assert (await _all(sessionmaker))[0].status == obligations.DONE


async def test_open_list_is_oldest_first_and_only_open(sessionmaker, clock):
    a = await _open(sessionmaker, "первый")
    b = await _open(sessionmaker, "второй")
    c = await _open(sessionmaker, "третий")
    async with sessionmaker() as session:
        await obligations.close(session, clock, b.id)
        rows = await obligations.open_list(session)
    assert [row.id for row in rows] == [a.id, c.id]


async def test_replace_focus_drops_the_old_focus_debt(sessionmaker, clock):
    async with sessionmaker() as session:
        first = await obligations.replace_focus(session, clock, "дописать главу")
        second = await obligations.replace_focus(session, clock, "отправить письмо")
        cleared = await obligations.replace_focus(session, clock, None)

    assert first is not None and second is not None and cleared is None
    rows = await _all(sessionmaker)
    assert [(row.text, row.status) for row in rows] == [
        ("дописать главу", obligations.DROPPED),
        ("отправить письмо", obligations.DROPPED),
    ]


def _row(text, opened_day, due=None):
    return Obligation(
        text=text,
        kind="promised",
        source="user",
        opened_at=datetime.datetime(2026, 9, opened_day, 10, tzinfo=datetime.timezone.utc),
        due_local_date=due,
    )


def test_prompt_lines_show_the_oldest_up_to_the_limit_and_mark_overdue():
    today = datetime.date(2026, 9, 25)
    rows = [
        _row("первый", 20, due=datetime.date(2026, 9, 24)),
        _row("второй", 21),
        _row("третий", 22, due=today),
        _row("четвёртый", 23),
    ]

    lines, overdue = obligations.prompt_lines(rows, today, 3)

    assert lines == [
        "«первый» (с 20.09, просрочено)",
        "«второй» (с 21.09)",
        "«третий» (с 22.09)",
    ]
    assert overdue is True


def test_prompt_lines_overdue_counts_rows_beyond_the_limit():
    today = datetime.date(2026, 9, 25)
    rows = [_row("а", 20), _row("б", 21), _row("в", 22, due=datetime.date(2026, 9, 1))]

    lines, overdue = obligations.prompt_lines(rows, today, 2)

    assert len(lines) == 2
    assert overdue is True


def test_prompt_lines_with_nothing_open():
    assert obligations.prompt_lines([], datetime.date(2026, 9, 25), 3) == ([], False)


# --- the missed-check-in sweep ------------------------------------------------


async def _add_checkin(sessionmaker, local_date) -> None:
    async with sessionmaker() as session:
        session.add(Checkin(local_date=local_date))
        await session.commit()


async def test_sweep_opens_a_checkin_debt_once(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 25, 0, 30, tz=TIMEZONE)
    today = clock_module.local_date(clock, TIMEZONE)
    await _add_checkin(sessionmaker, today - datetime.timedelta(days=3))

    async with sessionmaker() as session:
        first = await obligations.sweep_missed_checkin(session, clock, TIMEZONE)
        second = await obligations.sweep_missed_checkin(session, clock, TIMEZONE)

    assert first is not None
    assert first.kind == "checkin" and first.source == "checkin"
    assert first.due_local_date == today
    assert first.text == "чек-ин за 24.09"
    assert second is None
    assert len(await _all(sessionmaker)) == 1


async def test_sweep_skips_a_first_day_user(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 25, 0, 30, tz=TIMEZONE)
    async with sessionmaker() as session:
        assert await obligations.sweep_missed_checkin(session, clock, TIMEZONE) is None


async def test_sweep_skips_when_yesterday_was_done(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 25, 0, 30, tz=TIMEZONE)
    today = clock_module.local_date(clock, TIMEZONE)
    await _add_checkin(sessionmaker, today - datetime.timedelta(days=1))
    await _add_checkin(sessionmaker, today - datetime.timedelta(days=2))
    async with sessionmaker() as session:
        assert await obligations.sweep_missed_checkin(session, clock, TIMEZONE) is None


# --- the hooks ------------------------------------------------------------------


async def test_checkin_finish_closes_the_checkin_debt(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 25, 21, 0, tz=TIMEZONE)
    await _seed_state(sessionmaker)
    today = clock_module.local_date(clock, TIMEZONE)
    await _add_checkin(sessionmaker, today - datetime.timedelta(days=3))
    async with sessionmaker() as session:
        await obligations.sweep_missed_checkin(session, clock, TIMEZONE)
    other = await _open(sessionmaker, "прислать отчёт")
    await _add_checkin(sessionmaker, today)

    async with sessionmaker() as session:
        await checkin.finish(session, clock, TIMEZONE)

    by_kind = {row.kind: row.status for row in await _all(sessionmaker)}
    assert by_kind == {"checkin": obligations.DONE, "promised": obligations.OPEN}
    assert other is not None


async def test_set_due_opens_and_replaces_the_focus_debt(sessionmaker, clock):
    await _seed_state(sessionmaker)
    async with sessionmaker() as session:
        await commands.set_due(session, clock, "дописать главу", "command")
        await commands.set_due(session, clock, "отправить письмо", "command")
        rows = await obligations.open_list(session)
    assert [(row.kind, row.text, row.source) for row in rows] == [
        ("focus", "отправить письмо", "command")
    ]

    async with sessionmaker() as session:
        await commands.set_due(session, clock, "", "command")
        assert await obligations.open_list(session) == []


async def test_accepting_an_obligation_proposal_opens_a_promised_debt(sessionmaker, clock):
    await _seed_state(sessionmaker)
    async with sessionmaker() as session:
        created, _ = await proposal.create(
            session, clock, field=proposal.OBLIGATION, value="прислать отчёт", reason=None
        )
        assert await obligations.open_list(session) == []
        await proposal.accept(session, clock, created.id)
        rows = await obligations.open_list(session)
    assert [(row.kind, row.source, row.text) for row in rows] == [
        ("promised", "proposal", "прислать отчёт")
    ]


async def test_accepting_a_due_proposal_replaces_the_focus_debt(sessionmaker, clock):
    await _seed_state(sessionmaker)
    async with sessionmaker() as session:
        await commands.set_due(session, clock, "старое", "command")
        created, _ = await proposal.create(
            session, clock, field=proposal.DUE_ACTION, value="новое", reason=None
        )
        await proposal.accept(session, clock, created.id)
        rows = await obligations.open_list(session)
    assert [(row.kind, row.text) for row in rows] == [("focus", "новое")]


# --- Telegram: /paid, ob:*, the proposal cap ---------------------------------


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


def _build_dp(sessionmaker):
    fake_session = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake_session)
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, Settings(), FakeLLMProvider(text="Принято.")))
    return dp, bot, fake_session


async def _feed(dp, bot, payload: dict) -> None:
    await dp.feed_update(bot, Update.model_validate(payload, context={"bot": bot}))


def test_paid_is_in_the_command_menu():
    assert "paid" in [command.command for command in BOT_COMMANDS]


async def test_paid_with_no_debts(sessionmaker):
    await _seed_state(sessionmaker, 1)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/paid"))

    assert fake.sent[0].text == obligations_ui.EMPTY


async def test_paid_lists_open_debts_with_buttons(sessionmaker):
    await _seed_state(sessionmaker, 1)
    first = await _open(sessionmaker, "прислать отчёт")
    second = await _open(sessionmaker, "позвонить маме")
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/paid"))

    text = fake.sent[0].text
    assert "1. прислать отчёт" in text and "2. позвонить маме" in text
    data = [b.callback_data for r in fake.sent[0].reply_markup.inline_keyboard for b in r]
    assert data == [f"ob:d:{first.id}", f"ob:x:{first.id}", f"ob:d:{second.id}", f"ob:x:{second.id}"]


async def test_paid_n_closes_the_nth_debt(sessionmaker):
    await _seed_state(sessionmaker, 1)
    await _open(sessionmaker, "прислать отчёт")
    await _open(sessionmaker, "позвонить маме")
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/paid 2"))

    assert fake.sent[0].text == obligations_ui.CLOSED_TEXT.format(text="позвонить маме")
    statuses = [(row.text, row.status) for row in await _all(sessionmaker)]
    assert statuses == [("прислать отчёт", "open"), ("позвонить маме", "done")]


async def test_paid_n_is_replay_safe(sessionmaker):
    await _seed_state(sessionmaker, 1)
    await _open(sessionmaker, "первый")
    await _open(sessionmaker, "второй")
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/paid 1"))
    await _feed(dp, bot, _command_update(1, "/paid 1"))

    statuses = [row.status for row in await _all(sessionmaker)]
    assert statuses == ["done", "open"]


@pytest.mark.parametrize(
    ("arg", "expected"),
    [("abc", obligations_ui.USAGE), ("3", obligations_ui.OUT_OF_RANGE.format(n=3))],
)
async def test_paid_bad_argument(sessionmaker, arg, expected):
    await _seed_state(sessionmaker, 1)
    await _open(sessionmaker, "первый")
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, f"/paid {arg}"))

    assert fake.sent[0].text == expected


@pytest.mark.parametrize(("action", "status"), [("d", "done"), ("x", "dropped")])
async def test_ob_buttons_close_or_drop_and_rerender(sessionmaker, action, status):
    await _seed_state(sessionmaker)
    row = await _open(sessionmaker, "прислать отчёт")
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _callback_update(1, f"ob:{action}:{row.id}"))

    assert (await _all(sessionmaker))[0].status == status
    assert fake.answered
    assert "прислать отчёт" in fake.edits[-1].text
    assert obligations_ui.EMPTY in fake.edits[-1].text


async def test_ob_button_on_a_closed_debt_is_stale(sessionmaker, clock):
    await _seed_state(sessionmaker)
    row = await _open(sessionmaker, "прислать отчёт")
    async with sessionmaker() as session:
        await obligations.close(session, clock, row.id)
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _callback_update(1, f"ob:x:{row.id}"))

    assert fake.edits[-1].text.startswith(obligations_ui.STALE)
    assert (await _all(sessionmaker))[0].status == "done"


async def test_accepting_a_debt_proposal_at_the_cap_keeps_it_pending(sessionmaker, clock):
    await _seed_state(sessionmaker)
    for n in range(obligations.MAX_OPEN):
        await _open(sessionmaker, f"долг {n}")
    async with sessionmaker() as session:
        created, _ = await proposal.create(
            session, clock, field=proposal.OBLIGATION, value="шестой", reason=None
        )
    dp, bot, fake = _build_dp(sessionmaker)

    await _feed(dp, bot, _callback_update(1, f"p:a:{created.id}"))

    assert fake.answered[-1].text == proposals_ui.CAP_REACHED.format(max=obligations.MAX_OPEN)
    async with sessionmaker() as session:
        row = await session.get(Proposal, created.id)
    assert row.status == proposal.PENDING
    assert len(await _all(sessionmaker)) == obligations.MAX_OPEN
