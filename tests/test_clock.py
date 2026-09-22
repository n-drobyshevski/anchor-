"""app/core/clock.py tests (phase-3 plan sections 3 and 13).

The plan makes Europe/Paris DST mandatory coverage, and names both
dates: **2026-10-25**, when the clocks go back and 02:00-03:00 local
happens twice, and **2027-03-28**, when they go forward and
02:00-03:00 local never happens at all.

Those two days are where every naive scheduler breaks, in opposite
directions: on the long day a "once per day" job fires twice, and on
the short day a job scheduled inside the gap never fires. So the
assertions here are not about formatting -- they are about the two
properties the rest of Phase 3 stands on:

1. a local wall-clock time maps to exactly one instant, on both days;
2. a local calendar day is 25 hours long on one and 23 on the other,
   and "the next local midnight" knows it.
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

import pytest

from app.core.clock import (
    UTC,
    FrozenClock,
    SystemClock,
    combine_local,
    local_date,
    local_date_of,
    local_target,
    local_time_of_day,
    next_local_midnight,
    now_local,
    to_local,
    within_window,
)

PARIS = "Europe/Paris"
NEW_YORK = "America/New_York"

# The two dates the plan names.
FALL_BACK = datetime.date(2026, 10, 25)  # 25-hour local day
SPRING_FORWARD = datetime.date(2027, 3, 28)  # 23-hour local day


def _paris(moment: datetime.datetime) -> datetime.datetime:
    return moment.astimezone(ZoneInfo(PARIS))


# --- the clocks themselves --------------------------------------------


def test_system_clock_returns_an_aware_utc_instant():
    now = SystemClock().now_utc()
    assert now.tzinfo is not None
    assert now.utcoffset() == datetime.timedelta(0)


def test_frozen_clock_does_not_move_on_its_own():
    clock = FrozenClock(datetime.datetime(2026, 9, 22, 12, 0, tzinfo=UTC))
    first = clock.now_utc()
    assert clock.now_utc() == first


def test_frozen_clock_advances_and_sets():
    clock = FrozenClock(datetime.datetime(2026, 9, 22, 12, 0, tzinfo=UTC))
    clock.advance(datetime.timedelta(hours=3))
    assert clock.now_utc() == datetime.datetime(2026, 9, 22, 15, 0, tzinfo=UTC)
    clock.set(datetime.datetime(2027, 1, 1, 0, 0, tzinfo=UTC))
    assert clock.now_utc() == datetime.datetime(2027, 1, 1, 0, 0, tzinfo=UTC)


def test_a_naive_moment_is_refused_rather_than_assumed_to_be_utc():
    """The assumption is only ever wrong in October, which is the worst
    time to discover it."""
    with pytest.raises(ValueError):
        FrozenClock(datetime.datetime(2026, 9, 22, 12, 0))


def test_frozen_clock_normalizes_a_non_utc_moment_to_utc():
    clock = FrozenClock(
        datetime.datetime(2026, 9, 22, 12, 0, tzinfo=ZoneInfo(PARIS))
    )
    assert clock.now_utc() == datetime.datetime(2026, 9, 22, 10, 0, tzinfo=UTC)


# --- local date and the midnight boundary ------------------------------


def test_local_date_is_the_users_date_not_utcs():
    """23:30 UTC is already tomorrow in Paris, and still today in New York."""
    clock = FrozenClock(datetime.datetime(2026, 6, 15, 23, 30, tzinfo=UTC))
    assert local_date(clock, PARIS) == datetime.date(2026, 6, 16)
    assert local_date(clock, NEW_YORK) == datetime.date(2026, 6, 15)


def test_local_date_rolls_over_at_local_midnight():
    # 21:59:59 UTC = 23:59:59 Paris (CEST); one second later is tomorrow.
    before = FrozenClock(datetime.datetime(2026, 6, 15, 21, 59, 59, tzinfo=UTC))
    after = FrozenClock(datetime.datetime(2026, 6, 15, 22, 0, 0, tzinfo=UTC))
    assert local_date(before, PARIS) == datetime.date(2026, 6, 15)
    assert local_date(after, PARIS) == datetime.date(2026, 6, 16)


def test_local_date_of_reads_a_past_instant_in_the_users_zone():
    moment = datetime.datetime(2026, 6, 15, 23, 30, tzinfo=UTC)
    assert local_date_of(moment, PARIS) == datetime.date(2026, 6, 16)
    assert local_date_of(moment, NEW_YORK) == datetime.date(2026, 6, 15)


def test_now_local_and_local_time_of_day_agree():
    clock = FrozenClock(datetime.datetime(2026, 6, 15, 7, 5, tzinfo=UTC))
    assert now_local(clock, PARIS).hour == 9
    assert local_time_of_day(clock, PARIS) == datetime.time(9, 5)


def test_to_local_refuses_a_naive_instant():
    with pytest.raises(ValueError):
        to_local(datetime.datetime(2026, 6, 15, 12, 0), PARIS)


# --- DST: 2026-10-25, the 25-hour day ----------------------------------


def test_fall_back_the_morning_target_is_one_instant_reading_0900_locally():
    target = combine_local(FALL_BACK, datetime.time(9, 0), PARIS)
    # 09:00 local on that day is CET (+01:00), the post-transition offset.
    assert target == datetime.datetime(2026, 10, 25, 8, 0, tzinfo=UTC)
    assert _paris(target).strftime("%H:%M") == "09:00"


def test_fall_back_the_repeated_hour_resolves_to_its_first_occurrence():
    """02:30 local happens twice on 2026-10-25. combine_local takes the
    earlier one, so a fixed intent fires at the first instant matching
    its wall clock; the grace window covers the rest of the doubled
    hour and the unique constraint stops a second message."""
    target = combine_local(FALL_BACK, datetime.time(2, 30), PARIS)
    assert target == datetime.datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    # The second occurrence is an hour later and is NOT what we returned.
    second = datetime.datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    assert _paris(second).strftime("%H:%M") == "02:30"
    assert target != second


def test_fall_back_the_local_day_is_twenty_five_hours_long():
    midnight = combine_local(FALL_BACK, datetime.time(0, 0), PARIS)
    next_midnight = combine_local(
        FALL_BACK + datetime.timedelta(days=1), datetime.time(0, 0), PARIS
    )
    assert next_midnight - midnight == datetime.timedelta(hours=25)


def test_fall_back_next_local_midnight_is_twenty_five_hours_from_the_last():
    """The daily budget resets on the calendar boundary, not 24h later."""
    clock = FrozenClock(combine_local(FALL_BACK, datetime.time(0, 0), PARIS))
    assert next_local_midnight(clock, PARIS) - clock.now_utc() == datetime.timedelta(
        hours=25
    )


def test_fall_back_local_date_is_stable_across_the_repeated_hour():
    """Both 02:30s belong to the same local date -- so a job keyed on
    (kind, local_date) cannot run twice by crossing the fold."""
    first = FrozenClock(datetime.datetime(2026, 10, 25, 0, 30, tzinfo=UTC))
    second = FrozenClock(datetime.datetime(2026, 10, 25, 1, 30, tzinfo=UTC))
    assert local_date(first, PARIS) == local_date(second, PARIS) == FALL_BACK


# --- DST: 2027-03-28, the 23-hour day ----------------------------------


def test_spring_forward_the_morning_target_is_one_instant_reading_0900_locally():
    target = combine_local(SPRING_FORWARD, datetime.time(9, 0), PARIS)
    # 09:00 local is CEST (+02:00) by then.
    assert target == datetime.datetime(2027, 3, 28, 7, 0, tzinfo=UTC)
    assert _paris(target).strftime("%H:%M") == "09:00"


def test_spring_forward_a_target_inside_the_gap_normalizes_forward():
    """02:30 local does not exist on 2027-03-28. It resolves to the
    instant just after the jump rather than to nothing, so a message
    configured into the gap is sent late instead of lost."""
    target = combine_local(SPRING_FORWARD, datetime.time(2, 30), PARIS)
    assert target == datetime.datetime(2027, 3, 28, 1, 30, tzinfo=UTC)
    assert _paris(target).strftime("%H:%M") == "03:30"


def test_spring_forward_the_local_day_is_twenty_three_hours_long():
    midnight = combine_local(SPRING_FORWARD, datetime.time(0, 0), PARIS)
    next_midnight = combine_local(
        SPRING_FORWARD + datetime.timedelta(days=1), datetime.time(0, 0), PARIS
    )
    assert next_midnight - midnight == datetime.timedelta(hours=23)


def test_spring_forward_next_local_midnight_is_twenty_three_hours_from_the_last():
    clock = FrozenClock(combine_local(SPRING_FORWARD, datetime.time(0, 0), PARIS))
    assert next_local_midnight(clock, PARIS) - clock.now_utc() == datetime.timedelta(
        hours=23
    )


def test_the_morning_target_is_the_same_wall_clock_across_both_dst_days():
    """The property that matters to the user: 09:00 is 09:00, whatever
    the UTC offset is doing that week."""
    for day in (FALL_BACK, SPRING_FORWARD, datetime.date(2026, 7, 1)):
        clock = FrozenClock(combine_local(day, datetime.time(5, 0), PARIS))
        target = local_target(clock, PARIS, datetime.time(9, 0))
        assert _paris(target).strftime("%H:%M") == "09:00"
        assert _paris(target).date() == day


# --- the quiet-hours window --------------------------------------------


@pytest.mark.parametrize(
    "moment, expected",
    [
        (datetime.time(22, 29), False),
        (datetime.time(22, 30), True),  # inclusive start
        (datetime.time(23, 59), True),
        (datetime.time(0, 0), True),  # past midnight
        (datetime.time(7, 59), True),
        (datetime.time(8, 0), False),  # exclusive end
        (datetime.time(9, 0), False),
        (datetime.time(22, 0), False),  # the evening nag's slot
    ],
)
def test_quiet_hours_wrap_past_midnight(moment, expected):
    assert within_window(moment, datetime.time(22, 30), datetime.time(8, 0)) is expected


@pytest.mark.parametrize(
    "moment, expected",
    [
        (datetime.time(9, 0), False),
        (datetime.time(10, 0), True),
        (datetime.time(11, 59), True),
        (datetime.time(12, 0), False),
    ],
)
def test_a_window_that_does_not_wrap_is_a_plain_range(moment, expected):
    assert within_window(moment, datetime.time(10, 0), datetime.time(12, 0)) is expected


def test_an_empty_window_never_matches():
    """start == end is "no window", not "always" -- so setting
    QUIET_START == QUIET_END disables quiet hours rather than muting
    the bot permanently."""
    for hour in range(24):
        assert (
            within_window(
                datetime.time(hour, 0), datetime.time(8, 0), datetime.time(8, 0)
            )
            is False
        )
