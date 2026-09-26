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

4d adds `RESEARCH_SWEEP`, the daily card-expiry and clip-text-retention
job (app/research/sweeps.py, phase-4 plan sections 9 and 4), at most
once per local day. Unlike `TICK_DECIDE` it is *not* queued from inside
`heartbeat()` -- `_heartbeat_loop` below queues it as a sibling step
right after `heartbeat()` returns, so as not to perturb what
`heartbeat()` itself inserts (see that function's call site for why).
Like every other job kind above except `SEND_OUTBOUND`, it needs
neither `provider` nor a bot; unlike all of them, it needs neither
`safety_provider` either -- it is two SQL UPDATEs and a pair of log
lines, no model call at all.

8b adds `VAULT_SYNC` (queued every minute by `_heartbeat_loop` in mirror
and sync modes, pruned after an hour) and `VAULT_PURGE` (queued by
/delete inside its own transaction). Neither calls a model or needs the
bot; a purge that cannot reach the vault service is deferred, never
failed.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import time

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy import select as sql_select, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.clock import Clock, to_local, within_window
from app.core import amendments as amendments_module
from app.core.amendments import AMENDMENT_TRIAL
from app.core.extract import EXTRACT, ExtractOutcome, run_extract
from app.core.idle import IDLE_RUN
from app.core.idle.planner import plan_idle
from app.core.idle.runner import run_idle
from app.core.notebook import (
    NOTEBOOK_EXPIRY,
    NOTEBOOK_REFLECT,
    run_notebook_expiry,
    run_notebook_reflect,
)
from app.core import orders as orders_module
from app.core.obligations import OBLIGATION_SWEEP, sweep_missed_checkin
from app.core.orders import ORDERS_EXPIRY
from app.core.outbound import record_inbound
from app.core.outbound_send import SEND_OUTBOUND, run_send_outbound
from app.core.report import may_report_now
from app.core import review as review_module
from app.core.review import REVIEW_EXPIRY
from app.core.scheduler import (
    CLAUDE_LIBRARY_DIGEST,
    TICK_DECIDE,
    heartbeat,
    maybe_enqueue_backup,
    maybe_enqueue_library_digest,
    maybe_enqueue_notebook_expiry,
    maybe_enqueue_obligation_sweep,
    maybe_enqueue_orders_expiry,
    maybe_enqueue_planner_sync,
    maybe_enqueue_research_sweep,
    maybe_enqueue_retention_sweep,
    maybe_enqueue_review_expiry,
)
from app.core.tick import run_tick_decide
from app.core.retention import RETENTION_SWEEP, run_retention_sweep
from app.core.scene import SUMMARIZE_SCENE, Deferred, run_summarize_scene
from app.core.state import get_state
from app.db.models import HeartbeatState
from app.db.jobs import (
    claim_job,
    complete_job,
    defer_job,
    fail_job,
    recover_stuck_jobs,
    touch_job_lock,
)
from app.db.queue import claim, complete, fail, recover_stuck
from app.llm.provider import LLMProvider
from app.ops.backup import BACKUP, run_backup
from app.planner import actions as planner_actions
from app.planner.client import PlannerClient
from app.planner.jobs import (
    PLANNER_SYNC,
    PLANNER_WRITE,
    WRITE_ABANDONED_NOTICE,
    run_planner_sync,
    run_planner_write,
)
from app.research.jobs import RESEARCH, run_research_job
from app.research.sweeps import RESEARCH_SWEEP, run_daily_sweep
from app.tg import research as research_ui
from app.tg import vault as vault_ui
from app.tg import claude as claude_ui
from app.tg.orders import send_order_proposal
from app.tg.proposals import send_proposal
from app.vault.kinds import VAULT_PURGE, VAULT_SYNC
from app.vault.sync import maybe_enqueue_vault_sync, run_vault_purge, run_vault_sync

logger = logging.getLogger(__name__)

# How long a failed vault purge waits before the next try (plan 10).
VAULT_PURGE_RETRY = datetime.timedelta(minutes=5)

IDLE_SLEEP_SECONDS = 0.5
RECOVER_INTERVAL_SECONDS = 60


class WebDisabled(Exception):
    """Raised by process_one_update for a web-origin row (update_id < 0)
    when no `web_bot` was supplied (web-chat plan track 1). Caught by
    the same broad except every other row failure goes through, so it
    fails the row through the ordinary retry/MAX_ATTEMPTS path rather
    than a special one -- a worker that is ever started without
    WEB_UI_ENABLED while a stray web row exists (a redeploy mid-rollout,
    a config change) should retry and eventually fail it visibly, not
    crash the claim loop over one row.
    """
# Plan section 6: "A heartbeat task runs in the worker process every
# 60 s." Fine-grained enough for a 3-hour grace window, coarse enough
# that a minute of planning work per day is a rounding error.
HEARTBEAT_INTERVAL_SECONDS = 60


async def process_one_update(
    sessionmaker: async_sessionmaker[AsyncSession],
    dp: Dispatcher,
    bot: Bot,
    clock: Clock,
    web_bot: Bot | None = None,
) -> bool:
    """Claim and process a single update. Returns True iff a row was claimed.

    Extracted from the claim loop so tests can drive exactly one cycle
    without running an infinite loop.

    Web-chat plan track 1: `web_bot` defaults to `None` so every existing
    caller and test keeps working unchanged (the module docstring's
    "Phase 1 signatures unchanged" discipline, extended to this
    signature too). A row's origin -- the *sign* of its `update_id`
    (app/db/models.py's `TelegramUpdate` docstring: negative means web,
    minted by `web_update_seq`; Telegram's own ids are always
    non-negative) -- picks which `Bot` it is fed to: the real one for a
    Telegram row, `web_bot`'s WebSinkSession for a web one. That is the
    only functional change the whole synthetic-update design makes to
    this function: everything upstream (claim, record_inbound) and
    downstream (feed_update, complete/fail) is identical regardless of
    origin, because `dp.feed_update` reads `message.bot`/`callback.bot`
    for every send it makes and neither app/core/* nor app/tg/router.py
    otherwise cares which Bot subclass they were handed.

    A web-origin row (update_id < 0) claimed while `web_bot` is `None`
    (the web UI disabled or not wired into this worker) fails
    immediately, before `feed_update` ever runs -- there is no Bot to
    feed it to, and pretending the real one will do would silently leak
    a synthetic, negative-id update into Telegram-facing code that has
    never seen one.
    """
    async with sessionmaker() as session:
        row = await claim(session)

    if row is None:
        return False

    async with sessionmaker() as session:
        await record_inbound(session, clock)

    started_at = time.monotonic()
    try:
        if row.update_id < 0:
            if web_bot is None:
                raise WebDisabled("web UI is not enabled on this worker")
            bot_for_row = web_bot
        else:
            bot_for_row = bot
        update = Update.model_validate(row.payload, context={"bot": bot_for_row})
        await dp.feed_update(bot_for_row, update)
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
    safety_provider: LLMProvider | None = None,
    job_id: int | None = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    planner_client: PlannerClient | None = None,
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
        # H2: the extractor emits a strict JSON schema that decides what
        # gets written to memory. It runs on the safety model, not the
        # prose one.
        return await run_extract(
            session,
            settings,
            safety_provider or cheap_provider,
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
            # 5d: threaded through so a weekly_review row's analysis step
            # (app/core/review.py's analyze_week) has a safety-model
            # provider to run on. Falls back like every other H2 job
            # kind above, which is what the tests predating 5d rely on.
            safety_provider=safety_provider or cheap_provider,
        )
        return ExtractOutcome()

    if kind == RESEARCH:
        # 4b: a /read job's fetch-then-distill run. Distill is a strict-
        # schema call over a stranger's page text, so it runs on the
        # safety model like every other H2 job here -- never the main
        # model, which is reserved for in-character generation.
        outcome = await run_research_job(
            session,
            settings,
            safety_provider or cheap_provider,
            job_id=payload["job_id"],
            # A /read job carries its URL here; a /study job carries
            # its topic and packet on the study_job row instead.
            url=payload.get("url"),
            clock=clock,
            timezone=user_state.timezone,
        )
        if bot is not None:
            await _send_research_done(bot, settings, clock, user_state, outcome)
        return ExtractOutcome()

    if kind == RESEARCH_SWEEP:
        # 4d: card expiry + clip-text retention (plan sections 9 and 4).
        # No provider call and no bot, unlike every other kind above --
        # both sweeps are plain SQL housekeeping, so this needs neither
        # `provider` nor `safety_provider` and reports nothing back to
        # the user (there is no command this is a reply to).
        await run_daily_sweep(session, settings, clock)
        return ExtractOutcome()

    if kind == BACKUP:
        # 6e: the nightly encrypted backup (app/ops/backup.py). No
        # provider and no bot, same as RESEARCH_SWEEP above -- it is a
        # subprocess, an age-encrypt stream and an S3 upload, never a
        # model call, and never reports back to the user (the /state
        # "Бэкап: ..." line is how they find out, not a chat message).
        await run_backup(session, settings, clock)
        return ExtractOutcome()

    if kind == RETENTION_SWEEP:
        # 6e: the daily retention sweeps (app/core/retention.py) --
        # plain SQL housekeeping like RESEARCH_SWEEP/NOTEBOOK_EXPIRY
        # above, needing neither `provider` nor `safety_provider`.
        await run_retention_sweep(session, settings, clock)
        return ExtractOutcome()

    if kind == NOTEBOOK_REFLECT:
        # 5b: the same H2 shape as EXTRACT -- strict JSON on the safety
        # model, never the persona one, with the same cheap-provider
        # fallback the tests predating H2 rely on.
        await run_notebook_reflect(
            session,
            settings,
            safety_provider or cheap_provider,
            clock=clock,
            timezone=user_state.timezone,
            scene_id=payload["scene_id"],
        )
        return ExtractOutcome()

    if kind == NOTEBOOK_EXPIRY:
        # 5b: a bulk UPDATE closing stale open_thread entries -- plain
        # SQL housekeeping like RESEARCH_SWEEP above, so it needs
        # neither `provider` nor `safety_provider` and reports nothing
        # back to the user.
        await run_notebook_expiry(session, settings, clock=clock)
        return ExtractOutcome()

    if kind == ORDERS_EXPIRY:
        # 5c: moves stale proposed/awaiting_counter/countered rows to
        # expired (plan section 7's "Expiry") -- plain SQL housekeeping
        # like NOTEBOOK_EXPIRY above, needing neither provider nor a bot.
        await orders_module.expire_stale(session, clock=clock)
        return ExtractOutcome()

    if kind == OBLIGATION_SWEEP:
        # Phase 5: opens yesterday's missed check-in as a debt -- plain
        # SQL like ORDERS_EXPIRY above, idempotent by its unique index.
        state = await get_state(session)
        await sweep_missed_checkin(session, clock, state.timezone)
        return ExtractOutcome()

    if kind == REVIEW_EXPIRY:
        # 5d: moves stale pending review_proposal rows to expired (plan
        # section 8's "A daily sweep (review_expiry)") -- plain SQL
        # housekeeping like NOTEBOOK_EXPIRY/ORDERS_EXPIRY above.
        await review_module.run_review_expiry(session, settings, clock=clock)
        return ExtractOutcome()

    if kind == AMENDMENT_TRIAL:
        # 5d: the blocking eval subset, against a throwaway database
        # only (app/core/amendments.py's own docstring). `job_id` lets
        # the trial extend its own lease across a run of ~13 cases --
        # see app/db/jobs.touch_job_lock's docstring for why that
        # matters here specifically.
        outcome = ExtractOutcome()

        async def _on_case_done() -> None:
            if job_id is not None:
                await touch_job_lock(session, job_id)

        result = await amendments_module.run_trial(
            session,
            settings,
            clock=clock,
            amendment_id=payload["amendment_id"],
            on_case_done=_on_case_done,
        )
        if result is not None:
            outcome.amendment_trial_id = result.amendment_id
        return outcome

    if kind == IDLE_RUN:
        # 6a: the idle framework's own job kind. Unlike every other
        # branch above, this one needs `sessionmaker` rather than the
        # single `session` process_one_job already opened -- run_idle
        # claims, re-checks the gate and finishes the run in separate
        # transactions of its own (its own module docstring says why:
        # a preemption check must see rows another session commits
        # between steps). `provider` runs backfill's summary calls
        # (prose, same as SUMMARIZE_SCENE above); `safety_provider` runs
        # its reflect calls (strict JSON, same as NOTEBOOK_REFLECT
        # above). Never sends anything and never touches `bot` -- see
        # app/core/idle/'s own isolation test.
        if sessionmaker is None:
            raise ValueError("idle_run needs sessionmaker")
        await run_idle(
            sessionmaker,
            settings,
            provider or cheap_provider,
            safety_provider or cheap_provider,
            clock,
            run_id=payload["run_id"],
            job_id=job_id,
        )
        return ExtractOutcome()

    if kind == PLANNER_SYNC:
        # P2: no LLM call, no bot needed to do the work -- a bot is only
        # used, best-effort, for the once-only "reconnect the planner"
        # notice on a revoked grant (see app/planner/jobs.py).
        if planner_client is None:
            raise ValueError("planner_sync needs a PlannerClient")
        await run_planner_sync(
            session,
            settings,
            planner_client,
            clock,
            timezone=user_state.timezone,
            bot=bot,
            chat_id=user_state.chat_id,
        )
        return ExtractOutcome()

    if kind == PLANNER_WRITE:
        # P3: same shape as PLANNER_SYNC above -- no LLM call, bot used
        # only for the canned confirmation / revoked-grant notice.
        if planner_client is None:
            raise ValueError("planner_write needs a PlannerClient")
        await run_planner_write(
            session,
            settings,
            planner_client,
            clock,
            planner_action_id=payload["planner_action_id"],
            timezone=user_state.timezone,
            bot=bot,
            chat_id=user_state.chat_id,
        )
        return ExtractOutcome()

    if kind == TICK_DECIDE:
        # H2: the safety model decides whether there is a natural reason
        # to write first -- another strict-schema verdict. It plans an
        # outbound row at most; the send-time gate still has the last
        # word (plan section 8).
        await run_tick_decide(
            session,
            settings,
            safety_provider or cheap_provider,
            clock=clock,
            local_date=datetime.date.fromisoformat(payload["local_date"]),
            hour=payload["hour"],
        )
        return ExtractOutcome()

    if kind == VAULT_SYNC:
        # 8b: one sync pass (phase-8 plan section 7). No model call, no
        # bot: an unreachable vault completes the job normally, and the
        # next minute's pass tries again. 8c hands the PassResult back
        # on `outcome.vault_pass_result` -- process_one_job sends the
        # notice and any newly-sendable hold messages through
        # app/tg/vault.py *after* the job is marked done, the same
        # "send after, not inside" rule outcome.order_proposed and
        # outcome.amendment_trial_id already follow.
        result = await run_vault_sync(session, settings, clock)
        outcome = ExtractOutcome()
        outcome.vault_pass_result = result
        return outcome

    if kind == VAULT_PURGE:
        # 8b: /delete's reach into the vault (phase-8 plan section 10).
        # Any failure defers by five minutes -- Deferred keeps attempts
        # at 0, so "/delete must really delete" never gives up.
        if not await run_vault_purge(settings):
            raise Deferred(clock.now_utc() + VAULT_PURGE_RETRY)
        return ExtractOutcome()

    if kind == CLAUDE_LIBRARY_DIGEST:
        # C3: the library's once-a-day digest (connector plan section
        # 9). No provider, like RESEARCH_SWEEP/BACKUP above -- plain SQL
        # plus one Telegram send, gated by may_report_now inside
        # run_library_digest itself (which raises Deferred, not an
        # exception, when it is not allowed to send right now).
        if bot is not None:
            await claude_ui.run_library_digest(session, settings, clock, bot, payload)
        return ExtractOutcome()

    raise ValueError(f"unknown job kind: {kind}")


async def process_one_job(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    cheap_provider: LLMProvider,
    clock: Clock,
    bot: Bot | None = None,
    provider: LLMProvider | None = None,
    safety_provider: LLMProvider | None = None,
    planner_client: PlannerClient | None = None,
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
                session, settings, provider, cheap_provider, bot, clock, kind, payload,
                safety_provider, job_id, sessionmaker, planner_client,
            )
    except Deferred as deferred:
        async with sessionmaker() as session:
            await defer_job(session, job_id, deferred.run_after)
        logger.info("job deferred", extra={"job_id": job_id, "kind": kind})
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see module docstring
        async with sessionmaker() as session:
            terminal = await fail_job(session, job_id, type(exc).__name__)
            # A PLANNER_WRITE job that exhausts its retries without ever
            # raising PlannerAuthError (e.g. the planner stayed
            # unreachable the whole time) would otherwise leave the
            # planner_action stuck `accepted` with the user never told
            # the write did not happen -- see design review finding 6.
            if terminal and kind == PLANNER_WRITE:
                await planner_actions.mark_failed(session, payload["planner_action_id"])
                if bot is not None:
                    user_state = await get_state(session)
                    await bot.send_message(chat_id=user_state.chat_id, text=WRITE_ABANDONED_NOTICE)
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
        # 5c: a standing-order proposal from this turn's extraction,
        # sent through app/tg/orders.py -- never through
        # app/tg/proposals.py, because it is not a Proposal row (see
        # ExtractOutcome.order_proposed's own docstring).
        if outcome.order_proposed is not None and bot is not None:
            await _send_order_proposal(sessionmaker, bot, outcome.order_proposed)
        # 5d: the amendment_trial result message ("Поправка принята."/
        # "не прошла проверку и не применена."), sent through
        # app/tg/amendments.py -- never through app/tg/proposals.py, for
        # the same reason ExtractOutcome.order_proposed's own docstring
        # gives: this is not a Proposal row either.
        if outcome.amendment_trial_id is not None and bot is not None:
            await _send_amendment_result(sessionmaker, bot, outcome.amendment_trial_id)
        # 8c: the vault notice and any pending hold messages (plan
        # section 8), through app/tg/vault.py.
        if outcome.vault_pass_result is not None and bot is not None:
            await _send_vault_pass_updates(sessionmaker, bot, settings, clock, outcome.vault_pass_result)

    return True


async def _send_research_done(
    bot: Bot, settings: Settings, clock: Clock, user_state, outcome
) -> None:
    """One short line when a /read job finishes, if it may be sent.

    Out of character on purpose, like every other system reply: the
    bot reporting on a task, not Anchor talking.
    """
    if not may_report_now(settings, clock, user_state):
        logger.info(
            "research completion not sent", extra={"job_id": outcome.job_id, "event": "quiet"}
        )
        return
    text = research_ui.completion_text(
        status=outcome.status,
        error_code=outcome.error_code,
        visible_cards=outcome.visible_cards,
    )
    await bot.send_message(chat_id=user_state.chat_id, text=text)
    logger.info(
        "research completion sent",
        extra={"job_id": outcome.job_id, "cards": outcome.visible_cards},
    )


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


async def _send_order_proposal(
    sessionmaker: async_sessionmaker[AsyncSession], bot: Bot, order_id: int
) -> None:
    """The extractor's own standing-order proposal card (5c)."""
    async with sessionmaker() as session:
        user_state = await get_state(session)
    try:
        await send_order_proposal(sessionmaker, bot, chat_id=user_state.chat_id, order_id=order_id)
    except Exception as exc:  # noqa: BLE001 - a failed send must not fail the job
        logger.warning(
            "order proposal send failed",
            extra={"order_id": order_id, "event": type(exc).__name__},
        )


async def _send_vault_pass_updates(
    sessionmaker: async_sessionmaker[AsyncSession],
    bot: Bot,
    settings: Settings,
    clock: Clock,
    result,
) -> None:
    """The vault notice and any newly-sendable hold messages (8c)."""
    try:
        await vault_ui.send_pass_updates(sessionmaker, bot, settings, clock, result)
    except Exception as exc:  # noqa: BLE001 - a failed send must not fail the job
        logger.warning("vault pass send failed", extra={"event": type(exc).__name__})


async def _send_amendment_result(
    sessionmaker: async_sessionmaker[AsyncSession], bot: Bot, amendment_id: int
) -> None:
    """The amendment_trial result message (5d)."""
    from app.tg.amendments import send_trial_result

    async with sessionmaker() as session:
        user_state = await get_state(session)
    try:
        await send_trial_result(sessionmaker, bot, chat_id=user_state.chat_id, amendment_id=amendment_id)
    except Exception as exc:  # noqa: BLE001 - a failed send must not fail the job
        logger.warning(
            "amendment trial result send failed",
            extra={"amendment_id": amendment_id, "event": type(exc).__name__},
        )


async def _claim_loop(
    sessionmaker: async_sessionmaker[AsyncSession],
    dp: Dispatcher,
    bot: Bot,
    settings: Settings,
    cheap_provider: LLMProvider,
    clock: Clock,
    provider: LLMProvider | None = None,
    safety_provider: LLMProvider | None = None,
    web_bot: Bot | None = None,
    planner_client: PlannerClient | None = None,
) -> None:
    """Updates first, then due jobs, then idle (phase-2 plan section 3)."""
    while True:
        if await process_one_update(sessionmaker, dp, bot, clock, web_bot):
            continue
        if await process_one_job(
            sessionmaker, settings, cheap_provider, clock, bot, provider, safety_provider,
            planner_client,
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

    4d adds the research sweep enqueue as a second step in the same
    tick, right after `heartbeat()` -- deliberately not *inside*
    `heartbeat()`. app/core/scheduler.py's module docstring has the
    full reasoning; short version: several existing tests call
    `heartbeat()` directly and assert an exact `job` table state
    afterwards (e.g. "a failed planning gate inserts no row" means zero
    job rows, not just zero outbound rows), and those tests predate 4d.
    Running it as a sibling call here gets the same once-a-minute
    cadence -- which is all `maybe_enqueue_research_sweep`'s dedup key
    needs to become "once a local day" -- without touching what
    `heartbeat()` itself does or does not insert. Both calls share the
    broad except below for the same reason they are both here: a sweep
    that silently stopped enqueueing would be no louder a failure than a
    heartbeat that did, and neither deserves to take the other down with
    it, but a single try/except is simpler than two and the failure mode
    (log and retry next minute) is identical either way.
    """
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        try:
            async with sessionmaker() as session:
                await heartbeat(session, settings, clock)
            async with sessionmaker() as session:
                state = await get_state(session)
                await maybe_enqueue_research_sweep(session, clock, state.timezone)
            # P2: same shape and the same reason as the research sweep
            # right above -- see app/core/scheduler.py's module docstring
            # for why this is a sibling step and not inside heartbeat().
            async with sessionmaker() as session:
                state = await get_state(session)
                await maybe_enqueue_planner_sync(session, settings, clock, state.timezone)
            # 5b: the notebook's own daily sweep, same cadence and same
            # "not inside heartbeat()" reasoning as the research sweep
            # right above it -- see app/core/scheduler.py's module
            # docstring.
            async with sessionmaker() as session:
                state = await get_state(session)
                await maybe_enqueue_notebook_expiry(session, clock, state.timezone)
            # 5c: standing orders' own daily sweep, same cadence and same
            # "not inside heartbeat()" reasoning as the two sweeps above.
            async with sessionmaker() as session:
                state = await get_state(session)
                await maybe_enqueue_orders_expiry(session, clock, state.timezone)
            # Phase 5: the debt queue's missed-check-in sweep, same
            # cadence and reasoning.
            async with sessionmaker() as session:
                state = await get_state(session)
                await maybe_enqueue_obligation_sweep(session, clock, state.timezone)
            # 5d: the weekly review's own daily sweep, same cadence and
            # same "not inside heartbeat()" reasoning as the three above.
            async with sessionmaker() as session:
                state = await get_state(session)
                await maybe_enqueue_review_expiry(session, clock, state.timezone)
            # 6e: the nightly backup, same cadence and same "not inside
            # heartbeat()" reasoning as the sweeps above -- see
            # app/core/scheduler.py's maybe_enqueue_backup for the extra
            # gates (BACKUP_ENABLED, BACKUP_TIME) this one alone checks.
            async with sessionmaker() as session:
                state = await get_state(session)
                await maybe_enqueue_backup(session, settings, clock, state.timezone)
            # 6e: the daily retention sweeps, same cadence and same
            # "not inside heartbeat()" reasoning as every sweep above.
            async with sessionmaker() as session:
                state = await get_state(session)
                await maybe_enqueue_retention_sweep(session, clock, state.timezone)
            # C3: the library's once-a-day digest, same cadence and same
            # "not inside heartbeat()" reasoning as every sweep above --
            # see app/core/scheduler.py's maybe_enqueue_library_digest
            # for the extra gate (CLAUDE_ACCESS_ENABLED,
            # CLAUDE_LIBRARY_DIGEST_TIME) this one alone checks.
            async with sessionmaker() as session:
                state = await get_state(session)
                await maybe_enqueue_library_digest(session, settings, clock, state.timezone)
            # 6a: idle planning, same cadence and same "not inside
            # heartbeat()" reasoning as the four sweeps above -- see
            # app/core/scheduler.py's module docstring. plan_idle opens
            # its own session (app/core/idle/planner.py) rather than
            # reusing one from here, matching every other sibling step.
            async with sessionmaker() as session:
                await plan_idle(session, settings, clock)
            # 6a: the heartbeat's own liveness stamp (6e's /readyz reads
            # this). A bare targeted UPDATE, not through app/core/state.py
            # -- heartbeat_state is not user_state, and this loop is not
            # under app/core/idle/'s isolation rules anyway.
            async with sessionmaker() as session:
                await session.execute(
                    sql_update(HeartbeatState)
                    .where(HeartbeatState.id == 1)
                    .values(heartbeat_at=clock.now_utc())
                )
                await session.commit()
            # 8b: this minute's vault pass, in mirror/sync only -- a
            # sibling step for the same reason as the research sweep.
            async with sessionmaker() as session:
                await maybe_enqueue_vault_sync(session, settings, clock)
        except Exception as exc:  # noqa: BLE001 - see the docstring
            logger.warning("heartbeat failed", extra={"event": type(exc).__name__})


WATCHDOG_INTERVAL_SECONDS = 60


def watchdog_is_stale(
    heartbeat_at: datetime.datetime | None,
    now: datetime.datetime,
    started_at: datetime.datetime,
    stale_after: datetime.timedelta,
) -> bool:
    """Pure predicate behind the liveness watchdog (Phase 6 plan section
    9.6; milestone 6e). Separated from `_watchdog_loop` so a test can
    drive it with an injected clock and no database at all.

    Railway's own healthcheck (`/readyz`, app/tg/webhook.py) is only
    consulted at deploy time -- it does not restart a service that goes
    unhealthy later in its life. This predicate is what backs the
    in-process fallback: the process kills *itself* when the heartbeat
    has gone stale, so Railway's restart policy (which does apply to a
    crashed process) brings it back.

    **The startup grace.** Before `started_at + stale_after` has
    elapsed, a `heartbeat_at` of `None` -- the heartbeat loop has not
    stamped it even once yet -- is never stale. Without this, the
    watchdog would kill a freshly-deployed process during the first
    `HEARTBEAT_INTERVAL_SECONDS` or so of its life, before the
    heartbeat loop's very first tick has had a chance to run at all.
    Once that grace has passed, a still-`None` heartbeat_at *is* stale
    -- the heartbeat loop genuinely never ran, which is exactly the
    failure this watchdog exists to catch.
    """
    if heartbeat_at is None:
        return now - started_at > stale_after
    return now - heartbeat_at > stale_after


async def _watchdog_loop(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: Clock,
    *,
    exit_fn=None,
    started_at: datetime.datetime | None = None,
) -> None:
    """Checks heartbeat staleness once a minute; exits the process if stale.

    `exit_fn` defaults to `os._exit` (not `sys.exit`, which only raises
    SystemExit and would be caught by this loop's own broad except, or
    by aiohttp/asyncio machinery above it -- the point is an immediate,
    unrecoverable process exit for Railway's restart policy to act on).
    Overridable so a test can assert it was *called* with the right
    code rather than actually terminating the test process.
    """
    exit_fn = exit_fn if exit_fn is not None else (lambda code: os._exit(code))
    started_at = started_at if started_at is not None else clock.now_utc()
    stale_after = datetime.timedelta(minutes=settings.LIVENESS_STALE_MIN)
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)
        try:
            async with sessionmaker() as session:
                result = await session.execute(
                    sql_select(HeartbeatState.heartbeat_at).where(HeartbeatState.id == 1)
                )
                heartbeat_at = result.scalar_one_or_none()
        except Exception as exc:  # noqa: BLE001 - a check failure is not itself staleness
            logger.warning("watchdog check failed", extra={"event": type(exc).__name__})
            continue
        if watchdog_is_stale(heartbeat_at, clock.now_utc(), started_at, stale_after):
            logger.error("heartbeat stale, exiting", extra={"event": "heartbeat_stale"})
            exit_fn(1)
            return


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
    safety_provider: LLMProvider | None = None,
    web_bot: Bot | None = None,
    planner_client: PlannerClient | None = None,
) -> list[asyncio.Task]:
    """Start the claim loop, the recovery sweep and the heartbeat.

    `safety_provider` (H2) defaults to None, and every job that needs it
    falls back to `cheap_provider` -- which is what the tests predating
    H2 rely on. app/main.py always supplies it. `web_bot` (web-chat plan
    track 1) similarly defaults to None; track 2's app/main.py supplies
    it only when WEB_UI_ENABLED. `planner_client` (P2) likewise defaults
    to None; it is only required when a PLANNER_SYNC job is actually
    claimed, which cannot happen with PLANNER_ENABLED off (app/core/
    scheduler.py's maybe_enqueue_planner_sync never enqueues one), so
    tests that do not touch the planner pass no client.
    """
    claim_task = asyncio.create_task(
        _claim_loop(
            sessionmaker,
            dp,
            bot,
            settings,
            cheap_provider,
            clock,
            provider,
            safety_provider,
            web_bot,
            planner_client,
        ),
        name="anchor-claim-loop",
    )
    recover_task = asyncio.create_task(_recover_loop(sessionmaker), name="anchor-recover-loop")
    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(sessionmaker, settings, clock), name="anchor-heartbeat-loop"
    )
    # 6e: the liveness watchdog (plan section 9.6). Started alongside
    # the heartbeat loop, not folded into it -- a heartbeat tick that
    # raises is caught by that loop's own broad except and retried next
    # minute, which is the right behaviour for planning but the wrong
    # one for a watchdog: this loop must keep checking and, when the
    # heartbeat genuinely never recovers, actually exit the process.
    watchdog_task = asyncio.create_task(
        _watchdog_loop(sessionmaker, settings, clock), name="anchor-watchdog-loop"
    )
    return [claim_task, recover_task, heartbeat_task, watchdog_task]


async def stop_worker(tasks: list[asyncio.Task]) -> None:
    """Cancel and await the worker's background tasks (shutdown path)."""
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
