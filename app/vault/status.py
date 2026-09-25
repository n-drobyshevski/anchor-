"""Is the vault service up? The probe behind `/vault` and `/state` (plan section 8).

In `status` mode no sync pass runs, so nothing else would ever notice
the vault service going away. The two commands therefore ask vaultd
directly (`GET /v1/status`, one short request) and record the answer in
`vault_status`: `last_ok_at` and `ob_running_since` on success,
`last_unavailable_at` on failure. That is what lets `/state` say «нет
связи с 14:05» -- since when, not just whether.

`off` makes no request and writes nothing: the kill switch means the
bot does not so much as open a socket to the vault service.

5a treated `mirror` and `sync` like `status`; 5b implements `mirror`,
and `sync` behaves exactly like it until 5c (docs/decisions.md).
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.clock import Clock
from app.db.models import Job, VaultFile, VaultStatus
from app.vault import errors
from app.vault.client import VaultClient
from app.vault.errors import VaultError
from app.vault.kinds import VAULT_PURGE

logger = logging.getLogger(__name__)

OFF = "off"
OK = "ok"
STOPPED = "stopped"
UNREACHABLE = "unreachable"
UNAUTHORIZED = "unauthorized"

# The modes this build implements. `sync` arrives in 5c; until then it
# behaves exactly like `mirror` (docs/decisions.md).
IMPLEMENTED_MODES = ("off", "status", "mirror")

ClientFactory = Callable[[Settings], VaultClient]


@dataclass(frozen=True)
class Health:
    state: str
    running_since: datetime.datetime | None = None
    restarts: int | None = None
    last_exit_code: int | None = None
    last_ok_at: datetime.datetime | None = None


async def record_status(session: AsyncSession, **values) -> VaultStatus:
    stmt = (
        pg_insert(VaultStatus)
        .values(id=1, **values)
        .on_conflict_do_update(index_elements=[VaultStatus.id], set_=values)
        .returning(VaultStatus)
    )
    row = (await session.execute(stmt)).scalar_one()
    await session.commit()
    return row


async def probe(
    session: AsyncSession,
    settings: Settings,
    clock: Clock,
    client_factory: ClientFactory = VaultClient.from_settings,
) -> Health:
    if settings.VAULT_MODE == OFF:
        return Health(OFF)
    now = clock.now_utc()
    try:
        service = await client_factory(settings).status()
    except VaultError as exc:
        row = await record_status(session, last_unavailable_at=now)
        logger.warning("vault unavailable", extra={"error_code": exc.code})
        state = UNAUTHORIZED if exc.code == errors.UNAUTHORIZED else UNREACHABLE
        return Health(state, last_ok_at=row.last_ok_at)
    await record_status(session, last_ok_at=now, ob_running_since=service.running_since)
    return Health(
        OK if service.sync_running else STOPPED,
        running_since=service.running_since,
        restarts=service.restarts,
        last_exit_code=service.last_exit_code,
        last_ok_at=now,
    )


async def count_fact_files(session: AsyncSession) -> int:
    """Fact files Anchor has written and still tracks, for /vault."""
    result = await session.execute(
        select(func.count())
        .select_from(VaultFile)
        .where(VaultFile.role == "fact", VaultFile.disk_sha256.is_not(None))
    )
    return result.scalar_one()


async def purge_pending(session: AsyncSession) -> bool:
    """A /delete whose vault purge has not gone through yet (plan 8, /state)."""
    row = await session.execute(
        select(Job.id)
        .where(Job.kind == VAULT_PURGE, Job.status.in_(("pending", "processing")))
        .limit(1)
    )
    return row.first() is not None
