"""One idempotent chat turn (plan section 8).

Step numbers below match the plan's list.

Retries live here, not in the SDK or app/llm/xai.py: this is the only
way FakeLLMProvider (tests/conftest.py) can exercise "fails twice, then
succeeds" and "fails until exhausted" without a network, and it keeps
the retry budget in one visible place instead of splitting it between
this loop and a hidden SDK default. app/llm/xai.py builds its client
with max_retries=0 for exactly this reason.

Crash-safety is the point of the step ordering: dying between step 5
(provider call) and step 6 (DB write) re-runs the whole turn and wastes
one LLM call but delivers no duplicate; dying between step 6 and step 7
(send) leaves a row with sent_at NULL, so the next run resends without
a second model call; dying after step 7 makes the next run's step 2 a
no-op. This is what makes "kill the process mid-turn, restart, get
exactly one reply" (plan section 18) true.
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
from app.core.prompt import build_messages
from app.core.spend import check_cap, compute_cost, local_date_for
from app.core.state import get_state
from app.db.models import Message, SpendLedger
from app.llm.provider import LLMError, LLMProvider, LLMRetryableError
from app.tg.send import send_reply, start_typing, stop_typing

logger = logging.getLogger(__name__)

# A single-user bot has exactly one conversation; xAI's prompt cache is
# keyed on this string (the Responses API's prompt_cache_key field).
CONVERSATION_ID = "anchor-main"

MAX_RETRIES = 2
DEFAULT_BACKOFF_SECONDS = 1.0

CAP_REPLY_TEXT = "На сегодня всё, лимит. Продолжим завтра."
FAILURE_REPLY_TEXT = "Связь с моделью упала, попробуй чуть позже."

CHAT_CATEGORY = "chat"


async def _store_user_message_once(
    session: AsyncSession, update_id: int | None, content: str
) -> None:
    """Insert a `message` row for this update, unless one already exists.

    Moved here from app/tg/router.py in 1c: it is now step 1 of the
    idempotent turn rather than something the router does on its own.
    Idempotency guard for queue replay (e.g. after worker.recover_stuck
    resets a crashed row back to pending): there is no unique
    constraint on message.update_id, so this is a check-then-insert.
    That is race-free only because the worker's concurrency is 1 (plan
    section 6.3) -- never call this from more than one worker.
    """
    if update_id is not None:
        existing = await session.execute(
            select(Message.id).where(Message.update_id == update_id, Message.role == "user")
        )
        if existing.scalar_one_or_none() is not None:
            return
    session.add(Message(role="user", content=content, update_id=update_id, ooc=False))
    await session.commit()


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


async def run(
    sessionmaker: async_sessionmaker[AsyncSession],
    bot: Bot,
    settings: Settings,
    provider: LLMProvider,
    *,
    chat_id: int,
    update_id: int,
    user_text: str,
) -> None:
    """Run one idempotent chat turn. See module and plan section 8 docs."""

    # Step 1: store the user message idempotently.
    async with sessionmaker() as session:
        await _store_user_message_once(session, update_id, user_text)

    # Step 2: never regenerate if an assistant row already exists.
    async with sessionmaker() as session:
        existing = await _get_assistant_row(session, update_id)
    if existing is not None:
        if existing.sent_at is None:
            await send_reply(bot, chat_id, existing.content)
            async with sessionmaker() as session:
                await _mark_sent(session, existing.id)
        return

    # Step 3: spend cap check, before any LLM call.
    async with sessionmaker() as session:
        user_state = await get_state(session)
        over_cap = await check_cap(session, settings, user_state.timezone)
    if over_cap:
        async with sessionmaker() as session:
            await _insert_assistant_row(
                session,
                update_id=update_id,
                content=CAP_REPLY_TEXT,
                usd_cost=decimal.Decimal("0"),
                local_date=None,  # no ledger row: no call was made
            )
        await send_reply(bot, chat_id, CAP_REPLY_TEXT)
        async with sessionmaker() as session:
            row = await _get_assistant_row(session, update_id)
            await _mark_sent(session, row.id)
        return

    # Step 4/5: typing indicator, prompt assembly, and the provider call.
    typing_task = start_typing(bot, chat_id)
    try:
        async with sessionmaker() as session:
            messages = await build_messages(
                session,
                timezone=user_state.timezone,
                intensity=user_state.intensity,
                user_text=user_text,
                update_id=update_id,
                transcript_turns=settings.TRANSCRIPT_TURNS,
            )
        started_at = time.monotonic()
        response = await _complete_with_retries(provider, messages, update_id=update_id)
    finally:
        await stop_typing(typing_task)

    if response is None:
        await send_reply(bot, chat_id, FAILURE_REPLY_TEXT)
        return

    latency_ms = int((time.monotonic() - started_at) * 1000)
    usd_cost = compute_cost(response.usage, settings)
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

    # Step 6: assistant row + spend_ledger row, one transaction.
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
        )

    # Step 7: split, send, then mark sent.
    await send_reply(bot, chat_id, response.text)
    async with sessionmaker() as session:
        row = await _get_assistant_row(session, update_id)
        await _mark_sent(session, row.id)
