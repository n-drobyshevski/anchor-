"""Idle: background work the bot does on the user's spare quiet hours
(Phase 6 plan; approved plan §5's file-by-file for milestone 6a).

Kind constants and the job kind live here so every other module in this
package (and app/worker.py, which dispatches the `idle_run` job kind)
imports them from one place, the same convention app/core/scene.py's
`SUMMARIZE_SCENE` and app/core/notebook.py's `NOTEBOOK_REFLECT` set.

Only `BACKFILL` is implemented in 6a. The other six exist as constants
because `IdleRun.kind`'s CHECK constraint and the planner's PRIORITY
tuple both need the full Phase 6 vocabulary now -- app/core/idle/
gate.py's KIND_RULES returns `kind_rule:not_implemented` for all of
them until their own milestone (6b-6e) gives them a real rule and a
handler in app/core/idle/runner.py.
"""

from __future__ import annotations

BACKFILL = "backfill"
CONSOLIDATE = "consolidate"
REFLECT = "reflect"
PREBRIEF = "prebrief"
CRITIQUE = "critique"
RESEARCH = "research"
CANARY = "canary"

KINDS = (BACKFILL, CONSOLIDATE, REFLECT, PREBRIEF, CRITIQUE, RESEARCH, CANARY)

# The job kind app/db/jobs.py's `job.kind` carries and app/worker.py's
# `_run_job` dispatches on -- one job kind for every idle kind, since
# the idle_run row (not the job payload) is what says which work it is.
IDLE_RUN = "idle_run"

__all__ = [
    "BACKFILL",
    "CONSOLIDATE",
    "REFLECT",
    "PREBRIEF",
    "CRITIQUE",
    "RESEARCH",
    "CANARY",
    "KINDS",
    "IDLE_RUN",
]
