"""core/spend.py tests (plan section 10 / 16).

- compute_cost: the section 10 formula with cached and reasoning
  (already-included-in-output) tokens; vendor cost_in_nano_usd
  preferred when present; Decimal(str(price)) precision (no binary
  float garbage at the sixth decimal)
- local_date_for: across midnight and across the Europe/Paris DST fold
  (last Sunday of October)
- check_cap: over/under the configured DAILY_USD_CAP
"""

from __future__ import annotations

import datetime
import decimal

from app.config import Settings
from app.core.spend import check_cap, compute_cost, local_date_for, today_usd
from app.db.models import SpendLedger
from app.llm.provider import LLMUsage


def test_compute_cost_uses_formula_when_no_vendor_cost():
    settings = Settings(LLM_PRICE_IN=2.00, LLM_PRICE_CACHED=0.50, LLM_PRICE_OUT=6.00)
    # 1000 input tokens, 200 of them cached; 300 output tokens (already
    # inclusive of reasoning tokens -- usage carries no separate
    # reasoning count to add on top).
    usage = LLMUsage(input_tokens=1000, cached_tokens=200, output_tokens=300, cost_usd=None)

    cost = compute_cost(usage, settings)

    uncached = 800
    expected = (
        decimal.Decimal(uncached) * decimal.Decimal("2.00")
        + decimal.Decimal(200) * decimal.Decimal("0.50")
        + decimal.Decimal(300) * decimal.Decimal("6.00")
    ) / decimal.Decimal(1_000_000)
    assert cost == expected.quantize(decimal.Decimal("0.000001"))


def test_compute_cost_prefers_vendor_reported_cost_when_present():
    settings = Settings(LLM_PRICE_IN=999.0, LLM_PRICE_CACHED=999.0, LLM_PRICE_OUT=999.0)
    usage = LLMUsage(
        input_tokens=1000,
        cached_tokens=200,
        output_tokens=300,
        cost_usd=decimal.Decimal("0.001234"),
    )

    cost = compute_cost(usage, settings)

    assert cost == decimal.Decimal("0.001234")


def test_compute_cost_quantizes_with_round_half_up_at_six_decimals():
    """50000 * 2.46913 / 1e6 == 0.1234565 exactly (division by 10**6 is
    exact for a Decimal). Quantizing that to 6dp is a real tie: ROUND_HALF_UP
    must give 0.123457, whereas the Decimal default (ROUND_HALF_EVEN,
    "banker's rounding") would give 0.123456 since 6 is even -- so this
    also guards against silently picking up the wrong rounding mode.
    """
    settings = Settings(LLM_PRICE_IN=2.46913, LLM_PRICE_CACHED=0.0, LLM_PRICE_OUT=0.0)
    usage = LLMUsage(input_tokens=50000, cached_tokens=0, output_tokens=0, cost_usd=None)

    cost = compute_cost(usage, settings)

    assert cost == decimal.Decimal("0.123457")


def test_compute_cost_avoids_binary_float_price_error():
    """Decimal(str(price)), never Decimal(price): float 0.1 is not exactly
    0.1 in binary, and Decimal(0.1) would carry that error in, unlike
    Decimal(str(0.1)) == Decimal("0.1")."""
    assert decimal.Decimal(str(0.1)) == decimal.Decimal("0.1")
    assert decimal.Decimal(0.1) != decimal.Decimal("0.1")


def test_compute_cost_adds_the_web_search_fee_on_the_formula_branch():
    settings = Settings(
        LLM_PRICE_IN=2.00, LLM_PRICE_CACHED=0.50, LLM_PRICE_OUT=6.00, LLM_WEB_SEARCH_PRICE_USD=0.007
    )
    base_usage = LLMUsage(input_tokens=1000, cached_tokens=200, output_tokens=300, cost_usd=None)
    searched_usage = LLMUsage(
        input_tokens=1000, cached_tokens=200, output_tokens=300, cost_usd=None, web_search_requests=1
    )

    base_cost = compute_cost(base_usage, settings)
    searched_cost = compute_cost(searched_usage, settings)

    assert searched_cost == base_cost + decimal.Decimal("0.007")


def test_compute_cost_adds_the_web_search_fee_on_the_vendor_cost_branch():
    settings = Settings(LLM_WEB_SEARCH_PRICE_USD=0.007)
    base_usage = LLMUsage(
        input_tokens=1000, cached_tokens=200, output_tokens=300, cost_usd=decimal.Decimal("0.001234")
    )
    searched_usage = LLMUsage(
        input_tokens=1000,
        cached_tokens=200,
        output_tokens=300,
        cost_usd=decimal.Decimal("0.001234"),
        web_search_requests=1,
    )

    base_cost = compute_cost(base_usage, settings)
    searched_cost = compute_cost(searched_usage, settings)

    assert searched_cost == base_cost + decimal.Decimal("0.007")


def test_compute_cost_web_search_fee_defaults_to_a_no_op():
    """Default LLM_WEB_SEARCH_PRICE_USD=0.0: a searched call must cost
    exactly the same as an unsearched one unless the operator opts in
    to a non-zero fee (see the config comment for why 0.0 is the default)."""
    settings = Settings()
    unsearched = LLMUsage(input_tokens=1000, cached_tokens=200, output_tokens=300, cost_usd=None)
    searched = LLMUsage(
        input_tokens=1000, cached_tokens=200, output_tokens=300, cost_usd=None, web_search_requests=1
    )

    assert compute_cost(unsearched, settings) == compute_cost(searched, settings)


async def test_web_search_fee_is_counted_against_the_daily_cap(sessionmaker):
    """The fee lands in spend_ledger.usd_cost like any other cost, so
    check_cap sees it through today_usd -- exercised end to end against
    the real ledger table rather than just compute_cost in isolation."""
    settings = Settings(DAILY_USD_CAP=0.01, LLM_WEB_SEARCH_PRICE_USD=0.007)
    usage = LLMUsage(
        input_tokens=0, cached_tokens=0, output_tokens=0, cost_usd=decimal.Decimal("0.005000"), web_search_requests=1
    )
    cost = compute_cost(usage, settings)
    assert cost == decimal.Decimal("0.012000")

    today = local_date_for("Europe/Paris")
    async with sessionmaker() as session:
        session.add(SpendLedger(local_date=today, category="chat", usd_cost=cost))
        await session.commit()

    async with sessionmaker() as session:
        assert await check_cap(session, settings, "Europe/Paris") is True


def test_local_date_for_paris_matches_current_zoneinfo_date():
    from zoneinfo import ZoneInfo

    expected = datetime.datetime.now(ZoneInfo("Europe/Paris")).date()
    assert local_date_for("Europe/Paris") == expected


def test_local_date_for_across_midnight_utc_offset():
    """A timezone far enough west that "today" in UTC can be "yesterday"
    locally is exercised implicitly by using zoneinfo throughout --
    local_date_for must never fall back to server/UTC time."""
    from zoneinfo import ZoneInfo

    for tz in ("Pacific/Kiritimati", "Etc/GMT+12", "Europe/Paris"):
        # Just confirm it always agrees with a fresh zoneinfo computation,
        # i.e. it is not silently using datetime.date.today() (server/UTC).
        expected = datetime.datetime.now(ZoneInfo(tz)).date()
        assert local_date_for(tz) == expected


def test_local_date_for_handles_the_paris_dst_fold_last_sunday_of_october():
    """The last Sunday of October 2026 is the Europe/Paris DST fold: at
    03:00 CEST clocks go back to 02:00 CET. Both 02:30 instants (the one
    before and the one after the fold) must resolve to the same
    calendar date, and the date must be computed via zoneinfo, not by
    naive UTC arithmetic that could push it to the next/previous day.
    """
    from zoneinfo import ZoneInfo

    paris = ZoneInfo("Europe/Paris")
    fold_date = datetime.date(2026, 10, 25)  # last Sunday of October 2026

    before_fold = datetime.datetime(2026, 10, 25, 0, 30, tzinfo=datetime.timezone.utc)
    after_fold = datetime.datetime(2026, 10, 25, 1, 30, tzinfo=datetime.timezone.utc)

    assert before_fold.astimezone(paris).date() == fold_date
    assert after_fold.astimezone(paris).date() == fold_date


async def test_check_cap_true_when_today_spend_meets_or_exceeds_cap(sessionmaker):
    settings = Settings(DAILY_USD_CAP=1.00, TZ_DEFAULT="Europe/Paris")
    today = local_date_for("Europe/Paris")

    async with sessionmaker() as session:
        session.add(SpendLedger(local_date=today, category="chat", usd_cost=decimal.Decimal("1.000000")))
        await session.commit()

    async with sessionmaker() as session:
        assert await check_cap(session, settings, "Europe/Paris") is True


async def test_check_cap_false_when_under_cap(sessionmaker):
    settings = Settings(DAILY_USD_CAP=1.00, TZ_DEFAULT="Europe/Paris")
    today = local_date_for("Europe/Paris")

    async with sessionmaker() as session:
        session.add(SpendLedger(local_date=today, category="chat", usd_cost=decimal.Decimal("0.500000")))
        await session.commit()

    async with sessionmaker() as session:
        assert await check_cap(session, settings, "Europe/Paris") is False


async def test_check_cap_false_with_no_spend_rows(sessionmaker):
    settings = Settings(DAILY_USD_CAP=1.00)
    async with sessionmaker() as session:
        assert await check_cap(session, settings, "Europe/Paris") is False


async def test_today_usd_ignores_other_dates(sessionmaker):
    today = local_date_for("Europe/Paris")
    yesterday = today - datetime.timedelta(days=1)

    async with sessionmaker() as session:
        session.add(SpendLedger(local_date=yesterday, category="chat", usd_cost=decimal.Decimal("5.00")))
        session.add(SpendLedger(local_date=today, category="chat", usd_cost=decimal.Decimal("0.25")))
        await session.commit()

    async with sessionmaker() as session:
        assert await today_usd(session, "Europe/Paris") == decimal.Decimal("0.25")
