"""Task boards (boards.py): the user's Kanban Starlane boards, read as
cards and changed card by card.

Pinned here:

- only boards named in `Anchor/settings.md` are read or written, and only
  if they are Starlane boards; the setting's values are validated;
- the read returns cards (lane, first line, checked, date, block id) and
  lane titles, and nothing else of the file: no descriptions, no
  continuation lines, no archive, no history or settings;
- every operation leaves the rest of the file byte for byte as it was,
  CRLF line endings included, and reads back as what was asked;
- text that would become structure is refused, not escaped;
- a board that changed since it was read is a 409 and is not written;
- writes are undoable Echo changesets with their own budget;
- the log names a refusal's reason and never a board's name or a card.

Every board here is synthetic. The layout follows the plugin's own
board-format document.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest

from vaultd import boards, classes
from vaultd.config import BOARD_CHANGESETS_PER_DAY, ECHO_CHANGESETS_PER_DAY
from vaultd.knowledge import Refused
from vaultd.log import JsonFormatter
from tests.conftest import AUTH, write

NAME = "Work tasks.md"
SETTINGS = "---\nanchor: settings\ntask_boards: [Work tasks.md, General tasks.md]\n---\n"

BOARD = """---

kanban-starlane: board

---

## Todo (3)

Work for this week, not for the bot

- [ ] Write the report #q4 @{2026-11-20} ^abc123
- [ ] A multi-line card
    second line, private
- [/] Half done ^half01


## Doing

- [ ] Review the budget @@{10:30}


## Done

**Complete**
- [x] Ship the release ^done01




***

## Archive

- [x] Archived card ^arch01

%% kanban-starlane:history
```
{
"abc123":[{"at":"2026-09-24T09:00:00+03:00","type":"created","lane":"Todo"}]
}
```
%%

%% kanban-starlane:settings
```
{"kanban-starlane":"board","lane-width":300}
```
%%
"""


def sha(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def apply(ops, board: str = BOARD) -> str:
    return boards.apply(board.encode(), ops).decode()


def cards(text: str) -> list[dict]:
    return boards.describe(boards.parse(text.encode()))["cards"]


def changed_lines(before: str, after: str) -> tuple[list[str], list[str]]:
    """Lines only in `before`, lines only in `after` (as multisets)."""
    a, b = before.splitlines(keepends=True), after.splitlines(keepends=True)
    gone, new = list(a), list(b)
    for line in a:
        if line in new:
            new.remove(line)
    for line in b:
        if line in gone:
            gone.remove(line)
    return gone, new


# -- the setting -------------------------------------------------------------------


def test_task_boards_are_named_in_the_settings_file():
    rules = classes.parse_settings(SETTINGS.encode())
    assert (rules.state, rules.task_boards) == ("ok", ("Work tasks.md", "General tasks.md"))
    assert classes.parse_settings(b"---\nanchor: settings\n---\n").task_boards == ()


@pytest.mark.parametrize(
    "value",
    ["[Boards/Work.md]", "[../Work.md]", "[.hidden.md]", "[Work.txt]", "[.md]", "Work.md", "[1]", "[a.md, b.md, c.md, d.md, e.md]"],
)
def test_a_bad_task_boards_value_invalidates_the_file(value):
    assert classes.parse_settings(f"---\nanchor: settings\ntask_boards: {value}\n---\n".encode()).state == "invalid"


# -- reading -----------------------------------------------------------------------


def test_the_read_is_cards_and_lane_titles_only():
    seen = boards.describe(boards.parse(BOARD.encode()))
    assert seen["lanes"] == ["Todo", "Doing", "Done"]
    assert seen["done_lane"] is True and seen["dates"] is True
    assert seen["cards"] == [
        {"n": 0, "id": "abc123", "lane": "Todo", "text": "Write the report #q4", "checked": False, "date": "2026-11-20"},
        {"n": 1, "id": None, "lane": "Todo", "text": "A multi-line card", "checked": False, "date": None},
        {"n": 2, "id": "half01", "lane": "Todo", "text": "Half done", "checked": True, "date": None},
        {"n": 3, "id": None, "lane": "Doing", "text": "Review the budget @@{10:30}", "checked": False, "date": None},
        {"n": 4, "id": "done01", "lane": "Done", "text": "Ship the release", "checked": True, "date": None},
    ]
    dumped = str(seen)
    for hidden in ("not for the bot", "second line", "Archived", "lane-width", "created"):
        assert hidden not in dumped


def test_what_is_not_a_starlane_board_is_not_read():
    assert boards.parse(b"# Just a note\n\n- [ ] not a card\n") is None
    assert boards.parse(b"---\nkanban-plugin: board\n---\n\n## Todo\n\n- [ ] legacy\n") is None
    assert boards.parse(b"---\nkanban-starlane: board\n---\n\n## Todo\n\n- [ ] \xff\n") is None
    with pytest.raises(Refused, match="not_board"):
        boards.apply(b"# note\n", [{"op": "add", "id": "aaaa1111", "text": "x"}])


def test_a_board_with_its_own_date_syntax_keeps_its_dates_to_itself():
    custom = BOARD.replace('"lane-width":300', '"lane-width":300,"date-format":"DD.MM.YYYY"')
    seen = boards.describe(boards.parse(custom.encode()))
    assert seen["dates"] is False
    assert seen["cards"][0]["date"] is None and "@{2026-11-20}" in seen["cards"][0]["text"]
    with pytest.raises(Refused):
        apply([{"op": "date", "id": "abc123", "date": "2026-12-01"}], custom)
    assert "@{" not in apply([{"op": "add", "id": "new00001", "text": "New", "date": "2026-12-01"}], custom).split("## Doing")[0].split("Half done")[1]


# -- writing: each operation, and nothing else changes -----------------------------


def test_no_operation_that_changes_nothing_changes_a_byte():
    assert apply([{"op": "check", "id": "abc123", "checked": False}]) == BOARD
    assert apply([{"op": "text", "id": "half01", "text": "Half done"}]) == BOARD
    assert apply([{"op": "date", "id": "abc123", "date": "2026-11-20"}]) == BOARD


def test_tag_gives_a_card_a_block_id_and_touches_only_its_line():
    after = apply([{"op": "tag", "n": 1, "id": "tag00001"}])
    assert changed_lines(BOARD, after) == (["- [ ] A multi-line card\n"], ["- [ ] A multi-line card ^tag00001\n"])
    assert cards(after)[1]["id"] == "tag00001"
    for bad in ({"op": "tag", "n": 0, "id": "tag00001"}, {"op": "tag", "n": 99, "id": "tag00001"}, {"op": "tag", "n": 1, "id": "abc123"}):
        with pytest.raises(Refused):
            apply([bad])


def test_text_keeps_the_box_the_date_and_the_id():
    after = apply([{"op": "text", "id": "abc123", "text": "Write the final report"}])
    assert changed_lines(BOARD, after) == (
        ["- [ ] Write the report #q4 @{2026-11-20} ^abc123\n"],
        ["- [ ] Write the final report @{2026-11-20} ^abc123\n"],
    )
    assert apply([{"op": "text", "id": "half01", "text": "Still half"}]).count("- [/] Still half ^half01\n") == 1


def test_date_sets_moves_and_clears():
    assert "- [/] Half done @{2026-12-01} ^half01\n" in apply([{"op": "date", "id": "half01", "date": "2026-12-01"}])
    assert "- [ ] Write the report #q4 @{2026-12-24} ^abc123\n" in apply([{"op": "date", "id": "abc123", "date": "2026-12-24"}])
    assert "- [ ] Write the report #q4 ^abc123\n" in apply([{"op": "date", "id": "abc123", "date": None}])


def test_checking_moves_a_card_to_the_done_lane_with_its_continuation_lines():
    tagged = apply([{"op": "tag", "n": 1, "id": "tag00001"}])
    after = apply([{"op": "check", "id": "tag00001", "checked": True}], tagged)
    done = after.split("**Complete**\n")[1].split("***")[0]
    assert done.startswith("- [x] Ship the release ^done01\n- [x] A multi-line card ^tag00001\n    second line, private\n")
    assert "A multi-line card" not in after.split("## Doing")[0]
    listed = cards(after)
    assert [c["lane"] for c in listed if c["id"] == "tag00001"] == ["Done"]
    assert len(listed) == 5


def test_unchecking_a_done_card_moves_it_back_to_the_first_lane():
    after = apply([{"op": "check", "id": "done01", "checked": False}])
    assert "- [/] Half done ^half01\n- [ ] Ship the release ^done01\n" in after
    assert cards(after)[3] == {"n": 3, "id": "done01", "lane": "Todo", "text": "Ship the release", "checked": False, "date": None}
    # The done lane is empty now, and still the done lane.
    assert boards.describe(boards.parse(after.encode()))["done_lane"] is True
    again = apply([{"op": "check", "id": "done01", "checked": True}], after)
    assert "**Complete**\n- [x] Ship the release ^done01\n" in again


def test_without_a_done_lane_the_box_changes_in_place():
    plain = BOARD.replace("**Complete**\n", "")
    after = apply([{"op": "check", "id": "abc123", "checked": True}], plain)
    assert changed_lines(plain, after) == (
        ["- [ ] Write the report #q4 @{2026-11-20} ^abc123\n"],
        ["- [x] Write the report #q4 @{2026-11-20} ^abc123\n"],
    )


def test_add_goes_to_the_first_lane_or_the_done_lane():
    after = apply([{"op": "add", "id": "new00001", "text": "Call the bank", "date": "2026-11-12"}])
    assert changed_lines(BOARD, after) == ([], ["- [ ] Call the bank @{2026-11-12} ^new00001\n"])
    assert "- [/] Half done ^half01\n- [ ] Call the bank @{2026-11-12} ^new00001\n" in after
    done = apply([{"op": "add", "id": "new00002", "text": "Already done", "checked": True}])
    assert "- [x] Ship the release ^done01\n- [x] Already done ^new00002\n" in done


def test_add_to_an_empty_lane_and_to_a_bare_board():
    empty = "---\nkanban-starlane: board\n---\n\n## Todo\n\n## Done\n\n**Complete**\n"
    after = apply([{"op": "add", "id": "new00001", "text": "First"}, {"op": "add", "id": "new00002", "text": "Was done", "checked": True}], empty)
    assert after == "---\nkanban-starlane: board\n---\n\n## Todo\n\n- [ ] First ^new00001\n## Done\n\n**Complete**\n- [x] Was done ^new00002\n"
    assert [c["lane"] for c in cards(after)] == ["Todo", "Done"]
    no_newline = "---\nkanban-starlane: board\n---\n\n## Todo"
    assert apply([{"op": "add", "id": "new00001", "text": "First"}], no_newline).endswith("## Todo\n\n- [ ] First ^new00001\n")
    with pytest.raises(Refused, match="board_no_lane"):
        apply([{"op": "add", "id": "new00001", "text": "x"}], "---\nkanban-starlane: board\n---\n")


def test_move_takes_a_card_with_its_lines_to_the_end_of_the_named_lane():
    tagged = apply([{"op": "tag", "n": 1, "id": "tag00001"}, {"op": "add", "id": "doing001", "text": "Draft"}])
    tagged = apply([{"op": "move", "id": "doing001", "lane": "Doing"}], tagged)
    after = apply([{"op": "move", "id": "tag00001", "lane": "Doing"}], tagged)
    doing = after.split("## Doing\n")[1].split("## Done")[0]
    assert doing.startswith(
        "\n- [ ] Review the budget @@{10:30}\n- [ ] Draft ^doing001\n- [ ] A multi-line card ^tag00001\n    second line, private\n"
    )
    assert "A multi-line card" not in after.split("## Doing")[0]
    assert changed_lines(tagged, after) == ([], [])  # the same lines, one card's moved
    assert [c["lane"] for c in cards(after) if c["id"] == "tag00001"] == ["Doing"]
    assert after.endswith(BOARD[BOARD.index("***") :])


def test_move_into_the_done_lane_checks_and_out_of_it_unchecks():
    after = apply([{"op": "move", "id": "abc123", "lane": "Done"}])
    assert "**Complete**\n- [x] Ship the release ^done01\n- [x] Write the report #q4 @{2026-11-20} ^abc123\n" in after
    back = apply([{"op": "move", "id": "done01", "lane": "Doing"}])
    assert "- [ ] Review the budget @@{10:30}\n- [ ] Ship the release ^done01\n" in back
    assert [c["checked"] for c in cards(back) if c["id"] == "done01"] == [False]
    # A checked card moved between two other lanes is unchecked too: on a
    # board with a done lane, only that lane holds checked cards after a move.
    half = apply([{"op": "move", "id": "half01", "lane": "Doing"}])
    assert "- [ ] Half done ^half01\n" in half and "[/]" not in half.split("***")[0]


def test_move_to_the_lane_a_card_is_in_leaves_it_in_place():
    assert apply([{"op": "move", "id": "abc123", "lane": "Todo"}]) == BOARD
    assert apply([{"op": "move", "id": "done01", "lane": "Done"}]) == BOARD
    # In place, the box still follows the lane.
    after = apply([{"op": "move", "id": "half01", "lane": "Todo"}])
    assert changed_lines(BOARD, after) == (["- [/] Half done ^half01\n"], ["- [ ] Half done ^half01\n"])


def test_without_a_done_lane_move_keeps_the_box():
    plain = BOARD.replace("**Complete**\n", "")
    after = apply([{"op": "move", "id": "half01", "lane": "Done"}], plain)
    assert "## Done\n\n- [x] Ship the release ^done01\n- [/] Half done ^half01\n" in after
    assert [c["lane"] for c in cards(after) if c["id"] == "half01"] == ["Done"]


def test_move_names_a_lane_by_its_title_without_the_wip_count():
    after = apply([{"op": "move", "id": "done01", "lane": "Todo"}])
    assert "- [/] Half done ^half01\n- [ ] Ship the release ^done01\n" in after


@pytest.mark.parametrize(
    "op, reason",
    [
        ({"op": "move", "id": "abc123", "lane": "Archive"}, "bad_card"),  # past the thematic break
        ({"op": "move", "id": "abc123", "lane": "Todo (3)"}, "bad_card"),
        ({"op": "move", "id": "abc123", "lane": "Nowhere"}, "bad_card"),
        ({"op": "move", "id": "abc123", "lane": ""}, "bad_card"),
        ({"op": "move", "id": "abc123", "lane": 1}, "bad_card"),
        ({"op": "move", "id": "abc123"}, "bad_card"),
        ({"op": "move", "id": "abc123", "lane": "Doing", "checked": True}, "bad_card"),
        ({"op": "move", "id": "BAD ID", "lane": "Doing"}, "bad_card"),
        ({"op": "move", "id": "nope0000", "lane": "Doing"}, "card_missing"),
        ({"op": "move", "id": "arch01", "lane": "Todo"}, "card_missing"),  # an archived card
    ],
)
def test_a_bad_move_refuses_the_whole_batch(op, reason):
    with pytest.raises(Refused, match=reason):
        apply([{"op": "text", "id": "abc123", "text": "would have changed"}, op])


def test_the_done_lane_is_named_only_when_asked():
    parsed = boards.parse(BOARD.encode())
    assert "done_lane_title" not in boards.describe(parsed)
    assert boards.describe(parsed, lanes=True)["done_lane_title"] == "Done"
    plain = boards.parse(BOARD.replace("**Complete**\n", "").encode())
    assert boards.describe(plain, lanes=True)["done_lane_title"] is None


def test_remove_takes_the_card_and_its_continuation_lines_only():
    tagged = apply([{"op": "tag", "n": 1, "id": "tag00001"}])
    after = apply([{"op": "remove", "id": "tag00001"}], tagged)
    assert changed_lines(tagged, after) == (["- [ ] A multi-line card ^tag00001\n", "    second line, private\n"], [])
    with pytest.raises(Refused, match="card_missing"):
        apply([{"op": "remove", "id": "arch01"}])  # the archive is out of reach


def test_the_archive_history_and_settings_survive_every_operation():
    tail = BOARD[BOARD.index("***") :]
    after = apply(
        [
            {"op": "tag", "n": 1, "id": "tag00001"},
            {"op": "add", "id": "new00001", "text": "New"},
            {"op": "check", "id": "abc123", "checked": True},
            {"op": "text", "id": "half01", "text": "Renamed"},
            {"op": "remove", "id": "done01"},
        ]
    )
    assert after.endswith(tail)
    assert after.startswith(BOARD[: BOARD.index("## Todo")])
    assert "Work for this week, not for the bot\n" in after


def test_crlf_boards_stay_crlf():
    crlf = BOARD.replace("\n", "\r\n")
    after = apply([{"op": "add", "id": "new00001", "text": "New"}, {"op": "text", "id": "abc123", "text": "Renamed"}], crlf)
    assert "\n" not in after.replace("\r\n", "")
    assert "- [ ] New ^new00001\r\n" in after and "- [ ] Renamed @{2026-11-20} ^abc123\r\n" in after


@pytest.mark.parametrize(
    "text",
    ["", "   ", "two\nlines", "ends with ^blockid", "has @{2026-01-01} inside", "a %% comment", "x" * 501, 5, None, "tab\there\x00"],
)
def test_text_that_would_become_structure_is_refused(text):
    for op in ({"op": "add", "id": "new00001", "text": text}, {"op": "text", "id": "abc123", "text": text}):
        with pytest.raises(Refused, match="bad_card"):
            apply([op])


@pytest.mark.parametrize(
    "op",
    [
        {"op": "nuke"},
        {"op": "add", "id": "BAD ID", "text": "x"},
        {"op": "add", "id": "abc123", "text": "x"},
        {"op": "add", "id": "new00001", "text": "x", "lane": "Doing"},
        {"op": "add", "id": "new00001", "text": "x", "date": "tomorrow"},
        {"op": "check", "id": "abc123", "checked": "yes"},
        {"op": "text", "id": "nope0000", "text": "x"},
        {"op": "remove", "id": "abc123", "extra": 1},
        "remove",
    ],
)
def test_a_malformed_operation_refuses_the_whole_batch(op):
    with pytest.raises(Refused):
        apply([{"op": "text", "id": "abc123", "text": "would have changed"}, op])
    with pytest.raises(Refused):
        boards.apply(BOARD.encode(), [])


# -- the routes ----------------------------------------------------------------------


@pytest.fixture
def base(vault: Path):
    write(vault, "Anchor/settings.md", SETTINGS)
    write(vault, NAME, BOARD)


async def _post(client, ops, *, name=NAME, expected=None, changeset="boards_a"):
    return await client.post(
        "/v1/boards",
        json={"name": name, "if_sha256": sha(BOARD) if expected is None else expected, "ops": ops, "changeset": changeset},
        headers=AUTH,
    )


ADD = [{"op": "add", "id": "new00001", "text": "Call the bank"}]


async def test_get_lists_named_boards_and_their_states(client, vault: Path, base):
    write(vault, "Secret plans.md", BOARD)
    found = (await (await client.get("/v1/boards", headers=AUTH)).json())["boards"]
    assert [(b["name"], b["state"]) for b in found] == [("Work tasks.md", "ok"), ("General tasks.md", "missing")]
    assert found[0]["sha256"] == sha(BOARD) and len(found[0]["cards"]) == 5
    write(vault, "General tasks.md", "# not a board\n")
    found = (await (await client.get("/v1/boards", headers=AUTH)).json())["boards"]
    assert found[1] == {"name": "General tasks.md", "state": "not_board"}
    assert "done_lane_title" not in found[0]
    asked = (await (await client.get("/v1/boards?subs=1&titles=1&lanes=1", headers=AUTH)).json())["boards"]
    assert asked[0]["done_lane_title"] == "Done" and asked[1] == {"name": "General tasks.md", "state": "not_board"}


async def test_no_setting_no_boards(client, vault: Path):
    write(vault, NAME, BOARD)
    assert (await (await client.get("/v1/boards", headers=AUTH)).json()) == {"boards": []}
    assert (await _post(client, ADD)).status == 403
    write(vault, "Anchor/settings.md", "---\nanchor: settings\n---\n")
    assert (await _post(client, ADD)).status == 403
    assert (vault / NAME).read_text() == BOARD


async def test_a_write_applies_and_is_undoable(client, vault: Path, base):
    resp = await _post(client, ADD)
    assert resp.status == 200
    after = (vault / NAME).read_text()
    assert await resp.json() == {"sha256": sha(after), "changed": True}
    assert "- [ ] Call the bank ^new00001\n" in after
    changes = (await (await client.get("/v1/changes", headers=AUTH)).json())["changes"]
    assert [(c["writer"], c["purpose"]) for c in changes] == [("echo", "boards")]
    undo = await client.post("/v1/undo", params={"changeset": "boards_a", "writer": "echo"}, headers=AUTH)
    assert await undo.json() == {"restored": 1, "refused": 0}
    assert (vault / NAME).read_text() == BOARD


async def test_a_board_that_changed_is_a_409_and_is_left_alone(client, vault: Path, base):
    edited = BOARD.replace("Review the budget", "Review the whole budget")
    (vault / NAME).write_text(edited)
    assert (await _post(client, ADD)).status == 409
    assert (vault / NAME).read_text() == edited
    assert (await (await client.get("/v1/changes", headers=AUTH)).json())["changes"] == []


async def test_only_named_boards_can_be_written(client, vault: Path, base):
    write(vault, "Secret plans.md", BOARD)
    write(vault, "Library/x.md", BOARD)
    for name in ("Secret plans.md", "Library/x.md", "../Work tasks.md", "Anchor/settings.md", "General tasks.md"):
        assert (await _post(client, ADD, name=name)).status == 403
    assert (vault / "Secret plans.md").read_text() == BOARD


async def test_a_refused_batch_writes_nothing(client, vault: Path, base):
    assert (await _post(client, ADD + [{"op": "remove", "id": "nope0000"}])).status == 403
    assert (vault / NAME).read_text() == BOARD


async def test_a_batch_that_changes_nothing_spends_no_changeset(client, vault: Path, base):
    resp = await _post(client, [{"op": "check", "id": "abc123", "checked": False}])
    assert await resp.json() == {"sha256": sha(BOARD), "changed": False}
    assert (await (await client.get("/v1/changes", headers=AUTH)).json())["changes"] == []


async def test_a_symlinked_board_is_not_followed(client, vault: Path, tmp_path: Path):
    write(vault, "Anchor/settings.md", SETTINGS)
    outside = tmp_path / "outside.md"
    outside.write_text(BOARD)
    (vault / NAME).symlink_to(outside)
    found = (await (await client.get("/v1/boards", headers=AUTH)).json())["boards"]
    assert found[0]["state"] == "missing"
    assert (await _post(client, ADD)).status == 403
    assert outside.read_text() == BOARD


async def test_the_boards_budget_is_its_own(client, vault: Path, base, clock):
    current = BOARD
    for i in range(BOARD_CHANGESETS_PER_DAY):
        resp = await _post(client, [{"op": "text", "id": "half01", "text": f"Half {i}"}], expected=sha(current), changeset=f"boards_{i}")
        assert resp.status == 200
        current = (vault / NAME).read_text()
    late = [{"op": "text", "id": "half01", "text": "late"}]
    assert (await _post(client, late, expected=sha(current), changeset="boards_late")).status == 403
    content = '---\nanchor: knowledge\nsource_urls: ["https://example.org/x"]\ngap: 1\n---\nТекст.\n'
    for i in range(ECHO_CHANGESETS_PER_DAY):
        resp = await client.put("/v1/echo/inbox", json={"name": f"n{i}.md", "content": content, "changeset": f"echo_{i}"}, headers=AUTH)
        assert resp.status == 200
    clock.advance(hours=24, seconds=1)
    assert (await _post(client, late, expected=sha(current), changeset="boards_late")).status == 200


async def test_other_writers_cannot_share_a_board_changeset(client, vault: Path, base):
    assert (await _post(client, ADD)).status == 200
    content = '---\nanchor: knowledge\nsource_urls: ["https://example.org/x"]\ngap: 1\n---\nТекст.\n'
    resp = await client.put("/v1/echo/inbox", json={"name": "n.md", "content": content, "changeset": "boards_a"}, headers=AUTH)
    assert resp.status == 403
    resp = await client.put("/v1/echo/streams", json={"name": "s.md", "stream": "tag:s", "body": "x\n", "changeset": "boards_a"}, headers=AUTH)
    assert resp.status == 403


@pytest.mark.parametrize(
    "payload",
    [
        {"name": NAME, "if_sha256": "x", "ops": []},
        {"name": NAME, "if_sha256": "x", "ops": "add", "changeset": "boards_a"},
        {"name": 5, "if_sha256": "x", "ops": [], "changeset": "boards_a"},
        {"name": NAME, "if_sha256": "x", "ops": [], "changeset": "../x"},
    ],
)
async def test_a_malformed_body_is_a_400(client, base, payload):
    assert (await client.post("/v1/boards", json=payload, headers=AUTH)).status == 400


async def test_the_routes_need_the_token(client, base):
    assert (await client.get("/v1/boards")).status == 401
    assert (await client.post("/v1/boards", json={})).status == 401


async def test_logs_carry_the_reason_and_never_a_name_or_a_card(client, vault: Path, base, caplog):
    with caplog.at_level(logging.DEBUG):
        await client.get("/v1/boards", headers=AUTH)
        assert (await _post(client, [{"op": "add", "id": "new00001", "text": "Секретная задача"}])).status == 200
        assert (await _post(client, [{"op": "remove", "id": "nope0000"}], expected=sha((vault / NAME).read_text()))).status == 403
    formatter = JsonFormatter()
    dumped = "\n".join(part for r in caplog.records for part in (r.getMessage(), str(r.__dict__), formatter.format(r)))
    assert "card_missing" in dumped
    for leak in ("Work tasks", "Секретная", "Write the report", "Todo", "abc123"):
        assert leak not in dumped
