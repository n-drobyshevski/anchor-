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
import inspect
import pathlib

import pytest

from app import log as log_module
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
    # 8e (8e plan sections 7-8): vault notes reach only the persona's
    # turn. Personal note text never reaches a search provider or a research model, in any phase;
    # knowledge note text does not in 8e either.
    "app.vault.notes_personal": "personal vault notes reach only the persona's turn",
    "app.vault.notes_knowledge": "knowledge vault notes reach only the persona's turn in 8e",
}

# Names that would mean the same thing even if the import were indirect.
FORBIDDEN_NAMES = (
    "update_state",
    "cancel_outbound",
    "load_persona",
    "build_messages",
    "persona_active",
    "PERSONA_PATH",
    # 8e: the vault note modules in any import spelling, including
    # `from app.vault import notes_personal`, which the import scan above
    # reads as `app.vault` alone.
    "notes_personal",
    "notes_knowledge",
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


# The modules in this package allowed to log, and why each one is here.
#
# 4b: jobs.py, because it is the module that writes the database and
# can say "job 9 failed with dns_error" without saying anything about
# the page.
# 4d: sweeps.py, for the same reason -- it reports counts of rows it
# expired or blanked, which plan section 12 permits explicitly
# ("IDs, domains, error codes, counts and cost").
#
# Every other module stays silent. The fetcher, the search, the
# distiller and the risk rules all run on data the caller must not
# describe, and none of them has anything to say that is not about
# that data.
LOGGING_ALLOWED = {"jobs.py", "sweeps.py"}


@pytest.mark.parametrize(
    "path", [p for p in _modules() if p.name not in LOGGING_ALLOWED], ids=lambda p: p.name
)
def test_no_other_research_module_logs_anything(path):
    """4a has nothing to say, and 4b gave logging to jobs.py alone. The
    allowlist in app/log.py decides what a jobs.py log line keeps --
    see test_research_jobs_logging_uses_only_the_log_allowlist -- and a
    key like `url` or `topic` is not in it, which is why every other
    module here still has nothing to log at all."""
    code = _code_without_docstrings(path)
    assert "logging.getLogger" not in code, (
        f"{path}: before logging from research, check app/log.py's allowlist -- "
        "plan section 12 permits ids, domains, codes, counts and cost, and "
        "nothing else"
    )


@pytest.mark.parametrize("name", sorted(LOGGING_ALLOWED))
def test_research_logging_uses_only_the_log_allowlist(name):
    """Every `extra={...}` key a logging module uses must be one
    app/log.py's formatter actually keeps (plan section 12).

    Checked against `log.SAFE_EXTRA_KEYS` itself, not against a
    substring search of that module: `_REDACTED_KEYS` also lives there,
    so searching the source for `"text"` would have found it and passed
    a log line carrying page content -- the exact thing this is for.

    Every logger level is scanned, not just `.info`: a key is no safer
    for being logged as a warning.

    A static check on literal dict keys. Every call site in jobs.py
    uses a literal `extra={...}`, and the assertion below fails if that
    stops being true.
    """
    path = RESEARCH / name
    assert path.exists(), f"{name} is in LOGGING_ALLOWED but does not exist"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    keys: set[str] = set()
    call_sites = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "attr", "") not in ("debug", "info", "warning", "error", "critical"):
            continue
        for keyword in node.keywords:
            if keyword.arg != "extra":
                continue
            call_sites += 1
            assert isinstance(keyword.value, ast.Dict), (
                f"a non-literal extra= dict in {name}: this scanner cannot see "
                "through it, so it must not exist"
            )
            for key_node in keyword.value.keys:
                assert isinstance(key_node, ast.Constant) and isinstance(key_node.value, str), (
                    f"a computed log key in {name}: the allowlist cannot be "
                    "checked against it"
                )
                keys.add(key_node.value)

    assert call_sites, f"no logger call with extra= in {name} -- the scanner is broken"
    unknown = keys - set(log_module.SAFE_EXTRA_KEYS)
    assert not unknown, (
        f"app/research/{name} logs keys app/log.py drops: {sorted(unknown)}. "
        "Add them to SAFE_EXTRA_KEYS only if they are an id, a code, a count, "
        "a domain or a cost -- never a path, a topic or any page text."
    )


def test_the_log_allowlist_and_the_redaction_set_do_not_overlap():
    """Guards the guard. If a redacted spelling ever reached
    SAFE_EXTRA_KEYS, the test above would happily approve a log line
    carrying it, and the formatter would print it."""
    assert not (set(log_module.SAFE_EXTRA_KEYS) & log_module._REDACTED_KEYS)


def test_the_allowlist_holds_no_key_that_could_carry_free_text():
    """A key named for content rather than for an identifier is a
    preview waiting to happen. Names, not values -- this cannot see what
    a caller passes, only what the allowlist invites."""
    forbidden_substrings = ("text", "content", "body", "url", "path", "query", "topic",
                            "quote", "title", "message", "payload", "prompt")
    # Named exceptions, each with its reason. Adding one is a decision,
    # recorded in docs/decisions.md, not a way to quiet this test.
    exempt = {
        # The Claude connector dry run (app/web/oauth_probe.py): the path
        # of claude.ai's own public client-metadata document, logged only
        # when the host is claude.ai or claude.com and only in a
        # [A-Za-z0-9._~/-] shape. C2 must pin it; C2 also deletes the
        # probe, and this entry with it.
        "client_id_path",
    }
    offenders = [
        key
        for key in log_module.SAFE_EXTRA_KEYS
        if key not in exempt and any(part in key for part in forbidden_substrings)
    ]
    assert not offenders, f"allowlist invites free text under: {offenders}"


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
