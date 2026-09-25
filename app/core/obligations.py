"""The debt queue (phase 5, spec 2026-09-25, slice 4).

A debt is one thing the user still owes: last night's check-in, the
main action set with `/due`, or something they promised in chat and
confirmed with a button. The persona prompt shows the oldest few under
"## Долг", and persona.md's rule says the next order closes one of them
before anything new is invented.

**Who may open one.** Only code paths a user caused: `/due`
(`replace_focus`, kind='focus'), an accepted extractor proposal
(kind='promised', via app/core/proposal.py's accept()), and the daily
sweep for a missed evening check-in (kind='checkin'). The extractor
itself never calls into this module; tests/test_extract.py checks that
by AST.

**The cap.** At most `MAX_OPEN` rows are open. A CHECK cannot count
rows, so `open_()` counts under the caller's transaction and returns
None at the cap. A new debt needs an old one closed first, which is the
point: the queue must stay short enough to be read in one glance.

**Closing is idempotent.** `close()` only touches a row that is still
open, so a replayed button press returns None and changes nothing,
the same shape as app/core/orders.py's retire().

This module is listed in tests/test_autonomy_isolation.py: it reads
`checkin` rows through the model, never through app.core.checkin, and
writes no `user_state` column at all.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import func, select
from sqlalchemy import update as sql_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clock as clock_module
from app.core.clock import Clock
from app.db.models import Checkin, Obligation

logger = logging.getLogger(__name__)

KINDS = ("checkin", "focus", "promised", "missed", "custom")
SOURCES = ("user", "checkin", "command", "proposal")
OPEN = "open"
DONE = "done"
DROPPED = "dropped"

TEXT_MAX = 200
# The spec's cap. A constant, not a setting: /due, the proposal accept
# and the sweep all enforce it, and some of them have no Settings.
MAX_OPEN = 5

CHECKIN_TEXT = "чек-ин за {date}"

# The daily sweep's job kind (app/core/scheduler.py enqueues it,
# app/worker.py runs `sweep_missed_checkin`).
OBLIGATION_SWEEP = "obligation_sweep"


def _clean(text: str) -> str:
    return " ".join(text.split())[:TEXT_MAX]


async def open_count(session: AsyncSession) -> int:
    result = await session.execute(
        select(func.count()).select_from(Obligation).where(Obligation.status == OPEN)
    )
    return int(result.scalar_one())


async def open_(
    session: AsyncSession,
    *,
    text: str,
    kind: str,
    source: str,
    due_local_date: datetime.date | None = None,
    max_open: int = MAX_OPEN,
) -> Obligation | None:
    """Open one debt, or return None when the queue is full or `text` is empty.

    Commits. The count and the insert share the caller's transaction;
    there is one user, so a concurrent insert racing the count is not a
    case worth a lock.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown obligation kind: {kind}")
    if source not in SOURCES:
        raise ValueError(f"unknown obligation source: {source}")
    cleaned = _clean(text)
    if not cleaned:
        return None
    if await open_count(session) >= max_open:
        logger.info("obligation cap reached", extra={"event": "obligation_cap", "kind": kind})
        return None
    row = Obligation(text=cleaned, kind=kind, source=source, due_local_date=due_local_date)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    logger.info(
        "obligation opened", extra={"obligation_id": row.id, "kind": kind, "source": source}
    )
    return row


async def open_list(session: AsyncSession) -> list[Obligation]:
    """Every open debt, oldest first."""
    result = await session.execute(
        select(Obligation)
        .where(Obligation.status == OPEN)
        .order_by(Obligation.opened_at, Obligation.id)
    )
    return list(result.scalars().all())


async def close(
    session: AsyncSession, clock: Clock, obligation_id: int, status: str = DONE
) -> Obligation | None:
    """Close one open debt as done or dropped. None if it was not open.

    Keyed on status='open' in the UPDATE itself, so a replayed or
    concurrent press can close a row at most once.
    """
    if status not in (DONE, DROPPED):
        raise ValueError(f"cannot close an obligation as {status!r}")
    result = await session.execute(
        sql_update(Obligation)
        .where(Obligation.id == obligation_id, Obligation.status == OPEN)
        .values(status=status, closed_at=clock.now_utc())
        .returning(Obligation.id)
    )
    closed_id = result.scalar_one_or_none()
    await session.commit()
    if closed_id is None:
        return None
    logger.info("obligation closed", extra={"obligation_id": closed_id, "status": status})
    return await session.get(Obligation, closed_id, populate_existing=True)


async def close_kind(session: AsyncSession, clock: Clock, kind: str, status: str = DONE) -> int:
    """Close every open debt of `kind`. Returns how many. Commits."""
    result = await session.execute(
        sql_update(Obligation)
        .where(Obligation.kind == kind, Obligation.status == OPEN)
        .values(status=status, closed_at=clock.now_utc())
        .returning(Obligation.id)
    )
    count = len(result.all())
    await session.commit()
    if count:
        logger.info("obligations closed", extra={"kind": kind, "count": count, "status": status})
    return count


async def replace_focus(
    session: AsyncSession,
    clock: Clock,
    text: str | None,
    *,
    max_open: int = MAX_OPEN,
) -> Obligation | None:
    """`/due`'s debt: drop the open focus debt, then open the new one.

    An empty or None `text` (a bare `/due`, which clears the main
    action) only drops. Dropping first frees the slot the new row takes,
    so replacing never runs into the cap on its own account.
    """
    await close_kind(session, clock, "focus", DROPPED)
    if not text or not text.strip():
        return None
    return await open_(session, text=text, kind="focus", source="command", max_open=max_open)


def prompt_lines(
    rows: list[Obligation], today: datetime.date, limit: int
) -> tuple[list[str], bool]:
    """Render up to `limit` of the oldest open debts for "## Долг". Pure.

    `rows` is oldest first (as `open_list` returns). The overdue flag
    covers every row, not only the ones shown: a fourth, hidden overdue
    debt still means "close a debt first".
    """
    def overdue(row: Obligation) -> bool:
        return row.due_local_date is not None and row.due_local_date < today

    lines = []
    for row in rows[:limit]:
        since = row.opened_at.strftime("%d.%m") if row.opened_at else ""
        tail = ", просрочено" if overdue(row) else ""
        lines.append(f"«{row.text}» (с {since}{tail})" if since else f"«{row.text}»{tail}")
    return lines, any(overdue(row) for row in rows)


async def oldest_open_text(session: AsyncSession) -> str | None:
    result = await session.execute(
        select(Obligation.text)
        .where(Obligation.status == OPEN)
        .order_by(Obligation.opened_at, Obligation.id)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def sweep_missed_checkin(
    session: AsyncSession,
    clock: Clock,
    timezone: str,
    *,
    max_open: int = MAX_OPEN,
) -> Obligation | None:
    """Open a 'checkin' debt for yesterday's missed evening check-in.

    The same rule app/core/mood.py uses for настороже: yesterday has no
    check-in row, and at least one earlier check-in exists, so a user on
    their first day never starts in debt. Idempotent: the partial unique
    index on (kind, due_local_date) turns a second run for the same day
    into a no-op.

    `due_local_date` is today: the report was owed last night, so it is
    overdue from tomorrow on if still open.
    """
    today = clock_module.local_date(clock, timezone)
    yesterday = today - datetime.timedelta(days=1)
    has_yesterday = (
        await session.execute(select(Checkin.id).where(Checkin.local_date == yesterday).limit(1))
    ).scalar_one_or_none() is not None
    if has_yesterday:
        return None
    has_earlier = (
        await session.execute(select(Checkin.id).where(Checkin.local_date < yesterday).limit(1))
    ).scalar_one_or_none() is not None
    if not has_earlier:
        return None
    if await open_count(session) >= max_open:
        logger.info("obligation cap reached", extra={"event": "obligation_cap", "kind": "checkin"})
        return None
    result = await session.execute(
        pg_insert(Obligation)
        .values(
            text=CHECKIN_TEXT.format(date=yesterday.strftime("%d.%m")),
            kind="checkin",
            source="checkin",
            due_local_date=today,
        )
        .on_conflict_do_nothing(
            index_elements=["kind", "due_local_date"],
            index_where=Obligation.kind == "checkin",
        )
        .returning(Obligation.id)
    )
    new_id = result.scalar_one_or_none()
    await session.commit()
    if new_id is None:
        return None
    logger.info("obligation opened", extra={"obligation_id": new_id, "kind": "checkin", "source": "checkin"})
    return await session.get(Obligation, new_id)
