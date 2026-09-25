"""Recording and reading safety-model outcomes (H2).

Five calls decide things the persona must not: the welfare classifier,
the post-turn extractor, the tick decision, and -- since phase 4 -- the
research distiller and the research search. Each can fail in a way
that leaves no trace -- a timeout writes no spend_ledger row, an
unparseable reply is a silent no-op -- so a check that has stopped
working looks exactly like one with nothing to report.

This module is the trace. One row per call outcome, no content ever:
which check ran, how it ended, which model served it, when. That is
enough to answer the only question worth asking of it -- "is this
check actually running?" -- and not enough to reconstruct anything the
user said, or anything a fetched page said (plan sections 10 and 13,
and phase-4 plan section 12).

**Writes are best-effort, by design.** `record()` swallows its own
failures. An observability row that could raise would be able to fail
the user's reply, which is a worse bug than the blindness it fixes: the
welfare check exists to make a bad moment better, and a 500 from a
logging table must never be how it makes one worse.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import clock as clock_module
from app.core.clock import Clock
from app.core.welfare import ERROR, FALLBACK_HIT, PARSE_FAIL, TIMEOUT
from app.db.models import SafetyEvent

logger = logging.getLogger(__name__)

# Which check produced the row. Mirrors ck_safety_event_kind.
WELFARE = "welfare"
EXTRACTOR = "extractor"
TICK = "tick"
# Phase 4's two. `distill` is the call that turns a fetched page into
# cards -- strict JSON, so `parse_fail` means exactly what it means for
# the extractor. `search` is the `web` plugin call; it parses nothing,
# and its failure is coming back with no usable URL at all, which is
# `error`: the call did not do its job.
#
# Two kinds rather than one because they fail differently and for
# different reasons, and folding them together would make the one
# number worth watching unreadable.
DISTILL = "distill"
SEARCH = "search"
# 5b: the notebook reflection job (app/core/notebook.py). Same shape as
# DISTILL -- strict JSON, so `parse_fail` means exactly what it means
# there -- and its own kind rather than folding into EXTRACTOR, for the
# same reason DISTILL and SEARCH are not one kind: a reflection call
# that stops parsing must not hide inside a number that also covers the
# unrelated post-turn extractor.
NOTEBOOK = "notebook"
# 5d: the weekly review's own safety-model analysis call (app/core/
# review.py's analyze_week) -- same shape as NOTEBOOK above: strict
# JSON, so parse_fail means exactly what it means there, and its own
# kind rather than folding into EXTRACTOR or NOTEBOOK for the same
# reason those stayed apart from each other.
REVIEW = "review"
KINDS = (WELFARE, EXTRACTOR, TICK, DISTILL, SEARCH, NOTEBOOK, REVIEW)

# The outcomes that count as a failure on /state. A `fallback_hit` is
# deliberately neither: it is the keyword backstop doing its job, and
# folding it into either column would hide the one event most worth
# seeing.
FAILURE_OUTCOMES = (PARSE_FAIL, TIMEOUT, ERROR)

# How far back /state's welfare line looks.
WINDOW_DAYS = 7


async def record(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    clock: Clock,
    timezone: str,
    kind: str,
    outcome: str,
    model: str | None = None,
) -> None:
    """Write one outcome row. Never raises.

    Takes a sessionmaker rather than a session so the row lands in its
    own transaction: the caller is usually mid-turn, and an
    observability write has no business sharing a transaction with the
    reply it is observing -- a rollback on either side should not touch
    the other.
    """
    try:
        async with sessionmaker() as session:
            session.add(
                SafetyEvent(
                    local_date=clock_module.local_date(clock, timezone),
                    kind=kind,
                    outcome=outcome,
                    model=model,
                )
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - see the module docstring
        logger.warning(
            "safety event not recorded",
            extra={"event": type(exc).__name__, "kind": kind, "outcome": outcome},
        )


async def record_in(
    session: AsyncSession,
    *,
    clock: Clock,
    timezone: str,
    kind: str,
    outcome: str,
    model: str | None = None,
) -> None:
    """Stage one outcome row in the caller's transaction. Never raises.

    For the two background jobs, which already own a session and commit
    their own ledger row in it. Deliberately *not* used for a transport
    failure: that path re-raises so the queue can retry, which would roll
    this row back anyway -- and a failing extract or tick is already
    visible as a job row with a rising `attempts`. The welfare check has
    no queue behind it, fails open inline, and is the reason this table
    exists at all.
    """
    try:
        session.add(
            SafetyEvent(
                local_date=clock_module.local_date(clock, timezone),
                kind=kind,
                outcome=outcome,
                model=model,
            )
        )
    except Exception as exc:  # noqa: BLE001 - see the module docstring
        logger.warning(
            "safety event not staged",
            extra={"event": type(exc).__name__, "kind": kind, "outcome": outcome},
        )


async def counts(
    session: AsyncSession,
    clock: Clock,
    timezone: str,
    *,
    kind: str = WELFARE,
    days: int = WINDOW_DAYS,
) -> tuple[int, int]:
    """(ok, failures) for `kind` over the last `days` local days.

    Inclusive of today, so `days=7` is "this local day and the six
    before it". `fallback_hit` is in neither total -- see
    FAILURE_OUTCOMES.
    """
    today = clock_module.local_date(clock, timezone)
    since = today - datetime.timedelta(days=days - 1)
    result = await session.execute(
        select(SafetyEvent.outcome, func.count())
        .where(SafetyEvent.kind == kind, SafetyEvent.local_date >= since)
        .group_by(SafetyEvent.outcome)
    )
    tallied = {outcome: int(count) for outcome, count in result.all()}
    ok = tallied.get("ok", 0)
    failures = sum(tallied.get(name, 0) for name in FAILURE_OUTCOMES)
    return ok, failures


__all__ = [
    "DISTILL",
    "EXTRACTOR",
    "FAILURE_OUTCOMES",
    "KINDS",
    "NOTEBOOK",
    "REVIEW",
    "SEARCH",
    "TICK",
    "WELFARE",
    "WINDOW_DAYS",
    "counts",
    "record",
    "record_in",
]

# Re-exported so callers write safety_events.FALLBACK_HIT rather than
# reaching into app/core/welfare.py for a constant this table owns.
__all__ += ["ERROR", "FALLBACK_HIT", "PARSE_FAIL", "TIMEOUT"]
