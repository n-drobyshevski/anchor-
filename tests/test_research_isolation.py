"""Nothing in app/research/ can reach the persona or the state (plan 12, 14).

Phase 4's whole safety argument is a one-way valve: text from the open
web goes into the research package, and the only thing that ever comes
back out is a card the user explicitly adopted. Every rule below is one
wall of that valve, and every one of them is the kind of rule that a
docstring cannot keep -- the next person to need a timestamp, a state
flag or a quick log line writes the obvious thing, it works, the tests
pass, and the valve is gone.

So they are tests, in the shape this repo already uses for
"the extractor cannot write sensitive state" (tests/test_extract.py)
and "nothing in app/core/ reads the wall clock"
(tests/test_core_clock_discipline.py): walk the AST, name the file and
the line.

The plugin-call-site rule lives in tests/test_web_search_isolation.py
with the rest of its family, not here.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from app.research import errors, fetch

RESEARCH = pathlib.Path("app/research")


def _modules() -> list[pathlib.Path]:
    modules = sorted(RESEARCH.rglob("*.py"))
    assert modules, "app/research/ is empty -- this test would pass vacuously"
    return modules


def _code_without_docstrings(path: pathlib.Path) -> str:
    """The module's source with every docstring removed.

    Prose *about* a forbidden name is not a use of it, and this package
    is heavily commented about exactly the things it must not do. Same
    trick as tests/test_extract.py.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


# Modules whose import from app/research/ would mean the valve leaks.
# The reason is part of the data so a failure explains itself.
FORBIDDEN_IMPORTS = {
    "app.core.state": "writes user_state; research writes only study_* and memory",
    "app.core.prompt": "loads persona.md and builds the persona prompt",
    "app.core.outbound": "plans proactive messages",
    "app.core.outbound_gate": "decides whether a proactive message may be sent",
    "app.core.outbound_send": "sends proactive messages",
    "app.core.turn": "the persona turn, with state and transcript in scope",
    "app.core.tick": "the proactive tick",
    "app.core.scheduler": "plans proactive sends",
    "app.startup": "loads persona.md at boot",
}

# Names that would mean the same thing even if the import were indirect.
FORBIDDEN_NAMES = (
    "update_state",
    "cancel_outbound",
    "load_persona",
    "build_messages",
    "persona_active",
    "PERSONA_PATH",
)


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_no_research_module_imports_a_state_writer_or_the_persona(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    violations = [
        f"{path}: imports {name} ({FORBIDDEN_IMPORTS[name]})"
        for name in imported
        if name in FORBIDDEN_IMPORTS
    ]
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_no_research_module_names_a_state_writer_or_the_persona(path):
    code = _code_without_docstrings(path)
    violations = [f"{path}: mentions {name}" for name in FORBIDDEN_NAMES if name in code]
    assert not violations, "\n".join(violations)


def test_the_detector_would_actually_catch_a_violation():
    """Guards the guard. A scanner that matches nothing passes forever."""
    sample = ast.parse(
        '"""A docstring that says update_state and load_persona."""\n'
        "def go():\n"
        "    '''Also mentions cancel_outbound.'''\n"
        "    return 1\n"
    )
    for node in ast.walk(sample):
        if isinstance(node, (ast.Module, ast.FunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                node.body = body[1:] or [ast.Pass()]
    stripped = ast.unparse(sample)
    assert "update_state" not in stripped, "docstring stripping is what makes this usable"
    assert "update_state" in "x = update_state(session)", "and a real use still matches"


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_no_research_module_fetches_through_trafilatura(path):
    """trafilatura ships its own HTTP client. We use it as a string
    transform and nothing else: every byte from the web comes through
    app/research/fetch.py, where the address checks are."""
    code = _code_without_docstrings(path)
    for name in ("fetch_url", "fetch_response", "trafilatura.downloads", "sitemaps", "feeds"):
        assert name not in code, f"{path}: trafilatura.{name} bypasses our own fetcher"


def test_every_fetch_failure_uses_a_declared_code():
    """The closed set in errors.py is what keeps free text from a
    stranger's server out of the database and the logs (plan 12)."""
    code = _code_without_docstrings(pathlib.Path("app/research/fetch.py"))
    tree = ast.parse(code)
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "FetchFailure":
            for keyword in node.keywords:
                if keyword.arg != "error":
                    continue
                value = keyword.value
                if isinstance(value, ast.Attribute) and getattr(value.value, "id", "") == "errors":
                    used.add(value.attr)
                elif isinstance(value, ast.Constant):
                    pytest.fail(f"FetchFailure built from a literal: {value.value!r}")
                elif isinstance(value, ast.Name):
                    # A variable, which is always one of ours by the time
                    # it gets here -- the codes come from parse_target,
                    # vet_addresses or _read_capped, all of which return
                    # members of the closed set.
                    continue
    assert used, "no FetchFailure sites found -- the scanner is broken"
    unknown = {name for name in used if getattr(errors, name, None) not in errors.FETCH_ERROR_CODES}
    assert not unknown, f"codes not in FETCH_ERROR_CODES: {sorted(unknown)}"


def test_every_error_constant_is_in_the_closed_set():
    declared = {
        value
        for name, value in vars(errors).items()
        if name.isupper() and isinstance(value, str) and not name.startswith("_")
    }
    assert declared == set(errors.FETCH_ERROR_CODES)


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_no_research_module_logs_anything_yet(path):
    """4a has nothing to say. When 4b starts logging, the allowlist in
    app/log.py decides what survives -- and a key like `url` or `topic`
    is not in it, so this test is the reminder to think before adding
    one rather than a permanent ban on logging."""
    code = _code_without_docstrings(path)
    assert "logging.getLogger" not in code, (
        f"{path}: before logging from research, check app/log.py's allowlist -- "
        "plan section 12 permits ids, domains, codes, counts and cost, and "
        "nothing else"
    )


def test_the_fetcher_takes_its_limits_as_arguments_not_from_settings():
    """app/research/fetch.py never reads Settings.

    Not style: it is what lets every limit be exercised at a size a test
    can build (50 KB instead of 2 MB, three redirects instead of a real
    chain) without an environment, and what keeps the module importable
    in isolation.
    """
    code = _code_without_docstrings(pathlib.Path("app/research/fetch.py"))
    assert "Settings" not in code
    assert "get_settings" not in code
