"""The idle gate (Phase 6 plan section 4) -- a pure function, no I/O.

Same shape as app/core/outbound_gate.py's `gate()`: a snapshot of facts
in, a `(allowed, reason)` verdict out, nothing queried and nothing
touched. It runs **twice** per idle job -- once when the planner picks a
kind (app/core/idle/planner.py), and once again inside the runner right
before the first model call (app/core/idle/runner.py) -- so a user
message, a welfare trigger or a pause between those two moments is
caught before any spend happens.

Checks run in the order the plan's table gives them and **the first
failure wins**, exactly like the outbound gate: `disabled` beats
`paused` beats `welfare_cooldown` and so on down to the kind rule, which
only runs once every shared check has already passed. That ordering is
asserted in tests/test_idle_gate.py, not just documented here.

Row 6 (`busy`) takes `self_run_id`: the in-job re-check must not see its
*own* queued-then-running row as another job already in flight, which
is what would happen if `active_run_ids` included it unconditionally.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import re
from typing import Callable, Mapping, NamedTuple

from app.core.clock import within_window
from app.core.idle import BACKFILL, CANARY, CONSOLIDATE, CRITIQUE, PREBRIEF, REFLECT, RESEARCH

# Reason codes, in table order (plan section 4).
DISABLED = "disabled"
PAUSED = "paused"
WELFARE_COOLDOWN = "welfare_cooldown"
USER_ACTIVE = "user_active"
WINDOW = "window"
BUSY = "busy"
MAX_JOBS = "max_jobs"
IDLE_CAP = "idle_cap"
RESERVE = "reserve"
OK = "ok"

# Kind-specific failures are namespaced `kind_rule:<detail>`, mirroring
# app/core/outbound_gate.py's KIND_RULE_PREFIX convention.
KIND_RULE_PREFIX = "kind_rule:"
NOT_IMPLEMENTED = KIND_RULE_PREFIX + "not_implemented"
NOTHING_TO_BACKFILL = KIND_RULE_PREFIX + "nothing_to_backfill"
DAILY_LIMIT = KIND_RULE_PREFIX + "daily_limit"
# 6b.
NOT_ENOUGH_CLUSTERS = KIND_RULE_PREFIX + "not_enough_clusters"
NO_NEW_SUMMARY = KIND_RULE_PREFIX + "no_new_summary"

# Per-kind daily limits, plan section 6. Checked at row 10 before the
# kind's own rule, against runs that finished today (done, failed or
# undone -- a skip never ran, and an active run is already `busy`).
KIND_DAILY_MAX: dict[str, int] = {
    BACKFILL: 3,
    CONSOLIDATE: 1,
    REFLECT: 1,
    PREBRIEF: 1,
    CRITIQUE: 1,
    RESEARCH: 1,
    CANARY: 1,
}

# The welfare cooldown is a fixed 24h in the plan's table (row 3),
# unlike WELFARE_COOLDOWN_H which gates the *outbound* silence/tick
# kinds and is a setting. Idle's own cooldown is not configurable.
WELFARE_COOLDOWN_HOURS = 24


class GateResult(NamedTuple):
    """(allowed, reason). A NamedTuple so it unpacks as the plan's tuple."""

    allowed: bool
    reason: str


_WINDOW_RE = re.compile(r"^(\d{2}):(\d{2})-(\d{2}):(\d{2})$")


def parse_window(raw: str) -> tuple[datetime.time, datetime.time]:
    """`"HH:MM-HH:MM"` -> `(start, end)`. Raises ValueError on anything else.

    app/config.py's own `IDLE_WINDOW` validator checks the same shape at
    boot (fail loudly early); this is the business-logic side the gate
    actually calls, the same "settings shape checked at boot, parsed
    again here" split TICK_HOURS uses between app/config.py and its
    callers.
    """
    match = _WINDOW_RE.match(raw)
    if not match:
        raise ValueError(f"IDLE_WINDOW must be HH:MM-HH:MM, got {raw!r}")
    start_h, start_m, end_h, end_m = (int(part) for part in match.groups())
    return datetime.time(start_h, start_m), datetime.time(end_h, end_m)


@dataclasses.dataclass(frozen=True)
class IdleConfig:
    """The settings the gate reads, as Decimal/time values ready to compare."""

    enabled: bool
    after_h: int
    usd_cap: decimal.Decimal
    reserve_usd: decimal.Decimal
    job_usd_cap: decimal.Decimal
    max_jobs_per_day: int
    window_start: datetime.time
    window_end: datetime.time
    undo_days: int


def config_from_settings(settings) -> IdleConfig:
    """Build an `IdleConfig` from `app.config.Settings`."""
    start, end = parse_window(settings.IDLE_WINDOW)
    return IdleConfig(
        enabled=settings.IDLE_ENABLED,
        after_h=settings.IDLE_AFTER_H,
        usd_cap=decimal.Decimal(str(settings.IDLE_USD_CAP)),
        reserve_usd=decimal.Decimal(str(settings.IDLE_RESERVE_USD)),
        job_usd_cap=decimal.Decimal(str(settings.IDLE_JOB_USD_CAP)),
        max_jobs_per_day=settings.IDLE_MAX_JOBS_PER_DAY,
        window_start=start,
        window_end=end,
        undo_days=settings.IDLE_UNDO_DAYS,
    )


@dataclasses.dataclass(frozen=True)
class IdleFacts:
    """Everything the gate needs beyond `now` and `config` -- a snapshot,
    never a live query. app/core/idle/facts.py builds one of these from
    the database; tests build one by hand.

    `local_now` is the current instant rendered in the user's own zone
    (app/core/clock.to_local), because row 5's window check is a
    statement about the user's clock face, exactly like
    app/core/clock.within_window's own docstring for quiet hours.
    """

    persona_active: bool
    local_now: datetime.datetime
    welfare_at: datetime.datetime | None = None
    last_user_msg_at: datetime.datetime | None = None
    # ids of idle_run rows with status in ('queued', 'running') --
    # row 6's `busy` check, with `self_run_id` excluded by the caller.
    active_run_ids: frozenset[int] = dataclasses.field(default_factory=frozenset)
    jobs_today: int = 0
    idle_spend_today: decimal.Decimal = decimal.Decimal(0)
    spend_today: decimal.Decimal = decimal.Decimal(0)
    daily_usd_cap: decimal.Decimal = decimal.Decimal(0)
    # How many backfill units (pending summaries + pending reflections)
    # are waiting right now -- the only kind rule that is real in 6a.
    backfill_candidates: int = 0
    # Finished (done/failed/undone) runs per kind today, for the
    # KIND_DAILY_MAX check at row 10.
    kind_runs_today: Mapping[str, int] = dataclasses.field(default_factory=dict)
    # 6b: how many consolidate clusters exist right now (app/core/idle/
    # consolidate.find_clusters, shared with the job itself so the gate
    # and the job can never disagree).
    consolidate_clusters: int = 0
    # 6b: whether a scene summary has appeared since the last *done*
    # reflect run (app/core/idle/reflect.has_new_summary_since). A
    # skipped or failed run never advances this watermark, so a
    # transient failure cannot suppress reflect forever once new
    # material exists.
    reflect_has_new_summary: bool = False


def _backfill_rule(facts: IdleFacts) -> GateResult:
    if facts.backfill_candidates <= 0:
        return GateResult(False, NOTHING_TO_BACKFILL)
    return GateResult(True, OK)


def _consolidate_rule(facts: IdleFacts) -> GateResult:
    if facts.consolidate_clusters < 2:
        return GateResult(False, NOT_ENOUGH_CLUSTERS)
    return GateResult(True, OK)


def _reflect_rule(facts: IdleFacts) -> GateResult:
    if not facts.reflect_has_new_summary:
        return GateResult(False, NO_NEW_SUMMARY)
    return GateResult(True, OK)


def _not_implemented_rule(facts: IdleFacts) -> GateResult:
    return GateResult(False, NOT_IMPLEMENTED)


# One pure predicate per kind (plan §5: "KIND_RULES is a dict of pure
# per-kind predicates"). Kinds past 6b return kind_rule:not_implemented
# until their own milestone lands.
KIND_RULES: dict[str, Callable[[IdleFacts], GateResult]] = {
    BACKFILL: _backfill_rule,
    CONSOLIDATE: _consolidate_rule,
    REFLECT: _reflect_rule,
    PREBRIEF: _not_implemented_rule,
    CRITIQUE: _not_implemented_rule,
    RESEARCH: _not_implemented_rule,
    CANARY: _not_implemented_rule,
}


def idle_gate(
    kind: str,
    facts: IdleFacts,
    now: datetime.datetime,
    config: IdleConfig,
    *,
    self_run_id: int | None = None,
) -> GateResult:
    """Rows 1-10 of plan section 4, in order, first failure wins."""
    if not config.enabled:
        return GateResult(False, DISABLED)

    if not facts.persona_active:
        return GateResult(False, PAUSED)

    if facts.welfare_at is not None and now - facts.welfare_at < datetime.timedelta(
        hours=WELFARE_COOLDOWN_HOURS
    ):
        return GateResult(False, WELFARE_COOLDOWN)

    if facts.last_user_msg_at is not None and now - facts.last_user_msg_at < datetime.timedelta(
        hours=config.after_h
    ):
        return GateResult(False, USER_ACTIVE)

    if not within_window(facts.local_now.time(), config.window_start, config.window_end):
        return GateResult(False, WINDOW)

    active = set(facts.active_run_ids)
    active.discard(self_run_id)
    if active:
        return GateResult(False, BUSY)

    if facts.jobs_today >= config.max_jobs_per_day:
        return GateResult(False, MAX_JOBS)

    if facts.idle_spend_today + config.job_usd_cap > config.usd_cap:
        return GateResult(False, IDLE_CAP)

    if facts.spend_today + config.job_usd_cap > facts.daily_usd_cap - config.reserve_usd:
        return GateResult(False, RESERVE)

    if facts.kind_runs_today.get(kind, 0) >= KIND_DAILY_MAX[kind]:
        return GateResult(False, DAILY_LIMIT)

    return KIND_RULES[kind](facts)


__all__ = [
    "BUSY",
    "DAILY_LIMIT",
    "DISABLED",
    "IDLE_CAP",
    "KIND_RULE_PREFIX",
    "KIND_DAILY_MAX",
    "KIND_RULES",
    "MAX_JOBS",
    "NOT_ENOUGH_CLUSTERS",
    "NOT_IMPLEMENTED",
    "NOTHING_TO_BACKFILL",
    "NO_NEW_SUMMARY",
    "OK",
    "PAUSED",
    "RESERVE",
    "USER_ACTIVE",
    "WELFARE_COOLDOWN",
    "WELFARE_COOLDOWN_HOURS",
    "WINDOW",
    "GateResult",
    "IdleConfig",
    "IdleFacts",
    "config_from_settings",
    "idle_gate",
    "parse_window",
]
