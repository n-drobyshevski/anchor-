"""`/lens garden now`: one garden pass the user asked for.

app/core/idle/gate.py's `manual_garden_gate` drops only the rows that
keep idle work out of the user's way (user_active, window, the 168-hour
interval, `unchanged`) and keeps the rest; app/core/idle/planner.py's
`plan_manual_garden` queues the run with `manual` in its job payload;
app/core/idle/runner.py re-checks it with the manual gate and never
preempts it; app/tg/lens.py answers with one reply per reason.
"""

from __future__ import annotations

import datetime
import decimal

import pytest
from sqlalchemy import func, select

from app.core.clock import FrozenClock
from app.core.idle import LENS_GARDEN
from app.core.idle import gate as idle_gate
from app.core.idle import lens_garden
from app.core.idle.gate import manual_garden_gate
from app.core.idle.planner import plan_manual_garden
from app.core.idle.runner import run_idle
from app.db.models import IdleRun, Job
from app.tg import lens as lens_cmd
from conftest import FakeLLMProvider
from test_lens_garden_idle import (
    NOW,
    _config,
    _facts,
    _garden_rows,
    _link,
    _reply,
    _seed,
    _settings,
    _state,
)

LAST_WEEK = NOW - datetime.timedelta(days=3)  # Sunday of 2026-W39: another ISO week


# --- the manual gate -----------------------------------------------------------------


def test_the_manual_gate_ignores_what_keeps_idle_work_out_of_the_way():
    """The command is itself a message, and may come at any hour; a lens
    that has not changed, or a garden three days old, is the user's call."""
    facts = _facts(
        last_user_msg_at=NOW,
        garden_last_run_at=LAST_WEEK,
        garden_last_iso_week=idle_gate.iso_week(LAST_WEEK.date()),
        garden_last_version_id=2,
        garden_version_id=2,
        garden_done=0,
    )
    config = _config(window_start=datetime.time(3, 0), window_end=datetime.time(3, 1))
    assert not idle_gate.idle_gate(LENS_GARDEN, facts, NOW, config).allowed
    assert manual_garden_gate(facts, NOW, config) == idle_gate.GateResult(True, idle_gate.OK)


@pytest.mark.parametrize(
    ("facts", "config", "reason"),
    [
        ({}, {"enabled": False}, idle_gate.DISABLED),
        ({"persona_active": False}, {}, idle_gate.PAUSED),
        ({"welfare_at": NOW - datetime.timedelta(hours=2)}, {}, idle_gate.WELFARE_COOLDOWN),
        ({"active_run_ids": (7,)}, {}, idle_gate.BUSY),
        ({"jobs_today": 8}, {}, idle_gate.MAX_JOBS),
        ({"idle_spend_today": decimal.Decimal("0.21")}, {}, idle_gate.IDLE_CAP),
        ({"spend_today": decimal.Decimal("0.46")}, {}, idle_gate.RESERVE),
        ({}, {"garden_enabled": False}, idle_gate.GARDEN_OFF),
        ({"garden_notes": 2}, {}, idle_gate.LENS_SIZE),
        (
            {"garden_last_run_at": NOW - datetime.timedelta(hours=1),
             "garden_last_iso_week": idle_gate.iso_week(NOW.date())},
            {},
            idle_gate.NOT_DUE,
        ),
    ],
)
def test_the_manual_gate_keeps_the_rest(facts, config, reason):
    verdict = manual_garden_gate(_facts(**facts), NOW, _config(**config))
    assert verdict == idle_gate.GateResult(False, reason)


def test_the_manual_gate_excludes_its_own_run_from_busy():
    facts = _facts(active_run_ids=(7,))
    assert manual_garden_gate(facts, NOW, _config(), self_run_id=7).allowed


# --- queueing ------------------------------------------------------------------------


async def test_plan_manual_garden_queues_a_manual_run(sessionmaker):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        run_id, reason = await plan_manual_garden(session, _settings(), FrozenClock(NOW))
    assert reason == idle_gate.OK
    async with sessionmaker() as session:
        run = await session.get(IdleRun, run_id)
        job = (await session.execute(select(Job))).scalar_one()
    assert (run.kind, run.status) == (LENS_GARDEN, "queued")
    assert job.payload == {"run_id": run_id, "manual": True}


async def test_plan_manual_garden_refuses_without_inserting(sessionmaker):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        run_id, reason = await plan_manual_garden(
            session, _settings(LENS_GARDEN_ENABLED=False), FrozenClock(NOW)
        )
    assert (run_id, reason) == (None, idle_gate.GARDEN_OFF)
    async with sessionmaker() as session:
        runs = (await session.execute(select(func.count()).select_from(IdleRun))).scalar_one()
    assert runs == 0


async def test_a_queued_manual_run_makes_a_second_tap_busy(sessionmaker):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        await plan_manual_garden(session, _settings(), FrozenClock(NOW))
    async with sessionmaker() as session:
        assert await plan_manual_garden(session, _settings(), FrozenClock(NOW)) == (None, idle_gate.BUSY)


# --- running -------------------------------------------------------------------------


async def test_a_manual_run_is_never_preempted(sessionmaker):
    """The idle preemption test's twin: the same message after the start
    preempts an idle garden, but not the one the user asked for."""
    seeded = await _seed(sessionmaker, state=False)
    started = NOW - datetime.timedelta(minutes=5)
    async with sessionmaker() as session:
        await _state(session, last_user_msg_at=NOW - datetime.timedelta(minutes=1))
        await session.commit()
    ids = seeded.ids
    provider = FakeLLMProvider(text=_reply([_link(ids["Норберт Винер"], ids["Кибернетика"])]))
    async with sessionmaker() as session:
        run = IdleRun(kind=LENS_GARDEN, local_date=NOW.date(), status="running")
        session.add(run)
        await session.commit()
        run_id = run.id
    result = await lens_garden.run_lens_garden(
        sessionmaker, _settings(), FrozenClock(NOW),
        run_id=run_id, started_at=started, timezone="Europe/Paris",
        provider=provider, manual=True,
    )
    assert result.preempted is False
    assert await _garden_rows(sessionmaker) == 1


async def test_the_runner_uses_the_manual_gate_for_a_manual_garden(sessionmaker, monkeypatch):
    """With the user active a moment ago, the idle gate skips the run as
    `user_active`; the manual one runs it, and passes `manual` on."""
    await _seed(sessionmaker, state=False)
    async with sessionmaker() as session:
        await _state(session, last_user_msg_at=NOW - datetime.timedelta(seconds=30))
        await session.commit()
    seen = {}

    async def fake_run(*args, manual=False, **kwargs):
        seen["manual"] = manual
        return lens_garden.GardenResult()

    monkeypatch.setattr(lens_garden, "run_lens_garden", fake_run)

    async def one(manual: bool) -> IdleRun:
        async with sessionmaker() as session:
            run = IdleRun(kind=LENS_GARDEN, local_date=NOW.date(), status="queued")
            session.add(run)
            await session.commit()
            run_id = run.id
        await run_idle(
            sessionmaker, _settings(), FakeLLMProvider(), FakeLLMProvider(), FrozenClock(NOW),
            run_id=run_id, manual=manual,
        )
        async with sessionmaker() as session:
            return await session.get(IdleRun, run_id)

    idle = await one(False)
    assert (idle.status, idle.skip_reason) == ("skipped", idle_gate.USER_ACTIVE)
    assert "manual" not in seen
    manual = await one(True)
    assert manual.status == "done"
    assert seen["manual"] is True


# --- the command ---------------------------------------------------------------------


async def test_garden_now_starts_and_says_so(sessionmaker):
    await _seed(sessionmaker)
    reply = await lens_cmd.command(sessionmaker, _settings(), FrozenClock(NOW), "garden now")
    assert reply == lens_cmd.GARDEN_NOW_STARTED


async def test_garden_now_names_the_reason_it_cannot_run(sessionmaker):
    await _seed(sessionmaker)
    reply = await lens_cmd.command(
        sessionmaker, _settings(LENS_GARDEN_ENABLED=False), FrozenClock(NOW), "garden now"
    )
    assert reply == lens_cmd.GARDEN_NOW_REPLIES[idle_gate.GARDEN_OFF]


def test_every_manual_gate_reason_has_a_reply():
    reasons = {
        idle_gate.DISABLED, idle_gate.PAUSED, idle_gate.WELFARE_COOLDOWN, idle_gate.BUSY,
        idle_gate.MAX_JOBS, idle_gate.IDLE_CAP, idle_gate.RESERVE, idle_gate.GARDEN_OFF,
        idle_gate.LENS_SIZE, idle_gate.NOT_DUE,
    }
    assert reasons <= set(lens_cmd.GARDEN_NOW_REPLIES)


def test_usage_lists_the_command():
    assert "/lens garden now" in lens_cmd.USAGE
