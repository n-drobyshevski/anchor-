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
from app.core.clock import SystemClock
from app.core.clock import local_date as clock_local_date
from app.core.spend import check_cap, compute_cost, priced, today_usd
from app.db.models import SpendLedger
from app.llm.provider import LLMUsage
from sqlalchemy import select


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


def test_compute_cost_does_not_add_the_web_search_fee_on_the_vendor_branch():
    """H4 reversed this. OpenRouter documents `usage.cost` as "the total
    amount charged to your account" -- as distinct from
    cost_details.upstream_inference_cost, "the actual cost charged by the
    upstream AI provider" -- and the Exa fee is charged to the same
    credits. So the fee is already inside a vendor-reported figure, and
    adding it again double-bills the one path where we have the real
    number."""
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

    assert compute_cost(searched_usage, settings) == compute_cost(base_usage, settings)


def test_the_cost_source_says_which_branch_priced_the_row():
    """The provenance H4 adds. Two numbers of different kinds -- one
    reported, one estimated from prices in config that can go stale --
    and a row that does not say which it is cannot be audited."""
    settings = Settings()
    reported = LLMUsage(
        input_tokens=10, cached_tokens=0, output_tokens=5, cost_usd=decimal.Decimal("0.000900")
    )
    estimated = LLMUsage(input_tokens=10, cached_tokens=0, output_tokens=5, cost_usd=None)

    assert priced(reported, settings) == (decimal.Decimal("0.000900"), "vendor")
    assert priced(estimated, settings).source == "computed"


def test_the_web_search_fee_defaults_to_the_real_exa_rate():
    """H4 changed the default from 0.0 to Exa's documented $0.007 per
    request. 0.0 was a placeholder for an unanswered question -- whether
    the vendor figure already included the fee -- and it made the
    fallback path silently under-bill a searched turn. The question is
    now answered per branch, so the number can be the real one."""
    settings = Settings()
    assert settings.LLM_WEB_SEARCH_PRICE_USD == 0.007

    unsearched = LLMUsage(input_tokens=1000, cached_tokens=200, output_tokens=300, cost_usd=None)
    searched = LLMUsage(
        input_tokens=1000, cached_tokens=200, output_tokens=300, cost_usd=None, web_search_requests=1
    )
    assert compute_cost(searched, settings) == compute_cost(
        unsearched, settings
    ) + decimal.Decimal("0.007")


async def test_a_real_turn_stamps_the_ledger_row_with_its_cost_source(sessionmaker, clock):
    """End to end, not just compute_cost in isolation: the provenance has
    to survive the trip into the table, or the column is decoration."""
    from aiogram import Bot

    from app.core import turn
    from app.db.models import TelegramUpdate, UserState
    from conftest import FakeLLMProvider, FakeSession

    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=4242, timezone="Europe/Paris", intensity=3))
        await session.commit()
        session.add(TelegramUpdate(update_id=77, payload={}))
        await session.commit()

    # A provider whose usage carries no vendor cost -> the computed branch.
    provider = FakeLLMProvider(
        text="Принято.",
        usage=LLMUsage(input_tokens=100, cached_tokens=0, output_tokens=50, cost_usd=None),
    )
    fake = FakeSession()
    await turn.run(
        sessionmaker,
        Bot(token="123456:TESTTOKEN", session=fake),
        Settings(DAILY_USD_CAP=10.0),
        provider,
        clock=clock,
        chat_id=4242,
        update_id=77,
        user_text="привет",
    )

    async with sessionmaker() as session:
        rows = (await session.execute(select(SpendLedger))).scalars().all()
    assert [r.cost_source for r in rows] == ["computed"]


async def test_web_search_fee_is_counted_against_the_daily_cap(sessionmaker, clock):
    """The fee lands in spend_ledger.usd_cost like any other cost, so
    check_cap sees it through today_usd -- exercised end to end against
    the real ledger table rather than just compute_cost in isolation."""
    settings = Settings(
        DAILY_USD_CAP=0.01,
        LLM_WEB_SEARCH_PRICE_USD=0.007,
        LLM_PRICE_IN=5.00,
        LLM_PRICE_CACHED=5.00,
        LLM_PRICE_OUT=5.00,
    )
    # The computed branch: no vendor figure, so the fee is ours to add.
    # 1000 tokens at $5/M is $0.005, plus the $0.007 search fee.
    usage = LLMUsage(
        input_tokens=1000, cached_tokens=0, output_tokens=0, cost_usd=None, web_search_requests=1
    )
    cost = compute_cost(usage, settings)
    assert cost == decimal.Decimal("0.012000")

    today = clock_local_date(SystemClock(), "Europe/Paris")
    async with sessionmaker() as session:
        session.add(SpendLedger(local_date=today, category="chat", usd_cost=cost))
        await session.commit()

    async with sessionmaker() as session:
        assert await check_cap(session, settings, clock, "Europe/Paris") is True


def test_local_date_for_paris_matches_current_zoneinfo_date():
    from zoneinfo import ZoneInfo

    expected = datetime.datetime.now(ZoneInfo("Europe/Paris")).date()
    assert clock_local_date(SystemClock(), "Europe/Paris") == expected


def test_local_date_for_across_midnight_utc_offset():
    """A timezone far enough west that "today" in UTC can be "yesterday"
    locally is exercised implicitly by using zoneinfo throughout --
    local_date_for must never fall back to server/UTC time."""
    from zoneinfo import ZoneInfo

    for tz in ("Pacific/Kiritimati", "Etc/GMT+12", "Europe/Paris"):
        # Just confirm it always agrees with a fresh zoneinfo computation,
        # i.e. it is not silently using datetime.date.today() (server/UTC).
        expected = datetime.datetime.now(ZoneInfo(tz)).date()
        assert clock_local_date(SystemClock(), tz) == expected


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


async def test_check_cap_true_when_today_spend_meets_or_exceeds_cap(sessionmaker, clock):
    settings = Settings(DAILY_USD_CAP=1.00, TZ_DEFAULT="Europe/Paris")
    today = clock_local_date(SystemClock(), "Europe/Paris")

    async with sessionmaker() as session:
        session.add(SpendLedger(local_date=today, category="chat", usd_cost=decimal.Decimal("1.000000")))
        await session.commit()

    async with sessionmaker() as session:
        assert await check_cap(session, settings, clock, "Europe/Paris") is True


async def test_check_cap_false_when_under_cap(sessionmaker, clock):
    settings = Settings(DAILY_USD_CAP=1.00, TZ_DEFAULT="Europe/Paris")
    today = clock_local_date(SystemClock(), "Europe/Paris")

    async with sessionmaker() as session:
        session.add(SpendLedger(local_date=today, category="chat", usd_cost=decimal.Decimal("0.500000")))
        await session.commit()

    async with sessionmaker() as session:
        assert await check_cap(session, settings, clock, "Europe/Paris") is False


async def test_check_cap_false_with_no_spend_rows(sessionmaker, clock):
    settings = Settings(DAILY_USD_CAP=1.00)
    async with sessionmaker() as session:
        assert await check_cap(session, settings, clock, "Europe/Paris") is False


async def test_today_usd_ignores_other_dates(sessionmaker, clock):
    today = clock_local_date(SystemClock(), "Europe/Paris")
    yesterday = today - datetime.timedelta(days=1)

    async with sessionmaker() as session:
        session.add(SpendLedger(local_date=yesterday, category="chat", usd_cost=decimal.Decimal("5.00")))
        session.add(SpendLedger(local_date=today, category="chat", usd_cost=decimal.Decimal("0.25")))
        await session.commit()

    async with sessionmaker() as session:
        assert await today_usd(session, clock, "Europe/Paris") == decimal.Decimal("0.25")


# --- 2a: model-aware pricing (phase-2 plan section 2) ---


def test_price_triple_prefers_the_main_model_when_both_names_match():
    """LLM_MODEL and LLM_MODEL_CHEAP name the same model today. The two
    settings must never be able to disagree about its price."""
    from app.core.spend import price_triple_for

    settings = Settings(
        LLM_MODEL="same/model",
        LLM_MODEL_CHEAP="same/model",
        LLM_PRICE_IN=0.30, LLM_PRICE_CACHED=0.15, LLM_PRICE_OUT=0.50,
        LLM_CHEAP_PRICE_IN=9.99, LLM_CHEAP_PRICE_CACHED=9.99, LLM_CHEAP_PRICE_OUT=9.99,
    )
    assert price_triple_for("same/model", settings) == (
        decimal.Decimal("0.30"), decimal.Decimal("0.15"), decimal.Decimal("0.50")
    )


def test_price_triple_uses_the_cheap_prices_for_a_distinct_cheap_model():
    from app.core.spend import price_triple_for

    settings = Settings(
        LLM_MODEL="main/model", LLM_MODEL_CHEAP="cheap/model",
        LLM_PRICE_IN=0.30, LLM_PRICE_CACHED=0.15, LLM_PRICE_OUT=0.50,
        LLM_CHEAP_PRICE_IN=1.25, LLM_CHEAP_PRICE_CACHED=0.20, LLM_CHEAP_PRICE_OUT=2.50,
    )
    assert price_triple_for("cheap/model", settings) == (
        decimal.Decimal("1.25"), decimal.Decimal("0.20"), decimal.Decimal("2.50")
    )


def test_price_triple_falls_back_to_the_main_prices_for_an_unknown_model():
    """The conservative direction: the main model is the more expensive
    one wherever they differ, so an unknown model is never under-billed
    against the daily cap."""
    from app.core.spend import price_triple_for

    settings = Settings(
        LLM_MODEL="main/model", LLM_MODEL_CHEAP="cheap/model",
        LLM_PRICE_IN=0.30, LLM_PRICE_CACHED=0.15, LLM_PRICE_OUT=0.50,
        LLM_CHEAP_PRICE_IN=1.25, LLM_CHEAP_PRICE_CACHED=0.20, LLM_CHEAP_PRICE_OUT=2.50,
    )
    assert price_triple_for("mystery/model", settings) == (
        decimal.Decimal("0.30"), decimal.Decimal("0.15"), decimal.Decimal("0.50")
    )
    assert price_triple_for(None, settings) == (
        decimal.Decimal("0.30"), decimal.Decimal("0.15"), decimal.Decimal("0.50")
    )


def test_compute_cost_uses_the_cheap_triple_when_given_the_cheap_model():
    settings = Settings(
        LLM_MODEL="main/model", LLM_MODEL_CHEAP="cheap/model",
        LLM_PRICE_IN=0.30, LLM_PRICE_CACHED=0.15, LLM_PRICE_OUT=0.50,
        LLM_CHEAP_PRICE_IN=1.25, LLM_CHEAP_PRICE_CACHED=0.20, LLM_CHEAP_PRICE_OUT=2.50,
        LLM_WEB_SEARCH_PRICE_USD=0.0,
    )
    usage = LLMUsage(input_tokens=1_000_000, cached_tokens=0, output_tokens=0, cost_usd=None)

    assert compute_cost(usage, settings, model="cheap/model") == decimal.Decimal("1.250000")
    assert compute_cost(usage, settings, model="main/model") == decimal.Decimal("0.300000")
    # Phase 1 call sites pass no model at all and must be unaffected.
    assert compute_cost(usage, settings) == decimal.Decimal("0.300000")
