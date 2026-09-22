"""The `/read` job: enqueue, then run it end to end (phase-4 plan sections 4, 9, 12).

This is the only place in the research package that touches the
database. `app/research/fetch.py`, `distill.py` and `risk.py` are pure
functions and scripted providers; this module is what turns their
answers into `study_job` / `study_clip` / `study_card` rows, in the
same shape `app/core/extract.py` uses for its own background job: a
cap check, a provider call, a `spend_ledger` row, then validated
writes.

**Milestone 4b is `/read` only.** `enqueue_read` is the one entry point;
`/study`'s search-then-fetch-then-distill loop is 4c's job, and nothing
here assumes it exists yet.

**Where a /read URL lives: the job payload.** `study_job.query` is
documented (plan section 4, and `StudyJob`'s docstring) as a topic
string capped at 200 characters, which is what `/study` will put
there. A `/read` job has no topic at enqueue time -- it is decided
later from the fetched page's title (see `_topic_for`) -- so `query`
stays null for a read, and the address travels in the queue row's
JSONB `payload`, exactly as `update_id`, `scene_id` and `outbound_id`
do for the other job kinds.

Not in `query`, which was the obvious place and is the wrong one: the
200-character cap is a real limit on real links. An article URL
carrying campaign parameters, or any share link from a phone, runs
past it easily, and there is no honest refusal to give for that -- the
URL is fine, it simply does not fit a column meant for a topic. The
choice was a wrong-cause `BAD_URL` on a perfectly good link or a
column that means two things; the payload is neither.

`study_clip.url` is set independently, from the fetcher's own result,
and stays the authoritative record of what was actually read after
redirects.

**Why the cap is checked twice.** Once in `enqueue_read`, before a job
is even queued, and again in `run_research_job`, right before the one
call that costs money. A job can sit in the queue for a while behind
other work, and the daily budget the enqueue-time check saw is not
necessarily the one still true when the job is claimed.

**Why no safety_events row.** `app/core/extract.py` records one on
every run of its own job, and this module's docstring template asked
for the same here. But `app/core/safety_events.py`'s `kind` column is
`CHECK`-constrained to `('welfare', 'extractor', 'tick')`
(`ck_safety_event_kind` in `app/db/models.py`), and none of the three
describes a distill call. Adding a fourth needs a migration, which is
outside this worker's file list -- see the report handed back with
this milestone. `study_job.error_code` already carries the one signal
that table would add (did the model's JSON parse), so nothing is lost,
only unobserved by /state's per-day rollup.
"""

from __future__ import annotations

import decimal
import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core import clock as clock_module
from app.core.clock import Clock
from app.core.spend import check_cap, priced
from app.db.jobs import enqueue_job
from app.db.models import SpendLedger, StudyCard, StudyClip, StudyJob
from app.llm.provider import LLMProvider
from app.research import distill
from app.research.addresses import parse_target
from app.research.fetch import Clip, FetchFailure, fetch as default_fetch, make_robots_cache

logger = logging.getLogger(__name__)

# The job kind app/db/jobs.enqueue_job stores and app/worker.py dispatches
# on. `job` (app/db/models.py) has no CHECK constraint on `kind`, unlike
# `study_job.kind`, so this string needs no migration to exist.
RESEARCH = "research"

# study_job.kind values (ck_study_job_kind). Only READ is used in 4b.
READ = "read"
STUDY = "study"

# Refusal codes from enqueue_read. A closed set, same convention as
# app/research/errors.py: these are what a caller (Worker B's /read
# handler) branches on to choose a Russian reply, never shown raw.
DISABLED = "disabled"
QUOTA = "quota"
CAP = "cap"
BAD_URL = "bad_url"

# The topic handed to distill.call when a fetched page has no <title>
# (or trafilatura found none). Neutral on purpose: distill.py folds it
# into "Верни ... карточек по теме «{topic}»", and this phrase has to
# read sensibly there for a page about anything at all, without
# claiming a topic nobody chose.
UNTITLED_TOPIC = "содержимое страницы"


@dataclass(frozen=True)
class ResearchOutcome:
    """What a /read job produced, for the caller to report on.

    `error_code` is a fetch-failure code from app/research/errors.py,
    `"cap"`, or None -- never free text. A job that finished with zero
    surviving cards is `status="done"` with `error_code=None`: that is
    not a failure, and the wording for "nothing useful turned up" is
    the caller's to choose (Worker B), not this module's.
    """

    job_id: int
    status: str
    error_code: str | None
    visible_cards: int
    hidden_cards: int


async def _read_quota_used(session: AsyncSession, local_date) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(StudyJob)
        .where(StudyJob.kind == READ, StudyJob.local_date == local_date)
    )
    return result.scalar_one()


async def enqueue_read(
    session: AsyncSession,
    settings: Settings,
    clock: Clock,
    *,
    timezone: str,
    url: str,
) -> tuple[int | None, str | None]:
    """Queue a /read job for `url`, or refuse it (plan section 9's /read row).

    Checks run in the order the plan lists them: disabled, then the URL
    itself, then the daily quota, then the spend cap. `parse_target` is
    a syntax gate only -- scheme, userinfo, literal-IP publicness, and
    a plausible hostname shape -- and never resolves DNS: that is
    app/research/fetch.py's job, at fetch time, where a stale DNS
    answer accepted here could not do any harm anyway.

    Returns `(job_id, None)` on success, `(None, refusal_code)`
    otherwise. No row of any kind is written on a refusal.
    """
    if not settings.RESEARCH_ENABLED:
        return None, DISABLED

    target = parse_target(url)
    if isinstance(target, str):
        return None, BAD_URL

    local_date = clock_module.local_date(clock, timezone)
    used = await _read_quota_used(session, local_date)
    if used >= settings.RESEARCH_READS_PER_DAY:
        return None, QUOTA

    if await check_cap(session, settings, clock, timezone):
        return None, CAP

    job = StudyJob(kind=READ, status="queued", query=None, local_date=local_date)
    session.add(job)
    await session.flush()  # need job.id before enqueue_job's own commit
    await enqueue_job(
        session,
        RESEARCH,
        {"job_id": job.id, "url": target.url},
        dedup_key=f"research:{job.id}",
    )
    logger.info("read job queued", extra={"job_id": job.id})
    return job.id, None


async def _card_counts(session: AsyncSession, job_id: int) -> tuple[int, int]:
    """(visible, hidden) card counts for a job that already ran.

    Used only by the idempotent-rerun path: a job that is not
    `status='queued'` has already written whatever cards it was going
    to, and this reports them without touching anything.
    """
    result = await session.execute(
        select(StudyCard.status, func.count())
        .where(StudyCard.job_id == job_id)
        .group_by(StudyCard.status)
    )
    tallied = dict(result.all())
    hidden = tallied.get("hidden", 0)
    visible = sum(count for status, count in tallied.items() if status != "hidden")
    return visible, hidden


def _job_cap_hit(job: StudyJob, settings: Settings) -> bool:
    """Has this job alone spent RESEARCH_JOB_USD_CAP?

    For a /read job, which makes exactly one distill call, `usd_cost`
    is always 0 the one time this runs before that call -- the check
    exists for symmetry with the daily check_cap() call beside it, and
    so app/research/jobs.py's per-job accounting already works the day
    a /study job (multiple distills) calls the same helper.
    """
    return job.usd_cost >= decimal.Decimal(str(settings.RESEARCH_JOB_USD_CAP))


def _topic_for(clip: StudyClip) -> str:
    """The distill topic for a /read job: the page's own title, or a
    neutral stand-in (plan section 7 assumes a topic exists; /read has
    none supplied by the user, only whatever the page called itself)."""
    return clip.title or UNTITLED_TOPIC


async def run_research_job(
    session: AsyncSession,
    settings: Settings,
    provider: LLMProvider,
    *,
    job_id: int,
    url: str,
    clock: Clock,
    timezone: str,
    fetch_fn=None,
) -> ResearchOutcome:
    """Run one /read job to completion: fetch, distill, validate, write.

    `url` comes from the queue row's payload, not from the `study_job`
    row -- see the module docstring on where a /read URL lives.

    Idempotent: a job not found, or not `status='queued'`, is reported
    on without doing any work -- the queue can redeliver a completed
    job (a worker restart between `complete_job` and its ack, say) and
    this must not fetch or spend twice.

    `fetch_fn` defaults to app.research.fetch.fetch and exists purely
    as a test seam (plan section 14: "no real network").
    """
    fetch_fn = fetch_fn or default_fetch

    job = await session.get(StudyJob, job_id)
    if job is None:
        return ResearchOutcome(
            job_id=job_id, status="failed", error_code="not_found",
            visible_cards=0, hidden_cards=0,
        )
    if job.status != "queued":
        visible, hidden = await _card_counts(session, job.id)
        return ResearchOutcome(
            job_id=job.id, status=job.status, error_code=job.error_code,
            visible_cards=visible, hidden_cards=hidden,
        )

    job.status = "fetching"
    await session.commit()

    robots = make_robots_cache(
        timeout_s=settings.FETCH_TIMEOUT_S,
        max_redirects=settings.FETCH_MAX_REDIRECTS,
        user_agent=settings.FETCH_USER_AGENT,
    )
    result = await fetch_fn(
        url,
        timeout_s=settings.FETCH_TIMEOUT_S,
        max_bytes=settings.FETCH_MAX_BYTES,
        max_redirects=settings.FETCH_MAX_REDIRECTS,
        max_chars=settings.FETCH_MAX_CHARS,
        user_agent=settings.FETCH_USER_AGENT,
        allowed_domains=None,  # /read accepts any public domain (plan section 5)
        robots=robots,
    )

    if isinstance(result, FetchFailure):
        # A failed fetch still gets a clip row -- study_clip.fetch_error
        # exists for exactly this (plan section 4).
        session.add(
            StudyClip(
                job_id=job.id,
                url=url,
                domain=result.domain or "",
                http_status=result.http_status,
                fetch_error=result.error,
            )
        )
        job.status = "failed"
        job.error_code = result.error
        job.finished_at = clock.now_utc()
        await session.commit()
        logger.info(
            "read job failed at fetch",
            extra={"job_id": job.id, "domain": result.domain or "", "error_code": result.error},
        )
        return ResearchOutcome(
            job_id=job.id, status="failed", error_code=result.error,
            visible_cards=0, hidden_cards=0,
        )

    clip: Clip = result
    study_clip = StudyClip(
        job_id=job.id,
        url=clip.url,
        domain=clip.domain,
        title=clip.title,
        text=clip.text,
        text_sha256=clip.text_sha256,
        http_status=clip.http_status,
        fetched_at=clock.now_utc(),
    )
    session.add(study_clip)
    await session.flush()  # need study_clip.id before distill.call

    # Re-check both caps before the one call that costs money (plan
    # section 12: "the job stops, becomes failed:cap, and keeps any
    # cards already produced" -- there are none yet, but the clip stays).
    if await check_cap(session, settings, clock, timezone) or _job_cap_hit(job, settings):
        job.status = "failed"
        job.error_code = CAP
        job.finished_at = clock.now_utc()
        await session.commit()
        logger.info(
            "read job stopped at cap",
            extra={"job_id": job.id, "clip_id": study_clip.id},
        )
        return ResearchOutcome(
            job_id=job.id, status="failed", error_code=CAP,
            visible_cards=0, hidden_cards=0,
        )

    job.status = "distilling"
    await session.commit()

    response = await distill.call(
        provider,
        topic=_topic_for(study_clip),
        title=study_clip.title,
        text=study_clip.text,
        clip_id=study_clip.id,
        min_cards=settings.RESEARCH_CARDS_MIN,
        max_cards=settings.RESEARCH_CARDS_MAX,
    )

    cost = priced(response.usage, settings, model=response.model)
    session.add(
        SpendLedger(
            local_date=clock_module.local_date(clock, timezone),
            category=distill.RESEARCH_CATEGORY,
            model=response.model,
            tokens_in=response.usage.input_tokens,
            tokens_cached=response.usage.cached_tokens,
            tokens_out=response.usage.output_tokens,
            usd_cost=cost.usd,
            cost_source=cost.source,
        )
    )
    job.usd_cost = job.usd_cost + cost.usd
    await session.commit()

    payload = distill.parse_json(response.text)
    distilled = distill.validate(payload, clip_text=study_clip.text, max_cards=settings.RESEARCH_CARDS_MAX)

    visible = 0
    hidden = 0
    for card in distilled.cards:
        session.add(
            StudyCard(
                job_id=job.id,
                clip_id=study_clip.id,
                kind=card.kind,
                text=card.text,
                quote=card.quote,
                # Set by code, never from model output (plan section 12).
                source_url=study_clip.url,
                risk_model=card.risk_model,
                risk_rules=card.risk_rules,
                risk_final=card.risk_final,
                rule_hits=list(card.rule_hits),
                status="hidden" if card.hidden else "pending",
            )
        )
        if card.hidden:
            hidden += 1
        else:
            visible += 1

    job.status = "done"
    job.pins_used = 1
    job.finished_at = clock.now_utc()
    await session.commit()

    logger.info(
        "read job done",
        extra={
            "job_id": job.id,
            "clip_id": study_clip.id,
            "domain": study_clip.domain,
            "cards": visible + hidden,
            "dropped": sum(distilled.dropped.values()),
            "usd_cost": str(job.usd_cost),
        },
    )
    # Zero surviving cards is still `done` with error_code=None (plan
    # section 7); the wording for "nothing useful" belongs to whichever
    # command sends the completion message, not to this module.
    return ResearchOutcome(
        job_id=job.id, status="done", error_code=None,
        visible_cards=visible, hidden_cards=hidden,
    )


__all__ = [
    "BAD_URL",
    "CAP",
    "DISABLED",
    "QUOTA",
    "READ",
    "RESEARCH",
    "STUDY",
    "ResearchOutcome",
    "enqueue_read",
    "run_research_job",
]
