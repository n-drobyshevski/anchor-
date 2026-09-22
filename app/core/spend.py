"""Spend queries and cost computation against `spend_ledger` (plan section 10).

1b added only the read path, for /state. 1c adds the write side: cost
computation (compute_cost) and the cap check (check_cap) that
core/turn.py calls before every LLM call, per the user's decision to
ship the daily cap in 1c rather than 1d (plan section 0: the wallet
guard must exist the moment the API key goes live).

3a moves "today" onto the injected clock (phase-3 plan section 3).
`local_date_for(timezone)` is gone; every caller now passes a Clock and
goes through `app.core.clock.local_date`, so there is one definition of
the local calendar day for the whole application and a frozen clock can
drive the budget across a simulated midnight. The DST property it
carried -- that the cap rolls over on the user's calendar day, not
UTC's -- is unchanged and still tested here and in tests/test_clock.py.
"""

from __future__ import annotations

import decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core import clock as clock_module
from app.core.clock import Clock
from app.db.models import SpendLedger
from app.llm.provider import LLMUsage

_CENTS_EXPONENT = decimal.Decimal("0.000001")  # Numeric(10, 6): quantize to 6dp


async def today_usd(
    session: AsyncSession, clock: Clock, timezone: str
) -> decimal.Decimal:
    """Sum usd_cost in spend_ledger for "today" in `timezone` (an IANA name)."""
    local_today = clock_module.local_date(clock, timezone)
    result = await session.execute(
        select(func.coalesce(func.sum(SpendLedger.usd_cost), 0)).where(
            SpendLedger.local_date == local_today
        )
    )
    return decimal.Decimal(result.scalar_one())


async def today_by_category(
    session: AsyncSession, clock: Clock, timezone: str
) -> dict[str, decimal.Decimal]:
    """Today's spend per ledger category, largest first (plan section 11).

    Categories are not enumerated here: the ledger takes whatever
    category the writer used (chat, ooc, summary, extractor, and later
    welfare and checkin), and /state should show a new one the day it
    first appears rather than the day someone remembers to add it to a
    list.
    """
    local_today = clock_module.local_date(clock, timezone)
    result = await session.execute(
        select(SpendLedger.category, func.sum(SpendLedger.usd_cost))
        .where(SpendLedger.local_date == local_today)
        .group_by(SpendLedger.category)
        .order_by(func.sum(SpendLedger.usd_cost).desc())
    )
    return {category: decimal.Decimal(total) for category, total in result.all()}


async def check_cap(
    session: AsyncSession, settings: Settings, clock: Clock, timezone: str
) -> bool:
    """True iff today's spend has already reached DAILY_USD_CAP.

    Called *before* the LLM call (plan section 8 step 3): the check can
    let one call overshoot the cap slightly, which plan section 8
    explicitly accepts, in exchange for never blocking a call that is
    already in flight.
    """
    spent = await today_usd(session, clock, timezone)
    return spent >= decimal.Decimal(str(settings.DAILY_USD_CAP))


def price_triple_for(model: str | None, settings: Settings) -> tuple[
    decimal.Decimal, decimal.Decimal, decimal.Decimal
]:
    """The (input, cached, output) USD-per-million triple for `model`.

    2a makes cost model-aware (phase-2 plan section 2). The main model
    is matched first, so that when LLM_MODEL and LLM_MODEL_CHEAP name
    the same model -- which they do today, by decision -- the main
    triple is the one that applies and the two settings can never
    disagree about the price of one model.

    An unrecognized model (or None, which is what every Phase 1 caller
    passes) falls back to the main triple. That is the conservative
    direction: the main model is the more expensive of the two in every
    configuration where they differ at all, so an unknown model is
    never under-billed against the daily cap.

    Settings prices are floats and usd_cost is Numeric(10,6), so every
    price is converted via Decimal(str(price)) -- Decimal(float) would
    drag in that float's binary representation error (Decimal(0.1) is
    not 0.1), which would show up from the sixth decimal place onward.
    """
    if model is not None and model != settings.LLM_MODEL and model == settings.LLM_MODEL_CHEAP:
        return (
            decimal.Decimal(str(settings.LLM_CHEAP_PRICE_IN)),
            decimal.Decimal(str(settings.LLM_CHEAP_PRICE_CACHED)),
            decimal.Decimal(str(settings.LLM_CHEAP_PRICE_OUT)),
        )
    return (
        decimal.Decimal(str(settings.LLM_PRICE_IN)),
        decimal.Decimal(str(settings.LLM_PRICE_CACHED)),
        decimal.Decimal(str(settings.LLM_PRICE_OUT)),
    )


def compute_cost(
    usage: LLMUsage, settings: Settings, model: str | None = None
) -> decimal.Decimal:
    """usage.cost_usd (vendor-reported) when present, else the section 10 formula.

    `model` (2a) picks the price triple; it defaults to None, which
    means the main model, so every Phase 1 call site keeps its exact
    previous behaviour without being touched.
    """
    if usage.cost_usd is not None:
        cost = usage.cost_usd
    else:
        uncached = usage.input_tokens - usage.cached_tokens
        price_in, price_cached, price_out = price_triple_for(model, settings)
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
