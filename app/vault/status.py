"""Is the vault service up? The probe behind `/vault` and `/state` (plan section 8).

In `status` mode no sync pass runs, so nothing else would ever notice
the vault service going away. The two commands therefore ask vaultd
directly (`GET /v1/status`, one short request) and record the answer in
`vault_status`: `last_ok_at` and `ob_running_since` on success,
`last_unavailable_at` on failure. That is what lets `/state` say «нет
связи с 14:05» -- since when, not just whether.

`off` makes no request and writes nothing: the kill switch means the
bot does not so much as open a socket to the vault service.

5a treats `mirror` and `sync` exactly like `status` (docs/decisions.md):
they are accepted so that a config set ahead of a deploy cannot take
chat down, and until 5b/5c ship they do nothing more than this probe.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import Callable

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.clock import Clock
from app.db.models import VaultStatus
from app.vault import errors
from app.vault.client import VaultClient
from app.vault.errors import VaultError

logger = logging.getLogger(__name__)

OFF = "off"
OK = "ok"
STOPPED = "stopped"
UNREACHABLE = "unreachable"
UNAUTHORIZED = "unauthorized"

# The modes this build implements. `mirror` and `sync` arrive in 5b/5c.
IMPLEMENTED_MODES = ("off", "status")

ClientFactory = Callable[[Settings], VaultClient]


@dataclass(frozen=True)
class Health:
    state: str
    running_since: datetime.datetime | None = None
    restarts: int | None = None
    last_exit_code: int | None = None
    last_ok_at: datetime.datetime | None = None


async def _record(session: AsyncSession, **values) -> VaultStatus:
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
        row = await _record(session, last_unavailable_at=now)
        logger.warning("vault unavailable", extra={"error_code": exc.code})
        state = UNAUTHORIZED if exc.code == errors.UNAUTHORIZED else UNREACHABLE
        return Health(state, last_ok_at=row.last_ok_at)
    await _record(session, last_ok_at=now, ob_running_since=service.running_since)
    return Health(
        OK if service.sync_running else STOPPED,
        running_since=service.running_since,
        restarts=service.restarts,
        last_exit_code=service.last_exit_code,
        last_ok_at=now,
    )
