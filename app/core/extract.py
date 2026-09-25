"""The post-turn extractor (plan section 8).

After a delivered in-character turn, a cheap-model call is asked what
changed: new durable facts, a one-line journal entry, and at most one
suggested change to how the bot pushes. It runs as a background job, so
it never delays a reply.

**The invariant this module exists to keep** (plan sections 8 and 13):
the extractor has no write path to `intensity`, `focus_on`,
`due_action`, `streak`, `persona_active`, or rule memories.

That is enforced structurally, not by care. This module imports
exactly four things that can write: `memory.write_memory`,
`proposal.create`, a `Journal` row, and (5c) `orders.propose` --
itself an autonomy module with the very same shape of invariant, one
level down: `orders.propose` can only ever insert a `proposed` row, and
nothing in app/core/orders.py can touch `intensity`, `focus_on`,
`due_action`, `streak` or `persona_active` either (see that module's
own docstring). It does not import `update_state`, and the only name it
holds from app/core/proposal.py is `create` -- `accept()`, the single
function that can touch a sensitive field, is not in scope here and is
reachable only from a button press. tests/test_extract.py asserts that
by source inspection as well as by behaviour, because a future edit
could add the import back without any test noticing otherwise.

Everything the model returns is treated as an untrusted string. Strict
`json_schema` is requested (app/llm/openrouter.py) but is a reliability
measure only: `_validate` re-checks every field, every bound and every
id, because a model that can emit a field is a model that can emit the
wrong one.
"""

from __future__ import annotations

import json
import logging
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core import memory, orders, proposal, redact
from app.core.prompt import PERSONA_TRANSCRIPT_KINDS
from app.core import clock as clock_module
from app.core import safety_events
from app.core.clock import Clock
from app.core.scene import Deferred
from app.core.spend import check_cap, priced
from app.db.models import Journal, Memory, Message, SpendLedger, StateChange
from app.llm.provider import JSONSchema, LLMMessage, LLMProvider

logger = logging.getLogger(__name__)

EXTRACT = "extract"
EXTRACT_CATEGORY = "extractor"

JOURNAL_MAX = 240
MEMORY_TEXT_MAX = 300
REASON_MAX = 120
MAX_MEMORIES = 3
MAX_PROPOSALS = 1

# The extractor may only propose these three kinds directly. `rule` is
# accepted from the model but never written as a memory -- see _apply.
WRITABLE_KINDS = ("identity", "preference", "event")
MODEL_KINDS = (*WRITABLE_KINDS, "rule")
# 5c: `standing_order` joins due_action/focus_on (plan's "Where
# proposals live"). Unlike those two, a standing_order item never
# becomes a `Proposal` row at all -- _apply() below routes it straight
# to app/core/orders.propose(), because the negotiation needs that
# module's own statuses and counter_of, not this one's pending/accepted/
# rejected/expired shape.
#
# Phase 5 (spec 2026-09-25): `obligation`, a one-shot debt the user
# promised. It goes through proposal.create() like due_action, so it
# lands as a pending Proposal and only the [Принять] button opens it
# (proposal.accept -> app/core/obligations.py). This module never
# touches app/core/obligations.py; tests/test_extract.py checks that.
PROPOSAL_FIELDS = (
    proposal.DUE_ACTION,
    proposal.FOCUS_ON,
    proposal.STANDING_ORDER,
    proposal.OBLIGATION,
)

# 5c: the extractor's own cadence tokens for a standing_order proposal
# item. `null` (cadence omitted / None) is valid too -- validate() below
# drops a standing_order item with no cadence rather than guessing one.
ORDER_CADENCES = ("daily", "weekdays", "weekly", "once")

# How many transcript messages precede this turn's exchange in the
# extractor's input (plan section 8: "the last 4 transcript messages").
CONTEXT_MESSAGES = 4

EXTRACT_SCHEMA = JSONSchema(
    name="anchor_extract",
    strict=True,
    schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["journal", "memories", "proposals"],
        "properties": {
            "journal": {"type": ["string", "null"]},
            "memories": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "text", "supersedes_id", "confidence"],
                    "properties": {
                        "kind": {"type": "string", "enum": list(MODEL_KINDS)},
                        "text": {"type": "string"},
                        "supersedes_id": {"type": ["integer", "null"]},
                        "confidence": {"type": "number"},
                    },
                },
            },
            "proposals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["field", "value", "reason", "cadence", "weekday"],
                    "properties": {
                        "field": {"type": "string", "enum": list(PROPOSAL_FIELDS)},
                        "value": {"type": "string"},
                        "reason": {"type": "string"},
                        # 5c: only meaningful when field='standing_order'
                        # (plan's "Extractor schema"). null for the other
                        # two fields, which validate() enforces by simply
                        # never reading them for those.
                        "cadence": {"type": ["string", "null"], "enum": [*ORDER_CADENCES, None]},
                        "weekday": {"type": ["integer", "null"]},
                    },
                },
            },
        },
    },
)

# Plan section 8, verbatim.
EXTRACT_PROMPT = (
    "Ты — модуль учёта. По последнему обмену репликами верни JSON по схеме.\n"
    "- `memories`: только новые устойчивые факты, которые пользователь сам сказал о себе "
    "(кто он, что предпочитает, что произошло, какие правила он сам себе ставит). "
    "Не выдумывай и не додумывай. Если факт уточняет или отменяет существующий — "
    "укажи его id в `supersedes_id`.\n"
    "- Никогда не сохраняй: здоровье и диагнозы, кризисы и самоповреждение, пароли и "
    "номера документов/карт, подробности о третьих лицах сверх имени и роли.\n"
    "- `proposals`: только если пользователь явно договорился о новом главном действии "
    "или о включении/выключении фокуса. Иначе пусто.\n"
    "- standing_order — только если пользователь сам говорит о повторяющемся деле, "
    "которое хочет держать; текст его словами, без ужесточения.\n"
    "- obligation — только если пользователь сам пообещал одно конкретное разовое дело "
    "(«пришлю отчёт завтра»); текст его словами, без ужесточения.\n"
    "- `journal`: одно нейтральное предложение о том, что произошло, или null.\n"
    "Если ничего нового — пустые массивы и null."
)

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def parse_json(raw: str) -> dict | None:
    """Parse the model's reply, tolerating the wrappers models add.

    Strict schema should make this unnecessary, but it is requested,
    not guaranteed -- and when LLM_STRUCTURED_OUTPUTS is off it is not
    even requested. A fenced code block or a sentence of preamble is the
    common failure, so the first {...} span is tried before giving up.
    Returns None rather than raising: a model that returns prose is a
    no-op turn, not an error worth retrying three times.
    """
    for candidate in (raw, *(m.group() for m in [_JSON_BLOCK.search(raw)] if m)):
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _clean_text(value, limit: int) -> str | None:
    """A non-empty string within `limit`, or None. Never truncates.

    Truncating a fact at 300 characters would store half a sentence and
    call it a memory; dropping it keeps the store honest.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > limit:
        return None
    return text


def validate(payload: dict, *, offered_ids: set[int]) -> dict:
    """Coerce the model's output into something safe to apply.

    Every field is re-checked here regardless of what the schema
    promised. `offered_ids` are the memory ids that were actually shown
    to the extractor: a `supersedes_id` outside that set is nulled
    rather than honoured, so the model cannot retire a memory it was
    never shown (plan section 8's apply table).
    """
    journal = _clean_text(payload.get("journal"), JOURNAL_MAX)

    memories = []
    raw_memories = payload.get("memories")
    if isinstance(raw_memories, list):
        for item in raw_memories:
            if len(memories) >= MAX_MEMORIES:
                break
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            text = _clean_text(item.get("text"), MEMORY_TEXT_MAX)
            if kind not in MODEL_KINDS or text is None:
                continue
            confidence = item.get("confidence")
            if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
                continue
            supersedes_id = item.get("supersedes_id")
            if not isinstance(supersedes_id, int) or isinstance(supersedes_id, bool):
                supersedes_id = None
            if supersedes_id not in offered_ids:
                supersedes_id = None
            memories.append(
                {
                    "kind": kind,
                    "text": text,
                    "supersedes_id": supersedes_id,
                    "confidence": float(confidence),
                }
            )

    proposals = []
    raw_proposals = payload.get("proposals")
    if isinstance(raw_proposals, list):
        for item in raw_proposals:
            if len(proposals) >= MAX_PROPOSALS:
                break
            if not isinstance(item, dict):
                continue
            field = item.get("field")
            value = _clean_text(item.get("value"), MEMORY_TEXT_MAX)
            if field not in PROPOSAL_FIELDS or value is None:
                continue

            cadence = item.get("cadence")
            weekday = item.get("weekday")
            if not isinstance(weekday, int) or isinstance(weekday, bool) or not 1 <= weekday <= 7:
                weekday = None

            if field == proposal.STANDING_ORDER:
                # 5c: a valid cadence is required, and `weekly` requires
                # a weekday too -- otherwise the whole item is dropped
                # (plan's "Extractor schema": "Otherwise the proposal is
                # dropped").
                if cadence not in ORDER_CADENCES:
                    continue
                if cadence == "weekly" and weekday is None:
                    continue
                if cadence != "weekly":
                    weekday = None
            else:
                cadence = None
                weekday = None

            proposals.append(
                {
                    "field": field,
                    "value": value,
                    "reason": _clean_text(item.get("reason"), REASON_MAX),
                    "cadence": cadence,
                    "weekday": weekday,
                }
            )

    return {"journal": journal, "memories": memories, "proposals": proposals}


async def _context_messages(
    session: AsyncSession, *, update_id: int, limit: int
) -> list[Message]:
    """The `limit` in-character messages preceding this turn."""
    result = await session.execute(
        select(Message)
        .where(Message.ooc.is_(False))
        .where(Message.kind.in_(PERSONA_TRANSCRIPT_KINDS))
        .where(Message.update_id.is_distinct_from(update_id))
        .order_by(Message.id.desc())
        .limit(limit)
    )
    rows = list(result.scalars().all())
    rows.reverse()
    return rows


async def _turn_exchange(session: AsyncSession, update_id: int) -> tuple[str | None, str | None]:
    result = await session.execute(
        select(Message).where(Message.update_id == update_id).order_by(Message.id)
    )
    rows = list(result.scalars().all())
    user = next((r.content for r in rows if r.role == "user"), None)
    assistant = next((r.content for r in rows if r.role == "assistant"), None)
    return user, assistant


def build_input(
    *,
    intensity: int,
    focus_on: bool,
    due_action: str | None,
    offered: list[Memory],
    context: list[Message],
    user_text: str,
    assistant_text: str,
) -> str:
    """The user-role message the extractor sees (plan section 8's "Input").

    Memory ids appear here and **only** here: this is the one place in
    the codebase where a model is shown them, because `supersedes_id`
    requires it. The chat model never sees them (app/core/prompt.py).
    """
    lines = [
        "## Состояние",
        f"Интенсивность: {intensity}/5",
        f"Фокус: {'вкл' if focus_on else 'выкл'}",
        f"Главное действие: {due_action or 'нет'}",
    ]
    if offered:
        lines.append("")
        lines.append("## Что уже известно (id — текст)")
        lines.extend(f"{row.id} — {row.text}" for row in offered)
    if context:
        lines.append("")
        lines.append("## Недавние реплики")
        lines.extend(
            f"{'Пользователь' if row.role == 'user' else 'Anchor'}: {row.content}"
            for row in context
        )
    lines.append("")
    lines.append("## Последний обмен")
    lines.append(f"Пользователь: {user_text}")
    lines.append(f"Anchor: {assistant_text}")
    return "\n".join(lines)


class ExtractOutcome:
    """What an extraction changed, for the worker to act on.

    `created` are proposal ids in creation order; only the last can
    still be pending, since each create() expires the one before.
    `expired` are proposals whose buttons are now stale and should be
    edited away (plan section 8).

    `order_proposed` (5c) is the id of a `standing_order` row this turn
    proposed, or None. It is kept separate from `created`/`expired`
    because a standing-order proposal is never a `Proposal` row -- see
    _apply()'s own comment -- so app/worker.py sends its card through a
    different function, app/tg/orders.send_order_proposal, not
    app/tg/proposals.send_proposal.
    """

    __slots__ = ("memories", "created", "expired", "order_proposed", "amendment_trial_id")

    def __init__(self) -> None:
        self.memories: list[int] = []
        self.created: list[int] = []
        self.expired: list[int] = []
        self.order_proposed: int | None = None
        # 5d: the id of a PersonaAmendment whose `amendment_trial` job
        # just finished, or None. app/worker.py's process_one_job reads
        # this the same way it reads `order_proposed`, to send the
        # result message ("Поправка принята."/"не прошла") through
        # app/tg/amendments.py rather than app/tg/proposals.py -- an
        # amendment_trial outcome is not a Proposal row either.
        self.amendment_trial_id: int | None = None


async def _apply(
    session: AsyncSession,
    settings: Settings,
    clock: Clock,
    result: dict,
    *,
    local_date,
) -> ExtractOutcome:
    """Apply validated extractor output.

    The apply table from plan section 8, in code:

    - `journal`          -> a journal row.
    - identity/preference/event, confidence >= MEMORY_AUTOWRITE_MIN_CONF
                         -> a memory with source='extractor', deduped
                            per section 6. Below that confidence: dropped.
    - kind `rule`        -> **never** written. Becomes a proposal, at any
                            confidence, because a rule is the user
                            instructing themselves and only they may
                            agree to it.
    - `proposals`        -> a pending proposal row and nothing else.

    Redaction (app/core/redact.py) runs before any write.
    """
    outcome = ExtractOutcome()

    if result["journal"] is not None:
        secret = redact.find_secret(result["journal"])
        if secret is None:
            session.add(Journal(local_date=local_date, text=result["journal"]))
            session.add(
                StateChange(
                    field="journal", old_value=None, new_value=None, source="extractor"
                )
            )
            await session.commit()
        else:
            logger.info("journal rejected by redaction", extra={"event": secret})

    for item in result["memories"]:
        secret = redact.find_secret(item["text"])
        if secret is not None:
            logger.info("memory rejected by redaction", extra={"event": secret, "kind": item["kind"]})
            continue

        if item["kind"] == proposal.RULE:
            # Never auto-written at any confidence (plan section 8).
            created, expired = await proposal.create(
                session, clock, field=proposal.RULE, value=item["text"], reason=None
            )
            outcome.created.append(created.id)
            if expired is not None:
                outcome.expired.append(expired.id)
            continue

        if item["confidence"] < settings.MEMORY_AUTOWRITE_MIN_CONF:
            logger.info("memory dropped, low confidence", extra={"kind": item["kind"]})
            continue

        row = await memory.write_memory(
            session,
            kind=item["kind"],
            text=item["text"],
            source="extractor",
            supersedes_id=item["supersedes_id"],
        )
        if row is not None:
            outcome.memories.append(row.id)
            session.add(
                StateChange(
                    field="memory",
                    old_value=None,
                    new_value=str(row.id),
                    source="extractor",
                )
            )
            await session.commit()

    for item in result["proposals"]:
        secret = redact.find_secret(item["value"])
        if secret is not None:
            logger.info("proposal rejected by redaction", extra={"event": secret})
            continue

        if item["field"] == proposal.STANDING_ORDER:
            # 5c: routed to app/core/orders.propose(), never to
            # proposal.create() -- see PROPOSAL_FIELDS' own comment and
            # ExtractOutcome.order_proposed's docstring.
            order = await orders.propose(
                session, item["value"], item["cadence"], item["weekday"], source="anchor"
            )
            if order is not None:
                outcome.order_proposed = order.id
            continue

        created, expired = await proposal.create(
            session, clock, field=item["field"], value=item["value"], reason=item["reason"]
        )
        outcome.created.append(created.id)
        if expired is not None:
            outcome.expired.append(expired.id)

    return outcome


async def run_extract(
    session: AsyncSession,
    settings: Settings,
    provider: LLMProvider,
    *,
    update_id: int,
    memory_ids: list[int],
    clock: Clock,
    timezone: str,
    intensity: int,
    focus_on: bool,
    due_action: str | None,
) -> ExtractOutcome:
    """The `extract` job body. Returns what it changed.

    Skipped entirely at the daily cap (plan section 12): unlike a scene
    summary, an extraction has no later value -- the exchange it
    describes will have aged out of the transcript by tomorrow -- so it
    is dropped rather than deferred.
    """
    if await check_cap(session, settings, clock, timezone):
        logger.info("extract skipped, daily cap reached", extra={"update_id": update_id})
        return ExtractOutcome()

    user_text, assistant_text = await _turn_exchange(session, update_id)
    if user_text is None or assistant_text is None:
        logger.info("extract skipped, incomplete exchange", extra={"update_id": update_id})
        return ExtractOutcome()

    offered = []
    if memory_ids:
        result = await session.execute(select(Memory).where(Memory.id.in_(memory_ids)))
        offered = list(result.scalars().all())
    offered_ids = {row.id for row in offered}

    context = await _context_messages(
        session, update_id=update_id, limit=CONTEXT_MESSAGES
    )

    response = await provider.complete(
        [
            LLMMessage(role="system", content=EXTRACT_PROMPT),
            LLMMessage(
                role="user",
                content=build_input(
                    intensity=intensity,
                    focus_on=focus_on,
                    due_action=due_action,
                    offered=offered,
                    context=context,
                    user_text=user_text,
                    assistant_text=assistant_text,
                ),
            ),
        ],
        conversation_id=f"anchor-extract-{update_id}",
        json_schema=EXTRACT_SCHEMA,
    )

    cost = priced(response.usage, settings, model=response.model)
    usd_cost = cost.usd
    session.add(
        SpendLedger(
            local_date=clock_module.local_date(clock, timezone),
            category=EXTRACT_CATEGORY,
            model=response.model,
            tokens_in=response.usage.input_tokens,
            tokens_cached=response.usage.cached_tokens,
            tokens_out=response.usage.output_tokens,
            usd_cost=usd_cost,
            cost_source=cost.source,
        )
    )
    await session.commit()

    payload = parse_json(response.text)
    # H2: record whether the strict-schema call actually produced usable
    # JSON. Staged in this job's transaction; a transport failure instead
    # re-raises for the queue to retry, and is visible there.
    await safety_events.record_in(
        session,
        clock=clock,
        timezone=timezone,
        kind=safety_events.EXTRACTOR,
        outcome=safety_events.PARSE_FAIL if payload is None else "ok",
        model=response.model,
    )
    await session.commit()
    if payload is None:
        logger.warning("extract returned unparseable output", extra={"update_id": update_id})
        return ExtractOutcome()

    result = validate(payload, offered_ids=offered_ids)
    outcome = await _apply(
        session,
        settings,
        clock,
        result,
        local_date=clock_module.local_date(clock, timezone),
    )
    logger.info(
        "extract applied",
        extra={
            "update_id": update_id,
            "count": len(outcome.memories),
            "tokens_in": response.usage.input_tokens,
            "tokens_out": response.usage.output_tokens,
            "usd_cost": str(usd_cost),
        },
    )
    return outcome


# Deferred is re-exported so app/worker.py's job runner can treat the
# extract and summarize handlers uniformly; extraction itself never
# defers (see run_extract).
__all__ = [
    "EXTRACT",
    "EXTRACT_PROMPT",
    "EXTRACT_SCHEMA",
    "ExtractOutcome",
    "Deferred",
    "build_input",
    "parse_json",
    "run_extract",
    "validate",
]
