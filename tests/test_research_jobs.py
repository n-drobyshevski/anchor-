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

import datetime
import decimal
import json

import pytest
import sqlalchemy
from sqlalchemy import func, select

from app.config import Settings
from app.core import clock as clock_module
from app.core.clock import FrozenClock
from app.core import safety_events
from app.db.models import (
    Job,
    SafetyEvent,
    SpendLedger,
    StudyCard,
    StudyClip,
    StudyJob,
)
from app import worker
from app.llm.provider import LLMResponse, LLMUsage
from app.research import distill, errors, jobs, search
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


# --- the job-finished line (plan section 9) ---


class _State:
    """The three user_state fields _may_report_now reads, and nothing else."""

    def __init__(self, *, persona_active=True, quiet_until=None, timezone=TIMEZONE, chat_id=555):
        self.persona_active = persona_active
        self.quiet_until = quiet_until
        self.timezone = timezone
        self.chat_id = chat_id


def _at(hour: int, minute: int = 0) -> FrozenClock:
    """A clock at a given UTC hour. TIMEZONE is Europe/Paris (UTC+2 in
    September), so 12:00 UTC is 14:00 local and 21:00 UTC is 23:00."""
    return FrozenClock(datetime.datetime(2026, 9, 22, hour, minute, tzinfo=datetime.timezone.utc))


async def test_the_done_line_is_sent_in_ordinary_hours():
    assert worker._may_report_now(_settings(), _at(12), _State()) is True


async def test_a_paused_persona_gets_no_done_line():
    """Plan section 9: if sending is not allowed, send nothing -- the
    cards wait in /notes, which is where the line would have pointed."""
    assert worker._may_report_now(_settings(), _at(12), _State(persona_active=False)) is False


async def test_an_active_quiet_command_suppresses_the_done_line():
    until = datetime.datetime(2026, 9, 23, tzinfo=datetime.timezone.utc)
    assert worker._may_report_now(_settings(), _at(12), _State(quiet_until=until)) is False


async def test_an_expired_quiet_command_does_not():
    until = datetime.datetime(2026, 9, 21, tzinfo=datetime.timezone.utc)
    assert worker._may_report_now(_settings(), _at(12), _State(quiet_until=until)) is True


async def test_quiet_hours_suppress_the_done_line():
    """23:00 local, inside the default 22:30-08:00 window."""
    assert worker._may_report_now(_settings(), _at(21), _State()) is False


async def test_the_done_line_ignores_the_outbound_switch_and_the_cap():
    """It is a reply to a command the user typed, not an unsolicited
    message: plan section 9 keeps it off the outbound counters and out
    of the gate entirely."""
    settings = _settings()
    settings = settings.model_copy(update={"OUTBOUND_ENABLED": False, "DAILY_USD_CAP": 0.0})
    assert worker._may_report_now(settings, _at(12), _State()) is True


# --- /study: enqueue (plan sections 3, 9) -----------------------------

TOPIC = "как высыпаться"
FORUMS = ("reddit.com",)


def _study_settings(**kw) -> Settings:
    base = dict(
        RESEARCH_JOBS_PER_DAY=1,
        RESEARCH_MAX_SEARCHES=4,
        RESEARCH_MAX_PINS=2,
        PACKET_FORUMS="reddit.com",
        PACKET_REF="ru.wikipedia.org,en.wikipedia.org",
        PACKET_GUIDES="",
    )
    base.update(kw)
    return _settings(**base)


async def _enqueue_study(sessionmaker, clock, settings, *, packet="forums", topic=TOPIC):
    async with sessionmaker() as session:
        result = await jobs.enqueue_study(
            session, settings, clock, timezone=TIMEZONE, packet=packet, topic=topic
        )
        await session.commit()
        return result


async def test_study_enqueue_refuses_when_disabled(sessionmaker, clock):
    settings = _study_settings(RESEARCH_ENABLED=False)
    assert await _enqueue_study(sessionmaker, clock, settings) == (None, jobs.DISABLED)
    await _assert_nothing_queued(sessionmaker)


async def test_study_enqueue_refuses_an_unknown_packet(sessionmaker, clock):
    settings = _study_settings()
    assert await _enqueue_study(sessionmaker, clock, settings, packet="twitter") == (
        None,
        jobs.UNKNOWN_PACKET,
    )
    await _assert_nothing_queued(sessionmaker)


async def test_study_enqueue_refuses_a_known_but_unconfigured_packet(sessionmaker, clock):
    """A different refusal from an unknown name: plan section 9 answers
    «Пакеты: forums, guides, ref.» to one and «Пакет guides пока не
    настроен.» to the other."""
    settings = _study_settings()
    assert await _enqueue_study(sessionmaker, clock, settings, packet="guides") == (
        None,
        jobs.EMPTY_PACKET,
    )
    await _assert_nothing_queued(sessionmaker)


@pytest.mark.parametrize("packet", ["forums", "ref"])
async def test_study_enqueue_accepts_each_configured_packet(sessionmaker, clock, packet):
    settings = _study_settings()
    job_id, refusal = await _enqueue_study(sessionmaker, clock, settings, packet=packet)
    assert refusal is None and job_id is not None
    async with sessionmaker() as session:
        row = await session.get(StudyJob, job_id)
        assert row.kind == "study"
        assert row.packet == packet
        assert row.query == TOPIC


async def test_study_enqueue_refuses_an_empty_topic(sessionmaker, clock):
    settings = _study_settings()
    assert await _enqueue_study(sessionmaker, clock, settings, topic="   ") == (
        None,
        jobs.EMPTY_TOPIC,
    )
    await _assert_nothing_queued(sessionmaker)


async def test_study_enqueue_refuses_a_topic_past_the_column_limit(sessionmaker, clock):
    """Refused, never truncated: a truncated topic searches for
    something the user did not ask about."""
    settings = _study_settings()
    long_topic = "с" * (jobs.QUERY_MAX + 1)
    assert await _enqueue_study(sessionmaker, clock, settings, topic=long_topic) == (
        None,
        jobs.TOPIC_TOO_LONG,
    )
    await _assert_nothing_queued(sessionmaker)


async def test_study_enqueue_refuses_a_second_job_the_same_day(sessionmaker, clock):
    settings = _study_settings(RESEARCH_JOBS_PER_DAY=1)
    assert (await _enqueue_study(sessionmaker, clock, settings))[1] is None
    assert (await _enqueue_study(sessionmaker, clock, settings))[1] == jobs.QUOTA


async def test_study_and_read_quotas_are_separate(sessionmaker, clock):
    """Plan section 3 gives them different allowances because they cost
    differently. Spending one must not consume the other."""
    settings = _study_settings(RESEARCH_JOBS_PER_DAY=1, RESEARCH_READS_PER_DAY=1)

    assert (await _enqueue_study(sessionmaker, clock, settings))[1] is None
    async with sessionmaker() as session:
        _, refusal = await jobs.enqueue_read(
            session, settings, clock, timezone=TIMEZONE, url=URL
        )
        await session.commit()
    assert refusal is None, "a used study quota must not block a read"

    assert (await _enqueue_study(sessionmaker, clock, settings))[1] == jobs.QUOTA
    async with sessionmaker() as session:
        _, refusal = await jobs.enqueue_read(
            session, settings, clock, timezone=TIMEZONE, url=URL
        )
    assert refusal == jobs.QUOTA


# --- /study: the run (plan sections 6, 12) ----------------------------


class _Search:
    """A scripted stand-in for app.research.search.find_urls."""

    def __init__(self, *urls: str, error_code=None, calls: int = 1) -> None:
        self._urls = urls
        self._error = error_code
        self._calls = calls
        self.seen: list[dict] = []

    async def __call__(self, provider, **kwargs):
        self.seen.append(kwargs)
        usage = LLMUsage(
            input_tokens=4000, cached_tokens=0, output_tokens=3,
            cost_usd=decimal.Decimal("0.0081"),
        )
        responses = tuple(
            LLMResponse(text="", usage=usage, model="fake-safety") for _ in range(self._calls)
        )
        return search.SearchOutcome(
            urls=tuple(self._urls), error_code=self._error, responses=responses
        )


def _fetch_by_url(mapping):
    """A fetch seam that answers per URL, so a study job can meet a mix
    of readable and refusing pages."""

    async def _fetch(raw_url, **kwargs):
        _fetch.seen.append((raw_url, kwargs.get("allowed_domains")))
        return mapping[raw_url]

    _fetch.seen = []
    return _fetch


def _one_card(sentence: str) -> str:
    return _payload(
        [{"kind": "technique", "text": "Совет со страницы.", "quote": sentence, "risk": "low"}]
    )


async def _run_study(sessionmaker, clock, settings, job_id, *, search_fn, fetch_fn, provider):
    async with sessionmaker() as session:
        return await jobs.run_research_job(
            session, settings, provider, job_id=job_id, clock=clock, timezone=TIMEZONE,
            fetch_fn=fetch_fn, search_fn=search_fn,
        )


async def test_a_study_job_searches_then_reads_its_candidates(sessionmaker, clock):
    settings = _study_settings(RESEARCH_MAX_PINS=2)
    job_id, _ = await _enqueue_study(sessionmaker, clock, settings)

    a, b = "https://reddit.com/r/a", "https://reddit.com/r/b"
    searcher = _Search(a, b)
    fetcher = _fetch_by_url({a: _clip(url=a), b: _clip(url=b, text=CLIP_TEXT)})
    provider = FakeLLMProvider(text=_one_card(SENTENCE1))

    outcome = await _run_study(
        sessionmaker, clock, settings, job_id,
        search_fn=searcher, fetch_fn=fetcher, provider=provider,
    )

    assert outcome.status == "done"
    assert provider.calls == 2, "one distill per fetched page"
    async with sessionmaker() as session:
        job = await session.get(StudyJob, job_id)
        clips = (await session.execute(select(StudyClip).where(StudyClip.job_id == job_id))).scalars().all()
    assert job.pins_used == 2
    assert job.searches_used == 1
    assert len(clips) == 2


async def test_the_packet_allowlist_reaches_every_fetch(sessionmaker, clock):
    """The fetcher re-applies the allowlist at every redirect hop, but
    it can only do that if the job hands it the packet in the first
    place."""
    settings = _study_settings(RESEARCH_MAX_PINS=1)
    job_id, _ = await _enqueue_study(sessionmaker, clock, settings)
    a = "https://reddit.com/r/a"
    fetcher = _fetch_by_url({a: _clip(url=a)})

    await _run_study(
        sessionmaker, clock, settings, job_id,
        search_fn=_Search(a), fetch_fn=fetcher,
        provider=FakeLLMProvider(text=_one_card(SENTENCE1)),
    )

    assert fetcher.seen == [(a, FORUMS)]


async def test_more_candidates_than_pins_stops_at_the_pin_cap(sessionmaker, clock):
    settings = _study_settings(RESEARCH_MAX_PINS=2)
    job_id, _ = await _enqueue_study(sessionmaker, clock, settings)
    urls = [f"https://reddit.com/r/{n}" for n in range(5)]
    fetcher = _fetch_by_url({u: _clip(url=u) for u in urls})
    provider = FakeLLMProvider(text=_one_card(SENTENCE1))

    await _run_study(
        sessionmaker, clock, settings, job_id,
        search_fn=_Search(*urls), fetch_fn=fetcher, provider=provider,
    )

    assert len(fetcher.seen) == 2
    assert provider.calls == 2


async def test_a_refusing_candidate_is_recorded_and_the_next_one_is_tried(sessionmaker, clock):
    """Plan section 5.9 forbids working around a refusal, not noticing
    it. A packet whose first result disallows robots should still yield
    cards from the second."""
    settings = _study_settings(RESEARCH_MAX_PINS=1)
    job_id, _ = await _enqueue_study(sessionmaker, clock, settings)
    blocked, good = "https://reddit.com/r/blocked", "https://reddit.com/r/good"
    fetcher = _fetch_by_url(
        {
            blocked: FetchFailure(error=errors.ROBOTS_DISALLOW, domain="reddit.com"),
            good: _clip(url=good),
        }
    )

    outcome = await _run_study(
        sessionmaker, clock, settings, job_id,
        search_fn=_Search(blocked, good), fetch_fn=fetcher,
        provider=FakeLLMProvider(text=_one_card(SENTENCE1)),
    )

    assert outcome.status == "done"
    async with sessionmaker() as session:
        clips = (
            await session.execute(select(StudyClip).where(StudyClip.job_id == job_id))
        ).scalars().all()
        job = await session.get(StudyJob, job_id)
    assert {c.fetch_error for c in clips} == {errors.ROBOTS_DISALLOW, None}
    assert job.pins_used == 1, "a refused page does not spend a pin"


async def test_every_candidate_refusing_reports_the_wall_not_an_absence(sessionmaker, clock):
    """The acceptance checklist asks /study to "report clearly that
    Reddit blocked the fetch". An empty result would not be that."""
    settings = _study_settings()
    job_id, _ = await _enqueue_study(sessionmaker, clock, settings)
    urls = ["https://reddit.com/r/a", "https://reddit.com/r/b"]
    fetcher = _fetch_by_url(
        {u: FetchFailure(error=errors.ROBOTS_DISALLOW, domain="reddit.com") for u in urls}
    )
    provider = FakeLLMProvider()

    outcome = await _run_study(
        sessionmaker, clock, settings, job_id,
        search_fn=_Search(*urls), fetch_fn=fetcher, provider=provider,
    )

    assert outcome.status == "failed"
    assert outcome.error_code == errors.ROBOTS_DISALLOW
    assert provider.calls == 0, "nothing to distill, so nothing was spent on distilling"


async def test_a_search_that_finds_nothing_fails_the_job(sessionmaker, clock):
    settings = _study_settings()
    job_id, _ = await _enqueue_study(sessionmaker, clock, settings)

    outcome = await _run_study(
        sessionmaker, clock, settings, job_id,
        search_fn=_Search(error_code=search.NO_RESULTS, calls=2),
        fetch_fn=_fetch_by_url({}), provider=FakeLLMProvider(),
    )

    assert outcome.status == "failed"
    assert outcome.error_code == search.NO_RESULTS
    async with sessionmaker() as session:
        job = await session.get(StudyJob, job_id)
    assert job.searches_used == 2


async def test_every_search_call_is_ledgered_even_a_fruitless_one(sessionmaker, clock):
    settings = _study_settings()
    job_id, _ = await _enqueue_study(sessionmaker, clock, settings)

    await _run_study(
        sessionmaker, clock, settings, job_id,
        search_fn=_Search(error_code=search.NO_RESULTS, calls=2),
        fetch_fn=_fetch_by_url({}), provider=FakeLLMProvider(),
    )

    async with sessionmaker() as session:
        rows = (await session.execute(select(SpendLedger))).scalars().all()
        job = await session.get(StudyJob, job_id)
    assert len(rows) == 2
    assert {r.category for r in rows} == {"research"}
    assert job.usd_cost == sum(r.usd_cost for r in rows)


async def test_the_search_fee_rides_in_the_vendor_cost(sessionmaker, clock):
    """Plan section 6: "the fee is taken from the provider-reported
    cost". A row priced from the vendor figure records that, so a run
    that fell back to the token formula -- and therefore under-counts
    the plugin fee -- is visible rather than silently wrong."""
    settings = _study_settings()
    job_id, _ = await _enqueue_study(sessionmaker, clock, settings)

    await _run_study(
        sessionmaker, clock, settings, job_id,
        search_fn=_Search(error_code=search.NO_RESULTS, calls=1),
        fetch_fn=_fetch_by_url({}), provider=FakeLLMProvider(),
    )

    async with sessionmaker() as session:
        [row] = (await session.execute(select(SpendLedger))).scalars().all()
    assert row.cost_source == "vendor"
    assert row.usd_cost == decimal.Decimal("0.008100")


async def test_the_recent_clip_set_reaches_the_search(sessionmaker, clock):
    """Plan section 6's dedupe: a page clipped in the last 30 days is
    not offered again."""
    settings = _study_settings()
    job_id, _ = await _enqueue_study(sessionmaker, clock, settings)
    old = "https://reddit.com/r/already-read"
    async with sessionmaker() as session:
        prior = StudyJob(kind="read", local_date=datetime.date(2026, 9, 1), status="done")
        session.add(prior)
        await session.flush()
        session.add(
            StudyClip(job_id=prior.id, url=old, domain="reddit.com", fetched_at=clock.now_utc())
        )
        await session.commit()

    searcher = _Search(error_code=search.NO_RESULTS)
    await _run_study(
        sessionmaker, clock, settings, job_id,
        search_fn=searcher, fetch_fn=_fetch_by_url({}), provider=FakeLLMProvider(),
    )

    assert old in searcher.seen[0]["recent_urls"]
    assert searcher.seen[0]["allowed_domains"] == FORUMS
    assert searcher.seen[0]["max_calls"] == settings.RESEARCH_MAX_SEARCHES


async def test_the_cap_mid_job_stops_it_and_keeps_the_cards_already_made(sessionmaker, clock):
    """Plan section 12: the job stops, becomes failed:cap, and keeps any
    cards already produced."""
    settings = _study_settings(RESEARCH_MAX_PINS=2, RESEARCH_JOB_USD_CAP=0.01)
    job_id, _ = await _enqueue_study(sessionmaker, clock, settings)
    a, b = "https://reddit.com/r/a", "https://reddit.com/r/b"
    fetcher = _fetch_by_url({a: _clip(url=a), b: _clip(url=b)})

    outcome = await _run_study(
        sessionmaker, clock, settings, job_id,
        search_fn=_Search(a, b), fetch_fn=fetcher,
        provider=FakeLLMProvider(
            text=_one_card(SENTENCE1),
            usage=LLMUsage(
                input_tokens=5000, cached_tokens=0, output_tokens=600,
                cost_usd=decimal.Decimal("0.02"),
            ),
        ),
    )

    assert outcome.status == "failed"
    assert outcome.error_code == jobs.CAP
    assert outcome.visible_cards == 1, "the first page's card survives the stop"
    async with sessionmaker() as session:
        cards = (await session.execute(select(StudyCard))).scalars().all()
    assert len(cards) == 1


# --- /delete cancelling a job mid-run (plan section 9) ----------------


async def test_a_study_job_cancelled_mid_run_stops_writing(sessionmaker, clock):
    """`/delete` cancels queued and running jobs, then purges.

    `run_research_job` reads the status once at the top, which stops a
    job that has not started. This is the other half: a job already
    mid-flight must notice too, or its writes land after the purge --
    and because /delete uses RESTART IDENTITY, they can attach to a
    brand-new job that reused the id.
    """
    settings = _study_settings(RESEARCH_MAX_PINS=2)
    job_id, _ = await _enqueue_study(sessionmaker, clock, settings)
    a, b = "https://reddit.com/r/a", "https://reddit.com/r/b"

    # Fetch the first candidate, then cancel the way /delete does,
    # before the second.
    class _CancellingFetch:
        def __init__(self):
            self.calls = 0

        async def __call__(self, raw_url, **kwargs):
            self.calls += 1
            if self.calls == 1:
                async with sessionmaker() as other:
                    job = await other.get(StudyJob, job_id)
                    job.status = "cancelled"
                    await other.commit()
            return _clip(url=raw_url)

    fetcher = _CancellingFetch()
    outcome = await _run_study(
        sessionmaker, clock, settings, job_id,
        search_fn=_Search(a, b), fetch_fn=fetcher,
        provider=FakeLLMProvider(text=_one_card(SENTENCE1)),
    )

    assert outcome.status == "cancelled"
    assert fetcher.calls == 1, "the second candidate is never fetched"
    async with sessionmaker() as session:
        clips = (await session.execute(select(StudyClip))).scalars().all()
    # The in-flight clip was flushed but never committed, so it rolls
    # back with the session. That is the right outcome and not merely a
    # tolerable one: the job is being deleted, and a half-run's clip is
    # exactly the stray row this check exists to prevent. Spend is the
    # one thing that must survive, and it does -- _distill_into_cards
    # commits its ledger row before any card is written.
    assert clips == []


async def test_a_job_whose_id_was_reused_after_a_purge_stops(sessionmaker, clock):
    """The dangerous case, and the reason the check is not status-only.

    /delete uses RESTART IDENTITY, so a study_job created after the
    purge takes id 1 again. Its status is 'queued', not 'cancelled' --
    a status-only check would wave the old run straight through and
    file its clips and cards under a job the user had just started.
    `created_at` is what tells the two apart.
    """
    settings = _study_settings(RESEARCH_MAX_PINS=2)
    job_id, _ = await _enqueue_study(sessionmaker, clock, settings)
    a, b = "https://reddit.com/r/a", "https://reddit.com/r/b"

    class _PurgeAndRecreate:
        def __init__(self):
            self.calls = 0

        async def __call__(self, raw_url, **kwargs):
            self.calls += 1
            if self.calls == 1:
                # What /delete does, compressed: the row goes, and a new
                # job lands on the same id.
                async with sessionmaker() as other:
                    await other.execute(sqlalchemy.delete(StudyJob))
                    await other.execute(
                        sqlalchemy.text("ALTER SEQUENCE study_job_id_seq RESTART WITH 1")
                    )
                    other.add(
                        StudyJob(
                            kind="study", packet="forums", query="совсем другая тема",
                            local_date=datetime.date(2026, 9, 22), status="queued",
                        )
                    )
                    await other.commit()
            return _clip(url=raw_url)

    fetcher = _PurgeAndRecreate()
    outcome = await _run_study(
        sessionmaker, clock, settings, job_id,
        search_fn=_Search(a, b), fetch_fn=fetcher,
        provider=FakeLLMProvider(text=_one_card(SENTENCE1)),
    )

    assert outcome.status == "cancelled"
    assert fetcher.calls == 1, "the old run stops rather than continuing into the new job"
    async with sessionmaker() as session:
        fresh = (await session.execute(select(StudyJob))).scalars().all()
        clips = (await session.execute(select(StudyClip))).scalars().all()
        cards = (await session.execute(select(StudyCard))).scalars().all()
    assert len(fresh) == 1 and fresh[0].query == "совсем другая тема"
    assert clips == [], "no stray clip attached to the new job"
    assert cards == [], "no stray card attached to the new job"


async def test_spend_already_made_survives_a_cancellation(sessionmaker, clock):
    """The one thing a cancelled run must not lose. A distill that ran
    was paid for whether or not the job it belonged to still exists, and
    a ledger that forgets it under-reports the day."""
    settings = _study_settings(RESEARCH_MAX_PINS=2)
    job_id, _ = await _enqueue_study(sessionmaker, clock, settings)
    a, b = "https://reddit.com/r/a", "https://reddit.com/r/b"

    class _CancelAfterFirstDistill:
        def __init__(self):
            self.calls = 0

        async def __call__(self, raw_url, **kwargs):
            self.calls += 1
            if self.calls == 2:
                async with sessionmaker() as other:
                    job = await other.get(StudyJob, job_id)
                    job.status = "cancelled"
                    await other.commit()
            return _clip(url=raw_url)

    outcome = await _run_study(
        sessionmaker, clock, settings, job_id,
        search_fn=_Search(a, b), fetch_fn=_CancelAfterFirstDistill(),
        provider=FakeLLMProvider(text=_one_card(SENTENCE1)),
    )

    assert outcome.status == "cancelled"
    async with sessionmaker() as session:
        rows = (await session.execute(select(SpendLedger))).scalars().all()
    assert len(rows) == 2, "the search and the one distill that ran are both ledgered"


async def test_a_read_job_cancelled_before_distill_makes_no_provider_call(sessionmaker, clock):
    settings = _settings()
    job_id = await _enqueue(sessionmaker, clock, settings)

    async def _fetch_then_cancel(raw_url, **kwargs):
        async with sessionmaker() as other:
            job = await other.get(StudyJob, job_id)
            job.status = "cancelled"
            await other.commit()
        return _clip()

    provider = FakeLLMProvider(text=_one_card(SENTENCE1))
    async with sessionmaker() as session:
        outcome = await jobs.run_research_job(
            session, settings, provider, job_id=job_id, url=URL, clock=clock,
            timezone=TIMEZONE, fetch_fn=_fetch_then_cancel,
        )

    assert outcome.status == "cancelled"
    assert provider.calls == 0, "a cancelled job must not spend money on a distill"


# --- a job whose worker died mid-run (4d fixes) ------------------------


async def _park_at(sessionmaker, job_id: int, status: str) -> None:
    """Leave a job in the state a crashed worker would have left it in."""
    async with sessionmaker() as session:
        job = await session.get(StudyJob, job_id)
        job.status = status
        await session.commit()


@pytest.mark.parametrize("status", ["searching", "fetching", "distilling"])
async def test_a_job_interrupted_mid_run_is_failed_not_reported_as_finished(
    sessionmaker, clock, status
):
    """The queue recovers the *queue* row after STUCK_AFTER and
    redelivers it; nothing ever moved `study_job` out of its
    intermediate status.

    Until this fix that landed in the "already finished" branch, so the
    job was reported as still `fetching`, the queue row was completed,
    and the row wedged there forever -- while the user was told
    «Не получилось: техническая проблема», a failure the database had no
    record of.
    """
    settings = _settings()
    job_id = await _enqueue(sessionmaker, clock, settings)
    await _park_at(sessionmaker, job_id, status)
    provider = FakeLLMProvider()

    async with sessionmaker() as session:
        outcome = await jobs.run_research_job(
            session, settings, provider, job_id=job_id, url=URL, clock=clock,
            timezone=TIMEZONE, fetch_fn=_fetch_ok(_clip()),
        )

    assert outcome.status == "failed"
    assert outcome.error_code == jobs.INTERRUPTED
    assert provider.calls == 0, "a resume would re-spend on a call already paid for"

    async with sessionmaker() as session:
        row = await session.get(StudyJob, job_id)
    assert row.status == "failed"
    assert row.error_code == jobs.INTERRUPTED
    assert row.finished_at is not None, "the row must reach a terminal state"


async def test_an_interrupted_job_keeps_the_cards_it_already_made(sessionmaker, clock):
    """Same rule plan section 12 gives a job stopped at the cap: what it
    already produced survives."""
    settings = _settings()
    job_id = await _enqueue(sessionmaker, clock, settings)
    async with sessionmaker() as session:
        job = await session.get(StudyJob, job_id)
        clip = StudyClip(job_id=job.id, url=URL, domain="example.com", text=CLIP_TEXT)
        session.add(clip)
        await session.flush()
        session.add(
            StudyCard(
                job_id=job.id, clip_id=clip.id, kind="technique",
                text="Уже сделанная карточка.", quote=SENTENCE1,
                source_url=URL, risk_model="low", risk_rules="low", risk_final="low",
            )
        )
        job.status = "distilling"
        await session.commit()

    async with sessionmaker() as session:
        outcome = await jobs.run_research_job(
            session, settings, FakeLLMProvider(), job_id=job_id, url=URL, clock=clock,
            timezone=TIMEZONE, fetch_fn=_fetch_ok(_clip()),
        )

    assert outcome.error_code == jobs.INTERRUPTED
    assert outcome.visible_cards == 1
    async with sessionmaker() as session:
        assert len((await session.execute(select(StudyCard))).scalars().all()) == 1


@pytest.mark.parametrize("status", ["done", "failed", "cancelled"])
async def test_a_terminal_job_is_still_reported_not_relabelled(sessionmaker, clock, status):
    """The regression guard that matters most is `cancelled`: /delete set
    it, and turning it into `interrupted` would rewrite the record of a
    deletion the user asked for."""
    settings = _settings()
    job_id = await _enqueue(sessionmaker, clock, settings)
    await _park_at(sessionmaker, job_id, status)

    async with sessionmaker() as session:
        outcome = await jobs.run_research_job(
            session, settings, FakeLLMProvider(), job_id=job_id, url=URL, clock=clock,
            timezone=TIMEZONE, fetch_fn=_fetch_ok(_clip()),
        )

    assert outcome.status == status
    assert outcome.error_code != jobs.INTERRUPTED
    async with sessionmaker() as session:
        assert (await session.get(StudyJob, job_id)).status == status


async def test_the_interrupted_message_says_what_happened(clock):
    """Not «техническая проблема» -- nothing refused us, the worker went
    away, and the reply should say so."""
    from app.tg import research as research_ui

    text = research_ui.completion_text(
        status="failed", error_code=jobs.INTERRUPTED, visible_cards=0
    )
    assert text == "Не получилось: задание прервалось на полпути."
    assert research_ui.ERROR_RU_FALLBACK not in text


# --- the dedupe window is bounded on both branches (4d fixes) ---------


async def _clip_row(sessionmaker, *, url, fetched_at, job_created_at):
    async with sessionmaker() as session:
        job = StudyJob(
            kind="read", local_date=datetime.date(2026, 9, 1), status="done",
            created_at=job_created_at,
        )
        session.add(job)
        await session.flush()
        session.add(
            StudyClip(job_id=job.id, url=url, domain="example.com", fetched_at=fetched_at)
        )
        await session.commit()


async def test_a_failed_clip_stops_blocking_its_url_after_the_window(sessionmaker, clock):
    """The defect this fixes: `fetched_at` is NULL on every failed fetch
    and `study_clip` has no `created_at`, so filtering on NULL alone
    excluded a URL from every future /study permanently -- after one
    transient timeout -- and grew the set without bound.
    """
    now = clock.now_utc()
    recent = "https://reddit.com/r/failed-yesterday"
    ancient = "https://reddit.com/r/failed-long-ago"
    await _clip_row(
        sessionmaker, url=recent, fetched_at=None,
        job_created_at=now - datetime.timedelta(days=1),
    )
    await _clip_row(
        sessionmaker, url=ancient, fetched_at=None,
        job_created_at=now - datetime.timedelta(days=90),
    )

    async with sessionmaker() as session:
        seen = await jobs._recent_clip_urls(session, clock)

    assert recent in seen, "a fresh failure still stops us burning a pin on it"
    assert ancient not in seen, "a failure from ninety days ago is not evidence today"


async def test_a_successful_clip_is_bounded_by_its_own_timestamp(sessionmaker, clock):
    now = clock.now_utc()
    recent = "https://reddit.com/r/read-last-week"
    ancient = "https://reddit.com/r/read-last-year"
    await _clip_row(
        sessionmaker, url=recent, fetched_at=now - datetime.timedelta(days=7),
        job_created_at=now - datetime.timedelta(days=7),
    )
    await _clip_row(
        sessionmaker, url=ancient, fetched_at=now - datetime.timedelta(days=365),
        job_created_at=now - datetime.timedelta(days=365),
    )

    async with sessionmaker() as session:
        seen = await jobs._recent_clip_urls(session, clock)

    assert recent in seen
    assert ancient not in seen


async def test_a_stale_failed_url_is_offered_to_search_again(sessionmaker, clock):
    """End to end: the ancient failure is not in `recent_urls`, so the
    search is free to return it and the job to try it."""
    settings = _study_settings()
    stale = "https://reddit.com/r/failed-long-ago"
    await _clip_row(
        sessionmaker, url=stale, fetched_at=None,
        job_created_at=clock.now_utc() - datetime.timedelta(days=90),
    )
    job_id, _ = await _enqueue_study(sessionmaker, clock, settings)

    searcher = _Search(error_code=search.NO_RESULTS)
    await _run_study(
        sessionmaker, clock, settings, job_id,
        search_fn=searcher, fetch_fn=_fetch_by_url({}), provider=FakeLLMProvider(),
    )

    assert stale not in searcher.seen[0]["recent_urls"]


# --- the safety_event rollup (H2's table, widened for phase 4) --------


async def _events(sessionmaker, kind: str) -> list[tuple[str, str]]:
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(SafetyEvent.kind, SafetyEvent.outcome).where(SafetyEvent.kind == kind)
            )
        ).all()
    return [(k, o) for k, o in rows]


async def test_a_distill_that_parsed_records_ok(sessionmaker, clock):
    settings = _settings()
    job_id = await _enqueue(sessionmaker, clock, settings)

    async with sessionmaker() as session:
        await jobs.run_research_job(
            session, settings, FakeLLMProvider(text=_one_card(SENTENCE1)),
            job_id=job_id, url=URL, clock=clock, timezone=TIMEZONE,
            fetch_fn=_fetch_ok(_clip()),
        )

    assert await _events(sessionmaker, "distill") == [("distill", "ok")]


async def test_a_distill_that_would_not_parse_records_parse_fail(sessionmaker, clock):
    """The blind spot this closes: unparseable JSON makes a `done` job
    with zero cards, which is indistinguishable from a run of genuinely
    unhelpful pages until something aggregates it."""
    settings = _settings()
    job_id = await _enqueue(sessionmaker, clock, settings)

    async with sessionmaker() as session:
        outcome = await jobs.run_research_job(
            session, settings, FakeLLMProvider(text="не json вовсе"),
            job_id=job_id, url=URL, clock=clock, timezone=TIMEZONE,
            fetch_fn=_fetch_ok(_clip()),
        )

    # Still `done` with no cards -- that behaviour is plan section 7's
    # and does not change. What changes is that the fault is now visible.
    assert outcome.status == "done"
    assert outcome.visible_cards == 0
    assert await _events(sessionmaker, "distill") == [("distill", "parse_fail")]


async def test_a_search_that_found_nothing_records_an_error(sessionmaker, clock):
    settings = _study_settings()
    job_id, _ = await _enqueue_study(sessionmaker, clock, settings)

    await _run_study(
        sessionmaker, clock, settings, job_id,
        search_fn=_Search(error_code=search.NO_RESULTS, calls=2),
        fetch_fn=_fetch_by_url({}), provider=FakeLLMProvider(),
    )

    assert await _events(sessionmaker, "search") == [("search", "error")]


async def test_a_search_that_found_candidates_records_ok(sessionmaker, clock):
    settings = _study_settings(RESEARCH_MAX_PINS=1)
    job_id, _ = await _enqueue_study(sessionmaker, clock, settings)
    a = "https://reddit.com/r/a"

    await _run_study(
        sessionmaker, clock, settings, job_id,
        search_fn=_Search(a), fetch_fn=_fetch_by_url({a: _clip(url=a)}),
        provider=FakeLLMProvider(text=_one_card(SENTENCE1)),
    )

    assert await _events(sessionmaker, "search") == [("search", "ok")]
    assert await _events(sessionmaker, "distill") == [("distill", "ok")]


async def test_a_search_never_made_records_nothing(sessionmaker, clock):
    """No call, no outcome. A row saying a search failed when none was
    attempted would be the same lie the interrupted-job branch used to
    tell."""
    settings = _study_settings(RESEARCH_MAX_SEARCHES=0)
    job_id, _ = await _enqueue_study(sessionmaker, clock, settings)

    await _run_study(
        sessionmaker, clock, settings, job_id,
        search_fn=_Search(error_code=search.NO_RESULTS, calls=0),
        fetch_fn=_fetch_by_url({}), provider=FakeLLMProvider(),
    )

    assert await _events(sessionmaker, "search") == []


async def test_the_row_carries_no_page_content(sessionmaker, clock):
    """Plan section 12. The table records that a call happened and how it
    ended, and nothing a page or a card said."""
    settings = _settings()
    job_id = await _enqueue(sessionmaker, clock, settings)

    async with sessionmaker() as session:
        await jobs.run_research_job(
            session, settings, FakeLLMProvider(text=_one_card(SENTENCE1)),
            job_id=job_id, url=URL, clock=clock, timezone=TIMEZONE,
            fetch_fn=_fetch_ok(_clip()),
        )
        [row] = (
            await session.execute(select(SafetyEvent).where(SafetyEvent.kind == "distill"))
        ).scalars().all()

    assert row.model == "cydonia-fake"
    for column in ("url", "text", "quote", "topic", "domain"):
        assert not hasattr(row, column)


async def test_an_observability_failure_cannot_fail_the_job(sessionmaker, clock):
    """H2's rule, inherited: a row that could raise would be able to
    fail the job it observes, which is worse than the blindness."""
    settings = _settings()
    job_id = await _enqueue(sessionmaker, clock, settings)

    async with sessionmaker() as session:
        await safety_events.record_in(
            session, clock=clock, timezone=TIMEZONE,
            kind="not-a-real-kind", outcome="ok",
        )
        # The bad row is staged, not raised. It will fail at flush, which
        # is why record() exists for callers that cannot afford that --
        # here the point is only that staging itself never raises.
        session.expunge_all()
        outcome = await jobs.run_research_job(
            session, settings, FakeLLMProvider(text=_one_card(SENTENCE1)),
            job_id=job_id, url=URL, clock=clock, timezone=TIMEZONE,
            fetch_fn=_fetch_ok(_clip()),
        )
    assert outcome.status == "done"
