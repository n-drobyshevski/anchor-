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
from app.core import memory
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

FIELDS = (DUE_ACTION, FOCUS_ON, RULE, STANDING_ORDER)

# Values that parse as "focus on". Anything else is off, which is the
# safe direction: focus is a pressure-increasing mode, so an ambiguous
# proposal must not silently switch it on.
_FOCUS_ON_VALUES = {"on", "вкл", "включить", "true", "1", "да"}


def parse_focus(value: str) -> bool:
    return value.strip().lower() in _FOCUS_ON_VALUES


async def get_pending(session: AsyncSession) -> Proposal | None:
    result = await session.execute(
        select(Proposal).where(Proposal.status == PENDING).order_by(Proposal.id.desc()).limit(1)
    )
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

    expired = await get_pending(session)
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


async def accept(session: AsyncSession, clock: Clock, proposal_id: int) -> Proposal | None:
    """Apply a pending proposal. Returns None if it is not pending.

    Returning None on a non-pending row is what makes the accept button
    idempotent: a replayed callback finds the row already decided and
    applies nothing a second time.

    This is the **only** code path in the repo that writes `due_action`
    or `focus_on` from a proposal, and it is reachable only from a
    button. `source="button"` on every state_change, per plan section 8.
    """
    proposal = await session.get(Proposal, proposal_id)
    if proposal is None or proposal.status != PENDING:
        return None

    now = clock.now_utc()

    if proposal.field == DUE_ACTION:
        await update_state(session, "due_action", proposal.value, "button")
        await update_state(session, "due_set_at", now, "button")
    elif proposal.field == FOCUS_ON:
        enabled = parse_focus(proposal.value)
        await update_state(session, "focus_on", enabled, "button")
        await update_state(session, "focus_since", now if enabled else None, "button")
    elif proposal.field == RULE:
        # source="user": the user pressed the button, so this is their
        # rule, not the extractor's. Plan section 8's apply table says
        # exactly this.
        await memory.write_memory(
            session, kind="rule", text=proposal.value, source="user"
        )

    proposal.status = ACCEPTED
    proposal.decided_at = now
    await session.commit()
    await session.refresh(proposal)
    logger.info("proposal accepted", extra={"proposal_id": proposal_id, "field": proposal.field})
    return proposal


async def reject(session: AsyncSession, clock: Clock, proposal_id: int) -> Proposal | None:
    """Mark a pending proposal rejected. Returns None if it is not pending."""
    proposal = await session.get(Proposal, proposal_id)
    if proposal is None or proposal.status != PENDING:
        return None
    proposal.status = REJECTED
    proposal.decided_at = clock.now_utc()
    await session.commit()
    await session.refresh(proposal)
    logger.info("proposal rejected", extra={"proposal_id": proposal_id, "field": proposal.field})
    return proposal
