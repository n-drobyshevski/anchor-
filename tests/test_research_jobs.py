"""Worker A's half of milestone 4b: `/read` end to end (plan sections 4, 9, 12, 14).

`app/research/fetch.py` and `app/research/distill.py` have their own
test files; this one is about the job that glues them to the database
-- `enqueue_read` and `run_research_job` in app/research/jobs.py.

No test here touches the network: `fetch_fn` is always a stub returning
a canned `Clip` or `FetchFailure`, and every model call goes through
`FakeLLMProvider` with scripted JSON (tests/conftest.py). The two
things every test in this file ultimately cares about are the same two
invariants plan section 12 states for the whole research package:
`source_url` is set by code and cannot be overridden by the model, and
a `risk_final='high'` card is never anything but `status='hidden'`.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.core import clock as clock_module
from app.db.models import Job, SpendLedger, StudyCard, StudyClip, StudyJob
from app.research import distill, errors, jobs
from app.research.fetch import Clip, FetchFailure
from conftest import FakeLLMProvider

pytestmark = pytest.mark.asyncio

TIMEZONE = "Europe/Paris"
URL = "https://example.com/sleep-article"

# Three clean sentences, so a test can quote any one of them verbatim
# and stay clear of the others (distill.validate drops duplicates).
SENTENCE1 = "Sleep experts often recommend starting a wind-down routine an hour before bedtime."
SENTENCE2 = (
    "Dimming household lights and leaving your phone in another room "
    "can make it noticeably easier to fall asleep quickly."
)
SENTENCE3 = "A consistent wake-up time on weekends also helps keep the body clock steady."
# A fourth sentence that exists purely to trip risk.py's health_meds rule
# (the "mg" and "melatonin" patterns), for the hidden-card test.
SENTENCE4 = "Some people also take a 5 mg melatonin tablet an hour before bed to help the process."
CLIP_TEXT = " ".join([SENTENCE1, SENTENCE2, SENTENCE3, SENTENCE4])


def _settings(**kw) -> Settings:
    base = dict(
        RESEARCH_ENABLED=True,
        RESEARCH_READS_PER_DAY=3,
        RESEARCH_CARDS_MIN=1,
        RESEARCH_CARDS_MAX=6,
        RESEARCH_JOB_USD_CAP=1.0,
        DAILY_USD_CAP=10.0,
    )
    base.update(kw)
    return Settings(**base)


def _clip(*, url: str = URL, text: str = CLIP_TEXT, title: str | None = "Sleep Hygiene Tips") -> Clip:
    return Clip(
        url=url, domain="example.com", title=title, text=text,
        text_sha256="deadbeef", http_status=200,
    )


def _fetch_ok(clip: Clip):
    async def _fetch(raw_url, **kwargs):
        return clip

    return _fetch


def _fetch_fail(failure: FetchFailure):
    async def _fetch(raw_url, **kwargs):
        return failure

    return _fetch


def _payload(cards: list[dict]) -> str:
    return json.dumps({"cards": cards}, ensure_ascii=False)


async def _study_job_count(sessionmaker) -> int:
    async with sessionmaker() as session:
        result = await session.execute(select(func.count()).select_from(StudyJob))
        return result.scalar_one()


async def _research_queue_count(sessionmaker) -> int:
    async with sessionmaker() as session:
        result = await session.execute(
            select(func.count()).select_from(Job).where(Job.kind == jobs.RESEARCH)
        )
        return result.scalar_one()


async def _assert_nothing_queued(sessionmaker) -> None:
    assert await _study_job_count(sessionmaker) == 0
    assert await _research_queue_count(sessionmaker) == 0


# --- enqueue_read -----------------------------------------------------


async def test_enqueue_refuses_when_disabled(sessionmaker, clock):
    settings = _settings(RESEARCH_ENABLED=False)
    async with sessionmaker() as session:
        result = await jobs.enqueue_read(session, settings, clock, timezone=TIMEZONE, url=URL)
    assert result == (None, jobs.DISABLED)
    await _assert_nothing_queued(sessionmaker)


async def test_enqueue_refuses_a_malformed_url(sessionmaker, clock):
    settings = _settings()
    async with sessionmaker() as session:
        result = await jobs.enqueue_read(
            session, settings, clock, timezone=TIMEZONE, url="ftp://example.com/file"
        )
    assert result == (None, jobs.BAD_URL)
    await _assert_nothing_queued(sessionmaker)


async def test_enqueue_refuses_over_the_read_quota(sessionmaker, clock):
    settings = _settings(RESEARCH_READS_PER_DAY=1)
    today = clock_module.local_date(clock, TIMEZONE)
    async with sessionmaker() as session:
        session.add(StudyJob(kind=jobs.READ, status="done", local_date=today))
        await session.commit()

    async with sessionmaker() as session:
        result = await jobs.enqueue_read(session, settings, clock, timezone=TIMEZONE, url=URL)
    assert result == (None, jobs.QUOTA)
    # Only the row seeded above exists -- the refusal wrote nothing.
    assert await _study_job_count(sessionmaker) == 1
    assert await _research_queue_count(sessionmaker) == 0


async def test_enqueue_at_quota_minus_one_succeeds(sessionmaker, clock):
    settings = _settings(RESEARCH_READS_PER_DAY=2)
    today = clock_module.local_date(clock, TIMEZONE)
    async with sessionmaker() as session:
        session.add(StudyJob(kind=jobs.READ, status="done", local_date=today))
        await session.commit()

    async with sessionmaker() as session:
        job_id, code = await jobs.enqueue_read(
            session, settings, clock, timezone=TIMEZONE, url=URL
        )
    assert code is None
    assert job_id is not None

    async with sessionmaker() as session:
        job_row = await session.get(StudyJob, job_id)
        assert job_row is not None
        assert job_row.kind == "read"
        assert job_row.status == "queued"
        assert job_row.local_date == today

        queue_row = (
            await session.execute(select(Job).where(Job.kind == jobs.RESEARCH))
        ).scalar_one()
        assert queue_row.payload == {"job_id": job_id, "url": URL}


async def test_enqueue_refuses_at_the_cap(sessionmaker, clock):
    settings = _settings(DAILY_USD_CAP=0.0)
    async with sessionmaker() as session:
        result = await jobs.enqueue_read(session, settings, clock, timezone=TIMEZONE, url=URL)
    assert result == (None, jobs.CAP)
    await _assert_nothing_queued(sessionmaker)


# --- run_research_job ---------------------------------------------------


async def _enqueue(sessionmaker, clock, settings, url=URL) -> int:
    async with sessionmaker() as session:
        job_id, code = await jobs.enqueue_read(
            session, settings, clock, timezone=TIMEZONE, url=url
        )
    assert code is None, f"setup failed: enqueue_read refused with {code}"
    return job_id


async def test_a_fetch_failure_writes_a_clip_and_fails_the_job_without_calling_the_model(
    sessionmaker, clock
):
    settings = _settings()
    job_id = await _enqueue(sessionmaker, clock, settings)
    provider = FakeLLMProvider()
    failure = FetchFailure(error=errors.DNS_ERROR, domain="example.com")

    async with sessionmaker() as session:
        outcome = await jobs.run_research_job(
            session, settings, provider, job_id=job_id, url=URL, clock=clock, timezone=TIMEZONE,
            fetch_fn=_fetch_fail(failure),
        )

    assert outcome.status == "failed"
    assert outcome.error_code == errors.DNS_ERROR
    assert outcome.visible_cards == 0
    assert outcome.hidden_cards == 0
    assert provider.calls == 0

    async with sessionmaker() as session:
        job_row = await session.get(StudyJob, job_id)
        assert job_row.status == "failed"
        assert job_row.error_code == errors.DNS_ERROR
        assert job_row.finished_at is not None

        clip_row = (
            await session.execute(select(StudyClip).where(StudyClip.job_id == job_id))
        ).scalar_one()
        assert clip_row.fetch_error == errors.DNS_ERROR
        assert clip_row.domain == "example.com"
        assert clip_row.url == URL
        # `query` is the topic column and a /read has no topic; the URL
        # rides in the queue payload instead (see jobs.py's docstring).
        assert job_row.query is None


async def test_a_successful_read_writes_the_clip_and_one_card_per_surviving_card(
    sessionmaker, clock
):
    settings = _settings()
    job_id = await _enqueue(sessionmaker, clock, settings)
    clip = _clip()
    payload = _payload(
        [
            {"kind": "technique", "text": "Dim the lights and leave your phone in another "
             "room before bed.", "quote": SENTENCE2, "risk": "low"},
            {"kind": "routine", "text": "Wake up at the same time even on weekends to keep "
             "your body clock steady.", "quote": SENTENCE3, "risk": "low"},
        ]
    )
    provider = FakeLLMProvider(text=payload, model="safety-fake")

    async with sessionmaker() as session:
        outcome = await jobs.run_research_job(
            session, settings, provider, job_id=job_id, url=URL, clock=clock, timezone=TIMEZONE,
            fetch_fn=_fetch_ok(clip),
        )

    assert outcome.status == "done"
    assert outcome.error_code is None
    assert outcome.visible_cards == 2
    assert outcome.hidden_cards == 0
    assert provider.calls == 1
    # The isolated call sees only the topic and the page (plan section 7):
    # no state, memory or transcript ever reaches distill.call.
    assert provider.received_conversation_ids[0].startswith("anchor-distill-")

    async with sessionmaker() as session:
        job_row = await session.get(StudyJob, job_id)
        assert job_row.status == "done"
        assert job_row.pins_used == 1
        assert job_row.finished_at is not None

        clip_row = (
            await session.execute(select(StudyClip).where(StudyClip.job_id == job_id))
        ).scalar_one()
        assert clip_row.url == clip.url
        assert clip_row.title == clip.title
        assert clip_row.text == clip.text
        assert clip_row.fetched_at is not None

        cards = (
            await session.execute(select(StudyCard).where(StudyCard.job_id == job_id))
        ).scalars().all()
    assert len(cards) == 2
    for card in cards:
        assert card.status == "pending"
        assert card.source_url == clip.url


async def test_source_url_is_never_taken_from_the_model_even_when_it_looks_legitimate(
    sessionmaker, clock
):
    settings = _settings()
    job_id = await _enqueue(sessionmaker, clock, settings)
    clip = _clip()
    # The schema distill.py sends has no source_url property, but a
    # hostile or buggy provider can still put arbitrary extra keys in
    # its JSON -- this is exactly that case, and it must be ignored.
    payload = _payload(
        [
            {
                "kind": "technique",
                "text": "Dim the lights before bed.",
                "quote": SENTENCE2,
                "risk": "low",
                "source_url": "https://attacker.example/fake-source",
            }
        ]
    )
    provider = FakeLLMProvider(text=payload)

    async with sessionmaker() as session:
        outcome = await jobs.run_research_job(
            session, settings, provider, job_id=job_id, url=URL, clock=clock, timezone=TIMEZONE,
            fetch_fn=_fetch_ok(clip),
        )
    assert outcome.visible_cards == 1

    async with sessionmaker() as session:
        card = (
            await session.execute(select(StudyCard).where(StudyCard.job_id == job_id))
        ).scalar_one()
    assert card.source_url == clip.url
    assert card.source_url != "https://attacker.example/fake-source"


async def test_a_high_risk_card_is_written_hidden_even_when_the_model_said_low(
    sessionmaker, clock
):
    settings = _settings()
    job_id = await _enqueue(sessionmaker, clock, settings)
    clip = _clip()
    payload = _payload(
        [
            {
                "kind": "technique",
                "text": "Take a 5 mg melatonin tablet before bed to help you fall asleep faster.",
                "quote": SENTENCE4,
                "risk": "low",  # the model's own claim; the rules must overrule it
            }
        ]
    )
    provider = FakeLLMProvider(text=payload)

    async with sessionmaker() as session:
        outcome = await jobs.run_research_job(
            session, settings, provider, job_id=job_id, url=URL, clock=clock, timezone=TIMEZONE,
            fetch_fn=_fetch_ok(clip),
        )
    assert outcome.visible_cards == 0
    assert outcome.hidden_cards == 1

    async with sessionmaker() as session:
        card = (
            await session.execute(select(StudyCard).where(StudyCard.job_id == job_id))
        ).scalar_one()
    assert card.risk_model == "low"
    assert card.risk_rules == "high"
    assert card.risk_final == "high"
    assert card.status == "hidden"
    assert "health_meds" in card.rule_hits


async def test_the_spend_is_ledgered_under_research_and_matches_the_jobs_cost(
    sessionmaker, clock
):
    settings = _settings()
    job_id = await _enqueue(sessionmaker, clock, settings)
    clip = _clip()
    payload = _payload(
        [{"kind": "technique", "text": "Dim the lights before bed.", "quote": SENTENCE2, "risk": "low"}]
    )
    provider = FakeLLMProvider(text=payload)

    async with sessionmaker() as session:
        await jobs.run_research_job(
            session, settings, provider, job_id=job_id, url=URL, clock=clock, timezone=TIMEZONE,
            fetch_fn=_fetch_ok(clip),
        )

    async with sessionmaker() as session:
        job_row = await session.get(StudyJob, job_id)
        ledger_row = (
            await session.execute(
                select(SpendLedger).where(SpendLedger.category == distill.RESEARCH_CATEGORY)
            )
        ).scalar_one()
    assert ledger_row.usd_cost == job_row.usd_cost
    assert job_row.usd_cost > 0


async def test_the_cap_hit_before_distill_fails_the_job_and_keeps_the_clip(sessionmaker, clock):
    # Enqueued while the budget is open, then run once it is not -- the
    # daily cap can be spent by something else between the two moments.
    job_id = await _enqueue(sessionmaker, clock, _settings())
    tight = _settings(DAILY_USD_CAP=0.0)
    clip = _clip()
    provider = FakeLLMProvider()

    async with sessionmaker() as session:
        outcome = await jobs.run_research_job(
            session, tight, provider, job_id=job_id, url=URL, clock=clock, timezone=TIMEZONE,
            fetch_fn=_fetch_ok(clip),
        )

    assert outcome.status == "failed"
    assert outcome.error_code == "cap"
    assert provider.calls == 0

    async with sessionmaker() as session:
        job_row = await session.get(StudyJob, job_id)
        assert job_row.status == "failed"
        assert job_row.error_code == "cap"
        clip_row = (
            await session.execute(select(StudyClip).where(StudyClip.job_id == job_id))
        ).scalar_one()
        assert clip_row.url == clip.url  # the clip survives the cap failure


async def test_rerunning_a_finished_job_is_a_no_op(sessionmaker, clock):
    settings = _settings()
    job_id = await _enqueue(sessionmaker, clock, settings)
    clip = _clip()
    payload = _payload(
        [{"kind": "technique", "text": "Dim the lights before bed.", "quote": SENTENCE2, "risk": "low"}]
    )
    first_provider = FakeLLMProvider(text=payload)

    async with sessionmaker() as session:
        first = await jobs.run_research_job(
            session, settings, first_provider, job_id=job_id, url=URL, clock=clock, timezone=TIMEZONE,
            fetch_fn=_fetch_ok(clip),
        )
    assert first.status == "done"
    assert first.visible_cards == 1

    second_provider = FakeLLMProvider(text=payload)
    async with sessionmaker() as session:
        second = await jobs.run_research_job(
            session, settings, second_provider, job_id=job_id, url=URL, clock=clock, timezone=TIMEZONE,
            fetch_fn=_fetch_ok(clip),
        )

    assert second == first
    assert second_provider.calls == 0  # never fetched or distilled again

    async with sessionmaker() as session:
        cards = (
            await session.execute(select(StudyCard).where(StudyCard.job_id == job_id))
        ).scalars().all()
    assert len(cards) == 1  # not duplicated by the second run


async def test_a_long_url_is_queued_rather_than_refused(sessionmaker, clock):
    """A share link with campaign parameters runs past study_job.query's
    200-character cap. The URL is fine; only the column was too small,
    and refusing it as BAD_URL would have named the wrong cause."""
    settings = _settings()
    long_url = "https://example.com/a-fairly-long-article-slug?" + "&".join(
        f"utm_param_{i}=value{i}" for i in range(12)
    )
    assert len(long_url) > 200

    async with sessionmaker() as session:
        job_id, refusal = await jobs.enqueue_read(
            session, settings, clock, timezone=TIMEZONE, url=long_url
        )
        await session.commit()

    assert refusal is None and job_id is not None
    async with sessionmaker() as session:
        queued = (await session.execute(select(Job).where(Job.kind == jobs.RESEARCH))).scalar_one()
        assert queued.payload["url"] == long_url
        assert (await session.get(StudyJob, job_id)).query is None
