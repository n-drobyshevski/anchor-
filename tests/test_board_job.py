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

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.core import echo_write
from app.db.models import BoardPair, BoardSyncRun, Job, PlannerCredential, PlannerSnapshot, UserState
from app.planner import board_job, board_sync
from app.planner.client import UNSET, PlannerUnavailable
from app.tg import boards as boards_ui
from app.vault import errors
from app.vault.client import BoardCard, BoardPost, ChangeEntry, ChangeFile, TaskBoard, UndoResult
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


def _cards(*specs) -> tuple[BoardCard, ...]:
    return tuple(
        BoardCard(n=i, id=s.get("id"), lane="Todo", text=s["text"], checked=s.get("checked", False), date=s.get("date"))
        for i, s in enumerate(specs)
    )


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

    async def get_boards(self):
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
    def __init__(self, tasks=None, *, fail_on=None, log=None):
        self.tasks = {t["id"]: dict(t) for t in (tasks or [])}
        self.fail_on = fail_on or set()
        self.writes: list[tuple[str, dict]] = []
        self.log = log if log is not None else []
        self.by_request: dict[str, str] = {}

    def _maybe_fail(self, name):
        if name in self.fail_on:
            raise PlannerUnavailable(name)

    async def list_tasks(self, settings, session, clock, *, tag, **kw):
        self._maybe_fail("list_tasks")
        rows = [dict(t) for t in self.tasks.values() if tag in t.get("tags", [])]
        return {"count": len(rows), "tasks": rows}

    async def create_task(self, settings, session, clock, *, title, due_date, client_request_id, tags=None):
        self._maybe_fail("create_task")
        self.log.append("create")
        self.writes.append(("create", {"title": title, "due": due_date, "request": client_request_id, "tags": tags}))
        if client_request_id in self.by_request:
            task_id = self.by_request[client_request_id]
        else:
            task_id = f"new{len(self.by_request) + 1}"
            self.by_request[client_request_id] = task_id
            row = {"id": task_id, "title": title, "done": False, "tags": list(tags or [])}
            if due_date:
                row["dueDate"] = due_date
            self.tasks[task_id] = row
        return {"id": task_id, "title": title}

    async def complete_task(self, settings, session, clock, *, task_id, done=None):
        self._maybe_fail("complete_task")
        self.log.append("complete")
        self.writes.append(("complete", {"id": task_id, "done": done}))
        self.tasks[task_id]["done"] = True if done is None else done
        return {"id": task_id}

    async def update_task(self, settings, session, clock, *, task_id, title=None, due_date=UNSET, tags=None):
        self._maybe_fail("update_task")
        self.log.append("update")
        args = {"id": task_id}
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


def _task(task_id, title, *, done=False, due=None, tags=("work",)):
    row = {"id": task_id, "title": title, "done": done, "tags": list(tags)}
    if due:
        row["dueDate"] = due
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


def _board(*specs, name=NAME, dates=True, sha=SHA):
    return TaskBoard(name=name, state="ok", sha256=sha, lanes=("Todo", "Done"), done_lane=True, dates=dates, cards=_cards(*specs))


def _rebuild(vault: FakeVault, board: TaskBoard) -> TaskBoard:
    """The board after the fake vault's last post, via the merge's own model."""
    ops = vault.posts[-1][2]
    merged = board_job.as_board(board)
    plan = board_sync.Plan(board=board.name, units=[board_sync.Unit(ops=tuple(ops))])
    after, _, _ = board_sync.apply_to_state(merged, [], [], plan, tag="work")
    cards = tuple(BoardCard(c.n, c.id, "Todo", c.text, c.checked, c.date) for c in after.cards)
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
