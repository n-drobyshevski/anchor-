"""app/planner/board_job.py and app/tg/boards.py: one board-sync pass end
to end, against the throwaway database, with a fake planner and a fake
vault. Every title here is synthetic.

What each test pins: the order of a pass (creates, then the board write,
then the rows and the planner updates), what a 409 or a failure leaves
behind, that a preview writes nothing anywhere, that the agenda snapshot
is never touched, the gates, the enqueue, `/boards` and its undo, the
digest line and the logs.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import replace

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.core import echo_write
from app.db.models import BoardPair, BoardSubPair, BoardSyncRun, Job, PlannerCredential, PlannerSnapshot, UserState
from app.planner import board_job, board_sync
from app.planner.client import (
    UNSET,
    Collection,
    Column,
    PlannerToolError,
    PlannerToolRefused,
    PlannerToolUnknown,
    PlannerUnavailable,
)
from app.tg import boards as boards_ui
from app.vault import errors
from app.vault.client import BoardCard, BoardPost, BoardSub, ChangeEntry, ChangeFile, TaskBoard, UndoResult
from app.vault.errors import VaultError

pytestmark = pytest.mark.asyncio

TZ = "Europe/Paris"
NAME = "Work tasks.md"
SHA = "a" * 64
SECRET_TITLE = "Секретная задача про отчёт"


def _settings(**extra) -> Settings:
    values = dict(_env_file=None, PLANNER_ENABLED=True, VAULT_MODE="sync", VAULT_API_TOKEN="x" * 32, BOARD_SYNC_ENABLED=True)
    values.update(extra)
    return Settings(**values)


def _cards(*specs, subs_known: bool = False) -> tuple[BoardCard, ...]:
    """(`lane`: the card's lane, default "Todo".)"""
    """Cards as vaultd reports them; with `subs_known`, as a vaultd that
    reads subtasks does (`subs`: (id, depth, text) or a dict each)."""
    out = []
    for i, s in enumerate(specs):
        subs = []
        for k, sub in enumerate(s.get("subs", ())):
            if isinstance(sub, tuple):
                sub = {"id": sub[0], "depth": sub[1], "text": sub[2]}
            subs.append(BoardSub(k, sub.get("id"), sub["depth"], sub["text"], sub.get("checked", False), sub.get("date")))
        out.append(
            BoardCard(
                n=i, id=s.get("id"), lane=s.get("lane", "Todo"), text=s["text"], checked=s.get("checked", False),
                date=s.get("date"),
                subs=tuple(subs), after=tuple(s.get("after", ())), subs_known=subs_known,
                after_titles=tuple(s.get("titles", ())),
            )
        )
    return tuple(out)


class FakeVault:
    def __init__(self, boards=None, *, post=None, boards_error=None, changes=None, undo=None):
        self.boards = boards if boards is not None else []
        self.post = post
        self.boards_error = boards_error
        self.posts: list[tuple[str, str, list, str]] = []
        self.changes = changes or []
        self.undone: list[tuple[str, str]] = []
        self.undo = undo
        self.log: list[str] = []
        self.read_subs: list[bool] = []

    async def get_boards(self, subs=False):
        self.read_subs.append(subs)
        if self.boards_error:
            raise VaultError(self.boards_error)
        return self.boards

    async def post_board(self, name, if_sha256, ops, changeset):
        self.log.append("post")
        self.posts.append((name, if_sha256, ops, changeset))
        if isinstance(self.post, str):
            raise VaultError(self.post)
        return self.post or BoardPost(sha256="b" * 64, changed=True)

    async def list_changes(self):
        return self.changes

    async def undo_changeset(self, vault_ref, *, writer="claude"):
        self.undone.append((vault_ref, writer))
        return self.undo or UndoResult(restored=1, refused=0)


class FakePlanner:
    """The planner's MCP tools as board sync uses them. `refuse_cycles`
    makes `update_task` refuse blockers that would close a cycle, as the
    planner does."""

    def __init__(
        self, tasks=None, *, fail_on=None, log=None, refuse_cycles=True, states=False, refuse_states=(),
        collections=None, unknown_collections=False, refuse_moves=(),
    ):
        self.tasks = {t["id"]: dict(t) for t in (tasks or [])}
        # The workspace's collections ({id, name, columns: [{id, name, done,
        # position}]} each); None: a planner from before collections were
        # synced (no `columns`), as every older test here expects.
        self.collections = collections
        # A planner with no create_collection tool.
        self.unknown_collections = unknown_collections
        # Tasks whose move into a collection is refused.
        self.refuse_moves = set(refuse_moves)
        self.workspace_reads = 0
        # A planner with task states: every row carries one.
        self.states = states
        # Tasks the planner cannot give a state (no collection, no such column).
        self.refuse_states = set(refuse_states)
        if states:
            for row in self.tasks.values():
                row.setdefault("state", "done" if row["done"] else "planned")
        self.fail_on = fail_on or set()
        self.refuse_cycles = refuse_cycles
        self.writes: list[tuple[str, dict]] = []
        self.log = log if log is not None else []
        self.by_request: dict[str, str] = {}
        self.reads: list[str | None] = []

    def _maybe_fail(self, name):
        if name in self.fail_on:
            raise PlannerUnavailable(name)

    async def list_tasks(self, settings, session, clock, *, tag, include_completed=True, include_subtasks=False, **kw):
        self._maybe_fail("list_tasks")
        self.reads.append(tag)
        rows = [dict(t) for t in self.tasks.values() if tag is None or tag in t.get("tags", [])]
        if not include_subtasks:
            rows = [t for t in rows if t.get("parentId") is None]
        if not include_completed:
            rows = [t for t in rows if not t["done"]]
        return {"count": len(rows), "tasks": rows}

    # -- collections (the planner's own rules, lib/mcp/tools.ts) --

    def _columns(self, collection_id):
        found = next((c for c in self.collections or [] if c["id"] == collection_id), None)
        return sorted(found["columns"], key=lambda c: c["position"]) if found else []

    def _role_of(self, collection_id, name):
        cols = self._columns(collection_id)
        first_open = next((c["name"] for c in cols if not c["done"]), None)
        col = next((c for c in cols if c["name"] == name), None)
        if col is None:
            return None
        return "done" if col["done"] else ("planned" if col["name"] == first_open else "wip")

    def _first_of(self, collection_id, role):
        return next((c["name"] for c in self._columns(collection_id) if self._role_of(collection_id, c["name"]) == role), None)

    def _land(self, task_id, role, column=None):
        row = self.tasks[task_id]
        if row.get("collectionId"):
            row["column"] = column or self._first_of(row["collectionId"], role)
        if "state" in row or self.states:
            row["state"] = role
        row["done"] = role == "done"

    async def get_workspace(self, settings, session, clock):
        self._maybe_fail("get_workspace")
        self.workspace_reads += 1
        if self.collections is None:
            return None
        return tuple(
            Collection(c["id"], c["name"], tuple(Column(k["id"], k["name"], k["done"], k["position"]) for k in c["columns"]))
            for c in self.collections
        )

    async def create_collection(self, settings, session, clock, *, name, columns):
        self.log.append("create_collection")
        if self.unknown_collections:
            raise PlannerToolUnknown("no such tool")
        self.writes.append(("create_collection", {"name": name, "columns": list(columns)}))
        found = next((c for c in self.collections or [] if c["name"].lower() == name.lower()), None)
        created = found is None
        if found is None:
            n = len(self.collections or []) + 1
            found = {
                "id": f"col{n}", "name": name,
                "columns": [{"id": f"col{n}-{k}", "name": c, "done": d, "position": k} for k, (c, d) in enumerate(columns)],
            }
            self.collections = [*(self.collections or []), found]
        cols = tuple(Column(k["id"], k["name"], k["done"], k["position"]) for k in found["columns"])
        return Collection(found["id"], found["name"], cols), created

    async def create_task(
        self, settings, session, clock, *, title, due_date, client_request_id, tags=None, parent_id=None, blocked_by=None,
        collection_id=None,
    ):
        self._maybe_fail("create_task")
        self.log.append("create")
        write = {"title": title, "due": due_date, "request": client_request_id, "tags": tags}
        if parent_id is not None:
            write["parent"] = parent_id
        if collection_id is not None:
            write["collection"] = collection_id
        self.writes.append(("create", write))
        if client_request_id in self.by_request:
            task_id = self.by_request[client_request_id]
        else:
            task_id = f"new{len(self.by_request) + 1}"
            self.by_request[client_request_id] = task_id
            row = {"id": task_id, "title": title, "done": False, "tags": list(tags or []), "position": len(self.tasks)}
            if due_date:
                row["dueDate"] = due_date
            if parent_id is not None:
                row["parentId"] = parent_id
            if self.states:
                row["state"] = "planned"
            if collection_id is not None:
                row["collectionId"] = collection_id
                row["column"] = self._first_of(collection_id, "planned")
            self.tasks[task_id] = row
        return {"id": task_id, "title": title}

    def _closes_cycle(self, task_id, blockers) -> bool:
        stack, seen = list(blockers), set()
        while stack:
            current = stack.pop()
            if current == task_id:
                return True
            if current in seen:
                continue
            seen.add(current)
            stack.extend(self.tasks.get(current, {}).get("blockedBy", []))
        return False

    async def complete_task(self, settings, session, clock, *, task_id, done=None):
        self._maybe_fail("complete_task")
        self.log.append("complete")
        if task_id in self.refuse_states:
            raise PlannerToolRefused("not recorded")
        self.writes.append(("complete", {"id": task_id, "done": done}))
        done = True if done is None else done
        self.tasks[task_id]["done"] = done
        if "state" in self.tasks[task_id]:
            self.tasks[task_id]["state"] = "done" if done else "planned"
        if self.tasks[task_id].get("collectionId"):
            self._land(task_id, "done" if done else "planned")
        return {"id": task_id}

    async def update_task(
        self, settings, session, clock, *, task_id, title=None, due_date=UNSET, tags=None, blocked_by=None, state=None,
        column=None, collection_id=None,
    ):
        self._maybe_fail("update_task")
        self.log.append("update")
        args = {"id": task_id}
        if collection_id is not None:
            if task_id in self.refuse_moves:
                raise PlannerToolRefused("no move")
            args["collectionId"] = collection_id
            row = self.tasks[task_id]
            row["collectionId"] = collection_id
            self._land(task_id, "done" if row["done"] else "planned")
        if state is not None:
            if task_id in self.refuse_states:
                raise PlannerToolRefused("no column")
            args["state"] = state
            role = self._role_of(self.tasks[task_id].get("collectionId"), column) if column else None
            if column is not None:
                args["column"] = column
            self.tasks[task_id]["state"] = state
            self.tasks[task_id]["done"] = state == "done"
            if self.tasks[task_id].get("collectionId"):
                self._land(task_id, state, column if role == state else None)
        if blocked_by is not None:
            if self.refuse_cycles and self._closes_cycle(task_id, blocked_by):
                raise PlannerToolRefused("cycle")
            args["blockedBy"] = list(blocked_by)
            self.tasks[task_id]["blockedBy"] = list(blocked_by)
        if title is not None:
            args["title"] = title
            self.tasks[task_id]["title"] = title
        if due_date is not UNSET:
            args["dueDate"] = due_date
            self.tasks[task_id]["dueDate"] = due_date
        if tags is not None:
            args["tags"] = tags
            self.tasks[task_id]["tags"] = list(tags)
        self.writes.append(("update", args))
        return {"id": task_id}


def _task(task_id, title, *, done=False, due=None, tags=("work",), parent=None, position=0, blocked_by=None):
    row = {"id": task_id, "title": title, "done": done, "tags": list(tags), "position": position}
    if due:
        row["dueDate"] = due
    if parent is not None:
        row["parentId"] = parent
        row["tags"] = []
    if blocked_by is not None:
        row["blockedBy"] = list(blocked_by)
    return row


async def _link(sessionmaker, clock, *, status="active"):
    async with sessionmaker() as session:
        if await session.get(UserState, 1) is None:
            session.add(UserState(id=1, chat_id=4242, timezone=TZ))
        session.add(
            PlannerCredential(
                id=1, access_token="a", refresh_token="r",
                expires_at=clock.now_utc() + datetime.timedelta(hours=1), status=status,
            )
        )
        await session.commit()


async def _pass(sessionmaker, settings, planner, vault, clock, trigger=board_job.TRIGGER_AUTO):
    async with sessionmaker() as session:
        return await board_job.run_pass(session, settings, planner, vault, clock, trigger=trigger)


async def _rows(sessionmaker):
    async with sessionmaker() as session:
        return list((await session.execute(select(BoardPair).order_by(BoardPair.block_id))).scalars().all())


async def _runs(sessionmaker):
    async with sessionmaker() as session:
        return list((await session.execute(select(BoardSyncRun).order_by(BoardSyncRun.id))).scalars().all())


def _board(*specs, name=NAME, dates=True, sha=SHA, subs_known=False, modified=None):
    return TaskBoard(
        name=name, state="ok", sha256=sha, lanes=("Todo", "Done"), done_lane=True, dates=dates,
        cards=_cards(*specs, subs_known=subs_known), modified=modified,
    )


def _rebuild(vault: FakeVault, board: TaskBoard) -> TaskBoard:
    """The board after the fake vault's last post to it, via the merge's own model."""
    ops = next(p[2] for p in reversed(vault.posts) if p[0] == board.name)
    merged = board_job.as_board(board)
    plan = board_sync.Plan(board=board.name, units=[board_sync.Unit(ops=tuple(ops))])
    after, _, _ = board_sync.apply_to_state(merged, [], [], plan, tag="work")
    known = all(c.subs_known for c in board.cards)
    cards = tuple(
        BoardCard(
            c.n, c.id, "Todo", c.text, c.checked, c.date,
            subs=tuple(BoardSub(s.n, s.id, s.depth, s.text, s.checked, s.date) for s in c.subs),
            after=c.after, subs_known=known, after_titles=c.after_titles,
        )
        for c in after.cards
    )
    return TaskBoard(board.name, "ok", "c" * 64, board.lanes, board.done_lane, board.dates, cards)


# -- a full pass ------------------------------------------------------------------------


async def test_a_first_pass_creates_writes_and_records_in_that_order(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    log: list[str] = []
    board = _board({"text": "Новая на доске", "checked": True}, {"text": "Отчёт"})
    vault = FakeVault([board, TaskBoard("Other.md", "ok", SHA, (), False, True, ())])
    vault.log = log
    planner = FakePlanner([_task("t1", "отчёт"), _task("t2", "Из планера", due="2026-11-12")], log=log)

    result = await _pass(sessionmaker, _settings(), planner, vault, clock)

    assert result.outcome == board_job.OK
    # Creates (and the completion of a checked card's task) before the board write.
    assert log[:3] == ["create", "complete", "post"]
    (name, sha, ops, changeset) = vault.posts[0]
    assert (name, sha) == (NAME, SHA) and changeset.startswith("boards_")
    kinds = [op["op"] for op in ops]
    assert kinds == ["tag", "tag", "text", "add"]
    assert {"op": "add", "id": board_sync.mint_for_task(NAME, "t2"), "text": "Из планера", "date": "2026-11-12"} in ops
    create = planner.writes[0][1]
    assert (create["title"], create["tags"]) == ("Новая на доске", ["work"])
    assert create["request"].startswith("echo-board:")
    rows = await _rows(sessionmaker)
    assert sorted(r.task_id for r in rows) == ["new1", "t1", "t2"]
    assert all(r.board == NAME for r in rows)
    runs = await _runs(sessionmaker)
    assert [(r.board, r.outcome, r.tasks_created, r.cards_added, r.paired) for r in runs] == [(NAME, "ok", 1, 1, 1)]
    # "Other.md" is not in BOARD_SYNC_TAGS: never read further, never written.
    assert {p[0] for p in vault.posts} == {NAME}


async def test_a_second_pass_on_the_state_the_first_left_does_nothing(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    board = _board({"text": "Новая на доске"}, {"text": "Отчёт", "id": "abcd1"})
    vault = FakeVault([board])
    planner = FakePlanner([_task("t1", "Отчёт"), _task("t2", "Из планера")])
    await _pass(sessionmaker, _settings(), planner, vault, clock)
    vault.boards = [_rebuild(vault, board)]
    vault.posts.clear()
    planner.writes.clear()

    result = await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert result.outcome == board_job.OK
    assert vault.posts == [] and planner.writes == []


async def test_an_untagged_task_is_adopted_by_title_once_across_boards(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    general = _board({"text": "Отчёт"}, name="General tasks.md")
    work = _board({"text": "Отчёт"}, {"text": "Оплатить"})
    vault = FakeVault([work, general])
    planner = FakePlanner(
        [_task("u1", "отчёт", tags=()), _task("u2", "Оплатить", tags=("дом",)), _task("u3", "Старое", tags=(), done=True)]
    )

    preview = await _pass(sessionmaker, _settings(), planner, vault, clock, trigger=board_job.TRIGGER_PREVIEW)
    assert {b.name: b.counts.paired for b in preview.boards} == {"General tasks.md": 1, NAME: 0}
    assert planner.writes == [] and vault.posts == []
    # One read per pass, every task with its subtasks.
    assert planner.reads == [None]

    result = await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert result.outcome == board_job.OK
    # Boards go in name order: "General tasks.md" adopts u1, "Work tasks.md"
    # creates its own "Отчёт"; u2 has a tag of its own and is left alone.
    assert planner.tasks["u1"]["tags"] == ["general"]
    assert planner.tasks["u2"]["tags"] == ["дом"]
    assert planner.tasks["u3"]["tags"] == []
    created = sorted(w[1]["title"] for w in planner.writes if w[0] == "create")
    assert created == ["Оплатить", "Отчёт"]
    pairs = {(r.board, r.task_id) for r in await _rows(sessionmaker)}
    assert ("General tasks.md", "u1") in pairs


async def test_a_lost_tag_call_is_adopted_again_next_pass(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    board = _board({"text": "Отчёт"})
    vault = FakeVault([board])
    planner = FakePlanner([_task("u1", "Отчёт", tags=())], fail_on={"update_task"})
    result = await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert result.outcome == board_job.PLANNER_UNAVAILABLE
    assert await _rows(sessionmaker) == []

    planner.fail_on = set()
    vault.boards = [_rebuild(vault, board)]
    result = await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert result.outcome == board_job.OK
    assert planner.tasks["u1"]["tags"] == ["work"]
    assert [(r.block_id, r.task_id) for r in await _rows(sessionmaker)] == [(vault.boards[0].cards[0].id, "u1")]
    assert not any(w[0] == "create" for w in planner.writes)


async def test_planner_updates_follow_the_board_write(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    log: list[str] = []
    now = clock.now_utc()
    async with sessionmaker() as session:
        for block, task, title in (("aaaa1", "t1", "A"), ("bbbb1", "t2", "B")):
            session.add(BoardPair(board=NAME, block_id=block, task_id=task, title=title, done=False, created_at=now, updated_at=now))
        await session.commit()
    vault = FakeVault([_board({"text": "A2", "id": "aaaa1"}, {"text": "B", "id": "bbbb1"})])
    vault.log = log
    planner = FakePlanner([_task("t1", "A"), _task("t2", "B planner")], log=log)

    await _pass(sessionmaker, _settings(), planner, vault, clock)

    assert log == ["post", "update"]
    assert planner.tasks["t1"]["title"] == "A2"
    assert vault.posts[0][2] == [{"op": "text", "id": "bbbb1", "text": "B planner"}]
    assert {r.block_id: r.title for r in await _rows(sessionmaker)} == {"aaaa1": "A2", "bbbb1": "B planner"}


async def test_a_card_gone_untags_and_a_task_gone_removes(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    now = clock.now_utc()
    async with sessionmaker() as session:
        for block, task in (("aaaa1", "t1"), ("bbbb1", "t2")):
            session.add(BoardPair(board=NAME, block_id=block, task_id=task, title="X", done=False, created_at=now, updated_at=now))
        await session.commit()
    vault = FakeVault([_board({"text": "X", "id": "bbbb1"})])
    planner = FakePlanner([_task("t1", "X", tags=("work", "дом"))])

    await _pass(sessionmaker, _settings(), planner, vault, clock)

    assert vault.posts[0][2] == [{"op": "remove", "id": "bbbb1"}]
    assert planner.writes == [("update", {"id": "t1", "tags": ["дом"]})]
    assert "t1" in planner.tasks  # never deleted
    assert await _rows(sessionmaker) == []


# -- what a conflict or a failure leaves ------------------------------------------------------


async def test_a_409_changes_nothing_more_for_that_board(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    now = clock.now_utc()
    async with sessionmaker() as session:
        session.add(BoardPair(board=NAME, block_id="aaaa1", task_id="t1", title="A", done=False, created_at=now, updated_at=now))
        await session.commit()
    vault = FakeVault([_board({"text": "A2", "id": "aaaa1"}, {"text": "Новая"})], post=BoardPost(None, False, True))
    planner = FakePlanner([_task("t1", "A")])

    result = await _pass(sessionmaker, _settings(), planner, vault, clock)

    assert result.boards[0].outcome == board_job.CHANGED
    # The create ran (idempotent by its request id); the update did not.
    assert [w[0] for w in planner.writes] == ["create"]
    assert [(r.block_id, r.title) for r in await _rows(sessionmaker)] == [("aaaa1", "A")]
    runs = await _runs(sessionmaker)
    assert [(r.board, r.outcome, r.tasks_created) for r in runs] == [(NAME, "changed", 0)]

    # Next pass: the card is still untagged; the same request id brings
    # back the same task, so nothing is duplicated.
    vault.post = None
    await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert [t["title"] for t in planner.tasks.values()].count("Новая") == 1
    assert len(await _rows(sessionmaker)) == 2


async def test_a_planner_failure_aborts_quietly_and_never_touches_the_snapshot(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    vault = FakeVault([_board({"text": "Новая"})])
    planner = FakePlanner([], fail_on={"create_task"})

    result = await _pass(sessionmaker, _settings(), planner, vault, clock)

    assert result.outcome == board_job.PLANNER_UNAVAILABLE
    assert vault.posts == []
    assert await _rows(sessionmaker) == []
    assert [(r.board, r.outcome) for r in await _runs(sessionmaker)] == [(None, "planner_unavailable")]
    async with sessionmaker() as session:
        assert await session.get(PlannerSnapshot, 1) is None


async def test_a_failed_planner_update_leaves_the_old_snapshot_so_it_is_retried(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    now = clock.now_utc()
    async with sessionmaker() as session:
        session.add(BoardPair(board=NAME, block_id="aaaa1", task_id="t1", title="A", done=False, created_at=now, updated_at=now))
        await session.commit()
    vault = FakeVault([_board({"text": "A2", "id": "aaaa1"})])
    planner = FakePlanner([_task("t1", "A")], fail_on={"update_task"})

    assert (await _pass(sessionmaker, _settings(), planner, vault, clock)).outcome == board_job.PLANNER_UNAVAILABLE
    assert [(r.title) for r in await _rows(sessionmaker)] == ["A"]

    planner.fail_on = set()
    await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert planner.tasks["t1"]["title"] == "A2"
    assert vault.posts == []  # the board's change was never reverted


@pytest.mark.parametrize(("code", "outcome"), [(errors.REFUSED, "refused"), (errors.UNAVAILABLE, "unavailable")])
async def test_a_vault_failure_on_the_write_aborts_quietly(sessionmaker, frozen_clock, code, outcome):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    vault = FakeVault([_board()], post=code)
    planner = FakePlanner([_task("t1", "Из планера")])

    result = await _pass(sessionmaker, _settings(), planner, vault, clock)

    assert result.outcome == outcome
    assert await _rows(sessionmaker) == []
    assert [(r.board, r.outcome) for r in await _runs(sessionmaker)] == [(None, outcome)]


async def test_a_vault_that_does_not_answer_the_read_aborts_before_the_planner(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    planner = FakePlanner([], fail_on={"list_tasks"})
    result = await _pass(sessionmaker, _settings(), planner, FakeVault(boards_error=errors.UNAVAILABLE), clock)
    assert result.outcome == board_job.UNAVAILABLE


async def test_no_board_named_and_missing_boards_are_recorded(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    result = await _pass(sessionmaker, _settings(), FakePlanner(), FakeVault([]), clock)
    assert result.outcome == board_job.NO_BOARDS
    result = await _pass(
        sessionmaker, _settings(), FakePlanner(), FakeVault([TaskBoard(NAME, "missing"), TaskBoard("General tasks.md", "not_board")]), clock
    )
    assert result.outcome == board_job.OK
    assert [(r.board, r.outcome) for r in await _runs(sessionmaker)] == [
        (None, "no_boards"), ("General tasks.md", "not_board"), (NAME, "missing")
    ]


async def test_a_retried_create_that_answers_a_task_meant_for_a_card_does_not_duplicate(sessionmaker, frozen_clock):
    """The board write of an earlier pass was lost after its create: the
    planner now has the card's task, unrowed, with another title."""
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    vault = FakeVault([_board({"text": "Новая"})])
    planner = FakePlanner([])
    merged = board_job.as_board(vault.boards[0])
    request = board_sync.merge(merged, [], [], tag="work").creates[0].request_id
    planner.by_request[request] = "t9"
    planner.tasks["t9"] = _task("t9", "Retitled in the planner")

    await _pass(sessionmaker, _settings(), planner, vault, clock)

    ops = vault.posts[0][2]
    assert [op["op"] for op in ops] == ["tag"]  # no `add` for t9
    assert [r.task_id for r in await _rows(sessionmaker)] == ["t9"]


# -- the preview, and the gates ----------------------------------------------------------


async def test_a_preview_writes_nothing_anywhere_and_works_with_the_flag_off(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    vault = FakeVault([_board({"text": "Новая"}, {"text": "Отчёт"})])
    planner = FakePlanner([_task("t1", "Отчёт"), _task("t2", "Из планера")])

    result = await _pass(sessionmaker, _settings(BOARD_SYNC_ENABLED=False), planner, vault, clock, board_job.TRIGGER_PREVIEW)

    assert result.outcome == board_job.OK
    counts = result.counts
    assert (counts.tasks_created, counts.cards_added, counts.paired, counts.cards_tagged) == (1, 1, 1, 2)
    assert vault.posts == [] and planner.writes == []
    assert await _rows(sessionmaker) == [] and await _runs(sessionmaker) == []
    text = boards_ui.result_text(result)
    assert text.startswith("Пробный проход (ничего не записано):")
    assert "создать 1" in text and "Новая" not in text


@pytest.mark.parametrize(
    ("settings", "linked", "outcome"),
    [
        (_settings(VAULT_MODE="off"), True, board_job.VAULT_OFF),
        (_settings(BOARD_SYNC_ENABLED=False), True, board_job.DISABLED),
        (_settings(), False, board_job.NOT_LINKED),
        (_settings(PLANNER_ENABLED=False), True, board_job.NOT_LINKED),
    ],
)
async def test_a_pass_with_a_gate_shut_touches_nothing(sessionmaker, frozen_clock, settings, linked, outcome):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    if linked:
        await _link(sessionmaker, clock)
    vault = FakeVault([_board({"text": "Новая"})])
    planner = FakePlanner([], fail_on={"list_tasks"})
    result = await _pass(sessionmaker, settings, planner, vault, clock)
    assert result.outcome == outcome
    assert vault.posts == [] and await _runs(sessionmaker) == []


async def test_maybe_enqueue_is_gated_and_once_per_quarter_hour(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 1, tz=TZ)
    async with sessionmaker() as session:
        assert not await board_job.maybe_enqueue(session, _settings(), clock)  # not linked
    await _link(sessionmaker, clock)
    async with sessionmaker() as session:
        assert not await board_job.maybe_enqueue(session, _settings(BOARD_SYNC_ENABLED=False), clock)
        assert not await board_job.maybe_enqueue(session, _settings(VAULT_MODE="off"), clock)
        assert await board_job.maybe_enqueue(session, _settings(), clock)
        assert not await board_job.maybe_enqueue(session, _settings(), frozen_clock(2026, 11, 10, 9, 14, tz=TZ))
        assert await board_job.maybe_enqueue(session, _settings(), frozen_clock(2026, 11, 10, 9, 15, tz=TZ))
        jobs = (await session.execute(select(Job.kind, Job.payload))).all()
    assert jobs == [(board_job.BOARD_SYNC, {"trigger": "auto"})] * 2


async def test_the_worker_queues_a_pass_after_a_planner_sync_that_worked(sessionmaker, frozen_clock):
    from app.db.jobs import enqueue_job
    from app.worker import process_one_job

    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)

    class _Planner(FakePlanner):
        async def get_agenda(self, *a, **kw):
            return {"events": [], "tasks": []}

    async with sessionmaker() as session:
        await enqueue_job(session, "planner_sync", {})
    assert await process_one_job(sessionmaker, _settings(), None, clock, planner_client=_Planner())
    async with sessionmaker() as session:
        kinds = (await session.execute(select(Job.kind, Job.status).order_by(Job.id))).all()
    assert [k for k, _ in kinds] == ["planner_sync", board_job.BOARD_SYNC]
    async with sessionmaker() as session:
        await enqueue_job(session, "planner_sync", {})
    assert await process_one_job(sessionmaker, _settings(BOARD_SYNC_ENABLED=False), None, clock, planner_client=_Planner())
    async with sessionmaker() as session:
        assert (await session.execute(select(func.count()).select_from(Job).where(Job.kind == board_job.BOARD_SYNC))).scalar_one() == 1


# -- /boards --------------------------------------------------------------------------------


async def test_the_command_queues_preview_and_now_behind_their_gates(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)

    async def run(args, settings):
        return await boards_ui.command(sessionmaker, settings, clock, args, client_factory=lambda _s: FakeVault())

    assert await run("preview", _settings()) == boards_ui.NOT_LINKED
    await _link(sessionmaker, clock)
    assert await run("now", _settings(BOARD_SYNC_ENABLED=False)) == boards_ui.FLAG_OFF
    assert await run("preview", _settings(BOARD_SYNC_ENABLED=False)) == boards_ui.PREVIEW_QUEUED
    assert await run("preview", _settings(BOARD_SYNC_ENABLED=False)) == boards_ui.ALREADY_QUEUED
    assert await run("now", _settings()) == boards_ui.NOW_QUEUED
    assert await run("preview", _settings(VAULT_MODE="off")) == boards_ui.VAULT_OFF
    assert await run("preview please", _settings()) == boards_ui.USAGE
    async with sessionmaker() as session:
        payloads = (await session.execute(select(Job.payload).where(Job.kind == board_job.BOARD_SYNC).order_by(Job.id))).scalars().all()
    assert payloads == [{"trigger": "preview"}, {"trigger": "now"}]


async def test_status_reads_stored_state_only(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    now = clock.now_utc()
    async with sessionmaker() as session:
        text = await boards_ui.status_text(session, _settings(BOARD_SYNC_ENABLED=False))
    assert boards_ui.FLAG_OFF in text and boards_ui.NEVER_RAN in text
    async with sessionmaker() as session:
        session.add(BoardPair(board=NAME, block_id="aaaa1", task_id="t1", title=SECRET_TITLE, done=False, created_at=now, updated_at=now))
        session.add(BoardPair(board=NAME, block_id="bbbb1", task_id="t2", title="x", done=False, status="held", created_at=now, updated_at=now))
        session.add(BoardSyncRun(at=now, board=NAME, outcome="ok", conflicts=2, skipped=1))
        await session.commit()
        text = await boards_ui.status_text(session, _settings())
    assert "«Work tasks.md» ⇄ #work: пар 2 (ждут правки 1); проход 10.11 09:00 — готово, конфликтов 2, пропущено 1." in text
    assert SECRET_TITLE not in text
    async with sessionmaker() as session:
        session.add(BoardSyncRun(at=now + datetime.timedelta(minutes=15), board=None, outcome="no_boards"))
        await session.commit()
        text = await boards_ui.status_text(session, _settings())
    assert boards_ui.NO_BOARDS in text


async def test_undo_takes_back_the_newest_board_write_and_forgets_its_pairs(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    now = clock.now_utc()
    async with sessionmaker() as session:
        session.add(BoardPair(board=NAME, block_id="aaaa1", task_id="t1", title="A", done=False, created_at=now, updated_at=now))
        session.add(BoardPair(board="General tasks.md", block_id="aaaa1", task_id="t2", title="B", done=False, created_at=now, updated_at=now))
        await session.commit()
    files = [ChangeFile(path=NAME, sha256=None)]
    vault = FakeVault(
        changes=[
            ChangeEntry("boards_old", "write", now - datetime.timedelta(hours=1), False, files, "echo", "boards"),
            ChangeEntry("boards_new", "write", now, False, files, "echo", "boards"),
            ChangeEntry("streams_x", "write", now + datetime.timedelta(minutes=1), False, [], "echo", "streams"),
        ]
    )
    text = await boards_ui.command(sessionmaker, _settings(), clock, "undo", client_factory=lambda _s: vault)
    assert text == boards_ui.UNDO_REPLIES[echo_write.UNDONE]
    assert "Планер не откатывается" in text
    assert vault.undone == [("boards_new", "echo")]
    assert [r.board for r in await _rows(sessionmaker)] == ["General tasks.md"]


async def test_undo_shares_the_one_undo_call():
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("app/core/echo_write.py").read_text(encoding="utf-8"))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "undo_changeset"
    ]
    assert len(calls) == 1


# -- the digest and the logs ------------------------------------------------------------------


async def test_the_digest_line_counts_conflicts_and_the_most_titles_skipped_at_once(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    now = clock.now_utc()
    start, end = now - datetime.timedelta(days=1), now + datetime.timedelta(minutes=1)
    async with sessionmaker() as session:
        assert await boards_ui.digest_line(session, start, end) is None
        for minutes in (0, 15, 30):
            session.add(BoardSyncRun(at=now - datetime.timedelta(minutes=minutes), board=NAME, outcome="ok", skipped=1, conflicts=1 if minutes == 15 else 0))
        session.add(BoardSyncRun(at=now - datetime.timedelta(days=2), board=NAME, outcome="ok", conflicts=9))
        await session.commit()
        line = await boards_ui.digest_line(session, start, end)
    assert line == "Доски: за сутки конфликтов 1, пропущенных названий 1 — /boards."


async def test_logs_never_carry_a_title_or_a_board_name(sessionmaker, frozen_clock, caplog, monkeypatch):
    # alembic's fileConfig in the session fixture disables existing
    # loggers (tests/test_claude_privacy.py's live_loggers): re-enable.
    for name in ("app.planner.board_job", "app.core.echo_write"):
        monkeypatch.setattr(logging.getLogger(name), "disabled", False)
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    vault = FakeVault([_board({"text": SECRET_TITLE})])
    planner = FakePlanner([_task("t1", SECRET_TITLE + " 2")])
    with caplog.at_level(logging.DEBUG):
        await _pass(sessionmaker, _settings(), planner, vault, clock)
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert SECRET_TITLE not in logged and NAME not in logged and "Work tasks" not in logged
    assert "board_sync_ok" in logged


async def test_the_untagged_tasks_are_the_open_top_level_ones_with_no_tag():
    raw = {
        "tasks": [
            {"id": "a", "title": "A", "done": False},
            {"id": "b", "title": "B", "done": False, "tags": ["work"]},
            {"id": "c", "title": "C", "done": True},
            {"id": "d", "title": "D", "done": False, "parentId": "a"},
            {"id": "e", "title": "E", "done": False, "tags": [], "dueDate": "2026-11-12"},
        ]
    }
    # Cut from the pass's one parsed list, not parsed again.
    every = board_job.parse_all(raw)
    assert [(t.id, t.due) for t in board_job.untagged_of(every)] == [("a", None), ("e", "2026-11-12")]
    assert not hasattr(board_job, "parse_tasks") and not hasattr(board_job, "parse_untagged")
    with pytest.raises(PlannerToolError):
        board_job.parse_all({"tasks": [{"id": "a", "title": "A"}]})


# -- subtasks and dependencies ---------------------------------------------------------------

GENERAL = "General tasks.md"


async def _seed_pairs(sessionmaker, clock, pairs, sub_pairs=()):
    now = clock.now_utc()
    async with sessionmaker() as session:
        for board, block, task, title in pairs:
            session.add(BoardPair(board=board, block_id=block, task_id=task, title=title, done=False, created_at=now, updated_at=now))
        for board, block, card, task, title, parent in sub_pairs:
            session.add(
                BoardSubPair(
                    board=board, block_id=block, card_block_id=card, task_id=task, parent_block_id=parent,
                    title=title, done=False, created_at=now, updated_at=now,
                )
            )
        await session.commit()


async def _sub_rows(sessionmaker):
    async with sessionmaker() as session:
        return list((await session.execute(select(BoardSubPair).order_by(BoardSubPair.block_id))).scalars().all())


def _two_boards():
    work = _board(
        {"text": "Поездка", "id": "card1", "subs": [("sub1", 1, "Билеты"), (None, 2, "Поезд")],
         "after": [(GENERAL, "gen01")]},
        {"text": "Отчёт", "id": "card2"},
        subs_known=True,
    )
    general = _board({"text": "Купить чемодан", "id": "gen01"}, name=GENERAL, subs_known=True)
    return work, general


def _two_planners(**kw):
    return FakePlanner(
        [
            _task("t1", "Поездка", blocked_by=["t2", "elsewhere"]),
            _task("t2", "Отчёт"),
            _task("g1", "Купить чемодан", tags=("general",)),
            _task("s1", "Билеты", parent="t1", position=1),
            _task("s2", "Отель", parent="t1", position=2, done=True),
            _task("x9", "Чужое", tags=()),
        ],
        **kw,
    )


async def test_a_full_pass_with_subtasks_and_a_dependency_across_boards(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    await _seed_pairs(
        sessionmaker, clock,
        [(NAME, "card1", "t1", "Поездка"), (NAME, "card2", "t2", "Отчёт"), (GENERAL, "gen01", "g1", "Купить чемодан")],
        [(NAME, "sub1", "card1", "s1", "Билеты", None)],
    )
    work, general = _two_boards()
    vault = FakeVault([work, general])
    planner = _two_planners()

    preview = await _pass(sessionmaker, _settings(), planner, vault, clock, trigger=board_job.TRIGGER_PREVIEW)
    counts = {b.name: b.counts for b in preview.boards}
    assert (counts[NAME].subs_created, counts[NAME].subs_added, counts[NAME].deps_changed) == (1, 1, 1)
    assert vault.read_subs == [True] and planner.writes == [] and vault.posts == []
    text = boards_ui.result_text(preview)
    assert "подзадачи: добавить 1, изменить 0, убрать 0, создать в планере 1, отцепить 0, связать 0" in text
    assert "связи: изменить 1" in text and "Билеты" not in text

    result = await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert result.outcome == board_job.OK
    # The card's new subtask is a planner subtask of its task's subtask.
    (create,) = [w[1] for w in planner.writes if w[0] == "create"]
    assert (create["title"], create["parent"], create["tags"]) == ("Поезд", "s1", None)
    ops = {name: o for name, _sha, o, _cs in vault.posts}
    assert set(ops) == {NAME}
    kinds = [op["op"] for op in ops[NAME]]
    assert kinds == ["sub_tag", "after_set", "sub_add"]
    after = next(op for op in ops[NAME] if op["op"] == "after_set")
    assert after["links"] == [
        {"board": GENERAL, "id": "gen01", "label": "Купить чемодан"},
        {"board": NAME, "id": "card2", "label": "Отчёт"},
    ]
    add = next(op for op in ops[NAME] if op["op"] == "sub_add")
    assert (add["text"], add["checked"], "parent" in add) == ("Отель", True, False)
    # The planner keeps the blocker no board shows.
    assert sorted(planner.tasks["t1"]["blockedBy"]) == ["elsewhere", "g1", "t2"]
    subs = await _sub_rows(sessionmaker)
    assert sorted((s.task_id, s.parent_block_id) for s in subs) == [("new1", "sub1"), ("s1", None), ("s2", None)]
    rows = {r.block_id: r for r in await _rows(sessionmaker)}
    assert rows["card1"].deps == ["g1", "t2"]
    runs = {r.board: r for r in await _runs(sessionmaker)}
    assert (runs[NAME].subs_created, runs[NAME].subs_added, runs[NAME].deps_changed) == (1, 1, 1)

    # And the next pass on what this one left does nothing.
    vault.boards = [_rebuild(vault, work), general]
    vault.posts.clear()
    planner.writes.clear()
    result = await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert result.outcome == board_job.OK
    assert vault.posts == [] and planner.writes == []


async def test_a_lost_blockers_call_is_retried_and_a_refused_cycle_is_undone_on_the_card(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    await _seed_pairs(sessionmaker, clock, [(NAME, "card1", "t1", "A"), (NAME, "card2", "t2", "B")])
    board = _board({"text": "A", "id": "card1", "after": [(NAME, "card2")]}, {"text": "B", "id": "card2"}, subs_known=True)
    vault = FakeVault([board])
    planner = FakePlanner([_task("t1", "A"), _task("t2", "B", blocked_by=["t1"])], fail_on={"update_task"})

    # The call fails: the pass stops, the snapshot was never written.
    assert (await _pass(sessionmaker, _settings(), planner, vault, clock)).outcome == board_job.PLANNER_UNAVAILABLE
    assert {r.block_id: r.deps for r in await _rows(sessionmaker)}["card1"] is None

    # Back: t1 after t2 would close t2 after t1, and the planner says no. The
    # board write already gave card2 its link to card1, so the loop is on the
    # board: a conflict, skipped, and the snapshot stored as planned.
    planner.fail_on = set()
    vault.boards = [_rebuild(vault, board)] if vault.posts else [board]
    posts = len(vault.posts)
    result = await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert result.outcome == board_job.OK
    assert result.boards[0].counts.skipped >= 1 and result.boards[0].counts.conflicts >= 1
    assert "blockedBy" not in planner.tasks["t1"]
    assert {r.block_id: r.deps for r in await _rows(sessionmaker)}["card1"] == ["t2"]

    # The next pass reads the planner as having moved: card1's link comes off.
    if len(vault.posts) > posts:
        vault.boards = [_rebuild(vault, vault.boards[0])]
    posts = len(vault.posts)
    planner.writes.clear()
    result = await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert result.outcome == board_job.OK
    (_name, _sha, ops, _cs) = vault.posts[-1]
    assert ops == [{"op": "after_set", "id": "card1", "links": []}]
    assert planner.writes == []
    assert {r.block_id: r.deps for r in await _rows(sessionmaker)} == {"card1": [], "card2": ["t1"]}

    # And then nothing: converged, the planner's dependency on both sides.
    vault.boards = [_rebuild(vault, vault.boards[0])]
    posts = len(vault.posts)
    result = await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert result.outcome == board_job.OK
    assert len(vault.posts) == posts and planner.writes == []
    assert planner.tasks["t2"]["blockedBy"] == ["t1"] and "blockedBy" not in planner.tasks["t1"]
    assert [c.after for c in vault.boards[0].cards] == [(), ((NAME, "card1"),)]


async def test_an_older_vaultd_without_subtasks_touches_no_subtask(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    await _seed_pairs(
        sessionmaker, clock, [(NAME, "card1", "t1", "Поездка")], [(NAME, "sub1", "card1", "s1", "Билеты", None)]
    )
    # The old shape: no `subs`, no `after` on any card.
    vault = FakeVault([_board({"text": "Поездка", "id": "card1"})])
    planner = FakePlanner(
        [_task("t1", "Поездка", blocked_by=["t2"]), _task("t2", "Отчёт"), _task("s1", "Билеты", parent="t1"), _task("s2", "Отель", parent="t1")]
    )
    result = await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert result.outcome == board_job.OK
    assert vault.read_subs == [True]
    # The new tagged task t2 still becomes a card as before; nothing else.
    assert [op["op"] for op in vault.posts[0][2]] == ["add"]
    assert not any("blockedBy" in w[1] or "parent" in w[1] for w in planner.writes)
    subs = await _sub_rows(sessionmaker)
    assert [(s.block_id, s.status) for s in subs] == [("sub1", "ok")]
    assert {r.block_id: r.deps for r in await _rows(sessionmaker)}["card1"] is None


async def test_a_subtask_gone_from_the_card_is_detached_and_the_planner_keeps_it(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    await _seed_pairs(
        sessionmaker, clock, [(NAME, "card1", "t1", "Поездка")], [(NAME, "sub1", "card1", "s1", "Билеты", None)]
    )
    vault = FakeVault([_board({"text": "Поездка", "id": "card1"}, subs_known=True)])
    planner = FakePlanner([_task("t1", "Поездка"), _task("s1", "Билеты", parent="t1")])
    result = await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert result.boards[0].counts.subs_detached == 1
    assert vault.posts == [] and planner.writes == [] and "s1" in planner.tasks
    assert [(s.block_id, s.status) for s in await _sub_rows(sessionmaker)] == [("sub1", "detached")]
    await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert vault.posts == [] and planner.writes == []


async def test_forgetting_a_board_forgets_its_subtask_pairs(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _seed_pairs(
        sessionmaker, clock, [(NAME, "card1", "t1", "A")], [(NAME, "sub1", "card1", "s1", "Б", None), (GENERAL, "sub2", "c", "s2", "В", None)]
    )
    async with sessionmaker() as session:
        await board_job.forget_boards(session, [NAME])
    assert [s.board for s in await _sub_rows(sessionmaker)] == [GENERAL]


async def test_list_rows_are_read_strictly_with_positions_and_blockers():
    raw = {"tasks": [{"id": "a", "title": "A", "done": False, "position": 2.5, "blockedBy": ["b", "b"], "parentId": "p"}]}
    (task,) = board_job.parse_all(raw)
    assert (task.parent_id, task.position, task.blocked_by) == ("p", 2.5, ("b",))
    for bad in ({"position": "1"}, {"position": True}, {"blockedBy": "b"}, {"blockedBy": [1]}, {"parentId": 3}):
        with pytest.raises(PlannerToolError):
            board_job.parse_all({"tasks": [{"id": "a", "title": "A", "done": False, **bad}]})


async def test_an_older_planner_without_positions_touches_no_subtask(sessionmaker, frozen_clock):
    """An older planner would drop `parentId` and send no `blockedBy`."""
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    await _seed_pairs(sessionmaker, clock, [(NAME, "card1", "t1", "Поездка"), (NAME, "card2", "t2", "Отчёт")])
    board = _board(
        {"text": "Поездка", "id": "card1", "subs": [(None, 1, "Билеты")], "after": [(NAME, "card2")]},
        {"text": "Отчёт", "id": "card2"},
        subs_known=True,
    )
    vault = FakeVault([board])
    rows = [_task("t1", "Поездка"), _task("t2", "Отчёт")]
    for row in rows:
        del row["position"]
    planner = FakePlanner(rows)
    result = await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert result.outcome == board_job.OK
    assert vault.posts == [] and planner.writes == []
    assert await _sub_rows(sessionmaker) == []


async def test_subtask_pairings_are_counted_apart_from_card_pairings(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    await _seed_pairs(sessionmaker, clock, [(NAME, "card1", "t1", "Поездка")])
    board = _board({"text": "Поездка", "id": "card1", "subs": [(None, 1, "Билеты")]}, subs_known=True)
    vault = FakeVault([board])
    planner = FakePlanner([_task("t1", "Поездка"), _task("s1", "Билеты", parent="t1")])
    result = await _pass(sessionmaker, _settings(), planner, vault, clock)
    counts = result.boards[0].counts
    assert (counts.paired, counts.subs_paired) == (0, 1)
    (run,) = [r for r in await _runs(sessionmaker) if r.board == NAME]
    assert (run.paired, run.subs_paired) == (0, 1)
    text = boards_ui.result_text(result)
    assert "отцеплено 0, связано 1" in text and "пар по названию 0" in text


async def test_an_unwritable_after_line_is_tracked_apart_and_settles(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    await _seed_pairs(sessionmaker, clock, [(NAME, "card1", "t1", "A"), (NAME, "card2", "t2", "B"), (NAME, "card3", "t3", "C")])
    cards = _cards(
        {"text": "A", "id": "card1", "after": [(NAME, "card2")]}, {"text": "B", "id": "card2"}, {"text": "C", "id": "card3"},
        subs_known=True,
    )
    cards = (replace(cards[0], after_raw_extra=True), *cards[1:])
    board = TaskBoard(NAME, "ok", SHA, ("Todo", "Done"), True, True, cards)
    vault = FakeVault([board])
    planner = FakePlanner([_task("t1", "A", blocked_by=["t3"]), _task("t2", "B"), _task("t3", "C")])
    await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert vault.posts == [] and sorted(planner.tasks["t1"]["blockedBy"]) == ["t2", "t3"]
    rows = {r.block_id: r for r in await _rows(sessionmaker)}
    assert (rows["card1"].deps, rows["card1"].deps_card) == (["t2", "t3"], ["t2"])
    # The union is not sent again; the user's removal in the planner stays.
    planner.writes.clear()
    planner.tasks["t1"]["blockedBy"] = ["t3"]
    for _ in range(2):
        await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert planner.writes == [] and vault.posts == [] and planner.tasks["t1"]["blockedBy"] == ["t3"]


async def test_a_title_on_the_after_line_becomes_a_link_and_a_blocker(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    await _seed_pairs(sessionmaker, clock, [(NAME, "card1", "t1", "Поездка"), (GENERAL, "gen01", "g1", "Купить чемодан")])
    work = _board({"text": "Поездка", "id": "card1", "titles": ["купить чемодан"]}, subs_known=True)
    general = _board({"text": "Купить чемодан", "id": "gen01"}, name=GENERAL, subs_known=True)
    vault = FakeVault([work, general])
    planner = FakePlanner([_task("t1", "Поездка"), _task("g1", "Купить чемодан", tags=("general",))])

    result = await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert result.outcome == board_job.OK
    ops = {name: o for name, _sha, o, _cs in vault.posts}
    assert ops == {NAME: [{"op": "after_set", "id": "card1", "links": [{"board": GENERAL, "id": "gen01", "label": "Купить чемодан"}]}]}
    assert ("update", {"id": "t1", "blockedBy": ["g1"]}) in planner.writes
    rows = {r.block_id: r for r in await _rows(sessionmaker)}
    assert (rows["card1"].deps, rows["card1"].deps_card) == (["g1"], None)

    # The line is links now: the next pass does nothing.
    vault.boards = [_rebuild(vault, work), general]
    assert vault.boards[0].cards[0].after_titles == () and vault.boards[0].cards[0].after == ((GENERAL, "gen01"),)
    vault.posts.clear()
    planner.writes.clear()
    result = await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert result.outcome == board_job.OK
    assert vault.posts == [] and planner.writes == []


# -- the planner's button ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("trigger", "guarded"),
    [
        (board_job.TRIGGER_AUTO, True),
        (board_job.TRIGGER_PREVIEW, True),
        (board_job.TRIGGER_PLANNER, True),
        (board_job.TRIGGER_NOW, False),
    ],
)
async def test_only_boards_now_runs_past_the_mass_loss_guard(sessionmaker, frozen_clock, monkeypatch, trigger, guarded):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    seen = []

    async def fake_read_and_plan(*_args, guard=True, **_kwargs):
        seen.append(guard)
        return []

    monkeypatch.setattr(board_job, "_read_and_plan", fake_read_and_plan)
    await _pass(sessionmaker, _settings(), FakePlanner(), FakeVault(), clock, trigger=trigger)
    assert seen == [guarded]


async def test_the_planner_trigger_queues_once_a_minute_and_says_where_it_came_from(sessionmaker, frozen_clock):
    assert board_job.TRIGGER_PLANNER in board_job.TRIGGERS
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    async with sessionmaker() as session:
        assert await board_job.enqueue_command(session, clock, board_job.TRIGGER_PLANNER)
        assert not await board_job.enqueue_command(session, clock, board_job.TRIGGER_PLANNER)
        job = (await session.execute(select(Job).where(Job.kind == board_job.BOARD_SYNC))).scalar_one()
    assert job.payload == {"trigger": "planner"}
    assert job.dedup_key == board_job.command_dedup_key(clock.now_utc(), board_job.TRIGGER_PLANNER)
    with pytest.raises(ValueError):
        async with sessionmaker() as session:
            await board_job.enqueue_command(session, clock, board_job.TRIGGER_AUTO)
    result = board_job.PassResult(trigger=board_job.TRIGGER_PLANNER, outcome=board_job.OK)
    result.boards.append(board_job.BoardResult(NAME, board_job.OK, board_sync.Counts(cards_changed=2)))
    assert boards_ui.result_text(result).startswith("Проход по кнопке в планере:\n")


async def test_a_board_edited_moments_ago_gets_no_write_but_its_changes_reach_the_planner(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    now = int(clock.now_utc().timestamp())
    async with sessionmaker() as session:
        for block, task, title in (("aaaa1", "t1", "A"), ("bbbb1", "t2", "B")):
            session.add(BoardPair(board=NAME, block_id=block, task_id=task, title=title, done=False, created_at=clock.now_utc(), updated_at=clock.now_utc()))
        await session.commit()
    # Card A retitled on the board (goes to the planner); task B retitled in
    # the planner (would be written to the board); a new planner task (an add).
    board = _board({"text": "A2", "id": "aaaa1"}, {"text": "B", "id": "bbbb1"}, modified=now - 30)
    vault = FakeVault([board])
    planner = FakePlanner([_task("t1", "A"), _task("t2", "B planner"), _task("t3", "Новая")])

    result = await _pass(sessionmaker, _settings(), planner, vault, clock)

    assert result.outcome == board_job.OK
    assert [b.outcome for b in result.boards] == [board_job.BUSY]
    assert vault.posts == []
    assert planner.tasks["t1"]["title"] == "A2"
    rows = {r.block_id: r.title for r in await _rows(sessionmaker)}
    assert rows == {"aaaa1": "A2", "bbbb1": "B"}
    runs = await _runs(sessionmaker)
    # The run counts what ran (the retitle carried to the planner) and the
    # rest as waiting.
    assert [(r.outcome, r.waiting, r.tasks_changed, r.cards_changed, r.cards_added) for r in runs] == [("busy", 2, 1, 0, 0)]

    # Quiet again: the held writes go out.
    vault.boards = [_board({"text": "A2", "id": "aaaa1"}, {"text": "B", "id": "bbbb1"}, modified=now - 600)]
    result = await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert [b.outcome for b in result.boards] == [board_job.OK]
    kinds = sorted(op["op"] for op in vault.posts[0][2])
    assert kinds == ["add", "text"]


async def test_a_board_with_no_file_time_is_never_busy(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    vault = FakeVault([_board({"text": "Новая"}, modified=None)])
    result = await _pass(sessionmaker, _settings(), FakePlanner([]), vault, clock)
    assert [b.outcome for b in result.boards] == [board_job.OK] and vault.posts
# -- lanes by role ------------------------------------------------------------------------

LANES = ("Todo", "Doing", "Done")


def _lane_board(*specs, lanes=LANES, known=True):
    """A board from a vaultd that names its done lane (`known`)."""
    return TaskBoard(
        name=NAME, state="ok", sha256=SHA, lanes=lanes, done_lane="Done" in lanes, dates=True,
        cards=_cards(*specs, subs_known=True),
        done_lane_title="Done" if known and "Done" in lanes else None,
        lane_complete=tuple(t == "Done" for t in lanes) if known else (),
        lanes_known=known,
    )


def _rebuild_lanes(vault: FakeVault, board: TaskBoard) -> TaskBoard:
    """The board after the fake vault's last post, lanes and all, via the merge's model."""
    ops = next(p[2] for p in reversed(vault.posts) if p[0] == board.name)
    plan = board_sync.Plan(board=board.name, units=[board_sync.Unit(ops=tuple(ops))])
    after, _, _ = board_sync.apply_to_state(board_job.as_board(board), [], [], plan, tag="work")
    cards = tuple(
        BoardCard(c.n, c.id, c.lane, c.text, c.checked, c.date, subs_known=True) for c in after.cards
    )
    return replace(board, sha256="c" * 64, cards=cards)


async def test_a_pass_carries_lanes_as_states_both_ways(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    await _seed_pairs(sessionmaker, clock, [(NAME, "card1", "t1", "Отчёт"), (NAME, "card2", "t2", "Смета")])
    log: list[str] = []
    board = _lane_board(
        {"text": "Отчёт", "id": "card1", "lane": "Doing"},  # the user moved it
        {"text": "Смета", "id": "card2"},
        {"text": "Новая", "lane": "Doing"},
    )
    vault = FakeVault([board])
    vault.log = log
    planner = FakePlanner(
        [_task("t1", "Отчёт"), {**_task("t2", "Смета"), "state": "wip"}, {**_task("t3", "Из планера"), "state": "wip"}],
        states=True,
        log=log,
    )

    preview = await _pass(sessionmaker, _settings(), planner, vault, clock, trigger=board_job.TRIGGER_PREVIEW)
    assert "колонки: перенести 4" in boards_ui.result_text(preview)

    result = await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert result.outcome == board_job.OK
    # The new card's task is made in progress by its unit's call, after the board write.
    assert log[:2] == ["create", "post"]
    assert ("update", {"id": "new1", "state": "wip"}) in planner.writes
    (_name, _sha, ops, _cs) = vault.posts[0]
    added = board_sync.mint_for_task(NAME, "t3")
    assert [op for op in ops if op["op"] in ("add", "move")] == [
        {"op": "add", "id": added, "text": "Из планера"},
        {"op": "move", "id": "card2", "lane": "Doing"},
        {"op": "move", "id": added, "lane": "Doing"},
    ]
    assert ("update", {"id": "t1", "state": "wip"}) in planner.writes
    assert {t["id"]: t["state"] for t in planner.tasks.values()} == {"t1": "wip", "t2": "wip", "t3": "wip", "new1": "wip"}
    rows = {r.task_id: r for r in await _rows(sessionmaker)}
    assert {k: (r.state, r.done) for k, r in rows.items()} == {
        "t1": ("wip", False), "t2": ("wip", False), "t3": ("wip", False), "new1": ("wip", False)
    }
    (run,) = [r for r in await _runs(sessionmaker) if r.board == NAME]
    assert run.states_changed == 4
    assert "колонки: перенесено 4" in boards_ui.result_text(result)

    # The next pass on what this one left does nothing.
    vault.boards = [_rebuild_lanes(vault, board)]
    vault.posts.clear()
    planner.writes.clear()
    assert (await _pass(sessionmaker, _settings(), planner, vault, clock)).outcome == board_job.OK
    assert vault.posts == [] and planner.writes == []

    # A task completed in the planner: the card is checked into the done lane;
    # reopened in progress: one move out of it.
    planner.tasks["t1"].update(done=True, state="done")
    await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert vault.posts[-1][2] == [{"op": "check", "id": "card1", "checked": True}]
    vault.boards = [_rebuild_lanes(vault, vault.boards[0])]
    planner.tasks["t1"].update(done=False, state="wip")
    await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert vault.posts[-1][2] == [{"op": "move", "id": "card1", "lane": "Doing"}]


async def test_a_card_out_of_the_done_lane_reopens_its_task_in_one_state_call(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    now = clock.now_utc()
    async with sessionmaker() as session:
        session.add(BoardPair(board=NAME, block_id="card1", task_id="t1", title="A", done=True, state="done",
                              created_at=now, updated_at=now))
        await session.commit()
    vault = FakeVault([_lane_board({"text": "A", "id": "card1", "lane": "Doing"})])
    planner = FakePlanner([_task("t1", "A", done=True)], states=True)
    await _pass(sessionmaker, _settings(), planner, vault, clock)
    # The planner's state move clears the completion itself.
    assert planner.writes == [("update", {"id": "t1", "state": "wip"})]
    assert planner.tasks["t1"]["done"] is False
    (row,) = await _rows(sessionmaker)
    assert (row.state, row.done) == ("wip", False)


@pytest.mark.parametrize("older", ["planner", "vaultd"])
async def test_an_older_side_leaves_lanes_alone(sessionmaker, frozen_clock, older):
    """A planner whose rows carry no `state`, or a vaultd that does not
    name its done lane (and so has no `move`): done and open only."""
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    await _seed_pairs(sessionmaker, clock, [(NAME, "card1", "t1", "Отчёт")])
    vault = FakeVault([_lane_board({"text": "Отчёт", "id": "card1", "lane": "Doing"}, known=older != "vaultd")])
    planner = FakePlanner([_task("t1", "Отчёт"), _task("t2", "Новая")], states=older != "planner")
    if older == "vaultd":
        planner.tasks["t2"]["state"] = "wip"
    result = await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert result.outcome == board_job.OK
    assert all(op["op"] != "move" for _n, _s, ops, _c in vault.posts for op in ops)
    assert all("state" not in w[1] for w in planner.writes)
    assert result.boards[0].counts.states_changed == 0
    assert all(r.state is None for r in await _rows(sessionmaker))


async def test_list_rows_read_a_state_strictly_and_say_whether_all_carry_one():
    raw = {"tasks": [{"id": "a", "title": "A", "done": False, "position": 0, "state": "wip"}]}
    (task,) = board_job.parse_all(raw)
    assert task.state == "wip" and board_job.knows_states(raw)
    (task,) = board_job.parse_all({"tasks": [{"id": "a", "title": "A", "done": False}]})
    assert task.state is None
    mixed = {"tasks": [{"id": "a", "title": "A", "done": False, "state": "planned"}, {"id": "b", "title": "B", "done": False}]}
    assert not board_job.knows_states(mixed) and board_job.knows_states({"tasks": []})
    for bad in ("blocked", None, 1, "WIP"):
        with pytest.raises(PlannerToolError):
            board_job.parse_all({"tasks": [{"id": "a", "title": "A", "done": False, "state": bad}]})


async def test_the_board_carries_its_lanes_into_the_merge():
    merged = board_job.as_board(_lane_board({"text": "A", "lane": "Doing"}))
    assert (merged.lanes, merged.done_lane_title, merged.lane_complete, merged.lanes_known) == (
        LANES, "Done", (False, False, True), True
    )
    assert merged.cards[0].lane == "Doing"
    old = board_job.as_board(_lane_board({"text": "A"}, known=False))
    assert (old.done_lane_title, old.lane_complete, old.lanes_known) == (None, (), False)


async def test_a_state_the_planner_refuses_is_a_conflict_and_the_card_goes_back(sessionmaker, frozen_clock):
    """A task in no collection has no in-progress column: the planner
    refuses the state, the rest of the pass goes on, and the next pass
    puts the card back in the planner's lane instead of retrying."""
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    await _seed_pairs(sessionmaker, clock, [(NAME, "card1", "t1", "Отчёт")])
    board = _lane_board({"text": "Отчёт v2", "id": "card1", "lane": "Doing"}, {"text": "Новая", "lane": "Doing"})
    vault = FakeVault([board])
    planner = FakePlanner([_task("t1", "Отчёт")], states=True, refuse_states={"t1", "new1"})
    result = await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert result.outcome == board_job.OK
    assert planner.tasks["t1"]["title"] == "Отчёт v2" and planner.tasks["t1"]["state"] == "planned"
    counts = result.boards[0].counts
    assert (counts.conflicts, counts.skipped) == (2, 2)
    rows = {r.task_id: r.state for r in await _rows(sessionmaker)}
    assert rows == {"t1": "wip", "new1": "wip"}

    vault.boards = [_rebuild_lanes(vault, board)]
    vault.posts.clear()
    planner.writes.clear()
    await _pass(sessionmaker, _settings(), planner, vault, clock)
    new_card = next(c.id for c in vault.boards[0].cards if c.text == "Новая")
    assert sorted(vault.posts[0][2], key=lambda op: op["id"]) == sorted(
        [{"op": "move", "id": "card1", "lane": "Todo"}, {"op": "move", "id": new_card, "lane": "Todo"}], key=lambda op: op["id"]
    )
    assert planner.writes == []
    vault.boards = [_rebuild_lanes(vault, vault.boards[0])]
    vault.posts.clear()
    await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert vault.posts == [] and planner.writes == []


async def test_a_new_cards_state_call_runs_after_the_board_write_and_only_for_a_kept_unit(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    log: list[str] = []
    board = _lane_board({"text": "Новая", "lane": "Doing"})
    vault = FakeVault([board])
    vault.log = log
    planner = FakePlanner([], states=True, log=log)
    await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert log == ["create", "post", "update"]
    (row,) = await _rows(sessionmaker)
    assert (row.task_id, row.state) == ("new1", "wip")

    # A retried create whose task is already paired (a pass lost its tag
    # write): the unit keeps only its ops, and no state call is made.
    block = board_sync.mint_for_card(NAME, "Другая", 0)
    planner.by_request[board_sync.request_id(NAME, block)] = "new1"
    vault.boards = [_lane_board({"text": "Новая", "id": row.block_id, "lane": "Doing"}, {"text": "Другая", "lane": "Doing"})]
    planner.writes.clear()
    log.clear()
    await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert [w[0] for w in planner.writes] == ["create"]
    assert [r.task_id for r in await _rows(sessionmaker)] == ["new1"]


async def test_a_refused_state_takes_its_change_back_from_the_counts(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    await _seed_pairs(sessionmaker, clock, [(NAME, "card1", "t1", "Отчёт")])
    vault = FakeVault([_lane_board({"text": "Отчёт", "id": "card1", "lane": "Doing"})])
    planner = FakePlanner([_task("t1", "Отчёт")], states=True, refuse_states={"t1"})
    result = await _pass(sessionmaker, _settings(), planner, vault, clock)
    counts = result.boards[0].counts
    assert (counts.states_changed, counts.tasks_changed, counts.conflicts, counts.skipped) == (0, 0, 1, 1)
    (run,) = [r for r in await _runs(sessionmaker) if r.board == NAME]
    assert (run.states_changed, run.tasks_changed) == (0, 0)


async def test_a_mass_state_change_is_held_shown_in_the_preview_and_run_by_hand(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    await _seed_pairs(sessionmaker, clock, [(NAME, f"card{i}", f"t{i}", f"К{i}") for i in range(5)])
    vault = FakeVault([_lane_board(*({"text": f"К{i}", "id": f"card{i}", "lane": "Doing"} for i in range(5)))])
    planner = FakePlanner([_task(f"t{i}", f"К{i}") for i in range(5)], states=True)

    preview = await _pass(sessionmaker, _settings(), planner, vault, clock, trigger=board_job.TRIGGER_PREVIEW)
    text = boards_ui.result_text(preview)
    assert "колонки: перенести 0" in text and "придержано переносов между колонками 5" in text
    assert "придержано удалений" not in text

    result = await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert vault.posts == [] and planner.writes == []
    (run,) = [r for r in await _runs(sessionmaker) if r.board == NAME]
    assert (run.held, run.states_held, run.states_changed) == (5, 5, 0)
    assert all(r.state is None for r in await _rows(sessionmaker))

    result = await _pass(sessionmaker, _settings(), planner, vault, clock, trigger=board_job.TRIGGER_NOW)
    assert result.boards[0].counts.states_changed == 5
    assert {t["state"] for t in planner.tasks.values()} == {"wip"}


# -- collection per board, columns by name ------------------------------------------------------

NLANES = ("Todo", "Doing", "Waiting", "Done")


def _work_collection(*, name="Work tasks", cid="col1"):
    return {
        "id": cid, "name": name,
        "columns": [
            {"id": f"{cid}-0", "name": "Todo", "done": False, "position": 0},
            {"id": f"{cid}-1", "name": "Doing", "done": False, "position": 1},
            {"id": f"{cid}-2", "name": "Waiting", "done": False, "position": 2},
            {"id": f"{cid}-3", "name": "Done", "done": True, "position": 3},
        ],
    }


async def _seed_lane_pairs(sessionmaker, clock, pairs):
    """(block, task, title, state, lane) each, on NAME."""
    now = clock.now_utc()
    async with sessionmaker() as session:
        for block, task, title, state, lane in pairs:
            session.add(BoardPair(board=NAME, block_id=block, task_id=task, title=title, done=state == "done",
                                  state=state, lane=lane, created_at=now, updated_at=now))
        await session.commit()


async def test_a_first_pass_makes_the_collection_and_moves_its_tasks_into_it(sessionmaker, frozen_clock, caplog):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    # Production: a card in Doing whose task is in no collection, its state
    # refused last time (the snapshot says wip, the planner planned).
    await _seed_lane_pairs(sessionmaker, clock, [("card1", "t1", "Отчёт", "wip", None)])
    log: list[str] = []
    board = _lane_board(
        {"text": "Отчёт", "id": "card1", "lane": "Doing"},
        {"text": "Новая", "lane": "Waiting"},
        lanes=NLANES,
    )
    vault = FakeVault([board])
    vault.log = log
    planner = FakePlanner([_task("t1", "Отчёт")], states=True, collections=[], log=log)

    preview = await _pass(sessionmaker, _settings(), planner, vault, clock, trigger=board_job.TRIGGER_PREVIEW)
    text = boards_ui.result_text(preview)
    assert "коллекция: создать 1" in text and "задач перенести в коллекцию 1" in text
    assert planner.writes == [] and planner.collections == [] and vault.posts == []
    assert await _runs(sessionmaker) == []

    caplog.set_level(logging.DEBUG)
    result = await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert result.outcome == board_job.OK
    # The collection first, from the board's lanes, then the create in it.
    assert log[:3] == ["create_collection", "create", "post"]
    assert planner.writes[0] == (
        "create_collection",
        {"name": "Work tasks", "columns": [("Todo", False), ("Doing", False), ("Waiting", False), ("Done", True)]},
    )
    (create,) = [w[1] for w in planner.writes if w[0] == "create"]
    assert create["collection"] == "col1"
    updates = [w[1] for w in planner.writes if w[0] == "update"]
    # t1: moved into the collection before its column call; the new card's
    # task goes to the column of its lane.
    assert updates.index({"id": "t1", "collectionId": "col1"}) < updates.index({"id": "t1", "state": "wip", "column": "Doing"})
    assert {"id": "new1", "state": "wip", "column": "Waiting"} in updates
    assert {t["id"]: (t["collectionId"], t["column"]) for t in planner.tasks.values()} == {
        "t1": ("col1", "Doing"), "new1": ("col1", "Waiting")
    }
    rows = {r.task_id: r for r in await _rows(sessionmaker)}
    assert {k: (r.state, r.lane) for k, r in rows.items()} == {"t1": ("wip", "Doing"), "new1": ("wip", "Waiting")}
    (run,) = [r for r in await _runs(sessionmaker) if r.board == NAME]
    assert (run.collections_created, run.tasks_moved) == (1, 1)
    text = boards_ui.result_text(result)
    assert "коллекция: создано 1" in text and "задач перенесено в коллекцию 1" in text
    logged = " ".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert "Work tasks" not in logged and "Waiting" not in logged and "Отчёт" not in logged

    # The next pass finds the collection by name and does nothing.
    vault.boards = [_rebuild_lanes(vault, board)]
    vault.posts.clear()
    planner.writes.clear()
    assert (await _pass(sessionmaker, _settings(), planner, vault, clock)).outcome == board_job.OK
    assert vault.posts == [] and planner.writes == [] and len(planner.collections) == 1

    # Moves between two lanes of the same role now travel, both ways.
    vault.boards = [replace(vault.boards[0], cards=tuple(
        replace(c, lane="Waiting") if c.id == "card1" else c for c in vault.boards[0].cards
    ))]
    await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert planner.writes == [("update", {"id": "t1", "state": "wip", "column": "Waiting"})]
    planner.tasks["t1"]["column"] = "Doing"
    await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert vault.posts[-1][2] == [{"op": "move", "id": "card1", "lane": "Doing"}]
    rows = {r.task_id: r for r in await _rows(sessionmaker)}
    assert rows["t1"].lane == "Doing"


async def test_a_collection_of_that_name_is_found_not_made(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    vault = FakeVault([_lane_board({"text": "Новая"}, lanes=NLANES)])
    planner = FakePlanner([], states=True, collections=[_work_collection(name="WORK TASKS", cid="mine")])
    result = await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert result.outcome == board_job.OK and result.boards[0].counts.collections_created == 0
    assert not any(w[0] == "create_collection" for w in planner.writes)
    assert [w[1]["collection"] for w in planner.writes if w[0] == "create"] == ["mine"]
    assert planner.workspace_reads == 1


async def test_a_board_with_no_collection_named_or_an_older_planner_syncs_as_before(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    for settings, planner in (
        (_settings(BOARD_SYNC_COLLECTIONS="{}"), FakePlanner([], states=True, collections=[])),
        (_settings(), FakePlanner([], states=True)),  # collections with no columns
        (_settings(), FakePlanner([], states=True, collections=[], unknown_collections=True)),
    ):
        vault = FakeVault([_lane_board({"text": "Новая", "lane": "Doing"}, lanes=NLANES)])
        result = await _pass(sessionmaker, settings, planner, vault, clock)
        assert result.outcome == board_job.OK
        (create,) = [w[1] for w in planner.writes if w[0] == "create"]
        assert "collection" not in create
        assert ("update", {"id": "new1", "state": "wip"}) in planner.writes
        assert result.boards[0].counts.collections_created == 0
        async with sessionmaker() as session:
            await session.execute(BoardPair.__table__.delete())
            await session.commit()
    # With no collection named, get_workspace is not even read.
    planner = FakePlanner([], states=True, collections=[])
    await _pass(sessionmaker, _settings(BOARD_SYNC_COLLECTIONS="{}"), planner, FakeVault([_lane_board(lanes=NLANES)]), clock)
    assert planner.workspace_reads == 0


async def test_a_refused_completion_or_move_is_a_skip_never_an_abort(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    # An older planner (no collections): a task in no collection cannot be
    # completed (the planner's trigger), the card checked anyway.
    await _seed_lane_pairs(sessionmaker, clock, [("card1", "t1", "A", "planned", None)])
    vault = FakeVault([_lane_board({"text": "A", "id": "card1", "lane": "Done", "checked": True}, {"text": "Б", "checked": True},
                                   lanes=NLANES)])
    planner = FakePlanner([_task("t1", "A")], states=True, refuse_states={"t1"})
    real_create = planner.create_task

    async def create(*a, **kw):
        answer = await real_create(*a, **kw)
        planner.refuse_states.add(answer["id"])
        return answer

    planner.create_task = create
    result = await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert result.outcome == board_job.OK
    counts = result.boards[0].counts
    assert counts.conflicts == 2 and counts.skipped == 2 and counts.states_changed == 0

    # A move into the collection the planner refuses: a skip, not counted as moved.
    async with sessionmaker() as session:
        await session.execute(BoardPair.__table__.delete())
        await session.commit()
    await _seed_lane_pairs(sessionmaker, clock, [("card1", "t1", "A", "planned", None)])
    vault = FakeVault([_lane_board({"text": "A", "id": "card1"}, lanes=NLANES)])
    planner = FakePlanner([_task("t1", "A")], states=True, collections=[_work_collection()], refuse_moves={"t1"})
    result = await _pass(sessionmaker, _settings(), planner, vault, clock)
    assert result.outcome == board_job.OK
    assert (result.boards[0].counts.tasks_moved, result.boards[0].counts.skipped) == (0, 1)
    assert "collectionId" not in planner.tasks["t1"]


async def test_a_board_changed_under_the_pass_still_counts_its_collection(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 11, 10, 9, 0, tz=TZ)
    await _link(sessionmaker, clock)
    post = BoardPost(sha256=None, changed=False, conflict=True)
    vault = FakeVault([_lane_board({"text": "Новая"}, lanes=NLANES)], post=post)
    planner = FakePlanner([], states=True, collections=[])
    result = await _pass(sessionmaker, _settings(), planner, vault, clock)
    (board,) = result.boards
    assert board.outcome == board_job.CHANGED and board.counts.collections_created == 1
    (run,) = [r for r in await _runs(sessionmaker) if r.board == NAME]
    assert (run.outcome, run.collections_created, run.tasks_created) == ("changed", 1, 0)


async def test_list_rows_read_a_collection_and_a_column_strictly():
    (task,) = board_job.parse_all({"tasks": [{"id": "t1", "title": "A", "done": False, "position": 0,
                                               "collectionId": "c1", "column": "Doing"}]})
    assert (task.collection_id, task.column) == ("c1", "Doing")
    (task,) = board_job.parse_all({"tasks": [{"id": "t1", "title": "A", "done": False, "position": 0}]})
    assert (task.collection_id, task.column) == (None, None)
    for bad in ({"collectionId": ""}, {"collectionId": 3}, {"column": 1}):
        with pytest.raises(PlannerToolError):
            board_job.parse_all({"tasks": [{"id": "t1", "title": "A", "done": False, "position": 0, **bad}]})


def test_the_collections_setting_is_validated_like_the_tags():
    assert _settings().BOARD_SYNC_COLLECTIONS == {"Work tasks.md": "Work tasks", "General tasks.md": "General tasks"}
    assert _settings(BOARD_SYNC_COLLECTIONS='{"A.md": "Дела"}').BOARD_SYNC_COLLECTIONS == {"A.md": "Дела"}
    for bad in ('[1]', '{"A": "X"}', '{"A.md": ""}', '{"A.md": " X"}', '{"A.md": "X", "B.md": "x"}', 'nope',
                '{"A.md": "' + "x" * 101 + '"}'):
        with pytest.raises(ValueError):
            _settings(BOARD_SYNC_COLLECTIONS=bad)
