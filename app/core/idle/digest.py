"""`/digest` (Phase 6 plan section 7; approved plan §5).

`build_digest` renders the plain, out-of-character Russian text plus
which runs are undo-eligible. 6a renders the header (total idle cost
over the window), the "Сводки: догнала N" line and the "Пропуски: ..."
line -- every other bullet in plan section 7's example appears once its
own kind lands (6b-6d).

Nothing here ever touches Telegram: `app/tg/idle.py` sends the text this
module returns. See tests/test_idle_isolation.py for the structural
guarantee that nothing under app/core/idle/ can reach `app.tg` at all.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.idle import BACKFILL, CANARY, CONSOLIDATE, CRITIQUE, PREBRIEF, REFLECT
from app.db.models import IdleRun

WINDOW_24H = "24h"
WINDOW_7D = "7d"
WINDOWS = (WINDOW_24H, WINDOW_7D)

_PERIOD_LABEL = {WINDOW_24H: "24 ч", WINDOW_7D: "7 дн."}

HEADER = "Фоновая работа за {period} — ${cost:.2f}"
NOTHING_TEXT = "Фоновой работы не было."
SUMMARIZED_LINE = "• Сводки: догнала {n}"
# 6b (plan section 7): consolidate/reflect are single-transaction,
# reversible kinds, and the coordinator's resolution is one line **per
# run**, newest first, each with its own [Отменить] -- unlike
# SUMMARIZED_LINE's aggregate, a button must name exactly one run id,
# so these can never be folded into one summed bullet the way backfill
# is.
MEMORY_LINE = "• Память: {merged} объединения, {contradictions} противоречие"
NOTES_LINE = "• Заметки: +{added}, закрыто {closed}"
# 6c (plan section 7), verbatim.
PREBRIEF_LINE = "• Утро: заметки готовы"
CRITIQUE_LINE = "• Самопроверка: {count} ответов, ниже нормы — {below_norm}"
CANARY_OK_LINE = "• Канарейка: ок"
CANARY_REGRESSION_LINE = "• ⚠️ Регрессия: кейсы {cases}"
SKIPS_LINE = "Пропуски: {items}"


@dataclasses.dataclass(frozen=True)
class Digest:
    """The rendered text, and which idle_run ids /digest may offer
    [Отменить] for (`app/tg/idle.py`'s job, not this module's)."""

    text: str
    undoable_run_ids: tuple[int, ...]


def _period_start(clock: Clock, window: str) -> datetime.datetime:
    now = clock.now_utc()
    if window == WINDOW_7D:
        return now - datetime.timedelta(days=7)
    return now - datetime.timedelta(hours=24)


def _is_undoable(run: IdleRun, *, now: datetime.datetime, undo_days: int) -> bool:
    if not run.reversible or run.status != "done" or run.undone_at is not None:
        return False
    return (now - run.created_at) < datetime.timedelta(days=undo_days)


async def build_digest(
    session: AsyncSession, clock: Clock, *, undo_days: int, window: str = WINDOW_24H
) -> Digest:
    """Every idle_run row since `_period_start(window)`, rendered."""
    since = _period_start(clock, window)
    now = clock.now_utc()
    result = await session.execute(
        select(IdleRun).where(IdleRun.created_at >= since).order_by(IdleRun.id)
    )
    runs = list(result.scalars().all())

    if not runs:
        return Digest(text=NOTHING_TEXT, undoable_run_ids=())

    done = [r for r in runs if r.status == "done"]
    skipped = [r for r in runs if r.status == "skipped"]
    total_cost = sum((r.usd_cost for r in runs), start=decimal.Decimal(0))

    lines = [HEADER.format(period=_PERIOD_LABEL[window], cost=float(total_cost))]

    summarized = sum(
        int((r.summary or {}).get("summarized", 0) or 0) for r in done if r.kind == BACKFILL
    )
    if summarized:
        lines.append(SUMMARIZED_LINE.format(n=summarized))

    # 6b: one line per consolidate/reflect run, newest first -- each
    # such run is reversible and its own [Отменить] button must name
    # exactly this run's id, so they cannot be folded into one summed
    # bullet the way backfill's SUMMARIZED_LINE is.
    memory_reflect_runs = sorted(
        (r for r in done if r.kind in (CONSOLIDATE, REFLECT)), key=lambda r: r.id, reverse=True
    )
    for run in memory_reflect_runs:
        summary = run.summary or {}
        if run.kind == CONSOLIDATE:
            merged = int(summary.get("merged", 0) or 0)
            contradictions = int(summary.get("contradicted", 0) or 0)
            if merged or contradictions:
                lines.append(MEMORY_LINE.format(merged=merged, contradictions=contradictions))
        else:
            added = int(summary.get("added", 0) or 0)
            closed = int(summary.get("closed", 0) or 0)
            if added or closed:
                lines.append(NOTES_LINE.format(added=added, closed=closed))

    # 6c: prebrief/critique/canary are not per-run buttons (none of the
    # three is reversible -- app/core/idle/runner.py always sets
    # reversible=False for them), so each gets one summarizing line
    # rather than one line per run, the same posture SUMMARIZED_LINE
    # takes for backfill above.
    prebrief_runs = [r for r in done if r.kind == PREBRIEF]
    if any(int((r.summary or {}).get("notes", 0) or 0) > 0 for r in prebrief_runs):
        lines.append(PREBRIEF_LINE)

    critique_runs = sorted(
        (r for r in done if r.kind == CRITIQUE), key=lambda r: r.id, reverse=True
    )
    if critique_runs:
        summary = critique_runs[0].summary or {}
        lines.append(
            CRITIQUE_LINE.format(
                count=int(summary.get("count", 0) or 0),
                below_norm=int(summary.get("below_norm", 0) or 0),
            )
        )

    canary_runs = sorted((r for r in done if r.kind == CANARY), key=lambda r: r.id, reverse=True)
    if canary_runs:
        summary = canary_runs[0].summary or {}
        if summary.get("passed", True):
            lines.append(CANARY_OK_LINE)
        else:
            cases = summary.get("cases") or {}
            failed = sorted(case_id for case_id, ok in cases.items() if not ok)
            lines.append(CANARY_REGRESSION_LINE.format(cases=", ".join(failed)))

    undoable = tuple(
        r.id
        for r in sorted(done, key=lambda r: r.id, reverse=True)
        if _is_undoable(r, now=now, undo_days=undo_days)
    )

    skip_counts: dict[str, int] = {}
    for run in skipped:
        reason = run.skip_reason or "unknown"
        skip_counts[reason] = skip_counts.get(reason, 0) + 1
    if skip_counts:
        items = ", ".join(f"{reason} ×{count}" for reason, count in skip_counts.items())
        lines.append(SKIPS_LINE.format(items=items))

    return Digest(text="\n".join(lines), undoable_run_ids=undoable)


__all__ = [
    "WINDOW_24H",
    "WINDOW_7D",
    "WINDOWS",
    "PREBRIEF_LINE",
    "CRITIQUE_LINE",
    "CANARY_OK_LINE",
    "CANARY_REGRESSION_LINE",
    "Digest",
    "build_digest",
]
