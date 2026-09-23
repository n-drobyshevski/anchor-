"""The outbound gate (phase-3 plan section 5) -- the heart of Phase 3.

Every unsolicited message passes through `gate()`. It is a **pure
function with no I/O**: it takes a snapshot of state, counts and facts
and returns a verdict. It cannot query, it cannot send, and -- the
point of the whole design -- no model output is anywhere in its inputs.
The tick model (phase-3 plan section 8) proposes; this decides.

It runs **twice** per message: once when the heartbeat plans the row,
and once again immediately before generation. The second run is the
authoritative one. That closes the window where the user checks in,
types a pause word, or sets /quiet between planning and sending -- a
window that is minutes wide by design, because of the jitter.

Checks run in a fixed order and **the first failure wins**, so the
recorded reason is the most fundamental one. `paused` beats
`daily_budget` beats `kind_rule`. That ordering is asserted in
tests/test_outbound_gate.py, not just documented, because the reason
code is what /state shows the user and what a future me will read when
asking "why didn't it write?".

Purity is not incidental. The caller assembling GateState/GateCounts/
GateFacts does all the querying, which means this module has no session
to reach for even by accident, and the whole truth table is testable
without a database.
"""

from __future__ import annotations

import datetime
import decimal
from dataclasses import dataclass
from typing import Literal, NamedTuple

from app.core.clock import to_local, within_window

Kind = Literal["morning", "evening_nag", "silence", "tick", "weekly_review"]

MORNING: Kind = "morning"
EVENING_NAG: Kind = "evening_nag"
SILENCE: Kind = "silence"
TICK: Kind = "tick"
# 5d (phase-5 plan section 8; implementation plan's "Decisions"). The
# weekly review goes through the full gate like the two fixed intents,
# but joins the welfare-cooldown set with SILENCE and TICK: it is
# discretionary in the same sense they are (nobody agreed to a fixed
# time the way morning/evening are the routine itself), so a recent
# welfare trigger holds it back too.
WEEKLY_REVIEW: Kind = "weekly_review"

KINDS: tuple[Kind, ...] = (MORNING, EVENING_NAG, SILENCE, TICK, WEEKLY_REVIEW)

# Reason codes. `OK` is the only one that accompanies allowed=True.
OK = "ok"
DISABLED = "disabled"
PAUSED = "paused"
QUIET_CMD = "quiet_cmd"
QUIET_HOURS = "quiet_hours"
CAP = "cap"
IGNORED = "ignored"
WELFARE_COOLDOWN = "welfare_cooldown"
DAILY_BUDGET = "daily_budget"
MIN_GAP = "min_gap"

# Kind-specific failures are namespaced `kind_rule:<detail>` per the
# plan, so /state can show the family without enumerating the details.
KIND_RULE_PREFIX = "kind_rule:"

_CHECKIN_DONE = KIND_RULE_PREFIX + "checkin_done"
_NO_FOCUS = KIND_RULE_PREFIX + "no_focus"
_NEVER_WROTE = KIND_RULE_PREFIX + "never_wrote"
_RECENT_ACTIVITY = KIND_RULE_PREFIX + "recent_activity"
_RECENT_NUDGE = KIND_RULE_PREFIX + "recent_nudge"
_USER_ACTIVE = KIND_RULE_PREFIX + "user_active"
_TICK_CAP = KIND_RULE_PREFIX + "tick_cap"
_RECENT_OUTBOUND = KIND_RULE_PREFIX + "recent_outbound"
_REVIEW_EXISTS = KIND_RULE_PREFIX + "review_exists"


class GateResult(NamedTuple):
    """(allowed, reason). A NamedTuple so it unpacks as the plan's tuple."""

    allowed: bool
    reason: str


@dataclass(frozen=True)
class GateState:
    """The user_state fields the gate reads. A snapshot, not the row."""

    timezone: str
    persona_active: bool
    focus_on: bool
    ignored_in_row: int
    quiet_until: datetime.datetime | None = None
    last_user_msg_at: datetime.datetime | None = None
    last_outbound_at: datetime.datetime | None = None
    welfare_at: datetime.datetime | None = None


@dataclass(frozen=True)
class GateCounts:
    """What has already been spent and sent today (local date)."""

    sent_today: int = 0
    tick_sent_today: int = 0
    spend_today_usd: decimal.Decimal = decimal.Decimal(0)
    last_silence_sent_at: datetime.datetime | None = None


@dataclass(frozen=True)
class GateFacts:
    """Facts about today that only a query can answer."""

    checkin_today: bool = False
    # 5d: whether a weekly_review row already exists for the local
    # week_start the current instant falls in. The current code treats
    # any unknown kind as TICK (see _kind_rule's fallthrough below), so
    # WEEKLY_REVIEW needs its own explicit branch -- this is the fact
    # that branch reads. Only load_gate_inputs(kind=WEEKLY_REVIEW) (or
    # kind=None) ever populates it as True; every other caller leaves
    # the default, which is the conservative direction (a stale False
    # never blocks a legitimate review).
    review_exists_this_week: bool = False


@dataclass(frozen=True)
class GateConfig:
    """The section 2 knobs, lifted off Settings so the gate stays pure.

    Built by `config_from_settings()`; a dataclass rather than Settings
    itself so a test can express one row of the truth table without
    constructing the whole application config.
    """

    outbound_enabled: bool = True
    quiet_start: datetime.time = datetime.time(22, 30)
    quiet_end: datetime.time = datetime.time(8, 0)
    daily_usd_cap: decimal.Decimal = decimal.Decimal("1.00")
    max_unsolicited_per_day: int = 3
    min_gap_unanswered_h: int = 8
    max_ignored_in_row: int = 3
    silence_nudge_h: int = 48
    tick_max_per_day: int = 1
    tick_skip_if_active_h: int = 2
    welfare_cooldown_h: int = 24
    # Not a plan knob: section 5's tick rule says "skip if any other
    # outbound was sent within the last 2h". Named here rather than
    # inlined as a literal so the truth-table test can move it.
    tick_quiet_after_outbound_h: int = 2


def config_from_settings(settings) -> GateConfig:
    """Lift the Phase 3 settings into the gate's own input type."""
    return GateConfig(
        outbound_enabled=settings.OUTBOUND_ENABLED,
        quiet_start=settings.QUIET_START,
        quiet_end=settings.QUIET_END,
        daily_usd_cap=decimal.Decimal(str(settings.DAILY_USD_CAP)),
        max_unsolicited_per_day=settings.MAX_UNSOLICITED_PER_DAY,
        min_gap_unanswered_h=settings.MIN_GAP_UNANSWERED_H,
        max_ignored_in_row=settings.MAX_IGNORED_IN_ROW,
        silence_nudge_h=settings.SILENCE_NUDGE_H,
        tick_max_per_day=settings.TICK_MAX_PER_DAY,
        tick_skip_if_active_h=settings.TICK_SKIP_IF_ACTIVE_H,
        welfare_cooldown_h=settings.WELFARE_COOLDOWN_H,
    )


def _elapsed_under(
    now: datetime.datetime, moment: datetime.datetime | None, hours: float
) -> bool:
    """True iff `moment` exists and is less than `hours` before `now`."""
    if moment is None:
        return False
    return now - moment < datetime.timedelta(hours=hours)


def gate(
    kind: Kind,
    state: GateState,
    now: datetime.datetime,
    counts: GateCounts,
    facts: GateFacts,
    config: GateConfig,
) -> GateResult:
    """May an unsolicited message of `kind` be sent right now?

    `now` is an aware UTC instant (from the clock, never read here).
    Returns the first failing check's reason, or (True, OK).
    """
    if kind not in KINDS:
        raise ValueError(f"unknown outbound kind: {kind!r}")

    # 1. The global kill switch. Nothing below it matters.
    if not config.outbound_enabled:
        return GateResult(False, DISABLED)

    # 2. Paused: /out, a pause word, or a welfare trigger. Plan section
    #    11 -- nothing unsolicited while persona_active is false, and
    #    that includes the fixed intents.
    if not state.persona_active:
        return GateResult(False, PAUSED)

    # 3. /quiet, an explicit "not now" with an end time.
    if state.quiet_until is not None and state.quiet_until > now:
        return GateResult(False, QUIET_CMD)

    # 4. Quiet hours, compared as wall clocks (see clock.within_window).
    local_now = to_local(now, state.timezone)
    if within_window(local_now.time(), config.quiet_start, config.quiet_end):
        return GateResult(False, QUIET_HOURS)

    # 5. The wallet. Unsolicited messages are the first thing to go when
    #    the day's budget is gone -- the user asked for none of them.
    if counts.spend_today_usd >= config.daily_usd_cap:
        return GateResult(False, CAP)

    # 6. Back-off: N unanswered messages in a row and Anchor goes quiet
    #    until the user writes. Above the budget check because being
    #    ignored is a stronger signal than a counter resetting at
    #    midnight.
    if state.ignored_in_row >= config.max_ignored_in_row:
        return GateResult(False, IGNORED)

    # 7. Welfare cooldown. Only the *discretionary* kinds are held back;
    #    morning and evening are part of the agreed routine and resume
    #    with the persona. 5d: weekly_review joins silence and tick here
    #    (implementation plan's "Decisions": announced ahead of this
    #    milestone).
    if kind in (SILENCE, TICK, WEEKLY_REVIEW) and _elapsed_under(
        now, state.welfare_at, config.welfare_cooldown_h
    ):
        return GateResult(False, WELFARE_COOLDOWN)

    # 8. The daily count of unsolicited messages, all kinds together.
    if counts.sent_today >= config.max_unsolicited_per_day:
        return GateResult(False, DAILY_BUDGET)

    # 9. Minimum gap while the last message is still unanswered.
    if state.ignored_in_row >= 1 and _elapsed_under(
        now, state.last_outbound_at, config.min_gap_unanswered_h
    ):
        return GateResult(False, MIN_GAP)

    # 10. Kind-specific.
    return _kind_rule(kind, state, now, counts, facts, config)


def _kind_rule(
    kind: Kind,
    state: GateState,
    now: datetime.datetime,
    counts: GateCounts,
    facts: GateFacts,
    config: GateConfig,
) -> GateResult:
    if kind == MORNING:
        # No extra rule: the morning action is the routine itself.
        return GateResult(True, OK)

    if kind == EVENING_NAG:
        # Nagging someone who already checked in is the exact failure
        # mode this whole phase is trying to avoid.
        if facts.checkin_today:
            return GateResult(False, _CHECKIN_DONE)
        return GateResult(True, OK)

    if kind == SILENCE:
        if not state.focus_on:
            return GateResult(False, _NO_FOCUS)
        # No baseline means no nudge. A fresh install (or a /delete
        # reset) has last_user_msg_at NULL, and "never wrote" must not
        # read as "silent for infinity hours".
        if state.last_user_msg_at is None:
            return GateResult(False, _NEVER_WROTE)
        if _elapsed_under(now, state.last_user_msg_at, config.silence_nudge_h):
            return GateResult(False, _RECENT_ACTIVITY)
        # Section 4: the 48h rule itself is what keeps nudges apart,
        # since local_date dedup alone would allow one every midnight.
        if _elapsed_under(now, counts.last_silence_sent_at, config.silence_nudge_h):
            return GateResult(False, _RECENT_NUDGE)
        return GateResult(True, OK)

    if kind == WEEKLY_REVIEW:
        # 5d: refuse when this local week already has a row -- the
        # scheduled review must never re-plan the same week, and
        # /review bypasses the gate entirely (it regenerates on
        # purpose). The current code otherwise treats any kind not
        # matched above as TICK, which is exactly wrong for this one,
        # hence the explicit branch (implementation plan's "Decisions").
        if facts.review_exists_this_week:
            return GateResult(False, _REVIEW_EXISTS)
        return GateResult(True, OK)

    # TICK
    if _elapsed_under(now, state.last_user_msg_at, config.tick_skip_if_active_h):
        return GateResult(False, _USER_ACTIVE)
    if counts.tick_sent_today >= config.tick_max_per_day:
        return GateResult(False, _TICK_CAP)
    if _elapsed_under(now, state.last_outbound_at, config.tick_quiet_after_outbound_h):
        return GateResult(False, _RECENT_OUTBOUND)
    return GateResult(True, OK)
