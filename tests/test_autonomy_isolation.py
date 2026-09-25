"""Nothing in the new "personality" modules can reach a state writer, the
gate, or the scheduler (phase-5 plan section 12).

Phase 5's whole safety argument is the same shape as phase 4's research
valve (tests/test_research_isolation.py), just aimed the other way:
mood, voice/nicknames, and (from 5b on) the notebook, standing orders,
review and amendments may *read* `user_state` and the transcript, but
none of them may ever change `intensity`, `focus_on`, `due_action`,
`streak`, `persona_active`, an outbound gate decision, or trigger a
scheduled send. That is enforced here exactly as it is there: walk the
AST, name the file and the line, and prove the detector actually
detects something before trusting it to detect nothing.

`MODULES` lists app/core/mood.py, voice.py and persona_context.py --
5a's three. Milestones 5b-5d extend this list with notebook.py,
orders.py, review.py and amendments.py as each ships; nothing about the
detector below needs to change for that, only the list.

5c adds `orders.py` and widens `ALLOWED_USER_STATE_COLUMNS` from a flat
set into a per-module map -- `{module_name: {allowed columns}}` -- since
`voice.py`'s `nickname_last` and `orders.py`'s `awaiting`/`awaiting_ref`
are two different modules' two different narrow writers, and the old
flat set would have let either module write the other's column without
either the AST scanner or a human reader noticing.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

MODULES = [
    pathlib.Path("app/core/mood.py"),
    pathlib.Path("app/core/voice.py"),
    pathlib.Path("app/core/persona_context.py"),
    # 5b.
    pathlib.Path("app/core/notebook.py"),
    pathlib.Path("app/core/screen.py"),
    # 5c.
    pathlib.Path("app/core/orders.py"),
    # 5d.
    pathlib.Path("app/core/review.py"),
    pathlib.Path("app/core/amendments.py"),
    # 5e.
    pathlib.Path("app/core/callbacks.py"),
]

# Reason strings are part of the data so a failure explains itself --
# same convention as tests/test_research_isolation.py's own table.
FORBIDDEN_IMPORTS = {
    "app.core.state": "writes user_state (update_state/set_counters/record_change)",
    "app.core.outbound_gate": "decides whether a proactive message may be sent",
    "app.core.scheduler": "plans proactive sends",
    "app.core.outbound_send": "sends proactive messages",
    "app.core.checkin": "writes the streak and the daily check-in",
    "app.core.pause": "the hard/soft pause-word state machine",
    "app.core.welfare": "the welfare classifier and its persona-off trigger",
    "app.core.quiet": "writes user_state.quiet_until",
}

# Whole-package prefixes, not single modules: anything under app.tg is
# the Telegram layer, and none of these modules has any business
# knowing it exists, transport-wise or otherwise.
FORBIDDEN_PREFIXES = ("app.tg",)

# Per-module allow-list for a targeted `update(UserState).values(...)`
# rather than through app.core.state (see app/core/voice.py's
# remember_nickname docstring for why that split exists). A module with
# no entry here may write no UserState column at all. 5b-5d extend this
# as their own gatherers get their own narrow writers (e.g.
# `callback_scene`).
ALLOWED_USER_STATE_COLUMNS: dict[str, set[str]] = {
    "voice.py": {"nickname_last"},
    # 5c: the only two columns app/core/orders.py may touch (plan's
    # "Writes to `awaiting` from `orders.py`") -- never `streak`,
    # `intensity`, `focus_on`, `due_action` or `persona_active`.
    "orders.py": {"awaiting", "awaiting_ref"},
    # 5e: the only column app/core/callbacks.py's mark_delivered() may
    # touch on UserState -- never `intensity`, `focus_on`, `due_action`,
    # `streak` or `persona_active`.
    "callbacks.py": {"callback_scene"},
}


def _code_without_docstrings(path: pathlib.Path) -> str:
    """Same trick as tests/test_research_isolation.py: strip docstrings
    before scanning for a forbidden name, since prose *about* the rule
    is not a violation of it, and these modules are heavily commented
    about exactly what they must not do."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _imported_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_module_imports_a_state_writer_the_gate_or_the_scheduler(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = _imported_names(tree)
    violations = [
        f"{path}: imports {name} ({FORBIDDEN_IMPORTS[name]})"
        for name in imported
        if name in FORBIDDEN_IMPORTS
    ]
    violations.extend(
        f"{path}: imports {name} (app/tg/ is the Telegram layer)"
        for name in imported
        if any(name == prefix or name.startswith(prefix + ".") for prefix in FORBIDDEN_PREFIXES)
    )
    assert not violations, "\n".join(violations)


def test_the_detector_would_actually_catch_a_violation():
    """Guards the guard. A scanner that matches nothing passes forever."""
    sample = ast.parse(
        "from app.core.state import update_state\n"
        "from app.core.outbound_gate import gate\n"
        "import app.tg.router\n"
    )
    imported = _imported_names(sample)
    assert "app.core.state" in imported
    assert "app.core.outbound_gate" in imported
    assert any(name.startswith("app.tg") for name in imported)


def test_docstring_stripping_does_not_flag_prose_about_the_rule():
    """A module's own docstring is allowed to *name* app.core.state while
    explaining why it must never import it -- the AST import check above
    only looks at real import statements, never at text, but the
    detector's other half (test_research_isolation.py's sibling
    behaviour, mirrored here for parity) relies on the same stripping
    trick, so it gets its own explicit self-test."""
    sample = ast.parse(
        '"""This module must never import app.core.state."""\n'
        "def f():\n"
        "    '''Also never app.core.checkin.'''\n"
        "    return 1\n"
    )
    for node in ast.walk(sample):
        if isinstance(node, (ast.Module, ast.FunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                node.body = body[1:] or [ast.Pass()]
    stripped = ast.unparse(sample)
    assert "app.core.state" not in stripped
    assert "app.core.state" in "import app.core.state"


# --- the ALLOWED_USER_STATE_COLUMNS walk ------------------------------------


def _update_user_state_keyword_names(path: pathlib.Path) -> list[str]:
    """Every keyword name passed to `.values(...)` on an
    `update(UserState)` call in `path`.

    Matched structurally (an `Attribute` call named `values` whose
    receiver chain mentions `update` and `UserState`) rather than by a
    text search, so a keyword spread over several lines or renamed
    imports cannot dodge it.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "values":
            continue
        # Walk the receiver chain looking for `update(UserState)`.
        receiver = node.func.value
        receiver_src = ast.unparse(receiver)
        if "UserState" not in receiver_src:
            continue
        if "update(" not in receiver_src and "sql_update(" not in receiver_src:
            continue
        for keyword in node.keywords:
            if keyword.arg is not None:
                names.append(keyword.arg)
    return names


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_any_user_state_update_only_touches_allowed_columns(path):
    allowed = ALLOWED_USER_STATE_COLUMNS.get(path.name, set())
    names = _update_user_state_keyword_names(path)
    unknown = sorted(set(names) - allowed)
    assert not unknown, (
        f"{path}: update(UserState).values(...) writes column(s) {unknown}, "
        f"outside the allow-list {sorted(allowed)}"
    )


def test_the_column_detector_would_catch_a_synthetic_violation(tmp_path):
    """Guards the guard, using a synthetic module rather than app/core/
    voice.py itself -- this must fail on a column that is *not*
    whitelisted, regardless of what voice.py happens to write today."""
    sample = tmp_path / "offender.py"
    sample.write_text(
        "from sqlalchemy import update as sql_update\n"
        "from app.db.models import UserState\n"
        "def f(session):\n"
        "    return session.execute(\n"
        "        sql_update(UserState).where(UserState.id == 1)\n"
        "        .values(intensity=1, nickname_last='x')\n"
        "    )\n"
    )
    names = _update_user_state_keyword_names(sample)
    assert set(names) == {"intensity", "nickname_last"}
    unknown = set(names) - ALLOWED_USER_STATE_COLUMNS["voice.py"]
    assert unknown == {"intensity"}


def test_voice_module_actually_has_a_user_state_update_for_this_test_to_see():
    """If remember_nickname ever stops using update(UserState).values(...)
    (e.g. moves to ORM attribute assignment), this test -- not a silent
    pass above -- is what should notice."""
    names = _update_user_state_keyword_names(pathlib.Path("app/core/voice.py"))
    assert names, "no update(UserState).values(...) found in voice.py"


def test_orders_module_actually_has_a_user_state_update_for_this_test_to_see():
    """Same self-test as voice.py's, for app/core/orders.py's own
    targeted write of `awaiting`/`awaiting_ref` (`_set_awaiting`)."""
    names = _update_user_state_keyword_names(pathlib.Path("app/core/orders.py"))
    assert set(names) == {"awaiting", "awaiting_ref"}


def test_callbacks_module_actually_has_a_user_state_update_for_this_test_to_see():
    """Same self-test shape as voice.py's/orders.py's, for
    app/core/callbacks.py's own targeted write of `callback_scene`
    (`mark_delivered`)."""
    names = _update_user_state_keyword_names(pathlib.Path("app/core/callbacks.py"))
    assert set(names) == {"callback_scene"}


# --- 5e: the analogous walk for `update(Memory).values(...)` ---------------
#
# The same shape as ALLOWED_USER_STATE_COLUMNS above, aimed at the other
# table callbacks.py is allowed to touch: mark_delivered() bumps
# `Memory.last_used_at` after a callback is delivered, exactly like
# app/core/memory.py's own `mark_used` -- but this module must never
# reach any *other* Memory column (`pinned`, `superseded_by`, `text`,
# `confidence`, ...), which OWN_TABLE_WRITES above cannot express since
# it only sees table names, not columns.
ALLOWED_MEMORY_COLUMNS: dict[str, set[str]] = {
    "callbacks.py": {"last_used_at"},
}


def _update_memory_keyword_names(path: pathlib.Path) -> list[str]:
    """Every keyword name passed to `.values(...)` on an
    `update(Memory)` call in `path` -- same structural match as
    `_update_user_state_keyword_names` above, aimed at `Memory` instead
    of `UserState`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "values":
            continue
        receiver = node.func.value
        receiver_src = ast.unparse(receiver)
        if "Memory" not in receiver_src:
            continue
        if "update(" not in receiver_src and "sql_update(" not in receiver_src:
            continue
        for keyword in node.keywords:
            if keyword.arg is not None:
                names.append(keyword.arg)
    return names


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_any_memory_update_only_touches_allowed_columns(path):
    allowed = ALLOWED_MEMORY_COLUMNS.get(path.name, set())
    names = _update_memory_keyword_names(path)
    unknown = sorted(set(names) - allowed)
    assert not unknown, (
        f"{path}: update(Memory).values(...) writes column(s) {unknown}, "
        f"outside the allow-list {sorted(allowed)}"
    )


def test_the_memory_column_detector_would_catch_a_synthetic_violation(tmp_path):
    """Guards the guard, using a synthetic module -- this must fail on a
    Memory column that is not whitelisted, regardless of what
    callbacks.py happens to write today."""
    sample = tmp_path / "offender.py"
    sample.write_text(
        "from sqlalchemy import update as sql_update\n"
        "from app.db.models import Memory\n"
        "def f(session):\n"
        "    return session.execute(\n"
        "        sql_update(Memory).where(Memory.id == 1)\n"
        "        .values(last_used_at=None, pinned=True)\n"
        "    )\n"
    )
    names = _update_memory_keyword_names(sample)
    assert set(names) == {"last_used_at", "pinned"}
    unknown = set(names) - ALLOWED_MEMORY_COLUMNS["callbacks.py"]
    assert unknown == {"pinned"}


def test_callbacks_module_actually_has_a_memory_update_for_this_test_to_see():
    """If mark_delivered ever stopped using update(Memory).values(...)
    (e.g. moved behind app/core/memory.py's own mark_used), this test --
    not a silent pass above -- is what should notice."""
    names = _update_memory_keyword_names(pathlib.Path("app/core/callbacks.py"))
    assert names, "no update(Memory).values(...) found in callbacks.py"


# --- 5b: each module writes only its own table(s) --------------------------
#
# A narrower, per-module version of the same argument: not just "no
# forbidden import", but "no write to a table this module has no
# business touching at all". `notebook.py` writes `NotebookEntry` and
# the shared cost ledger (`SpendLedger`, exactly like every other H2 job
# body -- app/core/extract.py and app/core/scene.py both do the same);
# `voice.py` writes one `UserState` column, already covered above;
# `mood.py`, `persona_context.py` and `screen.py` write nothing at all.
OWN_TABLE_WRITES: dict[str, set[str]] = {
    "mood.py": set(),
    "voice.py": {"UserState"},
    "persona_context.py": set(),
    "notebook.py": {"NotebookEntry", "SpendLedger"},
    "screen.py": set(),
    # 5c: StandingOrder and CheckinOrderResult are orders.py's own
    # tables; UserState is the same narrow awaiting/awaiting_ref write
    # ALLOWED_USER_STATE_COLUMNS["orders.py"] covers above.
    "orders.py": {"StandingOrder", "CheckinOrderResult", "UserState"},
    # 5d: review.py writes WeeklyReview and ReviewProposal (plus the
    # shared SpendLedger, like every other H2 job body); amendments.py
    # writes PersonaAmendment (plus SpendLedger). Neither writes
    # StandingOrder, NotebookEntry or Message directly -- those go
    # through orders.py/notebook.py's own writers, or (for the persona
    # message itself) app/core/outbound_send.py / app/tg/review.py.
    "review.py": {"WeeklyReview", "ReviewProposal", "SpendLedger"},
    "amendments.py": {"PersonaAmendment", "SpendLedger"},
    # 5e: callbacks.py writes only the UserState.callback_scene column
    # (covered by ALLOWED_USER_STATE_COLUMNS above) and Memory.last_used_at
    # (covered by ALLOWED_MEMORY_COLUMNS below) -- never a new Memory
    # row, never any other table.
    "callbacks.py": {"UserState", "Memory"},
}

# Names a write call might be imported under -- this repo's own
# convention (`sql_update`/`sql_delete` to dodge shadowing `text()` or a
# builtin) plus the plain names and the Postgres dialect's `insert`.
_WRITE_CALL_NAMES = ("insert", "pg_insert", "update", "sql_update", "delete", "sql_delete")


def _write_targets(path: pathlib.Path) -> set[str]:
    """Every model name this module's own code writes to.

    Two shapes only, matching how this codebase actually writes:
    `session.add(Model(...))` / `session.add_all([Model(...), ...])`,
    and `insert(Model)` / `update(Model)` / `delete(Model)` (under any
    of the aliases above) as the first positional argument. A model
    reached any other way (a variable, a helper function) is not
    something this scanner can see -- exactly like
    `_update_user_state_keyword_names` above, this is a structural
    check, not a full write-effect analysis.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    targets: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func

        if isinstance(func, ast.Attribute) and func.attr in ("add", "add_all"):
            constructors = list(node.args[:1])
            if (
                func.attr == "add_all"
                and node.args
                and isinstance(node.args[0], (ast.List, ast.Tuple))
            ):
                constructors = list(node.args[0].elts)
            for value in constructors:
                if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
                    targets.add(value.func.id)

        if (
            isinstance(func, ast.Name)
            and func.id in _WRITE_CALL_NAMES
            and node.args
            and isinstance(node.args[0], ast.Name)
        ):
            targets.add(node.args[0].id)

    return targets


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_each_module_writes_only_its_own_tables(path):
    allowed = OWN_TABLE_WRITES[path.name]
    unknown = _write_targets(path) - allowed
    assert not unknown, (
        f"{path}: writes {sorted(unknown)}, outside its own table(s) {sorted(allowed)}"
    )


def test_the_own_table_write_detector_would_catch_a_violation(tmp_path):
    """Guards the guard, using a synthetic module -- this must fail on a
    table that is not allowed, regardless of what notebook.py happens to
    write today."""
    sample = tmp_path / "offender.py"
    sample.write_text(
        "from sqlalchemy import update as sql_update\n"
        "from app.db.models import Memory, NotebookEntry, UserState\n"
        "def f(session):\n"
        "    session.add(NotebookEntry(kind='observation', text='x', source='anchor'))\n"
        "    session.add(Memory(kind='event', text='x', source='anchor'))\n"
        "    return session.execute(sql_update(UserState).values(intensity=1))\n"
    )
    targets = _write_targets(sample)
    assert targets == {"NotebookEntry", "Memory", "UserState"}
    unknown = targets - {"NotebookEntry"}
    assert unknown == {"Memory", "UserState"}


def test_notebook_module_actually_writes_notebook_entry_for_this_test_to_see():
    """If run_notebook_reflect/add_user_intention ever stopped
    constructing NotebookEntry directly (e.g. moved behind a helper),
    this test -- not a silent pass above -- is what should notice."""
    targets = _write_targets(pathlib.Path("app/core/notebook.py"))
    assert "NotebookEntry" in targets


def test_orders_module_actually_writes_standing_order_for_this_test_to_see():
    """Same self-test shape as notebook.py's, for app/core/orders.py."""
    targets = _write_targets(pathlib.Path("app/core/orders.py"))
    assert {"StandingOrder", "CheckinOrderResult"} <= targets


def test_review_module_actually_writes_weekly_review_for_this_test_to_see():
    """Same self-test shape as notebook.py's, for app/core/review.py."""
    targets = _write_targets(pathlib.Path("app/core/review.py"))
    assert {"WeeklyReview", "ReviewProposal"} <= targets


def test_amendments_module_actually_writes_persona_amendment_for_this_test_to_see():
    """Same self-test shape as notebook.py's, for app/core/amendments.py."""
    targets = _write_targets(pathlib.Path("app/core/amendments.py"))
    assert "PersonaAmendment" in targets


# --- 5d: nothing under app/ or eval/ ever writes to persona/ ---------------
#
# The non-negotiable the whole milestone hinges on: persona.md is never
# written by this codebase. An amendment only ever changes what the live
# prompt carries under "## Поправки"; it never touches the file. This is
# the third structural guard in this file, after the import and
# own-table-write scans above, and it is deliberately a *repo-wide* AST
# walk rather than one more entry in MODULES -- the invariant is not
# "these particular modules don't write persona/", it is "nothing does".


def _writing_open_mode(node: ast.Call) -> ast.expr | None:
    """The `mode` argument of an `open(...)` call, positional or keyword,
    or None if `node` is not a call to `open` at all. `open(path)` alone
    (mode omitted) defaults to `"r"` and is not itself returned as a
    writing call -- callers check the mode string."""
    if not (isinstance(node.func, ast.Name) and node.func.id == "open"):
        return None
    if len(node.args) >= 2:
        return node.args[1]
    for keyword in node.keywords:
        if keyword.arg == "mode":
            return keyword.value
    return None


def _mode_writes(mode_arg: ast.expr | None) -> bool:
    """True for a mode that truncates or appends ("w"/"a"/"x", and any
    combination like "w+"/"rb+"). A non-literal (dynamic) mode fails
    closed -- treated as writing, since this scanner cannot know what
    string it evaluates to."""
    if mode_arg is None:
        return False
    if not (isinstance(mode_arg, ast.Constant) and isinstance(mode_arg.value, str)):
        return True
    return any(letter in mode_arg.value for letter in "wax")


def _mentions_persona(node: ast.expr) -> bool:
    """True iff `persona` (case-insensitively) appears anywhere in
    `node`'s own source -- the heuristic that tells "this call writes
    somewhere under persona/" from "this call writes an eval report or a
    database row", since a pure AST walk cannot evaluate a variable or a
    joined path to its runtime value. Matches a `Path(...) / "persona"`
    join, a literal `"persona/persona.md"`, or a name like
    `PERSONA_PATH` alike -- all three spell the word somewhere in the
    unparsed expression.
    """
    return "persona" in ast.unparse(node).lower()


def _persona_write_violations(path: pathlib.Path) -> list[str]:
    """Every `write_text`/`write_bytes` call whose receiver mentions
    `persona`, and every writing `open(...)` call whose path argument
    does, in `path` -- named with the file and line."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in ("write_text", "write_bytes")
            and _mentions_persona(node.func.value)
        ):
            hits.append(f"{path}:{node.lineno}: .{node.func.attr}(...) on {ast.unparse(node.func.value)}")
            continue
        mode_arg = _writing_open_mode(node)
        if mode_arg is not None and _mode_writes(mode_arg) and node.args and _mentions_persona(node.args[0]):
            hits.append(f"{path}:{node.lineno}: open({ast.unparse(node.args[0])}, ...)")
    return hits


def _all_source_files(*roots: str) -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for root in roots:
        files.extend(sorted(pathlib.Path(root).rglob("*.py")))
    return files


def test_nothing_under_app_or_eval_writes_to_persona_directory():
    """AST scan, repo-wide: no `write_text`, `write_bytes` or a
    truncating/appending `open()` call anywhere under app/ or eval/ that
    targets anything mentioning `persona` -- persona.md is never written
    by this codebase (implementation plan's non-negotiable, restated by
    app/core/amendments.py's and app/core/prompt.py's own docstrings).

    Scoped to calls whose target mentions `persona` (see
    `_mentions_persona`'s docstring), not to every writing call in the
    tree -- eval/run.py legitimately writes its own report file, and a
    scanner that flagged that too would be too broad to trust the day it
    stays green.
    """
    violations: list[str] = []
    for path in _all_source_files("app", "eval"):
        violations.extend(_persona_write_violations(path))
    assert not violations, "\n".join(violations)


def test_the_persona_write_scan_would_catch_a_violation(tmp_path):
    """Guards the guard: a synthetic module that does write persona.md,
    every way this scanner knows to look for one, must be caught."""
    sample = tmp_path / "offender.py"
    sample.write_text(
        "from pathlib import Path\n"
        "PERSONA_PATH = Path('persona/persona.md')\n"
        "def a():\n"
        "    PERSONA_PATH.write_text('x')\n"
        "def b():\n"
        "    PERSONA_PATH.write_bytes(b'x')\n"
        "def c():\n"
        "    with open('persona/persona.md', 'w') as f:\n"
        "        f.write('x')\n"
        "def d():\n"
        "    with open('persona/persona.md', mode='a') as f:\n"
        "        f.write('x')\n"
    )
    hits = _persona_write_violations(sample)
    assert len(hits) == 4


def test_the_persona_write_scan_does_not_flag_a_read_or_an_unrelated_write(tmp_path):
    """A plain read -- the one thing every persona-aware module actually
    does -- must not trip the scanner, or the test above would be
    meaningless noise on every run; neither should a write that has
    nothing to do with persona/ (eval/run.py's own report file)."""
    sample = tmp_path / "clean.py"
    sample.write_text(
        "from pathlib import Path\n"
        "PERSONA_PATH = Path('persona/persona.md')\n"
        "def f():\n"
        "    return PERSONA_PATH.read_text(encoding='utf-8')\n"
        "def g():\n"
        "    with open('persona/persona.md') as fh:\n"
        "        return fh.read()\n"
        "def h():\n"
        "    with open('persona/persona.md', 'r') as fh:\n"
        "        return fh.read()\n"
        "def report(path, report_text):\n"
        "    path.write_text(report_text, encoding='utf-8')\n"
        "def other(path):\n"
        "    with open(path, 'w') as fh:\n"
        "        fh.write('x')\n"
    )
    assert _persona_write_violations(sample) == []
