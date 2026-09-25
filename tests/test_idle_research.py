"""The `research` idle kind (Phase 6 plan section 6.5; milestone 6d).

Covers: topic selection (oldest `last_run_at` first, NULL first, user
topics only -- never invents one), the planner's priority slot, the
shared `/study` quota in both directions, the unchanged pipeline call
shape, no completion message, cards staying pending, and preemption.
`tests/test_idle_isolation.py` covers the "zero Telegram calls" property
generically for every kind including this one; this file is about
research's own behaviour.
"""

from __future__ import annotations

import datetime
import decimal

import pytest
from sqlalchemy import select

from app.config import Settings
from app.core.clock import FrozenClock
from app.core.idle import BACKFILL, RESEARCH
from app.core.idle.facts import load_idle_facts
from app.core.idle.gate import (
    NO_TOPICS,
    OK,
    QUOTA_USED,
    RESEARCH_DISABLED,
    config_from_settings,
    idle_gate,
)
from app.core.idle.planner import plan_idle
from app.core.idle.research import pick_topic
from app.core.idle.runner import run_idle
from app.db.models import IdleRun, InterestTopic, StudyCard, StudyJob, TelegramUpdate, UserState
from app.llm.provider import LLMResponse, LLMUsage
from app.research import jobs as research_jobs
from app.research import search
from app.research.fetch import Clip
from conftest import FakeLLMProvider

pytestmark = pytest.mark.asyncio

TZ = "Europe/Paris"
TOPIC_TEXT = "бессонница"
CLIP_TEXT = "Спать лучше в прохладной комнате."
CARD_JSON = (
    '{"cards": [{"kind": "technique", "text": "Совет со страницы.", '
    f'"quote": "{CLIP_TEXT}", "risk": "low"}}]}}'
)


def _clock(day: int = 23, hour: int = 12) -> FrozenClock:
    return FrozenClock(datetime.datetime(2026, 9, day, hour, 0, tzinfo=datetime.timezone.utc))


def _settings(**kw) -> Settings:
    base = dict(RESEARCH_ENABLED=True, DAILY_USD_CAP=10.0, IDLE_ENABLED=True)
    base.update(kw)
    return Settings(**base)


async def _seed_state(sessionmaker) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=555, timezone=TZ))
        await session.commit()


async def _add_topic(sessionmaker, *, text=TOPIC_TEXT, packet="forums", last_run_at=None, active=True) -> int:
    async with sessionmaker() as session:
        topic = InterestTopic(text=text, packet=packet, active=active, last_run_at=last_run_at)
        session.add(topic)
        await session.commit()
        await session.refresh(topic)
        return topic.id


def _clip() -> Clip:
    return Clip(
        url="https://reddit.com/r/sleep/comment",
        domain="reddit.com",
        title="Совет",
        text=CLIP_TEXT,
        text_sha256="deadbeef",
        http_status=200,
    )


def _patch_pipeline(monkeypatch, *, urls: tuple[str, ...] = None) -> None:
    """Stand in for the network the way tests/test_research_jobs.py's own
    fixtures do -- see app/core/idle/research.py's own docstring on why
    the pipeline itself is called unchanged."""
    clip = _clip()
    urls = urls if urls is not None else (clip.url,)

    async def _fake_fetch(url, **kwargs):
        return clip

    async def _fake_find_urls(provider, **kwargs):
        usage = LLMUsage(
            input_tokens=100, cached_tokens=0, output_tokens=3,
            cost_usd=decimal.Decimal("0.002"),
        )
        responses = (LLMResponse(text="", usage=usage, model="fake-safety"),) if urls else ()
        return search.SearchOutcome(urls=urls, responses=responses)

    monkeypatch.setattr("app.research.jobs.default_fetch", _fake_fetch)
    monkeypatch.setattr("app.research.search.find_urls", _fake_find_urls)


# --- pick_topic: user topics only, oldest last_run_at first -------------


async def test_pick_topic_prefers_never_run_over_run_long_ago(sessionmaker):
    old = await _add_topic(
        sessionmaker, text="старая тема",
        last_run_at=datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc),
    )
    never_run = await _add_topic(sessionmaker, text="новая тема", last_run_at=None)

    async with sessionmaker() as session:
        picked = await pick_topic(session)
    assert picked.id == never_run
    assert old != picked.id


async def test_pick_topic_then_oldest_last_run_at(sessionmaker):
    newer = await _add_topic(
        sessionmaker, text="недавняя",
        last_run_at=datetime.datetime(2026, 9, 20, tzinfo=datetime.timezone.utc),
    )
    older = await _add_topic(
        sessionmaker, text="давняя",
        last_run_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
    )
    async with sessionmaker() as session:
        picked = await pick_topic(session)
    assert picked.id == older
    assert newer != picked.id


async def test_pick_topic_ignores_inactive_topics(sessionmaker):
    await _add_topic(sessionmaker, text="снята", active=False, last_run_at=None)
    async with sessionmaker() as session:
        assert await pick_topic(session) is None


async def test_pick_topic_returns_none_without_any_topics(sessionmaker):
    async with sessionmaker() as session:
        assert await pick_topic(session) is None


# --- gate: never invents a topic, disabled, quota -----------------------


async def test_gate_no_topics_never_invents_one(sessionmaker):
    clock = _clock()
    await _seed_state(sessionmaker)
    settings = _settings()
    async with sessionmaker() as session:
        facts = await load_idle_facts(session, settings, clock, TZ)
    config = config_from_settings(settings)
    assert idle_gate(RESEARCH, facts, clock.now_utc(), config) == (False, NO_TOPICS)


async def test_gate_research_disabled(sessionmaker):
    clock = _clock()
    await _seed_state(sessionmaker)
    await _add_topic(sessionmaker)
    settings = _settings(RESEARCH_ENABLED=False)
    async with sessionmaker() as session:
        facts = await load_idle_facts(session, settings, clock, TZ)
    config = config_from_settings(settings)
    assert idle_gate(RESEARCH, facts, clock.now_utc(), config) == (False, RESEARCH_DISABLED)


async def test_gate_allows_with_a_topic_and_free_quota(sessionmaker):
    clock = _clock()
    await _seed_state(sessionmaker)
    await _add_topic(sessionmaker)
    settings = _settings()
    async with sessionmaker() as session:
        facts = await load_idle_facts(session, settings, clock, TZ)
    config = config_from_settings(settings)
    assert idle_gate(RESEARCH, facts, clock.now_utc(), config) == (True, OK)


# --- the shared quota, both directions -----------------------------------


async def test_a_users_study_today_blocks_idle_research(sessionmaker):
    """Plan section 6.5: "it runs only if the user hasn't used /study
    today". The counter is the same `study_job` table for both."""
    clock = _clock()
    await _seed_state(sessionmaker)
    await _add_topic(sessionmaker)
    settings = _settings(RESEARCH_JOBS_PER_DAY=1, PACKET_FORUMS=("reddit.com",))

    async with sessionmaker() as session:
        _job_id, refusal = await research_jobs.enqueue_study(
            session, settings, clock, timezone=TZ, packet="forums", topic="что угодно"
        )
        await session.commit()
    assert refusal is None

    async with sessionmaker() as session:
        facts = await load_idle_facts(session, settings, clock, TZ)
    config = config_from_settings(settings)
    assert idle_gate(RESEARCH, facts, clock.now_utc(), config) == (False, QUOTA_USED)


async def test_idle_research_today_blocks_a_later_study(sessionmaker, monkeypatch):
    """The reverse direction: idle runs first, so the user's own /study
    later the same day behaves exactly as if they had already used it."""
    clock = _clock()
    await _seed_state(sessionmaker)
    await _add_topic(sessionmaker)
    settings = _settings(RESEARCH_JOBS_PER_DAY=1, PACKET_FORUMS=("reddit.com",))
    _patch_pipeline(monkeypatch)
    safety_provider = FakeLLMProvider(text=CARD_JSON)

    async with sessionmaker() as session:
        run = IdleRun(kind=RESEARCH, local_date=clock.now_utc().date(), status="queued")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id

    await run_idle(
        sessionmaker, settings, FakeLLMProvider(), safety_provider, clock, run_id=run_id
    )

    async with sessionmaker() as session:
        assert (await session.get(IdleRun, run_id)).status == "done"
        _job_id, refusal = await research_jobs.enqueue_study(
            session, settings, clock, timezone=TZ, packet="forums", topic="что угодно"
        )
    assert refusal == research_jobs.QUOTA


# --- the pipeline is invoked unchanged ------------------------------------


async def test_pipeline_is_called_with_the_same_argument_shape_as_study(sessionmaker, monkeypatch):
    """Spy on `run_research_job` itself: idle research must call it with
    exactly the keyword shape `app/worker.py`'s own `RESEARCH` branch
    does for a `/study` job -- `job_id`, `url=None`, `clock`, `timezone`
    -- never extra keywords research's own idle wrapper might otherwise
    be tempted to add (a `spend_category`, say)."""
    clock = _clock()
    await _seed_state(sessionmaker)
    await _add_topic(sessionmaker)
    settings = _settings()
    _patch_pipeline(monkeypatch)

    seen: dict = {}
    real = research_jobs.run_research_job

    async def _spy(session, settings, provider, **kwargs):
        seen.update(kwargs)
        return await real(session, settings, provider, **kwargs)

    monkeypatch.setattr("app.research.jobs.run_research_job", _spy)

    async with sessionmaker() as session:
        run = IdleRun(kind=RESEARCH, local_date=clock.now_utc().date(), status="queued")
        session.add(run)
        await session.commit()
        run_id = run.id

    safety_provider = FakeLLMProvider(text=CARD_JSON)
    await run_idle(
        sessionmaker, settings, FakeLLMProvider(), safety_provider, clock, run_id=run_id
    )

    assert set(seen.keys()) == {"job_id", "url", "clock", "timezone"}
    assert seen["url"] is None
    assert seen["timezone"] == TZ
    assert isinstance(seen["job_id"], int)


# --- cards stay pending, last_run_at is set -------------------------------


async def test_run_research_produces_pending_cards_and_sets_last_run_at(sessionmaker, monkeypatch):
    clock = _clock()
    await _seed_state(sessionmaker)
    topic_id = await _add_topic(sessionmaker)
    settings = _settings()
    _patch_pipeline(monkeypatch)
    safety_provider = FakeLLMProvider(text=CARD_JSON)

    async with sessionmaker() as session:
        run = IdleRun(kind=RESEARCH, local_date=clock.now_utc().date(), status="queued")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id

    await run_idle(
        sessionmaker, settings, FakeLLMProvider(), safety_provider, clock, run_id=run_id
    )

    async with sessionmaker() as session:
        run = await session.get(IdleRun, run_id)
        assert run.status == "done"
        assert run.reversible is False
        assert run.summary == {"topic_id": topic_id, "cards": 1}
        topic = await session.get(InterestTopic, topic_id)
        assert topic.last_run_at == clock.now_utc()
        cards = (await session.execute(select(StudyCard))).scalars().all()
        assert len(cards) == 1
        assert cards[0].status == "pending"


async def test_run_research_reports_no_topic_id_in_summary_text(sessionmaker, monkeypatch):
    """plan section 8: idle_run.summary is numbers/ids only -- the topic's
    own (user-authored) text never appears in it."""
    clock = _clock()
    await _seed_state(sessionmaker)
    await _add_topic(sessionmaker, text="секретная тема, никогда не в логах")
    settings = _settings()
    _patch_pipeline(monkeypatch)
    safety_provider = FakeLLMProvider(text=CARD_JSON)

    async with sessionmaker() as session:
        run = IdleRun(kind=RESEARCH, local_date=clock.now_utc().date(), status="queued")
        session.add(run)
        await session.commit()
        run_id = run.id

    await run_idle(
        sessionmaker, settings, FakeLLMProvider(), safety_provider, clock, run_id=run_id
    )

    async with sessionmaker() as session:
        run = await session.get(IdleRun, run_id)
        assert "секретная" not in str(run.summary)
        assert set(run.summary.keys()) == {"topic_id", "cards"}


# --- preemption ------------------------------------------------------------


async def test_a_user_update_before_the_run_starts_skips_it_as_preempted(sessionmaker, monkeypatch):
    clock = FrozenClock(datetime.datetime(2020, 1, 1, 12, 0, tzinfo=datetime.timezone.utc))
    await _seed_state(sessionmaker)
    await _add_topic(sessionmaker)
    settings = _settings()
    _patch_pipeline(monkeypatch)

    async with sessionmaker() as session:
        run = IdleRun(kind=RESEARCH, local_date=clock.now_utc().date(), status="queued")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id

    async with sessionmaker() as session:
        session.add(TelegramUpdate(update_id=1, payload={}))
        await session.commit()

    provider = FakeLLMProvider(text="не должно быть вызвано")
    safety_provider = FakeLLMProvider(text=CARD_JSON)
    await run_idle(sessionmaker, settings, provider, safety_provider, clock, run_id=run_id)

    async with sessionmaker() as session:
        run = await session.get(IdleRun, run_id)
        assert run.status == "skipped"
        assert run.skip_reason == "preempted"
        jobs_written = (await session.execute(select(StudyJob))).scalars().all()
        assert jobs_written == [], "nothing must be queued or spent once preempted"
        topic = (await session.execute(select(InterestTopic))).scalars().first()
        assert topic.last_run_at is None


# --- the planner's own slot -------------------------------------------------


async def test_planner_picks_research_when_nothing_else_is_eligible(sessionmaker, monkeypatch):
    clock = _clock()
    await _seed_state(sessionmaker)
    await _add_topic(sessionmaker)
    settings = _settings(CANARY_DOW=5)  # 2026-09-23 is a Wednesday (3)
    _patch_pipeline(monkeypatch)

    async with sessionmaker() as session:
        run_id = await plan_idle(session, settings, clock)
        await session.commit()

    assert run_id is not None
    async with sessionmaker() as session:
        run = await session.get(IdleRun, run_id)
        assert run.kind == RESEARCH


async def test_planner_prefers_backfill_over_research_when_both_eligible(sessionmaker):
    """Priority order (plan section 5): backfill (1) beats research (6)."""
    from app.db.models import Message, Scene

    clock = _clock()
    await _seed_state(sessionmaker)
    await _add_topic(sessionmaker)
    settings = _settings()
    now = clock.now_utc()
    async with sessionmaker() as session:
        scene = Scene(
            started_at=now - datetime.timedelta(hours=2),
            ended_at=now - datetime.timedelta(hours=1),
        )
        session.add(scene)
        await session.commit()
        await session.refresh(scene)
        session.add_all(
            [
                Message(role="user", content="1", ooc=False, kind="chat", scene_id=scene.id),
                Message(role="assistant", content="2", ooc=False, kind="chat", scene_id=scene.id),
                Message(role="user", content="3", ooc=False, kind="chat", scene_id=scene.id),
            ]
        )
        await session.commit()

    async with sessionmaker() as session:
        run_id = await plan_idle(session, settings, clock)
        await session.commit()

    async with sessionmaker() as session:
        run = await session.get(IdleRun, run_id)
        assert run.kind == BACKFILL


async def test_planner_never_picks_research_without_a_topic(sessionmaker):
    clock = _clock()  # 2026-09-23 is a Wednesday; keep canary off its own dow
    await _seed_state(sessionmaker)
    settings = _settings(CANARY_DOW=5)

    async with sessionmaker() as session:
        run_id = await plan_idle(session, settings, clock)
        await session.commit()

    assert run_id is None


async def test_idle_research_cost_counts_toward_the_idle_cap_once(sessionmaker):
    """The pipeline ledgers under `research`, so today_idle_usd reads the
    run's cost from idle_run instead -- counted once, not twice, and a
    user's own /study spend (no idle_run row) is not idle spend."""
    from app.core.spend import today_idle_usd
    from app.db.models import SpendLedger

    clock = _clock()
    today = datetime.date(2026, 9, 23)
    async with sessionmaker() as session:
        session.add(IdleRun(kind=RESEARCH, local_date=today, status="done",
                            usd_cost=decimal.Decimal("0.03")))
        session.add(IdleRun(kind=BACKFILL, local_date=today, status="done",
                            usd_cost=decimal.Decimal("0.01")))
        for category, cost in (("research", "0.03"), ("research", "0.02"), ("idle:backfill", "0.01")):
            session.add(SpendLedger(local_date=today, category=category, model="m",
                                    tokens_in=1, tokens_cached=0, tokens_out=1,
                                    usd_cost=decimal.Decimal(cost)))
        await session.commit()
        total = await today_idle_usd(session, clock, TZ)
    assert total == decimal.Decimal("0.04")
