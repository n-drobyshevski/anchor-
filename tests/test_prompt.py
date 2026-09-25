"""core/prompt.py tests (plan section 9 / 16).

- persona is first and byte-identical across calls
- OOC (ooc=true) rows are excluded from the transcript
- the current turn's own row is excluded (the double-user-message bug
  regression: turn.py stores the user's message *before* calling
  build_messages, and appends user_text again as the final message --
  if the transcript included the just-stored row, the model would see
  the same text twice)
- the "now" block is the second-to-last message, right before the user
  text, and reports the configured intensity
- the transcript is oldest-first and capped at transcript_turns
- the Russian weekday name does not depend on strftime("%A")/locale
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from app.core import prompt
from app.core.prompt import (
    PERSONA_PATH,
    PLAN_HEADER,
    build_messages,
    build_now_block,
    load_persona,
)
from app.db.models import Message, TelegramUpdate


async def _add_message(sessionmaker, *, role: str, content: str, update_id: int, ooc: bool = False) -> None:
    async with sessionmaker() as session:
        # message.update_id is a foreign key into telegram_update, so a
        # row must exist there first (as it always would via the real
        # webhook/enqueue path). Two commits: SQLAlchemy's unit of work
        # does not infer cross-table insert ordering without an ORM
        # relationship() between Message and TelegramUpdate.
        session.add(TelegramUpdate(update_id=update_id, payload={}))
        await session.commit()
        session.add(Message(role=role, content=content, update_id=update_id, ooc=ooc))
        await session.commit()


def test_load_persona_reads_body_and_matches_startup_hash(tmp_path):
    persona_path = tmp_path / "persona.md"
    persona_path.write_text("# Anchor\n\nТы — Anchor.\n", encoding="utf-8")

    body, sha256 = load_persona(persona_path)

    import hashlib

    assert body == "# Anchor\n\nТы — Anchor.\n"
    assert sha256 == hashlib.sha256(body.encode("utf-8")).hexdigest()


def test_load_persona_default_path_points_at_repo_persona_file():
    assert PERSONA_PATH.name == "persona.md"
    body, _ = load_persona()
    assert body == PERSONA_PATH.read_text(encoding="utf-8")


async def test_persona_is_first_message_and_byte_identical_across_calls(sessionmaker, tmp_path, clock):
    persona_path = tmp_path / "persona.md"
    persona_body = "# Anchor\n\nТы — Anchor. Голос: коротко.\n"
    persona_path.write_text(persona_body, encoding="utf-8")

    async with sessionmaker() as session:
        first = await build_messages(
            session,
            clock=clock,
            timezone="Europe/Paris",
            intensity=3,
            user_text="привет",
            update_id=1,
            transcript_turns=30,
            persona_path=persona_path,
        )
        second = await build_messages(
            session,
            clock=clock,
            timezone="Europe/Paris",
            intensity=3,
            user_text="ещё раз",
            update_id=2,
            transcript_turns=30,
            persona_path=persona_path,
        )

    assert first[0].role == "system"
    assert first[0].content == persona_body
    assert second[0].content == first[0].content  # byte-identical across calls


async def test_ooc_messages_excluded_from_transcript(sessionmaker, tmp_path, clock):
    persona_path = tmp_path / "persona.md"
    persona_path.write_text("# Anchor\n", encoding="utf-8")

    await _add_message(sessionmaker, role="user", content="в роли", update_id=100)
    await _add_message(sessionmaker, role="assistant", content="OOC ответ", update_id=101, ooc=True)
    await _add_message(sessionmaker, role="user", content="тоже ooc", update_id=102, ooc=True)

    async with sessionmaker() as session:
        messages = await build_messages(
            session,
            clock=clock,
            timezone="Europe/Paris",
            intensity=3,
            user_text="новое сообщение",
            update_id=999,
            transcript_turns=30,
            persona_path=persona_path,
        )

    contents = [m.content for m in messages]
    assert "в роли" in contents
    assert "OOC ответ" not in contents
    assert "тоже ooc" not in contents


async def test_current_update_excluded_from_transcript_no_double_user_message(sessionmaker, tmp_path, clock):
    """Regression for the double-user-message bug: core/turn.py stores
    the user's row for this update_id *before* calling build_messages,
    so the transcript query must exclude that row -- otherwise the
    model sees the same user text twice (once from the transcript,
    once as the final user message appended below).
    """
    persona_path = tmp_path / "persona.md"
    persona_path.write_text("# Anchor\n", encoding="utf-8")

    update_id = 42
    await _add_message(sessionmaker, role="user", content="текущее сообщение", update_id=update_id)

    async with sessionmaker() as session:
        messages = await build_messages(
            session,
            clock=clock,
            timezone="Europe/Paris",
            intensity=3,
            user_text="текущее сообщение",
            update_id=update_id,
            transcript_turns=30,
            persona_path=persona_path,
        )

    user_messages = [m for m in messages if m.role == "user"]
    assert len(user_messages) == 1
    assert user_messages[0].content == "текущее сообщение"
    assert messages[-1].role == "user"


async def test_current_update_excluded_even_when_other_rows_have_null_update_id(sessionmaker, tmp_path, clock):
    """update_id.is_distinct_from(update_id), not !=: plain != would
    also (incorrectly) exclude historical rows whose update_id is NULL,
    since NULL != x is NULL/unknown in SQL, not true."""
    persona_path = tmp_path / "persona.md"
    persona_path.write_text("# Anchor\n", encoding="utf-8")

    async with sessionmaker() as session:
        session.add(Message(role="user", content="без update_id", update_id=None, ooc=False))
        await session.commit()

    async with sessionmaker() as session:
        messages = await build_messages(
            session,
            clock=clock,
            timezone="Europe/Paris",
            intensity=3,
            user_text="новое",
            update_id=7,
            transcript_turns=30,
            persona_path=persona_path,
        )

    contents = [m.content for m in messages]
    assert "без update_id" in contents


async def test_now_block_is_second_to_last_before_user_text(sessionmaker, tmp_path, clock):
    persona_path = tmp_path / "persona.md"
    persona_path.write_text("# Anchor\n", encoding="utf-8")

    async with sessionmaker() as session:
        messages = await build_messages(
            session,
            clock=clock,
            timezone="Europe/Paris",
            intensity=4,
            user_text="финальный текст",
            update_id=5,
            transcript_turns=30,
            persona_path=persona_path,
        )

    assert messages[-1].role == "user"
    assert messages[-1].content == "финальный текст"

    now_block = messages[-2]
    assert now_block.role == "system"
    assert "## Сейчас" in now_block.content
    assert "Интенсивность: 4/5" in now_block.content


async def test_transcript_is_oldest_first_and_capped_at_transcript_turns(sessionmaker, tmp_path, clock):
    persona_path = tmp_path / "persona.md"
    persona_path.write_text("# Anchor\n", encoding="utf-8")

    for i in range(5):
        await _add_message(sessionmaker, role="user", content=f"сообщение {i}", update_id=200 + i)

    async with sessionmaker() as session:
        messages = await build_messages(
            session,
            clock=clock,
            timezone="Europe/Paris",
            intensity=3,
            user_text="новое сообщение",
            update_id=999,
            transcript_turns=3,
            persona_path=persona_path,
        )

    transcript = messages[1:-2]  # exclude persona, now-block, user text
    assert [m.content for m in transcript] == ["сообщение 2", "сообщение 3", "сообщение 4"]


def test_build_now_block_includes_flags(clock):
    block = build_now_block(
        clock=clock,
        timezone="Europe/Paris",
        intensity=2,
        flags=["Пользователь сказал «жёлтый»: снизь интенсивность прямо сейчас."],
    )
    assert "Интенсивность: 2/5" in block
    assert "жёлтый" in block


# --- 5a: section order, omission, and the neutral/welfare exclusion ---------


async def test_full_order_with_every_section_populated(sessionmaker, tmp_path, clock):
    """Plan section 10's order, checked by header index: persona ->
    Поправки -> Голос -> pinned -> Договорённости -> Твои заметки ->
    Прошлые сессии -> transcript -> Сейчас -> user text."""
    persona_path = tmp_path / "persona.md"
    persona_path.write_text("# Anchor\n", encoding="utf-8")

    async with sessionmaker() as session:
        messages = await build_messages(
            session,
            clock=clock,
            timezone="Europe/Paris",
            intensity=3,
            user_text="привет",
            update_id=1,
            transcript_turns=30,
            persona_path=persona_path,
            amendments=["меньше вопросов по утрам"],
            voice_lines=["Коротко. По делу."],
            pinned=["живёт в Лилле"],
            orders=["не есть после десяти"],
            notebook={"intentions": ["держать темп"], "observations": ["пишет вечером"]},
            summaries=["вчера говорили про отчёт"],
            mood="ровный",
            nickname_directive="Обращение в этом ответе: напарник",
            retrieved=["живёт в Лилле"],
            techniques=["пятиминутка"],
            callback="ходил на скалодром две недели назад",
            flags=["Пользователь сказал «жёлтый»"],
        )

    contents = [m.content for m in messages]
    assert contents[0] == "# Anchor\n"
    idx = {
        "amendments": next(i for i, c in enumerate(contents) if "## Поправки" in c),
        "voice": next(i for i, c in enumerate(contents) if "## Голос" in c),
        "pinned": next(i for i, c in enumerate(contents) if prompt.PINNED_HEADER in c),
        "orders": next(i for i, c in enumerate(contents) if "## Договорённости" in c),
        "notebook": next(i for i, c in enumerate(contents) if "## Твои заметки" in c),
        "sessions": next(i for i, c in enumerate(contents) if prompt.SESSIONS_HEADER in c),
        "now": next(i for i, c in enumerate(contents) if "## Сейчас" in c),
    }
    assert (
        0
        < idx["amendments"]
        < idx["voice"]
        < idx["pinned"]
        < idx["orders"]
        < idx["notebook"]
        < idx["sessions"]
        < idx["now"]
    )
    assert messages[-1].role == "user"
    assert idx["now"] == len(messages) - 2

    now_block = messages[idx["now"]].content
    # "## Сейчас" internal order: mood before due action/last check-in,
    # nickname directive after both.
    assert now_block.index("Настроение:") < now_block.index("Главное действие:")
    assert now_block.index("Главное действие:") < now_block.index("Последний чек-ин:")
    assert now_block.index("Последний чек-ин:") < now_block.index("Обращение в этом ответе")
    # 5e: "## Можно вспомнить" sits after the techniques header and
    # before the flags -- plan section 10's own ordering ("## Приёмы"
    # then "## Можно вспомнить" then flags).
    assert now_block.index(prompt.TECHNIQUES_HEADER) < now_block.index(prompt.CALLBACK_HEADER)
    assert now_block.index(prompt.CALLBACK_HEADER) < now_block.index("Пользователь сказал «жёлтый»")


async def test_notebook_omits_empty_kinds_but_keeps_populated_ones(sessionmaker, tmp_path, clock):
    """Plan section 6: a kind with no active entries is left out entirely,
    not rendered as an empty "Незакрытое:" line."""
    persona_path = tmp_path / "persona.md"
    persona_path.write_text("# Anchor\n", encoding="utf-8")

    async with sessionmaker() as session:
        messages = await build_messages(
            session,
            clock=clock,
            timezone="Europe/Paris",
            intensity=3,
            user_text="привет",
            update_id=1,
            transcript_turns=30,
            persona_path=persona_path,
            notebook={"intentions": ["держать темп"], "observations": [], "threads": []},
        )

    contents = "\n".join(m.content for m in messages)
    assert "## Твои заметки" in contents
    assert "Намерения: держать темп" in contents
    assert "Наблюдения:" not in contents
    assert "Незакрытое:" not in contents


async def test_empty_new_sections_are_omitted(sessionmaker, tmp_path, clock):
    persona_path = tmp_path / "persona.md"
    persona_path.write_text("# Anchor\n", encoding="utf-8")

    async with sessionmaker() as session:
        messages = await build_messages(
            session,
            clock=clock,
            timezone="Europe/Paris",
            intensity=3,
            user_text="привет",
            update_id=1,
            transcript_turns=30,
            persona_path=persona_path,
        )

    contents = "\n".join(m.content for m in messages)
    for marker in (
        "## Поправки",
        "## Голос",
        "## Договорённости",
        "## Твои заметки",
        "Настроение:",
        "Обращение в этом ответе",
        "Без обращения в этом ответе",
        "## Можно вспомнить",
    ):
        assert marker not in contents


async def test_neutral_messages_carry_none_of_the_persona_mode_sections(sessionmaker):
    from app.core.prompt import build_neutral_messages

    async with sessionmaker() as session:
        messages = await build_neutral_messages(session, user_text="привет", update_id=None)

    contents = "\n".join(m.content for m in messages)
    for marker in (
        "Поправки",
        "## Голос",
        "Договорённости",
        "Твои заметки",
        "Настроение",
        "Обращение в этом ответе",
        "Без обращения",
        "Можно вспомнить",
    ):
        assert marker not in contents


async def test_welfare_prompts_carry_none_of_the_persona_mode_sections():
    """app/core/welfare.py never calls build_messages() at all, so this
    is a property of its own fixed prompts -- checked directly rather
    than through a turn, since welfare.py owns its own prompt text."""
    from app.core import welfare

    all_prompt_text = welfare.CLASSIFIER_PROMPT + welfare.WELFARE_PROMPT + welfare.FALLBACK_REPLY
    for marker in (
        "Поправки",
        "## Голос",
        "Договорённости",
        "Твои заметки",
        "Настроение",
        "Обращение в этом ответе",
        "Без обращения",
        "Можно вспомнить",
    ):
        assert marker not in all_prompt_text


def test_build_now_block_weekday_does_not_rely_on_locale(clock):
    """A hardcoded Russian weekday name, independent of strftime("%A")
    and any ru_RU locale being installed."""
    block = build_now_block(clock=clock, timezone="Europe/Paris", intensity=3)
    weekday_names = (
        "понедельник",
        "вторник",
        "среда",
        "четверг",
        "пятница",
        "суббота",
        "воскресенье",
    )
    assert any(name in block for name in weekday_names)

    now_local = datetime.datetime.now(ZoneInfo("Europe/Paris"))
    assert weekday_names[now_local.weekday()] in block


# --- P2: the planner section --------------------------------------------


def test_build_now_block_planner_none_is_byte_identical(clock):
    """planner=None (every pre-P2 call site) must change nothing at all."""
    without_param = build_now_block(clock=clock, timezone="Europe/Paris", intensity=3)
    with_none = build_now_block(clock=clock, timezone="Europe/Paris", intensity=3, planner=None)
    assert without_param == with_none
    assert PLAN_HEADER not in without_param


def test_build_now_block_planner_empty_list_omits_the_section(clock):
    block = build_now_block(clock=clock, timezone="Europe/Paris", intensity=3, planner=[])
    assert PLAN_HEADER not in block


def test_build_now_block_planner_lines_appear_after_due_action(clock):
    block = build_now_block(
        clock=clock,
        timezone="Europe/Paris",
        intensity=3,
        due_action="сдать отчёт",
        planner=["10:00 — «Встреча»"],
    )
    assert PLAN_HEADER in block
    assert "10:00 — «Встреча»" in block
    due_idx = block.index("Главное действие")
    plan_idx = block.index(PLAN_HEADER)
    retrieved_idx = block.find("Может быть важно")
    assert due_idx < plan_idx
    # No retrieved-memories section in this call, so there is nothing to
    # compare against; when there is one (below), plan must still come first.
    assert retrieved_idx == -1


def test_build_now_block_planner_precedes_retrieved_memories(clock):
    block = build_now_block(
        clock=clock,
        timezone="Europe/Paris",
        intensity=3,
        planner=["10:00 — «Встреча»"],
        retrieved=["любит утренний кофе"],
    )
    assert block.index(PLAN_HEADER) < block.index("Может быть важно")


# --- Phase 5 (spec 2026-09-25): "## Долг" and PERSONA_FILE -----------------


def test_build_now_block_debts_none_is_byte_identical(clock):
    without_param = build_now_block(clock=clock, timezone="Europe/Paris", intensity=3)
    assert build_now_block(clock=clock, timezone="Europe/Paris", intensity=3, debts=None) == without_param
    assert build_now_block(clock=clock, timezone="Europe/Paris", intensity=3, debts=[]) == without_param


def test_build_now_block_debts_follow_the_flat_lines_and_precede_the_planner(clock):
    block = build_now_block(
        clock=clock,
        timezone="Europe/Paris",
        intensity=3,
        nickname_directive="Обращение в этом ответе: капитан",
        debts=["«прислать отчёт» (с 20.09, просрочено)"],
        planner=["09:00 созвон"],
        flags=["ФЛАГ"],
    )
    lines = block.splitlines()
    debt_at = lines.index(prompt.DEBT_HEADER)
    assert lines[debt_at + 1] == "- «прислать отчёт» (с 20.09, просрочено)"
    assert lines.index("Обращение в этом ответе: капитан") < debt_at
    assert next(i for i, line in enumerate(lines) if line.startswith("Последний чек-ин")) < debt_at
    assert debt_at < lines.index(prompt.PLAN_HEADER)
    assert lines[-1] == "ФЛАГ"


def test_persona_file_setting_resolves_against_the_repo_root(tmp_path):
    from app.config import Settings

    assert prompt.persona_path_for(Settings()) == prompt.PERSONA_PATH
    private = tmp_path / "private.md"
    private.write_text("# Private\n", encoding="utf-8")
    # An absolute path is used as is.
    assert prompt.persona_path_for(Settings(PERSONA_FILE=str(private))) == private


def test_persona_carries_the_debt_and_short_mode_rules():
    body, _ = prompt.load_persona()
    assert "## Долг" in body
    assert "следующий приказ закрывает один из них" in body
    assert "режим: коротко" in body
    assert "Никогда не называй его вслух" in body
