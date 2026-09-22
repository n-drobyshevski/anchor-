"""The silence nudge, the back-off and the welfare cooldown (phase-3 plan sections 6, 11, 13).

These three are what stop a bot that can now speak first from becoming
a bot that will not stop. Each is already a row in the gate's truth
table (tests/test_outbound_gate.py); what is tested here is that the
heartbeat and the send path actually honour them end to end, against a
real database and a real clock.

The one worth reading is `test_three_unanswered_messages_then_silence`:
it walks the full loop the user would actually experience -- three
proactive messages over three days with no reply, then nothing at all,
then a single word from the user and the bot is back.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select

from app.config import Settings
from app.core.clock import FrozenClock, combine_local
from app.core.outbound import record_inbound
from app.core.outbound_gate import MORNING, SILENCE
from app.core.scheduler import heartbeat
from app.db.models import Outbound, UserState

TIMEZONE = "Europe/Paris"
CHAT_ID = 4242
DAY = datetime.date(2026, 9, 23)


def at(hour: int, minute: int = 0, day: datetime.date = DAY) -> FrozenClock:
    return FrozenClock(combine_local(day, datetime.time(hour, minute), TIMEZONE))


def settings(**overrides) -> Settings:
    base = dict(TZ_DEFAULT=TIMEZONE, JITTER_MAX_MIN=0, DAILY_USD_CAP=1.00)
    base.update(overrides)
    return Settings(_env_file=None, **base)


async def _seed(sessionmaker, **fields) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone=TIMEZONE, **fields))
        await session.commit()


async def _tick(sessionmaker, clock, cfg=None):
    async with sessionmaker() as session:
        return await heartbeat(session, cfg or settings(), clock)


async def _rows(sessionmaker) -> list[Outbound]:
    async with sessionmaker() as session:
        result = await session.execute(select(Outbound).order_by(Outbound.id))
        return list(result.scalars().all())


async def _mark_sent(sessionmaker, outbound_id: int, when) -> None:
    """Stand in for a delivered send, without the model or Telegram."""
    from app.core.outbound import record_outbound_sent

    async with sessionmaker() as session:
        row = await session.get(Outbound, outbound_id)
        row.status = "sent"
        row.sent_at = when
        await session.commit()
        await record_outbound_sent(session, FrozenClock(when))


# --- the silence nudge --------------------------------------------------


async def test_a_silence_nudge_is_planned_after_48h_with_focus_on(sessionmaker):
    silent_since = at(12, 0, DAY - datetime.timedelta(days=3)).now_utc()
    await _seed(sessionmaker, focus_on=True, last_user_msg_at=silent_since)

    assert await _tick(sessionmaker, at(14, 0)) is not None
    assert [row.kind for row in await _rows(sessionmaker)] == [SILENCE]


async def test_no_nudge_without_focus(sessionmaker):
    silent_since = at(12, 0, DAY - datetime.timedelta(days=3)).now_utc()
    await _seed(sessionmaker, focus_on=False, last_user_msg_at=silent_since)
    assert await _tick(sessionmaker, at(14, 0)) is None
    assert await _rows(sessionmaker) == []


async def test_no_nudge_before_48h(sessionmaker):
    silent_since = at(20, 0, DAY - datetime.timedelta(days=2)).now_utc()
    await _seed(sessionmaker, focus_on=True, last_user_msg_at=silent_since)
    # 47 hours later.
    assert await _tick(sessionmaker, at(19, 0)) is None
    assert await _rows(sessionmaker) == []


async def test_no_nudge_when_the_user_has_never_written(sessionmaker):
    """A fresh install, or a /delete reset. "Never wrote" must not read
    as "silent for infinity hours"."""
    await _seed(sessionmaker, focus_on=True, last_user_msg_at=None)
    assert await _tick(sessionmaker, at(14, 0)) is None


async def test_a_nudge_is_not_repeated_the_next_midnight(sessionmaker):
    """Dedup is (kind, local_date, bucket), which alone would allow a
    second nudge every midnight. The 48h rule is what keeps them
    apart (plan section 4)."""
    silent_since = at(12, 0, DAY - datetime.timedelta(days=3)).now_utc()
    await _seed(sessionmaker, focus_on=True, last_user_msg_at=silent_since)

    nudge_id = await _tick(sessionmaker, at(14, 0))
    await _mark_sent(sessionmaker, nudge_id, at(14, 0).now_utc())

    # Next local day, still silent. The local_date differs, so only the
    # 48h rule can stop it.
    tomorrow = DAY + datetime.timedelta(days=1)
    assert await _tick(sessionmaker, at(14, 0, tomorrow)) is None
    assert len(await _rows(sessionmaker)) == 1


async def test_the_nudge_is_considered_every_heartbeat_not_at_a_fixed_hour(sessionmaker):
    """Unlike the fixed intents it has no time window; it is gated
    entirely on elapsed silence."""
    silent_since = at(12, 0, DAY - datetime.timedelta(days=3)).now_utc()
    await _seed(sessionmaker, focus_on=True, last_user_msg_at=silent_since)
    assert await _tick(sessionmaker, at(13, 37)) is not None


async def test_a_fixed_intent_outranks_the_nudge_in_the_same_minute(sessionmaker):
    """The priority rule's real job: the nudge has no window, so
    without it the two would race for the same 09:00 tick."""
    silent_since = at(12, 0, DAY - datetime.timedelta(days=3)).now_utc()
    await _seed(sessionmaker, focus_on=True, last_user_msg_at=silent_since)

    await _tick(sessionmaker, at(9, 0))
    assert [row.kind for row in await _rows(sessionmaker)] == [MORNING]


async def test_the_nudge_is_not_planned_during_quiet_hours(sessionmaker):
    silent_since = at(12, 0, DAY - datetime.timedelta(days=3)).now_utc()
    await _seed(sessionmaker, focus_on=True, last_user_msg_at=silent_since)
    assert await _tick(sessionmaker, at(23, 30)) is None


async def test_the_nudge_jitter_cannot_cross_into_quiet_hours(sessionmaker):
    silent_since = at(12, 0, DAY - datetime.timedelta(days=3)).now_utc()
    await _seed(sessionmaker, focus_on=True, last_user_msg_at=silent_since)

    await _tick(sessionmaker, at(22, 25), settings(JITTER_MAX_MIN=15))
    (row,) = await _rows(sessionmaker)
    assert row.planned_for < combine_local(DAY, datetime.time(22, 30), TIMEZONE)


# --- the back-off -------------------------------------------------------


async def test_three_unanswered_messages_then_silence_then_recovery(sessionmaker):
    """Plan section 11, walked end to end.

    Three proactive messages with no reply, then nothing at all -- not
    even a fixed intent -- until the user writes one word.
    """
    await _seed(sessionmaker, last_user_msg_at=at(8, 0).now_utc())
    cfg = settings()

    for offset in range(3):
        day = DAY + datetime.timedelta(days=offset)
        planned = await _tick(sessionmaker, at(9, 0, day), cfg)
        assert planned is not None, f"day {offset} should still be allowed"
        await _mark_sent(sessionmaker, planned, at(9, 0, day).now_utc())

    async with sessionmaker() as session:
        assert (await session.get(UserState, 1)).ignored_in_row == 3

    # Day four: nothing, and nothing on the evening either.
    day_four = DAY + datetime.timedelta(days=3)
    assert await _tick(sessionmaker, at(9, 0, day_four), cfg) is None
    assert await _tick(sessionmaker, at(22, 0, day_four), cfg) is None
    assert len(await _rows(sessionmaker)) == 3

    # The user writes one word.
    async with sessionmaker() as session:
        await record_inbound(session, at(10, 0, day_four))

    # Same day is still blocked by min_gap, but the next morning works.
    day_five = DAY + datetime.timedelta(days=4)
    assert await _tick(sessionmaker, at(9, 0, day_five), cfg) is not None


async def test_an_unanswered_message_enforces_the_minimum_gap(sessionmaker):
    await _seed(sessionmaker, last_user_msg_at=at(8, 0).now_utc())
    morning = await _tick(sessionmaker, at(9, 0))
    await _mark_sent(sessionmaker, morning, at(9, 0).now_utc())

    # The evening nag is only 13 hours later, but MIN_GAP_UNANSWERED_H
    # is 8, so it is allowed. Tighten the gap and it is not.
    assert await _tick(sessionmaker, at(22, 0), settings(MIN_GAP_UNANSWERED_H=24)) is None
    assert await _tick(sessionmaker, at(22, 0)) is not None


async def test_an_answered_message_imposes_no_gap(sessionmaker):
    await _seed(sessionmaker, last_user_msg_at=at(8, 0).now_utc())
    morning = await _tick(sessionmaker, at(9, 0))
    await _mark_sent(sessionmaker, morning, at(9, 0).now_utc())

    async with sessionmaker() as session:
        await record_inbound(session, at(9, 30))

    assert await _tick(sessionmaker, at(22, 0), settings(MIN_GAP_UNANSWERED_H=24)) is not None


async def test_the_daily_budget_caps_unsolicited_messages(sessionmaker):
    """At most MAX_UNSOLICITED_PER_DAY, all kinds together."""
    silent_since = at(12, 0, DAY - datetime.timedelta(days=3)).now_utc()
    await _seed(sessionmaker, focus_on=True, last_user_msg_at=silent_since)
    cfg = settings(MAX_UNSOLICITED_PER_DAY=1)

    nudge = await _tick(sessionmaker, at(8, 30), cfg)
    await _mark_sent(sessionmaker, nudge, at(8, 30).now_utc())

    assert await _tick(sessionmaker, at(9, 0), cfg) is None


# --- the welfare cooldown -----------------------------------------------


@pytest.mark.parametrize("hours", [0, 5, 23])
async def test_the_discretionary_kinds_wait_out_the_welfare_cooldown(
    sessionmaker, hours
):
    triggered = at(14, 0, DAY - datetime.timedelta(days=1)).now_utc()
    silent_since = at(12, 0, DAY - datetime.timedelta(days=4)).now_utc()
    await _seed(
        sessionmaker,
        focus_on=True,
        last_user_msg_at=silent_since,
        welfare_at=triggered,
    )
    clock = FrozenClock(triggered + datetime.timedelta(hours=hours))
    assert await _tick(sessionmaker, clock) is None


async def test_the_nudge_returns_once_the_cooldown_expires(sessionmaker):
    triggered = at(14, 0, DAY - datetime.timedelta(days=1)).now_utc()
    silent_since = at(12, 0, DAY - datetime.timedelta(days=4)).now_utc()
    await _seed(
        sessionmaker,
        focus_on=True,
        last_user_msg_at=silent_since,
        welfare_at=triggered,
    )
    clock = FrozenClock(triggered + datetime.timedelta(hours=25))
    assert await _tick(sessionmaker, clock) is not None
    assert [row.kind for row in await _rows(sessionmaker)] == [SILENCE]


async def test_the_agreed_routine_resumes_with_the_persona(sessionmaker):
    """Morning and evening are not held back by the cooldown -- they
    are part of what the user signed up for, and they resume the
    moment the persona does."""
    triggered = at(14, 0, DAY - datetime.timedelta(days=1)).now_utc()
    await _seed(
        sessionmaker,
        last_user_msg_at=at(8, 0).now_utc(),
        welfare_at=triggered,
        persona_active=True,
    )
    assert await _tick(sessionmaker, at(9, 0)) is not None
    assert [row.kind for row in await _rows(sessionmaker)] == [MORNING]


async def test_nothing_is_planned_while_the_persona_is_off(sessionmaker):
    """The cooldown is the *second* line. The first is that a welfare
    trigger switches the persona off, and nothing unsolicited goes out
    while it is (plan section 11)."""
    triggered = at(14, 0).now_utc()
    await _seed(
        sessionmaker,
        last_user_msg_at=at(8, 0).now_utc(),
        welfare_at=triggered,
        persona_active=False,
    )
    tomorrow = DAY + datetime.timedelta(days=1)
    assert await _tick(sessionmaker, at(9, 0, tomorrow)) is None
    assert await _tick(sessionmaker, at(22, 0, tomorrow)) is None
