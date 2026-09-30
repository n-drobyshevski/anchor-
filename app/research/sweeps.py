"""The research loop's two lifecycle sweeps (phase-4 plan sections 4 and 9).

Two rules, both dated in wall-clock days rather than triggered by any
event:

- **Card expiry** (plan section 9): a `pending` card older than
  `RESEARCH_CARD_TTL_DAYS` is marked `expired`. Nobody decided on it in
  time, so `/notes` should stop offering it rather than let a growing
  backlog of stale suggestions sit there forever.
- **Clip text retention** (plan section 4): `study_clip.text` is set to
  NULL 30 days after `fetched_at`, keeping the row's metadata. A page's
  full extracted text is the most content-bearing thing this feature
  stores about a website that is not the user's own words, and there is
  no reason to keep it once no card from it can still be pending review
  (`RESEARCH_CARD_TTL_DAYS` defaults to 14, well under 30, so by the
  time text is forgotten every card that could reference it has already
  been decided or expired).

**One job, not two.** `run_daily_sweep` below runs both and is what
app/worker.py dispatches on a single job kind (`RESEARCH_SWEEP`). The
two operations are unrelated data-wise -- neither reads the other's
result -- but they are identical in every way that would justify
separate scheduling: same cadence (once a day), same injected `Clock`,
same "cheap SQL UPDATE, no provider call" shape, same failure mode
(a bug here is a bug in a WHERE clause, not a flaky network call worth
isolating so one retry doesn't waste the other's work). Splitting them
would mean two dedup keys, two enqueue calls from the scheduler, and
two log lines to correlate by hand for no isolation actually gained:
if one half's UPDATE statement is wrong, both halves running as one
transaction-per-function fail exactly the same way whether they share
a job or not, and app/worker.py's existing per-job try/except already
marks the whole thing failed and retries it, sweep-of-two included.

**Injected Clock, not `created_at`'s own default.** Both functions take
`clock` and compute their cutoff as `clock.now_utc() - timedelta(days=N)`,
then compare that instant to a stored timestamp column in SQL -- the
same shape app/research/jobs.py's `_recent_clip_urls` already uses for
its 30-day dedupe window. For `study_clip.fetched_at` this is clean:
app/research/jobs.py sets it explicitly from `clock.now_utc()` at fetch
time, so a FrozenClock in a test controls it completely. For
`study_card.created_at` it is one step looser -- that column is
`server_default=func.now()` and nothing in app/research/jobs.py
overrides it, so in production it is the *database's* clock, not the
Python one, that stamps a card's birth. That is the same category of
exception app/core/clock.py's own docstring already carves out for
`run_after <= func.now()` in app/db/queue.py: a wall-clock read outside
the injected `Clock`, deliberate, and harmless because the DB's clock
and the process's clock are the same physical clock in production and
drift only by milliseconds. A test that needs a specific card age sets
`created_at` explicitly at construction (SQLAlchemy only falls back to
`server_default` when no value is given), so `FrozenClock` still gives
full determinism over *this* module's behaviour -- it just cannot also
fake the moment a fixture row claims to have been created, which no
test here needs it to.

**Lens L4** (anchor-lens-plan.md section 9; the L4 spec section 4 with
the owner's amendment (b): a lens research's result is its own Telegram
message, sent when the job finishes). A lens card's clock starts when
the user could first see it, not when it was distilled -- the result
message can wait out quiet hours, a pause or the welfare cooldown:

- a lens card expires `RESEARCH_CARD_TTL_DAYS` after its job's
  `offered_at` (the result message went out);
- a lens card never offered expires twice that long after it was
  made, so a result that could never be sent does not wait forever;
- a lens card whose job lost its gap (`lens_gap_id` SET NULL: the
  garden went away) expires at once -- nothing can act on it any more.

And a lens job still unfinished `LENS_STALE_DAYS` after it was queued
fails as `stale` (app/research/jobs.py's `fail_stale_lens_jobs`): idle
may be off, and the gap must not wait forever. The garden hook then
sends its «ничего не нашлось» like any spent research's. Part of the
same daily job, for the same reasons as the two sweeps above.

**Logging.** app/log.py's `SAFE_EXTRA_KEYS` is the enforced allowlist;
both functions log only `event` and `count` (ids of individual cards or
clips are not logged, deliberately -- a list of which cards expired
this run is not otherwise available anywhere and is not worth adding
just to log it, per plan section 12's "counts, not content").
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import and_, or_, select
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.clock import Clock
from app.db.models import StudyCard, StudyClip, StudyJob
from app.research import jobs as research_jobs

logger = logging.getLogger(__name__)

# Event names for the two sweeps' log lines (see the module docstring on
# why they share one job kind but still log distinguishably).
EXPIRE_CARDS = "expire_cards"
FORGET_CLIP_TEXT = "forget_clip_text"
STALE_LENS_JOBS = "stale_lens_jobs"

# L4 (module docstring): a lens card never offered gets this many TTLs.
NEVER_OFFERED_FACTOR = 2

# Plan section 4: "set study_clip.text = null 30 days after fetched_at".
RETENTION_DAYS = 30

# job.kind app/worker.py dispatches on to run both sweeps (see the
# module docstring for why this is one job, not two). Named after the
# module rather than after either function, matching RESEARCH in
# app/research/jobs.py, which names the *job kind*, not either of the
# two shapes (read/study) it dispatches on internally.
RESEARCH_SWEEP = "research_sweep"

# ck_study_job... has no analogue here, but study_card's own
# ck_study_card_status lists this as one of the five values a card can
# hold, and ck_study_card_high_is_hidden is why expire_cards below must
# never touch a 'hidden' row: see that function's docstring.
_EXPIRED = "expired"


async def expire_cards(session: AsyncSession, settings: Settings, clock: Clock) -> int:
    """Mark `pending` cards older than `RESEARCH_CARD_TTL_DAYS` as `expired`.

    **Only `status='pending'` rows are ever matched.** `adopted` and
    `rejected` are decisions the user already made; re-marking either
    one would silently discard that decision, which is a worse bug than
    anything expiry is meant to fix. `hidden` is the one that actually
    matters to get right: `ck_study_card_high_is_hidden` requires every
    `risk_final='high'` card to be `status='hidden'` and nothing else,
    forever -- a naive "any old card" UPDATE that tried to also catch
    stale hidden cards would flip one to `'expired'` and the CHECK
    constraint would reject the whole UPDATE, failing the *entire*
    sweep including every card that was legitimately due. Filtering on
    `status == 'pending'` sidesteps that by construction, since a
    `pending` card can never have `risk_final='high'` in the first
    place (app/research/jobs.py only ever writes `status='hidden'` for
    those) -- there is no row this UPDATE could touch that the
    constraint would ever object to.

    "Older than N days" is `created_at < now - N days`, strictly: a
    card exactly `RESEARCH_CARD_TTL_DAYS` old is still inside its
    window, matching the same "half-open, boundary belongs to the
    earlier side" convention app/core/outbound_gate.py's grace windows
    use. Idempotent by construction -- an already-`expired` card is not
    `pending` and is never matched again.

    L4: a lens card follows the module docstring's three rules instead,
    all keyed on its job (`offered_at`, `lens_gap_id`), with the same
    strict boundaries; a /study or /read card is untouched by them.

    Returns the number of cards expired, for the caller to log.
    """
    now = clock.now_utc()
    ttl = datetime.timedelta(days=settings.RESEARCH_CARD_TTL_DAYS)
    cutoff = now - ttl
    result = await session.execute(
        sql_update(StudyCard)
        .where(StudyCard.status == "pending")
        .where(StudyCard.kind != research_jobs.LENS_CARD)
        .where(StudyCard.created_at < cutoff)
        .values(status=_EXPIRED)
    )
    lens_jobs = select(StudyJob.id).where(
        or_(
            StudyJob.lens_gap_id.is_(None),
            StudyJob.offered_at < cutoff,
        )
    )
    never_offered = select(StudyJob.id).where(
        StudyJob.lens_gap_id.is_not(None), StudyJob.offered_at.is_(None)
    )
    lens_result = await session.execute(
        sql_update(StudyCard)
        .where(StudyCard.status == "pending")
        .where(StudyCard.kind == research_jobs.LENS_CARD)
        .where(
            or_(
                StudyCard.job_id.in_(lens_jobs),
                and_(
                    StudyCard.job_id.in_(never_offered),
                    StudyCard.created_at < now - NEVER_OFFERED_FACTOR * ttl,
                ),
            )
        )
        .values(status=_EXPIRED)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    count = (result.rowcount or 0) + (lens_result.rowcount or 0)
    logger.info("cards expired", extra={"event": EXPIRE_CARDS, "count": count})
    return count


async def forget_clip_text(
    session: AsyncSession, clock: Clock, *, days: int = RETENTION_DAYS
) -> int:
    """Null `study_clip.text` on clips fetched more than `days` ago.

    A clip whose `fetched_at IS NULL` is a failed fetch (blocked,
    timed out, wrong content type -- app/research/fetch.py's
    `FetchFailure` path) and never had any text to begin with; it is
    excluded by the `fetched_at IS NOT NULL` filter rather than by
    accident of the date comparison, because `NULL < cutoff` is SQL
    NULL, not true, and a filter that merely relied on that would break
    the moment someone rewrote the WHERE clause to use `is distinct
    from` or an OR.

    Only `text` is nulled. `text_sha256`, `url`, `domain`, `title` and
    `http_status` all survive -- plan section 4 says "keeping the
    metadata", and `_recent_clip_urls`' 30-day re-fetch dedupe
    (app/research/jobs.py) reads `url`/`fetched_at` specifically because
    it must keep working on a clip whose text this function already
    forgot.

    **Idempotent in the sense that matters to a caller counting
    work done**, not merely in the sense that re-running it is
    harmless: the extra `StudyClip.text.is_not(None)` filter means a
    clip already nulled is not matched a second time, so a re-run
    returns 0 for it rather than reporting the same clip as newly
    forgotten on every run for the rest of its life.

    Returns the number of clips whose text was nulled, for the caller
    to log.
    """
    cutoff = clock.now_utc() - datetime.timedelta(days=days)
    result = await session.execute(
        sql_update(StudyClip)
        .where(StudyClip.fetched_at.is_not(None))
        .where(StudyClip.fetched_at < cutoff)
        .where(StudyClip.text.is_not(None))
        .values(text=None)
    )
    await session.commit()
    count = result.rowcount or 0
    logger.info("clip text forgotten", extra={"event": FORGET_CLIP_TEXT, "count": count})
    return count


async def fail_stale_lens_jobs(session: AsyncSession, clock: Clock) -> int:
    """L4 (module docstring): every lens job unfinished
    `LENS_STALE_DAYS` after it was queued fails as `stale`. The job
    module owns the rule; this commits it and logs the count."""
    count = await research_jobs.fail_stale_lens_jobs(session, clock.now_utc())
    await session.commit()
    logger.info("stale lens jobs failed", extra={"event": STALE_LENS_JOBS, "count": count})
    return count


async def run_daily_sweep(
    session: AsyncSession, settings: Settings, clock: Clock
) -> tuple[int, int]:
    """Run both sweeps once. `(expired, forgotten)`, for the job log line.

    This is the body of the `RESEARCH_SWEEP` job app/worker.py claims;
    app/core/scheduler.py enqueues it at most once per local day (see
    that module's `maybe_enqueue_research_sweep`). Order between the two
    calls does not matter -- neither reads a row the other writes -- so
    this runs them sequentially rather than concurrently for the same
    reason the rest of this codebase avoids concurrent DB work per job:
    worker concurrency is 1 throughout, and there is no benefit to
    asyncio.gather-ing two awaits that only ever run on one connection
    anyway.
    """
    expired = await expire_cards(session, settings, clock)
    forgotten = await forget_clip_text(session, clock)
    # L4: counted in its own log line, not in the return value, which
    # callers (and their tests) read as the two sweeps' counts.
    await fail_stale_lens_jobs(session, clock)
    return expired, forgotten


__all__ = [
    "EXPIRE_CARDS",
    "FORGET_CLIP_TEXT",
    "RESEARCH_SWEEP",
    "NEVER_OFFERED_FACTOR",
    "RETENTION_DAYS",
    "STALE_LENS_JOBS",
    "expire_cards",
    "fail_stale_lens_jobs",
    "forget_clip_text",
    "run_daily_sweep",
]
