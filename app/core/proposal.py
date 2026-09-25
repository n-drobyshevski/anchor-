"""Proposals: the gate between model output and sensitive state (plan section 8).

This module is why the extractor is safe. Nothing the model produces
reaches `user_state` or a rule memory directly; it lands here as a
`pending` row, and only `accept()` -- reachable solely from a button
press -- applies it.

That separation is structural, not conventional. app/core/extract.py
imports `create()` and nothing else from this module: it has no name in
scope that can change a sensitive field. `accept()` lives here with the
Telegram layer as its only caller. tests/test_extract.py asserts both
halves of that.

**One pending proposal at a time** (plan section 8). A new proposal
expires the outstanding one and returns it, so the caller can edit its
now-stale buttons away -- an expired decision that still looks
answerable is worse than no buttons at all.
"""

from __future__ import annotations

import logging

from sqlalchemy import select, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core import memory, obligations
from app.core.state import update_state
from app.db.models import Proposal

__all__ = [
    "Proposal",
    "PENDING",
    "ACCEPTED",
    "REJECTED",
    "EXPIRED",
    "DUE_ACTION",
    "FOCUS_ON",
    "RULE",
    "STANDING_ORDER",
    "OBLIGATION",
    "FIELDS",
    "accept",
    "create",
    "get_pending",
    "parse_focus",
    "reject",
    "set_message_id",
]

logger = logging.getLogger(__name__)

PENDING = "pending"
ACCEPTED = "accepted"
REJECTED = "rejected"
EXPIRED = "expired"

DUE_ACTION = "due_action"
FOCUS_ON = "focus_on"
RULE = "rule"
# 5c: widened alongside ck_proposal_field, for schema parity only -- no
# Proposal row is ever actually inserted with this field. See app/core/
# extract.py's _apply, which routes a standing_order item to
# app/core/orders.propose() instead, and that module's own docstring on
# why the negotiation needs its own row shape rather than this table's.
STANDING_ORDER = "standing_order"

# Phase 5 (spec 2026-09-25): a debt the user promised in chat. The
# extractor may propose one; only accept() below opens it.
OBLIGATION = "obligation"
FIELDS = (DUE_ACTION, FOCUS_ON, RULE, STANDING_ORDER, OBLIGATION)

# Values that parse as "focus on". Anything else is off, which is the
# safe direction: focus is a pressure-increasing mode, so an ambiguous
# proposal must not silently switch it on.
_FOCUS_ON_VALUES = {"on", "вкл", "включить", "true", "1", "да"}


def parse_focus(value: str) -> bool:
    return value.strip().lower() in _FOCUS_ON_VALUES


async def get_pending(session: AsyncSession, *, for_update: bool = False) -> Proposal | None:
    """The current pending proposal, if any.

    `for_update=True` locks the row (`SELECT ... FOR UPDATE`) and
    forces the returned object's attributes to reflect exactly what
    that locked read saw (`populate_existing`, since otherwise a row
    already present in this session's identity map -- e.g. read once
    earlier in the same request -- would keep its old, possibly stale,
    in-memory attributes even though the query itself re-read the row
    under lock). Callers that are about to expire this row -- `create()`
    below and `app/core/commands.py`'s `expire_proposal_for` -- pass
    this so a concurrent decision (accept/reject, from Telegram or the
    web) cannot land between this read and that write; every other
    caller (a plain GET/display) leaves it False, since locking a row
    nobody here is about to change would only serialize reads against
    writes for no reason.
    """
    stmt = select(Proposal).where(Proposal.status == PENDING).order_by(Proposal.id.desc()).limit(1)
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    result = await session.execute(stmt)
    return result.scalars().first()


async def create(
    session: AsyncSession, clock: Clock, *, field: str, value: str, reason: str | None
) -> tuple[Proposal, Proposal | None]:
    """Insert a pending proposal, expiring any outstanding one.

    Returns (new, expired_or_None). The caller is responsible for
    editing the expired proposal's buttons away -- this module does not
    import anything Telegram-shaped.
    """
    if field not in FIELDS:
        raise ValueError(f"unknown proposal field: {field}")

    expired = await get_pending(session, for_update=True)
    if expired is not None:
        expired.status = EXPIRED
        expired.decided_at = clock.now_utc()

    proposal = Proposal(field=field, value=value, reason=reason)
    session.add(proposal)
    await session.commit()
    await session.refresh(proposal)
    logger.info(
        "proposal created",
        extra={"proposal_id": proposal.id, "field": field, "expired_id": expired.id if expired else None},
    )
    return proposal, expired


async def set_message_id(session: AsyncSession, proposal_id: int, message_id: int) -> None:
    await session.execute(
        sql_update(Proposal).where(Proposal.id == proposal_id).values(tg_message_id=message_id)
    )
    await session.commit()


async def _lock_pending(session: AsyncSession, proposal_id: int) -> Proposal | None:
    """Lock `proposal_id`'s row (`SELECT ... FOR UPDATE`) and return it
    only if it is still pending; `None` otherwise (not found, or
    decided/expired by someone else).

    Telegram's callback handler and the web panel
    (app/web/panels/proposals.py) are two independent, concurrent ways
    to decide the same proposal -- a double accept, an accept racing a
    reject, or an accept racing `create()`'s own expiry (which now also
    locks, via `get_pending(for_update=True)`) must not all be applied.
    `session.get()` alone cannot prevent that: it is a plain identity-
    map read with no lock, and it can even return a *stale* cached
    object if this session already loaded this row earlier (a caller's
    own pre-check, say) -- that is why this issues an explicit `SELECT
    ... FOR UPDATE` with `populate_existing` instead of `session.get()`,
    exactly like `get_pending(for_update=True)` above. Under Postgres's
    default READ COMMITTED isolation, a `FOR UPDATE` select blocks
    behind any other transaction's uncommitted lock on the same row and
    then re-reads it post-commit, so the status this function returns
    is never a value a concurrent decision has already superseded.
    """
    result = await session.execute(
        select(Proposal)
        .where(Proposal.id == proposal_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    proposal = result.scalar_one_or_none()
    if proposal is None or proposal.status != PENDING:
        return None
    return proposal


async def accept(session: AsyncSession, clock: Clock, proposal_id: int) -> Proposal | None:
    """Apply a pending proposal. Returns None if it is not pending.

    Returning None on a non-pending row is what makes the accept button
    idempotent: a replayed callback finds the row already decided and
    applies nothing a second time -- and, since `_lock_pending` takes a
    row lock first, that is also true of two *concurrent* decisions
    (Telegram racing the web panel, or two web tabs), not only
    sequential ones: only the first to acquire the lock ever applies
    anything.

    This is the **only** code path in the repo that writes `due_action`
    or `focus_on` from a proposal, and it is reachable only from a
    button. `source="button"` on every state_change, per plan section 8.
    """
    proposal = await _lock_pending(session, proposal_id)
    if proposal is None:
        return None

    now = clock.now_utc()

    # Set *before* the field-specific write below, not after: `update_
    # state`/`write_memory` each commit internally (their own docstrings
    # say so), and `_lock_pending`'s row lock is released the instant
    # any commit on this session happens, not only the explicit one at
    # the end of this function. Setting `status`/`decided_at` first
    # means that very first commit already persists ACCEPTED, so a
    # concurrent decision that was blocked on this row's lock -- the
    # whole reason `_lock_pending` exists -- re-reads a row that is
    # already decided the instant it can see it at all, instead of a
    # window where the lock is free but the status still reads PENDING
    # (which let a concurrent accept or reject apply a second, wrong
    # decision on top of this one; verified by reproducing it with the
    # assignment left where an unlocked version of this function had
    # it, after the field-specific write).
    proposal.status = ACCEPTED
    proposal.decided_at = now

    if proposal.field == DUE_ACTION:
        await update_state(session, "due_action", proposal.value, "button")
        await update_state(session, "due_set_at", now, "button")
        # Phase 5: the main action is also the open 'focus' debt, in
        # step with app/core/commands.py's set_due().
        await obligations.replace_focus(session, clock, proposal.value)
    elif proposal.field == FOCUS_ON:
        enabled = parse_focus(proposal.value)
        await update_state(session, "focus_on", enabled, "button")
        await update_state(session, "focus_since", now if enabled else None, "button")
    elif proposal.field == OBLIGATION:
        # At the cap this opens nothing; app/tg/proposals.py checks the
        # cap before calling accept(), so a press at the cap leaves the
        # proposal pending instead of landing here.
        await obligations.open_(
            session, text=proposal.value, kind="promised", source="proposal"
        )
    elif proposal.field == RULE:
        # source="user": the user pressed the button, so this is their
        # rule, not the extractor's. Plan section 8's apply table says
        # exactly this.
        await memory.write_memory(
            session, kind="rule", text=proposal.value, source="user"
        )

    await session.commit()
    await session.refresh(proposal)
    logger.info("proposal accepted", extra={"proposal_id": proposal_id, "field": proposal.field})
    return proposal


async def reject(session: AsyncSession, clock: Clock, proposal_id: int) -> Proposal | None:
    """Mark a pending proposal rejected. Returns None if it is not
    pending -- including "not pending any more" to a decision that just
    won a race for the same row's lock; see `accept()`'s docstring and
    `_lock_pending` above.
    """
    proposal = await _lock_pending(session, proposal_id)
    if proposal is None:
        return None
    proposal.status = REJECTED
    proposal.decided_at = clock.now_utc()
    await session.commit()
    await session.refresh(proposal)
    logger.info("proposal rejected", extra={"proposal_id": proposal_id, "field": proposal.field})
    return proposal
