"""app/vault/client.py's task-board routes and app/planner/client.py's
board-sync wrappers, against loopback stubs. Synthetic cards only.
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.planner import client as planner_client
from app.planner.client import ALLOWED_TOOLS, PlannerClient, PlannerToolError
from app.vault import errors
from app.vault.client import BoardCard, BoardPost, TaskBoard, VaultClient, _purpose
from app.vault.errors import VaultError
from vault_stub import TOKEN, start_stub

SHA = "a" * 64


@pytest.fixture
async def stub():
    stub, server = await start_stub()
    yield stub
    await server.close()


def _client(stub) -> VaultClient:
    return VaultClient(stub.url, TOKEN)


def _card(n, **extra):
    base = {"n": n, "id": None, "lane": "Todo", "text": f"Карта {n}", "checked": False, "date": None}
    return {**base, **extra}


def _board(**extra):
    base = {
        "name": "Work tasks.md",
        "state": "ok",
        "sha256": SHA,
        "lanes": ["Todo", "Done"],
        "done_lane": True,
        "dates": True,
        "cards": [_card(0, id="abcd1", date="2026-11-12"), _card(1, checked=True)],
    }
    return {**base, **extra}


async def test_get_boards_parses_every_state(stub):
    stub.respond(
        "GET",
        "/v1/boards",
        200,
        {"boards": [_board(), {"name": "General tasks.md", "state": "missing"}, {"name": "X.md", "state": "not_board"}]},
    )
    boards = await _client(stub).get_boards()
    assert boards[0] == TaskBoard(
        name="Work tasks.md",
        state="ok",
        sha256=SHA,
        lanes=("Todo", "Done"),
        done_lane=True,
        dates=True,
        cards=(
            BoardCard(0, "abcd1", "Todo", "Карта 0", False, "2026-11-12"),
            BoardCard(1, None, "Todo", "Карта 1", True, None),
        ),
    )
    assert [(b.name, b.state, b.cards) for b in boards[1:]] == [("General tasks.md", "missing", ()), ("X.md", "not_board", ())]
    assert stub.calls() == [("GET", "/v1/boards")]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"boards": "x"},
        {"boards": [], "extra": 1},
        {"boards": [_board(state="weird")]},
        {"boards": [_board(sha256="short")]},
        {"boards": [_board(cards=[_card(1)])]},  # out of order
        {"boards": [_board(cards=[{**_card(0), "extra": 1}])]},
        {"boards": [_board(cards=[_card(0, checked="yes")])]},
        {"boards": [_board(cards=[_card(0, date="12.11.2026")])]},
        {"boards": [_board(cards=[_card(0, id="")])]},
        {"boards": [_board(dates="no")]},
        {"boards": [{"name": "A.md", "state": "missing", "cards": []}]},
        {"boards": [_board(), _board()]},  # one name twice
        {"boards": [{k: v for k, v in _board().items() if k != "lanes"}]},
    ],
)
async def test_a_board_answer_of_the_wrong_shape_is_a_protocol_error(stub, body):
    stub.respond("GET", "/v1/boards", 200, body)
    with pytest.raises(VaultError) as exc:
        await _client(stub).get_boards()
    assert exc.value.code == errors.BAD_RESPONSE


async def test_a_card_date_is_checked_for_shape_only(stub):
    """`@{2026-02-30}` is what the user typed; the merge decides."""
    stub.respond("GET", "/v1/boards", 200, {"boards": [_board(cards=[_card(0, date="2026-02-30")])]})
    (board,) = await _client(stub).get_boards()
    assert board.cards[0].date == "2026-02-30"


async def _post_server(status: int, answer: object):
    seen: list[dict] = []

    async def handler(request: web.Request) -> web.Response:
        seen.append(await request.json())
        return web.json_response(answer, status=status)

    app = web.Application()
    app.router.add_post("/v1/boards", handler)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    return server, seen


async def test_post_board_sends_the_body_and_parses_the_answer():
    server, seen = await _post_server(200, {"sha256": "b" * 64, "changed": True})
    try:
        client = VaultClient(str(server.make_url("")), TOKEN)
        ops = [{"op": "tag", "n": 0, "id": "abcd1234"}]
        got = await client.post_board("Work tasks.md", SHA, ops, "boards_1")
    finally:
        await server.close()
    assert got == BoardPost(sha256="b" * 64, changed=True)
    assert seen == [{"name": "Work tasks.md", "if_sha256": SHA, "ops": ops, "changeset": "boards_1"}]


async def test_a_409_is_an_outcome_not_an_error(stub):
    stub.respond("POST", "/v1/boards", 409, {"error": "conflict"})
    got = await _client(stub).post_board("Work tasks.md", SHA, [{"op": "remove", "id": "abcd1"}], "boards_1")
    assert got == BoardPost(sha256=None, changed=False, conflict=True)


@pytest.mark.parametrize(("status", "code"), [(403, errors.REFUSED), (400, errors.REFUSED), (500, errors.UNAVAILABLE)])
async def test_a_refusal_is_still_an_error(stub, status, code):
    stub.respond("POST", "/v1/boards", status, {})
    with pytest.raises(VaultError) as exc:
        await _client(stub).post_board("Work tasks.md", SHA, [{"op": "remove", "id": "abcd1"}], "boards_1")
    assert exc.value.code == code


@pytest.mark.parametrize("body", [{"sha256": SHA}, {"sha256": SHA, "changed": 1}, {"sha256": "x", "changed": True}])
async def test_a_post_answer_of_the_wrong_shape_is_a_protocol_error(stub, body):
    stub.respond("POST", "/v1/boards", 200, body)
    with pytest.raises(VaultError) as exc:
        await _client(stub).post_board("Work tasks.md", SHA, [{"op": "remove", "id": "abcd1"}], "boards_1")
    assert exc.value.code == errors.BAD_RESPONSE


async def test_a_409_elsewhere_is_still_a_conflict_error(stub):
    stub.respond("GET", "/v1/status", 409, {})
    with pytest.raises(VaultError) as exc:
        await _client(stub).status()
    assert exc.value.code == errors.CONFLICT


def test_the_boards_purpose_is_known():
    assert _purpose("boards") == "boards"
    with pytest.raises(VaultError):
        _purpose("cards")


# -- the planner wrappers ---------------------------------------------------------------


class _Recorder(PlannerClient):
    def __init__(self):
        super().__init__(None, "http://planner.invalid")
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, settings, session, clock, name, arguments):
        if name not in ALLOWED_TOOLS:
            raise PlannerToolError(name)
        self.calls.append((name, arguments))
        return {}


async def test_update_task_sends_only_the_fields_given():
    client = _Recorder()
    await client.update_task(None, None, None, task_id="t1", title="Новое")
    await client.update_task(None, None, None, task_id="t1", due_date=None)
    await client.update_task(None, None, None, task_id="t1", due_date="2026-11-12")
    await client.update_task(None, None, None, task_id="t1", tags=["дом"])
    await client.update_task(None, None, None, task_id="t1", tags=[])
    assert client.calls == [
        ("update_task", {"id": "t1", "title": "Новое"}),
        ("update_task", {"id": "t1", "dueDate": None}),
        ("update_task", {"id": "t1", "dueDate": "2026-11-12"}),
        ("update_task", {"id": "t1", "tags": ["дом"]}),
        ("update_task", {"id": "t1", "tags": []}),
    ]
    with pytest.raises(ValueError):
        await client.update_task(None, None, None, task_id="t1")


def test_update_task_cannot_send_any_other_field():
    import inspect

    params = set(inspect.signature(PlannerClient.update_task).parameters)
    assert params == {"self", "settings", "session", "clock", "task_id", "title", "due_date", "tags"}


async def test_list_tasks_asks_for_the_users_own_top_level_tasks():
    client = _Recorder()
    await client.list_tasks(None, None, None, tag="work")
    assert client.calls == [
        ("list_tasks", {"tag": "work", "mine": True, "includeSubtasks": False, "includeCompleted": True})
    ]


async def test_list_tasks_with_no_tag_sends_no_tag_filter():
    client = _Recorder()
    await client.list_tasks(None, None, None, tag=None, include_completed=False)
    assert client.calls == [("list_tasks", {"mine": True, "includeSubtasks": False, "includeCompleted": False})]


async def test_complete_task_sends_done_only_when_given():
    client = _Recorder()
    await client.complete_task(None, None, None, task_id="t1")
    await client.complete_task(None, None, None, task_id="t1", done=False)
    assert client.calls == [("complete_task", {"id": "t1"}), ("complete_task", {"id": "t1", "done": False})]


def test_update_task_is_allowed_but_no_delete_or_event_edit():
    assert {"update_task", "list_tasks", "complete_task", "create_task"} <= ALLOWED_TOOLS
    assert not {"delete_task", "update_event", "delete_event"} & ALLOWED_TOOLS
    assert planner_client.UNSET is not None


def test_json_roundtrip_of_ops_is_plain():
    """The batch is plain JSON: nothing in an op is a Python-only type."""
    ops = [{"op": "add", "id": "abcd1234", "text": "Т", "date": "2026-11-12"}]
    assert json.loads(json.dumps(ops)) == ops
