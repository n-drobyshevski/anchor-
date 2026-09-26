"""Vault holds: rule edits and mass deletions wait for a yes (plan section 8).

Two kinds. `mass_delete` bundles every fact that would otherwise be
forgotten past `limits.MASS_DELETE_MAX` in the rolling window into one
hold. `rule` gates a single fact whose `kind` is (or would become)
`rule` -- editing what Anchor is told to do is never silent.

`decide` and `expire_holds` are the only writers of `vault_hold.status`.
Both apply the same confirm/revert action; expiry always reverts
("anything short of a clean yes is a no, and the no is always the
direction where nothing is lost" -- plan section 8).

**Why no `hold_text` here.** The Telegram layer (phase C) builds the
Russian message from a hold's `payload` and, for `mass_delete`, the
number of `file_ids`; that formatting has no place in a module tests
pin to "no app.worker, no persona, no outbound" (tests/test_vault_
isolation.py). This module only ever touches `vault_hold` and
`vault_file`, plus `write_memory`/`set_pinned`/`forget_lineage` -- the
three functions the vault may change memory through.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import memory
from app.core.clock import Clock
from app.db.models import UserState, VaultFile, VaultHold
from app.vault import errors, limits

logger = logging.getLogger(__name__)

RULE = "rule"
MASS_DELETE = "mass_delete"

PENDING = "pending"
CONFIRMED = "confirmed"
REVERTED = "reverted"
EXPIRED = "expired"
STALE = "stale"
# Internal to _apply_rule only -- never written to vault_hold.status,
# whose CHECK constraint does not know this value. A rule-edit confirm
# that turns out to duplicate another active fact is *accepted*
# (hold.status stays "confirmed": the user's yes was real), but nothing
# is written to memory and the row is quarantined instead of applied or
# deleted.
_DUPLICATE = "duplicate"

# decide()'s outcomes.
CONFIRMED_RESULT = "confirmed"
REVERTED_RESULT = "reverted"
STALE_PRESS = "stale_press"  # the button itself: unknown id, not pending, wrong epoch
STALE_APPLY = "stale_apply"  # a fresh, pending rule-edit hold whose head moved
DUPLICATE_RESULT = "duplicate_fact"  # confirmed, but it duplicated another active fact


async def open_rule_hold(
    session: AsyncSession,
    *,
    file_id: int,
    kind: str,
    text: str,
    supersedes_id: int | None,
    clock: Clock,
) -> VaultHold:
    hold = VaultHold(
        kind=RULE,
        payload={"file_id": file_id, "kind": kind, "text": text, "supersedes_id": supersedes_id},
        created_at=clock.now_utc(),
    )
    session.add(hold)
    await session.flush()
    return hold


async def open_mass_delete_hold(
    session: AsyncSession, *, file_ids: list[int], clock: Clock
) -> VaultHold:
    hold = VaultHold(kind=MASS_DELETE, payload={"file_ids": list(file_ids)}, created_at=clock.now_utc())
    session.add(hold)
    await session.flush()
    return hold


@dataclass(frozen=True)
class DecideResult:
    outcome: str
    hold: VaultHold | None = None


async def _apply_mass_delete(session: AsyncSession, hold: VaultHold, *, confirm: bool) -> None:
    for file_id in hold.payload.get("file_ids", []):
        row = await session.get(VaultFile, file_id)
        if row is None:
            continue
        if confirm:
            if row.memory_id is None:
                await session.delete(row)
                continue
            outcome = await memory.forget_lineage(session, row.memory_id, source="vault", commit=False)
            if outcome == memory.FORGET_PROTECTED:
                row.state, row.reason = "restore", "protected"
                row.missing_since, row.render_digest, row.hold_id = None, None, None
                continue
            await session.delete(row)
        else:
            row.state, row.hold_id = "restore", None
            row.missing_since, row.render_digest = None, None


async def _apply_rule(session: AsyncSession, hold: VaultHold, *, confirm: bool) -> str:
    """Applies the hold's action. Returns the resulting hold status."""
    payload = hold.payload
    row = await session.get(VaultFile, payload["file_id"])
    if not confirm:
        if row is not None:
            if payload.get("supersedes_id") is None:
                # A new file the user never got to keep: let the render
                # pass's usual "memory is gone" cleanup delete it.
                row.state, row.hold_id = "ok", None
            else:
                # An edit to an existing fact: overwrite it from the DB.
                row.state, row.hold_id, row.render_digest = "ok", None, None
        return REVERTED

    supersedes_id = payload.get("supersedes_id")
    if supersedes_id is not None:
        current_head = row.memory_id if row is not None else None
        if current_head != supersedes_id:
            if row is not None:
                row.hold_id = None
                row.state = "ok" if row.state == "held" else row.state
            return STALE
        written = await memory.write_memory(
            session,
            kind=payload["kind"],
            text=payload["text"],
            source="vault",
            supersedes_id=supersedes_id,
            commit=False,
        )
        if written is None:
            # Bug fix: silently keeping the old text (or, for a new
            # file, deleting it via the render pass's "memory is gone"
            # cleanup) both discard the user's confirmed edit without a
            # trace. Quarantine instead: nothing is deleted, and /vault
            # can say why.
            if row is not None:
                row.state, row.reason, row.hold_id = "quarantined", errors.DUPLICATE_FACT, None
            return _DUPLICATE
        if row is not None:
            row.memory_id = written.id
    else:
        written = await memory.write_memory(
            session, kind=payload["kind"], text=payload["text"], source="vault", commit=False
        )
        if written is None:
            if row is not None:
                row.state, row.reason, row.hold_id = "quarantined", errors.DUPLICATE_FACT, None
            return _DUPLICATE
        if row is not None:
            row.memory_id = written.id
    if row is not None:
        row.state, row.hold_id = "ok", None
    return CONFIRMED


async def decide(
    session: AsyncSession, hold_id: int, epoch: str, confirm: bool, clock: Clock
) -> DecideResult:
    """Apply a Telegram button press. See the module docstring for the outcomes."""
    hold = await session.get(VaultHold, hold_id)
    if hold is None or hold.status != PENDING:
        return DecideResult(STALE_PRESS)
    state = (await session.execute(select(UserState))).scalar_one()
    if epoch != state.vault_epoch:
        return DecideResult(STALE_PRESS)

    if hold.kind == MASS_DELETE:
        await _apply_mass_delete(session, hold, confirm=confirm)
        hold.status = CONFIRMED_RESULT if confirm else REVERTED_RESULT
        hold.decided_at = clock.now_utc()
        await session.commit()
        logger.info("vault hold decided", extra={"hold_id": hold_id, "event": hold.status})
        return DecideResult(hold.status, hold)

    status = await _apply_rule(session, hold, confirm=confirm)
    hold.status = CONFIRMED if status == _DUPLICATE else status
    hold.decided_at = clock.now_utc()
    await session.commit()
    outcome = {
        CONFIRMED: CONFIRMED_RESULT,
        REVERTED: REVERTED_RESULT,
        STALE: STALE_APPLY,
        _DUPLICATE: DUPLICATE_RESULT,
    }[status]
    logger.info("vault hold decided", extra={"hold_id": hold_id, "event": outcome})
    return DecideResult(outcome, hold)


async def expire_holds(session: AsyncSession, clock: Clock) -> list[int]:
    """Marks every hold older than limits.HOLD_TTL_DAYS `expired`, reverting it.

    Returns the ids expired, for a caller that wants to know.
    """
    cutoff = clock.now_utc() - datetime.timedelta(days=limits.HOLD_TTL_DAYS)
    rows = (
        await session.execute(
            select(VaultHold).where(VaultHold.status == PENDING, VaultHold.created_at < cutoff)
        )
    ).scalars().all()
    expired_ids = []
    for hold in rows:
        if hold.kind == MASS_DELETE:
            await _apply_mass_delete(session, hold, confirm=False)
        else:
            await _apply_rule(session, hold, confirm=False)
        hold.status = EXPIRED
        hold.decided_at = clock.now_utc()
        expired_ids.append(hold.id)
        logger.info("vault hold expired", extra={"hold_id": hold.id})
    if rows:
        await session.commit()
    return expired_ids


async def pending_unsent(session: AsyncSession) -> list[VaultHold]:
    """Pending holds with no Telegram message sent yet (phase C's queue)."""
    return list(
        (
            await session.execute(
                select(VaultHold).where(VaultHold.status == PENDING, VaultHold.tg_message_id.is_(None))
            )
        ).scalars()
    )


async def mark_sent(session: AsyncSession, hold_id: int, message_id: int) -> None:
    hold = await session.get(VaultHold, hold_id)
    if hold is not None:
        hold.tg_message_id = message_id
        await session.commit()
