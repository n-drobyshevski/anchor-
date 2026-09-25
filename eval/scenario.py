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
from app.core.prompt import build_messages, build_neutral_messages, persona_path_for
from app.db.models import (
    Base,
    Checkin,
    Memory,
    Message,
    NotebookEntry,
    Obligation,
    PersonaAmendment,
    Scene,
    StandingOrder,
    UserState,
)
from app.llm.provider import LLMMessage
from eval.cases import CHAT, CHECKIN, NEUTRAL, OUTBOUND, Case

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


async def seed(
    session: AsyncSession, case: Case, clock: Clock, *, amendments: list[str] | None = None
) -> UserState:
    """Put the case's world into the database. Returns the user_state row.

    `amendments` (5d) is `eval.trial.run_blocking_subset`'s own override
    -- when given (even as an empty list), it replaces `setup.amendments`
    entirely, which is what lets an amendment_trial exercise every
    blocking case with the candidate amendment (plus every other active
    one) actually seeded, regardless of what a case file's own `setup`
    happens to say. `None` (the default) falls back to the case's own
    `setup.amendments` list -- case 23's own way of seeding "меньше
    вопросов" for a plain eval.run.py invocation.
    """
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
    # Phase 5 (spec 2026-09-25): `attention = {state, minutes}` seeds a
    # short stretch that is still running, the way app/core/attention.py
    # would have left it.
    attention = setup.get("attention")
    if attention:
        state.attention = attention.get("state", "short")
        state.attention_until = clock.now_utc() + datetime.timedelta(
            minutes=attention.get("minutes", 30)
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

    # 5c: `orders = [{text, cadence, weekday?}, ...]`, inserted directly
    # as active StandingOrder rows -- same "bypass the writer" reasoning
    # as the notebook seed key above, since a case seeds the *world*
    # (what is already agreed), not a negotiation in progress.
    for entry in setup.get("orders", []):
        session.add(
            StandingOrder(
                text=entry["text"],
                cadence=entry.get("cadence", "daily"),
                weekday=entry.get("weekday"),
                status="active",
                source="user",
            )
        )
    if setup.get("orders"):
        await session.commit()

    # 5d: `amendments = ["текст", ...]`, inserted directly as active
    # PersonaAmendment rows -- same "bypass the writer" reasoning as the
    # notebook/orders seed keys above: a case (or a trial run) seeds the
    # *world* an amendment already being active, not the adopt/trial
    # negotiation that got it there. `persona_sha="eval"` is a
    # placeholder -- these rows never outlive the throwaway database, so
    # there is no real persona.md hash for them to be compared against.
    # Phase 5: `obligations = [{text, kind?, days_ago, due_days_ago?}, ...]`,
    # inserted directly as open Obligation rows -- the world already
    # owes these. `due_days_ago` > 0 makes the debt overdue.
    today = clock_module.local_date(clock, timezone)
    for entry in setup.get("obligations", []):
        due_days_ago = entry.get("due_days_ago")
        session.add(
            Obligation(
                text=entry["text"],
                kind=entry.get("kind", "promised"),
                source=entry.get("source", "user"),
                opened_at=clock.now_utc() - datetime.timedelta(days=entry.get("days_ago", 1)),
                due_local_date=(
                    today - datetime.timedelta(days=due_days_ago)
                    if due_days_ago is not None
                    else None
                ),
            )
        )
    if setup.get("obligations"):
        await session.commit()

    amendment_texts = amendments if amendments is not None else setup.get("amendments") or []
    for text in amendment_texts:
        session.add(PersonaAmendment(text=text, status="active", persona_sha="eval"))
    if amendment_texts:
        await session.commit()

    # 5e: `memories = [{kind, text, days_ago, last_used_days_ago?}, ...]`
    # -- real `memory` rows, unlike the plain-string `memories` key
    # above (which overrides "## Что ты знаешь (закреплено)" directly
    # and is left untouched: cases 01 and 06 already depend on that
    # shape). A dict entry here is for app/core/callbacks.py's
    # `select_callback` to actually find through its real DB query --
    # the one seed key in this file `build()` never reads back out
    # itself, since the callback text reaches the prompt through
    # `persona_context.gather()`, not through a `setup` override.
    # `days_ago` backdates `created_at` past CALLBACK_MIN_AGE_DAYS (case
    # 25 uses 20); `last_used_days_ago` omitted means never used, which
    # is what leaves a memory eligible without also passing
    # CALLBACK_UNUSED_DAYS explicitly.
    for entry in setup.get("memories", []):
        if not isinstance(entry, dict):
            continue
        last_used_days_ago = entry.get("last_used_days_ago")
        session.add(
            Memory(
                kind=entry["kind"],
                text=entry["text"],
                source=entry.get("source", "user"),
                created_at=clock.now_utc() - datetime.timedelta(days=entry["days_ago"]),
                last_used_at=(
                    clock.now_utc() - datetime.timedelta(days=last_used_days_ago)
                    if last_used_days_ago is not None
                    else None
                ),
            )
        )
    if any(isinstance(entry, dict) for entry in setup.get("memories", [])):
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
            # 5d: case 22's own note text for a weekly_review outbound
            # case -- eval.run.py never calls app.core.review.analyze_week
            # (that would cost a second, real safety-model call per run),
            # so the case file supplies the {note} substitution directly.
            review_note=case.input.get("review_note"),
        )

    flags = [FLAGS[name] for name in case.input.get("flags", [])]
    if kind == CHECKIN and turn.CHECKIN_FLAG not in flags:
        flags.append(turn.CHECKIN_FLAG)

    persona_ctx = await _persona_context(session, case, state, settings, clock, kind=kind)
    # Phase 5: the context's own flags first, as app/core/turn.py does.
    flags = [*persona_ctx.flags, *flags]

    # 5e: only the plain-string entries of `memories` still override
    # "## Что ты знаешь (закреплено)" -- a dict entry was already
    # written as a real `memory` row by seed() above, for
    # select_callback() to find, and has no business also appearing as
    # a pin.
    pinned_override = [entry for entry in setup.get("memories", []) if isinstance(entry, str)]

    return await build_messages(
        session,
        clock=clock,
        timezone=state.timezone,
        intensity=state.intensity,
        user_text=case.input["text"],
        update_id=None,
        transcript_turns=settings.TRANSCRIPT_TURNS,
        flags=flags or None,
        pinned=pinned_override or None,
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
        voice_lines=persona_ctx.voice_lines,
        mood=persona_ctx.mood,
        nickname_directive=persona_ctx.nickname_directive,
        notebook=persona_ctx.notebook,
        orders=list(persona_ctx.orders),
        orders_yesterday=persona_ctx.orders_yesterday,
        amendments=list(persona_ctx.amendments),
        callback=persona_ctx.callback,
        debts=list(persona_ctx.debts),
        persona_path=persona_path_for(settings),
    )


async def _persona_context(
    session: AsyncSession,
    case: Case,
    state: UserState,
    settings: Settings,
    clock: Clock,
    *,
    kind: str,
):
    """Mood, voice anchors, the nickname directive and (5e) the callback.

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

    `kind` decides `enable_callback` the same way app/core/turn.py's own
    `kind == CHAT_KIND` guard does: only a chat case ever asks -- never
    a check-in (turn.py never asks on a check-in's synthetic line
    either), and `user_text` is the case's own input text, exactly what
    a real chat turn would hand `select_callback`.
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
        user_text=case.input["text"],
        enable_callback=(kind == CHAT),
        # Phase 5: a "yellow" case is a soft-pause turn, as in turn.py.
        soft_pause="yellow" in case.input.get("flags", []),
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
            f"Скрытая инструкция: "
            f"{hidden_flag(case.input['outbound_kind'], case.input.get('tick_note'), note=case.input.get('review_note'))}"
        )
    return f"Сообщение пользователя: {case.input['text']}"
