"""Persona amendments: trial before live (phase-5 plan sections 3 and 9;
milestone 5d).

**What this is, in one sentence.** A `persona_note` review proposal the
user adopts becomes a `persona_amendment` in `trial`; the
`amendment_trial` job runs the blocking eval subset against it with an
independent judge, and only a clean pass makes it `active` -- `failed`
otherwise, with no fallback and no partial credit. `persona.md` is
**never written**: an active amendment only changes what the live
prompt carries under `## Поправки (одобрены тобой)`
(app/core/prompt.py, wired by app/core/persona_context.py's `gather()`).

**The trial never touches the live database.** `run_trial()` delegates
the actual eval run to `eval.trial.run_blocking_subset`, which opens its
own throwaway database via `eval.db.throwaway_sessionmaker(admin_url=...)`
and drops it when done -- the same guarantee a manual `eval.run.py`
invocation gets. Nothing in this module ever imports `tests/conftest.py`
or touches the app's own `session` for anything but reading/writing its
own rows (the amendment itself, and the ledger).

**The judge must be independent, checked first, before anything else
runs.** If `settings.LLM_MODEL_JUDGE` is empty or equals
`settings.LLM_MODEL`, the trial fails immediately with reason
`no_independent_judge` -- no throwaway database is created and no API
call is made at all. There is no fallback to `LLM_MODEL_CHEAP`: eval.run.py
already refuses a same-model blocking run for the same reason (H5), and
an amendment that goes live on the strength of the model grading its
own homework is exactly the failure mode that check exists to prevent.

**`eval_report` holds pass/fail per case only, never model text** -- the
implementation plan's non-negotiable, enforced structurally by
`eval.trial.TrialResult.cases` being a `dict[str, bool]` end to end.
"""

from __future__ import annotations

import dataclasses
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core import review as review_module
from app.core.clock import Clock
from app.core.prompt import load_persona
from app.core.scene import Deferred
from app.core import clock as clock_module
from app.core.spend import check_cap
from app.db.models import PersonaAmendment, SpendLedger

logger = logging.getLogger(__name__)

AMENDMENT_TRIAL = "amendment_trial"
AMENDMENT_TRIAL_CATEGORY = "amendment_trial"

TRIAL = "trial"
ACTIVE = "active"
FAILED = "failed"
REVOKED = "revoked"
STATUSES = (TRIAL, ACTIVE, FAILED, REVOKED)

NO_INDEPENDENT_JUDGE = "no_independent_judge"
CASE_FAILED = "case_failed"
TRIAL_ENV_UNAVAILABLE = "trial_env_unavailable"

# The Russian strings, verbatim from the implementation plan.
CAP_TEXT = "Сначала отзови одну поправку."
CHECKING_TEXT = "Проверяю поправку…"
ACTIVE_TEXT = "Поправка принята."
FAILED_TEXT = "Поправка не прошла проверку и не применена."
STALE_CHANGED = "(персона изменилась — проверь)"
EMPTY_LIST_TEXT = "Поправок нет."


@dataclasses.dataclass(frozen=True)
class AdoptResult:
    """`adopt()`'s outcome: `status` is `"ok"`, `"cap"` or `"stale"`;
    `amendment` is the new row on `"ok"`, else None."""

    status: str
    amendment: PersonaAmendment | None = None


async def _count_active_or_trial(session: AsyncSession) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(PersonaAmendment)
        .where(PersonaAmendment.status.in_((TRIAL, ACTIVE)))
    )
    return result.scalar_one()


async def adopt(
    session: AsyncSession, settings: Settings, review_proposal_id: int, *, clock: Clock
) -> AdoptResult:
    """`am:a:<id>` -- adopting a `persona_note` review proposal.

    The cap counts `active` plus `trial` rows together (implementation
    plan's "Adopt": a second amendment cannot even start a trial while
    one is already running against the cap). Checked before the
    proposal is touched, so a capped-out user's proposal stays pending
    and answerable once they free up room -- the same "the row keeps its
    status" shape `app/core/orders.py`'s `accept()` uses for its own cap.
    """
    if await _count_active_or_trial(session) >= settings.AMENDMENTS_MAX_ACTIVE:
        return AdoptResult(status="cap")

    proposal = await review_module.mark_proposal(
        session, review_proposal_id, review_module.ADOPTED, clock=clock
    )
    if proposal is None:
        return AdoptResult(status="stale")

    _, persona_sha = load_persona()
    # Constructed inline in the `session.add(...)` call itself, matching
    # app/core/orders.py's `create_active()` -- the shape
    # tests/test_autonomy_isolation.py's structural AST scan (a `Model(...)`
    # passed directly to `.add()`) can actually see, unlike a
    # `row = Model(...); session.add(row)` two-step.
    session.add(
        PersonaAmendment(
            text=proposal.text, status=TRIAL, proposal_id=proposal.id, persona_sha=persona_sha
        )
    )
    await session.commit()
    result = await session.execute(
        select(PersonaAmendment)
        .where(PersonaAmendment.proposal_id == proposal.id)
        .order_by(PersonaAmendment.id.desc())
        .limit(1)
    )
    row = result.scalars().first()
    logger.info("amendment adopted", extra={"amendment_id": row.id})
    return AdoptResult(status="ok", amendment=row)


async def reject(session: AsyncSession, review_proposal_id: int, *, clock: Clock) -> bool:
    """`am:r:<id>` -- declining a `persona_note` review proposal."""
    row = await review_module.mark_proposal(
        session, review_proposal_id, review_module.REJECTED, clock=clock
    )
    return row is not None


async def revoke(session: AsyncSession, amendment_id: int, *, clock: Clock) -> bool:
    """`/amendments`' own [Отозвать] -- only a still-`active` row."""
    row = await session.get(PersonaAmendment, amendment_id)
    if row is None or row.status != ACTIVE:
        return False
    row.status = REVOKED
    row.revoked_at = clock.now_utc()
    await session.commit()
    logger.info("amendment revoked", extra={"amendment_id": amendment_id})
    return True


async def active_amendments(session: AsyncSession) -> list[PersonaAmendment]:
    result = await session.execute(
        select(PersonaAmendment).where(PersonaAmendment.status == ACTIVE).order_by(PersonaAmendment.id)
    )
    return list(result.scalars().all())


@dataclasses.dataclass(frozen=True)
class DisplayRow:
    """One `/amendments` list row: the amendment, and whether persona.md
    has changed since it was adopted."""

    amendment: PersonaAmendment
    stale: bool


async def list_for_display(session: AsyncSession) -> list[DisplayRow]:
    """`/amendments`' listing: active amendments, each flagged when
    `persona.md`'s current hash differs from the one it was adopted
    against -- persona.md is never rewritten to match, per the module
    docstring; the user is only told to check."""
    _, current_sha = load_persona()
    rows = await active_amendments(session)
    return [DisplayRow(amendment=row, stale=row.persona_sha != current_sha) for row in rows]


# --- the amendment_trial job --------------------------------------------


@dataclasses.dataclass(frozen=True)
class TrialOutcome:
    """What `run_trial()` decided, for app/worker.py's post-processing
    hook to render the result message from."""

    amendment_id: int
    status: str  # active | failed


async def run_trial(
    session: AsyncSession,
    settings: Settings,
    *,
    clock: Clock,
    amendment_id: int,
    on_case_done=None,
) -> TrialOutcome | None:
    """The `amendment_trial` job body (implementation plan's "The
    amendment_trial job").

    Returns None when the amendment is gone or no longer `trial` (a
    replayed job after a crash finds nothing to do -- the same
    idempotency shape every other job body in this codebase gives a
    row whose state has already moved on).

    `on_case_done` is awaited once per blocking case (via
    `eval.trial.run_blocking_subset`) so the caller can extend the
    claimed job's lease -- see app/worker.py's own comment on why a
    ~13-case, ~26-call trial needs that.
    """
    row = await session.get(PersonaAmendment, amendment_id)
    if row is None or row.status != TRIAL:
        logger.info("amendment trial skipped, not in trial", extra={"amendment_id": amendment_id})
        return None

    # Step 1: the judge check, strictly, before anything else -- no
    # throwaway database, no API call. See the module docstring.
    judge_model = settings.LLM_MODEL_JUDGE
    if not judge_model or judge_model == settings.LLM_MODEL:
        row.status = FAILED
        row.eval_report = {"cases": {}, "reason": NO_INDEPENDENT_JUDGE}
        await session.commit()
        logger.info(
            "amendment trial failed, no independent judge", extra={"amendment_id": amendment_id}
        )
        return TrialOutcome(amendment_id=amendment_id, status=FAILED)

    # Step 2: the daily spend cap, deferred like every other H2 job.
    #
    # A direct, read-only SELECT of UserState.timezone rather than
    # app.core.state.get_state() -- this module may not import
    # app.core.state at all (tests/test_autonomy_isolation.py's
    # FORBIDDEN_IMPORTS), even for a read, because that module is where
    # every *write* to user_state happens and the isolation test is a
    # blanket "this module never imports that one", not "never writes
    # through it".
    from app.db.models import UserState

    timezone_row = await session.execute(select(UserState.timezone).where(UserState.id == 1))
    timezone = timezone_row.scalar_one()
    if await check_cap(session, settings, clock, timezone):
        run_after = clock_module.next_local_midnight(clock, timezone)
        logger.info("amendment trial deferred by cap", extra={"amendment_id": amendment_id})
        raise Deferred(run_after)

    # Step 3: the blocking eval subset, in a throwaway database only.
    try:
        from eval.trial import run_blocking_subset

        others = [
            amendment.text
            for amendment in await active_amendments(session)
            if amendment.id != amendment_id
        ]
        result = await run_blocking_subset(
            settings,
            clock=clock,
            amendments=[*others, row.text],
            on_case_done=on_case_done,
        )
    except Exception as exc:  # noqa: BLE001 - an unusable trial env fails closed
        row.status = FAILED
        row.eval_report = {"cases": {}, "reason": TRIAL_ENV_UNAVAILABLE}
        await session.commit()
        logger.warning(
            "amendment trial environment unavailable",
            extra={"amendment_id": amendment_id, "event": type(exc).__name__},
        )
        return TrialOutcome(amendment_id=amendment_id, status=FAILED)

    # Step 4: apply the verdict.
    if result.passed:
        row.status = ACTIVE
        row.activated_at = clock.now_utc()
        row.eval_report = {"cases": result.cases, "reason": None}
    else:
        row.status = FAILED
        row.eval_report = {"cases": result.cases, "reason": CASE_FAILED}
    await session.commit()

    # Step 5: the ledger, summed persona-plus-judge cost across every
    # blocking case. eval.run.Outcome.usd_cost already covers both --
    # response.usage.cost_usd for the persona call plus verdict.usd_cost
    # for the judge -- so eval.trial.TrialResult.usd_cost (their sum
    # over every case) is the whole trial's cost; no further pricing
    # arithmetic is needed, only quantizing to the ledger's own
    # precision.
    session.add(
        SpendLedger(
            local_date=clock_module.local_date(clock, timezone),
            category=AMENDMENT_TRIAL_CATEGORY,
            model=settings.LLM_MODEL_JUDGE,
            tokens_in=0,
            tokens_cached=0,
            tokens_out=0,
            usd_cost=_decimal(result.usd_cost),
            cost_source="vendor",
        )
    )
    await session.commit()

    logger.info(
        "amendment trial finished",
        extra={"amendment_id": amendment_id, "passed": result.passed},
    )
    return TrialOutcome(amendment_id=amendment_id, status=row.status)


def _decimal(value: float):
    import decimal

    return decimal.Decimal(str(value)).quantize(
        decimal.Decimal("0.000001"), rounding=decimal.ROUND_HALF_UP
    )


__all__ = [
    "ACTIVE",
    "ACTIVE_TEXT",
    "AMENDMENT_TRIAL",
    "AMENDMENT_TRIAL_CATEGORY",
    "AdoptResult",
    "CAP_TEXT",
    "CASE_FAILED",
    "CHECKING_TEXT",
    "DisplayRow",
    "EMPTY_LIST_TEXT",
    "FAILED",
    "FAILED_TEXT",
    "NO_INDEPENDENT_JUDGE",
    "REVOKED",
    "STALE_CHANGED",
    "STATUSES",
    "TRIAL",
    "TRIAL_ENV_UNAVAILABLE",
    "TrialOutcome",
    "active_amendments",
    "adopt",
    "list_for_display",
    "reject",
    "revoke",
    "run_trial",
]
