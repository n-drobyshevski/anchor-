"""Suggested interest topics, the domain side: app/research/suggest.py
(anchor-interest-suggestions-plan.md sections 3, 4 and 5).

Every model call is a `FakeLLMProvider` with scripted JSON; the lens
notes are synthetic rows in the throwaway database. What these tests
care about most is the plan's one-way rule: the call sees topics,
dismissed suggestions and -- only while the lens is active -- lens
titles and summaries, and nothing else; and a topic exists only after a
tap (`decide`).
"""

from __future__ import annotations

import datetime
import decimal
import functools
import json

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.core.clock import FrozenClock
from app.db.models import (
    InterestSuggestBatch,
    InterestSuggestion,
    InterestTopic,
    Job,
    Journal,
    LensNote,
    Memory,
    Message,
    SpendLedger,
    UserState,
    VaultFile,
)
from app.llm.provider import LLMError, LLMUsage
from app.research import suggest
from conftest import FakeLLMProvider, make_bot

TIMEZONE = "Europe/Paris"
NOW = datetime.datetime(2026, 10, 24, 12, 0, tzinfo=datetime.timezone.utc)
LENS_TITLE = "Requisite variety"
LENS_SUMMARY = "A regulator needs at least as many states as the system it regulates."
KNOWLEDGE_BODY = "Private knowledge body that must never reach the call."


def _settings(**kw) -> Settings:
    base = dict(
        RESEARCH_ENABLED=True,
        DAILY_USD_CAP=10.0,
        PACKET_FORUMS="reddit.com",
        PACKET_REF="en.wikipedia.org",
        PACKET_GUIDES="",
        INTEREST_SUGGEST_PER_DAY=2,
        INTEREST_SUGGEST_MAX=5,
    )
    base.update(kw)
    return Settings(**base)


def _clock() -> FrozenClock:
    return FrozenClock(NOW)


def _reply(*items: dict) -> str:
    return json.dumps({"suggestions": list(items)}, ensure_ascii=False)


def _item(text="гигиена сна у подростков", packet="ref", why="Рядом с темой о сне.") -> dict:
    return {"text": text, "packet": packet, "why": why}


async def _topic(sessionmaker, text="бессонница", packet="forums", *, active=True) -> int:
    async with sessionmaker() as session:
        topic = InterestTopic(text=text, packet=packet, active=active)
        session.add(topic)
        await session.commit()
        return topic.id


async def _lens_note(sessionmaker, title=LENS_TITLE, summary=LENS_SUMMARY) -> int:
    async with sessionmaker() as session:
        file = VaultFile(path=f"Lens/{title}.md", role="note", note_class="knowledge")
        session.add(file)
        await session.flush()
        note = LensNote(
            vault_file_id=file.id, kind="concept", title=title, summary=summary,
            body="Body text.", body_hash=str(file.id).ljust(64, "x"), chars=10,
        )
        session.add(note)
        await session.commit()
        return note.id


async def _batch(sessionmaker, *, origin="manual", status="queued", created_at=NOW, **kw) -> int:
    async with sessionmaker() as session:
        batch = InterestSuggestBatch(
            origin=origin, status=status, local_date=created_at.date(), created_at=created_at, **kw
        )
        session.add(batch)
        await session.commit()
        return batch.id


async def _suggestion(sessionmaker, batch_id, text="тема", *, position=1, status="open", packet="ref"):
    async with sessionmaker() as session:
        row = InterestSuggestion(
            batch_id=batch_id, position=position, text=text, packet=packet, why="почему",
            status=status, decided_at=None if status == "open" else NOW,
        )
        session.add(row)
        await session.commit()
        return row.id


async def _run(sessionmaker, batch_id, provider, settings=None, clock=None, **kw):
    async with sessionmaker() as session:
        return await suggest.run_batch(
            session, settings or _settings(), clock or _clock(), batch_id=batch_id,
            timezone=TIMEZONE, provider=provider, **kw,
        )


async def _enqueue(sessionmaker, settings=None, clock=None) -> suggest.SuggestRun:
    async with sessionmaker() as session:
        return await suggest.enqueue_suggest(
            session, settings or _settings(), clock or _clock(), timezone=TIMEZONE
        )


async def _rows(sessionmaker, model):
    async with sessionmaker() as session:
        return list((await session.execute(select(model).order_by(model.id))).scalars())


def _user_message(provider: FakeLLMProvider) -> dict:
    (messages,) = provider.received_messages
    assert [m.role for m in messages] == ["system", "user"]
    return json.loads(messages[1].content)


# --- the input ----------------------------------------------------------------------


async def test_the_input_holds_topics_dismissed_and_packets_with_sites_only(sessionmaker):
    await _topic(sessionmaker, "бессонница", "forums")
    await _topic(sessionmaker, "старая тема", "ref", active=False)
    old = await _batch(sessionmaker, status="done")
    await _suggestion(sessionmaker, old, "отклонённая тема", status="dismissed")
    async with sessionmaker() as session:
        session.add(Message(role="user", content="личное сообщение пользователя"))
        await session.commit()
    await _lens_note(sessionmaker)

    batch_id = await _batch(sessionmaker)
    provider = FakeLLMProvider(text=_reply())
    await _run(sessionmaker, batch_id, provider)

    document = _user_message(provider)
    assert document == {
        "packets": [
            {"name": "forums", "meaning": suggest.PACKET_MEANINGS["forums"]},
            {"name": "ref", "meaning": suggest.PACKET_MEANINGS["ref"]},
        ],
        "topics": [
            {"text": "бессонница", "packet": "forums", "active": True},
            {"text": "старая тема", "packet": "ref", "active": False},
        ],
        "dismissed": ["отклонённая тема"],
        # LENS_ENABLED is off: no lens line at all.
        "lens": [],
    }
    everything = "".join(m.content for m in provider.received_messages[0])
    assert "личное сообщение" not in everything
    assert LENS_TITLE not in everything
    assert provider.received_schemas == [suggest.SUGGEST_SCHEMA]
    assert provider.received_conversation_ids == [f"anchor-interest-suggest-{batch_id}"]


async def test_lens_titles_and_summaries_go_in_only_while_the_lens_is_active(sessionmaker):
    await _lens_note(sessionmaker)
    batch_id = await _batch(sessionmaker)
    provider = FakeLLMProvider(text=_reply())
    await _run(sessionmaker, batch_id, provider, settings=_settings(LENS_ENABLED=True))
    assert _user_message(provider)["lens"] == [{"title": LENS_TITLE, "summary": LENS_SUMMARY}]
    assert "Body text." not in provider.received_messages[0][1].content


async def test_the_call_carries_topic_and_lens_strings_and_no_dialog_memory_or_journal(sessionmaker):
    """The privacy property, concretely: every string in what the provider
    received is either a topic, a dismissed text, a packet line or a lens
    title/summary; seeded sentinels from the user's conversation, memory,
    journal and a lens note's body appear nowhere, with the lens on."""
    await _topic(sessionmaker, "бессонница", "forums")
    await _lens_note(sessionmaker)
    async with sessionmaker() as session:
        session.add(Message(role="user", content="SENTINEL-DIALOG-user-said-this"))
        session.add(Message(role="assistant", content="SENTINEL-DIALOG-bot-said-this"))
        session.add(Memory(kind="identity", text="SENTINEL-MEMORY-fact", source="user"))
        session.add(Journal(local_date=NOW.date(), text="SENTINEL-JOURNAL-line"))
        file = VaultFile(path="Knowledge/private.md", role="note", note_class="knowledge")
        session.add(file)
        await session.flush()
        session.add(
            LensNote(
                vault_file_id=file.id, kind="concept", title="Titled note",
                summary="Its summary.", body=KNOWLEDGE_BODY, body_hash="b" * 64, chars=10,
            )
        )
        await session.commit()

    batch_id = await _batch(sessionmaker)
    provider = FakeLLMProvider(text=_reply())
    await _run(sessionmaker, batch_id, provider, settings=_settings(LENS_ENABLED=True))

    everything = "".join(m.content for m in provider.received_messages[0])
    for present in ("бессонница", LENS_TITLE, LENS_SUMMARY, "Titled note", "Its summary."):
        assert present in everything
    for absent in (
        "SENTINEL-DIALOG-user-said-this",
        "SENTINEL-DIALOG-bot-said-this",
        "SENTINEL-MEMORY-fact",
        "SENTINEL-JOURNAL-line",
        KNOWLEDGE_BODY,
        "Knowledge/private.md",
    ):
        assert absent not in everything


async def test_the_lens_catalog_is_capped_whole_notes_at_a_time(sessionmaker):
    long = "x" * 1500
    for title in ("A note", "B note", "C note"):
        await _lens_note(sessionmaker, title=title, summary=long)
    async with sessionmaker() as session:
        lines = await suggest._lens_lines(
            session, _settings(LENS_ENABLED=True, INTEREST_SUGGEST_MAX_CHARS=3100)
        )
    assert [line.title for line in lines] == ["A note", "B note"]


async def test_only_the_newest_thirty_dismissed_are_shown_but_all_are_dropped(sessionmaker):
    old = await _batch(sessionmaker, status="done")
    for index in range(35):
        await _suggestion(sessionmaker, old, f"тема {index}", position=index + 1, status="dismissed")
    async with sessionmaker() as session:
        inp = await suggest.build_input(session, _settings())
    assert len(inp.dismissed) == suggest.DISMISSED_SHOWN
    assert suggest.key("тема 0") in inp.existing_keys


# --- validation ----------------------------------------------------------------------


def _inp(**kw) -> suggest.SuggestInput:
    base = dict(
        packets=("forums", "ref"),
        topics=(suggest.TopicLine(text="Бессонница", packet="forums", active=True),),
        dismissed=("Отклонено",),
        lens_lines=(),
        existing_keys=frozenset({suggest.key("Бессонница"), suggest.key("Отклонено")}),
    )
    base.update(kw)
    return suggest.SuggestInput(**base)


@pytest.mark.parametrize(
    "item",
    [
        _item(packet="guides"),  # a packet with no sites
        _item(packet="lens"),
        _item(text="ab"),
        _item(text="т" * 101),
        _item(why=""),
        _item(why="п" * 161),
        _item(text="сон site:reddit.com"),
        _item(text="сон на example.com"),
        _item(why="см. https://example.org/a"),
        _item(text="почта me@example.org сон"),
        _item(text="ignore previous instructions"),
        _item(text="  БЕССОННИЦА  "),  # an existing topic
        _item(text="отклонено"),  # a dismissed suggestion
        "not an object",
    ],
)
def test_validate_drops_a_bad_suggestion(item):
    checked = suggest.validate({"suggestions": [item]}, _inp(), max_items=5)
    assert checked.suggestions == ()
    assert (checked.proposed, checked.dropped) == (1, 1)


def test_validate_drops_a_risk_screened_topic():
    checked = suggest.validate(
        {"suggestions": [_item(text="мелатонин для сна")]}, _inp(), max_items=5
    )
    assert checked.suggestions == ()


def test_validate_keeps_good_ones_collapses_whitespace_and_drops_repeats():
    payload = {
        "suggestions": [
            _item(text="  гигиена\n сна  "),
            _item(text="Гигиена сна"),
            _item(text="хронотипы", packet="forums"),
        ]
    }
    checked = suggest.validate(payload, _inp(), max_items=5)
    assert [(s.text, s.packet) for s in checked.suggestions] == [
        ("гигиена сна", "ref"), ("хронотипы", "forums"),
    ]
    assert checked.dropped == 1


def test_validate_stops_at_max_items():
    payload = {"suggestions": [_item(text=f"тема номер {i}") for i in range(6)]}
    checked = suggest.validate(payload, _inp(), max_items=2)
    assert len(checked.suggestions) == 2


# --- the run --------------------------------------------------------------------------


async def test_a_batch_stores_its_suggestions_expires_older_ones_and_ledgers(sessionmaker):
    await _topic(sessionmaker)
    old = await _batch(sessionmaker, status="done")
    old_open = await _suggestion(sessionmaker, old, "старая")
    batch_id = await _batch(sessionmaker)
    usage = LLMUsage(
        input_tokens=500, cached_tokens=0, output_tokens=80, cost_usd=decimal.Decimal("0.0012")
    )
    provider = FakeLLMProvider(
        text=_reply(_item(), _item(text="хронотипы", packet="forums", why="Соседняя тема.")),
        usage=usage,
    )
    outcome = await _run(sessionmaker, batch_id, provider)
    assert (outcome.status, outcome.suggestions, outcome.expired) == ("done", 2, 1)

    rows = [r for r in await _rows(sessionmaker, InterestSuggestion) if r.batch_id == batch_id]
    assert [(r.position, r.text, r.packet, r.status) for r in rows] == [
        (1, "гигиена сна у подростков", "ref", "open"),
        (2, "хронотипы", "forums", "open"),
    ]
    async with sessionmaker() as session:
        assert (await session.get(InterestSuggestion, old_open)).status == "expired"
        batch = await session.get(InterestSuggestBatch, batch_id)
        assert batch.usd_cost == decimal.Decimal("0.0012")
        assert batch.finished_at is not None
    (ledger,) = await _rows(sessionmaker, SpendLedger)
    assert (ledger.category, ledger.usd_cost) == ("interest_suggest", decimal.Decimal("0.0012"))
    # A batch proposes; it never makes a topic (only a tap does).
    assert len(await _rows(sessionmaker, InterestTopic)) == 1


async def test_a_batch_with_nothing_left_is_done_and_empty(sessionmaker):
    await _topic(sessionmaker)
    batch_id = await _batch(sessionmaker)
    outcome = await _run(sessionmaker, batch_id, FakeLLMProvider(text=_reply(_item(text="x"))))
    assert (outcome.status, outcome.suggestions, outcome.dropped) == ("done", 0, 1)


async def test_a_redelivered_batch_does_not_call_again(sessionmaker):
    await _topic(sessionmaker)
    batch_id = await _batch(sessionmaker)
    provider = FakeLLMProvider(text=_reply(_item()))
    await _run(sessionmaker, batch_id, provider)
    again = await _run(sessionmaker, batch_id, provider)
    assert provider.calls == 1
    assert (again.status, again.suggestions) == ("done", 1)
    missing = await _run(sessionmaker, batch_id + 100, provider)
    assert missing.error_code == suggest.NOT_FOUND


async def test_a_reply_that_does_not_parse_fails_the_batch_and_keeps_its_cost(sessionmaker):
    await _topic(sessionmaker)
    batch_id = await _batch(sessionmaker)
    outcome = await _run(sessionmaker, batch_id, FakeLLMProvider(text="not json"))
    assert (outcome.status, outcome.error_code) == ("failed", suggest.PARSE_FAIL)
    assert len(await _rows(sessionmaker, SpendLedger)) == 1


async def test_a_provider_error_fails_the_batch_unpaid(sessionmaker):
    await _topic(sessionmaker)
    batch_id = await _batch(sessionmaker)
    outcome = await _run(sessionmaker, batch_id, FakeLLMProvider(raises=[LLMError("down")]))
    assert (outcome.status, outcome.error_code) == ("failed", suggest.PROVIDER_ERROR)
    assert await _rows(sessionmaker, SpendLedger) == []


@pytest.mark.parametrize(
    ("settings", "seed_topic", "code"),
    [
        (_settings(RESEARCH_ENABLED=False), True, suggest.DISABLED),
        (_settings(), False, suggest.NOTHING),
        (_settings(PACKET_FORUMS="", PACKET_REF=""), True, suggest.NO_PACKET),
        (_settings(DAILY_USD_CAP=0.0), True, suggest.CAP),
    ],
)
async def test_the_run_refuses_before_the_call(sessionmaker, settings, seed_topic, code):
    if seed_topic:
        await _topic(sessionmaker)
    batch_id = await _batch(sessionmaker)
    provider = FakeLLMProvider(text=_reply(_item()))
    outcome = await _run(sessionmaker, batch_id, provider, settings=settings)
    assert (outcome.status, outcome.error_code, provider.calls) == ("failed", code, 0)


async def test_a_preempted_weekly_batch_never_calls(sessionmaker):
    await _topic(sessionmaker)
    batch_id = await _batch(sessionmaker, origin="weekly")
    provider = FakeLLMProvider(text=_reply(_item()))

    async def _no() -> bool:
        return False

    outcome = await _run(sessionmaker, batch_id, provider, may_call=_no)
    assert (outcome.error_code, provider.calls) == (suggest.PREEMPTED, 0)


# --- enqueue ---------------------------------------------------------------------------


async def test_enqueue_queues_a_batch_and_its_job(sessionmaker):
    await _topic(sessionmaker)
    result = await _enqueue(sessionmaker)
    assert result.refusal is None and not result.resend
    (batch,) = await _rows(sessionmaker, InterestSuggestBatch)
    assert (batch.id, batch.origin, batch.status, batch.local_date) == (
        result.batch_id, "manual", "queued", datetime.date(2026, 10, 24),
    )
    (job,) = await _rows(sessionmaker, Job)
    assert (job.kind, job.payload, job.dedup_key) == (
        suggest.INTEREST_SUGGEST, {"batch_id": batch.id}, f"suggest:{batch.id}",
    )


async def test_enqueue_with_only_an_active_lens_is_not_nothing(sessionmaker):
    await _lens_note(sessionmaker)
    assert (await _enqueue(sessionmaker)).refusal == suggest.NOTHING
    result = await _enqueue(sessionmaker, settings=_settings(LENS_ENABLED=True))
    assert result.refusal is None


@pytest.mark.parametrize(
    ("settings", "code"),
    [
        (_settings(RESEARCH_ENABLED=False), suggest.DISABLED),
        (_settings(PACKET_FORUMS="", PACKET_REF=""), suggest.NO_PACKET),
        (_settings(DAILY_USD_CAP=0.0), suggest.CAP),
        (_settings(INTEREST_SUGGEST_PER_DAY=0), suggest.QUOTA),
    ],
)
async def test_enqueue_refusals(sessionmaker, settings, code):
    await _topic(sessionmaker)
    assert (await _enqueue(sessionmaker, settings=settings)).refusal == code
    assert await _rows(sessionmaker, InterestSuggestBatch) == []
    assert await _rows(sessionmaker, Job) == []


async def test_enqueue_with_no_active_topic_is_nothing(sessionmaker):
    await _topic(sessionmaker, active=False)
    assert (await _enqueue(sessionmaker)).refusal == suggest.NOTHING


async def test_enqueue_is_busy_while_a_batch_is_queued_until_it_goes_stale(sessionmaker):
    await _topic(sessionmaker)
    first = await _enqueue(sessionmaker)
    assert (await _enqueue(sessionmaker)).refusal == suggest.BUSY
    later = _clock()
    later.advance(suggest.QUEUED_STALE + datetime.timedelta(minutes=1))
    result = await _enqueue(sessionmaker, clock=later)
    assert result.refusal is None
    async with sessionmaker() as session:
        stale = await session.get(InterestSuggestBatch, first.batch_id)
    assert (stale.status, stale.error_code) == ("failed", suggest.INTERRUPTED)


async def test_the_quota_counts_manual_batches_only(sessionmaker):
    await _topic(sessionmaker)
    for _ in range(2):
        await _batch(sessionmaker, origin="weekly", status="failed")
    await _batch(sessionmaker, status="failed")
    settings = _settings(INTEREST_SUGGEST_PER_DAY=2)
    assert (await _enqueue(sessionmaker, settings=settings)).refusal is None
    async with sessionmaker() as session:
        batch = (
            await session.execute(
                select(InterestSuggestBatch).where(InterestSuggestBatch.status == "queued")
            )
        ).scalar_one()
        batch.status = "failed"
        await session.commit()
    assert (await _enqueue(sessionmaker, settings=settings)).refusal == suggest.QUOTA


async def test_an_unsent_done_batch_is_handed_back_before_any_refusal(sessionmaker):
    batch_id = await _batch(sessionmaker, status="done")
    result = await _enqueue(sessionmaker, settings=_settings(INTEREST_SUGGEST_PER_DAY=0))
    assert result == suggest.SuggestRun(refusal=None, batch_id=batch_id, resend=True)
    assert len(await _rows(sessionmaker, InterestSuggestBatch)) == 1


# --- the send hook's helpers ------------------------------------------------------------


async def test_unsent_batch_mark_sent_and_the_48_hour_expiry(sessionmaker):
    old = await _batch(sessionmaker, status="done", created_at=NOW - datetime.timedelta(hours=60))
    old_open = await _suggestion(sessionmaker, old, "старая")
    async with sessionmaker() as session:
        assert await suggest.unsent_batch(session, NOW) is None
        assert await suggest.expire_unsent(session, NOW) == 1
        await session.commit()
        assert (await session.get(InterestSuggestion, old_open)).status == "expired"

    batch_id = await _batch(sessionmaker, status="done")
    await _suggestion(sessionmaker, batch_id, "новая")
    async with sessionmaker() as session:
        view = await suggest.unsent_batch(session, NOW)
        assert view.id == batch_id and [s.text for s in view.suggestions] == ["новая"]
        assert await suggest.mark_sent(session, batch_id, 777, NOW)
        assert not await suggest.mark_sent(session, batch_id, 778, NOW)
        await session.commit()
        assert await suggest.unsent_batch(session, NOW) is None
        assert (await session.get(InterestSuggestBatch, batch_id)).message_id == 777


async def test_an_older_unsent_batch_behind_a_newer_one_is_never_sent(sessionmaker):
    await _batch(sessionmaker, status="done", created_at=NOW - datetime.timedelta(hours=2))
    await _batch(sessionmaker, status="done", sent_at=NOW, message_id=5)
    async with sessionmaker() as session:
        assert await suggest.unsent_batch(session, NOW) is None


# --- taps -----------------------------------------------------------------------------


async def _decide(sessionmaker, suggestion_id, epoch, action, settings=None):
    async with sessionmaker() as session:
        return await suggest.decide(
            session, settings or _settings(), suggestion_id=suggestion_id, epoch=epoch,
            action=action, now=NOW,
        )


async def _epoch(sessionmaker, batch_id) -> str:
    async with sessionmaker() as session:
        return (await session.get(InterestSuggestBatch, batch_id)).epoch


async def test_add_creates_one_topic_and_a_replay_is_stale(sessionmaker):
    batch_id = await _batch(sessionmaker, status="done")
    sid = await _suggestion(sessionmaker, batch_id, "хронотипы", packet="forums")
    epoch = await _epoch(sessionmaker, batch_id)
    decision = await _decide(sessionmaker, sid, epoch, suggest.ADD_AND_RUN)
    assert (decision.outcome, decision.text, decision.packet, decision.batch_id) == (
        suggest.OK, "хронотипы", "forums", batch_id,
    )
    (topic,) = await _rows(sessionmaker, InterestTopic)
    assert (topic.id, topic.text, topic.packet, topic.active) == (
        decision.topic_id, "хронотипы", "forums", True,
    )
    async with sessionmaker() as session:
        row = await session.get(InterestSuggestion, sid)
        assert (row.status, row.topic_id) == ("added", topic.id)
    assert (await _decide(sessionmaker, sid, epoch, suggest.ADD)).outcome == suggest.STALE
    assert len(await _rows(sessionmaker, InterestTopic)) == 1


async def test_dismiss_and_a_stale_epoch(sessionmaker):
    batch_id = await _batch(sessionmaker, status="done")
    sid = await _suggestion(sessionmaker, batch_id)
    epoch = await _epoch(sessionmaker, batch_id)
    wrong = "aaaaaa" if epoch != "aaaaaa" else "bbbbbb"
    assert (await _decide(sessionmaker, sid, wrong, suggest.DISMISS)).outcome == suggest.STALE
    assert (await _decide(sessionmaker, sid, epoch, "z")).outcome == suggest.STALE
    assert (await _decide(sessionmaker, 2**40, epoch, suggest.ADD)).outcome == suggest.STALE
    decision = await _decide(sessionmaker, sid, epoch, suggest.DISMISS)
    assert (decision.outcome, decision.topic_id) == (suggest.OK, None)
    async with sessionmaker() as session:
        assert (await session.get(InterestSuggestion, sid)).status == "dismissed"
    assert await _rows(sessionmaker, InterestTopic) == []


async def test_a_topic_cap_refusal_keeps_the_suggestion_open(sessionmaker):
    for index in range(10):
        await _topic(sessionmaker, f"тема {index}")
    batch_id = await _batch(sessionmaker, status="done")
    sid = await _suggestion(sessionmaker, batch_id, "ещё одна")
    epoch = await _epoch(sessionmaker, batch_id)
    assert (await _decide(sessionmaker, sid, epoch, suggest.ADD)).outcome == "cap"
    async with sessionmaker() as session:
        row = await session.get(InterestSuggestion, sid)
        assert (row.status, row.decided_at, row.topic_id) == ("open", None, None)
    assert len(await _rows(sessionmaker, InterestTopic)) == 10


# --- the worker ----------------------------------------------------------------------


async def test_the_worker_runs_the_batch_then_the_send_hook(sessionmaker, monkeypatch):
    from app import worker as worker_module

    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=42, timezone=TIMEZONE))
        await session.commit()
    await _topic(sessionmaker)
    queued = await _enqueue(sessionmaker)
    provider = FakeLLMProvider(text=_reply(_item()))
    monkeypatch.setattr(
        worker_module, "run_suggest_batch", functools.partial(suggest.run_batch, provider=provider)
    )
    sent: list[int] = []

    async def _send(sessionmaker_, bot, settings, clock):
        async with sessionmaker_() as session:
            view = await suggest.unsent_batch(session, clock.now_utc())
        sent.append(view.id)
        return True

    monkeypatch.setattr(worker_module.interests_ui, "send_suggestions", _send)
    bot, _fake = make_bot()
    assert await worker_module.process_one_job(
        sessionmaker, _settings(), FakeLLMProvider(), _clock(), bot
    )
    assert sent == [queued.batch_id]
    (job,) = await _rows(sessionmaker, Job)
    assert job.status == "done"
    async with sessionmaker() as session:
        assert (await session.get(InterestSuggestBatch, queued.batch_id)).status == "done"
        assert (await session.execute(select(func.count()).select_from(Memory))).scalar_one() == 0
