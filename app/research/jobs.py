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

**Lens L4: gap-seeded research** (anchor-lens-plan.md section 9; the L4
spec sections 1-4 with the owner's amendments). A tap on «исследовать и написать»
under a lens garden gap calls `enqueue_lens_study`: the same checks as
/study, in the same order, then a `kind='study'`, `packet='lens'` job
with `lens_gap_id` set and **no queue row** -- nothing here reaches
Telegram, and `/study`'s completion message never fires for it. The
idle kind `lens_research` takes the oldest (`next_lens_job`), has its
query built from the gap (app/research/lens_query.py) and charged to
the job (`record_lens_query`), then runs `run_research_job` over
`PACKET_LENS`. The finished job's result goes out as its own Telegram
message (app/worker.py's garden hook: `unsent_lens_results`, then
`mark_offered`); its cards are `kind='lens'`, never a memory, and are
adopted into the vault's inbox (`adopt_lens_cards`, from
app/core/echo_write.py) or declined (`reject_lens_cards`). A lens job
still unfinished after `LENS_STALE_DAYS` fails as `stale`
(`fail_stale_lens_jobs`): idle may be off, and the gap must not wait
forever. Nothing here imports the lens module: a job knows its gap only
by id.

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

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core import clock as clock_module
from app.core import safety_events
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

# study_job.status, set by app/core/purge.py when /delete runs.
CANCELLED = "cancelled"

# L4 (module docstring): the packet of a gap-seeded lens research. Not
# one of PACKETS: `/study lens` stays refused.
LENS = "lens"
# study_card.kind of a lens research's cards, set by code.
LENS_CARD = "lens"
# error_code values a lens job can end with besides the pipeline's own:
# the gap went away before the query was built, the query was refused
# by lens_query.validate (nothing was searched), or the job waited too
# long to run.
GAP_GONE = "gap_gone"
QUERY_REFUSED = "query_refused"
STALE = "stale"
LENS_STALE_DAYS = 3
# lens_job_outcomes / unsent_lens_results.
RUNNING = "running"
READY = "ready"
SPENT = "spent"
ADOPTED = "adopted"

# A job in one of these has finished and will never do more work. Every
# other status is a job that was mid-run when something stopped it.
TERMINAL_STATUSES = ("done", "failed", CANCELLED)

# error_code for a job whose process died mid-run. Not a refusal code --
# nothing refused it, the worker simply went away.
INTERRUPTED = "interrupted"

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


def _job_domains(settings: Settings, packet: str | None) -> tuple[str, ...] | None:
    """The allowlist a queued study job searches and fetches within: its
    packet's, or -- L4 -- `PACKET_LENS` for a lens job (the L4 spec
    section 3). Not folded into `packet_domains`, which is /study's
    vocabulary: `/study lens` stays refused (`PACKETS` is unchanged)."""
    if packet == LENS:
        return settings.PACKET_LENS
    return packet_domains(settings, packet)


async def _quota_used(session: AsyncSession, kind: str, local_date) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(StudyJob)
        .where(StudyJob.kind == kind, StudyJob.local_date == local_date)
    )
    return result.scalar_one()


async def used_today(session: AsyncSession, local_date) -> dict[str, int]:
    """Today's `/study` jobs and `/read` reads: the counts the two quotas
    (RESEARCH_JOBS_PER_DAY, RESEARCH_READS_PER_DAY) check, for display."""
    return {STUDY: await _quota_used(session, STUDY, local_date), READ: await _read_quota_used(session, local_date)}


async def study_quota_used(
    session: AsyncSession, settings: Settings, clock: Clock, timezone: str
) -> bool:
    """True iff today's `/study` quota (`RESEARCH_JOBS_PER_DAY`) is already spent.

    Phase 6 plan section 6.5: idle `research` "shares the daily research
    quota: it runs only if the user hasn't used their /study today". The
    counter this reads -- `study_job` rows of kind `STUDY` for today's
    local date -- is the same one `enqueue_study`'s own check below
    counts, and idle research writes its `StudyJob` row into the same
    table (app/core/idle/research.py), never a parallel counter. That is
    what makes the sharing symmetric: whichever of the two runs first,
    the other sees this same query return `True` for the rest of the
    day, with nothing idle-specific for a plain `/study` to know about.
    """
    local_date = clock_module.local_date(clock, timezone)
    return await _quota_used(session, STUDY, local_date) >= settings.RESEARCH_JOBS_PER_DAY


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

    **Both branches are bounded, and the second one needs a join to be.**
    `fetched_at` is set only on a *successful* fetch, so a failed clip
    keeps it NULL forever and `study_clip` has no `created_at` of its
    own. Filtering on `fetched_at IS NULL` alone -- which is what this
    did until the 4d fixes -- excluded a URL from every future /study
    permanently after one failure, including a transient timeout, and
    grew the result set without bound for the life of the deployment.
    The parent job's `created_at` is the timestamp the clip does not
    have, and it is already there.

    `study_job.created_at` is stamped by the database
    (`server_default=func.now()`) while `since` comes from the injected
    clock. That is the same looseness app/research/sweeps.py documents
    and accepts for `study_card.created_at`, for the same reason: in
    production both are the same physical clock, and a test that cares
    sets `created_at` explicitly.
    """
    since = clock.now_utc() - datetime.timedelta(days=days)
    result = await session.execute(
        select(StudyClip.url)
        .join(StudyJob, StudyClip.job_id == StudyJob.id)
        .where(
            (StudyClip.fetched_at >= since)
            | (StudyClip.fetched_at.is_(None) & (StudyJob.created_at >= since))
        )
    )
    return set(result.scalars().all())


async def _still_ours(session: AsyncSession, job_id: int, created_at) -> bool:
    """False once /delete has cancelled or purged this job out from under us.

    `run_research_job` reads `study_job.status` once, at the top, which
    is enough to stop a job that has not started. It is not enough for
    one already mid-fetch or mid-distill: `app/core/purge.py`'s
    `cancel_research_jobs` flips the row to `'cancelled'` and the
    TRUNCATE then removes it, while this coroutine carries on and
    writes clips and cards for a job that no longer exists.

    Most of the time that write simply fails on the foreign key, which
    is noisy but harmless. The case worth closing is narrower and
    worse: `/delete` uses RESTART IDENTITY, so a *new* study_job
    created after the purge takes id 1 again, and a stray write from
    the old run attaches its clips and cards to it. The user would see
    cards in /notes from a job they deleted.

    So the row is re-read before every write-heavy step, and **the
    check is on `created_at` as well as on status**. Status alone would
    miss exactly the case worth closing: a job created after the purge
    is `'queued'`, not `'cancelled'`, so a status-only check would wave
    the old run straight through into the new job's rows. `created_at`
    is what tells the two apart.

    A missing row counts as cancelled too -- if the job is gone,
    whatever we were doing for it is no longer wanted.

    Re-read rather than refreshed: the purge commits in another
    transaction, and this module commits between steps, so a fresh
    query is the only thing guaranteed to see it.

    Not a complete guard, and deliberately not claimed as one. A row
    that vanishes *between* this check and the write it guards still
    produces a foreign-key violation on the flush -- noisy, and safe:
    the write is refused rather than misfiled, which is the property
    that matters.
    """
    row = (
        await session.execute(
            select(StudyJob.status, StudyJob.created_at).where(StudyJob.id == job_id)
        )
    ).first()
    if row is None:
        return False
    status, current_created_at = row
    return status != CANCELLED and current_created_at == created_at


def _cancelled_outcome(job_id: int, visible: int, hidden: int) -> ResearchOutcome:
    """Stop without touching the database -- the row may already be gone."""
    logger.info("research job cancelled mid-run", extra={"job_id": job_id})
    return ResearchOutcome(
        job_id=job_id,
        status=CANCELLED,
        error_code=None,
        visible_cards=visible,
        hidden_cards=hidden,
    )


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

    L4: a lens job (`packet == LENS`) distills in lens mode, its query
    as the question, and its cards are `LENS_CARD` -- set here, by
    code, never from the reply (the L4 spec section 4). Keyed on the
    packet, not `lens_gap_id`, which SET NULL may have cleared.
    """
    mode = distill.LENS if job.packet == LENS else distill.STUDY
    response = await distill.call(
        provider,
        topic=topic,
        title=clip.title,
        text=clip.text,
        clip_id=clip.id,
        min_cards=settings.RESEARCH_CARDS_MIN,
        max_cards=settings.RESEARCH_CARDS_MAX,
        mode=mode,
    )
    await _ledger(session, settings, clock, timezone, job, response)
    await session.commit()

    payload = distill.parse_json(response.text)
    # H2's table, widened for phase 4. Without this a distiller that has
    # started returning unparseable JSON produces `done` jobs with zero
    # cards, over and over, which looks exactly like a run of genuinely
    # unhelpful pages. Staged in this job's transaction the way
    # app/core/extract.py stages its own, and never raising -- an
    # observability row must not be able to fail the job it observes.
    await safety_events.record_in(
        session,
        clock=clock,
        timezone=timezone,
        kind=safety_events.DISTILL,
        outcome=safety_events.PARSE_FAIL if payload is None else "ok",
        model=response.model,
    )
    await session.commit()

    distilled = distill.validate(
        payload, clip_text=clip.text, max_cards=settings.RESEARCH_CARDS_MAX, mode=mode
    )

    visible = hidden = 0
    for card in distilled.cards:
        session.add(
            StudyCard(
                job_id=job.id,
                clip_id=clip.id,
                # Set by code: a lens job's cards are LENS_CARD whatever
                # the reply said (distill.validate sets it too).
                kind=LENS_CARD if mode == distill.LENS else card.kind,
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
    if job.status in TERMINAL_STATUSES:
        # An ordinary redelivery of a job that already finished -- a
        # worker restart between complete_job and its ack, say. Report
        # what it produced and touch nothing.
        visible, hidden = await _card_counts(session, job.id)
        return ResearchOutcome(
            job_id=job.id, status=job.status, error_code=job.error_code,
            visible_cards=visible, hidden_cards=hidden,
        )
    if job.status != "queued":
        # searching/fetching/distilling: this job was mid-run when its
        # process died. app/db/queue.py's recover_stuck_jobs returned
        # the *queue* row to pending after STUCK_AFTER and it has been
        # redelivered; nothing ever moved `study_job` out of its
        # intermediate status, and nothing ever would.
        #
        # Before 4d this fell into the branch above and was reported as
        # "already finished", which made the queue row complete and left
        # the study_job wedged forever -- while the user was told
        # «Не получилось: техническая проблема», a failure the database
        # had no record of.
        #
        # **Failed, not resumed, on purpose.** A resume would re-run a
        # search or a distill that may already have been paid for, and
        # /study's candidate loop has no resume point to start from.
        # Failing is honest and costs nothing; plan section 12 already
        # settles a job stopped mid-flight the same way (`failed:cap`
        # keeps whatever cards it made). The daily quota is not refunded
        # -- the money may genuinely be gone.
        #
        # A redelivery cannot be a job still running elsewhere:
        # recover_stuck_jobs waits STUCK_AFTER (5 minutes) before it
        # returns a claimed row to the queue at all.
        visible, hidden = await _card_counts(session, job.id)
        job.status = "failed"
        job.error_code = INTERRUPTED
        job.finished_at = clock.now_utc()
        await session.commit()
        logger.info(
            "research job was interrupted mid-run",
            extra={"job_id": job.id, "kind": job.kind, "error_code": INTERRUPTED,
                   "cards": visible + hidden},
        )
        return ResearchOutcome(
            job_id=job.id, status="failed", error_code=INTERRUPTED,
            visible_cards=visible, hidden_cards=hidden,
        )

    # The flag was on when this job was queued and may be off now. A job
    # never outlives the switch: finish it as refused before any fetch,
    # search or model call. Enqueue already refuses while it is off
    # (enqueue_study/enqueue_read), so this only catches the gap between.
    if not settings.RESEARCH_ENABLED:
        return await _finish(
            session, clock, job, status="failed", error_code=DISABLED, visible=0, hidden=0
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
    # Captured before anything commits, so a later check can tell this
    # job from a new one that reused its id after a purge.
    job_created_at = job.created_at

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

    if not await _still_ours(session, job.id, job_created_at):
        return _cancelled_outcome(job.id, 0, 0)
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
    allowed = _job_domains(settings, job.packet)
    if not allowed:
        # Only reachable if the packet was emptied in config between
        # enqueue and run; enqueue_study refuses this case outright.
        return await _finish(
            session, clock, job, status="failed", error_code=EMPTY_PACKET, visible=0, hidden=0
        )
    if job.packet == LENS and not job.query:
        # L4: a lens job searches only for the query its idle kind built
        # and stored (record_lens_query). Never reached through that
        # kind, which runs the pipeline only once a query is stored; a
        # lens job has no queue row for app/worker.py to run it by.
        return await _finish(
            session, clock, job, status="failed", error_code=QUERY_REFUSED, visible=0, hidden=0
        )

    job_created_at = job.created_at

    job.status = "searching"
    await session.commit()

    if await _capped(session, settings, clock, timezone, job):
        return await _finish(
            session, clock, job, status="failed", error_code=CAP, visible=0, hidden=0
        )

    if not await _still_ours(session, job.id, job_created_at):
        return _cancelled_outcome(job.id, 0, 0)
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
    if outcome.responses:
        # `error`, not `parse_fail`: a search parses nothing. Coming back
        # with no usable URL after both attempts means the plugin did not
        # do its job, which is what `error` has always meant in this
        # table. A search that was never made (no budget left) records
        # nothing -- there is no call to have an outcome.
        await safety_events.record_in(
            session,
            clock=clock,
            timezone=timezone,
            kind=safety_events.SEARCH,
            outcome="error" if not outcome.urls else "ok",
            model=outcome.responses[-1].model,
        )
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
        # Re-read per candidate rather than once: a /study job makes
        # several round trips and is the one most likely to still be
        # running when /delete lands.
        if not await _still_ours(session, job.id, job_created_at):
            return _cancelled_outcome(job.id, visible, hidden)
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

        if not await _still_ours(session, job.id, job_created_at):
            return _cancelled_outcome(job.id, visible, hidden)
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


# --- L4: gap-seeded lens research (module docstring) ---------------------------------


@dataclass(frozen=True)
class LensJob:
    """A queued lens research, as the idle kind takes it: the gap it is
    for (None once the garden is gone) and its query (None until built)."""

    id: int
    gap_id: int | None
    query: str | None


@dataclass(frozen=True)
class LensCard:
    """One visible, pending lens card: its text, its verbatim quote, and
    the page it came from (the URL and its domain, both set by code from
    the fetched clip, never by the model)."""

    id: int
    text: str
    quote: str
    source_url: str
    domain: str


@dataclass(frozen=True)
class LensCards:
    """A gap's research, as the result message and «в Inbox» need it: its
    visible pending cards by id, and how many were hidden (high risk)."""

    gap_id: int | None
    job_id: int
    cards: tuple[LensCard, ...]
    hidden: int


@dataclass(frozen=True)
class LensResult:
    """A finished lens research whose result message has not gone out:
    `READY` with at least one visible pending card, else `SPENT` (it
    failed, found nothing, or every card was hidden). `gap_id` is None
    when the garden went away meanwhile: nothing to send, only to mark."""

    job_id: int
    gap_id: int | None
    outcome: str
    cards: tuple[LensCard, ...]
    hidden: int


def lens_research_enabled(settings: Settings) -> bool:
    """Every switch lens research needs (the L4 spec section 1): research
    itself, the lens and its garden (the gap comes from there), and idle
    (the only thing that runs the job). With any one off, a tap would
    spend the day's quota on a job that never runs."""
    return (
        settings.RESEARCH_ENABLED
        and settings.LENS_ENABLED
        and settings.LENS_GARDEN_ENABLED
        and settings.IDLE_ENABLED
    )


async def enqueue_lens_study(
    session: AsyncSession,
    settings: Settings,
    clock: Clock,
    *,
    gap_id: int,
    timezone: str,
) -> tuple[int | None, str | None]:
    """Queue a lens research for one gap, or refuse it: `DISABLED`,
    `EMPTY_PACKET`, `QUOTA`, `CAP`, in `enqueue_study`'s order. The quota
    is /study's own (the job is `kind='study'`), shared with idle
    research, and spent at the tap. No queue row: the idle kind
    `lens_research` runs it. The caller has already moved the gap to
    `researched` in this transaction and rolls both back on a refusal.
    Flushes, never commits. Returns `(job_id, None)` or `(None, code)`."""
    if not lens_research_enabled(settings):
        return None, DISABLED
    if not settings.PACKET_LENS:
        return None, EMPTY_PACKET
    local_date = clock_module.local_date(clock, timezone)
    if await _quota_used(session, STUDY, local_date) >= settings.RESEARCH_JOBS_PER_DAY:
        return None, QUOTA
    if await check_cap(session, settings, clock, timezone):
        return None, CAP
    job = StudyJob(
        kind=STUDY, status="queued", packet=LENS, query=None, lens_gap_id=gap_id, local_date=local_date
    )
    session.add(job)
    await session.flush()
    # The job's id only, never its gap's (the L4 spec section 6): the job
    # id joins debug.study_*, and the pair would link a gap to its research.
    logger.info("lens study job queued", extra={"job_id": job.id})
    return job.id, None


async def next_lens_job(session: AsyncSession) -> LensJob | None:
    """The oldest queued lens research, or None. The idle gate's fact and
    the job's own first step read the same answer."""
    row = (
        await session.execute(
            select(StudyJob.id, StudyJob.lens_gap_id, StudyJob.query)
            .where(StudyJob.packet == LENS, StudyJob.status == "queued")
            .order_by(StudyJob.id)
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    return LensJob(id=row[0], gap_id=row[1], query=row[2])


async def _queued_lens_job(session: AsyncSession, job_id: int) -> StudyJob | None:
    job = await session.get(StudyJob, job_id)
    if job is None or job.packet != LENS or job.status != "queued":
        return None
    return job


async def record_lens_query(
    session: AsyncSession,
    settings: Settings,
    clock: Clock,
    *,
    timezone: str,
    job_id: int,
    response,
    query: str | None,
) -> bool:
    """Charge the query call to the job and store its query (the L4 spec
    section 2). The call's cost goes to `spend_ledger` and onto the
    job's `usd_cost`, exactly as a search or a distill does, so
    `RESEARCH_JOB_USD_CAP` covers it. `query` is lens_query.validate's
    answer: None (or anything that is not one line of at most
    `QUERY_MAX` characters) fails the job as `QUERY_REFUSED`, and
    nothing is searched. True when the query was stored; False when it
    was refused, or the job is no longer a queued lens job (the call is
    then not charged to it). A stored query is never rebuilt. Flushes,
    never commits."""
    job = await _queued_lens_job(session, job_id)
    if job is None:
        return False
    await _ledger(session, settings, clock, timezone, job, response)
    cleaned = query.strip() if isinstance(query, str) else ""
    if not cleaned or len(cleaned) > QUERY_MAX or "\n" in cleaned or "\r" in cleaned:
        job.status = "failed"
        job.error_code = QUERY_REFUSED
        job.finished_at = clock.now_utc()
        await session.flush()
        logger.info(
            "lens study query refused", extra={"job_id": job.id, "error_code": QUERY_REFUSED}
        )
        return False
    job.query = cleaned
    await session.flush()
    logger.info("lens study query built", extra={"job_id": job.id, "usd_cost": str(job.usd_cost)})
    return True


async def fail_lens_job(session: AsyncSession, clock: Clock, job_id: int, error_code: str) -> bool:
    """End a queued lens job before it searched (`GAP_GONE`, or the spend
    cap before the query call). False when it is not a queued lens job.
    Flushes, never commits."""
    job = await _queued_lens_job(session, job_id)
    if job is None:
        return False
    job.status = "failed"
    job.error_code = error_code
    job.finished_at = clock.now_utc()
    await session.flush()
    logger.info("lens study job failed", extra={"job_id": job.id, "error_code": error_code})
    return True


async def fail_stale_lens_jobs(session: AsyncSession, now: datetime.datetime) -> int:
    """Every lens job still unfinished `LENS_STALE_DAYS` after it was
    queued fails as `STALE` (the owner's amendment (b)): idle may be off,
    or the job was stuck, and the gap must not wait forever. Its result
    message («ничего не нашлось») then goes out like any spent job's.
    Returns how many. Flushes, never commits."""
    cutoff = now - datetime.timedelta(days=LENS_STALE_DAYS)
    failed = (
        await session.execute(
            update(StudyJob)
            .where(
                StudyJob.packet == LENS,
                StudyJob.status.not_in(TERMINAL_STATUSES),
                StudyJob.created_at < cutoff,
            )
            .values(status="failed", error_code=STALE, finished_at=now)
            .returning(StudyJob.id)
        )
    ).scalars().all()
    await session.flush()
    if failed:
        logger.info("lens study jobs went stale", extra={"count": len(failed), "error_code": STALE})
    return len(failed)


async def _lens_cards(
    session: AsyncSession, job_ids: list[int]
) -> tuple[dict[int, list[LensCard]], dict[int, int]]:
    """(job id -> visible pending cards by id, job id -> hidden count)."""
    visible: dict[int, list[LensCard]] = {job_id: [] for job_id in job_ids}
    hidden: dict[int, int] = {job_id: 0 for job_id in job_ids}
    if not job_ids:
        return visible, hidden
    rows = await session.execute(
        select(
            StudyCard.id,
            StudyCard.job_id,
            StudyCard.status,
            StudyCard.text,
            StudyCard.quote,
            StudyCard.source_url,
            StudyClip.domain,
        )
        .join(StudyClip, StudyClip.id == StudyCard.clip_id)
        .where(StudyCard.job_id.in_(job_ids), StudyCard.kind == LENS_CARD)
        .order_by(StudyCard.id)
    )
    for card_id, job_id, status, card_text, quote, url, domain in rows:
        if status == "pending":
            visible[job_id].append(LensCard(card_id, card_text, quote, url, domain))
        elif status == "hidden":
            hidden[job_id] += 1
    return visible, hidden


async def lens_card_views(session: AsyncSession, gap_ids) -> dict[int, LensCards]:
    """gap id -> its research's visible pending cards and hidden count,
    for every given gap that has a lens job."""
    ids = sorted({int(i) for i in gap_ids})
    if not ids:
        return {}
    jobs = dict(
        (
            await session.execute(
                select(StudyJob.id, StudyJob.lens_gap_id).where(StudyJob.lens_gap_id.in_(ids))
            )
        ).all()
    )
    visible, hidden = await _lens_cards(session, list(jobs))
    return {
        gap_id: LensCards(
            gap_id=gap_id, job_id=job_id, cards=tuple(visible[job_id]), hidden=hidden[job_id]
        )
        for job_id, gap_id in jobs.items()
    }


async def lens_job_outcomes(session: AsyncSession) -> dict[int, str]:
    """gap id -> where its research stands: `RUNNING` (not finished),
    `READY` (finished, a visible card still pending), `ADOPTED` (its
    cards went into the inbox) or `SPENT` (finished with nothing left:
    it failed, found nothing, or every card was hidden, declined or
    expired). Gaps with no lens job are absent."""
    jobs = (
        await session.execute(
            select(StudyJob.id, StudyJob.lens_gap_id, StudyJob.status).where(
                StudyJob.lens_gap_id.is_not(None)
            )
        )
    ).all()
    if not jobs:
        return {}
    tallies: dict[int, dict[str, int]] = {}
    for job_id, status, count in await session.execute(
        select(StudyCard.job_id, StudyCard.status, func.count())
        .where(StudyCard.job_id.in_([row[0] for row in jobs]), StudyCard.kind == LENS_CARD)
        .group_by(StudyCard.job_id, StudyCard.status)
    ):
        tallies.setdefault(job_id, {})[status] = count
    outcomes: dict[int, str] = {}
    for job_id, gap_id, status in jobs:
        tally = tallies.get(job_id, {})
        if status not in TERMINAL_STATUSES:
            outcomes[gap_id] = RUNNING
        elif tally.get("adopted"):
            outcomes[gap_id] = ADOPTED
        elif tally.get("pending"):
            outcomes[gap_id] = READY
        else:
            outcomes[gap_id] = SPENT
    return outcomes


async def unsent_lens_results(session: AsyncSession) -> list[LensResult]:
    """Finished lens researches (`done` or `failed`) whose result message
    has not gone out (`offered_at` is null), oldest first, each with its
    visible pending cards and hidden count (the owner's amendment (b):
    one message per research, as soon as it finishes). The sender marks
    each with `mark_offered` once sent -- or at once, unsent, when the
    gap is gone or no longer researched."""
    jobs = (
        await session.execute(
            select(StudyJob.id, StudyJob.lens_gap_id)
            .where(
                StudyJob.packet == LENS,
                StudyJob.status.in_(("done", "failed")),
                StudyJob.offered_at.is_(None),
            )
            .order_by(StudyJob.id)
        )
    ).all()
    visible, hidden = await _lens_cards(session, [row[0] for row in jobs])
    return [
        LensResult(
            job_id=job_id,
            gap_id=gap_id,
            outcome=READY if visible[job_id] else SPENT,
            cards=tuple(visible[job_id]),
            hidden=hidden[job_id],
        )
        for job_id, gap_id in jobs
    ]


async def mark_offered(session: AsyncSession, job_ids, now: datetime.datetime) -> int:
    """Stamp `offered_at` on these finished lens jobs (once: an already
    offered job keeps its time; an unfinished one is left alone). The
    cards' expiry counts from it. Returns how many. Flushes, never
    commits."""
    ids = sorted({int(i) for i in job_ids})
    if not ids:
        return 0
    marked = (
        await session.execute(
            update(StudyJob)
            .where(
                StudyJob.id.in_(ids),
                StudyJob.packet == LENS,
                StudyJob.status.in_(TERMINAL_STATUSES),
                StudyJob.offered_at.is_(None),
            )
            .values(offered_at=now)
        )
    ).rowcount or 0
    await session.flush()
    return marked


async def reject_lens_cards(session: AsyncSession, gap_id: int, now: datetime.datetime) -> int:
    """«не нужно» on a result message: every pending card of the gap's
    research becomes `rejected`. Returns how many. Flushes, never
    commits."""
    rejected = (
        await session.execute(
            update(StudyCard)
            .where(
                StudyCard.kind == LENS_CARD,
                StudyCard.status == "pending",
                StudyCard.job_id.in_(select(StudyJob.id).where(StudyJob.lens_gap_id == gap_id)),
            )
            .values(status="rejected", decided_at=now)
        )
    ).rowcount or 0
    await session.flush()
    logger.info("lens cards rejected", extra={"count": rejected})
    return rejected


async def adopt_lens_cards(
    session: AsyncSession, card_ids, changeset_id: int, now: datetime.datetime
) -> int:
    """The inbox note holding these cards is written: each still-pending
    lens card among them becomes `adopted`, pointing at the
    `echo_changeset` row instead of a memory
    (`ck_study_card_adopted_has_memory`). Only app/core/echo_write.py
    calls this. Returns how many. Flushes, never commits."""
    ids = sorted({int(i) for i in card_ids})
    if not ids:
        return 0
    adopted = (
        await session.execute(
            update(StudyCard)
            .where(
                StudyCard.id.in_(ids),
                StudyCard.kind == LENS_CARD,
                StudyCard.status == "pending",
            )
            .values(status="adopted", echo_changeset_id=changeset_id, decided_at=now)
        )
    ).rowcount or 0
    await session.flush()
    logger.info("lens cards adopted", extra={"count": adopted})
    return adopted


async def lens_cards_by_id(session: AsyncSession, card_ids) -> list[LensCard]:
    """These lens cards, by id and whatever their status now, in id order
    (hidden ones excepted: they were never in a note). For
    app/core/echo_write.py's replay of a write whose answer was lost: the
    note must hold the cards the tap chose, even if the sweep has since
    expired them (an expired card keeps its text)."""
    ids = sorted({int(i) for i in card_ids})
    if not ids:
        return []
    rows = await session.execute(
        select(
            StudyCard.id, StudyCard.text, StudyCard.quote, StudyCard.source_url, StudyClip.domain
        )
        .join(StudyClip, StudyClip.id == StudyCard.clip_id)
        .where(
            StudyCard.id.in_(ids), StudyCard.kind == LENS_CARD, StudyCard.status != "hidden"
        )
        .order_by(StudyCard.id)
    )
    return [LensCard(card_id, text, quote, url, domain) for card_id, text, quote, url, domain in rows]




__all__ = [
    "ADOPTED",
    "BAD_URL",
    "CAP",
    "DISABLED",
    "EMPTY_PACKET",
    "EMPTY_TOPIC",
    "GAP_GONE",
    "INTERRUPTED",
    "LENS",
    "LENS_CARD",
    "LENS_STALE_DAYS",
    "PACKETS",
    "QUERY_MAX",
    "QUERY_REFUSED",
    "QUOTA",
    "READ",
    "READY",
    "RESEARCH",
    "RUNNING",
    "SPENT",
    "STALE",
    "STUDY",
    "TOPIC_TOO_LONG",
    "UNKNOWN_PACKET",
    "LensCard",
    "LensCards",
    "LensJob",
    "LensResult",
    "ResearchOutcome",
    "adopt_lens_cards",
    "enqueue_lens_study",
    "enqueue_read",
    "enqueue_study",
    "fail_lens_job",
    "fail_stale_lens_jobs",
    "lens_card_views",
    "lens_cards_by_id",
    "lens_job_outcomes",
    "lens_research_enabled",
    "mark_offered",
    "next_lens_job",
    "record_lens_query",
    "reject_lens_cards",
    "unsent_lens_results",
    "packet_domains",
    "run_research_job",
    "study_quota_used",
]
