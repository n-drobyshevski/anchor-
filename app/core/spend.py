"""Spend queries against `spend_ledger` (plan section 10).

1b only needs the read path, for /state. Everything that writes to
spend_ledger is later work:

# TODO(phase-1c): compute usd_cost with the formula in plan section 10
# (uncached/cached/output token pricing) and insert a spend_ledger row
# alongside each assistant message, in core/turn.py.
# TODO(phase-1d): enforce DAILY_USD_CAP by calling today_usd() before
# the LLM call in core/turn.py's spend check (plan section 8 step 3).
"""

from __future__ import annotations

import datetime
import decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SpendLedger


async def today_usd(session: AsyncSession, timezone: str) -> decimal.Decimal:
    """Sum usd_cost in spend_ledger for "today" in `timezone` (an IANA name).

    local_date is computed here, not from the server's local time, so
    this matches plan section 10's "local_date is computed in
    user_state.timezone".
    """
    local_today = datetime.datetime.now(ZoneInfo(timezone)).date()
    result = await session.execute(
        select(func.coalesce(func.sum(SpendLedger.usd_cost), 0)).where(
            SpendLedger.local_date == local_today
        )
    )
    return decimal.Decimal(result.scalar_one())
