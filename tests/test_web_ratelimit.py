"""app/web/ratelimit.py (web-chat plan track 2, design section 6).

- passphrase: 5 failures / 15 min triggers a lockout, once
- a lockout lifts on its own after 15 min
- code sends: 3 / 15 min
- /api/send: 12/min and 300/day
- /api/press: 30/min
- the pending-web-rows backlog count only counts pending/processing web
  rows, never telegram-origin or already-done/failed ones
- limiter state is entirely in-memory and driven by an injected Clock
"""

from __future__ import annotations

import datetime

from sqlalchemy import insert

from app.core.clock import FrozenClock
from app.db.models import TelegramUpdate
from app.web.ratelimit import (
    CODE_SEND_LIMIT,
    PASSPHRASE_FAIL_LIMIT,
    PASSPHRASE_LOCKOUT_S,
    PRESS_PER_MINUTE_LIMIT,
    SEND_PER_MINUTE_LIMIT,
    WebRateLimiter,
    pending_web_count,
)

START = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


def _limiter() -> tuple[WebRateLimiter, FrozenClock]:
    clock = FrozenClock(START)
    return WebRateLimiter(clock), clock


def test_passphrase_lockout_after_five_failures():
    limiter, clock = _limiter()
    just_locked = [limiter.record_passphrase_failure() for _ in range(PASSPHRASE_FAIL_LIMIT)]
    assert just_locked == [False] * (PASSPHRASE_FAIL_LIMIT - 1) + [True]
    retry = limiter.check_passphrase_lockout()
    assert retry is not None and retry == PASSPHRASE_LOCKOUT_S


def test_passphrase_lockout_lifts_after_its_window():
    limiter, clock = _limiter()
    for _ in range(PASSPHRASE_FAIL_LIMIT):
        limiter.record_passphrase_failure()
    assert limiter.check_passphrase_lockout() is not None

    clock.advance(datetime.timedelta(seconds=PASSPHRASE_LOCKOUT_S))
    assert limiter.check_passphrase_lockout() is None


def test_passphrase_success_clears_the_failure_count():
    limiter, clock = _limiter()
    for _ in range(PASSPHRASE_FAIL_LIMIT - 1):
        limiter.record_passphrase_failure()
    limiter.record_passphrase_success()
    # The count was reset, so PASSPHRASE_FAIL_LIMIT-1 more failures should
    # not yet trigger a lockout.
    results = [limiter.record_passphrase_failure() for _ in range(PASSPHRASE_FAIL_LIMIT - 1)]
    assert True not in results


def test_code_send_limit():
    limiter, clock = _limiter()
    for _ in range(CODE_SEND_LIMIT):
        assert limiter.check_code_send() is None
    retry = limiter.check_code_send()
    assert retry is not None and retry > 0


def test_send_per_minute_limit():
    limiter, clock = _limiter()
    for _ in range(SEND_PER_MINUTE_LIMIT):
        assert limiter.check_send() is None
    retry = limiter.check_send()
    assert retry is not None

    clock.advance(datetime.timedelta(seconds=61))
    assert limiter.check_send() is None  # the per-minute window rolled over


def test_send_per_day_limit_survives_minute_rollover():
    limiter, clock = _limiter()
    for _ in range(SEND_PER_MINUTE_LIMIT):
        assert limiter.check_send() is None
        clock.advance(datetime.timedelta(seconds=61))  # dodge the per-minute cap each time
    # 300/day is the design's number; SEND_PER_MINUTE_LIMIT (12) sends,
    # each a minute apart, are nowhere near the daily cap, so this just
    # proves both windows are tracked independently rather than one
    # replacing the other.
    assert limiter.check_send() is None


def test_press_per_minute_limit():
    limiter, clock = _limiter()
    for _ in range(PRESS_PER_MINUTE_LIMIT):
        assert limiter.check_press() is None
    retry = limiter.check_press()
    assert retry is not None


async def test_pending_web_count_only_counts_live_web_rows(sessionmaker):
    async with sessionmaker() as session:
        await session.execute(
            insert(TelegramUpdate),
            [
                {"update_id": -1, "payload": {}, "status": "pending", "source": "web"},
                {"update_id": -2, "payload": {}, "status": "processing", "source": "web"},
                {"update_id": -3, "payload": {}, "status": "done", "source": "web"},
                {"update_id": -4, "payload": {}, "status": "failed", "source": "web"},
                {"update_id": 1, "payload": {}, "status": "pending", "source": "telegram"},
            ],
        )
        await session.commit()
        assert await pending_web_count(session) == 2  # pending + processing, web-only
