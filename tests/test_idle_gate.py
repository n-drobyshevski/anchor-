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
    NOT_IMPLEMENTED,
    NOTHING_TO_BACKFILL,
    OK,
    PAUSED,
    RESERVE,
    USER_ACTIVE,
    WELFARE_COOLDOWN,
    WINDOW,
    IdleConfig,
    IdleFacts,
    idle_gate,
    parse_window,
)
from app.core.idle import BACKFILL, CONSOLIDATE

TZ = datetime.timezone.utc
NOW = datetime.datetime(2026, 9, 23, 12, 0, tzinfo=TZ)


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


def test_row10_kind_rule_unimplemented_kinds():
    facts = _facts()
    assert idle_gate(CONSOLIDATE, facts, NOW, _config()) == (False, NOT_IMPLEMENTED)


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
