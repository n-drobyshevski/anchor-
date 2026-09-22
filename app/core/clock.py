"""The clock abstraction (phase-3 plan section 3).

Phase 3 is the first phase whose behaviour depends on *when* it is, not
just on what the user sent. A morning message that fires at 09:07, an
evening nag that must land before quiet hours, a 48-hour silence
nudge, a daily budget that resets at local midnight -- none of that can
be tested against the wall clock, and none of it can be trusted if
every module reads the time its own way.

So: one protocol, injected. `Clock.now_utc()` is the single source of
"now" for everything under app/core/, and `FrozenClock` lets a test
simulate a whole day (including both Europe/Paris DST transitions) in
milliseconds. tests/test_core_clock_discipline.py walks the AST of
every module in this package and fails if one calls datetime.now(),
datetime.utcnow(), date.today() or time.time() directly, so the rule
cannot decay back into scattered reads.

Two deliberate exceptions live *outside* this package and stay as they
are:

- app/db/queue.py compares `run_after <= func.now()` in SQL. Job
  due-ness must use the database's clock, because that is what makes a
  `run_after` written by one process meaningful to another after a
  restart. Injecting a Python clock there would let a frozen test
  clock hide a real scheduling bug.
- `server_default=func.now()` on every `created_at` column. Those are
  audit stamps, never read by logic. Every timestamp Phase 3 actually
  *reasons about* -- planned_for, sent_at, last_user_msg_at,
  last_outbound_at, welfare_at, quiet_until -- is set explicitly from
  the clock in Python, so a FrozenClock controls all of it.

**Local time.** Local is always `ZoneInfo(user_state.timezone)`, never
the host's zone and never TZ_DEFAULT (which only seeds the column at
first boot). "Local date" is the calendar date in that zone.
"""

from __future__ import annotations

import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

UTC = datetime.timezone.utc


class Clock(Protocol):
    """Reads the current instant. The only permitted source of "now"."""

    def now_utc(self) -> datetime.datetime:
        """The current instant, as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    """The real clock. One instance is built in app/main.py and injected."""

    __slots__ = ()

    def now_utc(self) -> datetime.datetime:
        return datetime.datetime.now(UTC)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "SystemClock()"


class FrozenClock:
    """A clock a test drives by hand.

    Lives in the application package rather than in conftest because the
    plan (section 3) names it alongside SystemClock, and because the
    manual eval harness and scripts/ may want to pin a moment too.

    The moment must be timezone-aware; a naive one is rejected rather
    than silently assumed to be UTC, since that assumption is exactly
    what produces an off-by-one-hour bug that only shows up in October.
    """

    __slots__ = ("_now",)

    def __init__(self, moment: datetime.datetime) -> None:
        self._now = _require_aware(moment).astimezone(UTC)

    def now_utc(self) -> datetime.datetime:
        return self._now

    def advance(self, delta: datetime.timedelta) -> datetime.datetime:
        """Move the clock forward (or back, with a negative delta)."""
        self._now = self._now + delta
        return self._now

    def set(self, moment: datetime.datetime) -> datetime.datetime:
        """Jump to an absolute instant."""
        self._now = _require_aware(moment).astimezone(UTC)
        return self._now

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"FrozenClock({self._now.isoformat()})"


def _require_aware(moment: datetime.datetime) -> datetime.datetime:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(
            "clock moments must be timezone-aware; a naive datetime would be "
            "guessed at, and the guess is only ever wrong across a DST change"
        )
    return moment


# --- local-time helpers ------------------------------------------------
#
# Everything that needs the user's wall clock goes through these, so
# there is exactly one place that knows local time is
# ZoneInfo(user_state.timezone).


def zone(timezone: str) -> ZoneInfo:
    """The tzinfo for an IANA name, e.g. 'Europe/Paris'."""
    return ZoneInfo(timezone)


def to_local(moment: datetime.datetime, timezone: str) -> datetime.datetime:
    """Render an aware instant in the user's zone."""
    return _require_aware(moment).astimezone(zone(timezone))


def now_local(clock: Clock, timezone: str) -> datetime.datetime:
    """The current instant, in the user's zone."""
    return to_local(clock.now_utc(), timezone)


def local_date(clock: Clock, timezone: str) -> datetime.date:
    """Today's calendar date in the user's zone."""
    return now_local(clock, timezone).date()


def local_date_of(moment: datetime.datetime, timezone: str) -> datetime.date:
    """The calendar date a past instant fell on, in the user's zone."""
    return to_local(moment, timezone).date()


def local_time_of_day(clock: Clock, timezone: str) -> datetime.time:
    """The current wall-clock time in the user's zone, seconds included."""
    return now_local(clock, timezone).time()


def combine_local(
    day: datetime.date, time_of_day: datetime.time, timezone: str
) -> datetime.datetime:
    """The instant at which the user's wall clock reads `time_of_day` on `day`.

    Returned as UTC, so callers compare instants and never wall clocks.

    Both DST edge cases are resolved by ZoneInfo's default `fold=0`,
    and both resolutions are deliberate:

    - **Repeated local time** (Europe/Paris 2026-10-25, 02:00-03:00
      happens twice): the *first* occurrence wins, so a fixed intent
      fires at the earliest instant matching its wall clock. The send
      grace window covers the rest of the doubled hour, and the
      `(kind, local_date, bucket)` unique constraint means the second
      occurrence cannot produce a second message.
    - **Skipped local time** (Europe/Paris 2027-03-28, 02:00-03:00 does
      not exist): the result normalizes forward -- a 02:30 target lands
      at the instant whose local reading is 03:30. A message scheduled
      into the gap is sent just after the jump rather than lost.

    Neither case can reach MORNING_TIME or EVENING_TIME at their
    defaults, but both are reachable through config, and both are
    pinned by tests/test_clock.py.
    """
    tz = zone(timezone)
    naive = datetime.datetime.combine(day, time_of_day)
    return naive.replace(tzinfo=tz).astimezone(UTC)


def local_target(
    clock: Clock, timezone: str, time_of_day: datetime.time
) -> datetime.datetime:
    """Today's instance of a wall-clock time, as a UTC instant."""
    return combine_local(local_date(clock, timezone), time_of_day, timezone)


def next_local_midnight(clock: Clock, timezone: str) -> datetime.datetime:
    """The next local midnight, as a UTC instant.

    Used to defer work until the daily budget resets. `day + 1` at
    00:00 rather than "now + 24h": the budget is keyed on the local
    calendar date, which is 23 or 25 hours long twice a year.
    """
    tomorrow = local_date(clock, timezone) + datetime.timedelta(days=1)
    return combine_local(tomorrow, datetime.time(0, 0), timezone)


def within_window(
    moment_time: datetime.time, start: datetime.time, end: datetime.time
) -> bool:
    """Is a wall-clock time inside [start, end), wrapping past midnight?

    Quiet hours are 22:30-08:00, i.e. the window spans midnight, so a
    naive `start <= t < end` is false for every instant in it. Compared
    as wall clocks rather than instants on purpose: "nothing between
    half ten at night and eight in the morning" is a statement about
    the user's clock face, and stays true on both DST days without any
    special handling.
    """
    if start == end:
        return False
    if start < end:
        return start <= moment_time < end
    return moment_time >= start or moment_time < end
