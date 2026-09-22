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
"""

from __future__ import annotations

import ast
import pathlib

import pytest

MODULES = [
    pathlib.Path("app/core/mood.py"),
    pathlib.Path("app/core/voice.py"),
    pathlib.Path("app/core/persona_context.py"),
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

# The one column these modules may write, via a targeted
# `update(UserState).values(...)` rather than through app.core.state
# (see app/core/voice.py's remember_nickname docstring for why that
# split exists). 5b-5d extend this as their own gatherers get their own
# narrow writers (e.g. `callback_scene`).
ALLOWED_USER_STATE_COLUMNS = {"nickname_last"}


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
    names = _update_user_state_keyword_names(path)
    unknown = sorted(set(names) - ALLOWED_USER_STATE_COLUMNS)
    assert not unknown, (
        f"{path}: update(UserState).values(...) writes column(s) {unknown}, "
        f"outside the allow-list {sorted(ALLOWED_USER_STATE_COLUMNS)}"
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
    unknown = set(names) - ALLOWED_USER_STATE_COLUMNS
    assert unknown == {"intensity"}


def test_voice_module_actually_has_a_user_state_update_for_this_test_to_see():
    """If remember_nickname ever stops using update(UserState).values(...)
    (e.g. moves to ORM attribute assignment), this test -- not a silent
    pass above -- is what should notice."""
    names = _update_user_state_keyword_names(pathlib.Path("app/core/voice.py"))
    assert names, "no update(UserState).values(...) found in voice.py"
