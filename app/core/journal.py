"""Reading the journal (W4 roadmap section 4, "Journal").

`journal` rows are written by app/core/extract.py and nothing else;
this module only reads them, for the web Check-in screen's feed
(`GET /api/journal`). app/core/tick.py keeps its own private
`_recent_journal` (the last three lines for the tick decision) -- that
helper answers a different question ("what has been happening lately",
capped at three) and is deliberately left alone rather than
generalized into this one.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Journal


async def list_journal(
    session: AsyncSession, offset: int, limit: int
) -> tuple[list[Journal], int]:
    """One page of journal rows, newest first, plus the total row count.

    Ordered by `local_date` descending, then `id` descending -- the day
    a line belongs to is what the feed groups by, and `id` breaks ties
    within a day in insertion order (newest first) without depending on
    `created_at`'s resolution.
    """
    result = await session.execute(
        select(Journal)
        .order_by(Journal.local_date.desc(), Journal.id.desc())
        .offset(offset)
        .limit(limit)
    )
    rows = list(result.scalars())
    total = (await session.execute(select(func.count()).select_from(Journal))).scalar_one()
    return rows, total
