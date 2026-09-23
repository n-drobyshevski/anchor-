"""`run_idle` -- the `idle_run` job body (approved plan §2, §5).

**Preemption.** `is_preempted()` checks two things: whether any
`telegram_update` row was received after `run.started_at` (it uses
`created_at`, the only "when did this arrive" timestamp that table
has), or whether `user_state.last_user_msg_at > started_at` -- pressing
a button counts as presence too, exactly as app/core/state.py's own
`last_user_msg_at` docstring says. The check runs before the first
model call (right after the in-job gate re-check, below) and, for
`backfill`, between every unit (app/core/idle/backfill.py). A future
single-transaction kind (6b's consolidate/reflect) would additionally
check after every model call and immediately before its final commit --
`RunContext.check_preempted()` below is what those kinds will call.

**Crash handling.** A redelivered job whose run is already anything but
`queued` becomes `failed`/`interrupted` and is never resumed -- the
same "failed rather than resumed" decision docs/decisions.md records for
research jobs, for the same reason: a resume point does not exist for
several of these kinds, and the money may already be spent either way.

**Idle jobs are never retried through fail_job.** Every exception is
caught here, turned into `failed:<code>` (via `skip_reason`, which
doubles as the failure code -- see `IdleRun`'s own docstring), and the
queue job still completes normally, so app/worker.py's ordinary
`fail_job` retry path never sees it and never pays for the same
work twice.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core import clock as clock_module
from app.core.clock import Clock
from app.core.idle import BACKFILL
from app.core.idle.candidates import REFLECTED_SCENE_IDS
from app.core.idle.facts import load_idle_facts
from app.core.idle.gate import config_from_settings, idle_gate
from app.db.models import IdleChange, IdleRun, SpendLedger, TelegramUpdate, UserState
from app.llm.provider import LLMProvider, LLMUsage

logger = logging.getLogger(__name__)


async def is_preempted(session: AsyncSession, clock: Clock, started_at: datetime.datetime) -> bool:
    """A user update since `started_at` -- see the module docstring."""
    result = await session.execute(
        select(TelegramUpdate.update_id)
        .where(TelegramUpdate.created_at > started_at)
        .limit(1)
    )
    if result.first() is not None:
        return True
    state = (await session.execute(select(UserState).where(UserState.id == 1))).scalar_one()
    return state.last_user_msg_at is not None and state.last_user_msg_at > started_at


class JobCapHit(Exception):
    """Raised by `RunContext.charge()` above `IDLE_JOB_USD_CAP`."""


@dataclasses.dataclass
class RunContext:
    """Per-run helpers for the single-transaction kind handlers 6b+ add
    (consolidate, reflect, ...). `app/core/idle/backfill.py` does not use
    this in 6a -- it reuses `run_summarize_scene`/`run_notebook_reflect`
    verbatim, which already manage their own commits and their own
    spend_ledger rows.
    """

    session: AsyncSession
    settings: Settings
    clock: Clock
    run_id: int
    kind: str
    started_at: datetime.datetime
    timezone: str
    _spent: decimal.Decimal = dataclasses.field(default=decimal.Decimal(0), init=False)

    async def check_preempted(self) -> bool:
        return await is_preempted(self.session, self.clock, self.started_at)

    async def charge(self, usage: LLMUsage, model: str | None) -> decimal.Decimal:
        """Ledger one model call under `idle:<kind>`; raise `JobCapHit`
        above `IDLE_JOB_USD_CAP`."""
        from app.core.spend import priced

        cost = priced(usage, self.settings, model=model)
        self._spent += cost.usd
        self.session.add(
            SpendLedger(
                local_date=clock_module.local_date(self.clock, self.timezone),
                category=f"idle:{self.kind}",
                model=model,
                tokens_in=usage.input_tokens,
                tokens_cached=usage.cached_tokens,
                tokens_out=usage.output_tokens,
                usd_cost=cost.usd,
                cost_source=cost.source,
            )
        )
        if self._spent > decimal.Decimal(str(self.settings.IDLE_JOB_USD_CAP)):
            raise JobCapHit(self.kind)
        return cost.usd

    async def record_change(
        self, table: str, row_id: int, op: str, before: dict | None, after: dict | None
    ) -> None:
        self.session.add(
            IdleChange(
                run_id=self.run_id, table_name=table, row_id=row_id, op=op, before=before, after=after
            )
        )


async def _finish(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    run_id: int,
    *,
    status: str,
    skip_reason: str | None = None,
    summary: dict | None = None,
    usd_cost: decimal.Decimal | None = None,
) -> None:
    async with session_factory() as session:
        run = await session.get(IdleRun, run_id)
        if run is None:
            return
        run.status = status
        run.finished_at = clock.now_utc()
        if skip_reason is not None:
            run.skip_reason = skip_reason
        if summary is not None:
            run.summary = summary
        if usd_cost is not None:
            run.usd_cost = usd_cost
        await session.commit()


async def spend_since(
    session_factory: async_sessionmaker[AsyncSession], started_at: datetime.datetime
) -> decimal.Decimal:
    """Sum spend_ledger rows written by this run -- ts >= started_at,
    category LIKE 'idle:%'. Recorded on the idle_run row regardless of
    how the run ends: money already spent is real even on a skipped or
    failed run (plan section 2: "The money was really spent, so the cap
    has to see it. That is accounting, not content.")."""
    from sqlalchemy import func

    async with session_factory() as session:
        result = await session.execute(
            select(func.coalesce(func.sum(SpendLedger.usd_cost), 0))
            .where(SpendLedger.ts >= started_at)
            .where(SpendLedger.category.like("idle:%"))
        )
        return decimal.Decimal(result.scalar_one())


async def run_idle(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    provider: LLMProvider,
    safety_provider: LLMProvider,
    clock: Clock,
    *,
    run_id: int,
) -> None:
    """Claim `run_id`, re-check the gate, run its kind, record the outcome."""
    async with session_factory() as session:
        run = await session.get(IdleRun, run_id)
        if run is None:
            logger.warning("idle run not found", extra={"run_id": run_id})
            return
        if run.status != "queued":
            # A redelivered job whose run is already running/done/etc --
            # see the module docstring. Never resumed.
            run.status = "failed"
            run.skip_reason = "interrupted"
            run.finished_at = clock.now_utc()
            await session.commit()
            logger.warning("idle run redelivered while not queued", extra={"run_id": run_id})
            return
        kind = run.kind
        started_at = clock.now_utc()
        run.status = "running"
        run.started_at = started_at
        await session.commit()

    async with session_factory() as session:
        state = (await session.execute(select(UserState).where(UserState.id == 1))).scalar_one()
        timezone = state.timezone
        facts = await load_idle_facts(session, settings, clock, timezone)
        config = config_from_settings(settings)
        verdict = idle_gate(kind, facts, clock.now_utc(), config, self_run_id=run_id)
        already_preempted = (
            await is_preempted(session, clock, started_at) if verdict.allowed else False
        )

    if not verdict.allowed:
        await _finish(session_factory, clock, run_id, status="skipped", skip_reason=verdict.reason)
        return
    if already_preempted:
        await _finish(session_factory, clock, run_id, status="skipped", skip_reason="preempted")
        return

    try:
        if kind == BACKFILL:
            from app.core.idle.backfill import run_backfill

            result = await run_backfill(
                session_factory,
                settings,
                provider,
                safety_provider,
                clock,
                started_at=started_at,
                timezone=timezone,
            )
        else:
            raise ValueError(f"idle kind not implemented: {kind}")
    except Exception as exc:  # noqa: BLE001 - never retried, see module docstring
        usd_cost = await spend_since(session_factory, started_at)
        await _finish(
            session_factory, clock, run_id, status="failed",
            skip_reason=type(exc).__name__, usd_cost=usd_cost,
        )
        logger.warning("idle run failed", extra={"run_id": run_id, "event": type(exc).__name__})
        return

    usd_cost = await spend_since(session_factory, started_at)

    if result.preempted and result.summarized == 0 and result.reflected == 0:
        await _finish(
            session_factory, clock, run_id, status="skipped", skip_reason="preempted", usd_cost=usd_cost,
        )
        return

    summary = {
        "summarized": result.summarized,
        "reflected": result.reflected,
        REFLECTED_SCENE_IDS: list(result.reflected_scene_ids),
    }
    if result.preempted:
        summary["preempted"] = True
    if result.job_cap:
        summary["job_cap"] = True
    await _finish(session_factory, clock, run_id, status="done", summary=summary, usd_cost=usd_cost)
    logger.info("idle run done", extra={"run_id": run_id, "event": kind, **summary})


__all__ = ["JobCapHit", "RunContext", "is_preempted", "run_idle", "spend_since"]
