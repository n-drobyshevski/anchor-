"""core/memory.py (phase-2 plan sections 4, 6, 11 and 14's "memory" line).

**Testing philosophy for trigram scores.** Any test asserting "this text
retrieves that memory" is implicitly asserting a float produced by a
particular pg_trgm version under a particular locale. Exactly one test
here touches that -- test_pg_trgm_sees_cyrillic -- so a locale or
version regression fails in one obvious place rather than flaking six
retrieval tests. Everything else asserts *behaviour*: ranking, caps,
exclusion, top-up, dedupe-skip, using texts chosen to be unambiguous.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select, text as sql_text

from app.core import memory
from app.db.models import Memory

pytestmark = pytest.mark.asyncio


async def _write(session, *, kind="identity", txt="факт", source="user", pinned=False, **kw):
    row = Memory(kind=kind, text=txt, source=source, pinned=pinned, **kw)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


# --- the one fragile assertion, isolated ---


async def test_pg_trgm_sees_cyrillic(sessionmaker):
    """Plan section 4's migration check, as a test.

    Under a plain `C` locale pg_trgm treats non-ASCII bytes as
    non-alphanumeric: show_trgm returns nothing and every similarity()
    over Russian is 0 -- with no error at all. Retrieval would return
    nothing forever and read as a bug in the retrieval code. If this
    test fails, nothing else in this file is meaningful.
    """
    async with sessionmaker() as session:
        trigrams = await session.execute(
            sql_text("SELECT coalesce(array_length(show_trgm('привет мир'), 1), 0)")
        )
        assert trigrams.scalar_one() >= 8

        # Case folding has to work too, or "Лилль" and "лилль" are
        # different facts as far as dedupe is concerned.
        case = await session.execute(sql_text("SELECT similarity('ЛИЛЛЬ', 'лилль')"))
        assert case.scalar_one() == pytest.approx(1.0)


# --- retrieval ---


async def test_retrieval_ranks_the_relevant_memory_first(sessionmaker):
    async with sessionmaker() as session:
        await _write(session, txt="пользователь живёт в Лилле")
        await _write(session, txt="пользователь работает аналитиком")
        await _write(session, txt="пользователь бегает по утрам")

        rows = await memory.retrieve_memories(session, "я сегодня думал про Лилль", 6)

    assert rows, "a message clearly about Lille should retrieve something"
    assert rows[0].text == "пользователь живёт в Лилле"


async def test_retrieval_survives_a_long_message(sessionmaker):
    """The reason app/core/memory.py flips plan section 6's argument
    order: with user_text as word_similarity's first argument the score
    collapses as the message grows, so the longest and most
    context-rich messages would retrieve nothing at all."""
    async with sessionmaker() as session:
        await _write(session, txt="пользователь живёт в Лилле")
        long_message = (
            "слушай, я сегодня ехал домой с работы и всю дорогу думал про Лилль, "
            "тут как-то серо и сыро, и вообще непонятно зачем это всё"
        )
        rows = await memory.retrieve_memories(session, long_message, 6)

    assert [row.text for row in rows] == ["пользователь живёт в Лилле"]


async def test_unrelated_message_retrieves_nothing(sessionmaker):
    async with sessionmaker() as session:
        await _write(session, kind="preference", txt="пользователь не любит когда его торопят")
        await _write(session, kind="preference", txt="пользователь работает аналитиком")
        rows = await memory.retrieve_memories(session, "что приготовить на ужин сегодня", 6)

    # Both stored memories are 'preference', so the identity/rule top-up
    # has nothing to offer and the result is genuinely empty.
    assert rows == []


async def test_short_messages_skip_retrieval_entirely(sessionmaker):
    """"ок"/"да" produce three or four padded trigrams that match almost
    anything, so they would inject noise rather than context."""
    async with sessionmaker() as session:
        await _write(session, txt="пользователь живёт в Лилле")
        assert await memory.retrieve_memories(session, "ок", 6) == []
        assert await memory.retrieve_memories(session, "да", 6) == []


async def test_pinned_memories_never_appear_in_the_retrieved_set(sessionmaker):
    """They are injected as their own prompt block; including them here
    would render duplicate bullets and double-count use_count."""
    async with sessionmaker() as session:
        await _write(session, txt="пользователь живёт в Лилле", pinned=True)
        rows = await memory.retrieve_memories(session, "я сегодня думал про Лилль", 6)

    assert rows == []


async def test_superseded_memories_are_never_retrieved(sessionmaker):
    async with sessionmaker() as session:
        old = await _write(session, txt="пользователь живёт в Лилле")
        new = await _write(session, txt="пользователь переехал в Руан")
        old.superseded_by = new.id
        await session.commit()

        rows = await memory.retrieve_memories(session, "я сегодня думал про Лилль", 6)

    assert all(row.id != old.id for row in rows)


async def test_retrieval_respects_the_cap(sessionmaker):
    async with sessionmaker() as session:
        for i in range(10):
            await _write(session, txt=f"пользователь живёт в Лилле, район номер {i}")
        rows = await memory.retrieve_memories(session, "я сегодня думал про Лилль", 3)

    assert len(rows) == 3


async def test_ties_go_to_the_older_last_used_at_nulls_first(sessionmaker):
    """Plan section 6. NULLS FIRST must be explicit -- Postgres defaults
    ASC to NULLS LAST, which would silently invert this rule."""
    now = datetime.datetime.now(datetime.timezone.utc)
    async with sessionmaker() as session:
        # Identical text, so word_similarity ties and only last_used_at
        # can break it.
        used_recently = await _write(
            session, txt="пользователь живёт в Лилле", last_used_at=now
        )
        used_long_ago = await _write(
            session,
            txt="пользователь живёт в Лилле",
            last_used_at=now - datetime.timedelta(days=30),
        )
        never_used = await _write(session, txt="пользователь живёт в Лилле")

        rows = await memory.retrieve_memories(session, "я сегодня думал про Лилль", 3)

    assert [row.id for row in rows] == [never_used.id, used_long_ago.id, used_recently.id]


async def test_topup_fills_to_three_when_few_match(sessionmaker):
    """Plan section 6: "if fewer than 3 match, top up with the 3 most
    recent identity/rule memories" -- read as fill *to* 3, not add 3."""
    async with sessionmaker() as session:
        match = await _write(session, txt="пользователь живёт в Лилле")
        await _write(session, kind="identity", txt="пользователя зовут Ник")
        await _write(session, kind="rule", txt="пользователь не работает по воскресеньям")
        await _write(session, kind="preference", txt="пользователь пьёт чай без сахара")

        rows = await memory.retrieve_memories(session, "я сегодня думал про Лилль", 6)

    assert len(rows) == 3, "one match plus two top-ups"
    assert rows[0].id == match.id, "the real match still ranks first"
    # The preference is not a top-up kind.
    assert all(row.kind in ("identity", "rule") for row in rows)


async def test_topup_excludes_pinned_and_already_retrieved(sessionmaker):
    async with sessionmaker() as session:
        match = await _write(session, txt="пользователь живёт в Лилле")
        pinned = await _write(session, kind="identity", txt="пользователя зовут Ник", pinned=True)
        rows = await memory.retrieve_memories(session, "я сегодня думал про Лилль", 6)

    ids = [row.id for row in rows]
    assert pinned.id not in ids
    assert ids.count(match.id) == 1


async def test_topup_does_not_run_when_three_already_match(sessionmaker):
    async with sessionmaker() as session:
        for i in range(3):
            await _write(session, txt=f"пользователь живёт в Лилле, дом {i}")
        await _write(session, kind="rule", txt="пользователь не работает по воскресеньям")

        rows = await memory.retrieve_memories(session, "я сегодня думал про Лилль", 6)

    assert len(rows) == 3
    assert all("Лилл" in row.text for row in rows)


# --- pinned block ---


async def test_pinned_memories_newest_first_and_capped(sessionmaker):
    async with sessionmaker() as session:
        for i in range(5):
            await _write(session, txt=f"закреплённый факт {i}", pinned=True)
        await _write(session, txt="незакреплённый факт")

        rows = await memory.pinned_memories(session, 3)

    assert len(rows) == 3
    assert [row.text for row in rows] == [
        "закреплённый факт 4",
        "закреплённый факт 3",
        "закреплённый факт 2",
    ]


async def test_superseded_pins_are_not_pinned_memories(sessionmaker):
    async with sessionmaker() as session:
        old = await _write(session, txt="старый закреплённый факт", pinned=True)
        new = await _write(session, txt="новый факт")
        old.superseded_by = new.id
        await session.commit()

        assert await memory.pinned_memories(session, 8) == []
        assert await memory.count_pinned(session) == 0


# --- mark_used ---


async def test_mark_used_sets_timestamp_and_increments(sessionmaker):
    async with sessionmaker() as session:
        row = await _write(session, txt="факт")
        assert row.last_used_at is None and row.use_count == 0

        await memory.mark_used(session, [row.id])
        await session.commit()
        await session.refresh(row)

    assert row.last_used_at is not None
    assert row.use_count == 1


async def test_mark_used_with_no_ids_is_a_no_op(sessionmaker):
    async with sessionmaker() as session:
        await memory.mark_used(session, [])


# --- write: dedupe and supersede ---


async def test_write_memory_stores_and_returns_the_row(sessionmaker):
    async with sessionmaker() as session:
        row = await memory.write_memory(
            session, kind="identity", text="пользователь живёт в Лилле", source="user"
        )

    assert row is not None
    assert row.source == "user"
    assert row.superseded_by is None


async def test_near_duplicate_is_skipped(sessionmaker):
    async with sessionmaker() as session:
        await memory.write_memory(
            session, kind="identity", text="пользователь живёт в Лилле", source="user"
        )
        again = await memory.write_memory(
            session, kind="identity", text="пользователь живет в Лилле", source="user"
        )
        rows = (await session.execute(select(Memory))).scalars().all()

    assert again is None
    assert len(rows) == 1


async def test_a_different_fact_is_not_deduped(sessionmaker):
    async with sessionmaker() as session:
        await memory.write_memory(
            session, kind="identity", text="пользователь живёт в Лилле", source="user"
        )
        other = await memory.write_memory(
            session, kind="preference", text="пользователь пьёт чай без сахара", source="user"
        )

    assert other is not None


async def test_supersede_sets_both_pointers(sessionmaker):
    async with sessionmaker() as session:
        old = await memory.write_memory(
            session, kind="identity", text="пользователь живёт в Лилле", source="user"
        )
        new = await memory.write_memory(
            session,
            kind="identity",
            text="пользователь переехал в Руан",
            source="user",
            supersedes_id=old.id,
        )
        await session.refresh(old)

    assert new is not None
    assert old.superseded_by == new.id
    assert new.superseded_by is None


async def test_a_supersede_worded_like_its_target_is_not_deduped(sessionmaker):
    """The precedence rule. An explicit supersedes_id is an intent to
    replace, so the row being replaced is excluded from the dedupe scan
    -- otherwise a correction worded closely to the fact it corrects
    would be silently dropped and the supersede lost."""
    async with sessionmaker() as session:
        old = await memory.write_memory(
            session, kind="identity", text="пользователь живёт в Лилле", source="user"
        )
        new = await memory.write_memory(
            session,
            kind="identity",
            text="пользователь живет в Лилле",  # > 0.6 similar to `old`
            source="user",
            supersedes_id=old.id,
        )
        await session.refresh(old)

    assert new is not None, "the supersede must not be dropped as a duplicate"
    assert old.superseded_by == new.id


async def test_supersede_still_dedupes_against_other_memories(sessionmaker):
    """Only the superseded row is exempt; everything else is checked."""
    async with sessionmaker() as session:
        target = await memory.write_memory(
            session, kind="identity", text="пользователь работает аналитиком", source="user"
        )
        await memory.write_memory(
            session, kind="identity", text="пользователь живёт в Лилле", source="user"
        )
        clash = await memory.write_memory(
            session,
            kind="identity",
            text="пользователь живет в Лилле",
            source="user",
            supersedes_id=target.id,
        )
        await session.refresh(target)

    assert clash is None
    assert target.superseded_by is None, "a dropped write must not retire anything"


async def test_an_unknown_supersedes_id_is_ignored_not_fatal(sessionmaker):
    async with sessionmaker() as session:
        row = await memory.write_memory(
            session,
            kind="identity",
            text="пользователь живёт в Лилле",
            source="user",
            supersedes_id=999_999,
        )

    assert row is not None


async def test_an_already_superseded_row_cannot_be_superseded_again(sessionmaker):
    """Chains stay linear, which is what makes hard_delete's relink
    unambiguous."""
    async with sessionmaker() as session:
        a = await memory.write_memory(session, kind="identity", text="факт А", source="user")
        b = await memory.write_memory(
            session, kind="identity", text="факт Б", source="user", supersedes_id=a.id
        )
        c = await memory.write_memory(
            session, kind="identity", text="факт В", source="user", supersedes_id=a.id
        )
        await session.refresh(a)

    assert a.superseded_by == b.id, "a stays pointed at b, not c"
    assert c is not None


# --- hard delete ---


async def test_forget_deletes_the_row(sessionmaker):
    async with sessionmaker() as session:
        row = await _write(session, txt="факт")
        assert await memory.hard_delete(session, row.id) is True
        remaining = (await session.execute(select(Memory))).scalars().all()

    assert remaining == []


async def test_forget_a_missing_row_returns_false(sessionmaker):
    async with sessionmaker() as session:
        assert await memory.hard_delete(session, 999_999) is False


async def test_forget_relinks_a_chain_and_does_not_resurrect(sessionmaker):
    """Plan section 11 says /forget "clears any superseded_by pointers to
    it". Taken literally, deleting `b` from a -> b -> c would set
    a.superseded_by = NULL and make `a` -- a fact the user explicitly
    replaced -- active again, because they deleted a *different* row."""
    async with sessionmaker() as session:
        a = await _write(session, txt="факт А")
        b = await _write(session, txt="факт Б")
        c = await _write(session, txt="факт В")
        a.superseded_by = b.id
        b.superseded_by = c.id
        await session.commit()

        await memory.hard_delete(session, b.id)
        await session.refresh(a)

    assert a.superseded_by == c.id, "a must stay retired, relinked to c"
    assert a.superseded_by is not None, "a must not come back to life"


async def test_forget_the_head_of_a_chain_clears_the_pointer(sessionmaker):
    """When the deleted row had no successor, relinking degenerates into
    exactly the clear section 11 describes."""
    async with sessionmaker() as session:
        a = await _write(session, txt="факт А")
        b = await _write(session, txt="факт Б")
        a.superseded_by = b.id
        await session.commit()

        await memory.hard_delete(session, b.id)
        await session.refresh(a)

    assert a.superseded_by is None


# --- pin / list ---


async def test_set_pinned_toggles(sessionmaker):
    async with sessionmaker() as session:
        row = await _write(session, txt="факт")
        assert (await memory.set_pinned(session, row.id, True)).pinned is True
        assert (await memory.set_pinned(session, row.id, False)).pinned is False


async def test_set_pinned_refuses_a_superseded_row(sessionmaker):
    async with sessionmaker() as session:
        old = await _write(session, txt="старый факт")
        new = await _write(session, txt="новый факт")
        old.superseded_by = new.id
        await session.commit()

        assert await memory.set_pinned(session, old.id, True) is None
        assert await memory.get_active(session, old.id) is None


async def test_list_active_pages_and_counts(sessionmaker):
    async with sessionmaker() as session:
        for i in range(25):
            await _write(session, txt=f"факт {i}")
        first, total = await memory.list_active(session, offset=0, limit=20)
        second, _ = await memory.list_active(session, offset=20, limit=20)

    assert total == 25
    assert len(first) == 20 and len(second) == 5
    assert first[0].text == "факт 0"


async def test_list_active_excludes_superseded(sessionmaker):
    async with sessionmaker() as session:
        old = await _write(session, txt="старый факт")
        new = await _write(session, txt="новый факт")
        old.superseded_by = new.id
        await session.commit()

        rows, total = await memory.list_active(session, offset=0, limit=20)

    assert total == 1
    assert [row.id for row in rows] == [new.id]


# --- pending_memory ---


async def test_take_pending_consumes_the_row(sessionmaker):
    async with sessionmaker() as session:
        pending = await memory.add_pending(session, "я живу в Лилле")
    async with sessionmaker() as session:
        assert await memory.take_pending(session, pending.id) == "я живу в Лилле"
    async with sessionmaker() as session:
        assert await memory.take_pending(session, pending.id) is None


async def test_purge_pending_drops_only_stale_rows(sessionmaker):
    async with sessionmaker() as session:
        fresh = await memory.add_pending(session, "свежий")
        stale = await memory.add_pending(session, "старый")
        await session.execute(
            sql_text("UPDATE pending_memory SET created_at = now() - interval '2 days' WHERE id = :i"),
            {"i": stale.id},
        )
        await session.commit()

        assert await memory.purge_pending_older_than(session, datetime.timedelta(days=1)) == 1
        assert await memory.take_pending(session, fresh.id) == "свежий"


# --- prompt assembly (plan sections 7 and 14's "prompt" line) ---


async def _persona(tmp_path):
    path = tmp_path / "persona.md"
    path.write_text("# Anchor\n", encoding="utf-8")
    return path


async def test_prompt_section_order_matches_plan_section_7(sessionmaker, tmp_path):
    from app.core.prompt import build_messages

    async with sessionmaker() as session:
        messages = await build_messages(
            session,
            timezone="Europe/Paris",
            intensity=3,
            user_text="новое сообщение",
            update_id=999,
            transcript_turns=30,
            pinned=["пользователь живёт в Лилле"],
            summaries=["Говорили об отчёте."],
            retrieved=["пользователь пьёт чай без сахара"],
            persona_path=await _persona(tmp_path),
        )

    assert [m.role for m in messages] == ["system", "system", "system", "system", "user"]
    assert messages[0].content.startswith("# Anchor")
    assert messages[1].content.startswith("## Что ты знаешь (закреплено)")
    assert "- пользователь живёт в Лилле" in messages[1].content
    assert messages[2].content.startswith("## Прошлые сессии")
    assert "- Говорили об отчёте." in messages[2].content
    # Retrieved memories live inside the now block: volatile content last.
    assert messages[-2].content.startswith("## Сейчас")
    assert "## Может быть важно" in messages[-2].content
    assert "- пользователь пьёт чай без сахара" in messages[-2].content
    assert messages[-1].content == "новое сообщение"


async def test_empty_sections_are_omitted_not_emitted_as_bare_headers(sessionmaker, tmp_path):
    """An empty heading is noise to the model and churns the byte-stable
    prefix the ordering exists to protect."""
    from app.core.prompt import build_messages

    async with sessionmaker() as session:
        messages = await build_messages(
            session,
            timezone="Europe/Paris",
            intensity=3,
            user_text="новое сообщение",
            update_id=999,
            transcript_turns=30,
            pinned=[],
            summaries=[],
            retrieved=[],
            persona_path=await _persona(tmp_path),
        )

    blob = "\n".join(m.content for m in messages)
    assert "## Что ты знаешь" not in blob
    assert "## Прошлые сессии" not in blob
    assert "## Может быть важно" not in blob
    assert [m.role for m in messages] == ["system", "system", "user"]


async def test_memory_ids_never_reach_the_chat_prompt(sessionmaker, tmp_path):
    """Plan section 7: "Memory IDs are never shown to the chat model.
    Only the extractor sees IDs." build_messages takes strings, not
    rows, which is what makes this structural rather than incidental."""
    from app.core.prompt import build_messages

    async with sessionmaker() as session:
        rows = [
            await _write(session, txt="пользователь живёт в Лилле"),
            await _write(session, kind="preference", txt="пользователь пьёт чай без сахара"),
        ]
        messages = await build_messages(
            session,
            timezone="Europe/Paris",
            intensity=3,
            user_text="новое сообщение",
            update_id=999,
            transcript_turns=30,
            pinned=[rows[0].text],
            retrieved=[rows[1].text],
            persona_path=await _persona(tmp_path),
        )

    blob = "\n".join(m.content for m in messages)
    for row in rows:
        assert f"#{row.id}" not in blob
        assert f"id={row.id}" not in blob

    # And the assembler cannot even see an id: it is handed strings.
    import inspect

    import app.core.prompt as prompt_module

    assert "Memory" not in inspect.getsource(prompt_module)


async def test_persona_transcript_excludes_welfare_and_canned_rows(sessionmaker, tmp_path):
    """Plan section 7 item 4. Welfare rows are excluded by kind here and
    by ooc elsewhere -- one filter failing must not be enough to leak a
    welfare exchange into the persona's context."""
    from app.core.prompt import build_messages
    from app.db.models import Message as MessageRow

    async with sessionmaker() as session:
        session.add_all(
            [
                MessageRow(role="user", content="обычная реплика", ooc=False, kind="chat"),
                MessageRow(role="user", content="реплика чек-ина", ooc=False, kind="checkin"),
                # ooc deliberately False: kind alone must exclude it.
                MessageRow(role="assistant", content="кризисный ответ", ooc=False, kind="welfare"),
                MessageRow(role="assistant", content="канонический ответ", ooc=False, kind="canned"),
            ]
        )
        await session.commit()

        messages = await build_messages(
            session,
            timezone="Europe/Paris",
            intensity=3,
            user_text="новое сообщение",
            update_id=999,
            transcript_turns=30,
            persona_path=await _persona(tmp_path),
        )

    contents = [m.content for m in messages]
    assert "обычная реплика" in contents
    assert "реплика чек-ина" in contents
    assert "кризисный ответ" not in contents
    assert "канонический ответ" not in contents


async def test_neutral_mode_still_sees_canned_rows(sessionmaker):
    """The kind filter is a build_messages parameter, not a change to the
    shared transcript helper -- adding it there would silently alter 1d's
    neutral mode."""
    from app.core.prompt import build_neutral_messages
    from app.db.models import Message as MessageRow

    async with sessionmaker() as session:
        session.add(
            MessageRow(role="assistant", content="канонический ответ", ooc=True, kind="canned")
        )
        await session.commit()

        messages = await build_neutral_messages(session, user_text="привет", update_id=999)

    assert "канонический ответ" in [m.content for m in messages]


async def test_recent_summaries_skips_unsummarized_and_open_scenes(sessionmaker):
    """A scene under MIN_MESSAGES_FOR_SUMMARY keeps summary=NULL as a
    valid terminal state, so omitting that filter would put empty
    bullets in every prompt."""
    import datetime as dt

    from app.core.scene import recent_summaries
    from app.db.models import Scene

    now = dt.datetime.now(dt.timezone.utc)
    async with sessionmaker() as session:
        session.add_all(
            [
                Scene(started_at=now, ended_at=now, summary="первая"),
                Scene(started_at=now, ended_at=now, summary=None),  # too short to summarize
                Scene(started_at=now, ended_at=None, summary="не должна попасть"),  # still open
                Scene(started_at=now, ended_at=now + dt.timedelta(minutes=1), summary="вторая"),
            ]
        )
        await session.commit()

        summaries = await recent_summaries(session)

    assert summaries == ["первая", "вторая"], "oldest first, only closed and summarized"


# --- last_used_at only after delivery (plan sections 6 and 14) ---


async def _turn_setup(sessionmaker, update_id: int):
    from aiogram import Bot

    from app.db.models import TelegramUpdate, UserState
    from conftest import FakeSession

    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=4242, timezone="Europe/Paris"))
        await session.commit()
        session.add(TelegramUpdate(update_id=update_id, payload={}))
        await session.commit()
    fake = FakeSession()
    return Bot(token="123456:TESTTOKEN", session=fake), fake


async def test_a_delivered_turn_marks_its_injected_memories_used(sessionmaker):
    from app.config import Settings
    from app.core import turn
    from conftest import FakeLLMProvider

    update_id = 7001
    bot, fake = await _turn_setup(sessionmaker, update_id)
    async with sessionmaker() as session:
        pinned = await _write(session, txt="пользователь живёт в Лилле", pinned=True)
        retrieved = await _write(
            session, kind="preference", txt="пользователь пьёт чай без сахара"
        )

    await turn.run(
        sessionmaker,
        bot,
        Settings(),
        FakeLLMProvider(text="Принято."),
        chat_id=4242,
        update_id=update_id,
        user_text="я сегодня думал про чай без сахара и про Лилль",
    )

    assert len(fake.sent) == 1
    async with sessionmaker() as session:
        for row_id in (pinned.id, retrieved.id):
            row = await session.get(Memory, row_id)
            assert row.use_count == 1, f"memory {row_id} should be marked used"
            assert row.last_used_at is not None


async def test_a_failed_generation_does_not_mark_memories_used(sessionmaker):
    """Section 6 says "after the turn is delivered". A model failure is
    not a delivery."""
    from app.config import Settings
    from app.core import turn
    from app.llm.provider import LLMError
    from conftest import FakeLLMProvider

    update_id = 7002
    bot, fake = await _turn_setup(sessionmaker, update_id)
    async with sessionmaker() as session:
        row = await _write(session, txt="пользователь живёт в Лилле", pinned=True)

    await turn.run(
        sessionmaker,
        bot,
        Settings(),
        FakeLLMProvider(raises=[LLMError("boom")]),
        chat_id=4242,
        update_id=update_id,
        user_text="я сегодня думал про Лилль",
    )

    async with sessionmaker() as session:
        row = await session.get(Memory, row.id)
    assert row.use_count == 0
    assert row.last_used_at is None


async def test_a_failed_send_does_not_mark_memories_used(sessionmaker):
    """mark_used sits downstream of send_reply, so a send that raises
    never reaches it."""
    from app.config import Settings
    from app.core import turn
    from conftest import FakeLLMProvider

    update_id = 7003
    bot, fake = await _turn_setup(sessionmaker, update_id)
    async with sessionmaker() as session:
        row = await _write(session, txt="пользователь живёт в Лилле", pinned=True)

    async def _boom(*args, **kwargs):
        raise RuntimeError("telegram is down")

    import app.core.turn as turn_module

    original = turn_module.send_reply
    turn_module.send_reply = _boom
    try:
        with pytest.raises(RuntimeError):
            await turn.run(
                sessionmaker,
                bot,
                Settings(),
                FakeLLMProvider(text="Принято."),
                chat_id=4242,
                update_id=update_id,
                user_text="я сегодня думал про Лилль",
            )
    finally:
        turn_module.send_reply = original

    async with sessionmaker() as session:
        row = await session.get(Memory, row.id)
    assert row.use_count == 0
    assert row.last_used_at is None


async def test_a_replayed_delivered_turn_does_not_double_increment(sessionmaker):
    """A replay returns at step 2 without rebuilding the prompt, so the
    injected ids do not exist on that path and mark_used cannot run
    twice for one update_id."""
    from app.config import Settings
    from app.core import turn
    from conftest import FakeLLMProvider

    update_id = 7004
    async with sessionmaker() as session:
        row = await _write(session, txt="пользователь живёт в Лилле", pinned=True)

    bot, _ = await _turn_setup(sessionmaker, update_id)
    provider = FakeLLMProvider(text="Принято.")
    for _ in range(2):
        await turn.run(
            sessionmaker,
            bot,
            Settings(),
            provider,
            chat_id=4242,
            update_id=update_id,
            user_text="я сегодня думал про Лилль",
        )

    async with sessionmaker() as session:
        row = await session.get(Memory, row.id)
    assert provider.calls == 1, "the replay must not call the model again"
    assert row.use_count == 1, "nor increment use_count again"


async def test_a_neutral_turn_does_no_memory_work(sessionmaker):
    """Retrieval sits inside the persona branch, so neutral mode neither
    injects nor marks anything."""
    from app.config import Settings
    from app.core import turn
    from app.db.models import TelegramUpdate, UserState
    from conftest import FakeLLMProvider, FakeSession
    from aiogram import Bot

    update_id = 7005
    async with sessionmaker() as session:
        session.add(
            UserState(id=1, chat_id=4242, timezone="Europe/Paris", persona_active=False)
        )
        await session.commit()
        session.add(TelegramUpdate(update_id=update_id, payload={}))
        await session.commit()
        row = await _write(session, txt="пользователь живёт в Лилле", pinned=True)

    bot = Bot(token="123456:TESTTOKEN", session=FakeSession())
    provider = FakeLLMProvider(text="Хорошо.")
    await turn.run(
        sessionmaker,
        bot,
        Settings(),
        provider,
        chat_id=4242,
        update_id=update_id,
        user_text="я сегодня думал про Лилль",
    )

    blob = "\n".join(m.content for m in provider.received_messages[0])
    assert "пользователь живёт в Лилле" not in blob
    async with sessionmaker() as session:
        row = await session.get(Memory, row.id)
    assert row.use_count == 0
