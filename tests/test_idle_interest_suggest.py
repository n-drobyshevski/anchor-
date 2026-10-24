"""The weekly idle kind `interest_suggest` (anchor-interest-suggestions-
plan.md section 5a): its gate, its facts, its body, the runner's
outcome mapping, the idle spend, the send hook after an idle run, and
its /digest line.

Every model call is a `FakeLLMProvider` with scripted JSON (patched in
as app/research/suggest.py's `build_suggest_provider`, the seam the
production path builds through); the topics and lens notes are
synthetic rows in the throwaway database.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import json

import pytest
from sqlalchemy import select

from app.config import Settings
from app.core.clock import FrozenClock
from app.core.idle import INTEREST_SUGGEST, KINDS, LENS_GARDEN, LENS_RESEARCH, RESEARCH
from app.core.idle import gate as idle_gate
from app.core.idle import interest_suggest as weekly
from app.core.idle.digest import INTEREST_SUGGEST_LINE, build_digest
from app.core.idle.facts import load_idle_facts
from app.core.idle.gate import (
    DAILY_LIMIT,
    NO_SUGGEST_PACKET,
    NOTHING_TO_SUGGEST,
    OK,
    RESEARCH_DISABLED,
    SUGGEST_INTERVAL,
    SUGGEST_WEEKLY_OFF,
    SUGGESTED_RECENTLY,
    UNDECIDED,
    UNDECIDED_FOR,
    IdleConfig,
    IdleFacts,
    config_from_settings,
)
from app.core.idle.gate import idle_gate as gate
from app.core.idle.planner import PRIORITY, plan_idle
from app.core.idle.runner import run_idle
from app.core.spend import today_idle_usd
from app.db.models import (
    IdleRun,
    InterestSuggestBatch,
    InterestSuggestion,
    InterestTopic,
    Job,
    LensNote,
    SpendLedger,
    UserState,
    VaultFile,
)
from app.llm.provider import LLMError, LLMUsage
from app.research import suggest
from conftest import FakeLLMProvider, make_bot

TZ = "Europe/Paris"
UTC = datetime.timezone.utc
# 14:00 in Paris: inside the default idle window, outside quiet hours.
NOW = datetime.datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
# 02:00 in Paris, inside the default quiet hours.
NIGHT = datetime.datetime(2026, 10, 24, 0, 0, tzinfo=UTC)
COST = decimal.Decimal("0.0021")


def _settings(**kw) -> Settings:
    base = dict(
        RESEARCH_ENABLED=True,
        IDLE_ENABLED=True,
        DAILY_USD_CAP=10.0,
        PACKET_FORUMS="reddit.com",
        PACKET_REF="en.wikipedia.org",
        PACKET_GUIDES="",
        # The garden and lens research out of the way, so the planner's
        # pick is this kind's own verdict.
        LENS_GARDEN_ENABLED=False,
    )
    base.update(kw)
    return Settings(**base)


def _reply(*texts: str) -> str:
    return json.dumps(
        {
            "suggestions": [
                {"text": text, "packet": "ref", "why": "Рядом с сохранённой темой."}
                for text in texts
            ]
        },
        ensure_ascii=False,
    )


def _provider(text: str | None = None, **kw) -> FakeLLMProvider:
    usage = LLMUsage(input_tokens=300, cached_tokens=0, output_tokens=80, cost_usd=COST)
    return FakeLLMProvider(
        text=text if text is not None else _reply("гигиена сна у подростков"),
        usage=usage, model="fake-safety", **kw,
    )


def _patch_provider(monkeypatch, provider: FakeLLMProvider) -> None:
    class _Client:
        async def close(self):
            pass

    monkeypatch.setattr("app.llm.openrouter.build_client", lambda api_key: _Client())
    monkeypatch.setattr(
        "app.research.suggest.build_suggest_provider", lambda settings, client: provider
    )


async def _seed(sessionmaker, *, topic: bool = True, **state) -> None:
    state.setdefault("last_user_msg_at", NOW - datetime.timedelta(hours=10))
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=555, timezone=TZ, **state))
        if topic:
            session.add(InterestTopic(text="бессонница", packet="forums", active=True))
        await session.commit()


async def _batch(sessionmaker, *, created_at, status="done", origin="manual", open_=0) -> int:
    async with sessionmaker() as session:
        batch = InterestSuggestBatch(
            origin=origin, status=status, local_date=created_at.date(), created_at=created_at,
        )
        session.add(batch)
        await session.flush()
        for position in range(1, open_ + 1):
            session.add(
                InterestSuggestion(
                    batch_id=batch.id, position=position, text=f"тема {position}",
                    packet="ref", why="почему", status="open",
                )
            )
        await session.commit()
        return batch.id


async def _queued_run(sessionmaker) -> int:
    async with sessionmaker() as session:
        run = IdleRun(kind=INTEREST_SUGGEST, local_date=NOW.date(), status="queued")
        session.add(run)
        await session.commit()
        return run.id


async def _rows(sessionmaker, model):
    async with sessionmaker() as session:
        return list((await session.execute(select(model).order_by(model.id))).scalars())


# --- the gate, pure ------------------------------------------------------------------


def _config(**overrides) -> IdleConfig:
    base = dict(
        enabled=True,
        after_h=3,
        usd_cap=decimal.Decimal("0.25"),
        reserve_usd=decimal.Decimal("0.50"),
        job_usd_cap=decimal.Decimal("0.05"),
        max_jobs_per_day=8,
        window_start=datetime.time(0, 0),
        window_end=datetime.time(23, 59),
        undo_days=7,
        research_enabled=True,
        suggest_weekly=True,
        suggest_packets=True,
    )
    base.update(overrides)
    return IdleConfig(**base)


def _facts(**overrides) -> IdleFacts:
    base = dict(
        persona_active=True,
        local_now=NOW,
        daily_usd_cap=decimal.Decimal("10"),
        suggest_has_source=True,
    )
    base.update(overrides)
    return IdleFacts(**base)


def test_the_kind_is_known_everywhere_an_idle_kind_must_be():
    assert INTEREST_SUGGEST in KINDS
    assert INTEREST_SUGGEST in idle_gate.KIND_RULES
    assert idle_gate.KIND_DAILY_MAX[INTEREST_SUGGEST] == 1


def test_it_comes_after_the_garden_and_lens_research_and_before_research():
    """After the garden, so its new gaps exist before suggestions are
    drawn from the same lens notes; lens research keeps its place right
    after the garden (the L4 spec section 2)."""
    assert PRIORITY.index(LENS_GARDEN) < PRIORITY.index(INTEREST_SUGGEST)
    assert PRIORITY.index(LENS_GARDEN) + 1 == PRIORITY.index(LENS_RESEARCH)
    assert PRIORITY.index(LENS_RESEARCH) + 1 == PRIORITY.index(INTEREST_SUGGEST)
    assert PRIORITY.index(INTEREST_SUGGEST) < PRIORITY.index(RESEARCH)


def test_an_open_lane_is_allowed():
    assert gate(INTEREST_SUGGEST, _facts(), NOW, _config()) == (True, OK)


@pytest.mark.parametrize(
    ("facts", "config", "reason"),
    [
        ({}, {"research_enabled": False}, RESEARCH_DISABLED),
        ({}, {"suggest_weekly": False}, SUGGEST_WEEKLY_OFF),
        ({"suggest_has_source": False}, {}, NOTHING_TO_SUGGEST),
        ({}, {"suggest_packets": False}, NO_SUGGEST_PACKET),
        (
            {"suggest_last_batch_at": NOW - SUGGEST_INTERVAL + datetime.timedelta(minutes=1)},
            {},
            SUGGESTED_RECENTLY,
        ),
        (
            {"suggest_open_batch_at": NOW - UNDECIDED_FOR + datetime.timedelta(minutes=1)},
            {},
            UNDECIDED,
        ),
    ],
    ids=["research_off", "weekly_off", "nothing", "no_packet", "recent", "undecided"],
)
def test_each_reason(facts, config, reason):
    assert gate(INTEREST_SUGGEST, _facts(**facts), NOW, _config(**config)) == (False, reason)


def test_the_reasons_come_in_plan_order():
    """Every condition failing at once: each fix uncovers the next, in
    the order section 5a lists them."""
    facts = dict(
        suggest_has_source=False,
        suggest_last_batch_at=NOW - datetime.timedelta(days=1),
        suggest_open_batch_at=NOW - datetime.timedelta(days=1),
    )
    config = dict(research_enabled=False, suggest_weekly=False, suggest_packets=False)
    order = [
        (RESEARCH_DISABLED, ("config", "research_enabled", True)),
        (SUGGEST_WEEKLY_OFF, ("config", "suggest_weekly", True)),
        (NOTHING_TO_SUGGEST, ("facts", "suggest_has_source", True)),
        (NO_SUGGEST_PACKET, ("config", "suggest_packets", True)),
        (SUGGESTED_RECENTLY, ("facts", "suggest_last_batch_at", None)),
        (UNDECIDED, ("facts", "suggest_open_batch_at", None)),
    ]
    for reason, (where, name, value) in order:
        assert gate(INTEREST_SUGGEST, _facts(**facts), NOW, _config(**config)).reason == reason
        (facts if where == "facts" else config)[name] = value
    assert gate(INTEREST_SUGGEST, _facts(**facts), NOW, _config(**config)) == (True, OK)


def test_the_windows_end_where_they_say():
    six_days = _facts(suggest_last_batch_at=NOW - SUGGEST_INTERVAL)
    assert gate(INTEREST_SUGGEST, six_days, NOW, _config()) == (True, OK)
    fortnight = _facts(suggest_open_batch_at=NOW - UNDECIDED_FOR)
    assert gate(INTEREST_SUGGEST, fortnight, NOW, _config()) == (True, OK)


def test_once_a_day_binds_before_the_kind_rule():
    facts = _facts(kind_runs_today={INTEREST_SUGGEST: 1})
    assert gate(INTEREST_SUGGEST, facts, NOW, _config()) == (False, DAILY_LIMIT)


@pytest.mark.parametrize(
    "packets",
    [
        {},
        {"PACKET_FORUMS": "", "PACKET_REF": ""},
        {"PACKET_FORUMS": "", "PACKET_REF": "", "PACKET_GUIDES": "example.org"},
    ],
)
def test_the_packet_switch_equals_offered_packets(packets):
    """gate.py may not import app/research/suggest.py; the two must agree."""
    settings = _settings(**packets)
    assert config_from_settings(settings).suggest_packets == bool(
        suggest.offered_packets(settings)
    )


def test_the_config_reads_the_weekly_switch():
    assert config_from_settings(_settings()).suggest_weekly is True
    assert config_from_settings(_settings(INTEREST_SUGGEST_WEEKLY=False)).suggest_weekly is False


# --- the facts -----------------------------------------------------------------------


async def test_the_facts_are_not_queried_while_the_weekly_batch_is_off(sessionmaker):
    await _seed(sessionmaker)
    await _batch(sessionmaker, created_at=NOW - datetime.timedelta(days=1), open_=1)
    for settings in (_settings(INTEREST_SUGGEST_WEEKLY=False), _settings(RESEARCH_ENABLED=False)):
        async with sessionmaker() as session:
            facts = await load_idle_facts(session, settings, FrozenClock(NOW), TZ)
        assert (
            facts.suggest_has_source, facts.suggest_last_batch_at, facts.suggest_open_batch_at
        ) == (False, None, None)


async def test_a_topic_or_an_active_lens_is_a_source(sessionmaker):
    await _seed(sessionmaker, topic=False)
    async with sessionmaker() as session:
        assert not (await weekly.gate_facts(session, _settings(LENS_ENABLED=True), NOW)).has_source
        file = VaultFile(path="Lens/variety.md", role="note", note_class="knowledge")
        session.add(file)
        await session.flush()
        session.add(
            LensNote(
                vault_file_id=file.id, kind="concept", title="Requisite variety",
                summary="A regulator needs as many states.", body="Body.",
                body_hash="v".ljust(64, "x"), chars=5,
            )
        )
        await session.commit()
        assert (await weekly.gate_facts(session, _settings(LENS_ENABLED=True), NOW)).has_source
        assert not (await weekly.gate_facts(session, _settings(LENS_ENABLED=False), NOW)).has_source
        session.add(InterestTopic(text="бессонница", packet="forums", active=True))
        await session.commit()
        assert (await weekly.gate_facts(session, _settings(LENS_ENABLED=False), NOW)).has_source


async def test_the_last_batch_counts_done_and_live_queued_batches_only(sessionmaker):
    await _seed(sessionmaker)
    await _batch(sessionmaker, created_at=NOW - datetime.timedelta(days=3), status="failed")
    await _batch(
        sessionmaker, created_at=NOW - suggest.QUEUED_STALE - datetime.timedelta(minutes=1),
        status="queued",
    )
    async with sessionmaker() as session:
        facts = await weekly.gate_facts(session, _settings(), NOW)
    assert facts.last_batch_at is None

    done_at = NOW - datetime.timedelta(days=4)
    await _batch(sessionmaker, created_at=done_at, origin="weekly")
    async with sessionmaker() as session:
        assert (await weekly.gate_facts(session, _settings(), NOW)).last_batch_at == done_at

    queued_at = NOW - datetime.timedelta(minutes=5)
    await _batch(sessionmaker, created_at=queued_at, status="queued")
    async with sessionmaker() as session:
        assert (await weekly.gate_facts(session, _settings(), NOW)).last_batch_at == queued_at


async def test_undecided_is_the_newest_done_batch_with_an_open_suggestion(sessionmaker):
    await _seed(sessionmaker)
    older = NOW - datetime.timedelta(days=9)
    await _batch(sessionmaker, created_at=older, open_=2)
    async with sessionmaker() as session:
        assert (await weekly.gate_facts(session, _settings(), NOW)).open_batch_at == older

    # A newer done batch with nothing open (every suggestion decided, or
    # none survived validation) answers for the user: not undecided.
    await _batch(sessionmaker, created_at=NOW - datetime.timedelta(days=7))
    async with sessionmaker() as session:
        assert (await weekly.gate_facts(session, _settings(), NOW)).open_batch_at is None


async def test_a_recent_manual_batch_pushes_the_weekly_one_back(sessionmaker):
    await _seed(sessionmaker)
    await _batch(sessionmaker, created_at=NOW - datetime.timedelta(days=2), origin="manual")
    async with sessionmaker() as session:
        facts = await load_idle_facts(session, _settings(), FrozenClock(NOW), TZ)
    assert gate(INTEREST_SUGGEST, facts, NOW, config_from_settings(_settings())) == (
        False,
        SUGGESTED_RECENTLY,
    )


# --- planning and the run ------------------------------------------------------------


async def test_the_planner_picks_it_and_the_run_makes_a_weekly_batch(sessionmaker, monkeypatch):
    await _seed(sessionmaker)
    clock = FrozenClock(NOW)
    settings = _settings()
    provider = _provider(_reply("гигиена сна у подростков", "дневной сон"))
    _patch_provider(monkeypatch, provider)

    async with sessionmaker() as session:
        run_id = await plan_idle(session, settings, clock)
    assert run_id is not None
    async with sessionmaker() as session:
        assert (await session.get(IdleRun, run_id)).kind == INTEREST_SUGGEST

    await run_idle(
        sessionmaker, settings, FakeLLMProvider(), FakeLLMProvider(), clock, run_id=run_id
    )

    assert provider.calls == 1
    (batch,) = await _rows(sessionmaker, InterestSuggestBatch)
    assert (batch.origin, batch.status, batch.local_date) == ("weekly", "done", NOW.date())
    assert [s.text for s in await _rows(sessionmaker, InterestSuggestion)] == [
        "гигиена сна у подростков",
        "дневной сон",
    ]
    async with sessionmaker() as session:
        run = await session.get(IdleRun, run_id)
    assert run.status == "done"
    assert run.reversible is False
    assert run.summary == {
        "suggest_batch_id": batch.id, "suggestions": 2, "proposed": 2, "dropped": 0, "expired": 0,
    }
    # The spend: ledgered once, under the command's category, and folded
    # into the run -- which is what IDLE_USD_CAP sees.
    (ledger,) = await _rows(sessionmaker, SpendLedger)
    assert (ledger.category, ledger.usd_cost) == (suggest.SUGGEST_CATEGORY, COST)
    assert run.usd_cost == COST == batch.usd_cost
    async with sessionmaker() as session:
        assert await today_idle_usd(session, clock, TZ) == COST

    # A weekly batch is not a manual one: the command's quota is untouched.
    async with sessionmaker() as session:
        assert await suggest.manual_used(session, NOW.date()) == 0

    # And it is now the week's batch: tomorrow's planner refuses it.
    async with sessionmaker() as session:
        facts = await load_idle_facts(
            session, settings, FrozenClock(NOW + datetime.timedelta(days=1)), TZ
        )
    assert gate(
        INTEREST_SUGGEST, facts, NOW + datetime.timedelta(days=1), config_from_settings(settings)
    ).reason == SUGGESTED_RECENTLY


async def test_it_is_preempted_before_the_call(sessionmaker, monkeypatch):
    """The runner's own check passes; the user writes before the call;
    `run_batch`'s `may_call` sees it. No call, no spend, no suggestion,
    and the run is a skip."""
    await _seed(sessionmaker)
    clock = FrozenClock(NOW)
    provider = _provider()
    _patch_provider(monkeypatch, provider)
    run_id = await _queued_run(sessionmaker)

    checks: list[bool] = []

    async def _is_preempted(session, clock_, started_at):
        checks.append(True)
        return len(checks) > 1

    monkeypatch.setattr("app.core.idle.runner.is_preempted", _is_preempted)
    await run_idle(
        sessionmaker, _settings(), FakeLLMProvider(), FakeLLMProvider(), clock, run_id=run_id
    )

    assert len(checks) == 2
    assert provider.calls == 0
    async with sessionmaker() as session:
        run = await session.get(IdleRun, run_id)
    assert (run.status, run.skip_reason, run.usd_cost) == ("skipped", "preempted", 0)
    (batch,) = await _rows(sessionmaker, InterestSuggestBatch)
    assert (batch.origin, batch.status, batch.error_code) == ("weekly", "failed", suggest.PREEMPTED)
    assert await _rows(sessionmaker, InterestSuggestion) == []
    assert await _rows(sessionmaker, SpendLedger) == []


async def test_the_body_checks_preemption_against_the_user_s_last_message(sessionmaker, monkeypatch):
    """The real check, called directly: a message after the run started."""
    await _seed(sessionmaker)
    provider = _provider()
    _patch_provider(monkeypatch, provider)
    started_at = NOW - datetime.timedelta(minutes=10)
    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
        state.last_user_msg_at = NOW - datetime.timedelta(minutes=5)
        await session.commit()

    result = await weekly.run_interest_suggest(
        sessionmaker, _settings(), FrozenClock(NOW),
        run_id=1, started_at=started_at, timezone=TZ,
    )

    assert result.preempted
    assert provider.calls == 0


async def test_a_provider_error_fails_the_run_with_the_batch_s_code(sessionmaker, monkeypatch):
    await _seed(sessionmaker)
    _patch_provider(monkeypatch, _provider(raises=[LLMError("down")]))
    run_id = await _queued_run(sessionmaker)

    await run_idle(
        sessionmaker, _settings(), FakeLLMProvider(), FakeLLMProvider(), FrozenClock(NOW),
        run_id=run_id,
    )

    async with sessionmaker() as session:
        run = await session.get(IdleRun, run_id)
    assert (run.status, run.skip_reason, run.usd_cost) == ("failed", suggest.PROVIDER_ERROR, 0)
    (batch,) = await _rows(sessionmaker, InterestSuggestBatch)
    assert (batch.status, batch.error_code) == ("failed", suggest.PROVIDER_ERROR)
    # A failed batch does not start the week: tomorrow may try again.
    async with sessionmaker() as session:
        assert (await weekly.gate_facts(session, _settings(), NOW)).last_batch_at is None


async def test_a_reply_that_does_not_parse_keeps_its_cost_on_the_run(sessionmaker, monkeypatch):
    await _seed(sessionmaker)
    _patch_provider(monkeypatch, _provider("not json"))
    run_id = await _queued_run(sessionmaker)

    await run_idle(
        sessionmaker, _settings(), FakeLLMProvider(), FakeLLMProvider(), FrozenClock(NOW),
        run_id=run_id,
    )

    async with sessionmaker() as session:
        run = await session.get(IdleRun, run_id)
    assert (run.status, run.skip_reason, run.usd_cost) == ("failed", suggest.PARSE_FAIL, COST)


async def test_a_source_gone_between_the_gate_and_the_call_is_a_skip(sessionmaker, monkeypatch):
    """The gate saw a topic; by the call it was retired. The batch fails
    unpaid with `nothing_to_suggest`, and the run is a skip with it."""
    await _seed(sessionmaker)
    provider = _provider()
    _patch_provider(monkeypatch, provider)
    run_id = await _queued_run(sessionmaker)

    real_gate_facts = weekly.gate_facts

    async def _retire_after_gate(session, settings, now):
        facts = await real_gate_facts(session, settings, now)
        for topic in (await session.execute(select(InterestTopic))).scalars():
            topic.active = False
        await session.commit()
        return facts

    monkeypatch.setattr("app.core.idle.facts.suggest_gate_facts", _retire_after_gate)
    await run_idle(
        sessionmaker, _settings(), FakeLLMProvider(), FakeLLMProvider(), FrozenClock(NOW),
        run_id=run_id,
    )

    assert provider.calls == 0
    async with sessionmaker() as session:
        run = await session.get(IdleRun, run_id)
    assert (run.status, run.skip_reason) == ("skipped", suggest.NOTHING)


def test_the_kind_s_skip_codes_are_the_refusals_and_preemption():
    assert weekly.SKIP_CODES == {
        suggest.DISABLED, suggest.NOTHING, suggest.NO_PACKET, suggest.CAP, suggest.PREEMPTED
    }
    assert suggest.PROVIDER_ERROR not in weekly.SKIP_CODES
    assert suggest.PARSE_FAIL not in weekly.SKIP_CODES


def test_the_summary_is_ints_and_no_log_record_attribute():
    import logging

    result = weekly.SuggestResult(batch_id=7, status="done", suggestions=3, proposed=4, dropped=1)
    summary = result.summary()
    assert all(isinstance(value, int) for value in summary.values())
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)
    assert not set(summary) & set(vars(record))
    assert dataclasses.fields(result)  # a dataclass, frozen like its siblings


# --- the send hook after an idle run -------------------------------------------------


async def _plan_and_process(sessionmaker, settings, clock, bot) -> int:
    from app import worker as worker_module

    async with sessionmaker() as session:
        run_id = await plan_idle(session, settings, clock)
    assert run_id is not None
    assert await worker_module.process_one_job(
        sessionmaker, settings, FakeLLMProvider(), clock, bot,
        safety_provider=FakeLLMProvider(),
    )
    (job,) = await _rows(sessionmaker, Job)
    assert job.status == "done"
    return run_id


async def test_the_worker_sends_the_weekly_batch_after_the_idle_run(sessionmaker, monkeypatch):
    await _seed(sessionmaker)
    _patch_provider(monkeypatch, _provider(_reply("гигиена сна у подростков")))
    bot, fake = make_bot()

    await _plan_and_process(sessionmaker, _settings(), FrozenClock(NOW), bot)

    (message,) = fake.sent
    assert message.chat_id == 555
    assert "«гигиена сна у подростков»" in message.text
    assert message.reply_markup is not None
    (batch,) = await _rows(sessionmaker, InterestSuggestBatch)
    assert (batch.origin, batch.message_id, batch.sent_at) == ("weekly", 1, NOW)


async def test_quiet_hours_hold_the_weekly_batch_until_the_heartbeat_after(
    sessionmaker, monkeypatch
):
    from app import worker as worker_module

    await _seed(sessionmaker, last_user_msg_at=None)
    _patch_provider(monkeypatch, _provider(_reply("гигиена сна у подростков")))
    bot, fake = make_bot()

    await _plan_and_process(sessionmaker, _settings(), FrozenClock(NIGHT), bot)
    assert fake.sent == []
    (batch,) = await _rows(sessionmaker, InterestSuggestBatch)
    assert (batch.status, batch.sent_at) == ("done", None)

    morning = NIGHT + datetime.timedelta(hours=8)  # 10:00 in Paris
    await worker_module._send_interest_suggestions(
        sessionmaker, bot, _settings(), FrozenClock(morning)
    )
    (message,) = fake.sent
    assert "«гигиена сна у подростков»" in message.text


async def test_a_weekly_batch_unsent_for_48_hours_expires(sessionmaker, monkeypatch):
    from app import worker as worker_module

    await _seed(sessionmaker, last_user_msg_at=None)
    _patch_provider(monkeypatch, _provider(_reply("гигиена сна у подростков")))
    bot, fake = make_bot()
    await _plan_and_process(sessionmaker, _settings(), FrozenClock(NIGHT), bot)

    later = NIGHT + suggest.UNSENT_TTL + datetime.timedelta(minutes=1)
    await worker_module._send_interest_suggestions(sessionmaker, bot, _settings(), FrozenClock(later))

    assert fake.sent == []
    (suggestion,) = await _rows(sessionmaker, InterestSuggestion)
    assert suggestion.status == "expired"
    # Expired, it no longer counts as undecided.
    async with sessionmaker() as session:
        assert (await weekly.gate_facts(session, _settings(), later)).open_batch_at is None


# --- /digest -------------------------------------------------------------------------


async def test_the_digest_shows_the_weekly_run_as_a_count(sessionmaker):
    async with sessionmaker() as session:
        session.add_all(
            [
                IdleRun(
                    kind=INTEREST_SUGGEST, local_date=NOW.date(), status="done",
                    summary={"suggest_batch_id": 1, "suggestions": 3, "proposed": 4,
                             "dropped": 1, "expired": 0},
                    usd_cost=COST, created_at=NOW,
                ),
                IdleRun(
                    kind=INTEREST_SUGGEST, local_date=NOW.date(), status="failed",
                    skip_reason=suggest.PROVIDER_ERROR, created_at=NOW,
                ),
            ]
        )
        await session.commit()
    async with sessionmaker() as session:
        digest = await build_digest(session, FrozenClock(NOW), undo_days=7)
    lines = digest.text.splitlines()
    assert INTEREST_SUGGEST_LINE.format(n=3) in lines
    assert sum(line.startswith("• Темы для поиска") for line in lines) == 1
    assert digest.undoable_run_ids == ()
