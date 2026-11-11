"""Subtasks and `after:` links inside a board card (board_subs.py, boards.py).

Pinned here:

- a subtask is a checkbox line indented 1..3 units (tab or four spaces)
  under a card, at most one deeper than the one before; a jump is opaque;
- every line the bot does not recognise is kept byte for byte and in
  place, and an operation that changes nothing changes no byte;
- each subtask operation and `after_set` touches only what it names, in
  the board's own indent style, and refuses anything malformed, all or
  nothing;
- checking and removing a card moves or removes its subtasks with it;
- the read carries subtasks and links only when asked (`?subs=1`), and
  the `after:` line's plain titles only when asked as well (`&titles=1`;
  without it a title counts as `after_raw_extra`, the old shape);

Every board here is synthetic.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from vaultd import boards
from vaultd.config import BOARD_AFTER_LINKS_MAX, BOARD_SUBS_PER_CARD_MAX
from vaultd.knowledge import Refused
from tests.conftest import AUTH, write

NAME = "Work tasks.md"
OTHER = "General tasks.md"
NAMED = (NAME, OTHER)
SETTINGS = "---\nanchor: settings\ntask_boards: [Work tasks.md, General tasks.md]\n---\n"

BOARD = """---

kanban-starlane: board

---

## Todo

- [ ] Plan the trip @{2026-11-14} ^card01
    - [x] Pick dates ^sub001
    - [ ] Book the train @{2026-11-17} ^sub002
        - [ ] Compare fares ^sub003
          a loose note under the fares
        - [?] Seat choice
    some free text under the card
    - [ ] Pack ^sub004
            - [ ] jumps too deep
- [ ] Water the plants ^card02
    after: [[#^card01|Plan the trip]], [[General tasks#^gen001|Buy soil]]
- [ ] Plain card ^card03


## Done

**Complete**
- [x] Old card ^done01
    - [x] Old sub ^sub009

***

## Archive

- [x] Archived ^arch01
    - [ ] Archived sub ^arch02

%% kanban-starlane:settings
```
{"kanban-starlane":"board"}
```
%%
"""

TABBED = BOARD.replace("    ", "\t")


def apply(ops, board: str = BOARD, *, name: str | None = NAME) -> str:
    return boards.apply(board.encode(), ops, name=name, named=NAMED).decode()


def card(text: str, block_id: str, *, titles: bool = False) -> dict:
    seen = boards.describe(boards.parse(text.encode()), name=NAME, subs=True, titles=titles)
    return next(c for c in seen["cards"] if c["id"] == block_id)


def with_after(line: str, board: str = BOARD) -> str:
    """BOARD with card02's `after:` line replaced by `line` (indent included)."""
    return board.replace("    after: [[#^card01|Plan the trip]], [[General tasks#^gen001|Buy soil]]", line)


def subs(text: str, block_id: str) -> list[tuple]:
    return [(s["id"], s["depth"], s["text"], s["checked"], s["date"]) for s in card(text, block_id)["subs"]]


def changed_lines(before: str, after: str) -> tuple[list[str], list[str]]:
    a, b = before.splitlines(keepends=True), after.splitlines(keepends=True)
    gone, new = list(a), list(b)
    for line in a:
        if line in new:
            new.remove(line)
    for line in b:
        if line in gone:
            gone.remove(line)
    return gone, new


EXPECTED = [
    ("sub001", 1, "Pick dates", True, None),
    ("sub002", 1, "Book the train", False, "2026-11-17"),
    ("sub003", 2, "Compare fares", False, None),
    (None, 2, "Seat choice", True, None),
    ("sub004", 1, "Pack", False, None),
]


# -- reading -----------------------------------------------------------------------


@pytest.mark.parametrize("board", [BOARD, TABBED, BOARD.replace("\n", "\r\n")], ids=["spaces", "tabs", "crlf"])
def test_subtasks_are_read_with_depth_mark_date_and_id(board):
    assert subs(board, "card01") == EXPECTED
    assert [s["n"] for s in card(board, "card01")["subs"]] == [0, 1, 2, 3, 4]
    assert subs(board, "card02") == [] and subs(board, "card03") == []


def test_the_card_itself_reads_as_before():
    first = card(BOARD, "card01")
    assert {k: first[k] for k in ("n", "id", "lane", "text", "checked", "date")} == {
        "n": 0,
        "id": "card01",
        "lane": "Todo",
        "text": "Plan the trip",
        "checked": False,
        "date": "2026-11-14",
    }
    # Without `subs` the card has exactly its old keys.
    plain = boards.describe(boards.parse(BOARD.encode()))["cards"][0]
    assert set(plain) == {"n", "id", "lane", "text", "checked", "date"}


def test_opaque_lines_are_not_read():
    dumped = str(boards.describe(boards.parse(BOARD.encode()), name=NAME, subs=True))
    for hidden in ("loose note", "free text", "jumps too deep", "Archived", "Buy soil", "Plan the trip]]"):
        assert hidden not in dumped


def test_a_depth_jump_is_not_a_subtask():
    jumpy = "---\nkanban-starlane: board\n---\n\n## Todo\n\n- [ ] Card ^card01\n        - [ ] starts at two\n    - [ ] one ^s0001\n            - [ ] three\n        - [ ] two ^s0002\n"
    assert subs(jumpy, "card01") == [("s0001", 1, "one", False, None), ("s0002", 2, "two", False, None)]


def test_after_links_resolve_to_board_file_names():
    second = card(BOARD, "card02")
    assert second["after"] == [{"board": NAME, "id": "card01"}, {"board": OTHER, "id": "gen001"}]
    assert second["after_raw_extra"] is False
    assert card(BOARD, "card01")["after"] == []


def test_after_variants_and_extra_text():
    board = BOARD.replace(
        "    after: [[#^card01|Plan the trip]], [[General tasks#^gen001|Buy soil]]",
        "    AFTER:[[#^card01]] and also, [[Other.md#^x-1|A, B]] something",
    )
    second = card(board, "card02")
    assert second["after"] == [{"board": NAME, "id": "card01"}, {"board": "Other.md", "id": "x-1"}]
    assert second["after_raw_extra"] is True
    deeper = BOARD.replace("    after: [[#", "        after: [[#")
    assert card(deeper, "card02")["after"] == []


def test_after_titles_mixed_with_links():
    board = with_after("    after: Wiz MCP integration, [[#^card01|Plan, the trip]], Buy soil ,[[General tasks#^gen001]]")
    second = card(board, "card02", titles=True)
    assert second["after"] == [{"board": NAME, "id": "card01"}, {"board": OTHER, "id": "gen001"}]
    assert second["after_titles"] == ["Wiz MCP integration", "Buy soil"]
    assert second["after_raw_extra"] is False
    # A card with no `after:` line, or one of links only, has no titles.
    assert card(BOARD, "card02", titles=True)["after_titles"] == []
    assert card(BOARD, "card01", titles=True)["after_titles"] == []


@pytest.mark.parametrize("unit", ["    ", "\t"], ids=["spaces", "tabs"])
def test_after_titles_are_stripped_in_either_indent(unit):
    board = BOARD if unit == "    " else TABBED
    old = f"{unit}after: [[#^card01|Plan the trip]], [[General tasks#^gen001|Buy soil]]"
    assert old in board
    second = card(board.replace(old, f"{unit}after:\t  Wiz MCP ,   ,\tBuy soil\t"), "card02", titles=True)
    assert second["after_titles"] == ["Wiz MCP", "Buy soil"]
    assert second["after"] == [] and second["after_raw_extra"] is False


@pytest.mark.parametrize(
    "segment",
    ["a ] b", "a | b", "a [ b", "half [[#^card01", "ends ^blockid", "due @{2026-11-14}", "a %% comment", "in\x07side", "x" * 501],
)
def test_after_titles_with_forbidden_text_are_extra(segment):
    second = card(with_after(f"    after: Buy soil, {segment}"), "card02", titles=True)
    assert second["after_raw_extra"] is True
    assert second["after_titles"] == ["Buy soil"]


def test_after_titles_without_asking_read_as_extra():
    board = with_after("    after: Wiz MCP, [[#^card01|Plan the trip]]")
    second = card(board, "card02")
    assert "after_titles" not in second
    assert second["after"] == [{"board": NAME, "id": "card01"}]
    assert second["after_raw_extra"] is True


def test_the_indent_style_is_the_boards():
    assert boards.parse(BOARD.encode()).indent == "    "
    assert boards.parse(TABBED.encode()).indent == "\t"
    bare = "---\nkanban-starlane: board\n---\n\n## Todo\n\n- [ ] Card ^card01\n"
    assert boards.parse(bare.encode()).indent == "    "


# -- no-ops and round trips --------------------------------------------------------


@pytest.mark.parametrize("board", [BOARD, TABBED, BOARD.replace("\n", "\r\n")], ids=["spaces", "tabs", "crlf"])
def test_operations_that_change_nothing_change_no_byte(board):
    ops = [
        {"op": "sub_check", "card": "card01", "id": "sub001", "checked": True},
        {"op": "sub_check", "card": "card01", "id": "sub002", "checked": False},
        {"op": "sub_text", "card": "card01", "id": "sub003", "text": "Compare fares"},
        {"op": "sub_date", "card": "card01", "id": "sub002", "date": "2026-11-17"},
        {"op": "text", "id": "card01", "text": "Plan the trip"},
        {
            "op": "after_set",
            "id": "card02",
            "links": [
                {"board": NAME, "id": "card01", "label": "Plan the trip"},
                {"board": OTHER, "id": "gen001", "label": "Buy soil"},
            ],
        },
        {"op": "after_set", "id": "card03", "links": []},
    ]
    assert apply(ops, board) == board


# -- the subtask operations ----------------------------------------------------------


def test_sub_tag_gives_a_subtask_an_id():
    after = apply([{"op": "sub_tag", "card": "card01", "n": 3, "id": "seat0001"}])
    assert changed_lines(BOARD, after) == (["        - [?] Seat choice\n"], ["        - [?] Seat choice ^seat0001\n"])
    for bad in (
        {"op": "sub_tag", "card": "card01", "n": 0, "id": "seat0001"},  # already has one
        {"op": "sub_tag", "card": "card01", "n": 9, "id": "seat0001"},
        {"op": "sub_tag", "card": "card01", "n": 3, "id": "card02"},  # a card's id
        {"op": "sub_tag", "card": "card01", "n": 3, "id": "sub009"},  # a subtask's id on another card
        {"op": "sub_tag", "card": "card01", "n": True, "id": "seat0001"},
        {"op": "sub_tag", "card": "nope0000", "n": 3, "id": "seat0001"},
    ):
        with pytest.raises(Refused):
            apply([bad])


def test_sub_add_top_level_goes_after_the_last_subtask_block():
    after = apply([{"op": "sub_add", "card": "card01", "id": "new00001", "text": "Charge the phone", "checked": False}])
    assert "    - [ ] Pack ^sub004\n            - [ ] jumps too deep\n    - [ ] Charge the phone ^new00001\n- [ ] Water" in after
    assert changed_lines(BOARD, after) == ([], ["    - [ ] Charge the phone ^new00001\n"])
    assert subs(after, "card01")[-1] == ("new00001", 1, "Charge the phone", False, None)


def test_sub_add_under_a_parent_goes_after_its_subtree():
    after = apply([{"op": "sub_add", "card": "card01", "id": "new00001", "text": "Print tickets", "checked": True, "parent": "sub002", "date": "2026-11-16"}])
    assert "        - [?] Seat choice\n        - [x] Print tickets @{2026-11-16} ^new00001\n    some free text" in after
    assert subs(after, "card01")[4] == ("new00001", 2, "Print tickets", True, "2026-11-16")
    deep = apply([{"op": "sub_add", "card": "card01", "id": "new00002", "text": "Window", "checked": False, "parent": "sub003"}])
    assert "          a loose note under the fares\n            - [ ] Window ^new00002\n        - [?] Seat choice\n" in deep
    assert subs(deep, "card01")[3] == ("new00002", 3, "Window", False, None)
    with pytest.raises(Refused, match="bad_card"):
        apply([{"op": "sub_add", "card": "card01", "id": "new00003", "text": "Too deep", "checked": False, "parent": "new00002"}], deep)


def test_sub_add_to_a_card_without_subtasks():
    after = apply([{"op": "sub_add", "card": "card02", "id": "new00001", "text": "Fill the can", "checked": False}])
    assert "    after: [[#^card01|Plan the trip]], [[General tasks#^gen001|Buy soil]]\n    - [ ] Fill the can ^new00001\n- [ ] Plain" in after
    plain = apply([{"op": "sub_add", "card": "card03", "id": "new00002", "text": "Step", "checked": False}])
    assert "- [ ] Plain card ^card03\n    - [ ] Step ^new00002\n\n\n## Done" in plain


def test_sub_add_writes_tabs_on_a_tabbed_board():
    after = apply(
        [
            {"op": "sub_add", "card": "card03", "id": "new00001", "text": "Step", "checked": False},
            {"op": "sub_add", "card": "card03", "id": "new00002", "text": "Inner", "checked": False, "parent": "new00001"},
            {"op": "after_set", "id": "card03", "links": [{"board": NAME, "id": "card01", "label": "Trip"}]},
        ],
        TABBED,
    )
    assert "- [ ] Plain card ^card03\n\tafter: [[#^card01|Trip]]\n\t- [ ] Step ^new00001\n\t\t- [ ] Inner ^new00002\n" in after


def test_sub_add_at_the_end_of_a_file_without_a_newline():
    bare = "---\nkanban-starlane: board\n---\n\n## Todo\n\n- [ ] Card ^card01"
    after = apply([{"op": "sub_add", "card": "card01", "id": "new00001", "text": "Step", "checked": False}], bare)
    assert after.endswith("- [ ] Card ^card01\n    - [ ] Step ^new00001\n")


def test_sub_text_keeps_indent_mark_date_and_id():
    after = apply([{"op": "sub_text", "card": "card01", "id": "sub002", "text": "Book the night train"}])
    assert changed_lines(BOARD, after) == (
        ["    - [ ] Book the train @{2026-11-17} ^sub002\n"],
        ["    - [ ] Book the night train @{2026-11-17} ^sub002\n"],
    )


def test_sub_check_changes_only_the_box():
    after = apply([{"op": "sub_check", "card": "card01", "id": "sub003", "checked": True}])
    assert changed_lines(BOARD, after) == (["        - [ ] Compare fares ^sub003\n"], ["        - [x] Compare fares ^sub003\n"])
    back = apply([{"op": "sub_check", "card": "card01", "id": "sub001", "checked": False}])
    assert "    - [ ] Pick dates ^sub001\n" in back
    # The card stays where it is: a subtask does not move its card.
    assert card(after, "card01")["lane"] == "Todo"


def test_sub_date_sets_and_clears():
    after = apply([{"op": "sub_date", "card": "card01", "id": "sub001", "date": "2026-11-12"}])
    assert changed_lines(BOARD, after) == (["    - [x] Pick dates ^sub001\n"], ["    - [x] Pick dates @{2026-11-12} ^sub001\n"])
    cleared = apply([{"op": "sub_date", "card": "card01", "id": "sub002", "date": None}])
    assert "    - [ ] Book the train ^sub002\n" in cleared


def test_sub_remove_takes_its_block_and_its_children():
    after = apply([{"op": "sub_remove", "card": "card01", "id": "sub002"}])
    assert changed_lines(BOARD, after) == (
        [
            "    - [ ] Book the train @{2026-11-17} ^sub002\n",
            "        - [ ] Compare fares ^sub003\n",
            "          a loose note under the fares\n",
            "        - [?] Seat choice\n",
        ],
        [],
    )
    assert [s[0] for s in subs(after, "card01")] == ["sub001", "sub004"]
    leaf = apply([{"op": "sub_remove", "card": "card01", "id": "sub003"}])
    assert changed_lines(BOARD, leaf) == (["        - [ ] Compare fares ^sub003\n", "          a loose note under the fares\n"], [])
    last = apply([{"op": "sub_remove", "card": "card01", "id": "sub004"}])
    assert changed_lines(BOARD, last) == (["    - [ ] Pack ^sub004\n", "            - [ ] jumps too deep\n"], [])


def test_sub_ops_reach_only_the_named_cards_subtasks():
    for bad in (
        {"op": "sub_remove", "card": "card02", "id": "sub001"},
        {"op": "sub_remove", "card": "arch01", "id": "arch02"},
        {"op": "sub_text", "card": "card01", "id": "nope0000", "text": "x"},
    ):
        with pytest.raises(Refused, match="card_missing"):
            apply([bad])


# -- after_set ---------------------------------------------------------------------


def test_after_set_creates_the_line_right_under_the_card():
    after = apply([{"op": "after_set", "id": "card01", "links": [{"board": OTHER, "id": "gen002", "label": "Get a map"}]}])
    assert "- [ ] Plan the trip @{2026-11-14} ^card01\n    after: [[General tasks#^gen002|Get a map]]\n    - [x] Pick dates ^sub001\n" in after
    assert changed_lines(BOARD, after) == ([], ["    after: [[General tasks#^gen002|Get a map]]\n"])
    assert card(after, "card01")["after"] == [{"board": OTHER, "id": "gen002"}]
    assert subs(after, "card01") == EXPECTED


def test_after_set_rewrites_and_removes():
    after = apply([{"op": "after_set", "id": "card02", "links": [{"board": NAME, "id": "card03", "label": "Plain, card"}]}])
    assert changed_lines(BOARD, after) == (
        ["    after: [[#^card01|Plan the trip]], [[General tasks#^gen001|Buy soil]]\n"],
        ["    after: [[#^card03|Plain, card]]\n"],
    )
    assert card(after, "card02")["after"] == [{"board": NAME, "id": "card03"}]
    gone = apply([{"op": "after_set", "id": "card02", "links": []}])
    assert changed_lines(BOARD, gone) == (["    after: [[#^card01|Plan the trip]], [[General tasks#^gen001|Buy soil]]\n"], [])
    assert card(gone, "card02")["after"] == []


def test_after_set_drops_extra_text_only_when_it_rewrites():
    board = BOARD.replace("Buy soil]]\n", "Buy soil]] (maybe)\n")
    assert card(board, "card02")["after_raw_extra"] is True
    assert apply([{"op": "sub_add", "card": "card02", "id": "new00001", "text": "x", "checked": False}], board).count("(maybe)") == 1
    rewritten = apply([{"op": "after_set", "id": "card02", "links": [{"board": OTHER, "id": "gen001", "label": "Buy soil"}]}], board)
    assert "(maybe)" not in rewritten and card(rewritten, "card02")["after_raw_extra"] is False


def test_a_titles_line_round_trips_and_after_set_writes_only_links():
    board = with_after("    after: Wiz MCP, [[#^card01|Plan the trip]],  Buy soil ")
    # Another op leaves the titles line byte for byte.
    other = apply([{"op": "sub_add", "card": "card02", "id": "new00001", "text": "x", "checked": False}], board)
    assert changed_lines(board, other) == ([], ["    - [ ] x ^new00001\n"])
    links = [
        {"board": NAME, "id": "card03", "label": "Wiz MCP"},
        {"board": NAME, "id": "card01", "label": "Plan the trip"},
        {"board": OTHER, "id": "gen001", "label": "Buy soil"},
    ]
    rewritten = apply([{"op": "after_set", "id": "card02", "links": links}], board)
    assert changed_lines(board, rewritten)[1] == [
        "    after: [[#^card03|Wiz MCP]], [[#^card01|Plan the trip]], [[General tasks#^gen001|Buy soil]]\n"
    ]
    second = card(rewritten, "card02", titles=True)
    assert second["after_titles"] == [] and second["after_raw_extra"] is False
    assert second["after"] == [{"board": NAME, "id": "card03"}, {"board": NAME, "id": "card01"}, {"board": OTHER, "id": "gen001"}]


def _link(**kw):
    return {"board": OTHER, "id": "gen001", "label": "Buy soil", **kw}


@pytest.mark.parametrize(
    "links",
    [
        "card01",
        [_link(board="Secret plans.md")],
        [_link(board="General tasks")],
        [_link(board=5)],
        [_link(id="BAD ID")],
        [_link(label="a ] b")],
        [_link(label="a | b")],
        [_link(label="a [ b")],
        [_link(label="")],
        [_link(label="two\nlines")],
        [_link(label="ends ^blockid")],
        [_link(extra=1)],
        [{"board": OTHER, "id": "gen001"}],
        [_link(), _link()],
        [_link(id=f"gen{i:04d}") for i in range(BOARD_AFTER_LINKS_MAX + 1)],
    ],
)
def test_a_bad_after_set_is_refused(links):
    with pytest.raises(Refused, match="bad_card"):
        apply([{"op": "after_set", "id": "card01", "links": links}])


def test_after_set_limits_and_its_name():
    ok = apply([{"op": "after_set", "id": "card01", "links": [_link(id=f"gen{i:04d}") for i in range(BOARD_AFTER_LINKS_MAX)]}])
    assert len(card(ok, "card01")["after"]) == BOARD_AFTER_LINKS_MAX
    with pytest.raises(Refused, match="bad_card"):
        apply([{"op": "after_set", "id": "card01", "links": []}], name=None)
    with pytest.raises(Refused, match="card_missing"):
        apply([{"op": "after_set", "id": "nope0000", "links": []}])


# -- whole cards still carry their subtasks ------------------------------------------


def test_checking_a_card_moves_its_subtasks_with_it():
    after = apply([{"op": "check", "id": "card01", "checked": True}])
    block = BOARD[BOARD.index("- [ ] Plan the trip") : BOARD.index("- [ ] Water")].replace("- [ ] Plan", "- [x] Plan", 1)
    assert after.split("**Complete**\n")[1].startswith("- [x] Old card ^done01\n    - [x] Old sub ^sub009\n" + block)
    assert card(after, "card01")["lane"] == "Done" and subs(after, "card01") == EXPECTED
    assert "Pick dates" not in after.split("## Done")[0]


def test_removing_a_card_removes_its_subtasks():
    after = apply([{"op": "remove", "id": "card01"}])
    assert after == BOARD.replace(BOARD[BOARD.index("- [ ] Plan the trip") : BOARD.index("- [ ] Water")], "")


def test_text_date_and_tag_touch_only_the_first_line():
    after = apply([{"op": "text", "id": "card01", "text": "Plan the big trip"}, {"op": "date", "id": "card01", "date": None}])
    assert changed_lines(BOARD, after) == (["- [ ] Plan the trip @{2026-11-14} ^card01\n"], ["- [ ] Plan the big trip ^card01\n"])
    untagged = BOARD.replace("- [ ] Plain card ^card03\n", "- [ ] Plain card\n    - [ ] Inner ^inn001\n")
    tagged = apply([{"op": "tag", "n": 2, "id": "card03"}], untagged)
    assert changed_lines(untagged, tagged) == (["- [ ] Plain card\n"], ["- [ ] Plain card ^card03\n"])
    # A new card's id may not be a subtask's.
    with pytest.raises(Refused, match="bad_card"):
        apply([{"op": "add", "id": "sub001", "text": "New"}])


# -- limits, refusals, dates=False ---------------------------------------------------


def test_a_card_holds_at_most_so_many_subtasks():
    ops = [{"op": "sub_add", "card": "card03", "id": f"s{i:05d}", "text": f"Step {i}", "checked": False} for i in range(BOARD_SUBS_PER_CARD_MAX)]
    full = apply(ops)
    assert len(card(full, "card03")["subs"]) == BOARD_SUBS_PER_CARD_MAX
    with pytest.raises(Refused, match="bad_card"):
        apply([{"op": "sub_add", "card": "card03", "id": "late0001", "text": "One more", "checked": False}], full)


@pytest.mark.parametrize(
    "op",
    [
        {"op": "sub_add", "card": "card01", "id": "new00001", "text": "x"},
        {"op": "sub_add", "card": "card01", "id": "new00001", "text": "x", "checked": "no"},
        {"op": "sub_add", "card": "card01", "id": "new00001", "text": "x", "checked": False, "lane": "Todo"},
        {"op": "sub_add", "card": "card01", "id": "sub001", "text": "x", "checked": False},
        {"op": "sub_add", "card": "card01", "id": "card02", "text": "x", "checked": False},
        {"op": "sub_add", "card": "card01", "id": "new00001", "text": "has @{2026-01-01}", "checked": False},
        {"op": "sub_add", "card": "card01", "id": "new00001", "text": "x", "checked": False, "date": "soon"},
        {"op": "sub_add", "card": "card01", "id": "new00001", "text": "x", "checked": False, "parent": "nope0000"},
        {"op": "sub_text", "card": "card01", "id": "sub001", "text": "two\nlines"},
        {"op": "sub_text", "card": "card01", "id": "sub001"},
        {"op": "sub_check", "card": "card01", "id": "sub001", "checked": 1},
        {"op": "sub_date", "card": "card01", "id": "sub001", "date": "2026-13"},
        {"op": "sub_remove", "card": "card01", "id": "sub001", "extra": True},
        {"op": "sub_nuke", "card": "card01"},
        {"op": "after_set", "id": "card01"},
    ],
)
def test_a_malformed_sub_operation_refuses_the_whole_batch(op):
    with pytest.raises(Refused):
        apply([{"op": "sub_check", "card": "card01", "id": "sub002", "checked": True}, op])


def test_a_board_with_its_own_date_syntax_takes_no_sub_dates():
    custom = BOARD.replace('{"kanban-starlane":"board"}', '{"kanban-starlane":"board","date-format":"DD.MM.YYYY"}')
    assert subs(custom, "card01")[1] == ("sub002", 1, "Book the train @{2026-11-17}", False, None)
    for bad in (
        {"op": "sub_date", "card": "card01", "id": "sub001", "date": "2026-11-12"},
        {"op": "sub_add", "card": "card01", "id": "new00001", "text": "x", "checked": False, "date": "2026-11-12"},
    ):
        with pytest.raises(Refused, match="bad_card"):
            apply([bad], custom)
    after = apply([{"op": "sub_add", "card": "card01", "id": "new00001", "text": "x", "checked": False, "date": None}], custom)
    assert "    - [ ] x ^new00001\n" in after
    assert apply([{"op": "sub_check", "card": "card01", "id": "sub002", "checked": True}], custom).count("    - [x] Book the train @{2026-11-17} ^sub002\n") == 1


# -- the routes ----------------------------------------------------------------------


@pytest.fixture
def base(vault: Path):
    write(vault, "Anchor/settings.md", SETTINGS)
    write(vault, NAME, BOARD)


async def test_get_carries_subtasks_only_when_asked(client, base):
    plain = (await (await client.get("/v1/boards", headers=AUTH)).json())["boards"][0]
    assert set(plain["cards"][0]) == {"n", "id", "lane", "text", "checked", "date"}
    full = (await (await client.get("/v1/boards", params={"subs": "1"}, headers=AUTH)).json())["boards"][0]
    first, second = full["cards"][0], full["cards"][1]
    assert [s["id"] for s in first["subs"]] == ["sub001", "sub002", "sub003", None, "sub004"]
    assert first["subs"][1] == {"n": 1, "id": "sub002", "depth": 1, "text": "Book the train", "checked": False, "date": "2026-11-17"}
    assert second["after"] == [{"board": NAME, "id": "card01"}, {"board": OTHER, "id": "gen001"}]
    assert second["after_raw_extra"] is False
    assert "after_titles" not in second
    titled = (await (await client.get("/v1/boards", params={"subs": "1", "titles": "1"}, headers=AUTH)).json())["boards"][0]
    assert titled["cards"][1]["after_titles"] == [] and titled["cards"][0]["after_titles"] == []
    # `titles` alone is not `subs`.
    alone = (await (await client.get("/v1/boards", params={"titles": "1"}, headers=AUTH)).json())["boards"][0]
    assert set(alone["cards"][0]) == {"n", "id", "lane", "text", "checked", "date"}


async def test_get_reports_titles_only_when_asked(client, vault: Path, base):
    write(vault, NAME, with_after("    after: Wiz MCP, [[#^card01|Plan the trip]]"))
    old = (await (await client.get("/v1/boards", params={"subs": "1"}, headers=AUTH)).json())["boards"][0]["cards"][1]
    assert set(old) == {"n", "id", "lane", "text", "checked", "date", "subs", "after", "after_raw_extra"}
    assert old["after_raw_extra"] is True
    new = (await (await client.get("/v1/boards", params={"subs": "1", "titles": "1"}, headers=AUTH)).json())["boards"][0]["cards"][1]
    assert new["after_titles"] == ["Wiz MCP"] and new["after_raw_extra"] is False
    assert new["after"] == [{"board": NAME, "id": "card01"}]


async def test_post_links_only_to_named_boards(client, vault: Path, base):
    sha = hashlib.sha256(BOARD.encode()).hexdigest()
    bad = [{"op": "after_set", "id": "card03", "links": [{"board": "Secret plans.md", "id": "abcd1234", "label": "x"}]}]
    resp = await client.post("/v1/boards", json={"name": NAME, "if_sha256": sha, "ops": bad, "changeset": "boards_a"}, headers=AUTH)
    assert resp.status == 403
    good = [{"op": "after_set", "id": "card03", "links": [{"board": OTHER, "id": "abcd1234", "label": "Soil"}]}]
    resp = await client.post("/v1/boards", json={"name": NAME, "if_sha256": sha, "ops": good, "changeset": "boards_a"}, headers=AUTH)
    assert resp.status == 200
    assert "- [ ] Plain card ^card03\n    after: [[General tasks#^abcd1234|Soil]]\n" in (vault / NAME).read_text()
