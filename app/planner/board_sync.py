"""The two-way merge between a task board in the vault and the planner's
tasks with that board's tag. Pure: no I/O, no clock, no database.

The user keeps tasks on Kanban Starlane boards at the root of the vault
(vaultd/vaultd/boards.py) and in the planner; a board is mapped to one
planner tag (`BOARD_SYNC_TAGS`). app/planner/board_job.py reads both
sides and the stored pair rows, calls `merge` once per board, and carries
out what it returns. This module only decides.

**The state** is one `Row` per tracked pair: the card's block id, the
task's id and the last synced snapshot of the three synced fields
(`title`, `done`, `due`). Every pass compares each side against that
snapshot, field by field:

- changed on one side only: carried to the other side;
- changed on both sides to different values: the planner wins, and it is
  counted as a conflict;
- changed on both sides to the same value: only the snapshot moves.

**New things.** A card with no row (no block id, or one no row knows)
becomes a planner task with the board's tag; a top-level, open task of
the user's own with the tag and no row becomes a card. Before either
rule, an unrowed card and an unrowed task are *paired* instead when the
card carries the id this module would mint for that task, or when their
titles are the same (stripped, case-insensitively); the planner's done
and due win on a fresh pair. Pairing runs on every pass, not only when
a board has no rows yet: a pass interrupted between the planner's
create and the board write leaves exactly such an unrowed couple, and
pairing is what keeps the next pass from duplicating it.

**Gone things.** A card that left the board (deleted, archived) while
its task still has the tag: the tag is removed from the task, never the
task. A task that left the tagged list (untagged, deleted, no longer the
user's own) while its card is still there: the card is removed. Both
gone: the row is dropped. Nothing with no row is ever touched except by
the "new" rules.

**What is not synced.** Lanes beyond open/done, continuation lines,
descriptions, subtasks, the archive. A board whose dates use their own
syntax (`dates: false`) syncs no due date in either direction. A done
planner task with no row is not added as a card: the planner keeps
every completed task forever, and adding them all to a board would bury
it (and past vaultd's 500 cards a board stops being one).

**Titles.** vaultd refuses a whole batch for one bad card text, so
`card_text_ok` mirrors its check (vaultd/vaultd/boards.py's
`_check_text`) and a title that fails it is never sent: that card is not
added or retitled, the pass counts it as skipped, and everything else
goes on. A card text the planner cannot take as a title (empty, over 500
characters) is skipped the same way. Hashtags and `@@{time}` inside a
card's text are part of the title and travel verbatim.

**Caps and order.** At most `OPS_MAX` board operations per board per
pass (vaultd's limit) and `creates_max` planner creates; what does not
fit waits for the next pass. Units are taken in a fixed order (existing
pairs by block id, then new pairs, new cards and new tasks), so the same
input always gives the same output. A merge run again on the state its
own output produced yields no operation, no call and no row change.

**Ids.** Block ids the bot mints are 12 hex characters (vaultd wants
`^[a-z0-9]{4,16}$`), derived from the board's name and, for a new card,
its text and occurrence, for a new task, the task's id -- so the same
card or task gets the same id on every pass, and the planner create's
`clientRequestId` (built from the board and that id) makes a retried
create return the task it already made.
"""

from __future__ import annotations

import datetime
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field, replace

# vaultd's limits (vaultd/vaultd/config.py).
OPS_MAX = 100
CARD_MAX_CHARS = 500
BOARD_MAX_CARDS = 500
# The planner's own (lib/mcp/tools.ts: `title: z.string().min(1).max(500)`).
TITLE_MAX_CHARS = 500
CREATES_MAX_DEFAULT = 20
# A pass that would drop more pairs than this, and more than half of the
# board's pairs, drops none (`merge`'s mass-loss guard).
MASS_LOSS_MIN = 3

ID_RE = re.compile(r"^[a-z0-9]{4,16}$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# vaultd/vaultd/boards.py's `_BLOCK_ID_RE` and `_DATE_RE`, copied exactly:
# a text that matches either would read back as structure.
_BLOCK_ID_RE = re.compile(r" \^([A-Za-z0-9-]+)$")
_DATE_RE = re.compile(r" ?@\{(\d{4}-\d{2}-\d{2})\}")
_BAD_CATEGORIES = ("Cc", "Cs", "Zl", "Zp")

ROW_OK = "ok"
# The pair has a field this pass could not carry (a title that cannot be
# a card or a task, a due date that is not a date): it is retried, and
# counted as skipped, on every pass until one side changes.
ROW_HELD = "held"


# -- input -------------------------------------------------------------------------


@dataclass(frozen=True)
class Card:
    """One active card as `GET /v1/boards` reports it."""

    n: int
    id: str | None
    text: str
    checked: bool
    date: str | None
    lane: str = ""


@dataclass(frozen=True)
class Board:
    name: str
    dates: bool
    cards: tuple[Card, ...]
    sha256: str = ""


@dataclass(frozen=True)
class Task:
    """One of the planner's `list_tasks` rows: the user's own, top-level,
    carrying the board's tag."""

    id: str
    title: str
    done: bool
    due: str | None = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Row:
    """A tracked pair and its last synced snapshot."""

    block_id: str
    task_id: str
    title: str
    done: bool
    due: str | None
    status: str = ROW_OK


# -- output ------------------------------------------------------------------------


@dataclass(frozen=True)
class Create:
    """A planner `create_task` (then `complete_task` when `done`). The
    task id is the planner's answer; `request_id` makes a retry return
    the task already made."""

    block_id: str
    title: str
    due: str | None
    done: bool
    tag: str
    request_id: str


@dataclass(frozen=True)
class Call:
    """A planner write on an existing task, made after the board write.

    `kind` is `title` (`update_task` title), `due` (`update_task`
    dueDate, None clears it), `done` (`complete_task`) or `untag`
    (`update_task` tags, `value` being the tags that stay)."""

    kind: str
    task_id: str
    value: object


@dataclass(frozen=True)
class RowChange:
    """`insert` (task_id is None when the unit's Create supplies it),
    `update` or `delete` of one row of this board."""

    op: str
    block_id: str
    task_id: str | None = None
    title: str = ""
    done: bool = False
    due: str | None = None
    status: str = ROW_OK


@dataclass(frozen=True)
class Unit:
    """Everything one pair (or one new card, or one new task) needs, kept
    together so a cap defers all of it or none. The executor makes
    `create` before the board write, sends `ops` in the board's batch,
    makes `calls` after the board write succeeded or was not needed, and
    applies `row` once all of that succeeded.

    `adds_task` is the planner task a new card is added for: if a create
    in the same pass turns out to answer with that very task (a create
    retried by its request id), the executor drops this unit."""

    ops: tuple[dict, ...] = ()
    create: Create | None = None
    calls: tuple[Call, ...] = ()
    row: RowChange | None = None
    adds_task: str | None = None


@dataclass
class Counts:
    """What a pass would do, or did. Numbers only: never a title."""

    cards_added: int = 0
    cards_changed: int = 0
    cards_tagged: int = 0
    cards_removed: int = 0
    tasks_created: int = 0
    tasks_changed: int = 0
    tasks_untagged: int = 0
    paired: int = 0
    conflicts: int = 0
    skipped: int = 0
    waiting: int = 0
    # Removals the mass-loss guard held back (see `merge`).
    held: int = 0

    def add(self, other: "Counts") -> None:
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))


@dataclass
class Plan:
    """`merge`'s result for one board."""

    board: str
    units: list[Unit] = field(default_factory=list)
    counts: Counts = field(default_factory=Counts)

    @property
    def ops(self) -> list[dict]:
        """The board's batch, in the order vaultd must apply it: `tag`
        first (it names cards by their index in the board as read, which
        only `tag` leaves alone), then changes, removals and additions."""
        order = {"tag": 0, "text": 1, "date": 1, "check": 1, "remove": 2, "add": 3}
        ops = [op for unit in self.units for op in unit.ops]
        return sorted(ops, key=lambda op: order[op["op"]])

    @property
    def creates(self) -> list[Create]:
        return [unit.create for unit in self.units if unit.create is not None]

    @property
    def calls(self) -> list[Call]:
        return [call for unit in self.units for call in unit.calls]

    @property
    def row_changes(self) -> list[RowChange]:
        return [unit.row for unit in self.units if unit.row is not None]


# -- screening ---------------------------------------------------------------------


def norm(text: str) -> str:
    """The form both sides are compared in: NFC, stripped. vaultd
    normalizes a card's text the same way when it writes one."""
    return unicodedata.normalize("NFC", text).strip()


def title_key(text: str) -> str:
    """The pairing key: the same title, stripped, case-insensitively."""
    return norm(text).casefold()


def card_text_ok(text: str) -> bool:
    """Whether vaultd would accept `text` as a card's text
    (vaultd/vaultd/boards.py's `_check_text`): one line of at most 500
    characters that reads back as the same text."""
    text = norm(text)
    if not text or len(text) > CARD_MAX_CHARS:
        return False
    if any(unicodedata.category(c) in _BAD_CATEGORIES for c in text):
        return False
    return not ("%%" in text or _BLOCK_ID_RE.search(text) or _DATE_RE.search(text))


def task_title_ok(text: str) -> bool:
    """Whether the planner takes `text` as a title."""
    text = norm(text)
    return 0 < len(text) <= TITLE_MAX_CHARS


def _date(value: str | None) -> str | None:
    """`value` when it is a real day as `yyyy-MM-dd`, else None. A planner
    value of another form is not a date the board can show, and a card's
    `@{2026-02-30}` is not one the planner can take."""
    if value is None or not ISO_DATE_RE.match(value):
        return None
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return None
    return value


# -- ids ---------------------------------------------------------------------------


def _hex(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


def mint_for_task(board: str, task_id: str) -> str:
    """The block id of the card added for `task_id` on `board`."""
    return _hex(f"task\0{board}\0{task_id}")


def mint_for_card(board: str, text: str, occurrence: int) -> str:
    """The block id given to a card that has none: its `occurrence`-th
    (from 0) among the board's untracked cards with the same text."""
    return _hex(f"card\0{board}\0{norm(text)}\0{occurrence}")


def request_id(board: str, block_id: str) -> str:
    """The planner create's `clientRequestId` (at most 100 characters)."""
    return f"echo-board:{_hex('board' + chr(0) + board)}:{block_id}"


class _Ids:
    """The ids on a board, plus the ones this pass mints, so no two cards
    ever share one (vaultd refuses the batch otherwise)."""

    def __init__(self, cards: tuple[Card, ...]) -> None:
        self.taken = {card.id for card in cards if card.id}

    def claim(self, wanted: str) -> str:
        candidate, salt = wanted, 0
        while candidate in self.taken:
            salt += 1
            candidate = _hex(f"{wanted}\0{salt}")
        self.taken.add(candidate)
        return candidate


# -- the merge ---------------------------------------------------------------------


_NOTHING = object()


def _three_way(base: object, card: object, task: object) -> tuple[object, object, bool]:
    """(to the card, to the task, conflict), `_NOTHING` meaning no write.
    `base` is `_NOTHING` for a pair with no snapshot yet: the planner wins."""
    if card == task:
        return _NOTHING, _NOTHING, False
    if base is _NOTHING or card == base:
        return task, _NOTHING, False
    if task == base:
        return _NOTHING, card, False
    return task, _NOTHING, True


@dataclass
class _Fields:
    """The outcome of comparing one pair's three fields."""

    ops: list[dict] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    snapshot: dict = field(default_factory=dict)
    conflicts: int = 0
    skipped: int = 0


def _compare(board: Board, card: Card, task: Task, row: Row | None, block_id: str) -> _Fields:
    out = _Fields()
    base = {
        "title": norm(row.title) if row else _NOTHING,
        "done": row.done if row else _NOTHING,
        "due": row.due if row else _NOTHING,
    }

    # Title.
    c_title, t_title = norm(card.text), norm(task.title)
    to_card, to_task, conflict = _three_way(base["title"], c_title, t_title)
    out.conflicts += conflict
    out.snapshot["title"] = c_title if to_card is _NOTHING and to_task is _NOTHING else None
    if to_card is not _NOTHING:
        if card_text_ok(to_card):
            out.ops.append({"op": "text", "id": block_id, "text": to_card})
            out.snapshot["title"] = to_card
        else:
            out.skipped += 1
    if to_task is not _NOTHING:
        if task_title_ok(to_task):
            out.calls.append(Call("title", task.id, to_task))
            out.snapshot["title"] = to_task
        else:
            out.skipped += 1
    if out.snapshot["title"] is None:
        # Not carried: the snapshot keeps its old value, so the next pass
        # sees the same difference and tries again.
        out.snapshot["title"] = row.title if row else c_title

    # Done.
    to_card, to_task, conflict = _three_way(base["done"], card.checked, task.done)
    out.conflicts += conflict
    if to_card is not _NOTHING:
        out.ops.append({"op": "check", "id": block_id, "checked": to_card})
    if to_task is not _NOTHING:
        out.calls.append(Call("done", task.id, to_task))
    out.snapshot["done"] = task.done if to_task is _NOTHING else card.checked

    # Due: only on a board whose dates the bot may read and write.
    if not board.dates:
        out.snapshot["due"] = row.due if row else None
        return out
    if card.date is not None and _date(card.date) is None:
        # The card's date is not a real day: neither side moves.
        out.skipped += 1
        out.snapshot["due"] = row.due if row else None
        return out
    t_due = task.due
    to_card, to_task, conflict = _three_way(base["due"], card.date, t_due)
    out.conflicts += conflict
    out.snapshot["due"] = card.date if to_card is _NOTHING and to_task is _NOTHING else None
    settled = to_card is _NOTHING and to_task is _NOTHING
    if to_card is not _NOTHING:
        if to_card is None or _date(to_card) is not None:
            out.ops.append({"op": "date", "id": block_id, "date": to_card})
            out.snapshot["due"] = to_card
            settled = True
        else:
            out.skipped += 1
    if to_task is not _NOTHING:
        out.calls.append(Call("due", task.id, to_task))
        out.snapshot["due"] = to_task
        settled = True
    if not settled:
        out.snapshot["due"] = row.due if row else None
    return out


def _row_for(block_id: str, task_id: str | None, f: _Fields, op: str) -> RowChange:
    return RowChange(
        op=op,
        block_id=block_id,
        task_id=task_id,
        title=f.snapshot["title"],
        done=f.snapshot["done"],
        due=f.snapshot["due"],
        status=ROW_HELD if f.skipped else ROW_OK,
    )


def _row_unchanged(row: Row, change: RowChange) -> bool:
    return (row.title, row.done, row.due, row.status) == (change.title, change.done, change.due, change.status)


def merge(
    board: Board,
    tasks: list[Task],
    rows: list[Row],
    *,
    tag: str,
    creates_max: int = CREATES_MAX_DEFAULT,
    guard: bool = True,
) -> Plan:
    """What one pass does for one board (module docstring).

    **The mass-loss guard.** A pair whose card or task is gone means a
    removal on the other side, and one side reading as empty by mistake
    (a planner answer cut short, a board the user is half way through
    reorganising, a tag renamed) would turn that into every card removed
    or every task untagged. So when one pass would drop more than
    `MASS_LOSS_MIN` pairs and more than half of the board's pairs, none
    of those removals is made: the pairs stay as they are and the count
    is reported as `held`, pass after pass, until the two sides agree
    again or the user runs `/boards now`, which passes `guard=False`
    because a person asked for exactly this board to be brought in step."""
    plan = Plan(board=board.name)
    counts = plan.counts
    ids = _Ids(board.cards)

    rows = sorted(rows, key=lambda r: r.block_id)
    row_ids = {row.block_id for row in rows}
    row_tasks = {row.task_id for row in rows}
    # A block id on two cards (a copied line) names the first: the plugin
    # never does this, and vaultd's own lookup takes the first too.
    cards_by_id: dict[str, Card] = {}
    for card in board.cards:
        if card.id and card.id not in cards_by_id:
            cards_by_id[card.id] = card
    tasks_by_id = {task.id: task for task in tasks}

    # (kind, unit, counts) in a fixed order, cut to the caps afterwards;
    # the kind is for reading only.
    candidates: list[tuple[str, Unit, Counts]] = []

    # 1. Existing pairs.
    for row in rows:
        card = cards_by_id.get(row.block_id)
        task = tasks_by_id.get(row.task_id)
        c = Counts()
        if card is None and task is None:
            candidates.append(("pair", Unit(row=RowChange("delete", row.block_id)), c))
            continue
        if card is None:
            # Gone from the board: the task keeps everything but the tag.
            stay = tuple(t for t in task.tags if t != tag)
            c.tasks_untagged = 1
            unit = Unit(calls=(Call("untag", task.id, stay),), row=RowChange("delete", row.block_id))
            candidates.append(("pair", unit, c))
            continue
        if task is None:
            # Gone from the tagged list: the card goes.
            c.cards_removed = 1
            unit = Unit(ops=({"op": "remove", "id": row.block_id},), row=RowChange("delete", row.block_id))
            candidates.append(("pair", unit, c))
            continue
        f = _compare(board, card, task, row, row.block_id)
        change = _row_for(row.block_id, row.task_id, f, "update")
        c.cards_changed = 1 if f.ops else 0
        c.tasks_changed = 1 if f.calls else 0
        c.conflicts, c.skipped = f.conflicts, f.skipped
        if f.ops or f.calls or not _row_unchanged(row, change):
            candidates.append(("pair", Unit(ops=tuple(f.ops), calls=tuple(f.calls), row=change), c))
        else:
            # Held as before: nothing to do, but still a skip (and, when
            # both sides moved, a conflict) this pass.
            counts.skipped += c.skipped
            counts.conflicts += c.conflicts

    # 2. Pairing what has no row.
    free_cards = [card for card in board.cards if card.id is None or (card.id not in row_ids and cards_by_id.get(card.id) is card)]
    free_tasks = sorted((t for t in tasks if t.id not in row_tasks), key=lambda t: t.id)
    pairs: list[tuple[Card, Task]] = []
    by_mint = {mint_for_task(board.name, t.id): t for t in free_tasks}
    for card in list(free_cards):
        task = by_mint.get(card.id) if card.id else None
        if task is not None:
            pairs.append((card, task))
            free_cards.remove(card)
            free_tasks.remove(task)
    tasks_by_key: dict[str, list[Task]] = {}
    for task in free_tasks:
        tasks_by_key.setdefault(title_key(task.title), []).append(task)
    for card in list(free_cards):
        bucket = tasks_by_key.get(title_key(card.text))
        if bucket:
            task = bucket.pop(0)
            pairs.append((card, task))
            free_cards.remove(card)
            free_tasks.remove(task)

    # Ids for the untracked cards that will need one, by occurrence of text.
    seen: dict[str, int] = {}
    minted: dict[int, str] = {}
    for card in board.cards:
        if card.id is None:
            key = norm(card.text)
            k = seen.get(key, 0)
            seen[key] = k + 1
            minted[card.n] = ids.claim(mint_for_card(board.name, card.text, k))

    for card, task in sorted(pairs, key=lambda p: p[0].n):
        block_id = card.id or minted[card.n]
        f = _compare(board, card, task, None, block_id)
        ops = ([{"op": "tag", "n": card.n, "id": block_id}] if card.id is None else []) + f.ops
        c = Counts(paired=1, cards_tagged=1 if card.id is None else 0)
        c.cards_changed = 1 if f.ops else 0
        c.tasks_changed = 1 if f.calls else 0
        c.skipped = f.skipped
        unit = Unit(ops=tuple(ops), calls=tuple(f.calls), row=_row_for(block_id, task.id, f, "insert"))
        candidates.append(("pair", unit, c))

    # 3. New cards: a planner task each.
    for card in free_cards:
        if not task_title_ok(card.text):
            counts.skipped += 1
            continue
        block_id = card.id or minted[card.n]
        title = norm(card.text)
        due = _date(card.date) if board.dates else None
        held = board.dates and card.date is not None and due is None
        create = Create(
            block_id=block_id,
            title=title,
            due=due,
            done=card.checked,
            tag=tag,
            request_id=request_id(board.name, block_id),
        )
        ops = ({"op": "tag", "n": card.n, "id": block_id},) if card.id is None else ()
        row = RowChange("insert", block_id, None, title, card.checked, due, ROW_HELD if held else ROW_OK)
        c = Counts(tasks_created=1, cards_tagged=1 if card.id is None else 0, skipped=1 if held else 0)
        candidates.append(("create", Unit(ops=ops, create=create, row=row), c))

    # 4. New tasks: a card each (open ones only, module docstring).
    for task in free_tasks:
        if task.done:
            continue
        title = norm(task.title)
        if not card_text_ok(title):
            counts.skipped += 1
            continue
        block_id = ids.claim(mint_for_task(board.name, task.id))
        due = _date(task.due) if board.dates else None
        op: dict = {"op": "add", "id": block_id, "text": title}
        if due is not None:
            op["date"] = due
        c = Counts(cards_added=1)
        # A planner due date that is not a date the board can show stays
        # off the card; the pair is held on it, as `_compare` would hold
        # it on every later pass.
        held = board.dates and task.due is not None and due is None
        c.skipped = 1 if held else 0
        row = RowChange("insert", block_id, task.id, title, False, due, ROW_HELD if held else ROW_OK)
        candidates.append(("add", Unit(ops=(op,), row=row, adds_task=task.id), c))

    losses = [
        (unit, c)
        for _kind, unit, c in candidates
        if c.cards_removed or c.tasks_untagged
    ]
    hold = guard and len(losses) > MASS_LOSS_MIN and len(losses) * 2 > len(rows)
    held_units = {id(unit) for unit, _c in losses} if hold else set()
    counts.held = len(held_units)

    # Caps: vaultd's batch size, the planner's creates, the board's cards.
    used_ops = creates = 0
    cards = len(board.cards)
    for _kind, unit, c in candidates:
        if id(unit) in held_units:
            continue
        removes = sum(1 for op in unit.ops if op["op"] == "remove")
        adds = sum(1 for op in unit.ops if op["op"] == "add")
        if (
            used_ops + len(unit.ops) > OPS_MAX
            or (unit.create is not None and creates >= creates_max)
            or cards - removes + adds > BOARD_MAX_CARDS
        ):
            counts.waiting += 1
            continue
        used_ops += len(unit.ops)
        creates += unit.create is not None
        cards += adds - removes
        plan.units.append(unit)
        counts.add(c)
    return plan


def apply_to_state(
    board: Board, tasks: list[Task], rows: list[Row], plan: Plan, *, tag: str, created_ids: dict[str, str] | None = None
) -> tuple[Board, list[Task], list[Row]]:
    """The state after `plan` ran in full: the board with its batch
    applied, the planner with its creates and calls made, the rows with
    their changes. A model of what vaultd and the planner do, for the
    tests' idempotence checks and for nothing else; `created_ids` maps a
    create's block id to the task id the planner answered (default:
    `new-<block id>`)."""
    created_ids = dict(created_ids or {})
    cards = list(board.cards)
    for op in plan.ops:
        kind = op["op"]
        if kind == "tag":
            cards[op["n"]] = replace(cards[op["n"]], id=op["id"])
            continue
        if kind == "add":
            cards.append(Card(n=len(cards), id=op["id"], text=op["text"], checked=bool(op.get("checked", False)), date=op.get("date")))
            continue
        index = next(i for i, card in enumerate(cards) if card.id == op["id"])
        if kind == "text":
            cards[index] = replace(cards[index], text=op["text"])
        elif kind == "date":
            cards[index] = replace(cards[index], date=op["date"])
        elif kind == "check":
            cards[index] = replace(cards[index], checked=op["checked"])
        elif kind == "remove":
            del cards[index]
    cards = [replace(card, n=i) for i, card in enumerate(cards)]

    by_id = {task.id: task for task in tasks}
    for create in plan.creates:
        task_id = created_ids.setdefault(create.block_id, f"new-{create.block_id}")
        by_id[task_id] = Task(task_id, create.title, create.done, create.due, (create.tag,))
    for call in plan.calls:
        task = by_id[call.task_id]
        if call.kind == "title":
            by_id[call.task_id] = replace(task, title=call.value)
        elif call.kind == "due":
            by_id[call.task_id] = replace(task, due=call.value)
        elif call.kind == "done":
            by_id[call.task_id] = replace(task, done=call.value)
        elif call.kind == "untag":
            by_id[call.task_id] = replace(task, tags=tuple(call.value))
    new_tasks = [task for task in by_id.values() if tag in task.tags]

    by_block = {row.block_id: row for row in rows}
    for change in plan.row_changes:
        if change.op == "delete":
            by_block.pop(change.block_id, None)
            continue
        task_id = change.task_id if change.task_id is not None else created_ids[change.block_id]
        by_block[change.block_id] = Row(change.block_id, task_id, change.title, change.done, change.due, change.status)
    return replace(board, cards=tuple(cards)), new_tasks, sorted(by_block.values(), key=lambda r: r.block_id)
