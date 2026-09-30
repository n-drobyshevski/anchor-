"""L5: critique records the lens ids behind grounded changes (the L5
spec's section 4; anchor-lens-plan.md section 10).

Attribution, not selection: for each sampled reply at `t =
reply.created_at`, the lens note ids of the grounded persona amendments,
review-proposed standing orders and reflect-grounded notebook entries
that were live in the persona prompt at `t`. These tests pin the three
windows (revoked, retired, closed and updated-after-`t` sources are
excluded), the first-seen union, that nothing changes with the lens off
or nothing grounded (summary byte-identical), that the judge's inputs
are unchanged, that the runner writes both keys into `idle_run.summary`
but never into a log line, and that neither key is in `SAFE_EXTRA_KEYS`.

Synthetic rows only: the lens ids point at no `lens_note` row at all,
which is also what a note that has since left the lens looks like.
"""

from __future__ import annotations

import datetime
import json
import logging

from sqlalchemy import select

from app.config import Settings
from app.core.clock import FrozenClock
from app.core.idle.critique import CritiqueResult, run_critique
from app.db.models import (
    IdleRun,
    Message,
    NotebookEntry,
    PersonaAmendment,
    ReviewProposal,
    Scene,
    StandingOrder,
    UserState,
    WeeklyReview,
)
from conftest import FakeLLMProvider

TIMEZONE = "Europe/Paris"
UTC = datetime.timezone.utc

GOOD_JUDGE_TEXT = (
    '{"voice": 5, "one_action": 5, "boundaries": 5, "no_pressure": 5, "third_parties": 5}'
)

# Three sampled replies, an hour apart, the day before the run.
T1 = datetime.datetime(2026, 9, 22, 18, 0, tzinfo=UTC)
T2 = T1 + datetime.timedelta(hours=1)
T3 = T1 + datetime.timedelta(hours=2)
BEFORE = T1 - datetime.timedelta(days=2)
MINUTE = datetime.timedelta(minutes=1)

BASE_SUMMARY_KEYS = {"count", "below_norm", "mean", "low_ids"}


def _clock() -> FrozenClock:
    return FrozenClock(datetime.datetime(2026, 9, 23, 12, 0, tzinfo=UTC))


def _settings(*, lens: bool) -> Settings:
    return Settings(CRITIQUE_SAMPLE=5, LENS_ENABLED=lens)


async def _seed_state(sessionmaker) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=555, timezone=TIMEZONE))
        await session.commit()


async def _seed_replies(sessionmaker, times: list[datetime.datetime]) -> list[int]:
    """One user turn and one persona reply per time, the reply at
    exactly that time."""
    ids = []
    async with sessionmaker() as session:
        scene = Scene(started_at=times[0] - MINUTE, ended_at=None)
        session.add(scene)
        await session.commit()
        await session.refresh(scene)
        for i, t in enumerate(times):
            session.add(
                Message(
                    role="user", content=f"вопрос {i}", ooc=False, kind="chat",
                    scene_id=scene.id, created_at=t - MINUTE,
                )
            )
            await session.commit()
            reply = Message(
                role="assistant", content=f"ответ {i}", ooc=False, kind="chat",
                scene_id=scene.id, created_at=t,
            )
            session.add(reply)
            await session.commit()
            await session.refresh(reply)
            ids.append(reply.id)
    return ids


async def _proposal(session, *, kind: str, lens_note_ids: list[int] | None) -> int:
    review = (await session.execute(select(WeeklyReview))).scalars().first()
    if review is None:
        review = WeeklyReview(week_start=datetime.date(2026, 9, 14), analysis={})
        session.add(review)
        await session.flush()
    proposal = ReviewProposal(
        review_id=review.id, kind=kind, text="синтетическое предложение",
        status="adopted", lens_note_ids=lens_note_ids,
    )
    session.add(proposal)
    await session.flush()
    return proposal.id


async def _amendment(
    sessionmaker, *, lens_note_ids, activated_at, revoked_at=None, status="active"
) -> None:
    async with sessionmaker() as session:
        proposal_id = await _proposal(session, kind="persona_note", lens_note_ids=lens_note_ids)
        session.add(
            PersonaAmendment(
                text="синтетическая поправка", status=status, proposal_id=proposal_id,
                persona_sha="sha", activated_at=activated_at, revoked_at=revoked_at,
            )
        )
        await session.commit()


async def _order(
    sessionmaker, *, lens_note_ids, decided_at, retired_at=None, status="active", linked=True
) -> None:
    async with sessionmaker() as session:
        proposal_id = (
            await _proposal(session, kind="standing_order", lens_note_ids=lens_note_ids)
            if linked
            else None
        )
        session.add(
            StandingOrder(
                text="синтетическое правило", cadence="daily", status=status,
                source="review" if linked else "user", decided_at=decided_at,
                retired_at=retired_at, review_proposal_id=proposal_id,
            )
        )
        await session.commit()


async def _entry(sessionmaker, *, lens_note_ids, updated_at, closed_at=None) -> None:
    async with sessionmaker() as session:
        closed = closed_at is not None
        session.add(
            NotebookEntry(
                kind="open_thread", text="синтетическая тема", source="anchor",
                active=not closed, closed_by="expiry" if closed else None,
                closed_at=closed_at, created_at=updated_at, updated_at=updated_at,
                lens_note_ids=lens_note_ids,
            )
        )
        await session.commit()


async def _critique(sessionmaker, *, lens: bool, judge: FakeLLMProvider | None = None) -> CritiqueResult:
    clock = _clock()
    return await run_critique(
        sessionmaker, _settings(lens=lens), clock,
        run_id=1, started_at=clock.now_utc(), timezone=TIMEZONE,
        judge_provider=judge or FakeLLMProvider(text=GOOD_JUDGE_TEXT),
    )


async def _seed_grounded(sessionmaker) -> None:
    """An amendment live throughout, an order live from between T1 and
    T2, and a notebook entry live throughout."""
    await _amendment(sessionmaker, lens_note_ids=[7, 3], activated_at=BEFORE)
    await _order(sessionmaker, lens_note_ids=[3, 9], decided_at=T2 - 30 * MINUTE)
    await _entry(sessionmaker, lens_note_ids=[11], updated_at=BEFORE)


# --- attribution --------------------------------------------------------


async def test_union_of_live_sources_in_first_seen_order(sessionmaker):
    await _seed_state(sessionmaker)
    await _seed_replies(sessionmaker, [T1, T2, T3])
    await _seed_grounded(sessionmaker)

    result = await _critique(sessionmaker, lens=True)

    # T1: amendment [7, 3] then entry [11]; T2 adds the order's 9 (its 3
    # is already seen). Notes absent from lens_note are kept.
    assert result.lens_note_ids == (7, 3, 11, 9)
    assert result.lens_grounded == 3
    assert result.summary_extra() == {"lens_note_ids": [7, 3, 11, 9], "lens_grounded": 3}


async def test_only_replies_with_a_live_source_count_as_grounded(sessionmaker):
    await _seed_state(sessionmaker)
    await _seed_replies(sessionmaker, [T1, T2, T3])
    # Live at T1 only: revoked exactly at T2 (the window is half-open).
    await _amendment(sessionmaker, lens_note_ids=[5], activated_at=BEFORE, revoked_at=T2)
    # Live from T3 on only: activated exactly at T3.
    await _amendment(sessionmaker, lens_note_ids=[6], activated_at=T3)

    result = await _critique(sessionmaker, lens=True)

    assert result.lens_note_ids == (5, 6)
    assert result.lens_grounded == 2


async def test_revoked_retired_closed_and_updated_after_sources_are_excluded(sessionmaker):
    await _seed_state(sessionmaker)
    await _seed_replies(sessionmaker, [T1, T2, T3])
    # Amendments: revoked before the sample; never activated (trial).
    await _amendment(sessionmaker, lens_note_ids=[21], activated_at=BEFORE, revoked_at=T1 - MINUTE)
    await _amendment(sessionmaker, lens_note_ids=[22], activated_at=None, status="trial")
    # Orders: retired before the sample; declined; a counter-proposal
    # (the user's own text, no review link); decided after the sample.
    await _order(
        sessionmaker, lens_note_ids=[31], decided_at=BEFORE, retired_at=T1 - MINUTE, status="retired",
    )
    await _order(sessionmaker, lens_note_ids=[32], decided_at=BEFORE, status="declined")
    await _order(sessionmaker, lens_note_ids=[33], decided_at=BEFORE, linked=False)
    await _order(sessionmaker, lens_note_ids=[34], decided_at=T3 + MINUTE)
    # Notebook: closed before the sample; updated after every reply
    # (missed rather than misattributed).
    await _entry(sessionmaker, lens_note_ids=[41], updated_at=BEFORE, closed_at=T1 - MINUTE)
    await _entry(sessionmaker, lens_note_ids=[42], updated_at=T3 + MINUTE)

    result = await _critique(sessionmaker, lens=True)

    assert result.lens_note_ids == ()
    assert result.lens_grounded == 0
    assert result.summary_extra() == {}


async def test_sources_without_lens_ids_never_count(sessionmaker):
    await _seed_state(sessionmaker)
    await _seed_replies(sessionmaker, [T1])
    # A review proposal made without the lens (NULL ids) or with an
    # empty list, and a per-scene notebook entry ('{}').
    await _amendment(sessionmaker, lens_note_ids=None, activated_at=BEFORE)
    await _order(sessionmaker, lens_note_ids=[], decided_at=BEFORE)
    await _entry(sessionmaker, lens_note_ids=[], updated_at=BEFORE)

    result = await _critique(sessionmaker, lens=True)

    assert result.lens_grounded == 0
    assert result.summary_extra() == {}


async def test_a_retired_order_counts_while_it_was_live(sessionmaker):
    await _seed_state(sessionmaker)
    await _seed_replies(sessionmaker, [T1, T2])
    await _order(
        sessionmaker, lens_note_ids=[8], decided_at=BEFORE, retired_at=T2, status="retired",
    )
    # A closed notebook entry still counts for the replies before it closed.
    await _entry(sessionmaker, lens_note_ids=[12], updated_at=BEFORE, closed_at=T1 + MINUTE)

    result = await _critique(sessionmaker, lens=True)

    assert result.lens_note_ids == (8, 12)
    assert result.lens_grounded == 1


# --- lens off, empty and preempted runs ------------------------------------


async def test_lens_off_records_nothing(sessionmaker):
    await _seed_state(sessionmaker)
    await _seed_replies(sessionmaker, [T1, T2, T3])
    await _seed_grounded(sessionmaker)

    result = await _critique(sessionmaker, lens=False)

    assert result.count == 3
    assert result.lens_note_ids == ()
    assert result.lens_grounded == 0
    assert result.summary_extra() == {}


async def test_empty_sample_records_nothing(sessionmaker):
    await _seed_state(sessionmaker)
    await _seed_grounded(sessionmaker)
    judge = FakeLLMProvider(text=GOOD_JUDGE_TEXT)

    result = await _critique(sessionmaker, lens=True, judge=judge)

    assert result.count == 0
    assert judge.calls == 0
    assert result.summary_extra() == {}


async def test_preempted_run_records_nothing(sessionmaker, monkeypatch):
    await _seed_state(sessionmaker)
    await _seed_replies(sessionmaker, [T1])
    await _seed_grounded(sessionmaker)

    async def _preempted(session, clock, started_at):
        return True

    monkeypatch.setattr("app.core.idle.runner.is_preempted", _preempted)

    result = await _critique(sessionmaker, lens=True)

    assert result.preempted is True
    assert result.summary_extra() == {}


def test_summary_extra_shapes():
    assert CritiqueResult(count=1, below_norm=0).summary_extra() == {}
    assert CritiqueResult(
        count=2, below_norm=0, lens_note_ids=(4, 2), lens_grounded=1
    ).summary_extra() == {"lens_note_ids": [4, 2], "lens_grounded": 1}


# --- the judge never learns ------------------------------------------------


async def test_judge_inputs_are_unchanged_by_the_lens(sessionmaker):
    await _seed_state(sessionmaker)
    await _seed_replies(sessionmaker, [T1, T2, T3])
    await _seed_grounded(sessionmaker)

    off = FakeLLMProvider(text=GOOD_JUDGE_TEXT)
    on = FakeLLMProvider(text=GOOD_JUDGE_TEXT)
    off_result = await _critique(sessionmaker, lens=False, judge=off)
    on_result = await _critique(sessionmaker, lens=True, judge=on)

    assert on_result.lens_grounded == 3
    assert on.calls == off.calls == 3
    assert on.received_messages == off.received_messages
    assert on.received_conversation_ids == off.received_conversation_ids
    assert on.received_schemas == off.received_schemas
    assert (on_result.count, on_result.below_norm, on_result.mean, on_result.low_ids) == (
        off_result.count, off_result.below_norm, off_result.mean, off_result.low_ids,
    )


# --- the runner: summary yes, log no ----------------------------------------


async def _run_through_runner(sessionmaker, monkeypatch, *, lens: bool) -> IdleRun:
    from app.core.idle.runner import run_idle

    judge = FakeLLMProvider(text=GOOD_JUDGE_TEXT)

    class _DummyClient:
        async def close(self):
            pass

    monkeypatch.setattr("app.llm.openrouter.build_client", lambda api_key: _DummyClient())
    monkeypatch.setattr(
        "app.core.idle.critique._build_judge_provider", lambda settings, client: judge
    )
    clock = _clock()
    async with sessionmaker() as session:
        run = IdleRun(kind="critique", local_date=clock.now_utc().date(), status="queued")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id

    await run_idle(
        sessionmaker, _settings(lens=lens), FakeLLMProvider(), FakeLLMProvider(), clock,
        run_id=run_id,
    )
    async with sessionmaker() as session:
        return await session.get(IdleRun, run_id)


async def test_runner_writes_both_keys_but_never_logs_them(sessionmaker, monkeypatch):
    import app.core.idle.runner as runner_module
    from app.log import _JsonFormatter

    await _seed_state(sessionmaker)
    await _seed_replies(sessionmaker, [T1, T2, T3])
    await _seed_grounded(sessionmaker)

    # The runner's own `logger.info` calls, captured directly: caplog
    # depends on logging config other suites may have replaced.
    info_calls: list[logging.LogRecord] = []

    def _info(msg, *args, extra=None, **kwargs):
        info_calls.append(
            runner_module.logger.makeRecord(
                runner_module.logger.name, logging.INFO, __file__, 0, msg, args, None, extra=extra,
            )
        )

    monkeypatch.setattr(runner_module.logger, "info", _info)
    run = await _run_through_runner(sessionmaker, monkeypatch, lens=True)

    assert run.status == "done", (run.status, run.skip_reason)
    assert run.summary["lens_note_ids"] == [7, 3, 11, 9]
    assert run.summary["lens_grounded"] == 3
    assert set(run.summary) == BASE_SUMMARY_KEYS | {"lens_note_ids", "lens_grounded"}

    done = [record for record in info_calls if record.getMessage() == "idle run done"]
    assert len(done) == 1
    line = json.loads(_JsonFormatter().format(done[0]))
    assert "lens_note_ids" not in line
    assert "lens_grounded" not in line


async def test_runner_summary_is_unchanged_with_the_lens_off(sessionmaker, monkeypatch):
    await _seed_state(sessionmaker)
    await _seed_replies(sessionmaker, [T1, T2, T3])
    await _seed_grounded(sessionmaker)

    run = await _run_through_runner(sessionmaker, monkeypatch, lens=False)

    assert run.status == "done", (run.status, run.skip_reason)
    assert set(run.summary) == BASE_SUMMARY_KEYS


async def test_runner_summary_is_unchanged_when_nothing_is_grounded(sessionmaker, monkeypatch):
    await _seed_state(sessionmaker)
    await _seed_replies(sessionmaker, [T1, T2, T3])

    run = await _run_through_runner(sessionmaker, monkeypatch, lens=True)

    assert run.status == "done", (run.status, run.skip_reason)
    assert set(run.summary) == BASE_SUMMARY_KEYS


def test_neither_key_is_a_safe_log_key():
    """Otherwise the runner's "idle run done" line, which spreads the
    summary into `extra`, would show note ids to Claude Code."""
    from app.log import SAFE_EXTRA_KEYS

    assert "lens_note_ids" not in SAFE_EXTRA_KEYS
    assert "lens_grounded" not in SAFE_EXTRA_KEYS


def test_critique_reaches_no_lens_module():
    """Attribution, not selection (plan section 10): critique reads ids
    through app.db.models only -- no app.vault, no lens_select."""
    import ast
    import pathlib

    import app.core.idle.critique as critique_module

    tree = ast.parse(pathlib.Path(critique_module.__file__).read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    assert not any(m.startswith("app.vault") for m in modules), modules
    assert "app.core.lens_select" not in modules
    assert "app.core.lens_review" not in modules
