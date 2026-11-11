"""The `BOARD_SYNC` job: one pass of keeping the user's task boards and
the planner in step (app/planner/board_sync.py decides, this module
reads, writes and stores).

**Its own job kind, in the background.** The user chose a background
sync over an on-command one, knowing it relaxes "Echo writes to the
vault only on the user's tap" (docs/decisions.md, "Board sync"). So it is
a job of its own, never an idle job: app/worker.py enqueues it after a
successful `PLANNER_SYNC` (`maybe_enqueue`), at most once per 15 minutes,
only while `BOARD_SYNC_ENABLED` is on, the planner link is active and
`VAULT_MODE` is not `off`. Its one vault write goes through
app/core/echo_write.py's `write_board`; tests/test_echo_isolation.py
names this module as the one caller, the documented exception to the
tap rule, and tests/test_idle_isolation.py keeps idle away from it.

**One pass, in an order where a crash can only cause a harmless repeat:**

1. Read every mapped board with its subtasks (`GET /v1/boards?subs=1`),
   the user's own tasks once (`list_tasks` with no tag, subtasks and
   done ones included: a subtask carries no tag, so a board's subtasks
   are found as the descendants of its tagged top-level tasks), and the
   stored pairs and subtask pairs. Compute every board's plan, then the
   dependencies across all of them (board_sync's `merge_deps`, from the
   rows as the pass found them), which go last into each board's batch;
   a task adopted by one board is not offered to the next. A board read
   from an older vaultd (no subtasks in the answer), or a pass whose
   planner rows carry no `position` (an older planner), leaves subtasks
   and dependencies alone and syncs cards as before.
2. Per board: the planner creates, tasks first, then subtasks (each
   under a task that already had a pair; idempotent by
   `clientRequestId`, so a pass that dies after one and before the board
   write gets the same task back next time, or pairs it by title).
3. `POST /v1/boards` with the board's batch, under the pass's one fresh
   changeset (`boards_...`). A 409 (the user typed, Sync landed) changes
   nothing more for that board this pass; the next pass reads it again.
4. Pairs whose unit needs no planner call are committed now.
5. The planner updates (title, due, done, untag, tag, blockers), each
   unit's rows committed only once its calls succeeded: a row never
   records a value the planner has not taken, so a lost call is retried,
   never reverted. Blockers the planner refuses (a cycle) are counted as
   a conflict and skipped, and their snapshot stored as planned: the next
   pass reads the planner as having moved and writes its set back to the
   card, so a loop's link put on the card by this pass's write comes off
   again instead of being retried forever.

A planner or vault failure aborts the pass quietly: logged by event name
only, recorded as an outcome in `board_sync_run`, never retried by the
queue (the next pass is 15 minutes away), and the agenda snapshot is
never touched. `/boards now`, `/boards preview` and the planner's "Sync
with Obsidian" button (`planner`, guard kept) queue the same job with a
trigger; the preview reads and computes and writes nothing
anywhere -- not the boards, not the planner, not a pair, not a run row --
and works with the flag off, so the first pairing can be checked before
the flag goes on.

Nothing here logs a title, a card's text or a board's name: events and
counts only.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field, replace

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core import echo_write
from app.core.clock import Clock
from app.db.jobs import enqueue_job
from app.db.models import BoardPair, BoardSubPair, BoardSyncRun
from app.planner import auth as planner_auth
from app.planner import board_sync
from app.planner.auth import PlannerAuthError
from app.planner.board_sync import Counts, Plan
from app.planner.client import PlannerToolError, PlannerToolRefused, PlannerUnavailable
from app.vault.client import TaskBoard
from app.vault.errors import VaultError

logger = logging.getLogger(__name__)

BOARD_SYNC = "board_sync"

TRIGGER_AUTO = "auto"
TRIGGER_NOW = "now"
TRIGGER_PREVIEW = "preview"
# The planner's "Sync with Obsidian" button (app/web/board_sync_trigger.py):
# a pass like `/boards now`, but with the mass-loss guard kept -- a tap in
# another app is not the deliberate "yes, remove them" that `/boards now` is.
TRIGGER_PLANNER = "planner"
TRIGGERS = (TRIGGER_AUTO, TRIGGER_NOW, TRIGGER_PREVIEW, TRIGGER_PLANNER)
COMMAND_TRIGGERS = (TRIGGER_NOW, TRIGGER_PREVIEW, TRIGGER_PLANNER)

# Pass-level outcomes (BoardSyncRun.outcome with `board` null), and the
# per-board ones besides `ok`.
OK = "ok"
CHANGED = "changed"
UNAVAILABLE = "unavailable"
REFUSED = "refused"
PLANNER_UNAVAILABLE = "planner_unavailable"
NO_BOARDS = "no_boards"
MISSING = "missing"
NOT_BOARD = "not_board"
# Not stored: what a preview or a `/boards now` says when the gates are shut.
DISABLED = "disabled"
VAULT_OFF = "vault_off"
NOT_LINKED = "not_linked"

RUNS_KEEP_DAYS = 14
BUCKET_MIN = 15

_PLANNER_ERRORS = (PlannerUnavailable, PlannerToolError, PlannerAuthError)


class _Abort(Exception):
    """The pass stops here; `outcome` says why."""

    def __init__(self, outcome: str) -> None:
        super().__init__(outcome)
        self.outcome = outcome


@dataclass
class BoardResult:
    name: str
    outcome: str
    counts: Counts = field(default_factory=Counts)


@dataclass
class PassResult:
    """What one pass did (or, for a preview, would do). Counts and codes
    only: what the user is shown and what `board_sync_run` holds."""

    trigger: str
    outcome: str
    boards: list[BoardResult] = field(default_factory=list)

    @property
    def counts(self) -> Counts:
        total = Counts()
        for board in self.boards:
            total.add(board.counts)
        return total


# -- enqueue ---------------------------------------------------------------------------


def gates_open(settings: Settings) -> bool:
    """The deploy-level gates of an automatic pass (the planner link is
    checked against the database by the callers)."""
    return settings.BOARD_SYNC_ENABLED and settings.PLANNER_ENABLED and settings.VAULT_MODE != "off"


def dedup_key(clock: Clock) -> str:
    now = clock.now_utc()
    return f"{BOARD_SYNC}:{now.date().isoformat()}:{now.hour}:{now.minute // BUCKET_MIN}"


async def maybe_enqueue(session: AsyncSession, settings: Settings, clock: Clock) -> bool:
    """After a successful PLANNER_SYNC: queue one automatic pass, at most
    one per 15-minute bucket, only while every gate is open."""
    if not gates_open(settings) or not await planner_auth.is_enabled(session):
        return False
    enqueued = await enqueue_job(session, BOARD_SYNC, {"trigger": TRIGGER_AUTO}, dedup_key=dedup_key(clock))
    if enqueued:
        logger.info("board sync queued", extra={"event": BOARD_SYNC})
    return enqueued


def command_dedup_key(now: datetime.datetime, trigger: str) -> str:
    """The job's dedup key for a command trigger: one per trigger per minute."""
    return f"{BOARD_SYNC}:{trigger}:{now.strftime('%Y-%m-%dT%H:%M')}"


async def enqueue_command(
    session: AsyncSession, clock: Clock, trigger: str, *, now: datetime.datetime | None = None
) -> bool:
    """`/boards now`, `/boards preview` or the planner's button: queue one
    pass of that trigger right away. A double tap within the same minute
    queues one. `now` (default: the clock's) picks the minute, so a caller
    that waits for the job can name its key (`command_dedup_key`)."""
    if trigger not in COMMAND_TRIGGERS:
        raise ValueError(f"not a command trigger: {trigger}")
    key = command_dedup_key(now or clock.now_utc(), trigger)
    return await enqueue_job(session, BOARD_SYNC, {"trigger": trigger}, dedup_key=key)


# -- reading -----------------------------------------------------------------------------


def _read_rows(raw: object) -> list[tuple[board_sync.Task, str | None]]:
    """`list_tasks`' answer, strictly: a row of the wrong shape is a
    planner failure (the pass aborts), never a task the merge could act
    on wrongly. Each task with its parent id (also on the task)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("tasks"), list):
        raise PlannerToolError("list_tasks: malformed answer")
    out = []
    seen: set[str] = set()
    for item in raw["tasks"]:
        if not isinstance(item, dict):
            raise PlannerToolError("list_tasks: malformed row")
        task_id, title, done = item.get("id"), item.get("title"), item.get("done")
        tags, due, parent = item.get("tags", []), item.get("dueDate"), item.get("parentId")
        if not (isinstance(task_id, str) and task_id and isinstance(title, str) and isinstance(done, bool)):
            raise PlannerToolError("list_tasks: malformed row")
        if not (isinstance(tags, list) and all(isinstance(t, str) for t in tags)):
            raise PlannerToolError("list_tasks: malformed tags")
        if due is not None and not isinstance(due, str):
            raise PlannerToolError("list_tasks: malformed due date")
        if parent is not None and not (isinstance(parent, str) and parent):
            raise PlannerToolError("list_tasks: malformed parent")
        position, blocked = item.get("position", 0), item.get("blockedBy", [])
        if isinstance(position, bool) or not isinstance(position, (int, float)):
            raise PlannerToolError("list_tasks: malformed position")
        if blocked is None:
            blocked = []
        if not (isinstance(blocked, list) and all(isinstance(b, str) and b for b in blocked)):
            raise PlannerToolError("list_tasks: malformed blockers")
        if task_id in seen:
            raise PlannerToolError("list_tasks: a task twice")
        seen.add(task_id)
        task = board_sync.Task(
            id=task_id,
            title=title,
            done=done,
            due=due,
            tags=tuple(tags),
            parent_id=parent,
            position=float(position),
            blocked_by=tuple(dict.fromkeys(blocked)),
        )
        out.append((task, parent))
    return out


def untagged_of(tasks: list[board_sync.Task]) -> list[board_sync.Task]:
    """The tasks a card may adopt (`parse_all`'s answer cut down):
    top-level, open, with no tag at all."""
    return [task for task in tasks if task.parent_id is None and not task.done and not task.tags]


def knows_subtasks(raw: object) -> bool:
    """Whether the planner that answered `list_tasks` is one with subtasks
    and blockers in its rows: every row carries `position`. An older one
    would drop a create's `parentId` (making a top-level task of a card's
    subtask) and read every blocker as gone, so with it a pass leaves
    subtasks and dependencies alone, as with an older vaultd."""
    rows = raw.get("tasks") if isinstance(raw, dict) else None
    return isinstance(rows, list) and all(isinstance(r, dict) and "position" in r for r in rows)


def parse_all(raw: object) -> list[board_sync.Task]:
    """`list_tasks`' answer with no tag filter and subtasks included."""
    return [task for task, _parent in _read_rows(raw)]


def as_board(board: TaskBoard) -> board_sync.Board:
    cards = tuple(
        board_sync.Card(
            n=c.n,
            id=c.id,
            text=c.text,
            checked=c.checked,
            date=c.date,
            lane=c.lane,
            subs=tuple(
                board_sync.Sub(n=s.n, id=s.id, depth=s.depth, text=s.text, checked=s.checked, date=s.date)
                for s in c.subs
            ),
            after=c.after,
            after_raw_extra=c.after_raw_extra,
            after_titles=c.after_titles,
        )
        for c in board.cards
    )
    # An older vaultd answers without subtasks: unknown, not "none".
    subs_known = all(c.subs_known for c in board.cards)
    return board_sync.Board(
        name=board.name, dates=board.dates, cards=cards, sha256=board.sha256 or "", subs_known=subs_known
    )


async def load_rows(session: AsyncSession, board: str) -> list[board_sync.Row]:
    rows = (await session.execute(select(BoardPair).where(BoardPair.board == board))).scalars().all()
    return [
        board_sync.Row(
            block_id=r.block_id,
            task_id=r.task_id,
            title=r.title,
            done=r.done,
            due=r.due.isoformat() if r.due else None,
            status=r.status,
            deps=tuple(r.deps) if r.deps is not None else None,
            deps_card=tuple(r.deps_card) if r.deps_card is not None else None,
        )
        for r in rows
    ]


async def load_sub_rows(session: AsyncSession, board: str) -> list[board_sync.SubRow]:
    rows = (await session.execute(select(BoardSubPair).where(BoardSubPair.board == board))).scalars().all()
    return [
        board_sync.SubRow(
            block_id=r.block_id,
            card_block_id=r.card_block_id,
            task_id=r.task_id,
            parent_block_id=r.parent_block_id,
            title=r.title,
            done=r.done,
            due=r.due.isoformat() if r.due else None,
            status=r.status,
        )
        for r in rows
    ]


# -- storing ------------------------------------------------------------------------------


async def _apply_row(
    session: AsyncSession, board: str, change: board_sync.RowChange, task_id: str | None, now: datetime.datetime
) -> None:
    """One row change, flushed, not committed."""
    existing = (
        await session.execute(
            select(BoardPair).where(BoardPair.board == board, BoardPair.block_id == change.block_id)
        )
    ).scalar_one_or_none()
    if change.op == "delete":
        if existing is not None:
            await session.delete(existing)
        await session.flush()
        return
    if change.op == "deps":
        if existing is not None:
            existing.deps = list(change.deps) if change.deps is not None else None
            existing.deps_card = list(change.deps_card) if change.deps_card is not None else None
            existing.updated_at = now
        await session.flush()
        return
    due = datetime.date.fromisoformat(change.due) if change.due else None
    if existing is None:
        existing = BoardPair(board=board, block_id=change.block_id, created_at=now)
        session.add(existing)
    existing.task_id = task_id or change.task_id
    existing.title = change.title
    existing.done = change.done
    existing.due = due
    existing.status = change.status
    existing.updated_at = now
    await session.flush()


async def _apply_sub_row(
    session: AsyncSession, board: str, change: board_sync.SubRowChange, task_id: str | None, now: datetime.datetime
) -> None:
    """One subtask row change, flushed, not committed."""
    existing = await session.get(BoardSubPair, (board, change.block_id))
    if change.op == "delete":
        if existing is not None:
            await session.delete(existing)
        await session.flush()
        return
    due = datetime.date.fromisoformat(change.due) if change.due else None
    if existing is None:
        existing = BoardSubPair(board=board, block_id=change.block_id, created_at=now)
        session.add(existing)
    existing.card_block_id = change.card_block_id
    existing.task_id = task_id or change.task_id
    existing.parent_block_id = change.parent_block_id
    existing.title = change.title
    existing.done = change.done
    existing.due = due
    existing.status = change.status
    existing.updated_at = now
    await session.flush()


async def _apply_unit_rows(
    session: AsyncSession, board: str, unit: board_sync.Unit, task_id: str | None, now: datetime.datetime
) -> None:
    """A unit's row and subtask rows; `task_id` is what its create answered."""
    if unit.row is not None:
        await _apply_row(session, board, unit.row, task_id, now)
    for change in unit.sub_rows:
        await _apply_sub_row(session, board, change, task_id, now)


def _run_row(now: datetime.datetime, board: str | None, outcome: str, counts: Counts | None = None) -> BoardSyncRun:
    values = {}
    if counts is not None:
        values = {name: getattr(counts, name) for name in Counts.__dataclass_fields__}
    return BoardSyncRun(at=now, board=board, outcome=outcome, **values)


async def forget_boards(session: AsyncSession, names: list[str]) -> int:
    """Drop every pair of these boards (after `/boards undo` put them back
    as they were): the next pass pairs them again by title instead of
    reading the undone cards as deleted. Commits."""
    if not names:
        return 0
    result = await session.execute(delete(BoardPair).where(BoardPair.board.in_(names)))
    await session.execute(delete(BoardSubPair).where(BoardSubPair.board.in_(names)))
    await session.commit()
    return result.rowcount or 0


# -- the pass ------------------------------------------------------------------------------


async def run_pass(
    session: AsyncSession,
    settings: Settings,
    planner,
    vault,
    clock: Clock,
    *,
    trigger: str = TRIGGER_AUTO,
) -> PassResult:
    """One pass (module docstring). `planner` is a PlannerClient (or a
    fake with the same methods), `vault` a VaultClient. Never raises for
    a planner or vault failure: the outcome says it."""
    dry_run = trigger == TRIGGER_PREVIEW
    result = PassResult(trigger=trigger, outcome=OK)
    if settings.VAULT_MODE == "off":
        result.outcome = VAULT_OFF
        return result
    if not dry_run and not settings.BOARD_SYNC_ENABLED:
        result.outcome = DISABLED
        return result
    if not settings.PLANNER_ENABLED or not await planner_auth.is_enabled(session):
        result.outcome = NOT_LINKED
        return result

    now = clock.now_utc()
    try:
        # `/boards now` is the one way past the mass-loss guard
        # (board_sync.merge); a preview keeps it, to show what would be
        # held, and so does the planner's button.
        plans = await _read_and_plan(
            session, settings, planner, vault, clock, result, guard=trigger != TRIGGER_NOW
        )
        if not dry_run:
            changeset = echo_write.new_boards_ref()
            for tag, board, plan in plans:
                outcome = await _carry_out(session, settings, planner, vault, clock, tag, board, plan, changeset)
                # A board that changed under the pass did none of it.
                counts = plan.counts if outcome == OK else Counts()
                result.boards.append(BoardResult(board.name, outcome, counts))
                session.add(_run_row(now, board.name, outcome, counts))
                await session.commit()
        else:
            result.boards.extend(BoardResult(board.name, OK, plan.counts) for _tag, board, plan in plans)
    except _Abort as abort:
        result.outcome = abort.outcome
    except _PLANNER_ERRORS:
        result.outcome = PLANNER_UNAVAILABLE

    if not dry_run:
        await session.rollback()
        for board in result.boards:
            if board.outcome in (MISSING, NOT_BOARD):
                session.add(_run_row(now, board.name, board.outcome))
        if result.outcome != OK:
            session.add(_run_row(now, None, result.outcome))
        await session.execute(
            delete(BoardSyncRun).where(BoardSyncRun.at < now - datetime.timedelta(days=RUNS_KEEP_DAYS))
        )
        await session.commit()
    logger.info(
        "board sync pass",
        extra={"event": f"board_sync_{result.outcome}", "count": len(result.boards)},
    )
    return result


async def _read_and_plan(
    session: AsyncSession, settings: Settings, planner, vault, clock: Clock, result: PassResult, *, guard: bool = True
) -> list[tuple[str, board_sync.Board, Plan]]:
    """Step 1: every read, then every plan. Records `missing`/`not_board`
    boards on `result` and raises `_Abort` when there is nothing to do."""
    try:
        boards = await vault.get_boards(subs=True)
    except VaultError:
        raise _Abort(UNAVAILABLE) from None
    mapped = [b for b in boards if b.name in settings.BOARD_SYNC_TAGS]
    if not mapped:
        raise _Abort(NO_BOARDS)
    plans = []
    every: list[board_sync.Task] | None = None
    untagged: list[board_sync.Task] = []
    planner_subs = False
    sides: list[board_sync.DepsSide] = []
    for board in sorted(mapped, key=lambda b: b.name):
        if board.state != "ok":
            result.boards.append(BoardResult(board.name, MISSING if board.state == "missing" else NOT_BOARD))
            continue
        if every is None:
            # One read for every board: a subtask has no tag of its own.
            raw = await planner.list_tasks(
                settings, session, clock, tag=None, include_subtasks=True, include_completed=True
            )
            every = parse_all(raw)
            untagged = untagged_of(every)
            planner_subs = knows_subtasks(raw)
        tag = settings.BOARD_SYNC_TAGS[board.name]
        tasks = [t for t in every if t.parent_id is None and tag in t.tags]
        subtasks = [t for t in every if t.parent_id is not None]
        rows = await load_rows(session, board.name)
        sub_rows = await load_sub_rows(session, board.name)
        merged = as_board(board)
        if not planner_subs:
            merged = replace(merged, subs_known=False)
        plan = board_sync.merge(
            merged,
            tasks,
            rows,
            tag=tag,
            creates_max=settings.BOARD_SYNC_CREATES_PER_PASS,
            guard=guard,
            untagged=untagged,
            subtasks=subtasks,
            sub_rows=sub_rows,
        )
        # One task, one board: what this board adopts is not offered to the next.
        taken = {call.task_id for call in plan.calls if call.kind == "tag"}
        untagged = [task for task in untagged if task.id not in taken]
        plans.append((tag, merged, plan))
        sides.append(board_sync.DepsSide(merged, tuple(tasks), tuple(rows), tuple(sub_rows)))
    # Dependencies, across boards, from the rows as the pass found them;
    # the lowest priority in each board's batch.
    deps = board_sync.merge_deps(sides, every or [])
    for _tag, merged, plan in plans:
        board_sync.add_units(plan, deps.get(merged.name))
    return plans


def _created_id(answer: object) -> str:
    task_id = answer.get("id") if isinstance(answer, dict) else None
    if not isinstance(task_id, str) or not task_id:
        raise PlannerToolError("create_task: no id")
    return task_id


async def _carry_out(
    session: AsyncSession,
    settings: Settings,
    planner,
    vault,
    clock: Clock,
    tag: str,
    board: board_sync.Board,
    plan: Plan,
    changeset: str,
) -> str:
    """Steps 2-5 for one board. Returns its outcome; raises `_Abort` (or
    a planner error) to stop the pass."""
    now = clock.now_utc()
    known_tasks = {row.task_id for row in await load_rows(session, board.name)}
    known_tasks |= {row.task_id for row in await load_sub_rows(session, board.name)}

    # 2. Planner creates: tasks, then subtasks (each under a task that
    # already had a pair, so its parent id is known).
    created: dict[str, str] = {}
    creates = sorted(plan.creates, key=lambda c: c.parent_task_id is not None)
    for create in creates:
        answer = await planner.create_task(
            settings, session, clock,
            title=create.title,
            due_date=create.due,
            client_request_id=create.request_id,
            tags=[create.tag] if create.tag else None,
            parent_id=create.parent_task_id,
        )
        task_id = _created_id(answer)
        if create.done:
            await planner.complete_task(settings, session, clock, task_id=task_id, done=True)
        created[create.block_id] = task_id

    # A create retried by its request id may answer with a task this pass
    # also meant to add as a card, or one already paired: the card being
    # tagged now is that task's card, so the add goes, and a second pair
    # for one task is never made.
    answered = set(created.values())
    units = []
    claimed: set[str] = set()
    for unit in plan.units:
        if unit.adds_task is not None and unit.adds_task in answered:
            continue
        if unit.create is not None:
            task_id = created[unit.create.block_id]
            if task_id in known_tasks or task_id in claimed:
                units.append(board_sync.Unit(ops=unit.ops))
                continue
            claimed.add(task_id)
        units.append(unit)
    kept = Plan(board=board.name, units=units, counts=plan.counts)

    # 3. The board write.
    ops = kept.ops
    if ops:
        written = await echo_write.write_board(vault, board.name, board.sha256, ops, changeset)
        if written.outcome == echo_write.BOARD_CHANGED:
            return CHANGED
        if written.outcome == echo_write.REFUSED:
            raise _Abort(REFUSED)
        if written.outcome != echo_write.OK:
            raise _Abort(UNAVAILABLE)

    # 4. Rows that wait on nothing else.
    for unit in kept.units:
        if not unit.calls:
            task_id = created.get(unit.create.block_id) if unit.create is not None else None
            await _apply_unit_rows(session, board.name, unit, task_id, now)
    await session.commit()

    # 5. Planner updates, each unit's rows once its calls are made.
    for unit in kept.units:
        if not unit.calls:
            continue
        refused = False
        for call in unit.calls:
            try:
                await _call(planner, settings, session, clock, call)
            except PlannerToolRefused:
                if call.kind != "blocked_by":
                    raise
                refused = True
        if refused:
            # A cycle, refused after the board write may already have put
            # the link on the card. The planner's set stands: the snapshot
            # is stored as planned all the same, so the next pass sees the
            # planner as the side that moved and writes its set back to
            # the card, taking the loop's link off (board_sync,
            # "Dependencies"). A conflict and a skip, never a loop.
            plan.counts.skipped += 1
            plan.counts.conflicts += 1
            if not unit.ops:
                plan.counts.deps_changed = max(0, plan.counts.deps_changed - 1)
        await _apply_unit_rows(session, board.name, unit, None, now)
        await session.commit()
    return OK


async def _call(planner, settings: Settings, session: AsyncSession, clock: Clock, call: board_sync.Call) -> None:
    if call.kind == "title":
        await planner.update_task(settings, session, clock, task_id=call.task_id, title=call.value)
    elif call.kind == "due":
        await planner.update_task(settings, session, clock, task_id=call.task_id, due_date=call.value)
    elif call.kind == "done":
        await planner.complete_task(settings, session, clock, task_id=call.task_id, done=bool(call.value))
    elif call.kind in ("untag", "tag"):
        await planner.update_task(settings, session, clock, task_id=call.task_id, tags=list(call.value))
    elif call.kind == "blocked_by":
        await planner.update_task(settings, session, clock, task_id=call.task_id, blocked_by=list(call.value))
    else:
        raise ValueError(f"unknown board call: {call.kind}")


# -- what /boards and the digest read ----------------------------------------------------


@dataclass(frozen=True)
class BoardStatus:
    name: str
    tag: str
    paired: int
    held: int
    last: BoardSyncRun | None


async def status(session: AsyncSession, settings: Settings) -> tuple[list[BoardStatus], BoardSyncRun | None]:
    """Each mapped board's stored state, and the newest pass-level run
    (a pass that never reached a board). Stored state only: no call."""
    out = []
    for name, tag in sorted(settings.BOARD_SYNC_TAGS.items()):
        rows = (await session.execute(select(BoardPair.status).where(BoardPair.board == name))).scalars().all()
        last = (
            await session.execute(
                select(BoardSyncRun)
                .where(BoardSyncRun.board == name)
                .order_by(BoardSyncRun.at.desc(), BoardSyncRun.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        out.append(BoardStatus(name, tag, len(rows), sum(1 for s in rows if s == board_sync.ROW_HELD), last))
    whole = (
        await session.execute(
            select(BoardSyncRun)
            .where(BoardSyncRun.board.is_(None))
            .order_by(BoardSyncRun.at.desc(), BoardSyncRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return out, whole


async def issues_between(
    session: AsyncSession, start: datetime.datetime, end: datetime.datetime
) -> tuple[int, int]:
    """(conflicts, skipped titles) over the passes in [start, end).

    Conflicts are summed: each is settled by the pass that found it. A
    skipped title is skipped again by every pass until one side changes,
    so summing would count one title ninety times a day; the figure is
    the most any one pass skipped."""
    runs = (
        await session.execute(
            select(BoardSyncRun.at, BoardSyncRun.conflicts, BoardSyncRun.skipped).where(
                BoardSyncRun.at >= start, BoardSyncRun.at < end, BoardSyncRun.board.is_not(None)
            )
        )
    ).all()
    per_pass: dict[datetime.datetime, int] = {}
    for at, _conflicts, skipped in runs:
        per_pass[at] = per_pass.get(at, 0) + skipped
    return sum(r[1] for r in runs), max(per_pass.values(), default=0)
