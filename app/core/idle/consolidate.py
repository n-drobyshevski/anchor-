"""The `consolidate` idle kind (Phase 6 plan section 6.2; milestone 6b).

**Input.** Clusters of active (`superseded_by IS NULL`), unpinned,
`source='extractor'` memories of kind `identity`, `preference` or
`event`, whose pairwise `similarity()` exceeds `SIMILARITY_MIN`. Never
user/adopt, pinned, rule or technique memories -- enforced twice, the
same "twice, on purpose" posture app/core/notebook.py's own docstring
describes: `find_clusters` never selects them into the candidate pool
in the first place, and `_reload_unprotected` re-checks the same
predicate at apply time, right before any write, so a race between
candidate-building and apply (a `/forget`, a `/pin`, a correction that
superseded a candidate) can only ever drop an operation, never write
through a protected row.

**Output.** `{"merges": [{"ids": [...], "text": "...", "kind": "..."}],
"contradictions": [{"keep_id": ..., "drop_id": ...}]}` -- `CONSOLIDATE_
SCHEMA` below, strict JSON via `LLM_MODEL_SAFETY`. `validate()` is a
pure function (no session), mirroring `app/core/notebook.py`'s own
`validate()`: every id must be in the candidate set, a merge needs
`>= 2` ids, a contradiction needs `keep_id != drop_id`, merge text must
be `<= 300` chars and pass `screen()` -- **including** an `intensity`
hit, unlike `app/core/notebook.add_user_intention`'s carve-out, because
this text is never the user's own words (plan section 6.2's code rules
list "the risk rules, injection scan and redaction on merged text" with
no exception) -- and **an id may be used at most once across the whole
payload**: the second operation naming an id already used by an earlier
one (in payload order) is dropped, not the first.

**Apply.** A merge inserts `Memory(source='consolidate')` and supersedes
every id it merged; a contradiction supersedes `drop_id` with `keep_id`
(`keep_id` is never written to -- only referenced -- but it must still
be a live, unprotected candidate, per the coordinator's resolution: "an
eligible, unprotected extractor memory", re-checked at apply time same
as `drop_id`). One transaction, opened by the caller (`runner.py`)
after the model call and after the in-job preemption check, matching
6a's `RunContext` and the plan's single-transaction requirement for
6b's kinds.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.config import Settings
from app.core import safety_events
from app.core.clock import Clock
from app.core.extract import parse_json
from app.core.idle.rowstate import row_state as _row_state
from app.core.screen import screen
from app.db.models import Memory
from app.llm.provider import JSONSchema, LLMMessage, LLMProvider

logger = logging.getLogger(__name__)

CONSOLIDATE_CATEGORY = "idle:consolidate"

# Tuning constants, deliberately not Settings -- same posture as
# app/core/memory.py's DEDUPE_MAX_SIMILARITY: a deploy must not be able
# to set the clustering threshold to 0 and start merging unrelated
# facts.
SIMILARITY_MIN = 0.45
MAX_CLUSTERS = 5
MAX_CLUSTER_MEMBERS = 6

# The only kinds and sources `find_clusters` ever selects, and the only
# ones the apply-time re-check ever accepts -- plan section 6.2: "never
# touch memories with source=user/adopt, pinned memories, rules, or
# techniques".
CONSOLIDATABLE_KINDS = ("identity", "preference", "event")
CANDIDATE_SOURCE = "extractor"

TEXT_MAX = 300

CONSOLIDATE_SCHEMA = JSONSchema(
    name="anchor_consolidate",
    strict=True,
    schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["merges", "contradictions"],
        "properties": {
            "merges": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["ids", "text", "kind"],
                    "properties": {
                        "ids": {"type": "array", "items": {"type": "integer"}},
                        "text": {"type": "string"},
                        "kind": {"type": "string", "enum": list(CONSOLIDATABLE_KINDS)},
                    },
                },
            },
            "contradictions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["keep_id", "drop_id"],
                    "properties": {
                        "keep_id": {"type": "integer"},
                        "drop_id": {"type": "integer"},
                    },
                },
            },
        },
    },
)

CONSOLIDATE_PROMPT = (
    "Тебе даны кластеры похожих фактов о пользователе (id, вид, текст). В каждом "
    "кластере одни и те же факты могли быть записаны по-разному, а могли "
    "противоречить друг другу. Объедини явные дубликаты/близкие формулировки в "
    "merges: {ids, text, kind} -- text короткий, фактами, без диагнозов и "
    "домыслов. Если два факта прямо противоречат друг другу, укажи "
    "contradictions: {keep_id, drop_id} -- keep_id тот, что вернее по контексту. "
    "Не трогай то, в чём нет явного дубликата или противоречия. Пиши по-русски."
)


@dataclasses.dataclass(frozen=True)
class ConsolidatePlan:
    """Validated, ready-to-apply output of one `consolidate` call."""

    merges: list[dict] = dataclasses.field(default_factory=list)
    contradictions: list[dict] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True)
class ConsolidateResult:
    merged: int
    contradicted: int
    dropped: int
    preempted: bool = False


def _clean_text(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > TEXT_MAX:
        return None
    return text


def validate(payload: dict, *, candidate_ids: set[int]) -> ConsolidatePlan:
    """Re-check every field of the model's output, trusting nothing --
    same "drop the offending item, not the whole payload" posture as
    `app/core/notebook.validate`. `candidate_ids` is the exact set
    `find_clusters` put in front of the model this run; nothing outside
    it may ever be named.

    IDs are tracked as used **across both merges and contradictions**,
    in payload order: the first operation to name an id wins, and a
    later operation naming an id already used by an earlier one is
    dropped whole (plan section 6.2's code rule: "an id used at most
    once across all operations").
    """
    used: set[int] = set()
    merges: list[dict] = []
    for item in payload.get("merges") or []:
        if len(merges) >= MAX_CLUSTERS:
            break
        if not isinstance(item, dict):
            continue
        raw_ids = item.get("ids")
        kind = item.get("kind")
        text = _clean_text(item.get("text"))
        if not isinstance(raw_ids, list) or kind not in CONSOLIDATABLE_KINDS or text is None:
            continue
        ids: list[int] = []
        seen: set[int] = set()
        for raw_id in raw_ids:
            if not isinstance(raw_id, int) or isinstance(raw_id, bool):
                continue
            if raw_id not in candidate_ids or raw_id in seen:
                continue
            seen.add(raw_id)
            ids.append(raw_id)
        if len(ids) < 2:
            continue
        if any(i in used for i in ids):
            continue
        if not screen(text).ok:
            continue
        used.update(ids)
        merges.append({"ids": ids, "text": text, "kind": kind})

    contradictions: list[dict] = []
    for item in payload.get("contradictions") or []:
        if not isinstance(item, dict):
            continue
        keep_id = item.get("keep_id")
        drop_id = item.get("drop_id")
        if not isinstance(keep_id, int) or isinstance(keep_id, bool):
            continue
        if not isinstance(drop_id, int) or isinstance(drop_id, bool):
            continue
        if keep_id == drop_id:
            continue
        if keep_id not in candidate_ids or drop_id not in candidate_ids:
            continue
        if keep_id in used or drop_id in used:
            continue
        used.add(keep_id)
        used.add(drop_id)
        contradictions.append({"keep_id": keep_id, "drop_id": drop_id})

    return ConsolidatePlan(merges=merges, contradictions=contradictions)


# --- building the candidate pool ------------------------------------------


def _candidate_query():
    return (
        select(Memory)
        .where(Memory.superseded_by.is_(None))
        .where(Memory.pinned.is_(False))
        .where(Memory.source == CANDIDATE_SOURCE)
        .where(Memory.kind.in_(CONSOLIDATABLE_KINDS))
    )


async def find_clusters(session: AsyncSession) -> list[list[int]]:
    """Up to `MAX_CLUSTERS` clusters of up to `MAX_CLUSTER_MEMBERS`
    candidate memory ids each, by pairwise trigram similarity.

    A self-join (`Memory` against its own `aliased` copy, `a.id < b.id`
    to avoid symmetric duplicate pairs) collects the edges, then plain
    union-find over them builds connected components. Components under
    2 members are dropped (nothing to merge). The result is ordered
    deterministically -- by member count desc, then by the component's
    own lowest id -- so two runs over the same data always agree, and
    each surviving component is truncated to its `MAX_CLUSTER_MEMBERS`
    lowest ids.

    Shared with `app/core/idle/facts.py` (the gate's own candidate
    count) so the gate and the job can never disagree about whether
    there is anything to do -- the same role `app/core/idle/candidates.py`
    plays for backfill.
    """
    b = aliased(Memory)
    edges = await session.execute(
        select(Memory.id, b.id)
        .join(b, Memory.id < b.id)
        .where(Memory.superseded_by.is_(None), b.superseded_by.is_(None))
        .where(Memory.pinned.is_(False), b.pinned.is_(False))
        .where(Memory.source == CANDIDATE_SOURCE, b.source == CANDIDATE_SOURCE)
        .where(Memory.kind.in_(CONSOLIDATABLE_KINDS), b.kind.in_(CONSOLIDATABLE_KINDS))
        .where(func.similarity(Memory.text, b.text) > SIMILARITY_MIN)
    )
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for left, right in edges.all():
        union(left, right)

    groups: dict[int, list[int]] = {}
    for node in parent:
        groups.setdefault(find(node), []).append(node)

    components = [sorted(members) for members in groups.values() if len(members) >= 2]
    components.sort(key=lambda members: (-len(members), members[0]))
    return [members[:MAX_CLUSTER_MEMBERS] for members in components[:MAX_CLUSTERS]]


def build_consolidate_input(clusters: list[list[Memory]]) -> str:
    """The user-role message. Ids shown here and only here, same rule
    as app/core/notebook.build_input's own docstring."""
    lines = []
    for i, cluster in enumerate(clusters, start=1):
        lines.append(f"## Кластер {i}")
        for memory in cluster:
            lines.append(f"{memory.id} — {memory.kind} — {memory.text}")
        lines.append("")
    return "\n".join(lines).strip()


async def _reload_unprotected(session: AsyncSession, memory_id: int) -> Memory | None:
    """Re-fetch `memory_id` and return it only if it is still exactly
    the shape `find_clusters` would have offered -- active, unpinned,
    `source='extractor'`, a consolidatable kind. The apply-time half of
    the "never touch" rule's two enforcement points (module docstring)."""
    memory = await session.get(Memory, memory_id, with_for_update=True)
    if memory is None:
        return None
    if memory.superseded_by is not None or memory.pinned:
        return None
    if memory.source != CANDIDATE_SOURCE or memory.kind not in CONSOLIDATABLE_KINDS:
        return None
    return memory


async def apply_consolidate(session: AsyncSession, ctx: RunContext, plan: ConsolidatePlan) -> ConsolidateResult:
    """Apply a validated `ConsolidatePlan` inside `ctx`'s transaction."""
    merged = 0
    contradicted = 0
    dropped = 0

    for item in plan.merges:
        originals = []
        ok = True
        for memory_id in item["ids"]:
            memory = await _reload_unprotected(session, memory_id)
            if memory is None:
                ok = False
                break
            originals.append(memory)
        if not ok or len(originals) < 2:
            dropped += 1
            continue

        new_memory = Memory(kind=item["kind"], text=item["text"], source="consolidate")
        session.add(new_memory)
        await session.flush()
        await ctx.record_change("memory", new_memory.id, "insert", None, _row_state(new_memory))

        for original in originals:
            before = _row_state(original)
            original.superseded_by = new_memory.id
            await ctx.record_change("memory", original.id, "supersede", before, _row_state(original))
        merged += 1

    for item in plan.contradictions:
        keep = await _reload_unprotected(session, item["keep_id"])
        drop = await _reload_unprotected(session, item["drop_id"])
        if keep is None or drop is None:
            dropped += 1
            continue
        before = _row_state(drop)
        drop.superseded_by = keep.id
        await ctx.record_change("memory", drop.id, "supersede", before, _row_state(drop))
        contradicted += 1

    return ConsolidateResult(merged=merged, contradicted=contradicted, dropped=dropped)


async def run_consolidate(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    safety_provider: LLMProvider,
    clock: Clock,
    *,
    run_id: int,
    started_at: datetime.datetime,
    timezone: str,
) -> ConsolidateResult:
    """The `consolidate` idle kind body (plan section 6.2), called by
    `app/core/idle/runner.py`. Reads and the model call happen outside
    any write transaction; the apply step opens one transaction only
    after the model call returns and after the in-job preemption
    re-check below, matching the plan's single-transaction requirement.
    """
    # Imported lazily, not at module level: app/core/idle/runner.py
    # imports app/core/idle/facts.py, which imports this module (for
    # find_clusters) -- a module-level `from app.core.idle.runner
    # import ...` here would close that into a cycle. By the time this
    # function actually runs, runner.py is always fully loaded (it is
    # the only caller), the same "lazy import breaks the cycle" move
    # app/core/idle/backfill.py's own module docstring documents for
    # its own runner.py imports.
    from app.core.idle.runner import RunContext, is_preempted

    async with session_factory() as session:
        cluster_ids = await find_clusters(session)
        if not cluster_ids:
            return ConsolidateResult(merged=0, contradicted=0, dropped=0)
        clusters: list[list[Memory]] = []
        for ids in cluster_ids:
            result = await session.execute(select(Memory).where(Memory.id.in_(ids)))
            by_id = {m.id: m for m in result.scalars().all()}
            clusters.append([by_id[i] for i in ids if i in by_id])
        candidate_ids = {memory.id for cluster in clusters for memory in cluster}
        user_text = build_consolidate_input(clusters)

    response = await safety_provider.complete(
        [
            LLMMessage(role="system", content=CONSOLIDATE_PROMPT),
            LLMMessage(role="user", content=user_text),
        ],
        conversation_id=f"anchor-idle-consolidate-{run_id}",
        json_schema=CONSOLIDATE_SCHEMA,
    )

    async with session_factory() as session:
        ctx = RunContext(
            session=session, settings=settings, clock=clock, run_id=run_id,
            kind="consolidate", started_at=started_at, timezone=timezone,
        )
        await ctx.charge(response.usage, response.model)

        payload = parse_json(response.text)
        await safety_events.record_in(
            session, clock=clock, timezone=timezone, kind=safety_events.NOTEBOOK,
            outcome=safety_events.PARSE_FAIL if payload is None else "ok", model=response.model,
        )
        await session.commit()

        if payload is None:
            logger.warning("consolidate returned unparseable output", extra={"run_id": run_id})
            return ConsolidateResult(merged=0, contradicted=0, dropped=0)

        plan = validate(payload, candidate_ids=candidate_ids)

    async with session_factory() as session:
        if await is_preempted(session, clock, started_at):
            return ConsolidateResult(merged=0, contradicted=0, dropped=0, preempted=True)

        ctx = RunContext(
            session=session, settings=settings, clock=clock, run_id=run_id,
            kind="consolidate", started_at=started_at, timezone=timezone,
        )
        result = await apply_consolidate(session, ctx, plan)
        await session.commit()

    logger.info(
        "consolidate run done",
        extra={
            "run_id": run_id, "merged": result.merged,
            "contradicted": result.contradicted, "dropped": result.dropped,
        },
    )
    return result


__all__ = [
    "CONSOLIDATABLE_KINDS",
    "CONSOLIDATE_CATEGORY",
    "CONSOLIDATE_PROMPT",
    "CONSOLIDATE_SCHEMA",
    "MAX_CLUSTERS",
    "MAX_CLUSTER_MEMBERS",
    "SIMILARITY_MIN",
    "ConsolidatePlan",
    "ConsolidateResult",
    "apply_consolidate",
    "build_consolidate_input",
    "find_clusters",
    "run_consolidate",
    "validate",
]
