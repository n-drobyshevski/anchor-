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

import datetime

from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core import turn
from app.core.clock import Clock
from app.core.outbound_send import build_outbound_messages, hidden_flag
from app.core.prompt import build_messages, build_neutral_messages
from app.db.models import Base, Message, Scene, UserState
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
    )
    session.add(state)
    await session.commit()

    scene = Scene(started_at=clock.now_utc())
    session.add(scene)
    await session.commit()
    await session.refresh(scene)

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
        # P4 (plan section 11): a case's `setup.planner` is the exact
        # rendered-lines list app/planner/snapshot.py's render_lines()
        # would hand build_messages() in production -- see
        # app/core/prompt.py's build_now_block docstring for why the
        # section is simply omitted when this stays None, same as
        # `techniques` and `retrieved` above.
        planner=setup.get("planner"),
        focus_on=state.focus_on,
        due_action=state.due_action,
        due_set_at=state.due_set_at,
        streak=state.streak,
        last_checkin_at=state.last_checkin_at,
    )


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
