"""The optional tick (phase-3 plan sections 6, 8 and 13).

Plan section 13 names four properties, and they come first below:

- at most one per day;
- skipped when the user was active in the last 2h;
- `send=false` creates no row;
- **a failed gate means no model call.**

The last is the one that matters most. The tick is the first place a
model has any say in whether an unsolicited message goes out, so the
order -- gate, then model, then gate again -- is the whole design. A
refused tick must cost nothing.

Everything else here defends the seam between "the model proposes" and
"the code decides": a truthy-but-not-boolean `send`, a blank note, an
over-long note, prose instead of JSON, a note carrying a card number.
All of them are a no, and all of them are still paid for and recorded.
"""

from __future__ import annotations

import datetime
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy import update as sql_update

from app.config import Settings
from app.core import tick
from app.core.clock import FrozenClock, combine_local
from app.core.outbound_gate import TICK
from app.core.scheduler import TICK_DECIDE, heartbeat, tick_dedup_key
from app.db.models import Job, Journal, Message, Outbound, SpendLedger, UserState
from conftest import FakeLLMProvider

TIMEZONE = "Europe/Paris"
CHAT_ID = 4242
DAY = datetime.date(2026, 9, 23)
HOUR = 14


def at(hour: int, minute: int = 0, day: datetime.date = DAY) -> FrozenClock:
    return FrozenClock(combine_local(day, datetime.time(hour, minute), TIMEZONE))


def settings(**overrides) -> Settings:
    base = dict(TZ_DEFAULT=TIMEZONE, JITTER_MAX_MIN=0, DAILY_USD_CAP=1.00)
    base.update(overrides)
    return Settings(_env_file=None, **base)


def payload(send=True, note="незакрытая тема про отчёт") -> str:
    return json.dumps({"send": send, "note": note}, ensure_ascii=False)


async def _seed(sessionmaker, **fields) -> None:
    """Quiet for 5 hours by default -- the tick's baseline allowed state."""
    base = dict(last_user_msg_at=at(9, 0).now_utc())
    base.update(fields)
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone=TIMEZONE, **base))
        await session.commit()


async def _decide(sessionmaker, provider, clock=None, cfg=None, hour=HOUR):
    async with sessionmaker() as session:
        return await tick.run_tick_decide(
            session,
            cfg or settings(),
            provider,
            clock=clock or at(HOUR, 0),
            local_date=DAY,
            hour=hour,
        )


async def _rows(sessionmaker) -> list[Outbound]:
    async with sessionmaker() as session:
        result = await session.execute(select(Outbound).order_by(Outbound.id))
        return list(result.scalars().all())


async def _ledger(sessionmaker) -> list[SpendLedger]:
    async with sessionmaker() as session:
        result = await session.execute(select(SpendLedger))
        return list(result.scalars().all())


# --- plan section 13's four ---------------------------------------------


async def test_a_failed_gate_means_no_model_call(sessionmaker):
    """The order is gate, then model. A tick the code would refuse
    anyway must not cost a cent."""
    await _seed(sessionmaker, persona_active=False)
    provider = FakeLLMProvider(text=payload())

    assert await _decide(sessionmaker, provider) is None

    assert provider.calls == 0
    assert await _rows(sessionmaker) == []
    assert await _ledger(sessionmaker) == []


async def test_at_most_one_tick_a_day(sessionmaker):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        session.add(
            Outbound(
                kind=TICK,
                local_date=DAY,
                bucket=10,
                planned_for=at(10, 0).now_utc(),
                status="sent",
                sent_at=at(10, 0).now_utc(),
            )
        )
        await session.commit()

    provider = FakeLLMProvider(text=payload())
    assert await _decide(sessionmaker, provider) is None
    assert provider.calls == 0


async def test_skipped_when_the_user_was_active_in_the_last_two_hours(sessionmaker):
    await _seed(sessionmaker, last_user_msg_at=at(13, 0).now_utc())
    provider = FakeLLMProvider(text=payload())

    assert await _decide(sessionmaker, provider) is None
    assert provider.calls == 0


async def test_send_false_creates_no_row(sessionmaker):
    await _seed(sessionmaker)
    provider = FakeLLMProvider(text=payload(send=False, note=""))

    assert await _decide(sessionmaker, provider) is None

    assert provider.calls == 1, "the model was asked"
    assert await _rows(sessionmaker) == []
    assert len(await _ledger(sessionmaker)) == 1, "and paid for"


# --- the yes path --------------------------------------------------------


async def test_a_yes_plans_a_tick_and_queues_its_send(sessionmaker):
    await _seed(sessionmaker)
    clock = at(HOUR, 0)

    outbound_id = await _decide(
        sessionmaker, FakeLLMProvider(text=payload(note="обещал доделать сегодня")), clock
    )

    assert outbound_id is not None
    (row,) = await _rows(sessionmaker)
    assert row.kind == TICK
    assert row.local_date == DAY
    assert row.bucket == HOUR, "the bucket is the hour that reserved the decision"
    assert row.tick_note == "обещал доделать сегодня"
    assert row.status == "planned"
    assert row.planned_for == clock.now_utc()  # JITTER_MAX_MIN=0

    async with sessionmaker() as session:
        jobs = list((await session.execute(select(Job))).scalars())
    assert [job.kind for job in jobs] == ["send_outbound"]
    assert jobs[0].payload == {"outbound_id": outbound_id}


async def test_the_note_reaches_the_hidden_flag(sessionmaker):
    """The whole point of the note: it is the «Повод» in the flag the
    send path hands the main model."""
    from app.core.outbound_send import hidden_flag

    await _seed(sessionmaker)
    await _decide(sessionmaker, FakeLLMProvider(text=payload(note="отчёт до пятницы")))

    (row,) = await _rows(sessionmaker)
    flag = hidden_flag(row.kind, row.tick_note)
    assert "«отчёт до пятницы»" in flag
    assert flag.endswith(
        "Это сообщение по твоей инициативе — не упрекай за молчание "
        "и не повышай интенсивность."
    )


async def test_the_schema_and_prompt_are_sent_with_the_call(sessionmaker):
    await _seed(sessionmaker)
    provider = FakeLLMProvider(text=payload())
    await _decide(sessionmaker, provider)

    assert provider.received_schemas == [tick.TICK_SCHEMA]
    (messages,) = provider.received_messages
    assert messages[0].role == "system"
    assert messages[0].content == tick.DECISION_PROMPT


def test_the_decision_prompt_is_the_plans_text_verbatim():
    assert tick.DECISION_PROMPT == (
        "Ты решаешь, стоит ли Anchor написать первым прямо сейчас. "
        "По умолчанию — нет. Да — только при естественном поводе: "
        "незакрытая тема из последнего разговора, главное действие с близким "
        "сроком, пользователь сам сказал, что сделает что-то сегодня. "
        "Не пиши просто чтобы напомнить о себе. Верни JSON."
    )


# --- validate(), the seam between proposing and deciding -----------------


def test_a_clean_yes_validates():
    assert tick.validate({"send": True, "note": "повод"}) == (True, "повод")


@pytest.mark.parametrize(
    "payload_dict",
    [
        {"send": False, "note": "повод"},
        {"send": "true", "note": "повод"},  # a string is not an answer
        {"send": 1, "note": "повод"},  # nor is a number
        {"send": None, "note": "повод"},
        {"note": "повод"},  # missing entirely
        {"send": True},  # no note at all
        {"send": True, "note": ""},
        {"send": True, "note": "   "},
        {"send": True, "note": 42},
        {"send": True, "note": None},
        {"send": True, "note": "я" * 121},
        None,
        "не JSON",
        [],
    ],
)
def test_anything_short_of_a_clean_yes_is_a_no(payload_dict):
    assert tick.validate(payload_dict) == (False, None)


def test_a_note_at_the_limit_is_kept():
    note = "я" * tick.NOTE_MAX
    assert tick.validate({"send": True, "note": note}) == (True, note)


def test_a_note_is_stripped():
    assert tick.validate({"send": True, "note": "  повод  "}) == (True, "повод")


@pytest.mark.parametrize(
    "note",
    [
        "карта 4111 1111 1111 1111",
        "почта somebody@example.com",
        "IBAN FR76 3000 6000 0112 3456 7890 189",
    ],
)
def test_a_note_carrying_something_unstorable_is_a_no(note):
    """The note is stored and later re-injected into a prompt, so it
    gets the same redaction every memory and proposal gets."""
    assert tick.validate({"send": True, "note": note}) == (False, None)


async def test_prose_instead_of_json_is_a_no_but_is_still_paid_for(sessionmaker):
    await _seed(sessionmaker)
    provider = FakeLLMProvider(text="Думаю, стоит написать! Он же обещал.")

    assert await _decide(sessionmaker, provider) is None

    assert await _rows(sessionmaker) == []
    assert len(await _ledger(sessionmaker)) == 1


async def test_an_over_long_note_is_dropped_not_truncated(sessionmaker):
    """Half a reason is not a better reason to interrupt someone -- and
    the row's check constraint can then never be hit at runtime."""
    await _seed(sessionmaker)
    await _decide(sessionmaker, FakeLLMProvider(text=payload(note="я" * 200)))
    assert await _rows(sessionmaker) == []


# --- the ledger ----------------------------------------------------------


async def test_the_decision_is_ledgered_under_tick(sessionmaker):
    await _seed(sessionmaker)
    await _decide(sessionmaker, FakeLLMProvider(text=payload(), model="cydonia-fake"))

    (entry,) = await _ledger(sessionmaker)
    assert entry.category == "tick"
    assert entry.local_date == DAY
    assert entry.usd_cost > 0


async def test_the_send_is_ledgered_separately_from_the_decision(sessionmaker):
    """Deciding and speaking are different budget lines."""
    from app.core.outbound_send import run_send_outbound
    from conftest import FakeSession
    from aiogram import Bot

    await _seed(sessionmaker)
    clock = at(HOUR, 0)
    outbound_id = await _decide(sessionmaker, FakeLLMProvider(text=payload()), clock)

    bot = Bot(token="123456:TESTTOKEN", session=FakeSession())
    async with sessionmaker() as session:
        await run_send_outbound(
            session,
            settings(),
            FakeLLMProvider(text="Ты обещал доделать отчёт. Что осталось?"),
            bot,
            clock=clock,
            outbound_id=outbound_id,
        )

    categories = sorted(entry.category for entry in await _ledger(sessionmaker))
    assert categories == ["outbound", "tick"]


# --- the input the model sees -------------------------------------------


async def test_the_input_carries_the_transcript_the_journal_and_the_due_action(
    sessionmaker,
):
    await _seed(sessionmaker, due_action="сдать отчёт", due_set_at=at(9, 0).now_utc())
    async with sessionmaker() as session:
        session.add_all(
            [
                Message(role="user", content="доделаю сегодня", ooc=False, kind="chat"),
                Message(role="assistant", content="Жду.", ooc=False, kind="chat"),
                Journal(local_date=DAY, text="обещал доделать отчёт"),
            ]
        )
        await session.commit()

    provider = FakeLLMProvider(text=payload(send=False, note=""))
    await _decide(sessionmaker, provider)

    (messages,) = provider.received_messages
    body = messages[1].content
    assert "доделаю сегодня" in body
    assert "Anchor: Жду." in body
    assert "обещал доделать отчёт" in body
    assert "«сдать отчёт»" in body
    assert "Пользователь писал: 5 ч назад" in body


async def test_welfare_and_canned_rows_never_reach_the_decision(sessionmaker):
    """The tick sees the persona transcript, which excludes both."""
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        session.add_all(
            [
                Message(role="user", content="СЕКРЕТ", ooc=True, kind="welfare"),
                Message(role="assistant", content="ШАБЛОН", ooc=True, kind="canned"),
                Message(role="user", content="видимое", ooc=False, kind="chat"),
            ]
        )
        await session.commit()

    provider = FakeLLMProvider(text=payload(send=False, note=""))
    await _decide(sessionmaker, provider)

    body = provider.received_messages[0][1].content
    assert "СЕКРЕТ" not in body
    assert "ШАБЛОН" not in body
    assert "видимое" in body


async def test_an_earlier_outbound_is_in_the_decisions_transcript(sessionmaker):
    """Anchor should not propose a tick about something it already said
    this morning."""
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        session.add(
            Message(
                role="assistant", content="Доброе утро.", ooc=False, kind="outbound"
            )
        )
        await session.commit()

    provider = FakeLLMProvider(text=payload(send=False, note=""))
    await _decide(sessionmaker, provider)
    assert "Доброе утро." in provider.received_messages[0][1].content


# --- privacy -------------------------------------------------------------


async def test_the_note_never_reaches_the_logs(sessionmaker, caplog):
    """Plan rule 8: no message text, prompts or completions in logs."""
    import logging

    await _seed(sessionmaker)
    secret = "очень узнаваемый повод"
    with caplog.at_level(logging.DEBUG):
        await _decide(sessionmaker, FakeLLMProvider(text=payload(note=secret)))

    blob = "\n".join(
        [record.getMessage() + str(record.__dict__) for record in caplog.records]
    )
    assert secret not in blob


# --- the heartbeat's half ------------------------------------------------


async def _tick_heartbeat(sessionmaker, clock, cfg=None):
    async with sessionmaker() as session:
        return await heartbeat(session, cfg or settings(), clock)


async def _jobs(sessionmaker, kind=TICK_DECIDE) -> list[Job]:
    async with sessionmaker() as session:
        result = await session.execute(select(Job).where(Job.kind == kind))
        return list(result.scalars().all())


@pytest.mark.parametrize("minute", [0, 1, 4])
async def test_the_heartbeat_queues_a_decision_in_the_first_five_minutes(
    sessionmaker, minute
):
    await _seed(sessionmaker)
    await _tick_heartbeat(sessionmaker, at(HOUR, minute))
    assert len(await _jobs(sessionmaker)) == 1


async def test_all_five_minutes_collapse_into_one_decision(sessionmaker):
    await _seed(sessionmaker)
    for minute in range(5):
        await _tick_heartbeat(sessionmaker, at(HOUR, minute))

    jobs = await _jobs(sessionmaker)
    assert len(jobs) == 1
    assert jobs[0].dedup_key == tick_dedup_key(DAY, HOUR)
    assert jobs[0].payload == {"local_date": DAY.isoformat(), "hour": HOUR}


async def test_nothing_is_queued_after_the_fifth_minute(sessionmaker):
    await _seed(sessionmaker)
    await _tick_heartbeat(sessionmaker, at(HOUR, 5))
    assert await _jobs(sessionmaker) == []


async def test_nothing_is_queued_outside_tick_hours(sessionmaker):
    await _seed(sessionmaker)
    await _tick_heartbeat(sessionmaker, at(15, 0))
    assert await _jobs(sessionmaker) == []


async def test_an_empty_tick_hours_is_the_off_switch(sessionmaker):
    await _seed(sessionmaker)
    await _tick_heartbeat(sessionmaker, at(HOUR, 0), settings(TICK_HOURS=""))
    assert await _jobs(sessionmaker) == []


async def test_each_tick_hour_gets_its_own_decision(sessionmaker):
    await _seed(sessionmaker)
    for hour in (10, 12, 14):
        await _tick_heartbeat(sessionmaker, at(hour, 0))
    assert len(await _jobs(sessionmaker)) == 3


async def test_the_tick_is_queued_even_when_a_fixed_intent_is_also_planned(
    sessionmaker,
):
    """The enqueue runs before the priority loop on purpose: the loop
    returns early on a refused gate, and the tick must not be
    collateral damage."""
    await _seed(sessionmaker)
    cfg = settings(MORNING_TIME="10:00", TICK_HOURS="10")

    planned = await _tick_heartbeat(sessionmaker, at(10, 0), cfg)

    assert planned is not None, "the morning message was planned"
    assert len(await _jobs(sessionmaker)) == 1, "and the tick was queued too"


async def test_the_tick_is_queued_even_when_the_planning_gate_refuses(sessionmaker):
    await _seed(sessionmaker, persona_active=False)
    cfg = settings(MORNING_TIME="14:00", TICK_HOURS="14")

    assert await _tick_heartbeat(sessionmaker, at(HOUR, 0), cfg) is None
    assert len(await _jobs(sessionmaker)) == 1


# --- end to end ----------------------------------------------------------


async def test_heartbeat_to_delivered_message(sessionmaker):
    """The whole chain, through the real worker dispatch."""
    from aiogram import Bot

    from app.worker import process_one_job
    from conftest import FakeSession

    await _seed(sessionmaker)
    clock = at(HOUR, 0)
    cfg = settings()

    await _tick_heartbeat(sessionmaker, clock, cfg)
    async with sessionmaker() as session:
        await session.execute(sql_update(Job).values(run_after=func.now()))
        await session.commit()

    decider = FakeLLMProvider(text=payload(note="обещал доделать отчёт"))
    fake = FakeSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake)

    # 1. The decision job.
    assert await process_one_job(sessionmaker, cfg, decider, clock, bot) is True
    (row,) = await _rows(sessionmaker)
    assert row.kind == TICK and row.status == "planned"

    # 2. The send job it queued.
    async with sessionmaker() as session:
        await session.execute(sql_update(Job).values(run_after=func.now()))
        await session.commit()

    main = FakeLLMProvider(text="Что осталось по отчёту?")
    assert (
        await process_one_job(sessionmaker, cfg, FakeLLMProvider(), clock, bot, main)
        is True
    )

    assert [m.text for m in fake.sent] == ["Что осталось по отчёту?"]
    (row,) = await _rows(sessionmaker)
    assert row.status == "sent"

    # The note reached the main model's flag.
    assert "«обещал доделать отчёт»" in main.received_messages[0][-1].content


async def test_a_second_tick_the_same_day_is_refused_at_the_gate(sessionmaker):
    """The first is sent, so the second never reaches the model."""
    from aiogram import Bot

    from app.core.outbound_send import run_send_outbound
    from conftest import FakeSession

    # Quiet since 06:00, so the 10:00 decision is not gated on
    # kind_rule:user_active before the scenario starts.
    await _seed(sessionmaker, last_user_msg_at=at(6, 0).now_utc())
    bot = Bot(token="123456:TESTTOKEN", session=FakeSession())

    first = await _decide(
        sessionmaker, FakeLLMProvider(text=payload()), at(10, 0), hour=10
    )
    assert first is not None
    async with sessionmaker() as session:
        await run_send_outbound(
            session,
            settings(),
            FakeLLMProvider(text="Первый тик."),
            bot,
            clock=at(10, 0),
            outbound_id=first,
        )

    second_provider = FakeLLMProvider(text=payload())
    assert await _decide(sessionmaker, second_provider, at(14, 0), hour=14) is None
    assert second_provider.calls == 0


async def test_a_tick_skipped_at_send_time_leaves_the_day_open(sessionmaker):
    """tick_sent_today counts delivered rows. A tick the send-time gate
    refused was never received, so a later hour may try again."""
    from aiogram import Bot

    from app.core.outbound_send import run_send_outbound
    from conftest import FakeSession

    await _seed(sessionmaker, last_user_msg_at=at(6, 0).now_utc())
    bot = Bot(token="123456:TESTTOKEN", session=FakeSession())

    first = await _decide(
        sessionmaker, FakeLLMProvider(text=payload()), at(10, 0), hour=10
    )
    assert first is not None
    # The user writes between planning and sending: user_active.
    async with sessionmaker() as session:
        await session.execute(
            sql_update(UserState)
            .where(UserState.id == 1)
            .values(last_user_msg_at=at(10, 1).now_utc())
        )
        await session.commit()
        await run_send_outbound(
            session,
            settings(),
            FakeLLMProvider(text="не должно уйти"),
            bot,
            clock=at(10, 2),
            outbound_id=first,
        )

    rows = await _rows(sessionmaker)
    assert rows[0].status == "skipped"
    assert rows[0].skip_reason == "kind_rule:user_active"

    # Hours later, quiet again: allowed.
    second = await _decide(
        sessionmaker, FakeLLMProvider(text=payload()), at(16, 0), hour=16
    )
    assert second is not None
