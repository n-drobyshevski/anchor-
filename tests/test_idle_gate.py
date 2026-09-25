"""The idle gate: table-driven over every row of Phase 6 plan section 4
(approved plan §5's test list for milestone 6a)."""

from __future__ import annotations

import datetime
import decimal

import pytest

from app.core.idle.gate import (
    BUSY,
    DAILY_LIMIT,
    DISABLED,
    IDLE_CAP,
    KIND_DAILY_MAX,
    MAX_JOBS,
    MORNING_DISABLED,
    NOT_CANARY_DOW,
    NOT_ENOUGH_CLUSTERS,
    NOT_EVENING,
    NOTHING_TO_BACKFILL,
    NOTE_EXISTS,
    NO_INDEPENDENT_JUDGE,
    NO_NEW_REPLIES,
    NO_NEW_SUMMARY,
    NO_TOPICS,
    OK,
    PAUSED,
    QUOTA_USED,
    RESEARCH_DISABLED,
    RESERVE,
    USER_ACTIVE,
    WELFARE_COOLDOWN,
    WINDOW,
    IdleConfig,
    IdleFacts,
    idle_gate,
    parse_window,
)
from app.core.idle import BACKFILL, CANARY, CONSOLIDATE, CRITIQUE, PREBRIEF, REFLECT, RESEARCH

TZ = datetime.timezone.utc
NOW = datetime.datetime(2026, 9, 23, 12, 0, tzinfo=TZ)
# 2026-09-23 is a Wednesday (isoweekday 3), matching CANARY_DOW's
# default -- used by the canary kind-rule tests below.
EVENING = datetime.datetime(2026, 9, 23, 20, 0, tzinfo=TZ)


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
    )
    base.update(overrides)
    return IdleConfig(**base)


def _facts(**overrides) -> IdleFacts:
    base = dict(
        persona_active=True,
        local_now=NOW,
        welfare_at=None,
        last_user_msg_at=None,
        active_run_ids=frozenset(),
        jobs_today=0,
        idle_spend_today=decimal.Decimal("0"),
        spend_today=decimal.Decimal("0"),
        daily_usd_cap=decimal.Decimal("1.00"),
        backfill_candidates=1,
    )
    base.update(overrides)
    return IdleFacts(**base)


# --- row by row -------------------------------------------------------


def test_row1_disabled():
    result = idle_gate(BACKFILL, _facts(), NOW, _config(enabled=False))
    assert result == (False, DISABLED)


def test_row2_paused():
    result = idle_gate(BACKFILL, _facts(persona_active=False), NOW, _config())
    assert result == (False, PAUSED)


def test_row3_welfare_cooldown():
    facts = _facts(welfare_at=NOW - datetime.timedelta(hours=1))
    assert idle_gate(BACKFILL, facts, NOW, _config()) == (False, WELFARE_COOLDOWN)


def test_row3_welfare_cooldown_expires_at_24h():
    facts = _facts(welfare_at=NOW - datetime.timedelta(hours=24))
    assert idle_gate(BACKFILL, facts, NOW, _config()).allowed is True


def test_row4_user_active():
    facts = _facts(last_user_msg_at=NOW - datetime.timedelta(hours=1))
    assert idle_gate(BACKFILL, facts, NOW, _config(after_h=3)) == (False, USER_ACTIVE)


def test_row4_user_active_clears_after_after_h():
    facts = _facts(last_user_msg_at=NOW - datetime.timedelta(hours=3))
    assert idle_gate(BACKFILL, facts, NOW, _config(after_h=3)).allowed is True


def test_row5_window_wraps_past_midnight():
    # 22:00-06:00 window; local now at 23:00 is inside, at 12:00 is outside.
    config = _config(window_start=datetime.time(22, 0), window_end=datetime.time(6, 0))
    inside = _facts(local_now=NOW.replace(hour=23, minute=0))
    outside = _facts(local_now=NOW.replace(hour=12, minute=0))
    assert idle_gate(BACKFILL, inside, NOW, config).allowed is True
    assert idle_gate(BACKFILL, outside, NOW, config) == (False, WINDOW)


def test_row5_window_wraps_past_midnight_early_morning_is_inside():
    config = _config(window_start=datetime.time(22, 0), window_end=datetime.time(6, 0))
    early = _facts(local_now=NOW.replace(hour=3, minute=0))
    assert idle_gate(BACKFILL, early, NOW, config).allowed is True


def test_parse_window_roundtrip():
    start, end = parse_window("22:00-06:00")
    assert start == datetime.time(22, 0)
    assert end == datetime.time(6, 0)


def test_parse_window_rejects_bad_format():
    with pytest.raises(ValueError):
        parse_window("22:00-0600")


def test_row6_busy():
    facts = _facts(active_run_ids=frozenset({5}))
    assert idle_gate(BACKFILL, facts, NOW, _config()) == (False, BUSY)


def test_row6_busy_excludes_self_run_id():
    facts = _facts(active_run_ids=frozenset({5}))
    result = idle_gate(BACKFILL, facts, NOW, _config(), self_run_id=5)
    assert result.allowed is True


def test_row6_busy_still_fails_for_a_different_run():
    facts = _facts(active_run_ids=frozenset({5, 6}))
    result = idle_gate(BACKFILL, facts, NOW, _config(), self_run_id=5)
    assert result == (False, BUSY)


def test_row7_max_jobs():
    facts = _facts(jobs_today=8)
    assert idle_gate(BACKFILL, facts, NOW, _config(max_jobs_per_day=8)) == (False, MAX_JOBS)


def test_row7_max_jobs_boundary_one_under_passes():
    facts = _facts(jobs_today=7)
    assert idle_gate(BACKFILL, facts, NOW, _config(max_jobs_per_day=8)).allowed is True


def test_row8_idle_cap_boundary_equal_passes():
    facts = _facts(idle_spend_today=decimal.Decimal("0.20"))
    config = _config(usd_cap=decimal.Decimal("0.25"), job_usd_cap=decimal.Decimal("0.05"))
    assert idle_gate(BACKFILL, facts, NOW, config).allowed is True


def test_row8_idle_cap_boundary_one_cent_over_fails():
    facts = _facts(idle_spend_today=decimal.Decimal("0.201"))
    config = _config(usd_cap=decimal.Decimal("0.25"), job_usd_cap=decimal.Decimal("0.05"))
    assert idle_gate(BACKFILL, facts, NOW, config) == (False, IDLE_CAP)


def test_row9_reserve_boundary_equal_passes():
    # spend_today + job_usd_cap == daily_usd_cap - reserve_usd -> allowed.
    facts = _facts(spend_today=decimal.Decimal("0.45"))
    config = _config(job_usd_cap=decimal.Decimal("0.05"), reserve_usd=decimal.Decimal("0.50"))
    facts = _facts(spend_today=decimal.Decimal("0.45"), daily_usd_cap=decimal.Decimal("1.00"))
    assert idle_gate(BACKFILL, facts, NOW, config).allowed is True


def test_row9_reserve_boundary_one_cent_over_fails():
    config = _config(job_usd_cap=decimal.Decimal("0.05"), reserve_usd=decimal.Decimal("0.50"))
    facts = _facts(spend_today=decimal.Decimal("0.451"), daily_usd_cap=decimal.Decimal("1.00"))
    assert idle_gate(BACKFILL, facts, NOW, config) == (False, RESERVE)


def test_row10_kind_rule_backfill_nothing_to_backfill():
    facts = _facts(backfill_candidates=0)
    assert idle_gate(BACKFILL, facts, NOW, _config()) == (False, NOTHING_TO_BACKFILL)


def test_row10_kind_rule_backfill_allows_when_candidates_exist():
    facts = _facts(backfill_candidates=1)
    assert idle_gate(BACKFILL, facts, NOW, _config()) == (True, OK)


# --- 6d kind rule: research ---------------------------------------------


def test_row10_kind_rule_research_disabled():
    facts = _facts(research_has_active_topic=True)
    config = _config(research_enabled=False)
    assert idle_gate(RESEARCH, facts, NOW, config) == (False, RESEARCH_DISABLED)


def test_row10_kind_rule_research_no_topics():
    facts = _facts(research_has_active_topic=False)
    config = _config(research_enabled=True)
    assert idle_gate(RESEARCH, facts, NOW, config) == (False, NO_TOPICS)


def test_row10_kind_rule_research_quota_used():
    facts = _facts(research_has_active_topic=True, research_quota_used=True)
    config = _config(research_enabled=True)
    assert idle_gate(RESEARCH, facts, NOW, config) == (False, QUOTA_USED)


def test_row10_kind_rule_research_allows_when_topic_and_quota_free():
    facts = _facts(research_has_active_topic=True, research_quota_used=False)
    config = _config(research_enabled=True)
    assert idle_gate(RESEARCH, facts, NOW, config) == (True, OK)


def test_row10_kind_rule_research_disabled_by_default():
    """`_config()`'s own default (`research_enabled=False`, mirroring
    `settings.RESEARCH_ENABLED`'s off-by-default) -- research never
    fires from a bare `IdleConfig()` the way the other 6b/6c kinds can."""
    facts = _facts(research_has_active_topic=True)
    assert idle_gate(RESEARCH, facts, NOW, _config()) == (False, RESEARCH_DISABLED)


# --- 6c kind rules: prebrief, critique, canary ---------------------------


def test_row10_kind_rule_prebrief_before_1900_local():
    facts = _facts(local_now=NOW)  # 12:00 local
    assert idle_gate(PREBRIEF, facts, NOW, _config()) == (False, NOT_EVENING)


def test_row10_kind_rule_prebrief_allows_after_1900_local():
    facts = _facts(local_now=EVENING)
    assert idle_gate(PREBRIEF, facts, EVENING, _config()) == (True, OK)


def test_row10_kind_rule_prebrief_morning_disabled():
    facts = _facts(local_now=EVENING)
    config = _config(morning_enabled=False)
    assert idle_gate(PREBRIEF, facts, EVENING, config) == (False, MORNING_DISABLED)


def test_row10_kind_rule_prebrief_note_already_exists():
    facts = _facts(local_now=EVENING, prebrief_note_exists_tomorrow=True)
    assert idle_gate(PREBRIEF, facts, EVENING, _config()) == (False, NOTE_EXISTS)


def test_row10_kind_rule_critique_no_independent_judge():
    facts = _facts(critique_has_new_replies=True)
    config = _config(independent_judge=False)
    assert idle_gate(CRITIQUE, facts, NOW, config) == (False, NO_INDEPENDENT_JUDGE)


def test_row10_kind_rule_critique_no_new_replies():
    facts = _facts(critique_has_new_replies=False)
    assert idle_gate(CRITIQUE, facts, NOW, _config()) == (False, NO_NEW_REPLIES)


def test_row10_kind_rule_critique_allows_when_new_replies_and_independent_judge():
    facts = _facts(critique_has_new_replies=True)
    assert idle_gate(CRITIQUE, facts, NOW, _config()) == (True, OK)


def test_row10_kind_rule_canary_wrong_weekday():
    facts = _facts()
    config = _config(canary_dow=5)  # NOW is a Wednesday (3)
    assert idle_gate(CANARY, facts, NOW, config) == (False, NOT_CANARY_DOW)


def test_row10_kind_rule_canary_no_independent_judge():
    facts = _facts()
    config = _config(canary_dow=3, independent_judge=False)
    assert idle_gate(CANARY, facts, NOW, config) == (False, NO_INDEPENDENT_JUDGE)


def test_row10_kind_rule_canary_allows_on_its_dow_with_independent_judge():
    facts = _facts()
    config = _config(canary_dow=3)
    assert idle_gate(CANARY, facts, NOW, config) == (True, OK)


# --- 6b kind rules: consolidate, reflect --------------------------------


def test_row10_kind_rule_consolidate_fewer_than_two_clusters():
    for clusters in (0, 1):
        facts = _facts(consolidate_clusters=clusters)
        assert idle_gate(CONSOLIDATE, facts, NOW, _config()) == (False, NOT_ENOUGH_CLUSTERS)


def test_row10_kind_rule_consolidate_allows_two_or_more_clusters():
    facts = _facts(consolidate_clusters=2)
    assert idle_gate(CONSOLIDATE, facts, NOW, _config()) == (True, OK)


def test_row10_kind_rule_reflect_no_new_summary():
    facts = _facts(reflect_has_new_summary=False)
    assert idle_gate(REFLECT, facts, NOW, _config()) == (False, NO_NEW_SUMMARY)


def test_row10_kind_rule_reflect_allows_when_new_summary_exists():
    facts = _facts(reflect_has_new_summary=True)
    assert idle_gate(REFLECT, facts, NOW, _config()) == (True, OK)


# --- first-failure-wins ordering ---------------------------------------


def test_first_failure_wins_disabled_beats_paused():
    facts = _facts(persona_active=False)
    result = idle_gate(BACKFILL, facts, NOW, _config(enabled=False))
    assert result.reason == DISABLED


def test_first_failure_wins_paused_beats_welfare():
    facts = _facts(persona_active=False, welfare_at=NOW - datetime.timedelta(minutes=1))
    result = idle_gate(BACKFILL, facts, NOW, _config())
    assert result.reason == PAUSED


def test_first_failure_wins_welfare_beats_user_active():
    facts = _facts(
        welfare_at=NOW - datetime.timedelta(minutes=1),
        last_user_msg_at=NOW - datetime.timedelta(minutes=1),
    )
    result = idle_gate(BACKFILL, facts, NOW, _config())
    assert result.reason == WELFARE_COOLDOWN


def test_first_failure_wins_busy_beats_max_jobs():
    facts = _facts(active_run_ids=frozenset({1}), jobs_today=99)
    result = idle_gate(BACKFILL, facts, NOW, _config(max_jobs_per_day=8))
    assert result.reason == BUSY


def test_first_failure_wins_idle_cap_beats_reserve():
    facts = _facts(
        idle_spend_today=decimal.Decimal("1.00"),
        spend_today=decimal.Decimal("1.00"),
        daily_usd_cap=decimal.Decimal("1.00"),
    )
    result = idle_gate(BACKFILL, facts, NOW, _config())
    assert result.reason == IDLE_CAP


def test_row10_per_kind_daily_limit():
    """Plan section 6's per-kind limits bind before the kind's own rule."""
    assert idle_gate(BACKFILL, _facts(kind_runs_today={BACKFILL: 2}), NOW, _config()) == (True, OK)
    assert idle_gate(BACKFILL, _facts(kind_runs_today={BACKFILL: 3}), NOW, _config()) == (
        False,
        DAILY_LIMIT,
    )
    assert KIND_DAILY_MAX == {
        "backfill": 3, "consolidate": 1, "reflect": 1, "prebrief": 1,
        "critique": 1, "research": 1, "canary": 1,
    }
