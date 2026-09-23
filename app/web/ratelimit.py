"""In-memory rate limits for the web chat (web-chat plan track 2, design
section 6).

Every limit here is **global**, not per-IP: this is a single-user app
sitting behind Railway's proxy, where `X-Forwarded-For` is trivially
spoofable and there is exactly one legitimate caller anyway (design
section 6's own reasoning, restated in `WebRateLimiter`'s docstring).
Counters are plain in-memory sliding windows -- `_SlidingWindow` below
-- because there is one process, one event loop, and no requirement
that a limit survive a redeploy; a restart resetting every counter to
zero is the accepted tradeoff for not needing a second Postgres table
that this feature would otherwise be the only writer of.

`WebRateLimiter` takes a `Clock` (app/core/clock.py) rather than reading
the wall clock itself, so tests can drive it with a `FrozenClock`
exactly as every time-dependent module under app/core/ already does.

Five of the design's six limits live in `WebRateLimiter`. The sixth --
"SSE: 3 concurrent" -- is enforced by `WebHub.subscribe()` itself
(app/web/hub.py's `MAX_SUBSCRIBERS`/`TooManySubscribers`), not
duplicated here: a stream that outlives the request that opened it (a
slow client, a dropped connection the OS has not noticed) would make a
counter kept only in this module drift from reality, where the hub's
own subscriber dict cannot.
"""

from __future__ import annotations

import collections
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.db.models import TelegramUpdate

logger = logging.getLogger(__name__)

# design section 6's table.
PASSPHRASE_FAIL_LIMIT = 5
PASSPHRASE_FAIL_WINDOW_S = 15 * 60
PASSPHRASE_LOCKOUT_S = 15 * 60
CODE_SEND_LIMIT = 3
CODE_SEND_WINDOW_S = 15 * 60
SEND_PER_MINUTE_LIMIT = 12
SEND_PER_MINUTE_WINDOW_S = 60
SEND_PER_DAY_LIMIT = 300
SEND_PER_DAY_WINDOW_S = 24 * 60 * 60
MAX_PENDING_WEB_ROWS = 20
PRESS_PER_MINUTE_LIMIT = 30
PRESS_PER_MINUTE_WINDOW_S = 60

LOCKOUT_ALERT_TEXT = "5 неудачных входов в веб"


class _SlidingWindow:
    """A fixed-size, fixed-duration sliding window of event timestamps.

    `hit()` is the common case: "record one more event now, unless the
    limit is already full, in which case tell the caller how long until
    the oldest event ages out." `record()` is for a window this module
    also needs to *count into* without gating on it itself (the
    passphrase-failure window, which the lockout logic above it
    inspects directly rather than letting `hit()`'s pass/fail decide
    anything).
    """

    def __init__(self, limit: int, window_s: float) -> None:
        self.limit = limit
        self.window_s = window_s
        self._events: collections.deque[float] = collections.deque()

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_s
        while self._events and self._events[0] <= cutoff:
            self._events.popleft()

    def hit(self, now: float) -> float | None:
        """Record an event if under the limit. Returns None if allowed,
        else the number of seconds until the window has room again.
        """
        self._evict(now)
        if len(self._events) >= self.limit:
            return self._events[0] + self.window_s - now
        self._events.append(now)
        return None

    def record(self, now: float) -> int:
        """Unconditionally record an event; returns the count now in-window."""
        self._evict(now)
        self._events.append(now)
        return len(self._events)

    def clear(self) -> None:
        self._events.clear()


class WebRateLimiter:
    """One instance per process, held on `app["web_rate_limiter"]`
    (app/web/routes.py's `setup_web`).
    """

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._passphrase_fail = _SlidingWindow(PASSPHRASE_FAIL_LIMIT, PASSPHRASE_FAIL_WINDOW_S)
        self._code_send = _SlidingWindow(CODE_SEND_LIMIT, CODE_SEND_WINDOW_S)
        self._send_minute = _SlidingWindow(SEND_PER_MINUTE_LIMIT, SEND_PER_MINUTE_WINDOW_S)
        self._send_day = _SlidingWindow(SEND_PER_DAY_LIMIT, SEND_PER_DAY_WINDOW_S)
        self._press_minute = _SlidingWindow(PRESS_PER_MINUTE_LIMIT, PRESS_PER_MINUTE_WINDOW_S)
        self._lockout_until: float | None = None

    def _now(self) -> float:
        return self._clock.now_utc().timestamp()

    # --- passphrase: 5 failures / 15 min -> a 15 min lockout ---

    def check_passphrase_lockout(self) -> float | None:
        """None if not locked out; otherwise seconds remaining."""
        if self._lockout_until is None:
            return None
        now = self._now()
        if now >= self._lockout_until:
            self._lockout_until = None
            return None
        return self._lockout_until - now

    def record_passphrase_failure(self) -> bool:
        """Record one failed passphrase attempt.

        Returns True iff this failure is the one that just triggered a
        *new* lockout (so the caller sends the Telegram alert exactly
        once per lockout, not once per subsequent 403 while already
        locked out).
        """
        now = self._now()
        count = self._passphrase_fail.record(now)
        if count >= PASSPHRASE_FAIL_LIMIT:
            self._lockout_until = now + PASSPHRASE_LOCKOUT_S
            self._passphrase_fail.clear()
            return True
        return False

    def record_passphrase_success(self) -> None:
        self._passphrase_fail.clear()

    # --- Telegram code sends: 3 / 15 min ---

    def check_code_send(self) -> float | None:
        return self._code_send.hit(self._now())

    # --- POST /api/send: 12/min, 300/day (pending backlog is a DB check) ---

    def check_send(self) -> float | None:
        now = self._now()
        retry = self._send_minute.hit(now)
        if retry is not None:
            return retry
        return self._send_day.hit(now)

    # --- POST /api/press: 30/min ---

    def check_press(self) -> float | None:
        return self._press_minute.hit(self._now())


async def pending_web_count(session: AsyncSession) -> int:
    """How many web-origin rows are still queued or in flight.

    Backs the "at most 20 pending web rows" limit (design section 6): a
    misbehaving or looping client can flood /api/send fast enough to
    build an unbounded backlog even under the 12/min rate limit if that
    were the only guard (12/min is a long-run average, not a queue-depth
    cap), and every one of those rows eventually reaches the LLM.
    'processing' counts too, not just 'pending': a row genuinely being
    worked by the single-concurrency claim loop is still outstanding
    work, not slack.
    """
    result = await session.execute(
        select(func.count())
        .select_from(TelegramUpdate)
        .where(TelegramUpdate.source == "web")
        .where(TelegramUpdate.status.in_(("pending", "processing")))
    )
    return result.scalar_one()
