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

1f adds `web_search`, threaded from run() down to the single provider
call: it is opt-in only, set by app/tg/router.py's /search handler and
nowhere else, so an ordinary text turn never sends OpenRouter's `web`
plugin. run_search_canned_reply() covers the two /search cases that
never reach the model at all (empty query, search disabled), via the
same _send_canned_reply() idempotency as every other canned reply here.
"""

from __future__ import annotations

import asyncio
import datetime
import decimal
import logging
import time

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core import pause
from app.core.outbound import cancel_outbound
from app.core import memory
from app.core.prompt import build_messages, build_neutral_messages
from app.core.scene import bump_message_count, ensure_open_scene, recent_summaries
from app.core.spend import check_cap, compute_cost, local_date_for
from app.core.state import Source, get_state, update_state
from app.db.models import Message, SpendLedger
from app.llm.provider import LLMError, LLMProvider, LLMRetryableError
from app.tg.send import send_reply, start_typing, stop_typing

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

# 1f: /search canned replies -- neither ever reaches the model.
SEARCH_EMPTY_REPLY_TEXT = "Что поискать? Напиши так: /search вопрос."
SEARCH_DISABLED_REPLY_TEXT = "Поиск сейчас выключен."

CHAT_CATEGORY = "chat"
OOC_CATEGORY = "ooc"

# message.kind (phase-2 plan section 4), distinct from the ledger's
# category above: `kind` says what sort of message this is, `category`
# says which budget line paid for it. A canned reply has kind='canned'
# and no ledger row at all.
CHAT_KIND = "chat"
CANNED_KIND = "canned"


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


async def _mark_sent(session: AsyncSession, message_id: int) -> None:
    message = await session.get(Message, message_id)
    message.sent_at = datetime.datetime.now(datetime.timezone.utc)
    await session.commit()


async def _insert_assistant_row(
    session: AsyncSession,
    *,
    update_id: int,
    content: str,
    usd_cost: decimal.Decimal,
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
            )
        )
    await session.commit()
    return message_id


async def _complete_with_retries(
    provider: LLMProvider, messages, *, update_id: int, web_search: bool = False
):
    """Call provider.complete, retrying retryable errors up to MAX_RETRIES times.

    Returns the LLMResponse on success, or None once retries (or a
    non-retryable error) are exhausted. Only type(exc).__name__ is
    logged, per the privacy rule -- never str(exc), which could echo
    prompt content back from an error body.
    """
    attempt = 0
    while True:
        try:
            return await provider.complete(
                messages, conversation_id=CONVERSATION_ID, web_search=web_search
            )
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
            await _mark_sent(session, row.id)


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
        chat_id=chat_id,
        update_id=update_id,
        text=text,
        category=OOC_CATEGORY,
        scene_id=scene_id,
    )


async def mark_update_handled(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
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
            await _mark_sent(session, message_id)


async def ensure_scene(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
) -> int:
    """Resolve the scene this inbound message belongs to (plan section 5).

    Exposed for app/tg/router.py, whose /out, /in and canned /search
    paths produce a turn without going through run(). Every entry point
    that writes a message row resolves its scene through here, so the
    "6h of silence closes the scene" rule cannot be bypassed by
    arriving as a command rather than as chat.
    """
    async with sessionmaker() as session:
        return await ensure_open_scene(session, idle_hours=settings.SCENE_IDLE_HOURS)


async def run_search_canned_reply(
    sessionmaker: async_sessionmaker[AsyncSession],
    bot: Bot,
    *,
    chat_id: int,
    update_id: int,
    text: str,
    scene_id: int | None = None,
) -> None:
    """The two /search cases that never reach the model: an empty query
    (SEARCH_EMPTY_REPLY_TEXT) or LLM_WEB_SEARCH=false
    (SEARCH_DISABLED_REPLY_TEXT). `text` picks which. Uses the same
    _send_canned_reply idempotency as every other canned reply in this
    module, so a queue replay of the same update_id sends at most once.
    """
    await _send_canned_reply(
        sessionmaker,
        bot,
        chat_id=chat_id,
        update_id=update_id,
        text=text,
        category=OOC_CATEGORY,
        scene_id=scene_id,
    )


async def run_hard_pause(
    sessionmaker: async_sessionmaker[AsyncSession],
    bot: Bot,
    *,
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
        await cancel_outbound()

    await _send_canned_reply(
        sessionmaker,
        bot,
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
    chat_id: int,
    update_id: int,
    scene_id: int | None = None,
) -> None:
    """/in only. The only place in this repo that sets persona_active=True
    (tests/test_turn.py enforces this by grepping app/ for the literal)."""
    async with sessionmaker() as session:
        already_handled = await _get_assistant_row(session, update_id)
    if already_handled is None:
        async with sessionmaker() as session:
            # Always "command": /in is the only caller and a pause word
            # can never resume the persona (plan section 7).
            await update_state(session, "persona_active", True, "command")

    await _send_canned_reply(
        sessionmaker,
        bot,
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
    chat_id: int,
    update_id: int,
    user_text: str,
    web_search: bool = False,
) -> None:
    """Run one idempotent chat turn. See module and plan sections 7/8 docs.

    1d reorders the top of this function: pause.match() is computed and
    user_state is read *before* step 1 (rather than at the old step 3),
    because step 1 now needs persona_active to decide whether this
    user message belongs in the persona transcript at all.

    1f: `web_search` is opt-in, set only by app/tg/router.py's /search
    handler. It flows straight through every existing gate (pause words,
    idempotency, the spend cap, neutral mode) to the single provider
    call at step 6 -- it changes nothing about how the turn is run,
    only whether that one call asks OpenRouter to search first.
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
    scene_id = await ensure_scene(sessionmaker, settings)

    # Step 1: store the user message idempotently. A safeword, or any
    # message sent while persona is already off, must never land in
    # the persona transcript.
    ooc = (not user_state.persona_active) or level == "hard"
    async with sessionmaker() as session:
        await _store_user_message_once(
            session, update_id, user_text, ooc=ooc, scene_id=scene_id, kind=CHAT_KIND
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
                await _mark_sent(session, existing.id)
        return

    # Step 3: HARD pause word -- persona off, no LLM call, ever.
    if level == "hard":
        await run_hard_pause(
            sessionmaker, bot, chat_id=chat_id, update_id=update_id, scene_id=scene_id
        )
        return

    # Step 4: SOFT pause word -- lower intensity, then carry on into a
    # normal turn with the flag. Still runs at intensity 1 (plan section 7).
    flags: list[str] | None = None
    if level == "soft":
        async with sessionmaker() as session:
            user_state = await update_state(
                session, "intensity", max(1, user_state.intensity - 1), "pause"
            )
        flags = [YELLOW_FLAG]

    # Step 5: spend cap check, before any LLM call. Unchanged from 1c.
    async with sessionmaker() as session:
        over_cap = await check_cap(session, settings, user_state.timezone)
    if over_cap:
        await _send_canned_reply(
            sessionmaker,
            bot,
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
                injected_memory_ids = [row.id for row in pinned_rows + retrieved_rows]
                messages = await build_messages(
                    session,
                    timezone=user_state.timezone,
                    intensity=user_state.intensity,
                    user_text=user_text,
                    update_id=update_id,
                    transcript_turns=settings.TRANSCRIPT_TURNS,
                    flags=flags,
                    pinned=[row.text for row in pinned_rows],
                    retrieved=[row.text for row in retrieved_rows],
                    summaries=await recent_summaries(session),
                )
            else:
                messages = await build_neutral_messages(
                    session,
                    user_text=user_text,
                    update_id=update_id,
                )
        started_at = time.monotonic()
        response = await _complete_with_retries(
            provider, messages, update_id=update_id, web_search=web_search
        )
    finally:
        await stop_typing(typing_task)

    if response is None:
        await send_reply(bot, chat_id, FAILURE_REPLY_TEXT)
        return

    latency_ms = int((time.monotonic() - started_at) * 1000)
    usd_cost = compute_cost(response.usage, settings, model=response.model)
    logger.info(
        "turn completed",
        extra={
            "update_id": update_id,
            "latency_ms": latency_ms,
            "tokens_in": response.usage.input_tokens,
            "tokens_cached": response.usage.cached_tokens,
            "tokens_out": response.usage.output_tokens,
            "usd_cost": str(usd_cost),
            "search": web_search,
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
            local_date=local_date_for(user_state.timezone),
            category=category,
            scene_id=scene_id,
            kind=CHAT_KIND,
        )

    # Split, send, then mark sent -- and only then mark the injected
    # memories used (plan section 6: "after the turn is delivered").
    # mark_used() does not commit; _mark_sent()'s commit covers both, so
    # "this reply was delivered" and "these memories were used" can
    # never diverge.
    await send_reply(bot, chat_id, response.text)
    async with sessionmaker() as session:
        row = await _get_assistant_row(session, update_id)
        await memory.mark_used(session, injected_memory_ids)
        await _mark_sent(session, row.id)
