"""app/planner/parse.py: the deterministic regex path and the safety-model
fallback for /task and /event (P3).

Shaped like tests/test_quiet_tz.py (table-driven, pure) for the regex
half, and like tests/test_extract.py for the fallback half: check_cap
gates the provider call, and every field is re-validated regardless of
which path produced it.
"""

from __future__ import annotations

import datetime
import decimal

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db.models import SpendLedger
from app.planner import parse as planner_parse
from conftest import FakeLLMProvider

pytestmark = pytest.mark.asyncio

TZ = "Europe/Paris"


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


# --- /task: the regex path -------------------------------------------------


async def test_task_with_a_relative_date_word_parses_without_a_provider_call(
    sessionmaker, frozen_clock, fake_llm_provider
):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    async with sessionmaker() as session:
        result = await planner_parse.parse_task(
            session, _settings(), fake_llm_provider, clock,
            text="Купить молоко на завтра", timezone=TZ,
        )
    assert result.title == "Купить молоко"
    assert result.due_date == datetime.date(2026, 9, 24)
    assert fake_llm_provider.calls == 0


async def test_task_with_an_iso_date_parses(sessionmaker, frozen_clock, fake_llm_provider):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    async with sessionmaker() as session:
        result = await planner_parse.parse_task(
            session, _settings(), fake_llm_provider, clock,
            text="Сдать отчёт to 2026-10-01", timezone=TZ,
        )
    assert result.due_date == datetime.date(2026, 10, 1)
    assert fake_llm_provider.calls == 0


async def test_task_with_no_date_has_no_due_date(sessionmaker, frozen_clock, fake_llm_provider):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    async with sessionmaker() as session:
        result = await planner_parse.parse_task(
            session, _settings(), fake_llm_provider, clock,
            text="Позвонить маме", timezone=TZ,
        )
    assert result.title == "Позвонить маме"
    assert result.due_date is None
    assert fake_llm_provider.calls == 0


async def test_empty_task_text_is_a_parse_error_without_a_provider_call(
    sessionmaker, frozen_clock, fake_llm_provider
):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    async with sessionmaker() as session:
        with pytest.raises(planner_parse.ParseError):
            await planner_parse.parse_task(
                session, _settings(), fake_llm_provider, clock, text="", timezone=TZ
            )
    assert fake_llm_provider.calls == 0


# --- /event: the regex path -------------------------------------------------


async def test_event_with_explicit_time_and_relative_day(sessionmaker, frozen_clock, fake_llm_provider):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    async with sessionmaker() as session:
        result = await planner_parse.parse_event(
            session, _settings(), fake_llm_provider, clock,
            text="Встреча с Аней в 18:00 завтра", timezone=TZ,
        )
    assert result.title == "Встреча с Аней"
    assert not result.all_day
    local_start = result.start.astimezone(datetime.timezone(datetime.timedelta(hours=2)))
    assert local_start.hour == 18 and local_start.minute == 0
    assert local_start.date() == datetime.date(2026, 9, 24)
    assert result.end - result.start == planner_parse.DEFAULT_EVENT_DURATION
    assert fake_llm_provider.calls == 0


async def test_event_with_an_explicit_duration(sessionmaker, frozen_clock, fake_llm_provider):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    async with sessionmaker() as session:
        result = await planner_parse.parse_event(
            session, _settings(), fake_llm_provider, clock,
            text="Встреча в 18:00 на 2 ч", timezone=TZ,
        )
    assert result.end - result.start == datetime.timedelta(hours=2)


async def test_event_all_day_marker(sessionmaker, frozen_clock, fake_llm_provider):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    async with sessionmaker() as session:
        result = await planner_parse.parse_event(
            session, _settings(), fake_llm_provider, clock,
            text="Конференция весь день завтра", timezone=TZ,
        )
    assert result.all_day
    assert result.end - result.start == datetime.timedelta(days=1)
    assert fake_llm_provider.calls == 0


async def test_event_with_neither_time_nor_all_day_falls_back_to_the_model(
    sessionmaker, frozen_clock
):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    provider = FakeLLMProvider(
        text='{"title": "Ужин с друзьями", "date": "2026-09-25", '
        '"start_time": "19:00", "end_time": "21:00", "all_day": false}'
    )
    async with sessionmaker() as session:
        result = await planner_parse.parse_event(
            session, _settings(), provider, clock, text="Ужин с друзьями в пятницу", timezone=TZ,
        )
    assert result.title == "Ужин с друзьями"
    assert provider.calls == 1
    async with sessionmaker() as session:
        rows = (await session.execute(select(SpendLedger))).scalars().all()
    assert len(rows) == 1
    assert rows[0].category == "planner"


async def test_event_end_before_start_is_rejected(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    provider = FakeLLMProvider(
        text='{"title": "Что-то", "date": "2026-09-25", '
        '"start_time": "19:00", "end_time": "18:00", "all_day": false}'
    )
    async with sessionmaker() as session:
        with pytest.raises(planner_parse.ParseError):
            await planner_parse.parse_event(
                session, _settings(), provider, clock, text="что-то непонятное", timezone=TZ,
            )


async def test_event_more_than_a_year_out_is_rejected(sessionmaker, frozen_clock, fake_llm_provider):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    async with sessionmaker() as session:
        with pytest.raises(planner_parse.ParseError):
            await planner_parse.parse_event(
                session, _settings(), fake_llm_provider, clock,
                text="Событие в 10:00 2028-01-01", timezone=TZ,
            )


# --- the daily USD cap gates the fallback call, like app/core/extract.py ---


async def test_check_cap_blocks_the_provider_call(sessionmaker, frozen_clock, fake_llm_provider):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    settings = _settings(DAILY_USD_CAP=0.01)
    async with sessionmaker() as session:
        session.add(
            SpendLedger(
                local_date=datetime.date(2026, 9, 23), category="chat",
                usd_cost=decimal.Decimal("1.00"),
            )
        )
        await session.commit()

        # "завтра" alone leaves no title for the regex path (see the
        # title-length test below), so this would reach the provider --
        # if check_cap did not block it first.
        with pytest.raises(planner_parse.ParseError):
            await planner_parse.parse_task(
                session, settings, fake_llm_provider, clock, text="завтра", timezone=TZ,
            )
    assert fake_llm_provider.calls == 0


async def test_a_title_over_the_limit_is_rejected_even_from_the_model(sessionmaker, frozen_clock):
    """The regex path itself would reject a 250-char title as unparseable
    and fall through to the model -- so this drives _validate_task's own
    length check directly, via a well-formed but oversized model reply."""
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    long_title = "A" * 250
    provider = FakeLLMProvider(text=f'{{"title": "{long_title}", "due_date": null}}')
    async with sessionmaker() as session:
        # "завтра" alone: the date word consumes the whole string, leaving
        # no title for the regex path -- it falls through to the model.
        with pytest.raises(planner_parse.ParseError):
            await planner_parse.parse_task(
                session, _settings(), provider, clock, text="завтра", timezone=TZ,
            )
    assert provider.calls == 1
