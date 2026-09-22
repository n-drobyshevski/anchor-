"""Sending a proactive message, idempotently (phase-3 plan section 7).

The `send_outbound` job body. Everything here exists to make one
promise true: **at most one delivered message per (kind, local_date,
bucket), across restarts, duplicate jobs and a crash at any point.**

The crash that actually matters is between "the model has been paid
for" and "Telegram has the text". Generation is the expensive,
non-idempotent step; sending is the cheap, repeatable one. So they are
separated by a commit:

    insert message (sent_at NULL) + ledger   <- commit
    send to Telegram
    set sent_at, status='sent', counters     <- commit

A crash in the middle leaves a stored message with `sent_at` NULL. The
re-run finds it at step 2 and **resends the stored text** rather than
generating again -- the user gets the message they were owed, and the
model is paid once. That is the opposite trade to the planning path,
and the right one: a duplicated send is a nuisance, a duplicated
generation is money.

**The gate runs again here, and this run is the authoritative one**
(plan section 5). Minutes pass between planning and sending, because
of the jitter. In those minutes the user can check in, type a pause
word, or set `/quiet` -- and the evening nag they just made redundant
must not arrive anyway.

**A failed generation sends nothing.** `status='failed'` and silence.
There is deliberately no canned fallback: a message the user did not
ask for has to earn its place, and boilerplate does not.

**No welfare classifier and no extractor** (plan section 7 step 5).
Both exist to react to something the *user* said, and there is no user
input here. Running the extractor on Anchor's own words would let it
propose facts about the user from a message the user never sent.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core import clock as clock_module
from app.core.clock import Clock
from app.core.outbound_gate import (
    EVENING_NAG,
    MORNING,
    SILENCE,
    TICK,
    config_from_settings,
    gate,
)
from app.db.models import Message, Outbound, SpendLedger
from app.llm.provider import LLMProvider

logger = logging.getLogger(__name__)

SEND_OUTBOUND = "send_outbound"
# NOTE: app/core/scheduler.py imports this constant, which makes
# scheduler -> outbound_send a one-way edge today only because nothing
# here needs the scheduler. If a later milestone ever wants
# run_send_outbound to reschedule itself -- a retry with backoff,
# calling plan() or pick_send_time() -- that edge becomes a cycle, and
# it is the same cycle 3d hit. TICK_DECIDE's placement in scheduler.py
# is the precedent for the fix: the constant moves to the enqueuer and
# the deviation gets a comment.

# Its own ledger category (plan section 7 step 6), so /state can show
# what speaking first costs, separately from replying.
OUTBOUND_CATEGORY = "outbound"
OUTBOUND_KIND = "outbound"


def outbound_dedup_key(outbound_id: int) -> str:
    """The job's dedup key (plan section 6). One job per row, ever."""
    return f"outbound:{outbound_id}"


# --- the hidden flags (plan section 7), verbatim ------------------------

KIND_FLAGS: dict[str, str] = {
    MORNING: (
        "Сейчас утро. Коротко назови главное действие на сегодня; "
        "если его нет — предложи выбрать одно. 2–4 предложения."
    ),
    EVENING_NAG: (
        "Вечер, чек-ина сегодня не было. Коротко напомни пройти его. "
        "Без отчитывания, 1–3 предложения."
    ),
    SILENCE: (
        "Пользователь молчит больше 48 часов при включённом фокусе. "
        "Короткий спокойный вопрос, как дела. Без давления, 1–2 предложения."
    ),
    TICK: (
        "Ты пишешь первым. Повод: «{note}». Коротко, 1–3 предложения, "
        "одно действие или вопрос."
    ),
}

# Added to every kind.
COMMON_FLAG = (
    "Это сообщение по твоей инициативе — не упрекай за молчание "
    "и не повышай интенсивность."
)


def hidden_flag(kind: str, tick_note: str | None = None) -> str:
    """The instruction that stands in for the user's message.

    It is passed as the trailing *user* turn rather than as a `[флаги]`
    line in the "now" block, for one practical reason: there is no user
    message here, and the chat template this bot runs against expects a
    conversation that ends with one. The flag is never stored -- only
    Anchor's reply is -- so it cannot leak into a later transcript.
    """
    template = KIND_FLAGS[kind]
    body = template.format(note=tick_note or "") if kind == TICK else template
    return f"{body}\n{COMMON_FLAG}"


async def build_outbound_messages(
    session, settings, state, *, clock, kind, tick_note=None, persona_context=None
):
    """The exact message list a proactive send is generated from.

    Factored out of run_send_outbound in 3e so `eval/` can build the
    real prompt rather than a lookalike. Plan section 9 is explicit
    that the harness "builds the real prompt through the production
    `prompt.py`" -- an eval that assembled its own approximation would
    pass happily while the thing that ships regressed.

    5a: an outbound message is a persona reply, so it gets the same
    mood/voice/nickname treatment a chat turn does, via app/core/
    persona_context.py. There is no update_id to exclude for the mood
    facts' "last user message" query (an outbound send has no current
    user message at all), and the voice seed falls back to the local
    calendar date, since no caller threads a scene id through here.

    `persona_context` lets run_send_outbound gather once and hand the
    *same* PersonaContext to both this function and the nickname write
    that follows a real delivery -- gathering twice would draw the
    nickname coin flip twice and could remember a different nickname
    than the one the sent prompt actually carried. Left `None` (the
    default), this function gathers its own, read-only context, which
    is what eval/scenario.py relies on: it calls this function directly
    with no delivery afterwards, so nothing here may have a side
    effect. The one write, remember_nickname(), lives in
    run_send_outbound's own step 7, after a real send.
    """
    from app.core.memory import retrieve_techniques
    from app.core import persona_context as persona_context_module
    from app.core.prompt import build_messages
    from app.core.scene import recent_summaries
    from app.core.turn import NICKNAME_RNG

    # 4d: adopted techniques are for in-character generation, which a
    # proactive message is (phase-4 plan section 10: "chat turns and
    # outbound generation, **not** the extractor or classifier").
    #
    # Matched against the hidden flag rather than against a user
    # message, because there is no user message here -- so in practice
    # this almost always falls through to least-recently-used, which is
    # the right behaviour: an unprompted message is exactly the place to
    # try a technique the user has not seen used in a while.
    techniques = await retrieve_techniques(
        session, hidden_flag(kind, tick_note), settings.RESEARCH_TECHNIQUES_IN_PROMPT
    )

    persona_ctx = persona_context
    if persona_ctx is None:
        persona_ctx = await persona_context_module.gather(
            session,
            settings,
            state,
            clock,
            scene_id=None,
            exclude_update_id=None,
            rng=NICKNAME_RNG,
        )

    return await build_messages(
        session,
        clock=clock,
        timezone=state.timezone,
        intensity=state.intensity,
        user_text=hidden_flag(kind, tick_note),
        update_id=None,
        transcript_turns=settings.TRANSCRIPT_TURNS,
        techniques=[row.text for row in techniques],
        summaries=await recent_summaries(session),
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


# --- the job body -------------------------------------------------------


async def _stored_message(session: AsyncSession, outbound_id: int) -> Message | None:
    result = await session.execute(
        select(Message).where(Message.outbound_id == outbound_id)
    )
    return result.scalar_one_or_none()


async def _finish(
    session: AsyncSession,
    row: Outbound,
    message: Message,
    clock: Clock,
) -> None:
    """Mark delivered and move the counters (plan section 7 step 7)."""
    from app.core.outbound import SENT, record_outbound_sent

    now = clock.now_utc()
    message.sent_at = now
    row.status = SENT
    row.sent_at = now
    row.message_id = message.id
    await session.commit()
    await record_outbound_sent(session, clock)


async def run_send_outbound(
    session: AsyncSession,
    settings: Settings,
    provider: LLMProvider,
    bot: Bot,
    *,
    clock: Clock,
    outbound_id: int,
) -> None:
    """The `send_outbound` job body. Steps are plan section 7's, in order."""
    # Local imports.
    #
    # `app.tg.outbound` has to stay local: app/core/ does not import
    # app/tg/ at module level, which is the layering rule app/core/
    # turn.py's own local import of app.tg.welfare follows for the same
    # reason. Nothing under core may depend on aiogram types being
    # importable to be importable itself.
    #
    # The app.core.* ones below are local out of caution rather than
    # necessity -- nothing in outbound, prompt, scene, spend, state or
    # turn imports this module, so hoisting them introduces no cycle.
    # That cleanup is deliberately not folded into a milestone that is
    # about behaviour.
    from app.core.outbound import (
        FAILED,
        PLANNED,
        SENT,
        SKIPPED,
        load_gate_inputs,
    )
    from app.core import persona_context as persona_context_module
    from app.core.spend import priced
    from app.core.state import get_state
    from app.core.scene import ensure_open_scene
    from app.core.turn import NICKNAME_RNG, _complete_with_retries
    from app.core.voice import remember_nickname
    from app.tg.outbound import send_outbound_message

    # 1. Still live?
    row = await session.get(Outbound, outbound_id)
    if row is None:
        logger.info("outbound row gone", extra={"outbound_id": outbound_id})
        return
    if row.status != PLANNED:
        # cancelled by a pause/welfare/quiet/delete, or already done.
        logger.info(
            "outbound not planned any more",
            extra={"outbound_id": outbound_id, "reason": row.status},
        )
        return

    state = await get_state(session)

    # 2. Did a previous run already generate this?
    existing = await _stored_message(session, outbound_id)
    if existing is not None:
        if existing.sent_at is None:
            # Crashed between the insert and the send. Resend the stored
            # text; never regenerate -- the model was already paid.
            await send_outbound_message(
                bot, state.chat_id, existing.content, kind=row.kind
            )
            await _finish(session, row, existing, clock)
            logger.info("outbound resent", extra={"outbound_id": outbound_id})
        else:
            row.status = SENT
            row.sent_at = existing.sent_at
            row.message_id = existing.id
            await session.commit()
        return

    # 3. The authoritative gate run.
    gate_state, counts, facts = await load_gate_inputs(
        session, clock, settings, state, kind=row.kind
    )
    verdict = gate(
        row.kind, gate_state, clock.now_utc(), counts, facts, config_from_settings(settings)
    )
    if not verdict.allowed:
        row.status = SKIPPED
        row.skip_reason = verdict.reason
        await session.commit()
        logger.info(
            "outbound skipped at send time",
            extra={"outbound_id": outbound_id, "event": row.kind, "reason": verdict.reason},
        )
        return

    # 4. Scene. An outbound counts as activity for scene timing, but it
    #    is not the user speaking -- last_user_msg_at is untouched.
    scene_id = await ensure_open_scene(
        session, clock, idle_hours=settings.SCENE_IDLE_HOURS
    )

    # 5. Generate. Main model, section 7's prompt plus the hidden flag.
    # 5a: gathered once, here, and handed into build_outbound_messages
    # so the nickname write below (step 7) remembers exactly the
    # nickname the sent prompt's directive carried -- gathering a
    # second time would draw the coin flip again and could disagree
    # with what was actually said.
    persona_ctx = await persona_context_module.gather(
        session,
        settings,
        state,
        clock,
        scene_id=None,
        exclude_update_id=None,
        rng=NICKNAME_RNG,
    )
    messages = await build_outbound_messages(
        session,
        settings,
        state,
        clock=clock,
        kind=row.kind,
        tick_note=row.tick_note,
        persona_context=persona_ctx,
    )
    response = await _complete_with_retries(
        provider, messages, update_id=outbound_id
    )
    if response is None:
        row.status = FAILED
        await session.commit()
        logger.warning(
            "outbound generation failed", extra={"outbound_id": outbound_id}
        )
        return

    # Re-read: the row may have been cancelled while the model was
    # thinking. Cheap, and it closes the one window the send-time gate
    # cannot cover.
    await session.refresh(row)
    if row.status != PLANNED:
        logger.info(
            "outbound cancelled during generation",
            extra={"outbound_id": outbound_id, "reason": row.status},
        )
        return

    text = response.text.strip()
    if not text:
        row.status = FAILED
        await session.commit()
        logger.warning("outbound generated nothing", extra={"outbound_id": outbound_id})
        return

    # 6. Store the message and the ledger row together.
    cost = priced(response.usage, settings, model=response.model)
    usd_cost = cost.usd
    message = await _insert_outbound_message(
        session,
        outbound_id=outbound_id,
        content=text,
        scene_id=scene_id,
        local_date=clock_module.local_date(clock, state.timezone),
        model=response.model,
        usage=response.usage,
        usd_cost=usd_cost,
        cost_source=cost.source,
    )
    if message is None:
        # A concurrent run inserted it. Let that run deliver it.
        logger.info("outbound already stored", extra={"outbound_id": outbound_id})
        return

    # 7. Send, then mark delivered and move the counters.
    await send_outbound_message(bot, state.chat_id, text, kind=row.kind)
    # 5a: only after the send above actually happened -- never on a
    # skipped, cancelled or failed row, all of which returned earlier.
    if persona_ctx.nickname is not None:
        await remember_nickname(session, persona_ctx.nickname)
    await _finish(session, row, message, clock)
    logger.info(
        "outbound sent",
        extra={
            "outbound_id": outbound_id,
            "event": row.kind,
            "tokens_in": response.usage.input_tokens,
            "tokens_out": response.usage.output_tokens,
            "usd_cost": str(usd_cost),
        },
    )


async def _insert_outbound_message(
    session: AsyncSession,
    *,
    outbound_id: int,
    content: str,
    scene_id: int | None,
    local_date,
    model: str | None,
    usage,
    usd_cost,
    cost_source: str | None = None,
) -> Message | None:
    """Insert the assistant row and its ledger row in one transaction.

    Keyed on `outbound_id`, which is UNIQUE -- the same DB-enforced
    idempotency app/core/turn.py gets from `reply_to_update`, so a
    replayed job cannot produce a second row or a second ledger entry.
    Returns None when the insert was a no-op.

    `ooc=False` and `kind='outbound'`: this is Anchor speaking in
    character, so it belongs in the persona transcript (plan section 7).
    """
    from app.core.scene import bump_message_count

    result = await session.execute(
        pg_insert(Message)
        .values(
            role="assistant",
            content=content,
            ooc=False,
            kind=OUTBOUND_KIND,
            outbound_id=outbound_id,
            scene_id=scene_id,
            model=model,
            tokens_in=usage.input_tokens,
            tokens_cached=usage.cached_tokens,
            tokens_out=usage.output_tokens,
            usd_cost=usd_cost,
        )
        .on_conflict_do_nothing(index_elements=["outbound_id"])
        .returning(Message.id)
    )
    inserted = result.first()
    if inserted is None:
        await session.commit()
        return None

    message_id = inserted[0]
    if scene_id is not None:
        await bump_message_count(session, scene_id)
    session.add(
        SpendLedger(
            local_date=local_date,
            category=OUTBOUND_CATEGORY,
            model=model,
            tokens_in=usage.input_tokens,
            tokens_cached=usage.cached_tokens,
            tokens_out=usage.output_tokens,
            usd_cost=usd_cost,
            cost_source=cost_source,
        )
    )
    await session.commit()
    return await session.get(Message, message_id)
