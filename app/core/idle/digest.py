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
from app.core.idle import BACKFILL
from app.db.models import IdleRun

WINDOW_24H = "24h"
WINDOW_7D = "7d"
WINDOWS = (WINDOW_24H, WINDOW_7D)

_PERIOD_LABEL = {WINDOW_24H: "24 ч", WINDOW_7D: "7 дн."}

HEADER = "Фоновая работа за {period} — ${cost:.2f}"
NOTHING_TEXT = "Фоновой работы не было."
SUMMARIZED_LINE = "• Сводки: догнала {n}"
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

    undoable = tuple(r.id for r in done if _is_undoable(r, now=now, undo_days=undo_days))

    skip_counts: dict[str, int] = {}
    for run in skipped:
        reason = run.skip_reason or "unknown"
        skip_counts[reason] = skip_counts.get(reason, 0) + 1
    if skip_counts:
        items = ", ".join(f"{reason} ×{count}" for reason, count in skip_counts.items())
        lines.append(SKIPS_LINE.format(items=items))

    return Digest(text="\n".join(lines), undoable_run_ids=undoable)


__all__ = ["WINDOW_24H", "WINDOW_7D", "WINDOWS", "Digest", "build_digest"]
