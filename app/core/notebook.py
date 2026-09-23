"""Anchor's own working notes: intentions, observations and open threads
(phase-5 plan sections 3 and 6; milestone 5b of the implementation plan).

**What this is, in one sentence.** After each scene closes, a safety-
model call looks back at it and may add an observation or an open
thread, close a thread that resolved, or tighten the wording of one of
its own earlier notes -- and the user sees every active entry through
`/mind` and can close *any* of them, including Anchor's.

**The one rule this whole module exists to keep**: Anchor can never
close or edit a `user`- or `review`-sourced entry, and reflection never
writes an `intention` at all -- only the user (`add_user_intention`
here) and, from milestone 5d, the weekly review may. That is enforced
twice, on purpose: `validate()` drops any `close`/`update` id that is
not an active `source='anchor'` entry before the model's output is even
looked at again, and `_close()` re-checks the same thing at the point
of the actual write, so a bug in `validate()` cannot turn into a write
on its own.

**Enqueue point.** `run_summarize_scene` (app/core/scene.py) enqueues
`NOTEBOOK_REFLECT` right after it writes the summary -- including on
its idempotent early-return path, where the dedup key collapses the
repeat -- because reflection needs the summary as input. It is not
enqueued from `ensure_open_scene`: that function only knows a scene
closed, not what it said.

**Welfare.** The model input is built from `scene.summarizable_messages()`
-- the same `ooc=false` + `SUMMARIZABLE_KINDS` double filter
`run_summarize_scene` uses -- and on top of that, reflection is skipped
entirely (no model call at all) when the scene contains *any*
`kind='welfare'` message, even one outside that filtered set. Belt and
braces, same reasoning as app/core/scene.py's own docstring: a welfare
trigger's neighbouring turns can carry its content by implication, and
the rule is that welfare content never reaches the notebook, full stop.

**One shared screen.** `app/core/screen.py` runs injection, redaction
and the risk rules, in that order, on every piece of text this module
is about to store -- the reflection job's `add`/`update` texts and the
user's own `/mind add` text alike. See that module's docstring for why
it lives there rather than being re-derived here.

**Similarity** is `func.similarity(NotebookEntry.text, text) > 0.6`
against active entries, the same expression and threshold as
`app.core.memory.near_duplicate` -- the table is small enough that no
trigram index is worth adding for it.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
from typing import Literal

from sqlalchemy import func, select, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core import clock as clock_module
from app.core import orders as orders_module
from app.core import safety_events
from app.core.clock import Clock
from app.core.extract import parse_json
from app.core.scene import (
    MIN_MESSAGES_FOR_SUMMARY,
    Deferred,
    render_dialogue,
    summarizable_messages,
)
from app.core.screen import screen
from app.core.spend import check_cap, priced
from app.db.models import Message, NotebookEntry, Scene, SpendLedger, UserState
from app.llm.provider import JSONSchema, LLMMessage, LLMProvider

logger = logging.getLogger(__name__)

NOTEBOOK_REFLECT = "notebook_reflect"
REFLECT_CATEGORY = "reflect"
NOTEBOOK_EXPIRY = "notebook_expiry"

INTENTION = "intention"
OBSERVATION = "observation"
OPEN_THREAD = "open_thread"
KINDS = (INTENTION, OBSERVATION, OPEN_THREAD)
# What `notebook_reflect` may itself add. Plan section 6: "Intentions
# are written only by the weekly review (5d) or by the user."
REFLECT_ADD_KINDS = (OBSERVATION, OPEN_THREAD)
SOURCES = ("anchor", "user", "review")

RESOLVED = "resolved"
STALE = "stale"
CLOSE_REASONS = (RESOLVED, STALE)

TEXT_MAX = 240
ADD_MAX = 3
CLOSE_MAX = 4
UPDATE_MAX = 2

SIMILARITY_MAX = 0.6  # same expression and cutoff as memory.near_duplicate

REFLECT_SCHEMA = JSONSchema(
    name="anchor_notebook_reflect",
    strict=True,
    schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["add", "close", "update"],
        "properties": {
            "add": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "text"],
                    "properties": {
                        "kind": {"type": "string", "enum": ["observation", "open_thread"]},
                        "text": {"type": "string"},
                    },
                },
            },
            "close": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "why"],
                    "properties": {
                        "id": {"type": "integer"},
                        "why": {"type": "string", "enum": list(CLOSE_REASONS)},
                    },
                },
            },
            "update": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "text"],
                    "properties": {
                        "id": {"type": "integer"},
                        "text": {"type": "string"},
                    },
                },
            },
        },
    },
)

# Plan section 6, verbatim.
REFLECT_PROMPT = (
    "Ты ведёшь рабочие заметки Anchor о пользователе. По этой сессии: добавь "
    "наблюдения (устойчивые закономерности в поведении, которые пользователь сам "
    "проявил) и незакрытые темы (что он обещал, начал или о чём стоит спросить "
    "позже). Закрой темы, которые решены. Пиши по-русски, коротко, фактами.\n"
    "Запрещено: диагнозы, психологические ярлыки и типы личности, здоровье, "
    "кризисы, догадки о мотивах, заметки об ужесточении, наказаниях или "
    "повышении интенсивности, подробности о третьих лицах."
)


@dataclasses.dataclass(frozen=True)
class Plan:
    """Validated, ready-to-apply output of one `notebook_reflect` call."""

    add: list[dict] = dataclasses.field(default_factory=list)
    close: list[dict] = dataclasses.field(default_factory=list)
    update: list[dict] = dataclasses.field(default_factory=list)


def _clean_text(value, limit: int = TEXT_MAX) -> str | None:
    """A non-empty string within `limit`, or None. Never truncates."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > limit:
        return None
    return text


def validate(payload: dict, *, entries: dict[int, str]) -> Plan:
    """Re-check every field of the model's output, trusting nothing.

    `entries` maps the id of every currently *active* entry to its
    `source` -- the full set, not only Anchor's, so a `close`/`update`
    id can be told apart three ways: not active at all (missing from
    the map), active but not Anchor's (present, source != 'anchor'), or
    a legitimate target (present, source == 'anchor'). Only the third
    survives.

    Every text -- `add` and `update` alike -- goes through `screen()`.
    Similarity is deliberately **not** checked here: it needs a live
    query against the database, so the caller (`run_notebook_reflect`)
    checks it while applying the plan, against entries that may have
    changed since this function ran.
    """
    add: list[dict] = []
    for item in payload.get("add") or []:
        if len(add) >= ADD_MAX:
            break
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        # Plan section 6: reflection never adds an intention. A model
        # that emits one anyway has that one item dropped, not the
        # whole payload -- the same "drop, never trust" posture as
        # every other field here.
        if kind not in REFLECT_ADD_KINDS:
            continue
        text = _clean_text(item.get("text"))
        if text is None:
            continue
        result = screen(text)
        if not result.ok:
            continue
        add.append({"kind": kind, "text": text})

    close: list[dict] = []
    for item in payload.get("close") or []:
        if len(close) >= CLOSE_MAX:
            break
        if not isinstance(item, dict):
            continue
        entry_id = item.get("id")
        why = item.get("why")
        if not isinstance(entry_id, int) or isinstance(entry_id, bool):
            continue
        if why not in CLOSE_REASONS:
            continue
        if entries.get(entry_id) != "anchor":
            continue
        close.append({"id": entry_id, "why": why})

    update: list[dict] = []
    for item in payload.get("update") or []:
        if len(update) >= UPDATE_MAX:
            break
        if not isinstance(item, dict):
            continue
        entry_id = item.get("id")
        text = _clean_text(item.get("text"))
        if not isinstance(entry_id, int) or isinstance(entry_id, bool) or text is None:
            continue
        if entries.get(entry_id) != "anchor":
            continue
        result = screen(text)
        if not result.ok:
            continue
        update.append({"id": entry_id, "text": text})

    return Plan(add=add, close=close, update=update)


# --- reading the notebook (used by /mind, persona_context and reflect) ----


@dataclasses.dataclass(frozen=True)
class NotebookView:
    """Active entries, grouped by kind. Each item is (id, text, source)."""

    intentions: list[tuple[int, str, str]] = dataclasses.field(default_factory=list)
    observations: list[tuple[int, str, str]] = dataclasses.field(default_factory=list)
    threads: list[tuple[int, str, str]] = dataclasses.field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.intentions or self.observations or self.threads)


async def active_entries(session: AsyncSession) -> NotebookView:
    """All active entries, oldest first within each kind. Read-only."""
    result = await session.execute(
        select(NotebookEntry)
        .where(NotebookEntry.active.is_(True))
        .order_by(NotebookEntry.kind, NotebookEntry.created_at, NotebookEntry.id)
    )
    view = NotebookView()
    by_kind = {INTENTION: view.intentions, OBSERVATION: view.observations, OPEN_THREAD: view.threads}
    for row in result.scalars().all():
        bucket = by_kind.get(row.kind)
        if bucket is not None:
            bucket.append((row.id, row.text, row.source))
    return view


def _flatten(view: NotebookView) -> list[tuple[int, str, str]]:
    return [*view.intentions, *view.observations, *view.threads]


def build_input(
    *,
    dialogue: str,
    summary: str | None,
    view: NotebookView,
    due_action: str | None,
    orders: list[str] | None = None,
) -> str:
    """The user-role message `run_notebook_reflect` sends.

    Entry ids are shown here **and only here** -- exactly the same
    reasoning as app/core/extract.py's memory ids: `close`/`update`
    needs them, and nothing else in the codebase ever puts a notebook
    id in front of a model. Non-Anchor entries are marked `(user)` /
    `(review)` so the model can tell what it is not allowed to touch,
    but the marking is a hint, not the boundary -- `validate()` is.

    5c: `orders` (plain text, no ids -- there is nothing here for the
    model to reference by id) lists the active standing orders after
    the due action, matching plan section 6's input list, "the due
    action and active standing orders".
    """
    lines = ["## Сессия", dialogue]
    if summary:
        lines.append("")
        lines.append("## Итог сессии")
        lines.append(summary)
    lines.append("")
    lines.append("## Твои текущие заметки (id — текст)")
    entries = _flatten(view)
    if entries:
        for entry_id, text, source in entries:
            marker = "" if source == "anchor" else f" ({source})"
            lines.append(f"{entry_id} — {text}{marker}")
    else:
        lines.append("(пока пусто)")
    lines.append("")
    lines.append(f"Главное действие: {due_action or 'нет'}")
    if orders:
        lines.append("Договорённости: " + "; ".join(orders))
    return "\n".join(lines)


# --- the reflect job -------------------------------------------------------


async def _has_welfare_message(session: AsyncSession, scene_id: int) -> bool:
    """Any kind='welfare' row in this scene, regardless of `ooc` -- see
    the module docstring on why this is a second filter, not a
    substitute for `summarizable_messages`'s own."""
    result = await session.execute(
        select(Message.id).where(Message.scene_id == scene_id).where(Message.kind == "welfare").limit(1)
    )
    return result.first() is not None


async def _already_reflected(session: AsyncSession, scene_id: int) -> bool:
    result = await session.execute(
        select(NotebookEntry.id).where(NotebookEntry.scene_id == scene_id).limit(1)
    )
    return result.first() is not None


async def _near_duplicate(
    session: AsyncSession, text: str, *, ignore_id: int | None = None
) -> bool:
    score = func.similarity(NotebookEntry.text, text).label("score")
    stmt = (
        select(NotebookEntry.id)
        .where(NotebookEntry.active.is_(True))
        .where(score > SIMILARITY_MAX)
        .limit(1)
    )
    if ignore_id is not None:
        stmt = stmt.where(NotebookEntry.id != ignore_id)
    result = await session.execute(stmt)
    return result.first() is not None


async def _count_active(session: AsyncSession, kind: str) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(NotebookEntry)
        .where(NotebookEntry.active.is_(True))
        .where(NotebookEntry.kind == kind)
    )
    return result.scalar_one()


async def _close_oldest_anchor(session: AsyncSession, kind: str, *, clock: Clock) -> None:
    """Make room under a per-kind cap by closing Anchor's own oldest entry.

    Never a user or review entry -- this only ever selects
    `source='anchor'` rows, which is what makes 5b's caps apply to
    Anchor's own output without ever touching something the user or the
    review wrote.
    """
    result = await session.execute(
        select(NotebookEntry)
        .where(NotebookEntry.active.is_(True))
        .where(NotebookEntry.kind == kind)
        .where(NotebookEntry.source == "anchor")
        .order_by(NotebookEntry.created_at, NotebookEntry.id)
        .limit(1)
    )
    oldest = result.scalars().first()
    if oldest is not None:
        _close(oldest, by="anchor", clock=clock)


def _close(entry: NotebookEntry, *, by: str, clock: Clock) -> bool:
    """Mutate `entry` closed, or refuse. No commit -- the caller does.

    The ownership rule, in one place: `by='anchor'` may only ever close
    a `source='anchor'` entry; `by='user'` (and `by='expiry'`, though
    the expiry sweep does its closing with a bulk UPDATE instead) may
    close anything active. This is the second of the two checks the
    module docstring describes -- `validate()` is the first, for the
    reflect job's own payload; this one is what a `/mind` ✖ press and a
    reflect `close` item both actually run through.
    """
    if not entry.active:
        return False
    if by == "anchor" and entry.source != "anchor":
        return False
    entry.active = False
    entry.closed_by = by
    entry.closed_at = clock.now_utc()
    return True


async def run_notebook_reflect(
    session: AsyncSession,
    settings: Settings,
    provider: LLMProvider,
    *,
    clock: Clock,
    timezone: str,
    scene_id: int,
) -> None:
    """The `notebook_reflect` job body (plan section 6).

    Idempotent through `_already_reflected`: a scene that already has
    notebook rows is never re-reflected on, which is what makes the
    dedup key on the enqueue side (`nb:<scene_id>`) collapse a replayed
    job into a genuine no-op rather than a second, different set of
    notes for the same session.
    """
    scene = await session.get(Scene, scene_id)
    if scene is None or scene.ended_at is None:
        return

    messages = await summarizable_messages(session, scene_id)
    if len(messages) < MIN_MESSAGES_FOR_SUMMARY:
        return

    if await _has_welfare_message(session, scene_id):
        logger.info("notebook reflect skipped, welfare scene", extra={"scene_id": scene_id})
        return

    if await _already_reflected(session, scene_id):
        return

    if await check_cap(session, settings, clock, timezone):
        run_after = clock_module.next_local_midnight(clock, timezone)
        logger.info("notebook reflect deferred by cap", extra={"scene_id": scene_id})
        raise Deferred(run_after)

    view = await active_entries(session)
    entries_by_id = {
        entry_id: source for entry_id, _, source in _flatten(view)
    }
    state = (await session.execute(select(UserState).where(UserState.id == 1))).scalar_one_or_none()
    due_action = state.due_action if state is not None else None
    # 5c: active standing orders join the due action in the reflect
    # input (plan section 6). Texts only, oldest first -- the same
    # "no ids beyond the notebook's own" rule build_input's docstring
    # already states.
    order_rows = await orders_module.active_orders(session)
    order_lines = [
        f"«{row.text}» ({orders_module.cadence_label(row.cadence, row.weekday)})"
        for row in order_rows
    ]

    response = await provider.complete(
        [
            LLMMessage(role="system", content=REFLECT_PROMPT),
            LLMMessage(
                role="user",
                content=build_input(
                    dialogue=render_dialogue(messages),
                    summary=scene.summary,
                    view=view,
                    due_action=due_action,
                    orders=order_lines,
                ),
            ),
        ],
        conversation_id=f"anchor-nb-{scene_id}",
        json_schema=REFLECT_SCHEMA,
    )

    cost = priced(response.usage, settings, model=response.model)
    session.add(
        SpendLedger(
            local_date=clock_module.local_date(clock, timezone),
            category=REFLECT_CATEGORY,
            model=response.model,
            tokens_in=response.usage.input_tokens,
            tokens_cached=response.usage.cached_tokens,
            tokens_out=response.usage.output_tokens,
            usd_cost=cost.usd,
            cost_source=cost.source,
        )
    )
    await session.commit()

    payload = parse_json(response.text)
    await safety_events.record_in(
        session,
        clock=clock,
        timezone=timezone,
        kind=safety_events.NOTEBOOK,
        outcome=safety_events.PARSE_FAIL if payload is None else "ok",
        model=response.model,
    )
    await session.commit()
    if payload is None:
        logger.warning("notebook reflect returned unparseable output", extra={"scene_id": scene_id})
        return

    plan = validate(payload, entries=entries_by_id)

    # Every item the model proposed that did not survive validate() is
    # already a drop, before apply() gets a chance to drop any more
    # (a near-duplicate add/update, or a close/update whose target
    # vanished between validate() and here).
    raw_add = payload.get("add") if isinstance(payload.get("add"), list) else []
    raw_close = payload.get("close") if isinstance(payload.get("close"), list) else []
    raw_update = payload.get("update") if isinstance(payload.get("update"), list) else []
    dropped = (
        (len(raw_add) - len(plan.add))
        + (len(raw_close) - len(plan.close))
        + (len(raw_update) - len(plan.update))
    )
    added = closed = updated = 0

    for item in plan.close:
        entry = await session.get(NotebookEntry, item["id"])
        if entry is not None and _close(entry, by="anchor", clock=clock):
            closed += 1
        else:
            dropped += 1

    for item in plan.update:
        entry = await session.get(NotebookEntry, item["id"])
        if entry is None or not entry.active or entry.source != "anchor":
            dropped += 1
            continue
        if await _near_duplicate(session, item["text"], ignore_id=entry.id):
            dropped += 1
            continue
        entry.text = item["text"]
        entry.updated_at = clock.now_utc()
        updated += 1

    caps = {OBSERVATION: settings.NOTEBOOK_MAX_OBSERVATIONS, OPEN_THREAD: settings.NOTEBOOK_MAX_THREADS}
    for item in plan.add:
        if await _near_duplicate(session, item["text"]):
            dropped += 1
            continue
        kind = item["kind"]
        if await _count_active(session, kind) >= caps[kind]:
            await _close_oldest_anchor(session, kind, clock=clock)
        session.add(
            NotebookEntry(
                kind=kind,
                text=item["text"],
                source="anchor",
                scene_id=scene_id,
            )
        )
        added += 1

    await session.commit()
    logger.info(
        "notebook reflected",
        extra={"scene_id": scene_id, "added": added, "closed": closed, "updated": updated, "dropped": dropped},
    )


async def run_notebook_expiry(session: AsyncSession, settings: Settings, *, clock: Clock) -> None:
    """Daily sweep: close `open_thread` entries older than the TTL.

    Only `open_thread` -- intentions and observations have no TTL (plan
    section 6). A bulk UPDATE, not `_close()` per row: there is no
    ownership question here (`closed_by='expiry'` applies to any active
    thread regardless of who opened it), so nothing needs the per-row
    check that function exists for.
    """
    cutoff = clock.now_utc() - datetime.timedelta(days=settings.NOTEBOOK_THREAD_TTL_DAYS)
    now = clock.now_utc()
    result = await session.execute(
        sql_update(NotebookEntry)
        .where(NotebookEntry.active.is_(True))
        .where(NotebookEntry.kind == OPEN_THREAD)
        .where(NotebookEntry.created_at < cutoff)
        .values(active=False, closed_by="expiry", closed_at=now)
        .returning(NotebookEntry.id)
    )
    closed_ids = result.fetchall()
    await session.commit()
    if closed_ids:
        logger.info("notebook threads expired", extra={"closed": len(closed_ids)})


# --- the user-facing writes (/mind) -----------------------------------------

AddResult = Literal["ok", "refused", "cap", "too_long", "duplicate"]


async def add_user_intention(
    session: AsyncSession, settings: Settings, text: str, *, clock: Clock
) -> AddResult:
    """`/mind add <текст>` (plan section 6's last bullet).

    A `risk_intensity` hit is deliberately **allowed** here -- the
    implementation plan's design decision: the plan refuses only
    high-risk text, an injection hit, or something unsafe to store;
    "быть строже к себе" as the user's own intention is their call, not
    Anchor's to refuse.
    """
    cleaned = text.strip()
    if len(cleaned) > TEXT_MAX:
        return "too_long"

    result = screen(cleaned)
    if not result.ok and result.reason != "risk_intensity":
        return "refused"

    if await _count_active(session, INTENTION) >= settings.NOTEBOOK_MAX_INTENTIONS:
        return "cap"

    if await _near_duplicate(session, cleaned):
        return "duplicate"

    session.add(NotebookEntry(kind=INTENTION, text=cleaned, source="user"))
    await session.commit()
    logger.info("notebook intention added", extra={"kind": INTENTION})
    return "ok"


async def replace_review_intentions(
    session: AsyncSession, settings: Settings, texts: list[str], *, clock: Clock
) -> None:
    """The weekly review's own intentions writer (phase-5 plan section 8;
    milestone 5d's implementation plan §"Intentions").

    Closes every active `source='review'` intention (`closed_by=
    'anchor'` -- the record of *who* closed it, not a claim that the
    reflection job did; that job is structurally barred from touching a
    review-sourced entry, see `_close()`'s own docstring, so this
    function sets the fields directly rather than going through it),
    then inserts the new ones as `source='review'`. This -- and
    `add_user_intention` -- are the **only** two writers of
    `kind='intention'` in the codebase: "Only the weekly review and the
    user write intentions" (module docstring above).

    Deduped against active entries by the same trigram-similarity rule
    every other write here uses, and capped by `NOTEBOOK_MAX_INTENTIONS`
    the same way `add_user_intention` is -- a review intention that
    does not fit is simply dropped (oldest-offered-first, since the
    model's own ordering is trusted for priority), never by closing the
    user's own intentions to make room. Callers (app/core/review.py) are
    expected to hand this already-screened text (`validate()`'s own
    `screen()` pass); this function re-checks only the length, which is
    this table's own constraint, not the risk rules.
    """
    result = await session.execute(
        select(NotebookEntry)
        .where(NotebookEntry.active.is_(True))
        .where(NotebookEntry.kind == INTENTION)
        .where(NotebookEntry.source == "review")
    )
    now = clock.now_utc()
    for entry in result.scalars().all():
        entry.active = False
        entry.closed_by = "anchor"
        entry.closed_at = now

    added = 0
    for text in texts:
        cleaned = text.strip()
        if not cleaned or len(cleaned) > TEXT_MAX:
            continue
        if await _count_active(session, INTENTION) >= settings.NOTEBOOK_MAX_INTENTIONS:
            break
        if await _near_duplicate(session, cleaned):
            continue
        session.add(NotebookEntry(kind=INTENTION, text=cleaned, source="review"))
        added += 1

    await session.commit()
    logger.info("review intentions replaced", extra={"added": added})


async def close_entry(session: AsyncSession, entry_id: int, *, by: str, clock: Clock) -> bool:
    """Close one entry. True only if it was active and `by` may close it.

    The user path (`by='user'`) accepts any source -- plan section 6:
    "The user can close any entry." -- which is what `/mind`'s ✖ button
    relies on.
    """
    entry = await session.get(NotebookEntry, entry_id)
    if entry is None:
        return False
    if not _close(entry, by=by, clock=clock):
        return False
    await session.commit()
    logger.info("notebook entry closed", extra={"closed": 1})
    return True


__all__ = [
    "ADD_MAX",
    "CLOSE_MAX",
    "CLOSE_REASONS",
    "INTENTION",
    "KINDS",
    "NOTEBOOK_EXPIRY",
    "NOTEBOOK_REFLECT",
    "NotebookView",
    "OBSERVATION",
    "OPEN_THREAD",
    "Plan",
    "REFLECT_ADD_KINDS",
    "REFLECT_CATEGORY",
    "REFLECT_PROMPT",
    "REFLECT_SCHEMA",
    "RESOLVED",
    "SIMILARITY_MAX",
    "SOURCES",
    "STALE",
    "TEXT_MAX",
    "UPDATE_MAX",
    "active_entries",
    "add_user_intention",
    "build_input",
    "close_entry",
    "replace_review_intentions",
    "run_notebook_expiry",
    "run_notebook_reflect",
    "validate",
]
