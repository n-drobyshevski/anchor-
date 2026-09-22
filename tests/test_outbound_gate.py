"""app/core/outbound_gate.py tests (phase-3 plan sections 5 and 13).

The plan asks for a table-driven test with one row per check plus the
kind rules, and for order precedence. That is what this is.

Two things beyond the table are asserted, because they are the
properties that make the gate trustworthy rather than merely correct:

- **Precedence.** The first failing check wins, and its reason is what
  gets recorded and shown in /state. A gate that returned `daily_budget`
  for a paused bot would send a future maintainer looking in the wrong
  place entirely.
- **Purity.** The module imports nothing that can do I/O. Test 5 of the
  plan calls the gate "a pure function with no I/O"; asserting it
  structurally means the property cannot be lost by someone adding "just
  one query" inside it later.
"""

from __future__ import annotations

import datetime
import decimal

import pytest

from app.core.clock import combine_local
from app.core.outbound_gate import (
    CAP,
    DAILY_BUDGET,
    DISABLED,
    EVENING_NAG,
    IGNORED,
    KIND_RULE_PREFIX,
    KINDS,
    MIN_GAP,
    MORNING,
    OK,
    PAUSED,
    QUIET_CMD,
    QUIET_HOURS,
    SILENCE,
    TICK,
    WELFARE_COOLDOWN,
    GateConfig,
    GateCounts,
    GateFacts,
    GateState,
    gate,
)

PARIS = "Europe/Paris"

# A Tuesday, 12:00 local -- outside quiet hours, not a DST day, so
# nothing in the baseline is accidentally load-bearing.
NOW = combine_local(datetime.date(2026, 9, 22), datetime.time(12, 0), PARIS)


def hours_ago(n: float) -> datetime.datetime:
    return NOW - datetime.timedelta(hours=n)


def a_state(**overrides) -> GateState:
    """A state in which morning, evening_nag and tick are all allowed."""
    base = dict(
        timezone=PARIS,
        persona_active=True,
        focus_on=False,
        ignored_in_row=0,
        quiet_until=None,
        last_user_msg_at=hours_ago(5),
        last_outbound_at=None,
        welfare_at=None,
    )
    base.update(overrides)
    return GateState(**base)


def a_silent_state(**overrides) -> GateState:
    """Focus on, and silent for longer than SILENCE_NUDGE_H."""
    base = dict(focus_on=True, last_user_msg_at=hours_ago(49))
    base.update(overrides)
    return a_state(**base)


def counts(**overrides) -> GateCounts:
    base = dict(
        sent_today=0,
        tick_sent_today=0,
        spend_today_usd=decimal.Decimal("0.02"),
        last_silence_sent_at=None,
    )
    base.update(overrides)
    return GateCounts(**base)


CONFIG = GateConfig()
NO_CHECKIN = GateFacts(checkin_today=False)


def run(kind, state=None, now=NOW, cnt=None, facts=NO_CHECKIN, config=CONFIG):
    return gate(kind, state or a_state(), now, cnt or counts(), facts, config)


# --- the baseline ------------------------------------------------------


@pytest.mark.parametrize("kind", [MORNING, EVENING_NAG, TICK])
def test_the_baseline_state_allows_every_kind_that_has_no_extra_rule(kind):
    assert run(kind) == (True, OK)


def test_the_baseline_silence_state_allows_a_nudge():
    assert run(SILENCE, a_silent_state()) == (True, OK)


def test_an_unknown_kind_raises_rather_than_being_silently_allowed():
    with pytest.raises(ValueError):
        run("surprise")


def test_the_result_unpacks_as_the_plans_tuple():
    allowed, reason = run(MORNING)
    assert allowed is True and reason == OK


# --- section 5, one test per row ---------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_row_1_the_kill_switch_stops_every_kind(kind):
    config = GateConfig(outbound_enabled=False)
    assert run(kind, a_silent_state(), config=config) == (False, DISABLED)


@pytest.mark.parametrize("kind", KINDS)
def test_row_2_a_paused_persona_stops_every_kind_including_fixed_intents(kind):
    """Plan section 11: nothing unsolicited while persona_active is
    false -- that covers /out, a pause word, and a welfare trigger."""
    assert run(kind, a_silent_state(persona_active=False)) == (False, PAUSED)


def test_row_3_quiet_until_in_the_future_blocks():
    state = a_state(quiet_until=NOW + datetime.timedelta(hours=1))
    assert run(MORNING, state) == (False, QUIET_CMD)


def test_row_3_quiet_until_in_the_past_does_not_block():
    state = a_state(quiet_until=NOW - datetime.timedelta(seconds=1))
    assert run(MORNING, state) == (True, OK)


@pytest.mark.parametrize(
    "local_hour, local_minute, blocked",
    [(22, 29, False), (22, 30, True), (23, 59, True), (3, 0, True), (7, 59, True), (8, 0, False)],
)
def test_row_4_quiet_hours_wrap_past_midnight(local_hour, local_minute, blocked):
    now = combine_local(
        datetime.date(2026, 9, 22), datetime.time(local_hour, local_minute), PARIS
    )
    # last_outbound_at/last_user_msg_at are relative to NOW; keep the
    # state neutral so only the hour is under test.
    state = a_state(last_user_msg_at=now - datetime.timedelta(hours=5))
    result = run(MORNING, state, now=now)
    assert result.allowed is not blocked
    if blocked:
        assert result.reason == QUIET_HOURS


def test_row_5_the_daily_spend_cap_blocks():
    assert run(MORNING, cnt=counts(spend_today_usd=decimal.Decimal("3.00"))) == (
        False,
        CAP,
    )


def test_row_5_just_under_the_cap_does_not_block():
    assert run(MORNING, cnt=counts(spend_today_usd=decimal.Decimal("2.999999"))) == (
        True,
        OK,
    )


@pytest.mark.parametrize("kind", KINDS)
def test_row_6_three_ignored_in_a_row_silences_every_kind(kind):
    """Plan section 11: after MAX_IGNORED_IN_ROW unanswered messages
    Anchor is silent until the user writes -- fixed intents included."""
    state = a_silent_state(ignored_in_row=3, last_outbound_at=hours_ago(30))
    assert run(kind, state) == (False, IGNORED)


def test_row_6_two_ignored_is_not_yet_silence():
    state = a_state(ignored_in_row=2, last_outbound_at=hours_ago(9))
    assert run(MORNING, state) == (True, OK)


@pytest.mark.parametrize("kind", [SILENCE, TICK])
def test_row_7_welfare_holds_back_the_discretionary_kinds(kind):
    state = a_silent_state(welfare_at=hours_ago(5))
    assert run(kind, state) == (False, WELFARE_COOLDOWN)


@pytest.mark.parametrize("kind", [MORNING, EVENING_NAG])
def test_row_7_welfare_does_not_hold_back_the_agreed_routine(kind):
    """Morning and evening resume with the persona; only the
    discretionary kinds wait out the cooldown."""
    state = a_state(welfare_at=hours_ago(5))
    assert run(kind, state) == (True, OK)


@pytest.mark.parametrize("kind", [SILENCE, TICK])
def test_row_7_the_cooldown_expires_after_twenty_four_hours(kind):
    state = a_silent_state(welfare_at=hours_ago(24.5))
    assert run(kind, state).allowed is True


def test_row_8_the_daily_budget_blocks_at_three():
    assert run(MORNING, cnt=counts(sent_today=3)) == (False, DAILY_BUDGET)


def test_row_8_two_sent_today_still_leaves_room():
    assert run(MORNING, cnt=counts(sent_today=2)) == (True, OK)


def test_row_9_an_unanswered_message_enforces_a_minimum_gap():
    state = a_state(ignored_in_row=1, last_outbound_at=hours_ago(2))
    assert run(MORNING, state) == (False, MIN_GAP)


def test_row_9_the_gap_lifts_after_min_gap_unanswered_h():
    state = a_state(ignored_in_row=1, last_outbound_at=hours_ago(8.5))
    assert run(MORNING, state) == (True, OK)


def test_row_9_an_answered_message_imposes_no_gap():
    """ignored_in_row is 0 because the user replied, so a recent
    outbound is no reason to wait."""
    state = a_state(ignored_in_row=0, last_outbound_at=hours_ago(1))
    assert run(MORNING, state) == (True, OK)


# --- row 10: the kind-specific rules -----------------------------------


def test_morning_has_no_extra_rule():
    assert run(MORNING) == (True, OK)


def test_evening_nag_is_skipped_when_a_checkin_already_happened():
    result = run(EVENING_NAG, facts=GateFacts(checkin_today=True))
    assert result.allowed is False
    assert result.reason == KIND_RULE_PREFIX + "checkin_done"


def test_evening_nag_fires_when_no_checkin_happened():
    assert run(EVENING_NAG, facts=GateFacts(checkin_today=False)) == (True, OK)


def test_silence_needs_focus_on():
    result = run(SILENCE, a_silent_state(focus_on=False))
    assert result.reason == KIND_RULE_PREFIX + "no_focus"


def test_silence_needs_a_last_message_to_measure_from():
    """A fresh install or a post-/delete reset has no baseline. "Never
    wrote" must not read as "silent for infinity hours"."""
    result = run(SILENCE, a_silent_state(last_user_msg_at=None))
    assert result.reason == KIND_RULE_PREFIX + "never_wrote"


def test_silence_waits_the_full_forty_eight_hours():
    result = run(SILENCE, a_silent_state(last_user_msg_at=hours_ago(47.5)))
    assert result.reason == KIND_RULE_PREFIX + "recent_activity"


def test_silence_fires_just_past_forty_eight_hours():
    assert run(SILENCE, a_silent_state(last_user_msg_at=hours_ago(48.5))) == (True, OK)


def test_silence_does_not_repeat_within_the_nudge_window():
    """Dedup is by (kind, local_date, bucket), which alone would permit
    a second nudge at the next midnight; the 48h rule is what actually
    keeps them apart (plan section 4)."""
    result = run(SILENCE, a_silent_state(), cnt=counts(last_silence_sent_at=hours_ago(10)))
    assert result.reason == KIND_RULE_PREFIX + "recent_nudge"


def test_silence_may_repeat_once_the_window_has_passed():
    assert run(
        SILENCE, a_silent_state(), cnt=counts(last_silence_sent_at=hours_ago(49))
    ) == (True, OK)


def test_tick_is_skipped_while_the_user_is_active():
    result = run(TICK, a_state(last_user_msg_at=hours_ago(1)))
    assert result.reason == KIND_RULE_PREFIX + "user_active"


def test_tick_is_capped_at_one_a_day():
    result = run(TICK, cnt=counts(tick_sent_today=1))
    assert result.reason == KIND_RULE_PREFIX + "tick_cap"


def test_tick_stands_off_for_two_hours_after_any_other_outbound():
    result = run(TICK, a_state(last_outbound_at=hours_ago(1)))
    assert result.reason == KIND_RULE_PREFIX + "recent_outbound"


def test_tick_is_allowed_once_the_two_hours_have_passed():
    assert run(TICK, a_state(last_outbound_at=hours_ago(2.5))) == (True, OK)


def test_tick_with_no_prior_message_at_all_is_allowed():
    """Unlike the silence nudge, a tick has no elapsed-time floor to
    measure, so a null last_user_msg_at is not a blocker."""
    assert run(TICK, a_state(last_user_msg_at=None)) == (True, OK)


# --- precedence --------------------------------------------------------


def test_the_kill_switch_outranks_everything():
    state = a_state(
        persona_active=False,
        quiet_until=NOW + datetime.timedelta(hours=1),
        ignored_in_row=9,
    )
    result = gate(
        MORNING,
        state,
        NOW,
        counts(sent_today=9, spend_today_usd=decimal.Decimal("99")),
        GateFacts(checkin_today=True),
        GateConfig(outbound_enabled=False),
    )
    assert result == (False, DISABLED)


def test_paused_outranks_every_check_below_it():
    """Plan section 5: the first failure wins. The reason is what
    /state shows, so it has to name the most fundamental cause."""
    state = a_state(
        persona_active=False,
        quiet_until=NOW + datetime.timedelta(hours=1),
        ignored_in_row=9,
        last_outbound_at=hours_ago(1),
        welfare_at=hours_ago(1),
    )
    result = gate(
        EVENING_NAG,
        state,
        NOW,
        counts(sent_today=9, spend_today_usd=decimal.Decimal("99")),
        GateFacts(checkin_today=True),
        CONFIG,
    )
    assert result == (False, PAUSED)


def test_quiet_cmd_outranks_quiet_hours():
    now = combine_local(datetime.date(2026, 9, 22), datetime.time(23, 0), PARIS)
    state = a_state(
        quiet_until=now + datetime.timedelta(hours=1),
        last_user_msg_at=now - datetime.timedelta(hours=5),
    )
    assert run(MORNING, state, now=now) == (False, QUIET_CMD)


def test_ignored_outranks_the_daily_budget():
    """Being ignored is a stronger signal than a counter that resets at
    midnight, so it is checked first and reported first."""
    state = a_state(ignored_in_row=3, last_outbound_at=hours_ago(30))
    assert run(MORNING, state, cnt=counts(sent_today=3)) == (False, IGNORED)


def test_the_daily_budget_outranks_the_kind_rule():
    result = run(
        EVENING_NAG, cnt=counts(sent_today=3), facts=GateFacts(checkin_today=True)
    )
    assert result == (False, DAILY_BUDGET)


# --- purity ------------------------------------------------------------


def test_the_gate_module_cannot_reach_the_database():
    """Section 5 calls this "a pure function with no I/O". Asserted
    structurally: the module imports nothing that could query, so the
    property survives someone later adding "just one lookup"."""
    import ast
    import pathlib

    source = pathlib.Path("app/core/outbound_gate.py").read_text()
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    forbidden = {"sqlalchemy", "aiogram", "asyncio", "openai", "aiohttp"}
    assert not (imported & forbidden)

    # And nothing from this repo that owns a session or a socket. The
    # clock is the one app import it is allowed, and only for the two
    # pure helpers (to_local, within_window).
    app_imports = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("app.")
    }
    assert app_imports == {"app.core.clock"}


def test_the_gate_is_not_a_coroutine():
    """A pure function has nothing to await. If this ever becomes async
    it is because someone put I/O in it."""
    import inspect

    assert not inspect.iscoroutinefunction(gate)
