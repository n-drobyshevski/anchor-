"""The `critique` idle kind: the sample, the five-item rubric via
`eval.judge.judge()`, aggregates only (no model text), its kind rule
(no independent judge, no new replies), and welfare/canned/checkin
exclusion (Phase 6 plan section 6.6; milestone 6c's own test list)."""

from __future__ import annotations

import datetime
import decimal

import pytest
from sqlalchemy import select

from app.config import Settings
from app.core.clock import FrozenClock
from app.core.idle.critique import CRITIQUE_ITEMS, has_new_replies_since, run_critique
from app.core.idle.gate import NO_INDEPENDENT_JUDGE, NO_NEW_REPLIES, config_from_settings, idle_gate
from app.core.idle.gate import IdleFacts
from app.db.models import IdleChange, IdleRun, Message, Scene, SpendLedger, UserState
from conftest import FakeLLMProvider

TIMEZONE = "Europe/Paris"

GOOD_JUDGE_TEXT = (
    '{"voice": 5, "one_action": 5, "boundaries": 5, "no_pressure": 5, "third_parties": 5}'
)
LOW_JUDGE_TEXT = (
    '{"voice": 5, "one_action": 5, "boundaries": 2, "no_pressure": 2, "third_parties": 5}'
)


def _clock() -> FrozenClock:
    return FrozenClock(datetime.datetime(2026, 9, 23, 12, 0, tzinfo=datetime.timezone.utc))


async def _seed_state(sessionmaker) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=555, timezone=TIMEZONE))
        await session.commit()


async def _seed_persona_replies(sessionmaker, n: int, *, kind: str = "chat") -> list[int]:
    ids = []
    async with sessionmaker() as session:
        scene = Scene(started_at=_clock().now_utc(), ended_at=None)
        session.add(scene)
        await session.commit()
        await session.refresh(scene)
        for i in range(n):
            user_msg = Message(
                role="user", content=f"вопрос {i}", ooc=False, kind="chat", scene_id=scene.id
            )
            session.add(user_msg)
            await session.commit()
            reply = Message(
                role="assistant", content=f"ответ {i}", ooc=False, kind=kind, scene_id=scene.id
            )
            session.add(reply)
            await session.commit()
            await session.refresh(reply)
            ids.append(reply.id)
    return ids


# --- kind rule --------------------------------------------------------


def test_kind_rule_no_independent_judge():
    config = config_from_settings(Settings(LLM_MODEL_JUDGE=""))
    facts = IdleFacts(
        persona_active=True, local_now=_clock().now_utc(),
        critique_has_new_replies=True, daily_usd_cap=decimal.Decimal("1.00"),
    )
    assert idle_gate("critique", facts, _clock().now_utc(), config) == (False, NO_INDEPENDENT_JUDGE)


def test_kind_rule_judge_same_as_persona_model_is_not_independent():
    config = config_from_settings(Settings(LLM_MODEL_JUDGE="thedrummer/cydonia-24b-v4.1"))
    facts = IdleFacts(
        persona_active=True, local_now=_clock().now_utc(),
        critique_has_new_replies=True, daily_usd_cap=decimal.Decimal("1.00"),
    )
    assert idle_gate("critique", facts, _clock().now_utc(), config) == (False, NO_INDEPENDENT_JUDGE)


def test_kind_rule_no_new_replies():
    config = config_from_settings(Settings())
    facts = IdleFacts(
        persona_active=True, local_now=_clock().now_utc(),
        critique_has_new_replies=False, daily_usd_cap=decimal.Decimal("1.00"),
    )
    assert idle_gate("critique", facts, _clock().now_utc(), config) == (False, NO_NEW_REPLIES)


def test_kind_rule_allows_with_independent_judge_and_new_replies():
    config = config_from_settings(Settings())
    facts = IdleFacts(
        persona_active=True, local_now=_clock().now_utc(),
        critique_has_new_replies=True, daily_usd_cap=decimal.Decimal("1.00"),
    )
    assert idle_gate("critique", facts, _clock().now_utc(), config) == (True, "ok")


# --- has_new_replies_since ------------------------------------------------


async def test_has_new_replies_since_none_ever(sessionmaker):
    async with sessionmaker() as session:
        assert await has_new_replies_since(session, None) is False


async def test_has_new_replies_since_true_after_a_persona_reply(sessionmaker):
    await _seed_persona_replies(sessionmaker, 1)
    async with sessionmaker() as session:
        assert await has_new_replies_since(session, None) is True


async def test_has_new_replies_since_ignores_welfare_and_canned(sessionmaker):
    async with sessionmaker() as session:
        scene = Scene(started_at=_clock().now_utc(), ended_at=None)
        session.add(scene)
        await session.commit()
        await session.refresh(scene)
        session.add_all(
            [
                Message(role="assistant", content="осторожно", ooc=False, kind="welfare", scene_id=scene.id),
                Message(role="assistant", content="шаблон", ooc=False, kind="canned", scene_id=scene.id),
            ]
        )
        await session.commit()
    async with sessionmaker() as session:
        assert await has_new_replies_since(session, None) is False


# --- run_critique -------------------------------------------------------


async def test_run_critique_scores_the_sample_and_stores_only_numbers(sessionmaker):
    clock = _clock()
    await _seed_state(sessionmaker)
    await _seed_persona_replies(sessionmaker, 3)
    async with sessionmaker() as session:
        run = IdleRun(kind="critique", local_date=clock.now_utc().date(), status="queued")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id

    judge_provider = FakeLLMProvider(text=GOOD_JUDGE_TEXT)
    result = await run_critique(
        sessionmaker, Settings(CRITIQUE_SAMPLE=5), clock,
        run_id=run_id, started_at=clock.now_utc(), timezone=TIMEZONE,
        judge_provider=judge_provider,
    )

    assert result.count == 3
    assert result.below_norm == 0
    assert result.mean == {item: 5.0 for item in CRITIQUE_ITEMS}
    assert result.low_ids == ()

    async with sessionmaker() as session:
        ledger = (await session.execute(select(SpendLedger))).scalars().all()
        assert any(row.category == "idle:critique" for row in ledger)
        # Nothing idle-reversible: critique writes no idle_change row.
        changes = (await session.execute(select(IdleChange))).scalars().all()
        assert changes == []


async def test_run_critique_flags_low_boundaries_or_pressure_scores(sessionmaker):
    clock = _clock()
    await _seed_state(sessionmaker)
    reply_ids = await _seed_persona_replies(sessionmaker, 1)

    judge_provider = FakeLLMProvider(text=LOW_JUDGE_TEXT)
    result = await run_critique(
        sessionmaker, Settings(CRITIQUE_SAMPLE=5), clock,
        run_id=1, started_at=clock.now_utc(), timezone=TIMEZONE,
        judge_provider=judge_provider,
    )

    assert result.below_norm == 1
    assert result.low_ids == (reply_ids[0],)


async def test_run_critique_excludes_outbound_context_and_scores_it_anyway(sessionmaker):
    """An outbound reply has no preceding user message -- it is still
    sampled and scored, just with empty prompt_text (module docstring)."""
    clock = _clock()
    await _seed_state(sessionmaker)
    async with sessionmaker() as session:
        scene = Scene(started_at=clock.now_utc(), ended_at=None)
        session.add(scene)
        await session.commit()
        await session.refresh(scene)
        reply = Message(role="assistant", content="доброе утро", ooc=False, kind="outbound", scene_id=scene.id)
        session.add(reply)
        await session.commit()

    judge_provider = FakeLLMProvider(text=GOOD_JUDGE_TEXT)
    result = await run_critique(
        sessionmaker, Settings(CRITIQUE_SAMPLE=5), clock,
        run_id=1, started_at=clock.now_utc(), timezone=TIMEZONE,
        judge_provider=judge_provider,
    )
    assert result.count == 1


async def test_run_critique_never_samples_welfare_ooc_or_canned(sessionmaker):
    clock = _clock()
    await _seed_state(sessionmaker)
    async with sessionmaker() as session:
        scene = Scene(started_at=clock.now_utc(), ended_at=None)
        session.add(scene)
        await session.commit()
        await session.refresh(scene)
        session.add_all(
            [
                Message(role="assistant", content="осторожно", ooc=False, kind="welfare", scene_id=scene.id),
                Message(role="assistant", content="шаблон", ooc=False, kind="canned", scene_id=scene.id),
                Message(role="assistant", content="ooc reply", ooc=True, kind="chat", scene_id=scene.id),
            ]
        )
        await session.commit()

    judge_provider = FakeLLMProvider(text=GOOD_JUDGE_TEXT)
    result = await run_critique(
        sessionmaker, Settings(CRITIQUE_SAMPLE=5), clock,
        run_id=1, started_at=clock.now_utc(), timezone=TIMEZONE,
        judge_provider=judge_provider,
    )
    assert result.count == 0
    assert judge_provider.calls == 0
