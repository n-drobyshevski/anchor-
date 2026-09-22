"""Adopted techniques in the prompt (phase-4 plan sections 10, 12, 14).

A `technique` memory is the only thing in this bot that started life as
text on a stranger's web page. It got here by surviving the fetcher,
the isolated distill call, the verbatim-quote anchor, the injection
list and the risk rules -- and then by the user reading it in /notes
and pressing [Принять]. This file is about what happens after that:
which turns see it, which must not, and how many at a time.

The first test is the one worth reading. Until 4d, an adopted technique
was an ordinary unpinned memory, so `retrieve_memories` returned it
into the general "Может быть важно" block with no separate cap. Nothing
caught it because RESEARCH_ENABLED was false through 4b and 4c and no
technique could exist yet.
"""

from __future__ import annotations

import datetime

import pytest

from app.core import memory, prompt
from app.core.clock import FrozenClock
from app.db.models import Memory

TIMEZONE = "Europe/Paris"
NOW = datetime.datetime(2026, 9, 22, 12, 0, tzinfo=datetime.timezone.utc)


def _clock() -> FrozenClock:
    return FrozenClock(NOW)


async def _add(session, text, *, kind="technique", last_used_at=None, source="adopt"):
    row = Memory(kind=kind, text=text, source=source, last_used_at=last_used_at)
    session.add(row)
    await session.flush()
    return row


# --- the pools are separate ---------------------------------------------


async def test_a_technique_is_not_returned_by_ordinary_retrieval(sessionmaker):
    """The leak 4d closed.

    A technique competing for MEMORY_RETRIEVED_MAX slots would crowd out
    facts about the user -- and would arrive under "Может быть важно",
    which tells the model it is something to stay consistent with rather
    than a method it may choose to use.
    """
    async with sessionmaker() as session:
        await _add(session, "Ложиться спать в одно и то же время каждый день.")
        await _add(session, "Пользователь любит ложиться поздно.", kind="preference", source="user")
        await session.commit()

        rows = await memory.retrieve_memories(session, "во сколько ложиться спать", 6)

    kinds = {row.kind for row in rows}
    assert "technique" not in kinds
    assert kinds == {"preference"}


async def test_a_technique_is_returned_by_the_technique_pool(sessionmaker):
    async with sessionmaker() as session:
        await _add(session, "Ложиться спать в одно и то же время каждый день.")
        await session.commit()

        rows = await memory.retrieve_techniques(session, "во сколько ложиться спать", 2)

    assert [row.text for row in rows] == ["Ложиться спать в одно и то же время каждый день."]


async def test_only_techniques_are_in_the_technique_pool(sessionmaker):
    async with sessionmaker() as session:
        await _add(session, "Пользователь ложится поздно.", kind="preference", source="user")
        await _add(session, "Никогда не давить на пользователя.", kind="rule", source="user")
        await session.commit()

        rows = await memory.retrieve_techniques(session, "во сколько ложиться спать", 2)

    assert rows == []


# --- the cap ------------------------------------------------------------


async def test_the_pool_is_capped(sessionmaker):
    """RESEARCH_TECHNIQUES_IN_PROMPT defaults to 2. These compete with
    retrieved memories for the same attention; the persona is not a
    reference manual."""
    async with sessionmaker() as session:
        for n in range(6):
            await _add(session, f"Приём про сон номер {n}: ложиться раньше каждый вечер.")
        await session.commit()

        rows = await memory.retrieve_techniques(session, "приём про сон", 2)

    assert len(rows) == 2


async def test_a_zero_cap_returns_nothing_and_asks_the_database_nothing(sessionmaker):
    async with sessionmaker() as session:
        await _add(session, "Ложиться спать в одно и то же время каждый день.")
        await session.commit()
        assert await memory.retrieve_techniques(session, "сон", 0) == []


# --- the fallback -------------------------------------------------------


async def test_nothing_matching_falls_back_to_least_recently_used(sessionmaker):
    """Plan section 10's fallback. The opposite of `_topup`'s known
    distortion: least-recently-used rotates, so a card adopted months
    ago is eventually tried instead of never being seen again."""
    async with sessionmaker() as session:
        await _add(session, "Гулять днём хотя бы двадцать минут.", last_used_at=NOW)
        await _add(
            session,
            "Разбивать задачу на три шага.",
            last_used_at=NOW - datetime.timedelta(days=30),
        )
        await session.commit()

        rows = await memory.retrieve_techniques(session, "совершенно посторонняя тема", 1)

    assert [row.text for row in rows] == ["Разбивать задачу на три шага."]


async def test_a_never_used_technique_is_the_least_recently_used_one(sessionmaker):
    """NULLS FIRST is explicit in the query for exactly this: Postgres
    would otherwise sort a never-used row last under ASC, so a freshly
    adopted card would be the last one ever offered."""
    async with sessionmaker() as session:
        await _add(session, "Гулять днём хотя бы двадцать минут.", last_used_at=NOW)
        await _add(session, "Совсем новый приём, ещё ни разу не использованный.")
        await session.commit()

        rows = await memory.retrieve_techniques(session, "посторонняя тема", 1)

    assert rows[0].text.startswith("Совсем новый приём")


async def test_a_match_beats_the_fallback_but_the_fallback_fills_the_rest(sessionmaker):
    async with sessionmaker() as session:
        await _add(session, "Ложиться спать в одно и то же время каждый день.")
        await _add(
            session, "Разбивать задачу на три шага.", last_used_at=NOW - datetime.timedelta(days=9)
        )
        await _add(session, "Гулять днём двадцать минут.", last_used_at=NOW)
        await session.commit()

        rows = await memory.retrieve_techniques(session, "во сколько ложиться спать", 2)

    texts = [row.text for row in rows]
    assert texts[0] == "Ложиться спать в одно и то же время каждый день."
    assert texts[1] == "Разбивать задачу на три шага.", "the fallback fills by least recent"


async def test_the_fallback_never_repeats_a_row_it_already_matched(sessionmaker):
    async with sessionmaker() as session:
        await _add(session, "Ложиться спать в одно и то же время каждый день.")
        await session.commit()

        rows = await memory.retrieve_techniques(session, "во сколько ложиться спать", 2)

    assert len(rows) == 1, "one technique cannot be offered twice to fill a cap of two"


async def test_a_superseded_technique_is_never_offered(sessionmaker):
    async with sessionmaker() as session:
        old = await _add(session, "Старый приём про сон, заменённый новым.")
        new = await _add(session, "Новый приём: ложиться до полуночи каждый день.")
        old.superseded_by = new.id
        await session.commit()

        rows = await memory.retrieve_techniques(session, "приём про сон", 2)

    assert [row.text for row in rows] == ["Новый приём: ложиться до полуночи каждый день."]


async def test_a_short_message_still_gets_techniques(sessionmaker):
    """Unlike ordinary retrieval, which needs MIN_QUERY_CHARS. «не могу
    начать» is four words and exactly when a method helps."""
    async with sessionmaker() as session:
        await _add(session, "Разбивать задачу на три шага.")
        await session.commit()

        assert await memory.retrieve_techniques(session, "не могу", 2) != []
        assert await memory.retrieve_memories(session, "не могу", 6) == []


# --- the prompt block ---------------------------------------------------


def test_the_block_has_its_own_header_and_says_who_approved_it():
    block = prompt.build_now_block(
        clock=_clock(),
        timezone=TIMEZONE,
        intensity=3,
        retrieved=["пользователь любит бегать"],
        techniques=["Ложиться в одно и то же время."],
    )
    assert prompt.TECHNIQUES_HEADER == "## Приёмы (одобрены тобой)"
    assert prompt.TECHNIQUES_HEADER in block
    assert "- Ложиться в одно и то же время." in block
    # Separate from the retrieved block: one is a fact to stay
    # consistent with, the other a method that may be used.
    assert block.index(prompt.RETRIEVED_HEADER) < block.index(prompt.TECHNIQUES_HEADER)


def test_no_techniques_means_no_header_at_all():
    """Same convention as every other optional block: omitted entirely
    rather than emitted empty."""
    block = prompt.build_now_block(clock=_clock(), timezone=TIMEZONE, intensity=3)
    assert prompt.TECHNIQUES_HEADER not in block


def test_the_flags_stay_last():
    block = prompt.build_now_block(
        clock=_clock(),
        timezone=TIMEZONE,
        intensity=3,
        techniques=["Ложиться в одно и то же время."],
        flags=["ФЛАГ: тест"],
    )
    assert block.index(prompt.TECHNIQUES_HEADER) < block.index("ФЛАГ: тест")
    assert block.rstrip().endswith("ФЛАГ: тест")


# --- which calls see them -----------------------------------------------


@pytest.mark.parametrize("module", ["extract", "welfare", "tick", "scene"])
def test_no_background_call_site_retrieves_techniques(module):
    """Plan section 10: "used for chat turns and outbound generation,
    **not** for the extractor or classifier".

    A technique is text that came from the open web. The extractor
    decides what gets written to memory and the classifier decides
    whether the user is in trouble; neither should be reading a
    stranger's phrasing while it does that.
    """
    import importlib
    import inspect

    source = inspect.getsource(importlib.import_module(f"app.core.{module}"))
    assert "retrieve_techniques" not in source
    assert "techniques=" not in source


# --- the invariant (plan sections 12, 14) -------------------------------


async def test_fetched_page_text_never_reaches_a_built_prompt(sessionmaker, clock):
    """Plan section 14: "build prompts after a job and assert the clip
    text is absent".

    The end of the one-way valve, checked end to end. A whole page is
    fetched and stored, a card is distilled from it, the card is
    adopted -- and the prompt that goes to the persona model contains
    the *card's* sentence and no other word of the page. Not the clip
    text, not the untouched paragraphs, not the title.
    """
    from app.core import cards
    from app.db.models import StudyCard, StudyClip, StudyJob

    page = (
        "СЕКРЕТНЫЙ АБЗАЦ КОТОРЫЙ НЕ ДОЛЖЕН ПОПАСТЬ В ПРОМПТ. "
        "Ложитесь спать в одно и то же время каждый день. "
        "ЕЩЁ ОДИН АБЗАЦ СО СТРАНИЦЫ КОТОРЫЙ ТОЖЕ НЕ ДОЛЖЕН ПОПАСТЬ."
    )

    async with sessionmaker() as session:
        job = StudyJob(kind="read", local_date=datetime.date(2026, 9, 22), status="done")
        session.add(job)
        await session.flush()
        clip = StudyClip(
            job_id=job.id,
            url="https://example.com/sleep",
            domain="example.com",
            title="ЗАГОЛОВОК СТРАНИЦЫ",
            text=page,
            http_status=200,
            fetched_at=clock.now_utc(),
        )
        session.add(clip)
        await session.flush()
        card = StudyCard(
            job_id=job.id,
            clip_id=clip.id,
            kind="technique",
            text="Ложиться в одно и то же время.",
            quote="Ложитесь спать в одно и то же время каждый день.",
            source_url=clip.url,
            risk_model="low",
            risk_rules="low",
            risk_final="low",
        )
        session.add(card)
        await session.commit()
        card_id = card.id

        assert await cards.adopt(session, card_id, clock=clock) == cards.ADOPTED
        await session.commit()

        rows = await memory.retrieve_techniques(session, "во сколько ложиться", 2)

    block = prompt.build_now_block(
        clock=_clock(), timezone=TIMEZONE, intensity=3,
        techniques=[row.text for row in rows],
    )

    # The adopted card's own sentence is there -- that is the point.
    assert "Ложиться в одно и то же время." in block
    # Nothing else from the page is, including the sentence the quote
    # was taken from: a card carries the model's paraphrase, not the
    # page's words.
    assert "СЕКРЕТНЫЙ АБЗАЦ" not in block
    assert "ЕЩЁ ОДИН АБЗАЦ" not in block
    assert "ЗАГОЛОВОК СТРАНИЦЫ" not in block
    assert "Ложитесь спать в одно и то же время каждый день." not in block
    assert "example.com" not in block, "not even the source domain"


async def test_a_rejected_card_never_reaches_the_prompt(sessionmaker, clock):
    """Adopting is the only way out of the valve. A card the user said
    no to is not a quieter yes."""
    from app.core import cards
    from app.db.models import StudyCard, StudyClip, StudyJob

    async with sessionmaker() as session:
        job = StudyJob(kind="read", local_date=datetime.date(2026, 9, 22), status="done")
        session.add(job)
        await session.flush()
        clip = StudyClip(job_id=job.id, url="https://example.com/x", domain="example.com", text="т")
        session.add(clip)
        await session.flush()
        session.add(
            StudyCard(
                job_id=job.id, clip_id=clip.id, kind="technique",
                text="Отклонённый приём, который пользователь не принял.",
                quote="цитата со страницы длиной побольше двадцати четырёх",
                source_url=clip.url,
                risk_model="low", risk_rules="low", risk_final="low",
            )
        )
        await session.commit()
        card = (await session.execute(__import__("sqlalchemy").select(StudyCard))).scalar_one()
        assert await cards.reject(session, card.id, clock=clock) == cards.REJECTED
        await session.commit()

        assert await memory.retrieve_techniques(session, "отклонённый приём", 2) == []


# --- /memories and /forget (plan section 10's last line) ----------------


async def test_memories_lists_techniques_with_their_kind(sessionmaker):
    """Plan section 10: "/memories shows techniques with kind
    `technique`". `list_active` has no kind filter, so this already
    held -- pinned here rather than changed, because a future filter
    added for tidiness would quietly hide the one memory kind the user
    most needs to be able to audit and remove."""
    async with sessionmaker() as session:
        await _add(session, "Ложиться спать в одно и то же время каждый день.")
        await _add(session, "Пользователь живёт в Лилле.", kind="identity", source="user")
        await session.commit()

        rows, total = await memory.list_active(session, offset=0, limit=10)

    assert total == 2
    by_kind = {row.kind: row.text for row in rows}
    assert by_kind["technique"] == "Ложиться спать в одно и то же время каждый день."


async def test_forget_removes_a_technique_like_any_other_memory(sessionmaker):
    """"/forget works on them as usual". The adopted card keeps its own
    row in study_card -- /forget is about what the persona sees, not
    about rewriting what the user decided."""
    async with sessionmaker() as session:
        row = await _add(session, "Ложиться спать в одно и то же время каждый день.")
        await session.commit()
        memory_id = row.id

        assert await memory.hard_delete(session, memory_id) is True
        await session.commit()

        assert await memory.retrieve_techniques(session, "во сколько ложиться", 2) == []
        _, total = await memory.list_active(session, offset=0, limit=10)
    assert total == 0
