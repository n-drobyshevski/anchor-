"""The `PLANNER_SYNC` job body (P2: read path only -- no `PLANNER_WRITE` here).

Makes no LLM call, like `app/research/sweeps.py`'s daily sweep: it is a
network call to the planner plus one upsert. app/worker.py dispatches
on `PLANNER_SYNC` the same way it dispatches every other job kind, and
a failure (network, or the planner returning garbage) simply fails the
job -- the normal retry/backoff machinery in app/db/jobs.py applies,
same as any other job kind.

The one exception is `PlannerAuthError`: retrying a sync against a
revoked grant cannot succeed, so this catches it, sends the user
exactly one "reconnect the planner" notice via
`app.planner.auth.mark_notice_sent`'s once-only gate, and lets the job
complete rather than fail-and-retry forever.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.clock import Clock
from app.planner import auth, snapshot
from app.planner.auth import PlannerAuthError
from app.planner.client import PlannerClient, PlannerToolError, PlannerUnavailable

logger = logging.getLogger(__name__)

PLANNER_SYNC = "planner_sync"

RELINK_NOTICE = (
    "Не получилось обновить план — доступ к планеру отозван. "
    "Набери /planner_link, чтобы подключить его заново."
)


async def run_planner_sync(
    session: AsyncSession,
    settings: Settings,
    client: PlannerClient,
    clock: Clock,
    *,
    timezone: str,
    bot: Bot | None = None,
    chat_id: int | None = None,
) -> None:
    """Refresh `planner_snapshot`. No provider call, no persona involved.

    On `PlannerAuthError` this does not re-raise: a revoked grant will
    not un-revoke itself on retry, and the point of the notice is to ask
    the user to act, not to spend the job queue's retry budget on
    something retrying cannot fix.
    """
    try:
        await snapshot.sync(session, settings, client, clock, timezone)
    except PlannerAuthError:
        if await auth.mark_notice_sent(session) and bot is not None and chat_id is not None:
            await bot.send_message(chat_id=chat_id, text=RELINK_NOTICE)
        return
    except (PlannerUnavailable, PlannerToolError) as exc:
        logger.warning("planner sync failed", extra={"event": type(exc).__name__})
        raise


__all__ = ["PLANNER_SYNC", "RELINK_NOTICE", "run_planner_sync"]
