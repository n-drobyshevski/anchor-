"""One idempotent chat turn (plan sections 7 and 8).

Step numbers below match the plan's list.

Retries live here, not in the SDK or app/llm/openrouter.py: this is the
only way FakeLLMProvider (tests/conftest.py) can exercise "fails twice,
then succeeds" and "fails until exhausted" without a network, and it
keeps the retry budget in one visible place instead of splitting it
between this loop and a hidden SDK default. app/llm/openrouter.py
builds its client with max_retries=0 for exactly this reason.

Crash-safety is the point of the step ordering: dying between step 5
(provider call) and step 6 (DB write) re-runs the whole turn and wastes
one LLM call but delivers no duplicate; dying between step 6 and step 7
(send) leaves a row with sent_at NULL, so the next run resends without
a second model call; dying after step 7 makes the next run's step 2 a
no-op. This is what makes "kill the process mid-turn, restart, get
exactly one reply" (plan section 18) true.

1d adds the off-switch (plan section 7): pause.match() is checked in
code, before any of the above, so a HARD word never reaches the model
and never spends a cent. run_hard_pause() and run_resume() are the
module functions behind /out, /in, and the HARD word branch of
run() -- both idempotent through the same reply_to_update mechanism as
the rest of this file, via _send_canned_reply().

2a stamps every message row with its scene (phase-2 plan sections 4/5):
run() calls scene.ensure_open_scene() once, before step 1, and both the
user row and the assistant row carry that scene_id and a `kind`. The
kind is what later milestones' exclusions key on -- canned replies are
written as kind='canned' from here on, so scene summaries and (in 2c)
the extractor never see them. scene.message_count is incremented only
when a row is actually inserted, so a queue replay cannot inflate it.

2b injects memory (plan sections 6 and 7). run() owns *when* retrieval
happens -- inside the persona branch only, so a neutral turn does zero
memory work -- and passes the results into build_messages() as plain
strings, never ids. It keeps the injected ids itself, because section 6
requires them to be marked used only **after the turn is delivered**:
mark_used() therefore runs in the same transaction as _mark_sent(),
downstream of send_reply().

That ordering is also what makes double-counting impossible. A queue
replay of an already-delivered turn returns at step 2 without ever
rebuilding the prompt, so the ids do not exist on that path and
mark_used cannot run twice for one update_id. A crash between the send
and the commit loses one increment instead; undercounting a use is
harmless, while overcounting would corrupt the last_used_at tie-break
that gives retrieval its callback variety.

2e adds the welfare check (plan section 10). The classifier runs
*beside* the main generation rather than before it, so it costs no extra
latency on the overwhelming majority of turns where it says `none`.
When it says `real`, the persona reply that came back is discarded
unsent -- its cost is still ledgered, because it was still billed -- the
persona is switched off, and a plain out-of-character reply goes out in
its place.

The check never runs on a neutral turn, a canned reply, a pause, or at
the cap, because every one of those returns before step 6. It does run
on a check-in note, which plan section 10 asks for explicitly.

2d adds the check-in note branch (plan section 9), and where it sits is
the whole design. It must run **after** pause.match -- plan section 13
puts pause words before everything, `awaiting` states included -- and
**before** _store_user_message_once, because a check-in stores a
synthetic summary line, not the user's raw note. That leaves exactly one
insertion point, marked "Step 0c" below.

It falls through rather than recursing: the branch rebinds its own
`user_text` to the synthetic line, sets kind='checkin' and adds the
hidden flag, and the same invocation carries on into step 1. Everything
downstream then happens once, through the path that already exists.

2c enqueues the post-turn extractor (plan section 8), and the *where*
matters more than the what. It is enqueued only on the success path of
an in-character turn, after the reply has been sent: never for a
neutral/OOC turn, never for a pause or cap or failure reply, never for
a command, and (in 2e) never for a welfare turn -- because every one of
those returns before this point. That is why the enqueue is a single
line at the very bottom of run() rather than a condition somewhere in
the middle: the control flow already encodes the rule.

5a (phase-5 plan section 5) gathers mood, this scene's voice anchors
and the nickname directive once per turn via app/core/persona_context.py
and threads the same `PersonaContext` into both build_messages() calls
below (the original attempt and the boundary retry), so a retry never
re-rolls any of the three. `remember_nickname()` runs only after the
persona reply is actually delivered -- never on a welfare turn (which
returns from run_welfare_turn long before this point) and never in
neutral mode (turn_persona_context stays None there).

5e (phase-5 plan sections 11a, 12 and 14) adds the callback the same
way: `gather()` is passed `enable_callback=(kind == CHAT_KIND)`, so a
check-in's synthetic turn never asks (its `kind` is CHECKIN_KIND by the
time it reaches step 6), and app/core/callbacks.py's `mark_delivered()`
runs right alongside `remember_nickname()` -- only after the reply is on
its way out, guarded by the same `kind == CHAT_KIND` check, which is
also what stops a check-in turn (persona-active, so
`turn_persona_context` is not None there either) from marking a scene's
callback slot used on a turn that never asked for one. Any callback
memory the selection picked is dropped from `retrieved_rows` before
`injected_memory_ids` is built, so it is never double-injected under
both "## Может быть важно" and "## Можно вспомнить", and its
`last_used_at` is bumped exactly once, by `mark_delivered`, not by
`memory.mark_used()`.

Milestone 1f once threaded an opt-in `web_search` flag from run() down
to the single provider call, behind a now-removed /search command.
Milestone 4a (2026-09) removed it: it reached the tree without a plan
behind it (see README's former "Hardening H3" section), and a real
research path -- explicit, budgeted, logged -- belongs in
app/research/ instead of as a raw plugin on a persona turn.
"""

from __future__ import annotations

import asyncio
import datetime
import decimal
import logging
import random
import time

from aiogram import Bot
from sqlalchemy import select, update as sql_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core import pause
from app.core import clock as clock_module
from app.core import boundaries, safety_events, welfare_terms
from app.core import attention, callbacks
from app.core.clock import Clock
from app.core.outbound import cancel_outbound, record_welfare
from app.core import checkin, memory, orders, welfare
from app.core import persona_context as persona_context_module
from app.core.prompt import build_messages, build_neutral_messages, persona_path_for
from app.core.scene import bump_message_count, ensure_open_scene, recent_summaries
from app.planner import actions as planner_actions
from app.planner import auth as planner_auth
from app.planner import intent as planner_intent
from app.planner import snapshot as planner_snapshot
from app.planner.jobs import PLANNER_SYNC
from app.planner.parse import PLANNER_CATEGORY as PLANNER_LEDGER_CATEGORY
from app.core.scheduler import planner_sync_dedup_key
from app.core.spend import Priced, check_cap, priced
from app.core.state import Source, get_state, update_state
from app.core.voice import remember_nickname
from app.db.jobs import enqueue_job
from app.db.models import Message, SpendLedger
from app.llm.provider import LLMError, LLMProvider, LLMRetryableError
from app.tg import planner as planner_ui
from app.tg.send import send_reply, start_typing, stop_typing

# 5a: the nickname coin flip's source of randomness. Module-level so a
# test can monkeypatch it to a seeded `random.Random(n)` for a
# deterministic outcome, the same injection style app/core/voice.py's
# own `voice_anchors()` uses for its *deterministic* draw (seeded by
# scene_id) -- this one is a genuine coin flip and has nothing to seed
# it by, so the object itself is the injection point instead.
NICKNAME_RNG = random.Random()

logger = logging.getLogger(__name__)

# A single-user bot has exactly one conversation. OpenRouter has no
# prompt-cache-key field (unlike xAI's Responses API), so this string
# is retained only as the seam's conversation identifier -- it is part
# of the LLMProvider.complete() call shape but currently unused by
# OpenRouterProvider.
CONVERSATION_ID = "anchor-main"

MAX_RETRIES = 2
DEFAULT_BACKOFF_SECONDS = 1.0

CAP_REPLY_TEXT = "На сегодня всё, лимит. Продолжим завтра."
FAILURE_REPLY_TEXT = "Связь с моделью упала, попробуй чуть позже."

# Plan section 7, verbatim.
PAUSE_REPLY_TEXT = "Ок, выхожу из роли. Всё на паузе, никаких сообщений от меня. Вернуться — /in."
RESUME_REPLY_TEXT = "Возвращаюсь."
YELLOW_FLAG = "Пользователь сказал «жёлтый»: снизь интенсивность прямо сейчас, мягче, без давления."

CHAT_CATEGORY = "chat"
OOC_CATEGORY = "ooc"

# The job kind enqueued after a delivered in-character turn (plan
# section 8). Spelled here rather than imported from app/core/extract.py
# to keep turn.py free of any dependency on the extractor itself -- the
# turn's job is to hand off, not to know what happens next.
EXTRACT = "extract"

# P4: the now-block note for a still-pending planner_action -- see the
# comment where planner_lines is built for why this must never read as
# "done".
PLANNER_PENDING_NOTE = "[план: предложено «{title}», ждёт подтверждения]"

# message.kind (phase-2 plan section 4), distinct from the ledger's
# category above: `kind` says what sort of message this is, `category`
# says which budget line paid for it. A canned reply has kind='canned'
# and no ledger row at all.
CHAT_KIND = "chat"
CANNED_KIND = "canned"
CHECKIN_KIND = "checkin"
WELFARE_KIND = "welfare"

# Plan section 9, verbatim: the hidden flag on the turn that follows a
# completed check-in.
CHECKIN_FLAG = (
    "Пользователь только что прошёл чек-ин; отреагируй коротко и дай одно действие на завтра."
)


async def _store_user_message_once(
    session: AsyncSession,
    update_id: int | None,
    content: str,
    *,
    ooc: bool = False,
    scene_id: int | None = None,
    kind: str = CHAT_KIND,
) -> bool:
    """Insert a `message` row for this update, unless one already exists.

    Moved here from app/tg/router.py in 1c: it is now step 1 of the
    idempotent turn rather than something the router does on its own.
    Idempotency guard for queue replay (e.g. after worker.recover_stuck
    resets a crashed row back to pending): there is no unique
    constraint on message.update_id, so this is a check-then-insert.
    That is race-free only because the worker's concurrency is 1 (plan
    section 6.3) -- never call this from more than one worker.

    `ooc` (1d): True when this message must never surface in the
    persona transcript -- a safeword itself, or any message sent while
    persona_active is already False. Defaults to False so callers that
    predate 1d (there are none left, but the default is cheap safety)
    keep the old behaviour.

    Returns True iff a row was actually inserted (2a), so the caller can
    increment scene.message_count exactly once per real message rather
    than once per replay.
    """
    if update_id is not None:
        existing = await session.execute(
            select(Message.id).where(Message.update_id == update_id, Message.role == "user")
        )
        if existing.scalar_one_or_none() is not None:
            return False
    session.add(
        Message(
            role="user",
            content=content,
            update_id=update_id,
            ooc=ooc,
            scene_id=scene_id,
            kind=kind,
        )
    )
    if scene_id is not None:
        await bump_message_count(session, scene_id)
    await session.commit()
    return True


async def _get_assistant_row(session: AsyncSession, update_id: int) -> Message | None:
    result = await session.execute(select(Message).where(Message.reply_to_update == update_id))
    return result.scalar_one_or_none()


async def _mark_sent(session: AsyncSession, clock: Clock, message_id: int) -> None:
    message = await session.get(Message, message_id)
    message.sent_at = clock.now_utc()
    await session.commit()


async def _insert_assistant_row(
    session: AsyncSession,
    *,
    update_id: int,
    content: str,
    usd_cost: decimal.Decimal,
    cost_source: str | None = None,
    model: str | None = None,
    tokens_in: int | None = None,
    tokens_cached: int | None = None,
    tokens_out: int | None = None,
    local_date: datetime.date | None = None,
    category: str = CHAT_CATEGORY,
    scene_id: int | None = None,
    kind: str = CHAT_KIND,
) -> int | None:
    """Insert the assistant row and, iff it was actually inserted, the
    matching spend_ledger row -- both in this one transaction.

    DB-enforced idempotency (design decision 8): reply_to_update is
    UNIQUE, so ON CONFLICT DO NOTHING makes a repeated insert for the
    same update_id a no-op instead of racing a check against an insert.
    The ledger row is written only if an id came back from the insert,
    so a no-op assistant insert can never produce a duplicate ledger
    row. Passing local_date=None (the spend-cap path) skips the ledger
    row entirely, by design: the cap message costs nothing and was
    never sent to the model.
    """
    stmt = (
        pg_insert(Message)
        .values(
            role="assistant",
            content=content,
            ooc=(category != CHAT_CATEGORY),
            update_id=update_id,
            reply_to_update=update_id,
            scene_id=scene_id,
            kind=kind,
            model=model,
            tokens_in=tokens_in,
            tokens_cached=tokens_cached,
            tokens_out=tokens_out,
            usd_cost=usd_cost,
        )
        .on_conflict_do_nothing(index_elements=["reply_to_update"])
        .returning(Message.id)
    )
    result = await session.execute(stmt)
    row = result.first()
    message_id = row[0] if row is not None else None

    # 2a: the scene's count follows the insert, not the attempt, so an
    # ON CONFLICT DO NOTHING no-op on replay leaves message_count alone.
    if message_id is not None and scene_id is not None:
        await bump_message_count(session, scene_id)

    if message_id is not None and local_date is not None:
        session.add(
            SpendLedger(
                local_date=local_date,
                category=category,
                model=model,
                tokens_in=tokens_in,
                tokens_cached=tokens_cached,
                tokens_out=tokens_out,
                usd_cost=usd_cost,
                cost_source=cost_source,
            )
        )
    await session.commit()
    return message_id


async def _complete_with_retries(provider: LLMProvider, messages, *, update_id: int):
    """Call provider.complete, retrying retryable errors up to MAX_RETRIES times.

    Returns the LLMResponse on success, or None once retries (or a
    non-retryable error) are exhausted. Only type(exc).__name__ is
    logged, per the privacy rule -- never str(exc), which could echo
    prompt content back from an error body.
    """
    attempt = 0
    while True:
        try:
            return await provider.complete(messages, conversation_id=CONVERSATION_ID)
        except LLMRetryableError as exc:
            if attempt >= MAX_RETRIES:
                logger.warning(
                    "llm call failed after retries",
                    extra={"update_id": update_id, "event": type(exc).__name__, "attempts": attempt + 1},
                )
                return None
            delay = exc.retry_after if exc.retry_after is not None else DEFAULT_BACKOFF_SECONDS
            attempt += 1
            await asyncio.sleep(delay)
        except LLMError as exc:
            logger.warning(
                "llm call failed",
                extra={"update_id": update_id, "event": type(exc).__name__},
            )
            return None


async def _send_canned_reply(
    sessionmaker: async_sessionmaker[AsyncSession],
    bot: Bot,
    *,
    clock: Clock,
    chat_id: int,
    update_id: int,
    text: str,
    category: str,
    scene_id: int | None = None,
) -> None:
    """Idempotently insert, then send, a fixed cost-zero assistant reply.

    The one mechanism behind every canned reply in this module -- the
    spend-cap block, run_hard_pause, and run_resume -- so a queue
    replay of the same update_id can only ever produce one send.
    _insert_assistant_row's ON CONFLICT DO NOTHING (keyed on the unique
    reply_to_update) makes a second insert a no-op; local_date=None
    means no ledger row is ever written for a canned reply, since no
    model call was made.
    """
    async with sessionmaker() as session:
        existing = await _get_assistant_row(session, update_id)
    if existing is None:
        async with sessionmaker() as session:
            await _insert_assistant_row(
                session,
                update_id=update_id,
                content=text,
                usd_cost=decimal.Decimal("0"),
                local_date=None,
                category=category,
                scene_id=scene_id,
                kind=CANNED_KIND,
            )
    if existing is None or existing.sent_at is None:
        content = existing.content if existing is not None else text
        await send_reply(bot, chat_id, content)
        async with sessionmaker() as session:
            row = await _get_assistant_row(session, update_id)
            await _mark_sent(session, clock, row.id)


async def _ledger_only(
    session: AsyncSession,
    *,
    response,
    settings: Settings,
    local_date: datetime.date,
    category: str,
) -> None:
    """Record what a call cost without storing anything it produced.

    Two 2e callers: the welfare classifier, whose output is a verdict
    rather than a message, and a discarded persona generation. Plan
    section 10 is explicit that a discarded reply's "cost is still
    ledgered" -- the money left regardless of whether the words did.
    """
    if response is None:
        return
    cost = priced(response.usage, settings, model=response.model)
    session.add(
        SpendLedger(
            local_date=local_date,
            category=category,
            model=response.model,
            tokens_in=response.usage.input_tokens,
            tokens_cached=response.usage.cached_tokens,
            tokens_out=response.usage.output_tokens,
            usd_cost=cost.usd,
            cost_source=cost.source,
        )
    )
    await session.commit()


async def _welfare_context(session: AsyncSession, update_id: int) -> list[Message]:
    """The last few in-character messages, for the classifier's input."""
    result = await session.execute(
        select(Message)
        .where(Message.ooc.is_(False))
        .where(Message.update_id.is_distinct_from(update_id))
        .order_by(Message.id.desc())
        .limit(welfare.CONTEXT_MESSAGES)
    )
    rows = list(result.scalars().all())
    rows.reverse()
    return rows


async def _retag_as_welfare(session: AsyncSession, update_id: int) -> None:
    """Move this turn's user message out of the persona's world.

    Plan section 10: welfare exchanges never reach the extractor, a
    scene summary, memory or the journal. The *reply* is written
    kind='welfare', ooc=True from the start, but the message that
    triggered it was stored back at step 1, before anyone knew -- as an
    ordinary in-character line. Left that way it would sit in the
    transcript and the next scene summary, which is exactly the content
    that must not be there.
    """
    await session.execute(
        sql_update(Message)
        .where(Message.update_id == update_id)
        .where(Message.role == "user")
        .values(kind=WELFARE_KIND, ooc=True)
    )
    await session.commit()


async def run_welfare_turn(
    sessionmaker: async_sessionmaker[AsyncSession],
    bot: Bot,
    settings: Settings,
    provider: LLMProvider,
    *,
    clock: Clock,
    chat_id: int,
    update_id: int,
    user_text: str,
    scene_id: int | None,
    discarded,
    timezone: str,
) -> None:
    """Drop the persona and answer plainly (plan section 10).

    Order matters. The persona goes off and the discarded generation is
    ledgered *before* the replacement reply is generated, so that a
    crash between the two leaves the bot paused and silent rather than
    paused-and-about-to-send-a-persona-reply. The discarded text is
    never stored, so no later replay can resurrect it.
    """
    async with sessionmaker() as session:
        await _ledger_only(
            session,
            response=discarded,
            settings=settings,
            local_date=clock_module.local_date(clock, timezone),
            category=CHAT_CATEGORY,
        )
        await update_state(session, "persona_active", False, "welfare")
        await _retag_as_welfare(session, update_id)
        # 3a (phase-3 plan section 4): the cooldown starts here, and is
        # read by the gate to hold back the two discretionary kinds
        # (silence, tick) for WELFARE_COOLDOWN_H. Morning and evening
        # are part of the agreed routine and resume with the persona.
        await record_welfare(session, clock)
        # 3b: the hook is real now. Any message already planned for
        # today is revoked here, and the send-time gate would refuse it
        # anyway on persona_active=false -- two independent stops,
        # because this is the one path where a proactive message
        # arriving would be actively harmful.
        await cancel_outbound(session, clock)

    text, usage = await welfare.generate_reply(provider, user_text)

    async with sessionmaker() as session:
        reply_cost = (
            priced(usage.usage, settings, model=usage.model)
            if usage is not None
            else Priced(decimal.Decimal("0"), None)
        )
        message_id = await _insert_assistant_row(
            session,
            update_id=update_id,
            content=text,
            usd_cost=reply_cost.usd,
            cost_source=reply_cost.source,
            model=usage.model if usage is not None else None,
            tokens_in=usage.usage.input_tokens if usage is not None else None,
            tokens_cached=usage.usage.cached_tokens if usage is not None else None,
            tokens_out=usage.usage.output_tokens if usage is not None else None,
            local_date=(
                clock_module.local_date(clock, timezone) if usage is not None else None
            ),
            category=welfare.WELFARE_CATEGORY,
            scene_id=scene_id,
            kind=WELFARE_KIND,
        )

    if message_id is None:
        return

    # Local import: app/tg/welfare.py imports this module for run_resume.
    from app.tg.welfare import send_welfare_reply

    await send_welfare_reply(bot, chat_id, text)
    async with sessionmaker() as session:
        await _mark_sent(session, clock, message_id)
    logger.info("welfare turn delivered", extra={"update_id": update_id})


async def already_handled(
    sessionmaker: async_sessionmaker[AsyncSession], update_id: int
) -> bool:
    """True iff this update already produced a reply.

    The replay gate, in public form. The worker re-runs an update after
    any crash between feed_update and complete(), and after the 60s
    stuck sweep (app/worker.py), so anything that mutates in response to
    an update has to ask this first or do its work twice. run_hard_pause
    and run_resume above use the same check inline; app/tg/router.py's
    memory commands use this.
    """
    async with sessionmaker() as session:
        return await _get_assistant_row(session, update_id) is not None


async def send_command_reply(
    sessionmaker: async_sessionmaker[AsyncSession],
    bot: Bot,
    *,
    clock: Clock,
    chat_id: int,
    update_id: int,
    text: str,
    scene_id: int | None = None,
) -> None:
    """A plain command reply, stored and sent exactly once (2b).

    The public name for _send_canned_reply on behalf of app/tg/memory.py
    and friends: the memory commands answer with fixed text that costs
    nothing and must survive a queue replay without double-sending,
    which is exactly what the canned-reply machinery already does.
    Stored as kind='canned', so plan section 7's transcript filter keeps
    it out of the persona's context.
    """
    await _send_canned_reply(
        sessionmaker,
        bot,
        clock=clock,
        chat_id=chat_id,
        update_id=update_id,
        text=text,
        category=OOC_CATEGORY,
        scene_id=scene_id,
    )


async def mark_update_handled(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    clock: Clock,
    update_id: int,
    text: str,
    scene_id: int | None = None,
) -> None:
    """Record that this update produced a reply, without sending anything.

    For the handful of replies that carry an inline keyboard and are
    therefore sent directly rather than through send_command_reply: the
    row still has to exist, because it is the thing _once() in
    app/tg/router.py checks to decide whether an update is a replay.

    `sent_at` is set here, unlike _send_canned_reply's two-step
    insert-then-send: the message has already gone out by the time this
    is called, so a row with sent_at NULL would invite a resend of text
    whose keyboard no longer matches any live state.
    """
    async with sessionmaker() as session:
        message_id = await _insert_assistant_row(
            session,
            update_id=update_id,
            content=text,
            usd_cost=decimal.Decimal("0"),
            local_date=None,
            category=OOC_CATEGORY,
            scene_id=scene_id,
            kind=CANNED_KIND,
        )
    if message_id is not None:
        async with sessionmaker() as session:
            await _mark_sent(session, clock, message_id)


async def ensure_scene(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, clock: Clock
) -> int:
    """Resolve the scene this inbound message belongs to (plan section 5).

    Exposed for app/tg/router.py, whose /out, /in and other canned-reply
    command paths produce a turn without going through run(). Every entry
    point that writes a message row resolves its scene through here, so
    the "6h of silence closes the scene" rule cannot be bypassed by
    arriving as a command rather than as chat.
    """
    async with sessionmaker() as session:
        return await ensure_open_scene(
            session, clock, idle_hours=settings.SCENE_IDLE_HOURS
        )


async def run_hard_pause(
    sessionmaker: async_sessionmaker[AsyncSession],
    bot: Bot,
    *,
    clock: Clock,
    chat_id: int,
    update_id: int,
    source: Source = "pause",
    scene_id: int | None = None,
) -> None:
    """HARD pause (plan section 7): shared by /out and the HARD pause-word path.

    `source` is what the state_change audit row records, and the two
    callers differ: /out is a command, a pause word is a pause. The
    audit log exists to answer "how did the persona get switched off",
    so collapsing both onto one value would throw away the only
    evidence that distinguishes them.

    State is mutated only the first time this update_id is seen (the
    same existing-row check that _send_canned_reply also does, run
    before any mutation so a replay never writes a second state_change
    row), then the fixed acknowledgement is sent idempotently.
    """
    async with sessionmaker() as session:
        already_handled = await _get_assistant_row(session, update_id)
    if already_handled is None:
        async with sessionmaker() as session:
            await update_state(session, "persona_active", False, source)
            await cancel_outbound(session, clock)

    await _send_canned_reply(
        sessionmaker,
        bot,
        clock=clock,
        chat_id=chat_id,
        update_id=update_id,
        text=PAUSE_REPLY_TEXT,
        category=OOC_CATEGORY,
        scene_id=scene_id,
    )


async def run_resume(
    sessionmaker: async_sessionmaker[AsyncSession],
    bot: Bot,
    *,
    clock: Clock,
    chat_id: int,
    update_id: int,
    scene_id: int | None = None,
    source: Source = "command",
) -> None:
    """The only place in this repo that sets persona_active=True.

    Two callers, both explicit user actions (plan section 13, which
    amends Phase 1's "/in only"): the /in command, and 2e's
    «Я в порядке, продолжаем» button. They share this function rather
    than each flipping the flag, which is what keeps
    tests/test_turn.py's grep-the-source invariant down to a single
    call site -- the strongest form the rule can take.

    `source` is what separates them in the audit log: a typed command
    versus a button press.
    """
    async with sessionmaker() as session:
        already_handled = await _get_assistant_row(session, update_id)
    if already_handled is None:
        async with sessionmaker() as session:
            # A pause word can never reach here: only /in and the
            # welfare button call this (plan sections 7 and 13).
            await update_state(session, "persona_active", True, source)
            # Phase 5: coming back in is also back to full attention.
            await attention.reset(session)

    await _send_canned_reply(
        sessionmaker,
        bot,
        clock=clock,
        chat_id=chat_id,
        update_id=update_id,
        text=RESUME_REPLY_TEXT,
        category=OOC_CATEGORY,
        scene_id=scene_id,
    )


async def run(
    sessionmaker: async_sessionmaker[AsyncSession],
    bot: Bot,
    settings: Settings,
    provider: LLMProvider,
    *,
    clock: Clock,
    chat_id: int,
    update_id: int,
    user_text: str,
    kind: str = CHAT_KIND,
    extra_flags: list[str] | None = None,
    safety_provider: LLMProvider | None = None,
) -> None:
    """Run one idempotent chat turn. See module and plan sections 7/8 docs.

    1d reorders the top of this function: pause.match() is computed and
    user_state is read *before* step 1 (rather than at the old step 3),
    because step 1 now needs persona_active to decide whether this
    user message belongs in the persona transcript at all.
    """

    # Step 0: a pause word is matched in code, before anything else.
    level = pause.match(user_text)
    async with sessionmaker() as session:
        user_state = await get_state(session)

    # Step 0b (2a): resolve the scene before anything is written, so
    # every row this turn produces -- user, assistant, or canned --
    # carries the same scene_id. Closing a stale scene here also queues
    # its summary (app/core/scene.py), which is why this runs on every
    # inbound message and not only on ones that reach the model.
    scene_id = await ensure_scene(sessionmaker, settings, clock)

    # Step 0c (2d): the check-in note step. Deliberately here and
    # nowhere else -- see the module docstring.
    #
    # A pause word of *either* level wins and clears the flag: plan
    # section 9's "Pause words always win and clear awaiting" is
    # unqualified, so "жёлтый" at the note step lowers intensity and
    # carries on as an ordinary turn rather than being filed as a note.
    flags: list[str] | None = list(extra_flags) if extra_flags else None
    if user_state.awaiting is not None and level is not None:
        async with sessionmaker() as session:
            await checkin.clear_awaiting(session)
    elif level is None and user_state.awaiting == checkin.AWAITING_NOTE:
        async with sessionmaker() as session:
            pending = await checkin.pending_note_checkin(
                session, clock, user_state.timezone
            )
            if pending is not None:
                await checkin.set_note(session, pending.id, user_text)
                row, streak = await checkin.finish(session, clock, user_state.timezone)
                # 5c: the day's order answers, if any, feed the same
                # synthetic line the Пропустить path builds
                # (app/tg/checkin.py's finish_and_react).
                order_results = await orders.results_for_checkin(session, row.id)
                # From here on this turn is the check-in's turn: the
                # raw note never becomes a message of its own.
                user_text = checkin.synthetic_line(row, order_results)
                kind = CHECKIN_KIND
                flags = [*(flags or []), CHECKIN_FLAG]
                user_state = await get_state(session)
                if pending.tg_message_id is not None:
                    # Local import: app/tg/checkin.py imports this
                    # module (lazily, for the same reason).
                    from app.tg.checkin import retire

                    await retire(bot, chat_id, pending.tg_message_id, streak)
    elif level is None and user_state.awaiting == orders.AWAITING_SO_COUNTER:
        # 5c: the plain text that follows «Изменить» (app/core/orders.py's
        # `start_counter`). Not a persona turn at all -- like a command,
        # it never reaches step 1 below, so the counter text itself never
        # becomes a transcript message. Guarded on already_handled(), the
        # same replay gate app/tg/router.py's keyboard-carrying commands
        # use via `_once` -- a replay is simply skipped, not re-sent,
        # matching that precedent exactly.
        if not await already_handled(sessionmaker, update_id):
            async with sessionmaker() as session:
                counter_outcome = await orders.submit_counter(
                    session, user_state.awaiting_ref, user_text, clock=clock
                )
            from app.tg.orders import send_counter_outcome

            await send_counter_outcome(
                sessionmaker, bot, chat_id=chat_id, outcome=counter_outcome
            )
            await mark_update_handled(
                sessionmaker, clock=clock, update_id=update_id, text="[so:counter]", scene_id=scene_id
            )
        return

    # Step 1: store the user message idempotently. A safeword, or any
    # message sent while persona is already off, must never land in
    # the persona transcript.
    ooc = (not user_state.persona_active) or level == "hard"
    async with sessionmaker() as session:
        await _store_user_message_once(
            session, update_id, user_text, ooc=ooc, scene_id=scene_id, kind=kind
        )

    # Step 2: never regenerate if an assistant row already exists.
    # Unchanged, and stays before any state mutation below, so a queue
    # replay can never write a second state_change row or double-send.
    async with sessionmaker() as session:
        existing = await _get_assistant_row(session, update_id)
    if existing is not None:
        if existing.sent_at is None:
            await send_reply(bot, chat_id, existing.content)
            async with sessionmaker() as session:
                await _mark_sent(session, clock, existing.id)
        return

    # Step 3: HARD pause word -- persona off, no LLM call, ever.
    if level == "hard":
        await run_hard_pause(
            sessionmaker,
            bot,
            clock=clock,
            chat_id=chat_id,
            update_id=update_id,
            scene_id=scene_id,
        )
        return

    # Step 4: SOFT pause word -- lower intensity, then carry on into a
    # normal turn with the flag. Still runs at intensity 1 (plan section 7).
    if level == "soft":
        async with sessionmaker() as session:
            user_state = await update_state(
                session, "intensity", max(1, user_state.intensity - 1), "pause"
            )
        flags = [*(flags or []), YELLOW_FLAG]

    # Step 5: spend cap check, before any LLM call. Unchanged from 1c.
    async with sessionmaker() as session:
        over_cap = await check_cap(session, settings, clock, user_state.timezone)
    if over_cap:
        await _send_canned_reply(
            sessionmaker,
            bot,
            clock=clock,
            chat_id=chat_id,
            update_id=update_id,
            text=CAP_REPLY_TEXT,
            category=CHAT_CATEGORY,
            scene_id=scene_id,
        )
        return

    # Step 6 (start): typing indicator, prompt assembly, and the provider call.
    # persona_active picks the prompt: the persona transcript with
    # flags, or the minimal neutral-mode prompt over ooc=True history.
    category = CHAT_CATEGORY if user_state.persona_active else OOC_CATEGORY
    typing_task = start_typing(bot, chat_id)
    injected_memory_ids: list[int] = []
    # 5a: set only on the persona branch below, and reused verbatim by
    # the boundary retry further down -- a retry must not re-roll the
    # mood, the voice anchors or the nickname; see app/core/
    # persona_context.py's docstring.
    turn_persona_context: persona_context_module.PersonaContext | None = None
    planner_active = False  # P4: only ever set True inside the persona branch below.
    planner_lines: list[str] = []  # P4: same -- reused verbatim by the boundary retry.
    try:
        async with sessionmaker() as session:
            if user_state.persona_active:
                # 2b: retrieval lives here, not in prompt.py, because
                # run() needs the ids back to mark them used after
                # delivery. prompt.py only ever sees the texts.
                pinned_rows = await memory.pinned_memories(
                    session, settings.MEMORY_PINNED_MAX
                )
                retrieved_rows = await memory.retrieve_memories(
                    session, user_text, settings.MEMORY_RETRIEVED_MAX
                )
                # 4d: a separate pool with its own small cap (phase-4
                # plan section 10). Their ids join the injected set, so
                # a technique that reached a delivered turn gets its
                # last_used_at bumped -- which is what rotates the
                # least-recently-used fallback in retrieve_techniques.
                technique_rows = await memory.retrieve_techniques(
                    session, user_text, settings.RESEARCH_TECHNIQUES_IN_PROMPT
                )
                # 5a: mood, this scene's voice anchors and the nickname
                # directive -- gathered once per turn, not once per
                # model call (see turn_persona_context's own comment
                # above). 5e: `enable_callback` is only ever True on an
                # ordinary chat turn -- never a check-in's synthetic
                # line (kind is CHECKIN_KIND by the time this runs, see
                # Step 0c above), never neutral mode or a welfare turn
                # (neither reaches this branch at all), and never an
                # outbound send (app/core/outbound_send.py's own
                # gather() calls leave this flag at its False default).
                # Phase 5: Anchor's attention for this turn, before
                # gather() so mood and the flags see it. Short mode only
                # adds a flag; generation below always runs.
                await attention.refresh(
                    session, settings, clock, user_state, exclude_update_id=update_id
                )
                turn_persona_context = await persona_context_module.gather(
                    session,
                    settings,
                    user_state,
                    clock,
                    scene_id=scene_id,
                    exclude_update_id=update_id,
                    rng=NICKNAME_RNG,
                    user_text=user_text,
                    enable_callback=(kind == CHAT_KIND),
                    soft_pause=(level == "soft"),
                )
                # 5e: dedupe -- a callback memory must never also appear
                # under "## Может быть важно" for the same turn (both
                # pools can draw from the same `event` kind).
                if turn_persona_context.callback_memory_id is not None:
                    retrieved_rows = [
                        row
                        for row in retrieved_rows
                        if row.id != turn_persona_context.callback_memory_id
                    ]
                injected_memory_ids = [
                    row.id for row in pinned_rows + retrieved_rows + technique_rows
                ]
                # P2: a snapshot read only -- never a live call to the
                # planner. A stale snapshot (or PLANNER_ENABLED off)
                # means an empty `planner_lines`, which
                # build_now_block(planner=None-or-[]) already renders as
                # no section at all. If it is stale, a PLANNER_SYNC job
                # is queued so the *next* turn sees a fresher one; this
                # turn never waits on it.
                planner_lines: list[str] = []
                planner_active = settings.PLANNER_ENABLED and await planner_auth.is_enabled(
                    session
                )
                if planner_active:
                    snap = await planner_snapshot.get_snapshot(session)
                    if planner_snapshot.is_stale(
                        snap, clock, settings.PLANNER_SNAPSHOT_MAX_AGE_MIN
                    ):
                        now_local = clock_module.now_local(clock, user_state.timezone)
                        await enqueue_job(
                            session,
                            PLANNER_SYNC,
                            {},
                            dedup_key=planner_sync_dedup_key(
                                now_local.date(), now_local.hour, now_local.minute // 5
                            ),
                        )
                    else:
                        planner_lines = planner_snapshot.render_lines(
                            snap,
                            clock,
                            user_state.timezone,
                            max_age_min=settings.PLANNER_SNAPSHOT_MAX_AGE_MIN,
                        )
                    # P4: a still-pending planner_action -- typed as
                    # /task, /event, or proposed from chat -- gets a
                    # notice here regardless of snapshot staleness, so
                    # the persona never claims (or implies) a write is
                    # already done while its confirm card is still
                    # sitting unanswered. This is the "[план: предложено
                    # ..., ждёт подтверждения]" line the P4 plan asks
                    # for; a card created *this* turn shows up starting
                    # the *next* one, since the intent call below runs
                    # concurrently with this very prompt, not before it.
                    await planner_actions.expire_stale(
                        session, clock, settings.PLANNER_PENDING_TTL_HOURS
                    )
                    pending = await planner_actions.pending(session)
                    planner_lines.extend(
                        PLANNER_PENDING_NOTE.format(title=action.payload.get("title", "?"))
                        for action in pending[: planner_actions.MAX_PENDING_NOTES]
                    )
                messages = await build_messages(
                    session,
                    clock=clock,
                    timezone=user_state.timezone,
                    intensity=user_state.intensity,
                    user_text=user_text,
                    update_id=update_id,
                    transcript_turns=settings.TRANSCRIPT_TURNS,
                    # Phase 5: the context's flags first, so the turn's
                    # own (check-in, yellow) and the retry flag stay last.
                    flags=[*turn_persona_context.flags, *(flags or [])],
                    pinned=[row.text for row in pinned_rows],
                    retrieved=[row.text for row in retrieved_rows],
                    techniques=[row.text for row in technique_rows],
                    summaries=await recent_summaries(session),
                    focus_on=user_state.focus_on,
                    due_action=user_state.due_action,
                    due_set_at=user_state.due_set_at,
                    streak=user_state.streak,
                    last_checkin_at=user_state.last_checkin_at,
                    voice_lines=turn_persona_context.voice_lines,
                    mood=turn_persona_context.mood,
                    nickname_directive=turn_persona_context.nickname_directive,
                    notebook=turn_persona_context.notebook,
                    orders=list(turn_persona_context.orders),
                    orders_yesterday=turn_persona_context.orders_yesterday,
                    amendments=list(turn_persona_context.amendments),
                    callback=turn_persona_context.callback,
                    planner=planner_lines,
                    debts=list(turn_persona_context.debts),
                    persona_path=persona_path_for(settings),
                )
            else:
                messages = await build_neutral_messages(
                    session,
                    user_text=user_text,
                    update_id=update_id,
                )
        started_at = time.monotonic()

        # 2e: the welfare classifier runs concurrently with the main
        # generation, not before it. On an ordinary turn it therefore
        # adds no latency at all -- the reply was already waiting on the
        # slower of the two calls.
        run_welfare = (
            safety_provider is not None and user_state.persona_active and level != "hard"
        )
        # P4: same gate as welfare -- persona mode, not a hard pause --
        # plus the feature flag, planner being linked at all, and the
        # prefilter (checked again inside detect(), so a caller mistake
        # here would still cost nothing; checked here too so a prefilter
        # miss never even joins the gather).
        run_intent = (
            run_welfare
            and settings.PLANNER_INTENT
            and planner_active
            and planner_intent.prefilter_hit(user_text)
        )
        welfare_context = []
        intent_result: planner_intent.IntentResult | None = None
        intent_response = None
        if run_welfare:
            async with sessionmaker() as session:
                welfare_context = await _welfare_context(session, update_id)
            if run_intent:
                response, (verdict, welfare_usage, welfare_outcome), (
                    intent_result,
                    intent_response,
                ) = await asyncio.gather(
                    _complete_with_retries(provider, messages, update_id=update_id),
                    welfare.classify(safety_provider, settings, welfare_context, user_text),
                    planner_intent.detect(
                        safety_provider, settings, clock,
                        user_text=user_text, timezone=user_state.timezone,
                    ),
                )
            else:
                response, (verdict, welfare_usage, welfare_outcome) = await asyncio.gather(
                    _complete_with_retries(provider, messages, update_id=update_id),
                    welfare.classify(safety_provider, settings, welfare_context, user_text),
                )
        else:
            verdict, welfare_usage = welfare.Verdict(), None
            welfare_outcome = None
            response = await _complete_with_retries(provider, messages, update_id=update_id)
    finally:
        await stop_typing(typing_task)

    # The classifier's own call is ledgered whatever it concluded: it
    # was billed, so it is recorded.
    if welfare_usage is not None:
        async with sessionmaker() as session:
            await _ledger_only(
                session,
                response=welfare_usage,
                settings=settings,
                local_date=clock_module.local_date(clock, user_state.timezone),
                category=welfare.WELFARE_CATEGORY,
            )

    # P4: same rule for the intent call -- category "planner", the same
    # ledger category app/planner/parse.py's own fallback calls use, so
    # /state's spend breakdown does not grow a third planner-shaped row
    # to explain.
    if intent_response is not None:
        async with sessionmaker() as session:
            await _ledger_only(
                session,
                response=intent_response,
                settings=settings,
                local_date=clock_module.local_date(clock, user_state.timezone),
                category=PLANNER_LEDGER_CATEGORY,
            )

    # H2: the deterministic backstop. A classifier that timed out, errored
    # or returned something unparseable produced a `none` verdict, which
    # is the fail-open behaviour plan section 10 requires -- but failing
    # open on the one case the check exists for is exactly the gap this
    # hardening pass is here to close. So when, and only when, the model
    # gave us nothing usable, the user's words and the two turns before
    # them are checked against a fixed list of terms nobody types by
    # accident (app/core/welfare_terms.py). A hit is treated as `real`.
    #
    # The backstop decides *whether*, never *what*: the reply, the
    # buttons and the persona-off behaviour below are untouched.
    fallback_fired = False
    if welfare_outcome is not None and welfare_outcome != welfare.OK:
        recent = [row.content for row in welfare_context[-2:]]
        fallback_fired = welfare_terms.hit(user_text, *recent)

    if welfare_outcome is not None:
        await safety_events.record(
            sessionmaker,
            clock=clock,
            timezone=user_state.timezone,
            kind=safety_events.WELFARE,
            outcome=welfare.FALLBACK_HIT if fallback_fired else welfare_outcome,
            model=welfare_usage.model if welfare_usage is not None else None,
        )

    if fallback_fired or verdict.is_real(settings):
        await run_welfare_turn(
            sessionmaker,
            bot,
            settings,
            safety_provider,
            clock=clock,
            chat_id=chat_id,
            update_id=update_id,
            user_text=user_text,
            scene_id=scene_id,
            discarded=response,
            timezone=user_state.timezone,
        )
        return

    if response is None:
        await send_reply(bot, chat_id, FAILURE_REPLY_TEXT)
        return

    # H5: the code-side half of the medical/legal boundary. persona.md
    # has always told the model to refuse; nothing checked whether it
    # did, which on an uncensored fine-tune is a preference rather than
    # a guarantee. The reply is read -- never the user's message -- and
    # a hit buys exactly one retry with an explicit correction.
    #
    # Only in persona mode. Neutral mode is already out of character and
    # has no persona instruction to reinforce, so a retry there would
    # spend money to say the same thing again.
    crossed = boundaries.check(response.text) if user_state.persona_active else None
    if crossed is not None:
        logger.info("boundary tripped", extra={"update_id": update_id, "event": crossed})
        # The discarded generation was billed, so it is ledgered -- the
        # same rule the welfare path follows for the reply it throws away.
        async with sessionmaker() as session:
            await _ledger_only(
                session,
                response=response,
                settings=settings,
                local_date=clock_module.local_date(clock, user_state.timezone),
                category=category,
            )
        async with sessionmaker() as session:
            retry_messages = await build_messages(
                session,
                clock=clock,
                timezone=user_state.timezone,
                intensity=user_state.intensity,
                user_text=user_text,
                update_id=update_id,
                transcript_turns=settings.TRANSCRIPT_TURNS,
                flags=[
                    *turn_persona_context.flags,
                    *(flags or []),
                    boundaries.RETRY_FLAG,
                ],
                pinned=[],
                retrieved=[],
                summaries=await recent_summaries(session),
                focus_on=user_state.focus_on,
                due_action=user_state.due_action,
                due_set_at=user_state.due_set_at,
                streak=user_state.streak,
                last_checkin_at=user_state.last_checkin_at,
                # 5a: the same context the first attempt used -- no
                # re-roll of mood/voice/nickname on a retry.
                voice_lines=turn_persona_context.voice_lines,
                mood=turn_persona_context.mood,
                nickname_directive=turn_persona_context.nickname_directive,
                notebook=turn_persona_context.notebook,
                orders=list(turn_persona_context.orders),
                orders_yesterday=turn_persona_context.orders_yesterday,
                amendments=list(turn_persona_context.amendments),
                callback=turn_persona_context.callback,
                planner=planner_lines,
                debts=list(turn_persona_context.debts),
                persona_path=persona_path_for(settings),
            )
        response = await _complete_with_retries(
            provider, retry_messages, update_id=update_id
        )
        # A second trip, a failed retry, or an empty one: stop asking and
        # say the thing the persona was supposed to say. Twice is enough
        # -- a model that ignored an explicit correction will not comply
        # on the third attempt, and the user is owed an answer.
        if response is None or boundaries.check(response.text) is not None:
            logger.info(
                "boundary refused", extra={"update_id": update_id, "event": crossed}
            )
            if response is not None:
                async with sessionmaker() as session:
                    await _ledger_only(
                        session,
                        response=response,
                        settings=settings,
                        local_date=clock_module.local_date(clock, user_state.timezone),
                        category=category,
                    )
            await _send_canned_reply(
                sessionmaker,
                bot,
                clock=clock,
                chat_id=chat_id,
                update_id=update_id,
                text=boundaries.REFUSAL_REPLY_TEXT,
                category=category,
                scene_id=scene_id,
            )
            return

    latency_ms = int((time.monotonic() - started_at) * 1000)
    cost = priced(response.usage, settings, model=response.model)
    usd_cost = cost.usd
    logger.info(
        "turn completed",
        extra={
            "update_id": update_id,
            "latency_ms": latency_ms,
            "tokens_in": response.usage.input_tokens,
            "tokens_cached": response.usage.cached_tokens,
            "tokens_out": response.usage.output_tokens,
            "usd_cost": str(usd_cost),
        },
    )

    # Step 6 (cont'd): assistant row + spend_ledger row, one transaction.
    # category picks ooc=True/False on the row and "chat"/"ooc" on the
    # ledger row -- the single lever neutral mode needs here.
    async with sessionmaker() as session:
        await _insert_assistant_row(
            session,
            update_id=update_id,
            content=response.text,
            usd_cost=usd_cost,
            model=response.model,
            tokens_in=response.usage.input_tokens,
            tokens_cached=response.usage.cached_tokens,
            tokens_out=response.usage.output_tokens,
            cost_source=cost.source,
            local_date=clock_module.local_date(clock, user_state.timezone),
            category=category,
            scene_id=scene_id,
            kind=kind,
        )

    # Split, send, then mark sent -- and only then mark the injected
    # memories used (plan section 6: "after the turn is delivered").
    # mark_used() does not commit; _mark_sent()'s commit covers both, so
    # "this reply was delivered" and "these memories were used" can
    # never diverge.
    await send_reply(bot, chat_id, response.text)
    async with sessionmaker() as session:
        row = await _get_assistant_row(session, update_id)
        await memory.mark_used(session, clock, injected_memory_ids)
        # 5a: only now, with the persona reply actually on its way to
        # the user -- never on a welfare turn (run_welfare_turn returns
        # long before this point) and never on neutral mode (category
        # is OOC_CATEGORY there, and turn_persona_context stays None).
        if (
            category == CHAT_CATEGORY
            and turn_persona_context is not None
            and turn_persona_context.nickname is not None
        ):
            await remember_nickname(session, turn_persona_context.nickname)
        # 5e: same "only after delivery" timing as remember_nickname
        # right above -- and the same guard, `kind == CHAT_KIND`, that
        # gathered the callback in the first place (never a check-in's
        # synthetic turn, never neutral mode or welfare, both of which
        # already leave turn_persona_context None).
        if (
            category == CHAT_CATEGORY
            and turn_persona_context is not None
            and kind == CHAT_KIND
        ):
            await callbacks.mark_delivered(
                session,
                scene_id=scene_id,
                memory_id=turn_persona_context.callback_memory_id,
                clock=clock,
            )
        await _mark_sent(session, clock, row.id)

    # Step 8 (2c): hand the delivered exchange to the extractor.
    # Only in-character turns get here -- see the module docstring.
    # dedup_key makes a queue replay of this update a no-op, and the
    # injected memory ids ride along because the extractor needs them
    # to validate any supersedes_id it proposes (plan section 8).
    if category == CHAT_CATEGORY:
        async with sessionmaker() as session:
            await enqueue_job(
                session,
                EXTRACT,
                {"update_id": update_id, "memory_ids": injected_memory_ids},
                dedup_key=f"extract:{update_id}",
            )

    # P4: a chat-detected intent becomes exactly the same pending card
    # /task and /event produce -- never a write of its own (module
    # docstring of app/planner/intent.py). This sits after the reply is
    # already sent, so the confirm card always arrives as a follow-up
    # message, never mixed into (or ahead of) the persona's own line.
    # The daily write cap is honoured here too (count_today), same as
    # app/tg/router.py's _handle_write_command -- a chat-inferred
    # proposal is not exempt from the cap a typed /task or /event
    # would hit.
    if intent_result is not None:
        async with sessionmaker() as session:
            count = await planner_actions.count_today(session, clock, user_state.timezone)
            if count < settings.PLANNER_MAX_WRITES_PER_DAY:
                action = await planner_actions.create(
                    session, clock, kind=intent_result.kind, payload=intent_result.payload
                )
                action_id = action.id
            else:
                action_id = None
        if action_id is not None:
            await planner_ui.send_confirm_card(
                sessionmaker,
                bot,
                chat_id=chat_id,
                action_id=action_id,
                timezone=user_state.timezone,
            )
