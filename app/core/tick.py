"""The optional tick: deciding whether to write first (phase-3 plan section 8).

Everything proactive before this fires on a clock rule. The tick fires
on a **judgement**, and it is the first time a model's output has any
say in whether an unsolicited message goes out. So the shape of this
module is dictated by plan section 11's invariant: **the model only
proposes.**

What the model may do: return `{"send": bool, "note": "<=120 chars"}`.
That is all. The note becomes `outbound.tick_note` and is interpolated
into a hidden flag at generation time. It cannot skip a gate, cannot
change `intensity`, `focus_on`, `due_action`, `streak` or
`persona_active`, and cannot make anything happen that the code gate
has not already allowed -- twice. A tick passes three checks in order:

    1. the planning gate, here, *before* the model is called;
    2. the model, which defaults to no;
    3. the send-time gate, in app/core/outbound_send.py, which is
       authoritative and runs minutes later.

**A refused gate means no model call.** Plan section 8 step 1. That is
why the gate is first and not a filter on the result: a tick the code
would refuse anyway must not cost anything.

**The call is ledgered whichever way it goes**, including when the
reply is unparseable prose. The money left regardless of whether a
message did -- the same rule Phase 2 applies to a discarded welfare
generation.

**Nothing here logs the note.** Plan rule 8: no message text, prompts
or completions in logs. The log lines carry the decision and the
outbound id, never the reason.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core import clock as clock_module
from app.core import safety_events
from app.core import redact
from app.core.clock import Clock
from app.core.extract import parse_json
from app.core.outbound import load_gate_inputs
from app.core.outbound_gate import TICK, config_from_settings, gate
from app.core.prompt import build_now_block, recent_transcript
from app.core.scheduler import pick_send_time, plan
from app.core.spend import priced
from app.core.state import get_state
from app.db.models import Journal, SpendLedger
from app.planner import snapshot as planner_snapshot
from app.llm.provider import JSONSchema, LLMMessage, LLMProvider

logger = logging.getLogger(__name__)

# Its own ledger line, so /state can show what *deciding* costs
# separately from what speaking costs.
TICK_CATEGORY = "tick"

# Plan section 8 step 2's input sizes.
CONTEXT_MESSAGES = 6
JOURNAL_LINES = 3

# The outbound table's own check constraint. Kept as a constant here so
# validate() can refuse an over-long note rather than let the insert
# raise (see _clean_note).
NOTE_MAX = 120

DECISION_PROMPT = (
    "Ты решаешь, стоит ли Anchor написать первым прямо сейчас. "
    "По умолчанию — нет. Да — только при естественном поводе: "
    "незакрытая тема из последнего разговора, главное действие с близким "
    "сроком, пользователь сам сказал, что сделает что-то сегодня. "
    "Не пиши просто чтобы напомнить о себе. Верни JSON."
)

TICK_SCHEMA = JSONSchema(
    name="anchor_tick",
    strict=True,
    schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["send", "note"],
        "properties": {
            "send": {"type": "boolean"},
            "note": {"type": "string"},
        },
    },
)

_ROLE_LABELS = {"user": "Пользователь", "assistant": "Anchor"}


# --- validation (pure) --------------------------------------------------


def _clean_note(value) -> str | None:
    """A non-empty note within NOTE_MAX, or None. Never truncates.

    Mirrors app/core/extract.py's `_clean_text` and its reasoning: a
    note over the limit means the model ignored its instructions, and
    half of a reason is not a better reason to interrupt someone.
    Dropping also means `ck_outbound_tick_note_length` can never be hit
    at runtime.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > NOTE_MAX:
        return None
    return text


def validate(payload: dict | None) -> tuple[bool, str | None]:
    """`(send, note)` from a parsed reply. Any doubt is a no.

    One return shape for every failure -- unparseable, wrong types, a
    truthy-but-not-boolean `send`, a blank note, an over-long note, a
    note carrying something that must never be stored -- so the caller
    has exactly one path and cannot accidentally act on half a result.

    **`send: true` with a blank note is a no.** The tick's whole
    premise is a natural reason; "yes, but I cannot say why" is not
    one, and the hidden flag would render «Повод: «».»
    """
    if not isinstance(payload, dict):
        return False, None

    send = payload.get("send")
    # `is not True` rather than falsiness: a model that returns the
    # string "true" or the number 1 has not answered the question.
    if send is not True:
        return False, None

    note = _clean_note(payload.get("note"))
    if note is None:
        return False, None

    # The note is stored and later re-injected into a prompt, so it
    # gets the same redaction every memory and proposal gets (phase-2
    # plan section 8).
    if not redact.is_safe_to_store(note):
        logger.info("tick note rejected by redaction")
        return False, None

    return True, note


# --- the model's input --------------------------------------------------


async def _recent_journal(session: AsyncSession, limit: int) -> list[str]:
    """The last `limit` journal lines, oldest first.

    Private: the tick is the only consumer today. See the amended
    docstring on db.models.Journal for why the journal reaches a prompt
    at all now.
    """
    result = await session.execute(
        select(Journal.text).order_by(Journal.id.desc()).limit(limit)
    )
    lines = list(result.scalars().all())
    lines.reverse()
    return lines


def _hours_since(now: datetime.datetime, moment: datetime.datetime | None) -> str:
    if moment is None:
        return "никогда"
    hours = (now - moment).total_seconds() / 3600
    return f"{hours:.0f} ч назад"


def build_input(
    *,
    now_block: str,
    hours_since_user: str,
    transcript: list,
    journal: list[str],
    due_action: str | None,
    due_set_at_ago: str | None,
) -> str:
    """Plan section 8 step 2's input, as one user message.

    The "now" block already carries the due action and when it was set,
    but section 8 lists them separately and they are the single most
    load-bearing fact for this decision -- a deadline today is the
    clearest legitimate reason to write. Repeating them costs a few
    tokens and removes any chance of the model skimming past them.
    """
    parts = [now_block, "", f"Пользователь писал: {hours_since_user}"]

    if due_action:
        when = f" (задано {due_set_at_ago})" if due_set_at_ago else ""
        parts.append(f"Главное действие: «{due_action}»{when}")
    else:
        parts.append("Главное действие: нет")

    if transcript:
        parts.append("")
        parts.append("## Последние реплики")
        parts.extend(
            f"{_ROLE_LABELS.get(row.role, row.role)}: {row.content}"
            for row in transcript
        )

    if journal:
        parts.append("")
        parts.append("## Дневник")
        parts.extend(f"- {line}" for line in journal)

    return "\n".join(parts)


# --- the job ------------------------------------------------------------


async def run_tick_decide(
    session: AsyncSession,
    settings: Settings,
    provider: LLMProvider,
    *,
    clock: Clock,
    local_date: datetime.date,
    hour: int,
) -> int | None:
    """The `tick_decide` job body. Returns the outbound id, or None.

    `local_date` and `hour` come from the job payload rather than from
    the clock, so the row's `bucket` matches the dedup key that
    reserved this decision even when the queue runs a minute late.
    """
    state = await get_state(session)
    now = clock.now_utc()

    # 1. The gate, before anything is spent (plan section 8 step 1).
    gate_state, counts, facts = await load_gate_inputs(
        session, clock, settings, state, kind=TICK
    )
    verdict = gate(TICK, gate_state, now, counts, facts, config_from_settings(settings))
    if not verdict.allowed:
        logger.info("tick not considered", extra={"reason": verdict.reason})
        return None

    # 2. Ask the safety model (H2: strict JSON, not prose).
    transcript = await recent_transcript(session, CONTEXT_MESSAGES)
    journal = await _recent_journal(session, JOURNAL_LINES)
    # P2: a snapshot read only, same discipline as app/core/turn.py --
    # the tick decision must never itself wait on the planner.
    planner_lines: list[str] = []
    if settings.PLANNER_ENABLED:
        snap = await planner_snapshot.get_snapshot(session)
        planner_lines = planner_snapshot.render_lines(
            snap, clock, state.timezone, max_age_min=settings.PLANNER_SNAPSHOT_MAX_AGE_MIN
        )
    now_block = build_now_block(
        clock=clock,
        timezone=state.timezone,
        intensity=state.intensity,
        focus_on=state.focus_on,
        due_action=state.due_action,
        due_set_at=state.due_set_at,
        streak=state.streak,
        last_checkin_at=state.last_checkin_at,
        planner=planner_lines,
    )
    user_content = build_input(
        now_block=now_block,
        hours_since_user=_hours_since(now, state.last_user_msg_at),
        transcript=transcript,
        journal=journal,
        due_action=state.due_action,
        due_set_at_ago=_hours_since(now, state.due_set_at)
        if state.due_set_at
        else None,
    )

    response = await provider.complete(
        [
            LLMMessage(role="system", content=DECISION_PROMPT),
            LLMMessage(role="user", content=user_content),
        ],
        conversation_id=f"anchor-tick-{local_date.isoformat()}-{hour}",
        json_schema=TICK_SCHEMA,
    )

    # 3. Ledger before parsing: the call was billed whatever came back.
    cost = priced(response.usage, settings, model=response.model)
    usd_cost = cost.usd
    session.add(
        SpendLedger(
            local_date=clock_module.local_date(clock, state.timezone),
            category=TICK_CATEGORY,
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
    send, note = validate(payload)
    # H2: same record as the extractor. `send=False` from well-formed
    # JSON is a decision, not a failure -- only unparseable output is.
    await safety_events.record_in(
        session,
        clock=clock,
        timezone=state.timezone,
        kind=safety_events.TICK,
        outcome=safety_events.PARSE_FAIL if payload is None else "ok",
        model=response.model,
    )
    await session.commit()
    if not send:
        logger.info("tick decided against", extra={"usd_cost": str(usd_cost)})
        return None

    # 4. Propose. The send-time gate still has the last word.
    outbound_id = await plan(
        session,
        settings,
        clock,
        TICK,
        local_date=local_date,
        planned_for=pick_send_time(TICK, settings, clock, state.timezone),
        bucket=hour,
        tick_note=note,
    )
    logger.info(
        "tick decided to write",
        extra={"outbound_id": outbound_id, "usd_cost": str(usd_cost)},
    )
    return outbound_id
