"""app/core/outbound_send.py tests (phase-3 plan sections 7 and 13).

The plan names three properties for the send path, and each is here:

- the **send-time gate re-check** (a check-in completed between
  planning and sending skips the evening nag with a `kind_rule` reason);
- **a crash between the insert and the send resends, it does not
  regenerate** -- the expensive step happens once, the cheap one
  repeats;
- **a failed generation sends nothing**, with no canned fallback.

Plus the cancel triggers (pause, welfare, /delete), the counters, and
the transcript rule -- an outbound message is in the persona transcript
and never enqueues the extractor.

Nothing here touches the network: FakeLLMProvider for the model,
FakeSession for Telegram.
"""

from __future__ import annotations

import datetime
import decimal

import pytest
from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy import update as sql_update

from app.config import Settings
from app.core import prompt, turn
from app.core.clock import FrozenClock, combine_local
from app.core.outbound import cancel_outbound
from app.core.outbound_gate import EVENING_NAG, MORNING
from app.core.outbound_send import (
    COMMON_FLAG,
    KIND_FLAGS,
    OUTBOUND_CATEGORY,
    hidden_flag,
    run_send_outbound,
)
from app.core.state import get_state
from app.db.models import (
    Job,
    Message,
    Outbound,
    SpendLedger,
    TelegramUpdate,
    UserState,
)
from app.llm.provider import LLMError
from conftest import FakeLLMProvider, FakeSession

TIMEZONE = "Europe/Paris"
CHAT_ID = 4242
DAY = datetime.date(2026, 9, 23)


def at(hour: int, minute: int = 0, day: datetime.date = DAY) -> FrozenClock:
    return FrozenClock(combine_local(day, datetime.time(hour, minute), TIMEZONE))


def settings(**overrides) -> Settings:
    base = dict(TZ_DEFAULT=TIMEZONE, JITTER_MAX_MIN=0, DAILY_USD_CAP=1.00)
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _bot() -> tuple[Bot, FakeSession]:
    fake = FakeSession()
    return Bot(token="123456:TESTTOKEN", session=fake), fake


async def _seed(sessionmaker, **fields) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone=TIMEZONE, **fields))
        await session.commit()


async def _plan_row(sessionmaker, *, kind=MORNING, status="planned", **fields) -> int:
    async with sessionmaker() as session:
        row = Outbound(
            kind=kind,
            local_date=DAY,
            bucket=0,
            planned_for=combine_local(DAY, datetime.time(9, 0), TIMEZONE),
            status=status,
            **fields,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id


async def _run(sessionmaker, provider, bot, clock, outbound_id, cfg=None) -> None:
    async with sessionmaker() as session:
        await run_send_outbound(
            session,
            cfg or settings(),
            provider,
            bot,
            clock=clock,
            outbound_id=outbound_id,
        )


async def _row(sessionmaker, outbound_id) -> Outbound:
    async with sessionmaker() as session:
        return await session.get(Outbound, outbound_id)


async def _messages(sessionmaker) -> list[Message]:
    async with sessionmaker() as session:
        result = await session.execute(select(Message).order_by(Message.id))
        return list(result.scalars().all())


# --- the happy path ----------------------------------------------------


async def test_a_morning_message_is_generated_stored_sent_and_ledgered(sessionmaker):
    await _seed(sessionmaker)
    outbound_id = await _plan_row(sessionmaker)
    provider = FakeLLMProvider(text="  Доброе утро. Сегодня — отчёт.  ")
    bot, fake = _bot()
    clock = at(9, 0)

    await _run(sessionmaker, provider, bot, clock, outbound_id)

    assert provider.calls == 1
    assert [m.text for m in fake.sent] == ["Доброе утро. Сегодня — отчёт."]

    row = await _row(sessionmaker, outbound_id)
    assert row.status == "sent"
    assert row.sent_at == clock.now_utc()
    assert row.message_id is not None

    (message,) = await _messages(sessionmaker)
    assert message.kind == "outbound"
    assert message.ooc is False
    assert message.outbound_id == outbound_id
    assert message.sent_at == clock.now_utc()
    assert message.role == "assistant"
    assert row.message_id == message.id

    async with sessionmaker() as session:
        ledger = list((await session.execute(select(SpendLedger))).scalars())
    assert [entry.category for entry in ledger] == [OUTBOUND_CATEGORY]
    assert ledger[0].local_date == DAY
    assert ledger[0].usd_cost > 0


async def test_the_evening_nag_carries_the_checkin_button(sessionmaker):
    await _seed(sessionmaker)
    outbound_id = await _plan_row(sessionmaker, kind=EVENING_NAG)
    bot, fake = _bot()

    await _run(sessionmaker, FakeLLMProvider(text="Чек-ина не было."), bot, at(22, 0), outbound_id)

    (sent,) = fake.sent
    assert sent.reply_markup is not None
    (button,) = sent.reply_markup.inline_keyboard[0]
    assert button.text == "Чек-ин"
    assert button.callback_data == "c:start"


async def test_the_morning_message_carries_no_buttons(sessionmaker):
    await _seed(sessionmaker)
    outbound_id = await _plan_row(sessionmaker)
    bot, fake = _bot()
    await _run(sessionmaker, FakeLLMProvider(), bot, at(9, 0), outbound_id)
    assert fake.sent[0].reply_markup is None


# --- the hidden flags --------------------------------------------------


def test_the_hidden_flags_are_the_plans_text_verbatim():
    assert KIND_FLAGS[MORNING] == (
        "Сейчас утро. Коротко назови главное действие на сегодня; "
        "если его нет — предложи выбрать одно. 2–4 предложения."
    )
    assert KIND_FLAGS[EVENING_NAG] == (
        "Вечер, чек-ина сегодня не было. Коротко напомни пройти его. "
        "Без отчитывания, 1–3 предложения."
    )
    assert COMMON_FLAG == (
        "Это сообщение по твоей инициативе — не упрекай за молчание "
        "и не повышай интенсивность."
    )


def test_every_kind_gets_the_common_flag():
    for kind in (MORNING, EVENING_NAG, "silence", "tick"):
        assert hidden_flag(kind, "повод").endswith(COMMON_FLAG)


async def test_the_flag_reaches_the_model_and_is_never_stored(sessionmaker):
    await _seed(sessionmaker)
    outbound_id = await _plan_row(sessionmaker)
    provider = FakeLLMProvider()
    bot, _ = _bot()

    await _run(sessionmaker, provider, bot, at(9, 0), outbound_id)

    (messages,) = provider.received_messages
    assert messages[-1].role == "user"
    assert messages[-1].content == hidden_flag(MORNING)

    # Only Anchor's reply is stored; the instruction is ephemeral.
    stored = await _messages(sessionmaker)
    assert all(KIND_FLAGS[MORNING] not in row.content for row in stored)


# --- the send-time gate ------------------------------------------------


async def test_a_checkin_between_planning_and_sending_skips_the_nag(sessionmaker):
    """The plan's worked example. The jitter means minutes pass, and in
    those minutes the user made the nag redundant."""
    clock = at(22, 10)
    await _seed(sessionmaker, last_checkin_at=clock.now_utc())
    outbound_id = await _plan_row(sessionmaker, kind=EVENING_NAG)
    provider = FakeLLMProvider()
    bot, fake = _bot()

    await _run(sessionmaker, provider, bot, clock, outbound_id)

    row = await _row(sessionmaker, outbound_id)
    assert row.status == "skipped"
    assert row.skip_reason == "kind_rule:checkin_done"
    assert provider.calls == 0, "the gate runs before the model, not after"
    assert fake.sent == []
    assert await _messages(sessionmaker) == []


async def test_a_pause_between_planning_and_sending_skips_the_message(sessionmaker):
    await _seed(sessionmaker, persona_active=False)
    outbound_id = await _plan_row(sessionmaker)
    provider = FakeLLMProvider()
    bot, fake = _bot()

    await _run(sessionmaker, provider, bot, at(9, 0), outbound_id)

    assert (await _row(sessionmaker, outbound_id)).skip_reason == "paused"
    assert provider.calls == 0
    assert fake.sent == []


async def test_quiet_hours_at_send_time_skip_the_message(sessionmaker):
    await _seed(sessionmaker)
    outbound_id = await _plan_row(sessionmaker)
    provider = FakeLLMProvider()
    bot, fake = _bot()

    await _run(sessionmaker, provider, bot, at(23, 0), outbound_id)

    assert (await _row(sessionmaker, outbound_id)).skip_reason == "quiet_hours"
    assert fake.sent == []


async def test_reaching_the_cap_between_planning_and_sending_skips_it(sessionmaker):
    await _seed(sessionmaker)
    outbound_id = await _plan_row(sessionmaker)
    async with sessionmaker() as session:
        session.add(
            SpendLedger(local_date=DAY, category="chat", usd_cost=decimal.Decimal("1.00"))
        )
        await session.commit()

    provider = FakeLLMProvider()
    bot, fake = _bot()
    await _run(sessionmaker, provider, bot, at(9, 0), outbound_id)

    assert (await _row(sessionmaker, outbound_id)).skip_reason == "cap"
    assert provider.calls == 0
    assert fake.sent == []


# --- idempotency -------------------------------------------------------


async def test_a_crash_between_insert_and_send_resends_without_regenerating(sessionmaker):
    """The whole point of the commit between step 6 and step 7: the
    user gets the message they were owed, and the model is paid once."""
    await _seed(sessionmaker)
    outbound_id = await _plan_row(sessionmaker)
    async with sessionmaker() as session:
        session.add(
            Message(
                role="assistant",
                content="Уже сгенерировано.",
                ooc=False,
                kind="outbound",
                outbound_id=outbound_id,
                sent_at=None,
            )
        )
        await session.commit()

    provider = FakeLLMProvider(text="НЕ ДОЛЖНО ПОЯВИТЬСЯ")
    bot, fake = _bot()
    clock = at(9, 5)
    await _run(sessionmaker, provider, bot, clock, outbound_id)

    assert provider.calls == 0, "never regenerate what was already paid for"
    assert [m.text for m in fake.sent] == ["Уже сгенерировано."]
    row = await _row(sessionmaker, outbound_id)
    assert row.status == "sent"
    assert row.sent_at == clock.now_utc()
    assert len(await _messages(sessionmaker)) == 1


async def test_a_replayed_job_after_a_successful_send_does_nothing(sessionmaker):
    await _seed(sessionmaker)
    outbound_id = await _plan_row(sessionmaker)
    provider = FakeLLMProvider()
    bot, fake = _bot()

    await _run(sessionmaker, provider, bot, at(9, 0), outbound_id)
    await _run(sessionmaker, provider, bot, at(9, 1), outbound_id)

    assert provider.calls == 1
    assert len(fake.sent) == 1
    assert len(await _messages(sessionmaker)) == 1

    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(SpendLedger)) == 1


async def test_a_row_that_is_no_longer_planned_is_a_no_op(sessionmaker):
    await _seed(sessionmaker)
    outbound_id = await _plan_row(sessionmaker, status="cancelled")
    provider = FakeLLMProvider()
    bot, fake = _bot()

    await _run(sessionmaker, provider, bot, at(9, 0), outbound_id)

    assert provider.calls == 0
    assert fake.sent == []
    assert (await _row(sessionmaker, outbound_id)).status == "cancelled"


async def test_a_missing_row_is_a_no_op(sessionmaker):
    """/delete truncates the table while a job is still queued."""
    await _seed(sessionmaker)
    provider = FakeLLMProvider()
    bot, fake = _bot()
    await _run(sessionmaker, provider, bot, at(9, 0), 9999)
    assert provider.calls == 0 and fake.sent == []


# --- failure -----------------------------------------------------------


async def test_a_failed_generation_sends_nothing_and_leaves_no_trace(sessionmaker):
    await _seed(sessionmaker)
    outbound_id = await _plan_row(sessionmaker)
    provider = FakeLLMProvider(raises=[LLMError("boom")])
    bot, fake = _bot()

    await _run(sessionmaker, provider, bot, at(9, 0), outbound_id)

    assert (await _row(sessionmaker, outbound_id)).status == "failed"
    assert fake.sent == [], "no canned fallback: plan section 7"
    assert await _messages(sessionmaker) == []
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(SpendLedger)) == 0


async def test_an_empty_generation_is_a_failure_not_an_empty_message(sessionmaker):
    await _seed(sessionmaker)
    outbound_id = await _plan_row(sessionmaker)
    bot, fake = _bot()

    await _run(sessionmaker, FakeLLMProvider(text="   "), bot, at(9, 0), outbound_id)

    assert (await _row(sessionmaker, outbound_id)).status == "failed"
    assert fake.sent == []


async def test_a_failed_send_leaves_the_message_resendable(sessionmaker):
    """Telegram is down. The row stays `planned` with a stored message
    whose sent_at is NULL, so the job's retry resends it."""
    await _seed(sessionmaker)
    outbound_id = await _plan_row(sessionmaker)

    class ExplodingSession(FakeSession):
        async def make_request(self, bot, method, timeout=None):
            raise RuntimeError("telegram is down")

    bot = Bot(token="123456:TESTTOKEN", session=ExplodingSession())
    with pytest.raises(RuntimeError):
        await _run(sessionmaker, FakeLLMProvider(text="Доброе утро."), bot, at(9, 0), outbound_id)

    row = await _row(sessionmaker, outbound_id)
    assert row.status == "planned"
    (message,) = await _messages(sessionmaker)
    assert message.sent_at is None

    # The retry resends the stored text and does not pay again.
    provider = FakeLLMProvider()
    bot2, fake2 = _bot()
    await _run(sessionmaker, provider, bot2, at(9, 1), outbound_id)
    assert provider.calls == 0
    assert [m.text for m in fake2.sent] == ["Доброе утро."]
    assert (await _row(sessionmaker, outbound_id)).status == "sent"


# --- counters ----------------------------------------------------------


async def test_a_sent_outbound_moves_the_counters_but_not_last_user_msg_at(sessionmaker):
    """Plan section 7 step 4: an outbound counts as activity for scene
    timing, but it is not the user speaking."""
    user_wrote_at = at(8, 0).now_utc()
    await _seed(sessionmaker, last_user_msg_at=user_wrote_at, ignored_in_row=1)
    outbound_id = await _plan_row(sessionmaker)
    bot, _ = _bot()
    clock = at(9, 0)

    await _run(sessionmaker, FakeLLMProvider(), bot, clock, outbound_id)

    async with sessionmaker() as session:
        state = await get_state(session)
    assert state.ignored_in_row == 2
    assert state.last_outbound_at == clock.now_utc()
    assert state.last_user_msg_at == user_wrote_at


async def test_a_skipped_outbound_moves_no_counters(sessionmaker):
    await _seed(sessionmaker, persona_active=False)
    outbound_id = await _plan_row(sessionmaker)
    bot, _ = _bot()

    await _run(sessionmaker, FakeLLMProvider(), bot, at(9, 0), outbound_id)

    async with sessionmaker() as session:
        state = await get_state(session)
    assert state.ignored_in_row == 0
    assert state.last_outbound_at is None


# --- the transcript ----------------------------------------------------


async def test_an_outbound_message_is_in_the_persona_transcript(sessionmaker):
    """Plan section 7: Anchor has to remember what it said unprompted,
    or it repeats itself tomorrow."""
    await _seed(sessionmaker)
    outbound_id = await _plan_row(sessionmaker)
    bot, _ = _bot()
    clock = at(9, 0)
    await _run(sessionmaker, FakeLLMProvider(text="Доброе утро."), bot, clock, outbound_id)

    async with sessionmaker() as session:
        messages = await prompt.build_messages(
            session,
            clock=clock,
            timezone=TIMEZONE,
            intensity=3,
            user_text="привет",
            update_id=1,
            transcript_turns=30,
        )
    assert any(
        m.role == "assistant" and m.content == "Доброе утро." for m in messages
    )


async def test_an_outbound_never_enqueues_the_extractor(sessionmaker):
    """The extractor proposes facts about the *user* from what the user
    said. There is no user input here."""
    await _seed(sessionmaker)
    outbound_id = await _plan_row(sessionmaker)
    bot, _ = _bot()
    await _run(sessionmaker, FakeLLMProvider(), bot, at(9, 0), outbound_id)

    async with sessionmaker() as session:
        jobs = list((await session.execute(select(Job))).scalars())
    assert [job.kind for job in jobs] == []


async def test_an_outbound_opens_a_scene_and_counts_toward_it(sessionmaker):
    await _seed(sessionmaker)
    outbound_id = await _plan_row(sessionmaker)
    bot, _ = _bot()
    await _run(sessionmaker, FakeLLMProvider(), bot, at(9, 0), outbound_id)

    (message,) = await _messages(sessionmaker)
    assert message.scene_id is not None


# --- cancellation ------------------------------------------------------


async def test_cancel_outbound_cancels_every_planned_row(sessionmaker):
    await _seed(sessionmaker)
    morning = await _plan_row(sessionmaker)
    evening = await _plan_row(sessionmaker, kind=EVENING_NAG)
    sent = await _plan_row(sessionmaker, kind="tick", status="sent")
    clock = at(9, 0)

    async with sessionmaker() as session:
        assert await cancel_outbound(session, clock) == 2

    assert (await _row(sessionmaker, morning)).status == "cancelled"
    assert (await _row(sessionmaker, evening)).status == "cancelled"
    assert (await _row(sessionmaker, sent)).status == "sent", "a delivered row is history"


async def test_cancelling_nothing_is_not_an_error(sessionmaker):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        assert await cancel_outbound(session, at(9, 0)) == 0


async def test_the_pending_job_no_ops_on_a_cancelled_row(sessionmaker):
    """Cancellation does not delete the job; the job checks the row."""
    await _seed(sessionmaker)
    outbound_id = await _plan_row(sessionmaker)
    clock = at(9, 0)
    async with sessionmaker() as session:
        await cancel_outbound(session, clock)

    provider = FakeLLMProvider()
    bot, fake = _bot()
    await _run(sessionmaker, provider, bot, clock, outbound_id)

    assert provider.calls == 0
    assert fake.sent == []
    assert (await _row(sessionmaker, outbound_id)).status == "cancelled"


async def test_a_hard_pause_cancels_planned_messages(sessionmaker):
    await _seed(sessionmaker)
    outbound_id = await _plan_row(sessionmaker)
    async with sessionmaker() as session:
        session.add(TelegramUpdate(update_id=1, payload={}))
        await session.commit()
    bot, _ = _bot()

    await turn.run_hard_pause(
        sessionmaker, bot, clock=at(9, 5), chat_id=CHAT_ID, update_id=1
    )

    assert (await _row(sessionmaker, outbound_id)).status == "cancelled"
    async with sessionmaker() as session:
        assert (await get_state(session)).persona_active is False


async def test_delete_cancels_then_wipes_planned_messages(sessionmaker):
    """/delete cancels first and truncates second (plan section 6). The
    cancel is belt and braces -- but a future wipe that spared a table
    must not leave a scheduled message behind to fire afterwards."""
    from app.core import purge

    await _seed(sessionmaker)
    outbound_id = await _plan_row(sessionmaker)
    clock = at(9, 5)

    async with sessionmaker() as session:
        await cancel_outbound(session, clock)
    assert (await _row(sessionmaker, outbound_id)).status == "cancelled"

    async with sessionmaker() as session:
        await purge.delete_everything(session, settings(), clock)

    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(Outbound)) == 0
        assert await session.scalar(select(func.count()).select_from(Job)) == 0


async def test_a_welfare_trigger_cancels_planned_messages(sessionmaker):
    """The one path where a proactive message arriving would be
    actively harmful, so it is stopped twice: cancelled here, and
    refused by the send-time gate on persona_active=false anyway."""
    await _seed(sessionmaker)
    outbound_id = await _plan_row(sessionmaker)
    async with sessionmaker() as session:
        session.add(TelegramUpdate(update_id=7, payload={}))
        await session.commit()
    bot, _ = _bot()
    clock = at(15, 0)

    await turn.run_welfare_turn(
        sessionmaker,
        bot,
        settings(),
        FakeLLMProvider(text="Я рядом. Как ты на самом деле?"),
        clock=clock,
        chat_id=CHAT_ID,
        update_id=7,
        user_text="стоп, мне реально плохо",
        scene_id=None,
        discarded=None,
        timezone=TIMEZONE,
    )

    assert (await _row(sessionmaker, outbound_id)).status == "cancelled"
    async with sessionmaker() as session:
        state = await get_state(session)
    assert state.persona_active is False
    assert state.welfare_at == clock.now_utc()


async def _make_job_due(sessionmaker) -> None:
    """Bring the queued send_outbound job forward to "now", DB-side.

    Job due-ness is `run_after <= func.now()` in SQL -- deliberately the
    *database's* clock, not the injected one (see app/core/clock.py's
    module docstring: that is what makes a run_after written by one
    process meaningful to another after a restart). A FrozenClock
    therefore cannot make a job claimable, and pretending otherwise
    would hide exactly the kind of scheduling bug the rule protects.
    """
    async with sessionmaker() as session:
        await session.execute(sql_update(Job).values(run_after=func.now()))
        await session.commit()


# --- end to end, through the worker's job dispatch ---------------------


async def test_the_heartbeat_plans_and_the_worker_delivers(sessionmaker):
    """Planning and sending are two mechanisms joined by the job table.
    This is the seam, exercised through the real worker dispatch rather
    than by calling run_send_outbound directly."""
    from app.core.scheduler import heartbeat
    from app.worker import process_one_job

    await _seed(sessionmaker)
    clock = at(9, 0)
    cfg = settings()

    async with sessionmaker() as session:
        outbound_id = await heartbeat(session, cfg, clock)
    assert outbound_id is not None
    await _make_job_due(sessionmaker)

    provider = FakeLLMProvider(text="Доброе утро. Одно дело на сегодня.")
    bot, fake = _bot()
    claimed = await process_one_job(
        sessionmaker, cfg, FakeLLMProvider(), clock, bot, provider
    )

    assert claimed is True
    assert [m.text for m in fake.sent] == ["Доброе утро. Одно дело на сегодня."]
    assert (await _row(sessionmaker, outbound_id)).status == "sent"

    async with sessionmaker() as session:
        job = (await session.execute(select(Job))).scalars().one()
    assert job.status == "done"


async def test_a_job_whose_send_fails_is_retried_not_lost(sessionmaker):
    from app.core.scheduler import heartbeat
    from app.worker import process_one_job

    await _seed(sessionmaker)
    clock = at(9, 0)
    cfg = settings()
    async with sessionmaker() as session:
        await heartbeat(session, cfg, clock)
    await _make_job_due(sessionmaker)

    class ExplodingSession(FakeSession):
        async def make_request(self, bot, method, timeout=None):
            raise RuntimeError("telegram is down")

    bot = Bot(token="123456:TESTTOKEN", session=ExplodingSession())
    await process_one_job(
        sessionmaker, cfg, FakeLLMProvider(), clock, bot, FakeLLMProvider()
    )

    async with sessionmaker() as session:
        job = (await session.execute(select(Job))).scalars().one()
    assert job.status == "pending", "back on the queue for another attempt"
    assert job.attempts == 1
