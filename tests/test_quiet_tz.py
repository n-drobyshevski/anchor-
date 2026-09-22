"""/quiet and /tz (phase-3 plan sections 10 and 13).

The parser is pure, so its grammar is a table. The handlers are tested
through the real router and dispatcher, the same way /due and /focus
are in tests/test_state_commands.py -- a command that parses correctly
but never reaches `update_state` would pass a parser test and fail the
user.

The property worth stating: **`/quiet` both blocks and cancels.** The
gate's `quiet_cmd` check stops new planning, and `cancel_outbound`
revokes a message already sitting in the queue with its jitter
running. Either one alone leaves a hole, and the hole is the case
that matters -- `/quiet` typed at 08:55 when the morning message is
already planned for 09:07.
"""

from __future__ import annotations

import datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy import select

from app.config import Settings
from app.core.clock import FrozenClock, combine_local
from app.core.quiet import OFF, clamp, parse
from app.db.models import Outbound, StateChange, TelegramUpdate, UserState
from app.tg.router import (
    QUIET_CLAMPED,
    QUIET_OFF_REPLY,
    QUIET_USAGE,
    TZ_UNKNOWN,
    TZ_USAGE,
    build_router,
)
from conftest import FakeLLMProvider, FakeSession

CHAT_ID = 555
TIMEZONE = "Europe/Paris"
DAY = datetime.date(2026, 9, 23)


def at(hour: int, minute: int = 0) -> FrozenClock:
    return FrozenClock(combine_local(DAY, datetime.time(hour, minute), TIMEZONE))


def _command_update(update_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": CHAT_ID, "type": "private"},
            "from": {"id": CHAT_ID, "is_bot": False, "first_name": "Test"},
            "text": text,
            "entities": [
                {
                    "type": "bot_command",
                    "offset": 0,
                    "length": len(text.split(" ", 1)[0]),
                }
            ],
        },
    }


def _build(sessionmaker, clock, settings=None):
    fake = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake)
    dp = Dispatcher()
    dp.include_router(
        build_router(
            sessionmaker,
            settings or Settings(_env_file=None, TZ_DEFAULT=TIMEZONE),
            FakeLLMProvider(),
            None,
            clock,
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


async def _feed(dp, bot, payload) -> None:
    await dp.feed_update(bot, Update.model_validate(payload, context={"bot": bot}))


async def _state(sessionmaker) -> UserState:
    async with sessionmaker() as session:
        return await session.get(UserState, 1)


# --- the parser --------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("30m", datetime.timedelta(minutes=30)),
        ("2h", datetime.timedelta(hours=2)),
        ("1d", datetime.timedelta(days=1)),
        ("3D", datetime.timedelta(days=3)),
        ("  2h  ", datetime.timedelta(hours=2)),
        # Russian suffixes: every other string in this bot is Russian.
        ("30м", datetime.timedelta(minutes=30)),
        ("4ч", datetime.timedelta(hours=4)),
        ("2д", datetime.timedelta(days=2)),
        # A bare number has exactly one sensible reading.
        ("45", datetime.timedelta(minutes=45)),
    ],
)
def test_the_parser_reads_a_duration(raw, expected):
    assert parse(raw) == expected


@pytest.mark.parametrize("raw", ["off", "OFF", "выкл", "0", "0m", " off "])
def test_the_parser_reads_off(raw):
    assert parse(raw) == OFF


@pytest.mark.parametrize("raw", ["", "  ", "abc", "2w", "h", "-3h", "2 hours", "1.5h"])
def test_the_parser_rejects_nonsense(raw):
    assert parse(raw) is None


def test_over_the_maximum_is_clamped_not_rejected():
    """"/quiet 30d" means "not for a long time". Answering with a usage
    error would leave the bot talking, which is the opposite."""
    assert clamp(datetime.timedelta(days=30), 7) == datetime.timedelta(days=7)
    assert clamp(datetime.timedelta(days=2), 7) == datetime.timedelta(days=2)


# --- /quiet ------------------------------------------------------------


async def test_quiet_sets_quiet_until_and_audits_it(sessionmaker):
    await _seed(sessionmaker, 1)
    clock = at(8, 55)
    dp, bot, fake = _build(sessionmaker, clock)

    await _feed(dp, bot, _command_update(1, "/quiet 2h"))

    state = await _state(sessionmaker)
    assert state.quiet_until == clock.now_utc() + datetime.timedelta(hours=2)

    async with sessionmaker() as session:
        changes = list((await session.execute(select(StateChange))).scalars())
    assert [c.field for c in changes] == ["quiet_until"]
    assert changes[0].source == "command"

    assert fake.sent[0].text.startswith("Тихо до ")


async def test_quiet_reports_the_end_time_in_local_time(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build(sessionmaker, at(8, 55))
    await _feed(dp, bot, _command_update(1, "/quiet 2h"))
    # 08:55 + 2h = 10:55 local on the user's clock, not UTC's.
    assert "10:55" in fake.sent[0].text


async def test_quiet_cancels_what_is_already_planned(sessionmaker):
    """The case that matters: /quiet at 08:55 when the morning message
    is already sitting in the queue with its jitter running."""
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        session.add(
            Outbound(
                kind="morning",
                local_date=DAY,
                bucket=0,
                planned_for=combine_local(DAY, datetime.time(9, 7), TIMEZONE),
                status="planned",
            )
        )
        await session.commit()

    dp, bot, _ = _build(sessionmaker, at(8, 55))
    await _feed(dp, bot, _command_update(1, "/quiet 2h"))

    async with sessionmaker() as session:
        rows = list((await session.execute(select(Outbound))).scalars())
    assert [row.status for row in rows] == ["cancelled"]


async def test_quiet_over_the_max_is_clamped_and_says_so(sessionmaker):
    await _seed(sessionmaker, 1)
    clock = at(9, 0)
    dp, bot, fake = _build(sessionmaker, clock)

    await _feed(dp, bot, _command_update(1, "/quiet 30d"))

    state = await _state(sessionmaker)
    assert state.quiet_until == clock.now_utc() + datetime.timedelta(days=7)
    assert fake.sent[0].text.startswith("Тихо до ")
    assert "7" in fake.sent[0].text
    assert QUIET_CLAMPED.split("{")[0] in fake.sent[0].text


async def test_quiet_off_clears_it(sessionmaker):
    clock = at(9, 0)
    await _seed(
        sessionmaker, 1, quiet_until=clock.now_utc() + datetime.timedelta(hours=5)
    )
    dp, bot, fake = _build(sessionmaker, clock)

    await _feed(dp, bot, _command_update(1, "/quiet off"))

    assert (await _state(sessionmaker)).quiet_until is None
    assert fake.sent[0].text == QUIET_OFF_REPLY


async def test_quiet_off_does_not_cancel_anything(sessionmaker):
    """Turning quiet *off* is not a reason to revoke a planned message;
    it is a reason to let one through."""
    await _seed(sessionmaker, 1)
    async with sessionmaker() as session:
        session.add(
            Outbound(
                kind="morning",
                local_date=DAY,
                bucket=0,
                planned_for=combine_local(DAY, datetime.time(9, 7), TIMEZONE),
                status="planned",
            )
        )
        await session.commit()

    dp, bot, _ = _build(sessionmaker, at(9, 0))
    await _feed(dp, bot, _command_update(1, "/quiet off"))

    async with sessionmaker() as session:
        rows = list((await session.execute(select(Outbound))).scalars())
    assert [row.status for row in rows] == ["planned"]


@pytest.mark.parametrize("text", ["/quiet", "/quiet abc", "/quiet 2w"])
async def test_quiet_with_nonsense_explains_and_changes_nothing(sessionmaker, text):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build(sessionmaker, at(9, 0))

    await _feed(dp, bot, _command_update(1, text))

    assert (await _state(sessionmaker)).quiet_until is None
    assert fake.sent[0].text == QUIET_USAGE
    async with sessionmaker() as session:
        assert list((await session.execute(select(StateChange))).scalars()) == []


# --- /tz ---------------------------------------------------------------


async def test_tz_sets_the_zone_and_reports_the_local_time(sessionmaker):
    await _seed(sessionmaker, 1)
    clock = at(9, 0)  # 07:00 UTC
    dp, bot, fake = _build(sessionmaker, clock)

    await _feed(dp, bot, _command_update(1, "/tz America/New_York"))

    assert (await _state(sessionmaker)).timezone == "America/New_York"
    assert "America/New_York" in fake.sent[0].text
    assert "03:00" in fake.sent[0].text  # 07:00 UTC in New York, EDT


async def test_tz_is_audited(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, _ = _build(sessionmaker, at(9, 0))
    await _feed(dp, bot, _command_update(1, "/tz Asia/Tokyo"))

    async with sessionmaker() as session:
        changes = list((await session.execute(select(StateChange))).scalars())
    assert [(c.field, c.new_value, c.source) for c in changes] == [
        ("timezone", "Asia/Tokyo", "command")
    ]


@pytest.mark.parametrize(
    "bad", ["Europe/Atlantis", "GMT+3", "москва", "../../etc/passwd", "Europe"]
)
async def test_tz_rejects_an_unknown_zone_without_changing_anything(sessionmaker, bad):
    """Validated by constructing the ZoneInfo, not by matching a
    pattern: an unknown-but-plausible name is exactly the input that
    would otherwise crash every local-time computation afterwards."""
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build(sessionmaker, at(9, 0))

    await _feed(dp, bot, _command_update(1, f"/tz {bad}"))

    assert (await _state(sessionmaker)).timezone == TIMEZONE
    assert fake.sent[0].text == TZ_UNKNOWN


async def test_tz_with_no_argument_explains(sessionmaker):
    await _seed(sessionmaker, 1)
    dp, bot, fake = _build(sessionmaker, at(9, 0))
    await _feed(dp, bot, _command_update(1, "/tz"))
    assert fake.sent[0].text == TZ_USAGE
    assert (await _state(sessionmaker)).timezone == TIMEZONE


async def test_changing_the_zone_moves_the_fixed_intents(sessionmaker):
    """Plan section 15's acceptance item: /tz America/New_York shifts
    morning and evening to New York local time."""
    from app.core.scheduler import heartbeat

    await _seed(sessionmaker, 1)
    dp, bot, _ = _build(sessionmaker, at(9, 0))
    await _feed(dp, bot, _command_update(1, "/tz America/New_York"))

    settings = Settings(_env_file=None, JITTER_MAX_MIN=0)
    # 09:00 Paris is 03:00 in New York -- too early for the morning
    # message there.
    async with sessionmaker() as session:
        assert await heartbeat(session, settings, at(9, 0)) is None
    # 15:00 Paris is 09:00 in New York.
    async with sessionmaker() as session:
        assert await heartbeat(session, settings, at(15, 0)) is not None


# --- the extended /state (plan section 10) -----------------------------


async def _state_text(sessionmaker, clock) -> str:
    dp, bot, fake = _build(sessionmaker, clock)
    await _feed(dp, bot, _command_update(99, "/state"))
    return fake.sent[-1].text


async def test_state_shows_nothing_yet_when_nothing_is_planned(sessionmaker):
    await _seed(sessionmaker, 99)
    text = await _state_text(sessionmaker, at(12, 0))
    assert "Тихо до: —" in text
    assert "Без ответа подряд: 0" in text
    assert "Сам написал сегодня: 0 / 3" in text
    assert "Следующее: —" in text
    assert "Последний отказ: —" in text


async def test_state_shows_quiet_until_in_local_time(sessionmaker):
    clock = at(9, 0)
    await _seed(
        sessionmaker, 99, quiet_until=clock.now_utc() + datetime.timedelta(hours=2)
    )
    assert "Тихо до: 23.09 11:00" in await _state_text(sessionmaker, clock)


async def test_an_expired_quiet_is_not_shown_as_live(sessionmaker):
    clock = at(9, 0)
    await _seed(
        sessionmaker, 99, quiet_until=clock.now_utc() - datetime.timedelta(minutes=1)
    )
    assert "Тихо до: —" in await _state_text(sessionmaker, clock)


async def test_state_shows_the_next_planned_message(sessionmaker):
    await _seed(sessionmaker, 99)
    async with sessionmaker() as session:
        session.add(
            Outbound(
                kind="evening_nag",
                local_date=DAY,
                bucket=0,
                planned_for=combine_local(DAY, datetime.time(22, 7), TIMEZONE),
                status="planned",
            )
        )
        await session.commit()
    assert "Следующее: вечер в 22:07" in await _state_text(sessionmaker, at(12, 0))


async def test_state_counts_what_was_actually_sent_today(sessionmaker):
    await _seed(sessionmaker, 99, ignored_in_row=2)
    async with sessionmaker() as session:
        session.add_all(
            [
                Outbound(
                    kind="morning",
                    local_date=DAY,
                    bucket=0,
                    planned_for=combine_local(DAY, datetime.time(9, 0), TIMEZONE),
                    status="sent",
                    sent_at=at(9, 0).now_utc(),
                ),
                Outbound(
                    kind="silence",
                    local_date=DAY,
                    bucket=0,
                    planned_for=combine_local(DAY, datetime.time(14, 0), TIMEZONE),
                    status="cancelled",
                ),
            ]
        )
        await session.commit()

    text = await _state_text(sessionmaker, at(15, 0))
    assert "Сам написал сегодня: 1 / 3" in text
    assert "Без ответа подряд: 2" in text


async def test_state_shows_todays_last_refusal(sessionmaker):
    """The reason code is the whole point of the gate recording one --
    "why didn't it write?" should be answerable without the logs."""
    await _seed(sessionmaker, 99)
    async with sessionmaker() as session:
        session.add(
            Outbound(
                kind="evening_nag",
                local_date=DAY,
                bucket=0,
                planned_for=combine_local(DAY, datetime.time(22, 0), TIMEZONE),
                status="skipped",
                skip_reason="kind_rule:checkin_done",
            )
        )
        await session.commit()
    assert "Последний отказ: kind_rule:checkin_done" in await _state_text(
        sessionmaker, at(23, 0)
    )


async def test_a_cancelled_message_is_visible_in_state(sessionmaker):
    """Plan section 15: «пурпурный» while a message is planned -> it
    never arrives, and /state shows it as cancelled or skipped."""
    await _seed(sessionmaker, 1, 99)
    async with sessionmaker() as session:
        session.add(
            Outbound(
                kind="morning",
                local_date=DAY,
                bucket=0,
                planned_for=combine_local(DAY, datetime.time(9, 7), TIMEZONE),
                status="planned",
            )
        )
        await session.commit()

    dp, bot, _ = _build(sessionmaker, at(8, 55))
    await _feed(dp, bot, _command_update(1, "/quiet 2h"))

    text = await _state_text(sessionmaker, at(9, 0))
    assert "Следующее: —" in text, "a cancelled row is no longer upcoming"
