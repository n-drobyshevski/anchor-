"""Deletions from the vault: forgetting facts whose files vanished (plan section 7.2).

Called once per pass, in `sync` mode only, with the pre-ingest `absent`
snapshot (tracked fact/journal rows whose path is not in the manifest,
minus whatever ingest claimed as a rename this pass -- app/vault/
sync.py's job, not this module's).

**Grace and warmup, nothing else.** A file counts as deleted only once
it has been missing for `limits.DELETE_GRACE_S` *and* `ob` has been
running continuously for `limits.SYNC_WARMUP_S`min -- both computed
from `Clock` and the stored `missing_since`/`ob_running_since`, never
from a live clock read mid-pass, so a test can freeze time and reason
about the exact boundary.

**The rolling-hour cap is one hold for the whole batch,** not one per
file: `vault_status.forgets_window` remembers every timestamp a vault
forget actually happened at (pruned to the window on each pass), and if
this pass's candidates would push the count over
`limits.MASS_DELETE_MAX`, all of them wait behind a single
`mass_delete` hold instead of some going through and others not.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import memory
from app.core.clock import Clock
from app.db.models import VaultFile, VaultHold, VaultStatus
from app.vault import errors, holds, limits

logger = logging.getLogger(__name__)


def _parse(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value)


async def _has_pending_mass_delete(session: AsyncSession) -> bool:
    row = (
        await session.execute(
            select(VaultHold.id)
            .where(VaultHold.kind == holds.MASS_DELETE, VaultHold.status == holds.PENDING)
            .limit(1)
        )
    ).first()
    return row is not None


async def process_deletions(
    session: AsyncSession,
    *,
    absent: list[VaultFile],
    clock: Clock,
    ob_running_since: datetime.datetime | None,
    result,
) -> None:
    now = clock.now_utc()
    status = (await session.execute(select(VaultStatus))).scalar_one_or_none()
    forgets_window = list(status.forgets_window) if status is not None else []
    cutoff = now - limits.MASS_DELETE_WINDOW
    forgets_window = [t for t in forgets_window if _parse(t) > cutoff]

    warm = (
        ob_running_since is not None
        and (now - ob_running_since).total_seconds() >= limits.SYNC_WARMUP_S
    )

    candidates: list[VaultFile] = []
    for row in absent:
        if row.state in ("held", "restore"):
            continue
        if row.missing_since is None:
            row.missing_since = now
            continue
        missing_for = (now - row.missing_since).total_seconds()
        if missing_for < limits.DELETE_GRACE_S or not warm:
            continue
        if row.role == "journal":
            row.state = "dismissed"
            continue
        candidates.append(row)

    if not candidates:
        await session.commit()
        return

    if await _has_pending_mass_delete(session):
        # Further deletions wait behind the hold already pending.
        await session.commit()
        return

    if len(forgets_window) + len(candidates) > limits.MASS_DELETE_MAX:
        hold = await holds.open_mass_delete_hold(
            session, file_ids=[row.id for row in candidates], clock=clock
        )
        for row in candidates:
            row.state, row.hold_id = "held", hold.id
        result.held += len(candidates)
        result.new_hold_ids.append(hold.id)
        logger.info("vault mass delete hold opened", extra={"hold_id": hold.id, "count": len(candidates)})
    else:
        for row in candidates:
            if row.memory_id is None:
                await session.delete(row)
                continue
            outcome = await memory.forget_lineage(session, row.memory_id, source="vault", commit=False)
            if outcome == memory.FORGET_PROTECTED:
                row.state, row.reason = "restore", errors.PROTECTED
                row.missing_since, row.render_digest = None, None
                logger.info("vault forget protected", extra={"reason_code": errors.PROTECTED})
                continue
            forgets_window.append(now.isoformat())
            await session.delete(row)
            result.forgotten_facts += 1
            logger.info("vault fact forgotten", extra={"source": "vault"})

    stmt = (
        pg_insert(VaultStatus)
        .values(id=1, forgets_window=forgets_window)
        .on_conflict_do_update(index_elements=[VaultStatus.id], set_={"forgets_window": forgets_window})
    )
    await session.execute(stmt)
    await session.commit()
