"""One set of rules for /due, /focus, /quiet, /tz and proposal expiry,
shared by Telegram and the web panels (W2 plan section 3, "Backend: one
set of rules for both transports").

Every function here is the business half of a `app/tg/router.py`
handler with the reply text, the `Message`/`CommandObject` parsing and
the idempotent-reply plumbing stripped out -- session in, `UserState`
(or nothing) out. `app/tg/router.py`'s handlers now call these and keep
doing their own argument parsing and canned replies, so Telegram's
behaviour is byte-identical before and after this module existed
(tests/test_state_commands.py, tests/test_quiet_tz.py). `app/web/
panels/state.py` calls the same functions with `source="web"`, which is
the whole point: one set of rules, not a second implementation that can
drift from the first.

None of these functions validate anything Telegram's own handlers did
not already validate inline (empty text, an on/off flag, an already-
clamped `until`) -- **except** `set_timezone`, which owns the `ZoneInfo`
check itself because that check was already the whole of the Telegram
handler's own validation and both transports must reject the exact same
set of strings. Web-specific validation that has no Telegram parallel
(the due-text length cap, an ISO timestamp's shape, "until" being in
the past) lives in `app/web/panels/state.py`, not here, so this module
never gains a rule Telegram was never asked to enforce.
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import obligations
from app.core.clock import Clock
from app.core.outbound import cancel_outbound
from app.core.proposal import EXPIRED, get_pending
from app.core.state import Source, update_state
from app.db.models import Proposal, UserState

# W2's due-text cap (roadmap section 4, StateDTO.limits.due_max_len).
# Telegram's /due has never had one -- command.args can be arbitrarily
# long, and set_due() below still accepts whatever it is handed -- so
# this constant is consulted only by app/web/panels/state.py before it
# ever calls set_due(), matching app/tg/memory.py's MEMORY_TEXT_MAX in
# shape (a cap the web validates against, not one the core write
# enforces) but not in value: the due action is a short label ("сдать
# отчёт до пятницы"), not a note.
DUE_ACTION_MAX_LEN = 300


class InvalidTimezone(ValueError):
    """Raised by set_timezone() for any string ZoneInfo will not accept."""


async def set_due(
    session: AsyncSession, clock: Clock, text: str | None, source: Source
) -> UserState:
    """due_action + due_set_at (plan section 9's /due).

    Empty (after strip) clears both fields -- app/tg/router.py's /due
    handler has always treated a bare "/due" this way, and Telegram
    behaviour must not change under this refactor. Web callers reject
    an empty string *before* calling this (StateDTO's HTTP contract has
    no "clear" action), so this branch is Telegram-only in practice, but
    living here rather than duplicated keeps it that way structurally
    rather than by convention.
    """
    stripped = (text or "").strip()
    # Phase 5: the main action is also the open 'focus' debt. The two
    # move together here and in proposal.accept(), their only writers.
    await obligations.replace_focus(session, clock, stripped or None)
    if stripped:
        await update_state(session, "due_action", stripped, source)
        return await update_state(session, "due_set_at", clock.now_utc(), source)
    await update_state(session, "due_action", None, source)
    return await update_state(session, "due_set_at", None, source)


async def set_focus(
    session: AsyncSession, clock: Clock, enabled: bool, source: Source
) -> UserState:
    """focus_on + focus_since (plan section 9's /focus)."""
    await update_state(session, "focus_on", enabled, source)
    since = clock.now_utc() if enabled else None
    return await update_state(session, "focus_since", since, source)


async def set_quiet(
    session: AsyncSession,
    clock: Clock,
    until: datetime.datetime | None,
    source: Source,
) -> UserState:
    """quiet_until, plus cancel_outbound() when a quiet period is set
    (plan section 9's /quiet).

    `until=None` is "/quiet off": only the field is cleared, nothing is
    cancelled -- turning quiet *off* is a reason to let a planned
    message through, not a reason to revoke it (tests/test_quiet_tz.py's
    test_quiet_off_does_not_cancel_anything pins this for Telegram; nothing
    about the web transport changes that).

    Callers own clamping `until` to `settings.QUIET_MAX_DAYS` before
    calling this -- app/tg/router.py's /quiet already did that with
    `app.core.quiet.clamp`, and app/web/panels/state.py's POST
    /api/state/quiet does the equivalent for an absolute ISO timestamp.
    """
    state = await update_state(session, "quiet_until", until, source)
    if until is not None:
        await cancel_outbound(session, clock)
    return state


async def set_timezone(session: AsyncSession, tz: str, source: Source) -> UserState:
    """timezone, after validating `tz` against the real IANA database
    (plan section 9's /tz).

    Raises InvalidTimezone for anything ZoneInfo itself would reject --
    matching app/tg/router.py's original reasoning verbatim: the tz
    database is the only authority on what is a real zone, and an
    unknown-but-plausible name is exactly the input that would otherwise
    be accepted and then crash every local-time computation afterwards.
    No state is touched when this raises.
    """
    try:
        ZoneInfo(tz)
    except Exception as exc:  # noqa: BLE001 - ZoneInfoNotFoundError, ValueError, OSError
        raise InvalidTimezone(tz) from exc
    return await update_state(session, "timezone", tz, source)


async def expire_proposal_for(
    session: AsyncSession, clock: Clock, field: str
) -> Proposal | None:
    """Expire the pending proposal for `field`, if there is one.

    The core half of app/tg/router.py's `_expire_proposal_for`: "a
    direct command outranks an outstanding proposal for the same
    field", so a live "Принять" is never left sitting there ready to
    overwrite what the user (on either transport) just typed. Returns
    the now-expired row, or None when there was nothing to expire, so
    the caller (Telegram or web) knows whether it has a Telegram message
    whose buttons still need retiring.

    `for_update=True` locks the row before checking it, so this cannot
    expire a proposal that a concurrent `accept()`/`reject()` (Telegram
    racing the web panel) has, at this exact moment, already decided --
    see app/core/proposal.py's `_lock_pending`/`get_pending` docstrings.
    """
    pending = await get_pending(session, for_update=True)
    if pending is None or pending.field != field:
        return None
    pending.status = EXPIRED
    pending.decided_at = clock.now_utc()
    await session.commit()
    await session.refresh(pending)
    return pending
