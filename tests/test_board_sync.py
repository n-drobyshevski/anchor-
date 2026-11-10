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
