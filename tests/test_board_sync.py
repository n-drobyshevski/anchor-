"""app/planner/board_sync.py: the board <-> planner merge, every rule.

Pure: no database, no vault, no planner. Every title here is synthetic.
Each rule is checked on its own, and then the idempotence property: the
merge run again on the state its own output produced does nothing.
"""

from __future__ import annotations

import pytest

from app.planner import board_sync as bs
from app.planner.board_sync import Board, Card, Row, Task

NAME = "Work tasks.md"
TAG = "work"


def board(*cards: Card, dates: bool = True) -> Board:
    return Board(name=NAME, dates=dates, cards=tuple(Card(n=i, id=c.id, text=c.text, checked=c.checked, date=c.date) for i, c in enumerate(cards)))


def card(text: str, id: str | None = None, *, checked: bool = False, date: str | None = None) -> Card:
    return Card(n=0, id=id, text=text, checked=checked, date=date)


def task(id: str, title: str, *, done: bool = False, due: str | None = None, tags=(TAG,)) -> Task:
    return Task(id=id, title=title, done=done, due=due, tags=tuple(tags))


def row(block_id: str, task_id: str, title: str, *, done: bool = False, due: str | None = None) -> Row:
    return Row(block_id=block_id, task_id=task_id, title=title, done=done, due=due)


def merge(b: Board, tasks, rows, **kw) -> bs.Plan:
    return bs.merge(b, list(tasks), list(rows), tag=TAG, **kw)


def settle(b: Board, tasks, rows, *, passes: int = 1, **kw):
    """Run `passes` passes, each applied in full; return the final state and the last plan."""
    tasks, rows = list(tasks), list(rows)
    plan = None
    for _ in range(passes):
        plan = merge(b, tasks, rows, **kw)
        b, tasks, rows = bs.apply_to_state(b, tasks, rows, plan, tag=TAG)
    return b, tasks, rows, plan


def assert_idle(b: Board, tasks, rows, **kw) -> bs.Plan:
    plan = merge(b, tasks, rows, **kw)
    assert plan.ops == [] and plan.creates == [] and plan.calls == [] and plan.row_changes == []
    return plan


# -- one side changed ----------------------------------------------------------------


def test_a_card_retitled_on_the_board_retitles_the_task():
    plan = merge(board(card("Отчёт v2", "aaaa1")), [task("t1", "Отчёт")], [row("aaaa1", "t1", "Отчёт")])
    assert plan.calls == [bs.Call("title", "t1", "Отчёт v2")]
    assert plan.ops == []
    assert plan.row_changes[0].title == "Отчёт v2"
    assert (plan.counts.tasks_changed, plan.counts.conflicts) == (1, 0)


def test_a_task_retitled_in_the_planner_retitles_the_card():
    plan = merge(board(card("Отчёт", "aaaa1")), [task("t1", "Отчёт v2")], [row("aaaa1", "t1", "Отчёт")])
    assert plan.ops == [{"op": "text", "id": "aaaa1", "text": "Отчёт v2"}]
    assert plan.calls == []
    assert plan.counts.cards_changed == 1


def test_checked_on_the_board_completes_the_task_and_back():
    plan = merge(board(card("A", "aaaa1", checked=True)), [task("t1", "A")], [row("aaaa1", "t1", "A")])
    assert plan.calls == [bs.Call("done", "t1", True)]
    plan = merge(board(card("A", "aaaa1")), [task("t1", "A", done=True)], [row("aaaa1", "t1", "A", done=True)])
    assert plan.calls == [bs.Call("done", "t1", False)]


def test_done_in_the_planner_checks_the_card():
    plan = merge(board(card("A", "aaaa1")), [task("t1", "A", done=True)], [row("aaaa1", "t1", "A")])
    assert plan.ops == [{"op": "check", "id": "aaaa1", "checked": True}]


def test_due_dates_travel_both_ways_and_can_be_cleared():
    plan = merge(board(card("A", "aaaa1", date="2026-11-12")), [task("t1", "A")], [row("aaaa1", "t1", "A")])
    assert plan.calls == [bs.Call("due", "t1", "2026-11-12")]
    plan = merge(board(card("A", "aaaa1")), [task("t1", "A", due="2026-11-12")], [row("aaaa1", "t1", "A", due="2026-11-12")])
    assert plan.calls == [bs.Call("due", "t1", None)]
    plan = merge(board(card("A", "aaaa1")), [task("t1", "A", due="2026-11-13")], [row("aaaa1", "t1", "A")])
    assert plan.ops == [{"op": "date", "id": "aaaa1", "date": "2026-11-13"}]
    plan = merge(board(card("A", "aaaa1", date="2026-11-13")), [task("t1", "A")], [row("aaaa1", "t1", "A", due="2026-11-13")])
    assert plan.ops == [{"op": "date", "id": "aaaa1", "date": None}]


def test_fields_are_merged_one_by_one():
    """The board moved the title, the planner the date: both travel."""
    plan = merge(
        board(card("A2", "aaaa1")), [task("t1", "A", due="2026-11-20")], [row("aaaa1", "t1", "A")]
    )
    assert plan.calls == [bs.Call("title", "t1", "A2")]
    assert plan.ops == [{"op": "date", "id": "aaaa1", "date": "2026-11-20"}]
    assert plan.counts.conflicts == 0


def test_the_same_change_on_both_sides_only_moves_the_snapshot():
    plan = merge(board(card("A2", "aaaa1")), [task("t1", "A2")], [row("aaaa1", "t1", "A")])
    assert plan.ops == [] and plan.calls == []
    assert plan.row_changes == [bs.RowChange("update", "aaaa1", "t1", "A2", False, None)]
    assert plan.counts.conflicts == 0


def test_whitespace_and_normal_form_are_not_a_change():
    plan = merge(board(card("Ёлка", "aaaa1")), [task("t1", "Ёлка  ")], [row("aaaa1", "t1", "Ёлка")])
    assert plan.ops == [] and plan.calls == [] and plan.row_changes == []


# -- conflicts ------------------------------------------------------------------------


def test_both_sides_changed_the_planner_wins_and_it_counts():
    plan = merge(board(card("A board", "aaaa1")), [task("t1", "A planner")], [row("aaaa1", "t1", "A")])
    assert plan.ops == [{"op": "text", "id": "aaaa1", "text": "A planner"}]
    assert plan.calls == []
    assert plan.counts.conflicts == 1


def test_a_done_conflict_and_a_date_conflict_each_count():
    plan = merge(
        board(card("A", "aaaa1", checked=False, date="2026-11-01")),
        [task("t1", "A", done=False, due="2026-11-02")],
        [row("aaaa1", "t1", "A", done=True, due="2026-11-03")],
    )
    # done: board went True->False, planner True->False: same, no conflict.
    # due: both moved, differently: the planner wins.
    assert plan.ops == [{"op": "date", "id": "aaaa1", "date": "2026-11-02"}]
    assert plan.counts.conflicts == 1


# -- new on the board -----------------------------------------------------------------


def test_a_new_card_without_an_id_becomes_a_tagged_task_and_gets_an_id():
    plan = merge(board(card("Позвонить", checked=False, date="2026-11-15")), [], [])
    (create,) = plan.creates
    assert (create.title, create.due, create.done, create.tag) == ("Позвонить", "2026-11-15", False, TAG)
    assert bs.ID_RE.match(create.block_id)
    assert plan.ops == [{"op": "tag", "n": 0, "id": create.block_id}]
    assert create.request_id == bs.request_id(NAME, create.block_id)
    assert len(create.request_id) <= 100
    (change,) = plan.row_changes
    assert (change.op, change.block_id, change.task_id) == ("insert", create.block_id, None)
    assert plan.counts.tasks_created == 1 and plan.counts.cards_tagged == 1


def test_a_new_card_with_an_unknown_id_keeps_it_and_needs_no_tag_op():
    plan = merge(board(card("Позвонить", "plugin1", checked=True)), [], [])
    (create,) = plan.creates
    assert create.block_id == "plugin1" and create.done is True
    assert plan.ops == []


def test_the_minted_id_and_request_id_are_stable_across_passes():
    """A create retried after a lost board write asks with the same id."""
    first = merge(board(card("Позвонить")), [], []).creates[0]
    again = merge(board(card("Позвонить")), [], []).creates[0]
    assert first == again


def test_two_untracked_cards_with_the_same_text_get_different_ids():
    plan = merge(board(card("Дубль"), card("Дубль")), [], [])
    ids = {c.block_id for c in plan.creates}
    assert len(ids) == 2


def test_a_minted_id_never_collides_with_an_existing_one():
    wanted = bs.mint_for_card(NAME, "X", 0)
    plan = merge(board(card("Other", wanted), card("X")), [], [])
    ids = [op["id"] for op in plan.ops if op["op"] == "tag"]
    assert ids and ids[0] != wanted


def test_dates_off_board_creates_without_a_due():
    plan = merge(board(card("A @{2026-11-15}"), dates=False), [], [])
    (create,) = plan.creates
    assert create.due is None and create.title == "A @{2026-11-15}"


def test_hashtags_and_time_stay_in_the_title():
    plan = merge(board(card("Созвон #срочно @@{10:30}")), [], [])
    assert plan.creates[0].title == "Созвон #срочно @@{10:30}"


@pytest.mark.parametrize("text", ["", "я" * 501])
def test_a_card_text_the_planner_cannot_take_is_skipped(text):
    plan = merge(board(card(text), card("Хорошая")), [], [])
    assert [c.title for c in plan.creates] == ["Хорошая"]
    assert plan.counts.skipped == 1


# -- new in the planner ---------------------------------------------------------------


def test_a_new_tagged_task_becomes_a_card():
    plan = merge(board(), [task("t1", "Купить хлеб", due="2026-11-11")], [])
    (op,) = plan.ops
    assert op == {"op": "add", "id": bs.mint_for_task(NAME, "t1"), "text": "Купить хлеб", "date": "2026-11-11"}
    assert plan.row_changes == [bs.RowChange("insert", op["id"], "t1", "Купить хлеб", False, "2026-11-11")]
    assert plan.counts.cards_added == 1


def test_a_done_task_with_no_row_is_not_added():
    plan = merge(board(), [task("t1", "Давно сделано", done=True)], [])
    assert plan.ops == [] and plan.row_changes == []


def test_a_new_task_on_a_dates_off_board_goes_without_its_date():
    plan = merge(board(dates=False), [task("t1", "A", due="2026-11-11")], [])
    assert plan.ops == [{"op": "add", "id": bs.mint_for_task(NAME, "t1"), "text": "A"}]


@pytest.mark.parametrize(
    "title",
    [
        "две\nстроки",
        "%% комментарий",
        "срок @{2026-11-12}",
        "хвост ^abc1",
        "я" * 501,
        "таб\tвнутри",
        "   ",
    ],
)
def test_a_title_vaultd_would_refuse_is_skipped_and_the_rest_still_go(title):
    plan = merge(board(), [task("t1", title), task("t2", "Нормальная")], [])
    assert [op["text"] for op in plan.ops] == ["Нормальная"]
    assert plan.counts.skipped == 1


def test_the_card_text_screen_mirrors_vaultd():
    assert bs.card_text_ok("Созвон #срочно @@{10:30}")
    assert bs.card_text_ok("  с пробелами  ")
    assert bs.card_text_ok("я" * 500)
    for bad in ("", "a\rb", "a b", "a %% b", "x ^id-1", "x @{2026-01-01}", "x@{2026-01-01}", "я" * 501):
        assert not bs.card_text_ok(bad), bad


def test_a_planner_retitle_that_cannot_be_a_card_is_held_not_written():
    rows = [row("aaaa1", "t1", "A")]
    plan = merge(board(card("A", "aaaa1")), [task("t1", "A\nB")], rows)
    assert plan.ops == [] and plan.calls == []
    assert plan.counts.skipped == 1
    (change,) = plan.row_changes
    assert change.title == "A" and change.status == bs.ROW_HELD
    # Held: the next pass skips it again and changes nothing.
    held = [row("aaaa1", "t1", "A")]
    held = [bs.Row("aaaa1", "t1", "A", False, None, bs.ROW_HELD)]
    again = merge(board(card("A", "aaaa1")), [task("t1", "A\nB")], held)
    assert again.ops == [] and again.calls == [] and again.row_changes == []
    assert again.counts.skipped == 1


def test_a_card_retitle_the_planner_cannot_take_is_held():
    plan = merge(board(card("я" * 600, "aaaa1")), [task("t1", "A")], [row("aaaa1", "t1", "A")])
    assert plan.calls == [] and plan.counts.skipped == 1


def test_a_planner_due_that_is_not_a_date_is_held():
    plan = merge(board(card("A", "aaaa1")), [task("t1", "A", due="someday")], [row("aaaa1", "t1", "A")])
    assert plan.ops == [] and plan.counts.skipped == 1
    plan = merge(board(), [task("t1", "A", due="someday")], [])
    assert plan.ops == [{"op": "add", "id": bs.mint_for_task(NAME, "t1"), "text": "A"}]
    assert plan.row_changes[0].status == bs.ROW_HELD


# -- gone -----------------------------------------------------------------------------


def test_a_card_gone_from_the_board_untags_the_task_and_drops_the_row():
    plan = merge(board(), [task("t1", "A", tags=("дом", TAG, "x"))], [row("aaaa1", "t1", "A")])
    assert plan.calls == [bs.Call("untag", "t1", ("дом", "x"))]
    assert plan.ops == []
    assert plan.row_changes == [bs.RowChange("delete", "aaaa1")]
    assert plan.counts.tasks_untagged == 1


def test_a_task_gone_from_the_list_removes_the_card():
    plan = merge(board(card("A", "aaaa1")), [], [row("aaaa1", "t1", "A")])
    assert plan.ops == [{"op": "remove", "id": "aaaa1"}]
    assert plan.calls == []
    assert plan.row_changes == [bs.RowChange("delete", "aaaa1")]


def test_both_gone_drops_the_row_and_touches_nothing():
    plan = merge(board(card("other", "zzzz9")), [task("t9", "other planner")], [row("aaaa1", "t1", "A")])
    deletes = [c for c in plan.row_changes if c.op == "delete"]
    assert deletes == [bs.RowChange("delete", "aaaa1")]
    assert not [op for op in plan.ops if op["op"] == "remove"]
    assert not [c for c in plan.calls if c.kind == "untag"]


def test_dates_off_leaves_both_dates_alone():
    plan = merge(
        board(card("A @{2026-11-01}", "aaaa1"), dates=False),
        [task("t1", "A @{2026-11-01}", due="2026-12-01")],
        [row("aaaa1", "t1", "A @{2026-11-01}")],
    )
    assert plan.ops == [] and plan.calls == [] and plan.row_changes == []


# -- pairing --------------------------------------------------------------------------


def test_first_run_pairs_the_same_title_instead_of_duplicating():
    b = board(card("Отчёт"), card("Только на доске"), card("ПОЗВОНИТЬ  "))
    tasks = [task("t1", "отчёт"), task("t2", "Позвонить", done=True), task("t3", "Только в планере")]
    plan = merge(b, tasks, [])
    assert plan.counts.paired == 2
    assert [c.title for c in plan.creates] == ["Только на доске"]
    assert [op["text"] for op in plan.ops if op["op"] == "add"] == ["Только в планере"]
    # The planner wins a fresh pair: the card takes its title and done.
    assert {"op": "text", "id": plan.ops[0]["id"], "text": "отчёт"} in plan.ops
    checks = [op for op in plan.ops if op["op"] == "check"]
    assert len(checks) == 1 and checks[0]["checked"] is True
    inserted = {c.task_id for c in plan.row_changes if c.task_id}
    assert {"t1", "t2", "t3"} == inserted
    assert plan.counts.conflicts == 0


def test_duplicate_titles_pair_one_to_one_in_order():
    plan = merge(board(card("Дубль"), card("Дубль"), card("Дубль")), [task("t2", "Дубль"), task("t1", "Дубль")], [])
    assert plan.counts.paired == 2
    assert len(plan.creates) == 1
    paired = [c.task_id for c in plan.row_changes if c.task_id]
    assert paired == ["t1", "t2"]


def test_a_card_carrying_the_id_minted_for_a_task_pairs_with_it():
    """The board write of an `add` landed but the rows were never committed."""
    minted = bs.mint_for_task(NAME, "t1")
    plan = merge(board(card("Retitled meanwhile", minted)), [task("t1", "Original")], [])
    assert plan.creates == []
    assert plan.row_changes[0].task_id == "t1"
    assert plan.ops == [{"op": "text", "id": minted, "text": "Original"}]


def test_pairing_also_runs_when_rows_exist():
    """After a lost board write the planner already has the new task."""
    rows = [row("aaaa1", "t1", "A")]
    plan = merge(board(card("A", "aaaa1"), card("Новая")), [task("t1", "A"), task("t2", "Новая")], rows)
    assert plan.creates == []
    assert plan.counts.paired == 1


def test_a_task_with_no_row_is_never_touched_beyond_the_new_rules():
    plan = merge(board(card("A", "aaaa1")), [task("t1", "A"), task("t2", "B", tags=(TAG, "дом"))], [row("aaaa1", "t1", "A")])
    assert plan.calls == []
    assert [op["op"] for op in plan.ops] == ["add"]


def test_a_copied_card_with_a_tracked_id_is_left_alone():
    plan = merge(board(card("A", "aaaa1"), card("A copy", "aaaa1")), [task("t1", "A")], [row("aaaa1", "t1", "A")])
    assert plan.ops == [] and plan.creates == [] and plan.calls == []


# -- adopting untagged tasks ----------------------------------------------------------


def test_a_free_card_adopts_an_untagged_task_of_the_same_title():
    b = board(card("Отчёт", date="2026-11-20"), card("Только на доске"))
    loose = [task("u1", "отчёт ", due="2026-11-12", tags=())]
    plan = merge(b, [], [], untagged=loose)
    assert plan.counts.paired == 1 and plan.counts.tasks_changed == 1
    assert [c.title for c in plan.creates] == ["Только на доске"]
    assert bs.Call("tag", "u1", (TAG,)) in plan.calls
    inserted = [c for c in plan.row_changes if c.task_id == "u1"]
    assert len(inserted) == 1
    # A fresh pair: the planner's title and due win, as with a tagged one.
    block = inserted[0].block_id
    assert {"op": "text", "id": block, "text": "отчёт"} in plan.ops
    assert {"op": "date", "id": block, "date": "2026-11-12"} in plan.ops


def test_only_open_tasks_with_no_tag_at_all_are_adopted():
    b = board(card("A"), card("B"), card("C"))
    loose = [task("u1", "A", tags=("дом",)), task("u2", "B", done=True, tags=()), task("u3", "C", tags=())]
    plan = merge(b, [], [], untagged=loose)
    assert [c.task_id for c in plan.calls] == ["u3"]
    assert sorted(c.title for c in plan.creates) == ["A", "B"]


def test_a_tagged_task_pairs_before_an_untagged_one_is_adopted():
    b = board(card("A"), card("A"))
    plan = merge(b, [task("t1", "A")], [], untagged=[task("u1", "A", tags=()), task("u2", "A", tags=())])
    assert plan.counts.paired == 2 and plan.creates == []
    assert [c.task_id for c in plan.calls if c.kind == "tag"] == ["u1"]


def test_an_untagged_task_with_no_card_is_never_touched():
    plan = merge(board(card("A")), [], [], untagged=[task("u1", "Другое", tags=())])
    assert plan.calls == [] and [op["op"] for op in plan.ops] == ["tag"]
    assert [c.title for c in plan.creates] == ["A"]


def test_an_adopted_task_already_in_a_row_or_the_tagged_list_is_ignored():
    rows = [row("aaaa1", "t1", "A")]
    plan = merge(board(card("A", "aaaa1"), card("B")), [task("t1", "A")], rows, untagged=[task("t1", "B", tags=())])
    assert plan.calls == [] and [c.title for c in plan.creates] == ["B"]


def test_adoption_then_nothing():
    b = board(card("Отчёт"), card("Позвонить", checked=True))
    loose = [task("u1", "Отчёт", tags=()), task("u2", "позвонить", tags=())]
    plan = merge(b, [], [], untagged=loose)
    b, tasks, rows = bs.apply_to_state(b, [], [], plan, tag=TAG, untagged=loose)
    assert {t.id: t.tags for t in tasks} == {"u1": (TAG,), "u2": (TAG,)}
    # The card's done wins nothing on a fresh pair: the planner's open stays.
    assert [c.checked for c in b.cards] == [False, False]
    assert_idle(b, tasks, rows)


# -- caps and order -------------------------------------------------------------------


def test_at_most_a_hundred_ops_and_the_rest_waits():
    tasks = [task(f"t{i:03}", f"Задача {i}") for i in range(130)]
    plan = merge(board(), tasks, [])
    assert len(plan.ops) == bs.OPS_MAX
    assert plan.counts.waiting == 30


def test_creates_are_capped_and_the_rest_waits():
    b = board(*(card(f"Карта {i}") for i in range(25)))
    plan = merge(b, [], [], creates_max=20)
    assert len(plan.creates) == 20 and plan.counts.waiting == 5
    plan = merge(b, [], [], creates_max=3)
    assert len(plan.creates) == 3


def test_a_board_is_never_filled_past_vaultds_card_limit():
    b = board(*(card(f"c{i}", f"id{i:04}") for i in range(498)))
    rows = [row(f"id{i:04}", f"r{i}", f"c{i}") for i in range(498)]
    tasks = [task(f"r{i}", f"c{i}") for i in range(498)] + [task(f"n{i}", f"new {i}") for i in range(5)]
    plan = merge(b, tasks, rows)
    assert plan.counts.cards_added == 2 and plan.counts.waiting == 3


def test_tag_ops_come_first_in_the_batch():
    rows = [row("aaaa1", "t1", "A")]
    plan = merge(board(card("A", "aaaa1"), card("Новая")), [task("t1", "A2"), task("t5", "Из планера")], rows)
    kinds = [op["op"] for op in plan.ops]
    assert kinds == ["tag", "text", "add"]


def test_the_output_is_deterministic():
    b = board(card("x"), card("y", "yyyy1"), card("z"))
    tasks = [task("t3", "c"), task("t1", "a"), task("t2", "y")]
    assert merge(b, tasks, []) == merge(b, list(reversed(tasks)), [])


# -- idempotence ----------------------------------------------------------------------


def test_a_first_run_then_nothing():
    b = board(card("Отчёт"), card("Только на доске", date="2026-11-20"), card("Позвонить", checked=True))
    tasks = [task("t1", "Отчёт", due="2026-11-11"), task("t2", "Только в планере"), task("t3", "позвонить")]
    b, tasks, rows, _ = settle(b, tasks, [])
    assert len(rows) == 4
    assert {r.task_id for r in rows} >= {"t1", "t2", "t3"}
    assert_idle(b, tasks, rows)


def test_every_kind_of_change_then_nothing():
    b = board(
        card("A2", "aaaa1"),
        card("B", "bbbb1", checked=True),
        card("C", "cccc1", date="2026-11-01"),
        card("D", "dddd1"),
        card("Новая"),
    )
    tasks = [
        task("t1", "A"),
        task("t2", "B"),
        task("t3", "C planner", due="2026-11-05"),
        task("t5", "E", tags=(TAG, "дом")),
        task("t6", "Из планера"),
    ]
    rows = [
        row("aaaa1", "t1", "A"),
        row("bbbb1", "t2", "B"),
        row("cccc1", "t3", "C"),
        row("dddd1", "t4", "D"),
        row("eeee1", "t5", "E"),
        row("ffff1", "t7", "F"),
    ]
    plan = merge(b, tasks, rows)
    assert plan.counts.cards_removed == 1  # D: its task left the list
    assert plan.counts.tasks_untagged == 1  # E: its card left the board
    assert plan.counts.tasks_created == 1 and plan.counts.cards_added == 1
    b, tasks, rows = bs.apply_to_state(b, tasks, rows, plan, tag=TAG)
    assert "t5" not in {t.id for t in tasks}
    assert_idle(b, tasks, rows)


def test_a_conflict_then_nothing():
    b, tasks, rows, plan = settle(board(card("A board", "aaaa1")), [task("t1", "A planner")], [row("aaaa1", "t1", "A")])
    assert plan.counts.conflicts == 1
    assert b.cards[0].text == "A planner"
    assert_idle(b, tasks, rows)


def test_capped_work_converges_over_passes_then_nothing():
    b = board(*(card(f"Карта {i}") for i in range(30)))
    tasks = [task(f"t{i:03}", f"Задача {i}") for i in range(120)]
    b, tasks, rows, plan = settle(b, tasks, [], passes=4, creates_max=20)
    assert plan.counts.waiting == 0
    assert len(rows) == 150
    assert_idle(b, tasks, rows, creates_max=20)


def test_held_titles_are_stable():
    b, tasks, rows, _ = settle(board(card("A", "aaaa1")), [task("t1", "A\nB")], [row("aaaa1", "t1", "A")])
    plan = assert_idle(b, tasks, rows)
    assert plan.counts.skipped == 1


def test_a_card_date_that_is_not_a_real_day_moves_nothing():
    plan = merge(board(card("A", date="2026-02-30")), [], [])
    assert plan.creates[0].due is None and plan.counts.skipped == 1
    assert plan.row_changes[0].status == bs.ROW_HELD
    b, tasks, rows, _ = settle(board(card("A", "aaaa1", date="2026-02-30")), [task("t1", "A", due="2026-03-01")], [row("aaaa1", "t1", "A")])
    assert b.cards[0].date == "2026-02-30" and tasks[0].due == "2026-03-01"
    assert assert_idle(b, tasks, rows).counts.skipped == 1


from app.planner import board_sync  # noqa: E402

# -- the mass-loss guard ---------------------------------------------------------------


def _paired(n: int):
    """n pairs in step: cards, tasks and rows."""
    cards = tuple(board_sync.Card(n=i, id=f"id{i:04d}", text=f"Task {i}", checked=False, date=None) for i in range(n))
    tasks = [board_sync.Task(id=f"t{i}", title=f"Task {i}", done=False, tags=("work",)) for i in range(n)]
    rows = [board_sync.Row(block_id=f"id{i:04d}", task_id=f"t{i}", title=f"Task {i}", done=False, due=None) for i in range(n)]
    return cards, tasks, rows


def test_a_planner_that_reads_as_empty_removes_no_cards():
    cards, _tasks, rows = _paired(8)
    board = board_sync.Board(name="Work tasks.md", dates=True, cards=cards)
    plan = board_sync.merge(board, [], rows, tag="work")
    assert plan.ops == []
    assert plan.units == []
    assert (plan.counts.held, plan.counts.cards_removed) == (8, 0)


def test_a_board_that_reads_as_empty_untags_no_tasks():
    _cards, tasks, rows = _paired(8)
    board = board_sync.Board(name="Work tasks.md", dates=True, cards=())
    plan = board_sync.merge(board, tasks, rows, tag="work")
    assert plan.units == []
    assert (plan.counts.held, plan.counts.tasks_untagged) == (8, 0)


def test_a_few_removals_or_a_minority_still_go_through():
    cards, tasks, rows = _paired(8)
    board = board_sync.Board(name="Work tasks.md", dates=True, cards=cards)
    plan = board_sync.merge(board, tasks[3:], rows, tag="work")
    assert (plan.counts.held, plan.counts.cards_removed) == (0, 3)
    # Four of eight is not more than half.
    plan = board_sync.merge(board, tasks[4:], rows, tag="work")
    assert (plan.counts.held, plan.counts.cards_removed) == (0, 4)
    plan = board_sync.merge(board, tasks[5:], rows, tag="work")
    assert (plan.counts.held, plan.counts.cards_removed) == (5, 0)
    # A small board can lose all of its three pairs: that is three removals, not a mass.
    cards, _tasks, rows = _paired(3)
    plan = board_sync.merge(board_sync.Board(name="Work tasks.md", dates=True, cards=cards), [], rows, tag="work")
    assert (plan.counts.held, plan.counts.cards_removed) == (0, 3)


def test_the_guard_holds_only_the_removals_and_a_hand_run_pass_gets_past_it():
    cards, _tasks, rows = _paired(8)
    extra = board_sync.Card(n=8, id=None, text="Brand new", checked=False, date=None)
    board = board_sync.Board(name="Work tasks.md", dates=True, cards=cards + (extra,))
    plan = board_sync.merge(board, [], rows, tag="work")
    assert (plan.counts.held, plan.counts.tasks_created, plan.counts.cards_removed) == (8, 1, 0)
    forced = board_sync.merge(board, [], rows, tag="work", guard=False)
    assert (forced.counts.held, forced.counts.cards_removed) == (0, 8)


# -- subtasks ----------------------------------------------------------------------------

Sub = bs.Sub
SubRow = bs.SubRow


def sub(text: str, id: str | None = None, depth: int = 1, *, checked: bool = False, date: str | None = None) -> Sub:
    return Sub(n=0, id=id, depth=depth, text=text, checked=checked, date=date)


def scard(text: str, id: str, *subs: Sub, checked: bool = False, after=(), extra: bool = False) -> Card:
    return Card(
        n=0, id=id, text=text, checked=checked, date=None,
        subs=tuple(bs.replace(s, n=k) for k, s in enumerate(subs)), after=tuple(after), after_raw_extra=extra,
    )


def sboard(*cards: Card, dates: bool = True, name: str = NAME, known: bool = True) -> Board:
    return Board(name=name, dates=dates, cards=tuple(bs.replace(c, n=i) for i, c in enumerate(cards)), subs_known=known)


def stask(id: str, title: str, parent: str, *, position: float = 0, done: bool = False, due: str | None = None) -> Task:
    return Task(id=id, title=title, done=done, due=due, parent_id=parent, position=position)


def srow(block: str, card_id: str, task_id: str, title: str, *, parent=None, done=False, due=None, status=bs.ROW_OK) -> SubRow:
    return SubRow(block, card_id, task_id, parent, title, done, due, status)


def planner(*tasks: Task) -> dict[str, Task]:
    return {t.id: t for t in tasks}


def smerge(b: Board, p: dict, rows, sub_rows, **kw) -> bs.Plan:
    tops, subs = bs.view(p, TAG)
    return bs.merge(b, tops, list(rows), tag=TAG, subtasks=subs, sub_rows=list(sub_rows), **kw)


def ssettle(b: Board, p: dict, rows, sub_rows, *, passes: int = 1, **kw):
    rows, sub_rows = list(rows), list(sub_rows)
    plan = None
    for _ in range(passes):
        plan = smerge(b, p, rows, sub_rows, **kw)
        b, p, rows, sub_rows = bs.apply_state(b, p, rows, sub_rows, plan)
    return b, p, rows, sub_rows, plan


def assert_sidle(b: Board, p: dict, rows, sub_rows, **kw) -> bs.Plan:
    plan = smerge(b, p, rows, sub_rows, **kw)
    assert plan.ops == [] and plan.creates == [] and plan.calls == []
    assert plan.row_changes == [] and plan.sub_row_changes == []
    return plan


TOP = task("t1", "Поездка")
TOP_ROW = row("card1", "t1", "Поездка")


def test_a_card_subtask_and_a_planner_subtask_of_the_same_title_pair():
    b = sboard(scard("Поездка", "card1", sub("Билеты", checked=True)))
    p = planner(TOP, stask("s1", "билеты", "t1"))
    plan = smerge(b, p, [TOP_ROW], [])
    block = plan.sub_row_changes[0].block_id
    # The planner wins a fresh pair: its title and its open box.
    assert plan.ops == [
        {"op": "sub_tag", "card": "card1", "n": 0, "id": block},
        {"op": "sub_text", "card": "card1", "id": block, "text": "билеты"},
        {"op": "sub_check", "card": "card1", "id": block, "checked": False},
    ]
    assert plan.creates == [] and plan.calls == []
    assert plan.sub_row_changes == [bs.SubRowChange("insert", block, "card1", "s1", None, "билеты", False, None)]
    assert (plan.counts.paired, plan.counts.subs_changed) == (1, 1)
    b, p, rows, subs, _ = ssettle(b, p, [TOP_ROW], [])
    assert_sidle(b, p, rows, subs)


def test_a_subtask_carrying_the_id_minted_for_a_planner_subtask_pairs_with_it():
    minted = bs.mint_for_task(NAME, "s1")
    b = sboard(scard("Поездка", "card1", sub("Другое", minted)))
    plan = smerge(b, planner(TOP, stask("s1", "Билеты", "t1")), [TOP_ROW], [])
    assert plan.sub_row_changes[0].task_id == "s1" and plan.creates == []


def test_a_new_subtask_on_the_card_becomes_a_planner_subtask_of_the_cards_task():
    b = sboard(scard("Поездка", "card1", sub("Билеты", checked=True, date="2026-11-14")))
    plan = smerge(b, planner(TOP), [TOP_ROW], [])
    (create,) = plan.creates
    assert (create.title, create.parent_task_id, create.tag, create.done, create.due) == (
        "Билеты", "t1", None, True, "2026-11-14"
    )
    assert create.request_id == bs.request_id(NAME, create.block_id)
    assert plan.ops == [{"op": "sub_tag", "card": "card1", "n": 0, "id": create.block_id}]
    assert plan.counts.subs_created == 1
    b, p, rows, subs, _ = ssettle(b, planner(TOP), [TOP_ROW], [])
    assert p[f"new-{create.block_id}"].parent_id == "t1"
    assert_sidle(b, p, rows, subs)


def test_a_new_planner_subtask_is_added_under_its_mapped_parent_open_or_done():
    b = sboard(scard("Поездка", "card1", sub("Билеты", "sub1")))
    p = planner(
        TOP,
        stask("s1", "Билеты", "t1"),
        stask("s3", "Сравнить цены", "s1", position=2, done=True),
        stask("s2", "Поезд", "s1", position=1, due="2026-11-17"),
        stask("s4", "Отель", "t1", position=5),
    )
    rows = [TOP_ROW]
    subs = [srow("sub1", "card1", "s1", "Билеты")]
    plan = smerge(b, p, rows, subs)
    adds = [op for op in plan.ops if op["op"] == "sub_add"]
    # Top level first, then under the paired parent, by position.
    assert adds == [
        {"op": "sub_add", "card": "card1", "id": bs.mint_for_task(NAME, "s4"), "text": "Отель", "checked": False},
        {"op": "sub_add", "card": "card1", "id": bs.mint_for_task(NAME, "s2"), "text": "Поезд", "checked": False,
         "parent": "sub1", "date": "2026-11-17"},
        {"op": "sub_add", "card": "card1", "id": bs.mint_for_task(NAME, "s3"), "text": "Сравнить цены", "checked": True,
         "parent": "sub1"},
    ]
    assert plan.counts.subs_added == 3
    b, p, rows, subs, _ = ssettle(b, p, rows, subs)
    assert [(s.text, s.depth) for s in b.cards[0].subs] == [("Билеты", 1), ("Поезд", 2), ("Сравнить цены", 2), ("Отель", 1)]
    assert_sidle(b, p, rows, subs)


def test_a_pair_made_in_this_pass_waits_a_pass_for_its_subtasks():
    b = sboard(scard("Поездка", "card1", sub("Билеты")))
    p = planner(TOP, stask("s2", "Отель", "t1"))
    plan = smerge(b, p, [], [])
    assert plan.counts.paired == 1
    assert plan.sub_row_changes == [] and not any(op["op"].startswith("sub_") for op in plan.ops)
    b, p, rows, subs, _ = ssettle(b, p, [], [], passes=2)
    assert sorted(s.text for s in b.cards[0].subs) == ["Билеты", "Отель"]
    assert len(subs) == 2
    assert_sidle(b, p, rows, subs)


def test_a_subtask_whose_parent_is_not_paired_yet_waits():
    b = sboard(scard("Поездка", "card1", sub("Билеты"), sub("Поезд", depth=2)))
    plan = smerge(b, planner(TOP), [TOP_ROW], [])
    assert [c.title for c in plan.creates] == ["Билеты"]
    assert plan.counts.waiting == 1
    b, p, rows, subs, _ = ssettle(b, planner(TOP), [TOP_ROW], [], passes=2)
    parent = next(t for t in p.values() if t.title == "Билеты")
    child = next(t for t in p.values() if t.title == "Поезд")
    assert child.parent_id == parent.id
    assert {s.block_id: s.parent_block_id for s in subs}[next(s.block_id for s in subs if s.task_id == child.id)] == next(
        s.block_id for s in subs if s.task_id == parent.id
    )
    assert_sidle(b, p, rows, subs)


def test_subtask_fields_merge_three_ways_and_the_planner_wins():
    rows = [TOP_ROW]
    subs = [srow("sub1", "card1", "s1", "Билеты"), srow("sub2", "card1", "s2", "Отель")]
    b = sboard(scard("Поездка", "card1", sub("Билеты на поезд", "sub1", checked=True), sub("Отель у моря", "sub2")))
    p = planner(TOP, stask("s1", "Билеты", "t1", due="2026-11-20"), stask("s2", "Отель в центре", "t1"))
    plan = smerge(b, p, rows, subs)
    assert plan.calls == [bs.Call("title", "s1", "Билеты на поезд"), bs.Call("done", "s1", True)]
    assert {"op": "sub_date", "card": "card1", "id": "sub1", "date": "2026-11-20"} in plan.ops
    assert {"op": "sub_text", "card": "card1", "id": "sub2", "text": "Отель в центре"} in plan.ops
    assert plan.counts.conflicts == 1 and plan.counts.subs_changed == 2
    b, p, rows, subs, _ = ssettle(b, p, rows, subs)
    assert_sidle(b, p, rows, subs)


def test_a_dates_off_board_syncs_no_subtask_date():
    b = sboard(scard("Поездка", "card1", sub("Билеты", "sub1")), dates=False)
    p = planner(TOP, stask("s1", "Билеты", "t1", due="2026-11-20"), stask("s2", "Отель", "t1", due="2026-11-21"))
    plan = smerge(b, p, [TOP_ROW], [srow("sub1", "card1", "s1", "Билеты")])
    assert not any(op["op"] == "sub_date" or "date" in op for op in plan.ops)


def test_a_subtask_gone_from_the_planner_leaves_the_card_with_what_is_under_it():
    b = sboard(scard("Поездка", "card1", sub("Билеты", "sub1"), sub("Поезд", "sub2", 2), sub("Новый", None, 3), sub("Отель", "sub3")))
    subs = [
        srow("sub1", "card1", "s1", "Билеты"),
        srow("sub2", "card1", "s2", "Поезд", parent="sub1"),
        srow("sub3", "card1", "s3", "Отель"),
    ]
    p = planner(TOP, stask("s2", "Поезд", "t1"), stask("s3", "Отель", "t1"))
    plan = smerge(b, p, [TOP_ROW], subs)
    # One op for the subtree: vaultd removes a subtask with its block.
    assert plan.ops == [{"op": "sub_remove", "card": "card1", "id": "sub1"}]
    assert sorted(c.block_id for c in plan.sub_row_changes if c.op == "delete") == ["sub1", "sub2"]
    assert plan.creates == [] and plan.calls == []
    assert plan.counts.subs_removed == 1


def test_a_subtask_gone_from_the_card_is_detached_never_deleted():
    subs = [srow("sub1", "card1", "s1", "Билеты")]
    b = sboard(scard("Поездка", "card1"))
    p = planner(TOP, stask("s1", "Билеты", "t1"))
    plan = smerge(b, p, [TOP_ROW], subs)
    assert plan.ops == [] and plan.calls == [] and plan.creates == []
    assert plan.sub_row_changes == [bs.SubRowChange("update", "sub1", "card1", "s1", None, "Билеты", False, None, bs.ROW_DETACHED)]
    assert plan.counts.subs_detached == 1
    b, p, rows, subs, _ = ssettle(b, p, [TOP_ROW], subs)
    # Never added back, and the planner's edits no longer travel.
    assert_sidle(b, p, rows, subs)
    p["s1"] = bs.replace(p["s1"], title="Билеты 2")
    assert_sidle(b, p, rows, subs)
    # The planner's subtask goes too: the row is dropped.
    del p["s1"]
    plan = smerge(b, p, rows, subs)
    assert plan.sub_row_changes == [bs.SubRowChange("delete", "sub1")] and plan.ops == []


def test_both_gone_drops_the_subtask_row():
    plan = smerge(sboard(scard("Поездка", "card1")), planner(TOP), [TOP_ROW], [srow("sub1", "card1", "s1", "Б")])
    assert plan.sub_row_changes == [bs.SubRowChange("delete", "sub1")]
    assert plan.ops == [] and plan.calls == [] and plan.counts.subs_detached == 0


def test_a_card_that_leaves_takes_its_subtask_rows_and_no_planner_subtask():
    subs = [srow("sub1", "card1", "s1", "Б")]
    p = planner(TOP, stask("s1", "Б", "t1"))
    plan = smerge(sboard(), p, [TOP_ROW], subs)
    assert plan.calls == [bs.Call("untag", "t1", ())]
    assert plan.sub_row_changes == [bs.SubRowChange("delete", "sub1")]
    # And the card stays while its task leaves: the card goes with its subtasks.
    plan = smerge(sboard(scard("Поездка", "card1", sub("Б", "sub1"))), planner(), [TOP_ROW], subs)
    assert plan.ops == [{"op": "remove", "id": "card1"}]
    assert plan.sub_row_changes == [bs.SubRowChange("delete", "sub1")]


def test_a_board_read_without_subtasks_leaves_them_alone():
    subs = [srow("sub1", "card1", "s1", "Б")]
    b = sboard(scard("Поездка", "card1"), known=False)
    p = planner(TOP, stask("s1", "Б", "t1"), stask("s2", "В", "t1"))
    assert_sidle(b, p, [TOP_ROW], subs)


def test_the_guard_counts_subtask_losses_with_the_cards():
    subs = [srow(f"sub{i}", "card1", f"s{i}", f"S{i}") for i in range(5)]
    b = sboard(scard("Поездка", "card1"))
    p = planner(TOP, *(stask(f"s{i}", f"S{i}", "t1") for i in range(5)))
    plan = smerge(b, p, [TOP_ROW], subs)
    assert plan.sub_row_changes == [] and plan.counts.held == 5
    plan = smerge(b, p, [TOP_ROW], subs, guard=False)
    assert plan.counts.subs_detached == 5


def test_subtask_titles_either_side_cannot_take_are_skipped():
    b = sboard(scard("Поездка", "card1", sub("x" * 501)))
    p = planner(TOP, stask("s1", "Сорок %% процентов", "t1"))
    plan = smerge(b, p, [TOP_ROW], [])
    assert plan.ops == [] and plan.creates == [] and plan.counts.skipped == 2


def test_a_planner_subtask_deeper_than_the_board_allows_is_skipped():
    b = sboard(scard("Поездка", "card1", sub("A", "sub1"), sub("B", "sub2", 2), sub("C", "sub3", 3)))
    subs = [srow("sub1", "card1", "s1", "A"), srow("sub2", "card1", "s2", "B", parent="sub1"), srow("sub3", "card1", "s3", "C", parent="sub2")]
    p = planner(TOP, stask("s1", "A", "t1"), stask("s2", "B", "s1"), stask("s3", "C", "s2"), stask("s4", "D", "s3"))
    plan = smerge(b, p, [TOP_ROW], subs)
    assert plan.ops == [] and plan.counts.skipped == 1


def test_sub_tags_go_before_anything_that_moves_subtasks():
    b = sboard(scard("Поездка", "card1", sub("Старое", "sub1"), sub("Новое")))
    p = planner(TOP, stask("s2", "Ещё", "t1"))
    plan = smerge(b, p, [TOP_ROW], [srow("sub1", "card1", "s1", "Старое")])
    assert [op["op"] for op in plan.ops] == ["sub_tag", "sub_remove", "sub_add"]


def test_every_kind_of_subtask_change_converges_then_nothing():
    b = sboard(
        scard("Поездка", "card1", sub("Билеты", "sub1"), sub("Поезд", None, 2), sub("Уже нет", "sub9")),
        scard("Ремонт", "card2", sub("Краска")),
    )
    p = planner(
        TOP,
        task("t2", "Ремонт"),
        stask("s1", "Билеты", "t1", done=True),
        stask("s5", "Отель", "t1"),
        stask("s6", "Кисти", "t2"),
        stask("s7", "Краска", "t2"),
    )
    rows = [TOP_ROW, row("card2", "t2", "Ремонт")]
    subs = [srow("sub1", "card1", "s1", "Билеты"), srow("sub9", "card1", "s9", "Уже нет")]
    b, p, rows, subs, _ = ssettle(b, p, rows, subs, passes=3)
    assert_sidle(b, p, rows, subs)
    assert [(s.text, s.checked) for s in b.cards[0].subs] == [("Билеты", True), ("Поезд", False), ("Отель", False)]
    assert sorted(s.text for s in b.cards[1].subs) == ["Кисти", "Краска"]
    assert sorted(t.title for t in p.values() if t.parent_id == "s1") == ["Поезд"]
    assert sorted(t.title for t in p.values() if t.parent_id == "t2") == ["Кисти", "Краска"]


def test_subtask_creates_share_the_creates_cap():
    b = sboard(scard("Поездка", "card1", *(sub(f"П{i}") for i in range(5))))
    plan = smerge(b, planner(TOP), [TOP_ROW], [], creates_max=2)
    assert len(plan.creates) == 2 and plan.counts.waiting == 3
    b, p, rows, subs, _ = ssettle(b, planner(TOP), [TOP_ROW], [], passes=3, creates_max=2)
    assert_sidle(b, p, rows, subs, creates_max=2)


# -- dependencies --------------------------------------------------------------------------

OTHER = "General tasks.md"


def dside(b: Board, p: dict, rows, sub_rows=(), tag: str = TAG) -> bs.DepsSide:
    tops = tuple(t for t in p.values() if t.parent_id is None and tag in t.tags)
    return bs.DepsSide(b, tops, tuple(rows), tuple(sub_rows))


def deps_plan(sides, p: dict) -> dict[str, bs.Plan]:
    """Each board's dependency units as a plan of their own."""
    out = {}
    for name, dp in bs.merge_deps(sides, list(p.values())).items():
        plan = bs.Plan(board=name)
        bs.add_units(plan, dp)
        out[name] = plan
    return out


def dtask(id: str, title: str, *, blocked=(), tags=(TAG,)) -> Task:
    return Task(id=id, title=title, done=False, tags=tuple(tags), blocked_by=tuple(blocked))


def drow(block: str, task_id: str, title: str, deps=None) -> Row:
    return Row(block, task_id, title, False, None, bs.ROW_OK, None if deps is None else tuple(deps))


def two_cards(after=(), extra=False, deps=None, blocked=()):
    b = sboard(scard("Сначала", "aaaa1"), scard("Потом", "bbbb1", after=after, extra=extra), scard("Третья", "cccc1"))
    p = planner(dtask("t1", "Сначала"), dtask("t2", "Потом", blocked=blocked), dtask("t3", "Третья"))
    rows = [drow("aaaa1", "t1", "Сначала"), drow("bbbb1", "t2", "Потом", deps), drow("cccc1", "t3", "Третья")]
    return b, p, rows


def test_the_first_comparison_writes_the_union_to_both_sides():
    b, p, rows = two_cards(after=[(NAME, "aaaa1")], blocked=["t3"])
    plan = deps_plan([dside(b, p, rows)], p)[NAME]
    assert plan.ops == [
        {"op": "after_set", "id": "bbbb1", "links": [
            {"board": NAME, "id": "aaaa1", "label": "Сначала"},
            {"board": NAME, "id": "cccc1", "label": "Третья"},
        ]}
    ]
    assert plan.calls == [bs.Call("blocked_by", "t2", ("t3", "t1"))]
    # Every compared card gets its snapshot, empty ones too.
    assert plan.row_changes == [
        bs.RowChange("deps", "aaaa1", deps=()),
        bs.RowChange("deps", "bbbb1", deps=("t1", "t3")),
        bs.RowChange("deps", "cccc1", deps=()),
    ]
    assert plan.counts.deps_changed == 1


def test_agreeing_sides_only_record_the_snapshot_then_nothing():
    b, p, rows = two_cards(after=[(NAME, "aaaa1")], blocked=["t1"])
    plan = deps_plan([dside(b, p, rows)], p)[NAME]
    assert plan.ops == [] and plan.calls == []
    assert bs.RowChange("deps", "bbbb1", deps=("t1",)) in plan.row_changes
    b, p, rows, _subs = bs.apply_state(b, p, rows, [], plan)
    assert deps_plan([dside(b, p, rows)], p)[NAME].units == []


def test_a_link_added_on_the_card_reaches_the_planner():
    b, p, rows = two_cards(after=[(NAME, "aaaa1"), (NAME, "cccc1")], blocked=["t1"], deps=["t1"])
    plan = deps_plan([dside(b, p, rows)], p)[NAME]
    assert plan.ops == [] and plan.calls == [bs.Call("blocked_by", "t2", ("t1", "t3"))]


def test_a_blocker_removed_in_the_planner_leaves_the_card():
    b, p, rows = two_cards(after=[(NAME, "aaaa1"), (NAME, "cccc1")], blocked=["t1"], deps=["t1", "t3"])
    plan = deps_plan([dside(b, p, rows)], p)[NAME]
    assert plan.calls == []
    assert plan.ops == [{"op": "after_set", "id": "bbbb1", "links": [{"board": NAME, "id": "aaaa1", "label": "Сначала"}]}]
    # The last one going removes the line.
    b, p, rows = two_cards(after=[(NAME, "aaaa1")], blocked=[], deps=["t1"])
    assert deps_plan([dside(b, p, rows)], p)[NAME].ops == [{"op": "after_set", "id": "bbbb1", "links": []}]


def test_both_sides_changed_the_planner_wins():
    b, p, rows = two_cards(after=[(NAME, "cccc1")], blocked=[], deps=["t1"])
    plan = deps_plan([dside(b, p, rows)], p)[NAME]
    assert plan.ops == [{"op": "after_set", "id": "bbbb1", "links": []}]
    assert plan.calls == [] and plan.counts.conflicts == 1


def test_blockers_no_board_shows_and_links_no_row_resolves_are_kept():
    b, p, rows = two_cards(after=[(NAME, "zzzz9"), (NAME, "aaaa1")], blocked=["elsewhere", "t1"], deps=["t1"])
    p["t2"] = bs.replace(p["t2"], blocked_by=("elsewhere",))
    plan = deps_plan([dside(b, p, rows)], p)[NAME]
    # The planner dropped t1: the card follows, the unknown link stays.
    assert plan.ops == [{"op": "after_set", "id": "bbbb1", "links": [{"board": NAME, "id": "zzzz9", "label": "zzzz9"}]}]
    # The card added t3: "elsewhere" stays on the task.
    b, p, rows = two_cards(after=[(NAME, "aaaa1"), (NAME, "cccc1")], blocked=["elsewhere", "t1"], deps=["t1"])
    plan = deps_plan([dside(b, p, rows)], p)[NAME]
    assert plan.calls == [bs.Call("blocked_by", "t2", ("elsewhere", "t1", "t3"))]


def test_a_dependency_on_a_card_on_another_board_is_linked_there():
    work = sboard(scard("Потом", "bbbb1", after=[(OTHER, "gen01")]))
    general = sboard(scard("Купить краску", "gen01"), scard("Позвонить", "gen02"), name=OTHER)
    p = planner(dtask("t2", "Потом", blocked=["g2"]), dtask("g1", "Купить краску", tags=("general",)), dtask("g2", "Позвонить", tags=("general",)))
    sides = [
        dside(work, p, [drow("bbbb1", "t2", "Потом")]),
        dside(general, p, [drow("gen01", "g1", "Купить краску"), drow("gen02", "g2", "Позвонить")], tag="general"),
    ]
    plans = deps_plan(sides, p)
    assert plans[NAME].ops == [{"op": "after_set", "id": "bbbb1", "links": [
        {"board": OTHER, "id": "gen01", "label": "Купить краску"},
        {"board": OTHER, "id": "gen02", "label": "Позвонить"},
    ]}]
    assert plans[NAME].calls == [bs.Call("blocked_by", "t2", ("g2", "g1"))]
    assert plans[OTHER].units == [] or all(not u.ops and not u.calls for u in plans[OTHER].units)


def test_a_blocker_that_is_a_paired_subtask_resolves_through_its_row():
    b = sboard(scard("Потом", "bbbb1"), scard("Поездка", "card1", sub("Билеты", "sub1")))
    p = planner(dtask("t2", "Потом", blocked=["s1"]), dtask("t1", "Поездка"), stask("s1", "Билеты", "t1"))
    side = dside(b, p, [drow("bbbb1", "t2", "Потом"), drow("card1", "t1", "Поездка")], [srow("sub1", "card1", "s1", "Билеты")])
    plan = deps_plan([side], p)[NAME]
    assert plan.ops == [{"op": "after_set", "id": "bbbb1", "links": [{"board": NAME, "id": "sub1", "label": "Билеты"}]}]


def test_labels_are_screened():
    b, p, rows = two_cards(blocked=["t1"])
    p["t1"] = bs.replace(p["t1"], title="Сначала [важно] | срочно")
    plan = deps_plan([dside(b, p, rows)], p)[NAME]
    assert plan.ops[0]["links"] == [{"board": NAME, "id": "aaaa1", "label": "Сначала важно  срочно"}]
    # A title no label can take: the card's own text instead.
    p["t1"] = bs.replace(p["t1"], title="%% тайное")
    assert deps_plan([dside(b, p, rows)], p)[NAME].ops[0]["links"][0]["label"] == "Сначала"


def test_a_line_with_other_text_on_it_is_not_rewritten_but_the_planner_still_follows():
    b, p, rows = two_cards(after=[(NAME, "aaaa1")], extra=True, blocked=["t3"])
    plan = deps_plan([dside(b, p, rows)], p)[NAME]
    assert plan.ops == []
    assert plan.calls == [bs.Call("blocked_by", "t2", ("t3", "t1"))]
    assert plan.counts.skipped == 1
    # The snapshot is not moved, so the card side is retried every pass.
    assert bs.RowChange("deps", "bbbb1", deps=None) in plan.row_changes


def test_a_board_read_without_subtasks_has_no_dependencies_either():
    b, p, rows = two_cards(blocked=["t1"])
    b = bs.replace(b, subs_known=False)
    assert deps_plan([dside(b, p, rows)], p)[NAME].units == []


def test_dependency_units_take_only_the_room_left_in_the_batch():
    b, p, rows = two_cards(blocked=["t1"])
    plan = bs.Plan(board=NAME, units=[bs.Unit(ops=tuple({"op": "check", "id": "aaaa1", "checked": True} for _ in range(100)))])
    bs.add_units(plan, bs.merge_deps([dside(b, p, rows)], list(p.values()))[NAME])
    assert len(plan.ops) == 100 and plan.counts.waiting == 1


def test_dependencies_settle_then_nothing_across_boards():
    work = sboard(scard("Потом", "bbbb1", after=[(OTHER, "gen01"), (NAME, "aaaa1")]), scard("Сначала", "aaaa1"))
    general = sboard(scard("Купить краску", "gen01", after=[(NAME, "aaaa1")]), name=OTHER)
    p = planner(
        dtask("t1", "Сначала"),
        dtask("t2", "Потом", blocked=["outside"]),
        dtask("g1", "Купить краску", tags=("general",), blocked=["t2"]),
    )
    rows = {NAME: [drow("bbbb1", "t2", "Потом"), drow("aaaa1", "t1", "Сначала")], OTHER: [drow("gen01", "g1", "Купить краску")]}
    boards = {NAME: work, OTHER: general}
    for _ in range(3):
        sides = [dside(boards[NAME], p, rows[NAME]), dside(boards[OTHER], p, rows[OTHER], tag="general")]
        plans = deps_plan(sides, p)
        for name in (NAME, OTHER):
            boards[name], p, rows[name], _subs = bs.apply_state(boards[name], p, rows[name], [], plans[name])
    sides = [dside(boards[NAME], p, rows[NAME]), dside(boards[OTHER], p, rows[OTHER], tag="general")]
    assert all(plan.units == [] for plan in deps_plan(sides, p).values())
    assert set(p["t2"].blocked_by) == {"outside", "g1", "t1"}
    assert set(p["g1"].blocked_by) == {"t2", "t1"}
    assert set(boards[OTHER].cards[0].after) == {(NAME, "bbbb1"), (NAME, "aaaa1")}
