"""Turning a case into the real prompt (phase-3 plan section 9).

Section 9 is explicit that the harness "builds the real prompt through
the production `prompt.py`". That is the whole value of it: a harness
that assembled its own approximation would keep passing while the
thing that actually ships regressed.

So every case goes through the same function the bot uses:

    chat, checkin  -> prompt.build_messages()
    neutral        -> prompt.build_neutral_messages()
    outbound       -> outbound_send.build_outbound_messages()

which is also why this needs a database. `build_messages` reads the
transcript out of `message`, so the only honest way to give a case a
conversation history is to put one in a table. A throwaway database is
created per run, migrated with the project's own Alembic revisions,
truncated between cases and dropped at the end -- the same approach
tests/conftest.py takes, for the same reason.
"""

from __future__ import annotations

import dataclasses
import datetime
import random

from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core import turn
from app.core import clock as clock_module
from app.core import persona_context as persona_context_module
from app.core import voice as voice_module
from app.core.clock import Clock
from app.core.outbound_send import build_outbound_messages, hidden_flag
from app.core.prompt import build_messages, build_neutral_messages
from app.db.models import Base, Checkin, Message, NotebookEntry, Scene, UserState
from app.llm.provider import LLMMessage
from eval.cases import CHECKIN, NEUTRAL, OUTBOUND, Case

# Flags a case may ask for by name, resolved to the production
# constants so an eval can never drift from what the bot sends.
FLAGS = {
    "yellow": turn.YELLOW_FLAG,
    "checkin": turn.CHECKIN_FLAG,
}


async def reset(session: AsyncSession) -> None:
    """Empty every table between cases, so none leaks into the next."""
    tables = ", ".join(table.name for table in reversed(Base.metadata.sorted_tables))
    await session.execute(sql_text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
    await session.commit()


def _ago(clock: Clock, hours: float | None) -> datetime.datetime | None:
    if hours is None:
        return None
    return clock.now_utc() - datetime.timedelta(hours=hours)


async def seed(session: AsyncSession, case: Case, clock: Clock) -> UserState:
    """Put the case's world into the database. Returns the user_state row."""
    setup = case.setup

    state = UserState(
        id=1,
        chat_id=4242,
        timezone=setup.get("timezone", "Europe/Paris"),
        persona_active=setup.get("persona_active", True),
        intensity=setup.get("intensity", 3),
        focus_on=setup.get("focus_on", False),
        focus_since=_ago(clock, setup.get("focus_since_hours_ago")),
        due_action=setup.get("due_action"),
        due_set_at=_ago(clock, setup.get("due_set_at_hours_ago")),
        streak=setup.get("streak", 0),
        last_checkin_at=_ago(clock, setup.get("last_checkin_at_hours_ago")),
        last_user_msg_at=_ago(clock, setup.get("last_user_msg_at_hours_ago")),
        # 5a: mood inputs (phase-5 plan section 4). welfare_at forces
        # rule 1 (ровный); checkins below feed rules 2/3 via the real
        # load_mood_facts() query, exactly as production computes them
        # -- no separate "mood" setup key exists, by design, so a case
        # cannot claim a mood the seeded facts would not actually
        # produce.
        welfare_at=_ago(clock, setup.get("welfare_at_hours_ago")),
    )
    session.add(state)
    await session.commit()

    scene = Scene(started_at=clock.now_utc())
    session.add(scene)
    await session.commit()
    await session.refresh(scene)

    # 5a: `checkins = [{days_ago, due_result}, ...]`, newest first or
    # not -- load_mood_facts() orders by local_date itself, so the
    # list's order in the TOML does not matter.
    timezone = setup.get("timezone", "Europe/Paris")
    for entry in setup.get("checkins", []):
        local_date = clock_module.local_date(clock, timezone) - datetime.timedelta(
            days=entry["days_ago"]
        )
        session.add(Checkin(local_date=local_date, due_result=entry.get("due_result")))
    if setup.get("checkins"):
        await session.commit()

    for line in setup.get("transcript", []):
        session.add(
            Message(
                role=line["role"],
                content=line["content"],
                ooc=line.get("ooc", False),
                kind=line.get("kind", "chat"),
                scene_id=scene.id,
            )
        )
    await session.commit()

    # 5b: `notebook = [{kind, text, source}, ...]`, inserted directly as
    # NotebookEntry rows -- deliberately bypassing app/core/notebook.py's
    # own `validate()`/`screen()`, because case 20 tests the persona
    # prompt's own backstop against an entry that should never have
    # passed the writer in the first place (a paraphrased injection).
    # Going through the real writer here would make that untestable: it
    # would simply refuse to store the seed and the case would pass for
    # the wrong reason.
    for entry in setup.get("notebook", []):
        session.add(
            NotebookEntry(
                kind=entry["kind"],
                text=entry["text"],
                source=entry.get("source", "anchor"),
            )
        )
    if setup.get("notebook"):
        await session.commit()

    await session.refresh(state)
    return state


async def build(
    session: AsyncSession, case: Case, state: UserState, settings: Settings, clock: Clock
) -> list[LLMMessage]:
    """The exact message list the bot would send for this case."""
    setup = case.setup
    kind = case.input["kind"]

    if kind == NEUTRAL:
        return await build_neutral_messages(
            session, user_text=case.input["text"], update_id=None
        )

    if kind == OUTBOUND:
        return await build_outbound_messages(
            session,
            settings,
            state,
            clock=clock,
            kind=case.input["outbound_kind"],
            tick_note=case.input.get("tick_note"),
        )

    flags = [FLAGS[name] for name in case.input.get("flags", [])]
    if kind == CHECKIN and turn.CHECKIN_FLAG not in flags:
        flags.append(turn.CHECKIN_FLAG)

    persona_ctx = await _persona_context(session, case, state, settings, clock)

    return await build_messages(
        session,
        clock=clock,
        timezone=state.timezone,
        intensity=state.intensity,
        user_text=case.input["text"],
        update_id=None,
        transcript_turns=settings.TRANSCRIPT_TURNS,
        flags=flags or None,
        pinned=setup.get("memories"),
        summaries=setup.get("summaries"),
        retrieved=setup.get("retrieved"),
        # 4d, phase-4 plan section 10: adopted `technique` memories, set
        # only by cases 14-16 -- every other case's `setup` has no
        # `techniques` key, so this stays None and their prompts are
        # byte-for-byte unchanged.
        techniques=setup.get("techniques"),
        focus_on=state.focus_on,
        due_action=state.due_action,
        due_set_at=state.due_set_at,
        streak=state.streak,
        last_checkin_at=state.last_checkin_at,
        voice_lines=persona_ctx.voice_lines,
        mood=persona_ctx.mood,
        nickname_directive=persona_ctx.nickname_directive,
        notebook=persona_ctx.notebook,
    )


async def _persona_context(
    session: AsyncSession, case: Case, state: UserState, settings: Settings, clock: Clock
):
    """Mood, voice anchors and the nickname directive for a chat/check-in case.

    Goes through the real `persona_context.gather()` -- the same
    function app/core/turn.py calls -- so mood is computed from
    whatever `seed()` put in `checkin`/`user_state`, never asserted by
    the case file directly, and voice anchors come from the real
    `voice.md`.

    `setup.nickname` is the one deliberate override: `"none"` forces
    "Без обращения в этом ответе." (case 24), a literal name forces
    that address, and leaving the key out draws a nickname the normal
    way but from an `rng` seeded by the case id -- deterministic across
    runs of the same case, without needing a `nickname` key on every
    other case just to pin its prompt down.
    """
    setup = case.setup
    scene_row = await session.execute(select(Scene.id).order_by(Scene.id.desc()).limit(1))
    scene_id = scene_row.scalar_one_or_none()

    rng = random.Random(case.id)
    persona_ctx = await persona_context_module.gather(
        session,
        settings,
        state,
        clock,
        scene_id=scene_id,
        exclude_update_id=None,
        rng=rng,
    )

    nickname = setup.get("nickname")
    if nickname == "none":
        return dataclasses.replace(
            persona_ctx, nickname=None, nickname_directive=voice_module.directive(None)
        )
    if nickname:
        return dataclasses.replace(
            persona_ctx, nickname=nickname, nickname_directive=voice_module.directive(nickname)
        )
    return persona_ctx


def situation(case: Case) -> str:
    """What the judge is told the bot was reacting to."""
    kind = case.input["kind"]
    if kind == OUTBOUND:
        return (
            f"Бот пишет первым, без запроса пользователя "
            f"({case.input['outbound_kind']}). "
            f"Скрытая инструкция: {hidden_flag(case.input['outbound_kind'], case.input.get('tick_note'))}"
        )
    return f"Сообщение пользователя: {case.input['text']}"
