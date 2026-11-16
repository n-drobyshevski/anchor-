"""The eval history: how often each blocking case passes, per model pair
(anchor-eval-reliability-plan.md sections 3.3 and 3.4, milestone E1a).

**What it guarantees.** One `eval_case_run` row per blocking case per
amendment trial (`source='trial'`, `source_id` the amendment's id) or
canary (`source='canary'`, `source_id` the idle run's id), holding the
case id, how many times it ran and how the runs ended, and the persona
and judge model ids they ran on. Integers and ids only: no reply, no
prompt, no judge's words reach this table, because none reaches
`eval.trial.TrialResult` in the first place.

**This module is the only one that names the table** (a test scans
app/ and eval/ for it). Callers hand it `TrialResult.runs`; it never
commits, so a row lands in the same transaction as the trial's own
verdict or the canary's ledger row.

**What the history says** (`signal`). Over the **canary** rows of the
*current* persona and judge models only -- a model switch starts the
history again, since a case saturated on one model says nothing about
the next -- a case with at least `EVAL_SIGNAL_MIN_RUNS` runs is `saturated`
(every run passed), `dead` (no run passed) or `live` (anything
between). A case with fewer runs is in none of the three. Nothing is
promoted, demoted or removed from the blocking set by this: which cases
block is still a line in a case file the user edits.

Trial rows are recorded but left out of the signal: a trial stops a case
as soon as it is decided (plan section 3.2), so its rows lean towards
"passed" (two quick passes and done) and would make a case look more
saturated than it is. The canary always runs every case in full; its
rate is the one the plan's section 3.4 means.

Every time comes from the injected `Clock`.
"""

from __future__ import annotations

import dataclasses

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.db.models import EvalCaseRun

TRIAL = "trial"
CANARY = "canary"
SOURCES = (TRIAL, CANARY)

_COUNT_KEYS = ("runs", "passed", "check_failed", "judge_failed", "unusable")


@dataclasses.dataclass(frozen=True)
class Signal:
    """Case ids per class, sorted (plan section 3.4)."""

    saturated: tuple[str, ...] = ()
    live: tuple[str, ...] = ()
    dead: tuple[str, ...] = ()

    def as_summary(self) -> dict[str, list[str]]:
        """The three lists for `idle_run.summary`."""
        return {"saturated": list(self.saturated), "live": list(self.live), "dead": list(self.dead)}


async def record(
    session: AsyncSession,
    *,
    clock: Clock,
    source: str,
    source_id: int,
    runs: dict[str, dict[str, int]],
    persona_model: str,
    judge_model: str,
) -> int:
    """Add one row per case in `runs` (`TrialResult.runs`); returns how
    many. A case with no run (a count of 0) is skipped: there is nothing
    to learn from it. Does not commit."""
    if source not in SOURCES:
        raise ValueError(f"unknown eval history source {source!r}")
    at = clock.now_utc()
    added = 0
    for case_id, counts in sorted(runs.items()):
        values = {key: int(counts.get(key, 0)) for key in _COUNT_KEYS}
        if values["runs"] <= 0:
            continue
        session.add(
            EvalCaseRun(
                at=at,
                source=source,
                source_id=source_id,
                case_id=str(case_id),
                persona_model=persona_model,
                judge_model=judge_model,
                **values,
            )
        )
        added += 1
    return added


def classify(runs: int, passed: int, min_runs: int) -> str | None:
    """`saturated`, `live`, `dead`, or None below `min_runs`."""
    if runs < min_runs:
        return None
    if passed >= runs:
        return "saturated"
    if passed <= 0:
        return "dead"
    return "live"


async def signal(
    session: AsyncSession, *, persona_model: str, judge_model: str, min_runs: int
) -> Signal:
    """Which cases still carry signal under this model pair (module
    docstring), from the canary's rows only: a trial's early stop biases
    its rows towards "passed"."""
    rows = await session.execute(
        select(
            EvalCaseRun.case_id,
            func.sum(EvalCaseRun.runs),
            func.sum(EvalCaseRun.passed),
        )
        .where(EvalCaseRun.source == CANARY)
        .where(EvalCaseRun.persona_model == persona_model)
        .where(EvalCaseRun.judge_model == judge_model)
        .group_by(EvalCaseRun.case_id)
    )
    classes: dict[str, list[str]] = {"saturated": [], "live": [], "dead": []}
    for case_id, runs, passed in rows.all():
        label = classify(int(runs or 0), int(passed or 0), min_runs)
        if label is not None:
            classes[label].append(case_id)
    return Signal(
        saturated=tuple(sorted(classes["saturated"])),
        live=tuple(sorted(classes["live"])),
        dead=tuple(sorted(classes["dead"])),
    )


__all__ = ["CANARY", "SOURCES", "Signal", "TRIAL", "classify", "record", "signal"]
