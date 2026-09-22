"""Single-concurrency worker: claim -> feed_update -> complete/fail.

Concurrency is 1, which preserves message order for a single user
(plan section 6.3). recover_stuck runs in its own task every 60s —
folding it into the claim loop would starve it whenever the queue stays
busy.

Transaction boundary: claim() commits (releasing the FOR UPDATE row
lock) before feed_update runs. A Postgres lock is never held across a
Telegram API call.

Privacy: on failure only type(exc).__name__ and update_id are logged or
stored as the queue row's error — never exception args, which could
carry payload content.

2a adds background jobs (phase-2 plan section 3) to the same loop.
Inbound updates are claimed **first**: a job is only looked for when
the update queue is empty, so background work can never delay a reply
the user is waiting on. Concurrency stays 1 across both queues, which
keeps ordering total -- a job and an update never run at once.

3a stamps the inbound counters here rather than in a router
middleware (phase-3 plan section 4). This function is the only caller
of dp.feed_update in the repo, so it is the one place every inbound
update must pass: plain text, a slash command, a button press, and
even an update no handler matches. A middleware on the message
observer would miss callback queries, and "the user tapped a button"
is the user being present just as much as a sentence is.

3b adds the heartbeat as a third background task, modelled on the
recovery sweep rather than on anything new. It **plans, it never
does**: a few indexed SELECTs and at most one insert per minute, no
model call and no Telegram call. Everything slow goes into the job
table instead, where the priority rule above -- updates first, jobs
only when the update queue is empty -- already makes an inbound
message outrank a proactive one. That is why the heartbeat cannot
starve inbound handling: there is nothing in it long enough to.

The job path also gains the **main** provider in 3b. Until now only
background work ran as a job, so `cheap_provider` was enough;
`send_outbound` generates an in-character message and needs the same
model a reply would use.

It is recorded **before** feed_update, not after. The counters say
"the user was here", which is already true by the time the row is
claimed, and stamping first means a handler that raises still clears
the back-off -- a crash on the user's message must not leave Anchor
counting them as ignoring it.
"""

from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.clock import Clock
from app.core.extract import EXTRACT, ExtractOutcome, run_extract
from app.core.outbound import record_inbound
from app.core.outbound_send import SEND_OUTBOUND, run_send_outbound
from app.core.scheduler import heartbeat
from app.core.scene import SUMMARIZE_SCENE, Deferred, run_summarize_scene
from app.core.state import get_state
from app.db.jobs import claim_job, complete_job, defer_job, fail_job, recover_stuck_jobs
from app.db.queue import claim, complete, fail, recover_stuck
from app.llm.provider import LLMProvider
from app.tg.proposals import send_proposal

logger = logging.getLogger(__name__)

IDLE_SLEEP_SECONDS = 0.5
RECOVER_INTERVAL_SECONDS = 60
# Plan section 6: "A heartbeat task runs in the worker process every
# 60 s." Fine-grained enough for a 3-hour grace window, coarse enough
# that a minute of planning work per day is a rounding error.
HEARTBEAT_INTERVAL_SECONDS = 60


async def process_one_update(
    sessionmaker: async_sessionmaker[AsyncSession], dp: Dispatcher, bot: Bot, clock: Clock
) -> bool:
    """Claim and process a single update. Returns True iff a row was claimed.

    Extracted from the claim loop so tests can drive exactly one cycle
    without running an infinite loop.
    """
    async with sessionmaker() as session:
        row = await claim(session)

    if row is None:
        return False

    async with sessionmaker() as session:
        await record_inbound(session, clock)

    started_at = time.monotonic()
    try:
        update = Update.model_validate(row.payload, context={"bot": bot})
        await dp.feed_update(bot, update)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see module docstring
        async with sessionmaker() as session:
            await fail(session, row.update_id, type(exc).__name__)
        logger.warning(
            "update processing failed",
            extra={
                "update_id": row.update_id,
                "event": type(exc).__name__,
                "latency_ms": int((time.monotonic() - started_at) * 1000),
            },
        )
    else:
        async with sessionmaker() as session:
            await complete(session, row.update_id)
        logger.info(
            "update processed",
            extra={
                "update_id": row.update_id,
                "latency_ms": int((time.monotonic() - started_at) * 1000),
            },
        )

    return True


async def _run_job(
    session: AsyncSession,
    settings: Settings,
    provider: LLMProvider | None,
    cheap_provider: LLMProvider,
    bot: Bot,
    clock: Clock,
    kind: str,
    payload: dict,
) -> ExtractOutcome:
    """Dispatch one claimed job to its handler.

    Returns what the job changed, for the caller to act on.
    A dict lookup would be tidier, but the handlers do not share a
    signature, so this stays an explicit branch and an unknown kind
    raises rather than being silently dropped.
    """
    user_state = await get_state(session)

    if kind == SUMMARIZE_SCENE:
        await run_summarize_scene(
            session,
            settings,
            cheap_provider,
            scene_id=payload["scene_id"],
            clock=clock,
            timezone=user_state.timezone,
        )
        return ExtractOutcome()

    if kind == EXTRACT:
        return await run_extract(
            session,
            settings,
            cheap_provider,
            update_id=payload["update_id"],
            memory_ids=payload.get("memory_ids") or [],
            clock=clock,
            timezone=user_state.timezone,
            intensity=user_state.intensity,
            focus_on=user_state.focus_on,
            due_action=user_state.due_action,
        )

    if kind == SEND_OUTBOUND:
        # The only job kind that uses the main model and the bot: it
        # generates an in-character message and delivers it.
        if provider is None or bot is None:
            raise ValueError("send_outbound needs the main provider and a bot")
        await run_send_outbound(
            session,
            settings,
            provider,
            bot,
            clock=clock,
            outbound_id=payload["outbound_id"],
        )
        return ExtractOutcome()

    raise ValueError(f"unknown job kind: {kind}")


async def process_one_job(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    cheap_provider: LLMProvider,
    clock: Clock,
    bot: Bot | None = None,
    provider: LLMProvider | None = None,
) -> bool:
    """Claim and run a single due job. Returns True iff a job was claimed.

    Mirrors process_one_update: claim (which commits and releases the
    lock), run outside the lock, then complete or fail. Deferred is not
    a failure -- see app/core/scene.Deferred.
    """
    async with sessionmaker() as session:
        job = await claim_job(session)

    if job is None:
        return False

    job_id, kind, payload = job.id, job.kind, job.payload
    started_at = time.monotonic()
    outcome = ExtractOutcome()
    try:
        async with sessionmaker() as session:
            outcome = await _run_job(
                session, settings, provider, cheap_provider, bot, clock, kind, payload
            )
    except Deferred as deferred:
        async with sessionmaker() as session:
            await defer_job(session, job_id, deferred.run_after)
        logger.info("job deferred", extra={"job_id": job_id, "kind": kind})
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see module docstring
        async with sessionmaker() as session:
            await fail_job(session, job_id, type(exc).__name__)
        logger.warning(
            "job failed",
            extra={
                "job_id": job_id,
                "kind": kind,
                "event": type(exc).__name__,
                "latency_ms": int((time.monotonic() - started_at) * 1000),
            },
        )
    else:
        async with sessionmaker() as session:
            await complete_job(session, job_id)
        logger.info(
            "job processed",
            extra={
                "job_id": job_id,
                "kind": kind,
                "latency_ms": int((time.monotonic() - started_at) * 1000),
            },
        )
        # Sent after the job is marked done, not inside it: a send that
        # fails must not roll the job back and re-run the model call.
        # A proposal with no message is recoverable (the next one
        # expires it); a double-charged extraction is not.
        if outcome.created and bot is not None:
            await _send_proposals(sessionmaker, bot, outcome)

    return True


async def _send_proposals(
    sessionmaker: async_sessionmaker[AsyncSession], bot: Bot, outcome: ExtractOutcome
) -> None:
    """Send confirmation messages for proposals a job created.

    Only the newest can still be pending -- proposal.create() expires
    any outstanding one -- so send_proposal() is a no-op for the rest,
    by its own pending check. The first expired id is handed along so
    its now-stale buttons get edited away (plan section 8).
    """
    async with sessionmaker() as session:
        user_state = await get_state(session)
    expired_id = outcome.expired[0] if outcome.expired else None
    for proposal_id in outcome.created:
        try:
            await send_proposal(
                sessionmaker,
                bot,
                chat_id=user_state.chat_id,
                proposal_id=proposal_id,
                expired_id=expired_id,
            )
        except Exception as exc:  # noqa: BLE001 - a failed send must not fail the job
            logger.warning(
                "proposal send failed",
                extra={"proposal_id": proposal_id, "event": type(exc).__name__},
            )


async def _claim_loop(
    sessionmaker: async_sessionmaker[AsyncSession],
    dp: Dispatcher,
    bot: Bot,
    settings: Settings,
    cheap_provider: LLMProvider,
    clock: Clock,
    provider: LLMProvider | None = None,
) -> None:
    """Updates first, then due jobs, then idle (phase-2 plan section 3)."""
    while True:
        if await process_one_update(sessionmaker, dp, bot, clock):
            continue
        if await process_one_job(
            sessionmaker, settings, cheap_provider, clock, bot, provider
        ):
            continue
        await asyncio.sleep(IDLE_SLEEP_SECONDS)


async def _heartbeat_loop(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: Clock,
) -> None:
    """Plan due intents once a minute (phase-3 plan section 6).

    Broad except, like the loops above: a heartbeat that died on one
    bad tick would silently stop every proactive message for the rest
    of the process's life, and the first sign would be a morning that
    never arrived.
    """
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        try:
            async with sessionmaker() as session:
                await heartbeat(session, settings, clock)
        except Exception as exc:  # noqa: BLE001 - see the docstring
            logger.warning("heartbeat failed", extra={"event": type(exc).__name__})


async def _recover_loop(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    while True:
        await asyncio.sleep(RECOVER_INTERVAL_SECONDS)
        async with sessionmaker() as session:
            recovered = await recover_stuck(session)
            recovered_jobs = await recover_stuck_jobs(session)
        if recovered:
            logger.info("recovered stuck rows", extra={"count": recovered})
        if recovered_jobs:
            logger.info("recovered stuck jobs", extra={"count": recovered_jobs})


async def run_worker(
    sessionmaker: async_sessionmaker[AsyncSession],
    dp: Dispatcher,
    bot: Bot,
    settings: Settings,
    cheap_provider: LLMProvider,
    clock: Clock,
    provider: LLMProvider | None = None,
) -> list[asyncio.Task]:
    """Start the claim loop, the recovery sweep and the heartbeat."""
    claim_task = asyncio.create_task(
        _claim_loop(sessionmaker, dp, bot, settings, cheap_provider, clock, provider),
        name="anchor-claim-loop",
    )
    recover_task = asyncio.create_task(_recover_loop(sessionmaker), name="anchor-recover-loop")
    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(sessionmaker, settings, clock), name="anchor-heartbeat-loop"
    )
    return [claim_task, recover_task, heartbeat_task]


async def stop_worker(tasks: list[asyncio.Task]) -> None:
    """Cancel and await the worker's background tasks (shutdown path)."""
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
