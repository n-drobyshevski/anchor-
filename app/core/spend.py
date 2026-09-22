"""Spend queries and cost computation against `spend_ledger` (plan section 10).

1b added only the read path, for /state. 1c adds the write side: cost
computation (compute_cost) and the cap check (check_cap) that
core/turn.py calls before every LLM call, per the user's decision to
ship the daily cap in 1c rather than 1d (plan section 0: the wallet
guard must exist the moment the API key goes live).
"""

from __future__ import annotations

import datetime
import decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import SpendLedger
from app.llm.provider import LLMUsage

_CENTS_EXPONENT = decimal.Decimal("0.000001")  # Numeric(10, 6): quantize to 6dp


def local_date_for(timezone: str) -> datetime.date:
    """Today's date in `timezone` (an IANA name).

    Factored out of today_usd so the ledger row (core/turn.py) and the
    cap check compute "today" identically -- both must agree on which
    calendar day a call belongs to, including across the Europe/Paris
    DST fold.
    """
    return datetime.datetime.now(ZoneInfo(timezone)).date()


async def today_usd(session: AsyncSession, timezone: str) -> decimal.Decimal:
    """Sum usd_cost in spend_ledger for "today" in `timezone` (an IANA name)."""
    local_today = local_date_for(timezone)
    result = await session.execute(
        select(func.coalesce(func.sum(SpendLedger.usd_cost), 0)).where(
            SpendLedger.local_date == local_today
        )
    )
    return decimal.Decimal(result.scalar_one())


async def check_cap(session: AsyncSession, settings: Settings, timezone: str) -> bool:
    """True iff today's spend has already reached DAILY_USD_CAP.

    Called *before* the LLM call (plan section 8 step 3): the check can
    let one call overshoot the cap slightly, which plan section 8
    explicitly accepts, in exchange for never blocking a call that is
    already in flight.
    """
    spent = await today_usd(session, timezone)
    return spent >= decimal.Decimal(str(settings.DAILY_USD_CAP))


def compute_cost(usage: LLMUsage, settings: Settings) -> decimal.Decimal:
    """usage.cost_usd (vendor-reported) when present, else the section 10 formula.

    Settings prices are floats and usd_cost is Numeric(10,6), so every
    price is converted via Decimal(str(price)) -- Decimal(float) would
    drag in that float's binary representation error (Decimal(0.1) is
    not 0.1), which would show up from the sixth decimal place onward.
    """
    if usage.cost_usd is not None:
        cost = usage.cost_usd
    else:
        uncached = usage.input_tokens - usage.cached_tokens
        price_in = decimal.Decimal(str(settings.LLM_PRICE_IN))
        price_cached = decimal.Decimal(str(settings.LLM_PRICE_CACHED))
        price_out = decimal.Decimal(str(settings.LLM_PRICE_OUT))
        million = decimal.Decimal(1_000_000)
        cost = (
            uncached * price_in + usage.cached_tokens * price_cached + usage.output_tokens * price_out
        ) / million
    # Applies to both branches above deliberately: a web search can
    # accompany either a vendor-reported cost or a formula fallback, and
    # the fee is independent of which one priced the tokens. Defaults to
    # zero -- see the LLM_WEB_SEARCH_PRICE_USD comment in app/config.py
    # for why (OpenRouter may already include the Exa fee in cost_usd).
    cost += usage.web_search_requests * decimal.Decimal(str(settings.LLM_WEB_SEARCH_PRICE_USD))
    return cost.quantize(_CENTS_EXPONENT, rounding=decimal.ROUND_HALF_UP)
