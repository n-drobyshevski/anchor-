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

import datetime
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
from app.research import distill, search
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
# 4c, /study only.
UNKNOWN_PACKET = "unknown_packet"
EMPTY_PACKET = "empty_packet"
TOPIC_TOO_LONG = "topic_too_long"
EMPTY_TOPIC = "empty_topic"

# The three packet names /study accepts (plan section 9). Not a config
# value: the names are the command's vocabulary, and only the domains
# behind each one are the user's to set.
PACKETS = ("forums", "guides", "ref")

# ck_study_job_query_length. A /study topic goes in `query`, and a topic
# past this is refused at enqueue rather than truncated -- truncation
# would search for something the user did not ask about.
QUERY_MAX = 200

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


def packet_domains(settings: Settings, packet: str | None) -> tuple[str, ...] | None:
    """The allowlist behind a packet name, or None if the name is unknown.

    An empty tuple is a *known* packet with nothing configured, which is
    a different refusal: plan section 9 answers «Пакеты: forums, guides,
    ref.» to a name it does not recognise and «Пакет guides пока не
    настроен.» to one it recognises and cannot use.
    """
    if packet not in PACKETS:
        return None
    return {
        "forums": settings.PACKET_FORUMS,
        "guides": settings.PACKET_GUIDES,
        "ref": settings.PACKET_REF,
    }[packet]


async def _quota_used(session: AsyncSession, kind: str, local_date) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(StudyJob)
        .where(StudyJob.kind == kind, StudyJob.local_date == local_date)
    )
    return result.scalar_one()


async def enqueue_study(
    session: AsyncSession,
    settings: Settings,
    clock: Clock,
    *,
    timezone: str,
    packet: str,
    topic: str,
) -> tuple[int | None, str | None]:
    """Queue a /study job, or refuse it (plan section 9's /study row).

    Checks in the order the plan lists them: disabled, the packet name,
    the packet's contents, the daily quota, then the spend cap.

    **The quota is per kind.** `/study` and `/read` have separate daily
    allowances (RESEARCH_JOBS_PER_DAY and RESEARCH_READS_PER_DAY,
    plan section 3), because they cost differently -- a study job is
    one or two searches plus two distills, a read is one distill on a
    page the user already chose. Using up one must not consume the
    other.
    """
    if not settings.RESEARCH_ENABLED:
        return None, DISABLED

    domains = packet_domains(settings, packet)
    if domains is None:
        return None, UNKNOWN_PACKET
    if not domains:
        return None, EMPTY_PACKET

    topic = topic.strip()
    if not topic:
        return None, EMPTY_TOPIC
    if len(topic) > QUERY_MAX:
        return None, TOPIC_TOO_LONG

    local_date = clock_module.local_date(clock, timezone)
    if await _quota_used(session, STUDY, local_date) >= settings.RESEARCH_JOBS_PER_DAY:
        return None, QUOTA
    if await check_cap(session, settings, clock, timezone):
        return None, CAP

    job = StudyJob(
        kind=STUDY, status="queued", packet=packet, query=topic, local_date=local_date
    )
    session.add(job)
    await session.flush()
    await enqueue_job(session, RESEARCH, {"job_id": job.id}, dedup_key=f"research:{job.id}")
    logger.info("study job queued", extra={"job_id": job.id})
    return job.id, None


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


async def _recent_clip_urls(session: AsyncSession, clock: Clock, *, days: int = 30) -> set[str]:
    """URLs already clipped recently, for search's dedupe (plan section 6).

    Re-reading a page we read last week spends a fetch and a distill to
    produce cards the user has already seen and already decided about.
    Matched on `study_clip.url`, which `app/research/addresses.py`
    normalised on the way in, so two spellings of one page count as one.

    Failed fetches are included deliberately: a page that refused us on
    Monday is not a better candidate on Tuesday, and retrying it would
    burn the job's one or two pins on the same refusal.
    """
    since = clock.now_utc() - datetime.timedelta(days=days)
    result = await session.execute(
        select(StudyClip.url).where(StudyClip.fetched_at.is_(None) | (StudyClip.fetched_at >= since))
    )
    return set(result.scalars().all())


async def _ledger(
    session: AsyncSession,
    settings: Settings,
    clock: Clock,
    timezone: str,
    job: StudyJob,
    response,
) -> None:
    """Record one provider call against the day and against the job.

    Every call, including a search that found nothing usable: it still
    cost a plugin fee and a promptful of input tokens, and an accounting
    that only counted useful calls would under-report exactly the runs
    worth noticing.

    The search plugin's fee is inside `usage.cost` rather than added
    here -- OpenRouter charges it to the same credits and reports
    `cost` as "the total amount charged to your account" (H4, and
    phase-4 plan section 6: "the fee is taken from the provider-reported
    cost"). `cost_source` on the row records which branch priced it, so
    a run that fell back to the token formula is visible as one that
    under-counts the fee rather than silently wrong.
    """
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


async def _capped(
    session: AsyncSession, settings: Settings, clock: Clock, timezone: str, job: StudyJob
) -> bool:
    """Either budget exhausted? Checked before every call that costs money."""
    return await check_cap(session, settings, clock, timezone) or _job_cap_hit(job, settings)


async def _fetch_into_clip(
    session: AsyncSession,
    settings: Settings,
    clock: Clock,
    job: StudyJob,
    url: str,
    *,
    allowed_domains,
    robots,
    fetch_fn,
) -> tuple[StudyClip, str | None]:
    """Fetch one URL and write its clip row. `(clip, error_code)`.

    A failed fetch still gets a row -- `study_clip.fetch_error` exists
    for exactly that (plan section 4), and it is also what makes "Reddit
    blocked us" a finding the user can be told about rather than an
    absence they have to infer.
    """
    result = await fetch_fn(
        url,
        timeout_s=settings.FETCH_TIMEOUT_S,
        max_bytes=settings.FETCH_MAX_BYTES,
        max_redirects=settings.FETCH_MAX_REDIRECTS,
        max_chars=settings.FETCH_MAX_CHARS,
        user_agent=settings.FETCH_USER_AGENT,
        allowed_domains=allowed_domains,
        robots=robots,
    )
    if isinstance(result, FetchFailure):
        clip = StudyClip(
            job_id=job.id,
            url=url,
            domain=result.domain or "",
            http_status=result.http_status,
            fetch_error=result.error,
        )
        session.add(clip)
        await session.flush()
        return clip, result.error

    fetched: Clip = result
    clip = StudyClip(
        job_id=job.id,
        url=fetched.url,
        domain=fetched.domain,
        title=fetched.title,
        text=fetched.text,
        text_sha256=fetched.text_sha256,
        http_status=fetched.http_status,
        fetched_at=clock.now_utc(),
    )
    session.add(clip)
    await session.flush()
    return clip, None


async def _distill_into_cards(
    session: AsyncSession,
    settings: Settings,
    provider: LLMProvider,
    clock: Clock,
    timezone: str,
    job: StudyJob,
    clip: StudyClip,
    *,
    topic: str,
) -> tuple[int, int]:
    """One distill call over one clip, then the cards it earned.

    Returns `(visible, hidden)`. Zero of both is an ordinary outcome:
    a page can simply have nothing in it worth a card, and plan section
    7 says that is `done`, not a failure.
    """
    response = await distill.call(
        provider,
        topic=topic,
        title=clip.title,
        text=clip.text,
        clip_id=clip.id,
        min_cards=settings.RESEARCH_CARDS_MIN,
        max_cards=settings.RESEARCH_CARDS_MAX,
    )
    await _ledger(session, settings, clock, timezone, job, response)
    await session.commit()

    payload = distill.parse_json(response.text)
    distilled = distill.validate(
        payload, clip_text=clip.text, max_cards=settings.RESEARCH_CARDS_MAX
    )

    visible = hidden = 0
    for card in distilled.cards:
        session.add(
            StudyCard(
                job_id=job.id,
                clip_id=clip.id,
                kind=card.kind,
                text=card.text,
                quote=card.quote,
                # Set by code, never from model output (plan section 12).
                source_url=clip.url,
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
    logger.info(
        "clip distilled",
        extra={
            "job_id": job.id,
            "clip_id": clip.id,
            "domain": clip.domain,
            "cards": visible + hidden,
            "dropped": sum(distilled.dropped.values()),
        },
    )
    return visible, hidden


async def _finish(
    session: AsyncSession,
    clock: Clock,
    job: StudyJob,
    *,
    status: str,
    error_code: str | None,
    visible: int,
    hidden: int,
) -> ResearchOutcome:
    job.status = status
    job.error_code = error_code
    job.finished_at = clock.now_utc()
    await session.commit()
    logger.info(
        "research job finished",
        extra={
            "job_id": job.id,
            "kind": job.kind,
            "error_code": error_code or "",
            "cards": visible + hidden,
            "usd_cost": str(job.usd_cost),
        },
    )
    return ResearchOutcome(
        job_id=job.id,
        status=status,
        error_code=error_code,
        visible_cards=visible,
        hidden_cards=hidden,
    )


async def run_research_job(
    session: AsyncSession,
    settings: Settings,
    provider: LLMProvider,
    *,
    job_id: int,
    url: str | None = None,
    clock: Clock,
    timezone: str,
    fetch_fn=None,
    search_fn=None,
) -> ResearchOutcome:
    """Run one research job to completion.

    Two shapes behind one entry point. A `/read` job fetches the single
    URL it was given and distills it. A `/study` job searches for
    candidates first (plan section 6), then fetches and distills up to
    `RESEARCH_MAX_PINS` of them.

    `url` comes from the queue row's payload and is required for a
    `/read` -- see the module docstring on where a /read URL lives. A
    `/study` job carries its topic in `study_job.query` and its
    allowlist in `study_job.packet`, so it needs neither.

    Idempotent: a job not found, or not `status='queued'`, is reported
    on without doing any work -- the queue can redeliver a completed
    job (a worker restart between `complete_job` and its ack, say) and
    this must not fetch or spend twice.

    `fetch_fn` and `search_fn` default to the real implementations and
    exist purely as test seams (plan section 14: "no real network").
    """
    fetch_fn = fetch_fn or default_fetch
    search_fn = search_fn or search.find_urls

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

    robots = make_robots_cache(
        timeout_s=settings.FETCH_TIMEOUT_S,
        max_redirects=settings.FETCH_MAX_REDIRECTS,
        user_agent=settings.FETCH_USER_AGENT,
    )
    if job.kind == READ:
        if not url:
            return await _finish(
                session, clock, job, status="failed", error_code=BAD_URL, visible=0, hidden=0
            )
        return await _run_read(
            session, settings, provider, clock, timezone, job, url,
            robots=robots, fetch_fn=fetch_fn,
        )
    return await _run_study(
        session, settings, provider, clock, timezone, job,
        robots=robots, fetch_fn=fetch_fn, search_fn=search_fn,
    )


async def _run_read(
    session, settings, provider, clock, timezone, job, url, *, robots, fetch_fn
) -> ResearchOutcome:
    """One URL the user chose: fetch it, distill it, done.

    `allowed_domains=None` -- a /read accepts any public domain the user
    supplies (plan section 5). The address and robots rules still apply.
    """
    job.status = "fetching"
    await session.commit()

    clip, error = await _fetch_into_clip(
        session, settings, clock, job, url,
        allowed_domains=None, robots=robots, fetch_fn=fetch_fn,
    )
    if error is not None:
        return await _finish(
            session, clock, job, status="failed", error_code=error, visible=0, hidden=0
        )

    if await _capped(session, settings, clock, timezone, job):
        return await _finish(
            session, clock, job, status="failed", error_code=CAP, visible=0, hidden=0
        )

    job.status = "distilling"
    await session.commit()
    visible, hidden = await _distill_into_cards(
        session, settings, provider, clock, timezone, job, clip, topic=_topic_for(clip)
    )
    job.pins_used = 1
    return await _finish(
        session, clock, job, status="done", error_code=None, visible=visible, hidden=hidden
    )


async def _run_study(
    session, settings, provider, clock, timezone, job, *, robots, fetch_fn, search_fn
) -> ResearchOutcome:
    """Search for candidates, then read up to RESEARCH_MAX_PINS of them.

    **A candidate that refuses us is not the end of the job.** Plan
    section 5.9 forbids working around a refusal, not noticing it: the
    clip row records the code and the loop moves to the next candidate,
    because a packet of five results whose first entry disallows robots
    should still produce cards from the other four. What it must not do
    is try forever -- so `pins_used` counts pages actually read and
    stops at the cap, while the candidate list itself is what bounds the
    attempts.

    When every candidate refused us, the job fails with the *last*
    refusal code, so the completion message can say which wall we hit
    rather than "nothing found" -- the acceptance checklist asks for
    "reports clearly that Reddit blocked the fetch", and an empty
    result would not be that report.
    """
    allowed = packet_domains(settings, job.packet)
    if not allowed:
        # Only reachable if the packet was emptied in config between
        # enqueue and run; enqueue_study refuses this case outright.
        return await _finish(
            session, clock, job, status="failed", error_code=EMPTY_PACKET, visible=0, hidden=0
        )

    job.status = "searching"
    await session.commit()

    if await _capped(session, settings, clock, timezone, job):
        return await _finish(
            session, clock, job, status="failed", error_code=CAP, visible=0, hidden=0
        )

    recent = await _recent_clip_urls(session, clock)
    outcome = await search_fn(
        provider,
        topic=job.query or "",
        allowed_domains=allowed,
        recent_urls=recent,
        job_id=job.id,
        max_calls=settings.RESEARCH_MAX_SEARCHES,
    )
    for response in outcome.responses:
        await _ledger(session, settings, clock, timezone, job, response)
    job.searches_used = outcome.calls
    await session.commit()

    if outcome.error_code is not None or not outcome.urls:
        return await _finish(
            session, clock, job,
            status="failed", error_code=outcome.error_code or search.NO_RESULTS,
            visible=0, hidden=0,
        )

    job.status = "fetching"
    await session.commit()

    visible = hidden = 0
    last_error: str | None = None
    for candidate in outcome.urls:
        if job.pins_used >= settings.RESEARCH_MAX_PINS:
            break
        if await _capped(session, settings, clock, timezone, job):
            # Plan section 12: the job stops, becomes failed:cap, and
            # keeps any cards already produced.
            return await _finish(
                session, clock, job, status="failed", error_code=CAP,
                visible=visible, hidden=hidden,
            )
        clip, error = await _fetch_into_clip(
            session, settings, clock, job, candidate,
            allowed_domains=allowed, robots=robots, fetch_fn=fetch_fn,
        )
        if error is not None:
            last_error = error
            await session.commit()
            continue

        job.pins_used = job.pins_used + 1
        job.status = "distilling"
        await session.commit()
        got_visible, got_hidden = await _distill_into_cards(
            session, settings, provider, clock, timezone, job, clip, topic=job.query or ""
        )
        visible += got_visible
        hidden += got_hidden

    if job.pins_used == 0:
        # Every candidate refused us. Report the wall, not an absence.
        return await _finish(
            session, clock, job, status="failed", error_code=last_error or search.NO_RESULTS,
            visible=0, hidden=0,
        )
    return await _finish(
        session, clock, job, status="done", error_code=None, visible=visible, hidden=hidden
    )




__all__ = [
    "BAD_URL",
    "CAP",
    "DISABLED",
    "EMPTY_PACKET",
    "EMPTY_TOPIC",
    "PACKETS",
    "QUERY_MAX",
    "QUOTA",
    "READ",
    "RESEARCH",
    "STUDY",
    "TOPIC_TOO_LONG",
    "UNKNOWN_PACKET",
    "ResearchOutcome",
    "enqueue_read",
    "enqueue_study",
    "packet_domains",
    "run_research_job",
]
