"""Parsing `/quiet` (phase-3 plan section 10).

A pure module: text in, a duration or a sentinel out. No session, no
clock, no state -- so the whole grammar is a table-driven test and the
handler in app/tg/router.py is left with nothing to get wrong except
the wiring.

The grammar is `/quiet <N>m|h|d` and `/quiet off`, exactly as the plan
writes it. Two liberties beyond that, both in the direction of "do
what the user obviously meant":

- Russian suffixes (`м`, `ч`, `д`) are accepted alongside the Latin
  ones. Every other user-facing string in this bot is Russian, and a
  Cyrillic keyboard makes `30м` the natural thing to type.
- A bare number is minutes. `/quiet 30` has exactly one sensible
  reading, and rejecting it to teach a syntax lesson serves nobody.

**Over the maximum is clamped, not rejected.** `/quiet 30d` means "not
for a long time"; answering with a usage error would leave the bot
talking, which is the opposite of what was asked. It is clamped to
QUIET_MAX_DAYS and the reply states the real end time, so the clamp is
visible rather than silent.
"""

from __future__ import annotations

import datetime
import re

# Returned by parse() for `/quiet off`.
OFF = "off"

_OFF_WORDS = frozenset({"off", "выкл", "стоп", "0"})

# <number><unit>, unit optional (minutes by default).
_PATTERN = re.compile(r"^(\d{1,4})\s*([mhdмчд]?)$", re.IGNORECASE)

_UNITS: dict[str, datetime.timedelta] = {
    "": datetime.timedelta(minutes=1),
    "m": datetime.timedelta(minutes=1),
    "м": datetime.timedelta(minutes=1),
    "h": datetime.timedelta(hours=1),
    "ч": datetime.timedelta(hours=1),
    "d": datetime.timedelta(days=1),
    "д": datetime.timedelta(days=1),
}


def parse(raw: str) -> datetime.timedelta | str | None:
    """`timedelta` for a duration, `OFF` for off, `None` for nonsense.

    Three outcomes rather than an exception, because all three are
    ordinary user input and the handler answers each differently.
    """
    text = (raw or "").strip().lower()
    if not text:
        return None
    if text in _OFF_WORDS:
        return OFF

    match = _PATTERN.match(text)
    if match is None:
        return None

    amount = int(match.group(1))
    if amount <= 0:
        # "/quiet 0m" is off, not a zero-length silence.
        return OFF
    return _UNITS[match.group(2)] * amount


def clamp(duration: datetime.timedelta, max_days: int) -> datetime.timedelta:
    """Hold a duration to QUIET_MAX_DAYS. See the module docstring."""
    ceiling = datetime.timedelta(days=max_days)
    return min(duration, ceiling)
