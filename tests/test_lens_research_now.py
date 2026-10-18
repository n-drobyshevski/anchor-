"""`/lens research now`: the queued lens research, run when the user asks.

app/core/idle/gate.py's `manual_lens_research_gate` drops the rows that
keep idle work out of the user's way (user_active, window, the kind's
once-a-day limit) and keeps the switches, the pause, the welfare
cooldown, `busy`, every money row and the kind's own rule;
app/core/idle/planner.py's `plan_manual_lens_research` queues the run
with `manual` in its job payload; app/core/idle/runner.py re-checks it
with the manual gate and app/core/idle/lens_research.py never preempts
it; app/tg/lens.py answers with one reply per reason. Every note, gap,
page and card is synthetic (tests/test_lens_research_idle.py's).
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal

import pytest
from sqlalchemy import func, select

from app.core.idle import LENS_RESEARCH
from app.core.idle import gate as idle_gate
from app.core.idle.gate import manual_lens_research_gate
from app.core.idle.planner import plan_manual_lens_research
from app.core.idle.runner import run_idle
from app.db.models import IdleRun, Job, StudyCard, StudyJob, UserState
from app.tg import lens as lens_cmd
from test_lens_research_idle import (
    GOOD_DISTILL,
    NOW,
    Pipeline,
    ScriptedProvider,
    _config,
    _facts,
    clock,
    lens_settings,
    query_reply,
    seed_garden,
    tap,
)


# --- the manual gate -----------------------------------------------------------------


def test_the_manual_gate_ignores_what_keeps_idle_work_out_of_the_way():
    """The command is itself a message, may come at any hour, and the
    kind may already have run today."""
    facts = _facts(
        lens_job_queued=True,
        last_user_msg_at=NOW,
        kind_runs_today={LENS_RESEARCH: 1},
    )
    config = _config(window_start=datetime.time(3, 0), window_end=datetime.time(3, 1))
    assert not idle_gate.idle_gate(LENS_RESEARCH, facts, NOW, config).allowed
    assert manual_lens_research_gate(facts, NOW, config) == idle_gate.GateResult(
        True, idle_gate.OK
    )


@pytest.mark.parametrize(
    ("facts", "config", "reason"),
    [
        ({}, {"enabled": False}, idle_gate.DISABLED),
        ({"persona_active": False}, {}, idle_gate.PAUSED),
        ({"welfare_at": NOW - datetime.timedelta(hours=2)}, {}, idle_gate.WELFARE_COOLDOWN),
        ({"active_run_ids": (7,)}, {}, idle_gate.BUSY),
        ({"jobs_today": 8}, {}, idle_gate.MAX_JOBS),
        ({"idle_spend_today": decimal.Decimal("0.21")}, {}, idle_gate.IDLE_CAP),
        ({"spend_today": decimal.Decimal("9.46")}, {}, idle_gate.RESERVE),
        ({}, {"lens_research_enabled": False}, idle_gate.LENS_RESEARCH_OFF),
        ({}, {"lens_packet": False}, idle_gate.EMPTY_LENS_PACKET),
        ({"lens_job_queued": False}, {}, idle_gate.NO_LENS_JOB),
    ],
)
def test_the_manual_gate_keeps_the_rest(facts, config, reason):
    # `_facts` pins persona_active, so the rest goes on with replace().
    facts = dataclasses.replace(_facts(lens_job_queued=True), **facts)
    verdict = manual_lens_research_gate(facts, NOW, _config(**config))
    assert verdict == idle_gate.GateResult(False, reason)


def test_the_manual_gate_excludes_its_own_run_from_busy():
    facts = _facts(lens_job_queued=True, active_run_ids=(7,))
    assert manual_lens_research_gate(facts, NOW, _config(), self_run_id=7).allowed


# --- queueing ------------------------------------------------------------------------


async def test_plan_queues_a_manual_run(sessionmaker):
    g = await seed_garden(sessionmaker)
    await tap(sessionmaker, g["tension"])
    async with sessionmaker() as session:
        run_id, reason = await plan_manual_lens_research(session, lens_settings(), clock())
    assert reason == idle_gate.OK
    async with sessionmaker() as session:
        run = await session.get(IdleRun, run_id)
        job = (await session.execute(select(Job))).scalar_one()
    assert (run.kind, run.status) == (LENS_RESEARCH, "queued")
    assert job.payload == {"run_id": run_id, "manual": True}


async def test_plan_refuses_without_a_tap_and_inserts_nothing(sessionmaker):
    await seed_garden(sessionmaker)
    async with sessionmaker() as session:
        result = await plan_manual_lens_research(session, lens_settings(), clock())
    assert result == (None, idle_gate.NO_LENS_JOB)
    async with sessionmaker() as session:
        runs = (await session.execute(select(func.count()).select_from(IdleRun))).scalar_one()
    assert runs == 0


# --- running -------------------------------------------------------------------------


async def test_a_manual_run_ignores_a_user_who_just_wrote_and_is_never_preempted(
    sessionmaker, monkeypatch
):
    """With the user active a moment ago the idle run is skipped as
    `user_active`; the manual one runs to cards, even when the user
    writes again mid-run (which preempts an idle run after its query)."""
    g = await seed_garden(sessionmaker, last_user_msg_at=NOW - datetime.timedelta(seconds=30))
    job_id = await tap(sessionmaker, g["tension"])
    Pipeline(monkeypatch)

    async def _user_writes(call_number):
        async with sessionmaker() as session:
            state = await session.get(UserState, 1)
            state.last_user_msg_at = NOW + datetime.timedelta(seconds=1)
            await session.commit()

    async def one(manual: bool) -> IdleRun:
        async with sessionmaker() as session:
            run = IdleRun(kind=LENS_RESEARCH, local_date=NOW.date(), status="queued")
            session.add(run)
            await session.commit()
            run_id = run.id
        provider = ScriptedProvider(query_reply(), GOOD_DISTILL, on_call=_user_writes)
        await run_idle(
            sessionmaker, lens_settings(), provider, provider, clock(),
            run_id=run_id, manual=manual,
        )
        async with sessionmaker() as session:
            return await session.get(IdleRun, run_id)

    idle = await one(False)
    assert (idle.status, idle.skip_reason) == ("skipped", idle_gate.USER_ACTIVE)
    manual = await one(True)
    assert manual.status == "done"
    assert manual.summary["cards"] == 1
    async with sessionmaker() as session:
        job = await session.get(StudyJob, job_id)
        cards = (await session.execute(select(StudyCard))).scalars().all()
    assert job.status == "done"
    assert len(cards) == 1


# --- the command ---------------------------------------------------------------------


async def test_research_now_starts_and_says_so(sessionmaker):
    g = await seed_garden(sessionmaker)
    await tap(sessionmaker, g["tension"])
    reply = await lens_cmd.command(sessionmaker, lens_settings(), clock(), "research now")
    assert reply == lens_cmd.RESEARCH_NOW_STARTED


async def test_research_now_says_there_is_nothing_to_run(sessionmaker):
    await seed_garden(sessionmaker)
    reply = await lens_cmd.command(sessionmaker, lens_settings(), clock(), "research now")
    assert reply == lens_cmd.RESEARCH_NOW_REPLIES[idle_gate.NO_LENS_JOB]


def test_every_manual_gate_reason_has_a_reply():
    reasons = {
        idle_gate.DISABLED, idle_gate.PAUSED, idle_gate.WELFARE_COOLDOWN, idle_gate.BUSY,
        idle_gate.MAX_JOBS, idle_gate.IDLE_CAP, idle_gate.RESERVE,
        idle_gate.LENS_RESEARCH_OFF, idle_gate.EMPTY_LENS_PACKET, idle_gate.NO_LENS_JOB,
    }
    assert reasons <= set(lens_cmd.RESEARCH_NOW_REPLIES)


def test_usage_lists_the_command():
    assert "/lens research now" in lens_cmd.USAGE
