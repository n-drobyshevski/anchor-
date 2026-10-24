"""The `interest_suggest` idle kind: the weekly batch of suggested
interest topics (anchor-interest-suggestions-plan.md section 5a; the
owner's decision 3, section 9).

Once a week, unasked, the bot proposes a handful of research topics --
the same batch `/interests suggest` makes, through the same
app/research/suggest.py `run_batch`, with `origin='weekly'`. It proposes
and never decides: only a tap on the batch message's buttons makes an
`interest_topic` row, and nothing is searched until the user adds a
topic and asks for a run, or idle research reaches it.

**It sends nothing.** The batch's one message goes out through the
send hook a manual batch uses (app/worker.py's
`_send_interest_suggestions`, called after every idle run and on every
heartbeat tick, gated by `may_report_now`) -- the lens garden's shape:
an idle job writes, a worker hook sends. So it needs no exception to
"idle never messages" beyond the garden's own, and
tests/test_idle_isolation.py's mock-bot test runs it like every other
kind.

**What the call sees** is app/research/suggest.py's `build_input` and
nothing else: the user's own topics, the dismissed suggestions, the
configured packets and, only while the lens is active, lens titles and
catalog summaries. This module reads no conversation data either; it
imports suggest.py, which reaches the lens through `app.vault.lens`
alone (tests/test_vault_notes_isolation.py), and touches only the
batch tables for its gate facts.

**The gate's facts** (`gate_facts`) are shared with nothing else that
could disagree: whether there is anything to suggest from is
suggest.py's `has_source`, which the command asks too; the two times
are read off `interest_suggest_batch` here. app/core/idle/gate.py's
`_interest_suggest_rule` turns them into `suggested_recently` and
`undecided`.

**Preemption** is checked before the call and never after the batch
is stored, as for the other single-call kinds: the runner checks once
right before this body, and `run_batch` asks `may_call` once more just
before the call (after its own input and `check_cap`). A preempted run
leaves its batch `failed` with `preempted`, unpaid, and the run is
`skipped:preempted`. Once the reply is in, the batch is stored whatever
the user does meanwhile -- the money is spent, and the message waits
for the hook's holds anyway.

**Spend.** `run_batch` ledgers the call under `interest_suggest`
(suggest.py's `SUGGEST_CATEGORY`), never `idle:interest_suggest`, so the
command and the weekly batch are one line in /spend. The runner folds
the batch's `usd_cost` into `idle_run.usd_cost`, and app/core/spend.py's
`today_idle_usd` adds that for this kind, as it does for idle research,
so `IDLE_USD_CAP` sees it and nothing is counted twice.

**A failure** (a provider error, a reply that does not parse) fails the
batch with its code and the run with the same code. A failed batch
does not count as `suggested_recently`, so the next day may try again
(`KIND_DAILY_MAX` keeps it to once a day). A manual `/interests
suggest` does not count weekly batches against its quota.

Logs carry ids and counts only, never a topic, a suggestion or a reason.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import logging

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core import clock as clock_module
from app.core.clock import Clock
from app.db.models import InterestSuggestBatch, InterestSuggestion
from app.llm.provider import LLMProvider
from app.research import suggest

logger = logging.getLogger(__name__)


# --- the gate's facts ---------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class SuggestFacts:
    """`IdleFacts`' three `suggest_*` fields (app/core/idle/gate.py)."""

    has_source: bool
    last_batch_at: datetime.datetime | None
    open_batch_at: datetime.datetime | None


async def gate_facts(
    session: AsyncSession, settings: Settings, now: datetime.datetime
) -> SuggestFacts:
    """The weekly gate's facts, three queries.

    `last_batch_at` is the newest batch of either origin that counts
    against the week: `done`, or `queued` and younger than suggest.py's
    `QUEUED_STALE` (a batch being made right now is about to be done; a
    queued one past that has lost its job, and the next `/interests
    suggest` marks it interrupted). A `failed` batch never counts.
    `open_batch_at` is the newest `done` batch's `created_at` when it
    still has an `open` suggestion, else None."""
    last_batch_at = (
        await session.execute(
            select(InterestSuggestBatch.created_at)
            .where(
                or_(
                    InterestSuggestBatch.status == "done",
                    (InterestSuggestBatch.status == "queued")
                    & (InterestSuggestBatch.created_at >= now - suggest.QUEUED_STALE),
                )
            )
            .order_by(InterestSuggestBatch.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    newest_done = (
        await session.execute(
            select(InterestSuggestBatch.id, InterestSuggestBatch.created_at)
            .where(InterestSuggestBatch.status == "done")
            .order_by(InterestSuggestBatch.created_at.desc(), InterestSuggestBatch.id.desc())
            .limit(1)
        )
    ).first()
    open_batch_at = None
    if newest_done is not None:
        has_open = (
            await session.execute(
                select(
                    exists().where(
                        InterestSuggestion.batch_id == newest_done.id,
                        InterestSuggestion.status == suggest.OPEN,
                    )
                )
            )
        ).scalar_one()
        if has_open:
            open_batch_at = newest_done.created_at
    return SuggestFacts(
        has_source=await suggest.has_source(session, settings),
        last_batch_at=last_batch_at,
        open_batch_at=open_batch_at,
    )


# --- the run ------------------------------------------------------------------------


# Batch codes the runner records as a skip, not a failure: the world
# changed between the gate and the call (a switch, the sources, the
# cap), or the user came back. Every other code fails the run.
SKIP_CODES = frozenset(
    {suggest.DISABLED, suggest.NOTHING, suggest.NO_PACKET, suggest.CAP, suggest.PREEMPTED}
)


@dataclasses.dataclass(frozen=True)
class SuggestResult:
    """What one weekly run did, as ids, a code and counts. `summary()` is
    `idle_run.summary`: ints only, and no key shares a name with a
    `LogRecord` attribute (app/core/idle/runner.py spreads it into a log
    call's `extra`, which keeps only app/log.py's SAFE_EXTRA_KEYS)."""

    batch_id: int
    status: str
    error_code: str | None = None
    suggestions: int = 0
    proposed: int = 0
    dropped: int = 0
    expired: int = 0
    usd_cost: decimal.Decimal = decimal.Decimal(0)

    @property
    def preempted(self) -> bool:
        return self.error_code == suggest.PREEMPTED

    def summary(self) -> dict[str, int]:
        return {
            "suggest_batch_id": self.batch_id,
            "suggestions": self.suggestions,
            "proposed": self.proposed,
            "dropped": self.dropped,
            "expired": self.expired,
        }


async def run_interest_suggest(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: Clock,
    *,
    run_id: int,
    started_at: datetime.datetime,
    timezone: str,
    provider: LLMProvider | None = None,
) -> SuggestResult:
    """The `interest_suggest` idle kind body, called by app/core/idle/
    runner.py after its gate re-check and first preemption check.

    Inserts a `weekly` batch and runs it through suggest.py's
    `run_batch`, with the preemption check as its `may_call`. `provider`
    lets a test inject a `FakeLLMProvider`; production leaves it None
    and `run_batch` builds `build_suggest_provider`. Never raises for a
    failed batch: the outcome's code says what happened, and the runner
    records it."""
    # Lazy, as in lens_garden.py: runner.py imports facts.py, which
    # imports this module for `gate_facts`.
    from app.core.idle.runner import is_preempted

    async with session_factory() as session:
        batch = suggest.new_batch(
            origin=suggest.WEEKLY,
            local_date=clock_module.local_date(clock, timezone),
            now=clock.now_utc(),
        )
        session.add(batch)
        await session.flush()
        batch_id = batch.id
        await session.commit()

    async with session_factory() as session:

        async def may_call() -> bool:
            return not await is_preempted(session, clock, started_at)

        outcome = await suggest.run_batch(
            session, settings, clock,
            batch_id=batch_id, timezone=timezone, provider=provider, may_call=may_call,
        )

    result = SuggestResult(
        batch_id=outcome.batch_id,
        status=outcome.status,
        error_code=outcome.error_code,
        suggestions=outcome.suggestions,
        proposed=outcome.proposed,
        dropped=outcome.dropped,
        expired=outcome.expired,
        usd_cost=outcome.usd_cost,
    )
    logger.info(
        "weekly suggestion batch finished",
        extra={
            "run_id": run_id,
            "suggest_batch_id": batch_id,
            "count": result.suggestions,
            "error_code": result.error_code,
        },
    )
    return result


__all__ = [
    "SKIP_CODES",
    "SuggestFacts",
    "SuggestResult",
    "gate_facts",
    "run_interest_suggest",
]
