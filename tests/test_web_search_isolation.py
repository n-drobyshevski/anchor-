"""No call site may ask the provider for a web search by accident.

`/search` reached the tree with milestone 1f without a plan behind it, and
it defaulted *on* -- it was the one path in this bot that sent the user's
words to a third party (Exa, via OpenRouter's `web` plugin). Milestone 4a
removed the command, the setting and the plumbing entirely.

That removal does not retire this file: the property that matters was
never "the current five call sites pass False", it is that **no call
site can pass a truthy `web_search=` at all**, now or after a future
milestone adds one back deliberately (milestone 4c's research search
module). So this file keeps guarding it structurally:

- **`_web_search_true_sites`**: an AST walk over all of `app/` finds
  every call that passes a truthy `web_search=`, and asserts the list is
  empty. A new module that reintroduces the parameter without updating
  `ALLOWED_SITES` fails here immediately.
- **`_web_search_defaults`**: asserts that any function which still
  declares a `web_search` parameter defaults it to `False`. Vacuously
  true today (no function declares it), but it is cheap insurance for
  whatever declares it next.

The wire itself -- that an ordinary call sends no `plugins`, no `tools`,
no `tool_choice` and no `functions` -- is pinned in
tests/test_openrouter.py. This file covers everything above that seam.

Modelled on tests/test_core_clock_discipline.py, the repo's convention for
a rule that must outlive the people who read the plan.
"""

from __future__ import annotations

import ast
import pathlib

APP = pathlib.Path("app")

# No call site is sanctioned today. This tuple gains exactly one entry,
# ("app/research/search.py", "<its function name>"), in milestone 4c --
# and must never gain another.
ALLOWED_SITES: tuple[tuple[str, str], ...] = ()

# Request-shaping keys that would hand the model a capability. They may be
# built in exactly one module -- the provider that owns the wire format.
TOOL_KEYS = ("plugins", "tools", "tool_choice", "functions")
TOOL_KEY_OWNER = pathlib.Path("app/llm/openrouter.py")


# --- structural: who passes web_search=True -----------------------------


def _enclosing_function(tree: ast.AST, node: ast.AST) -> str:
    """Name of the innermost def containing `node`, or "<module>"."""
    best = "<module>"
    best_start = -1
    for candidate in ast.walk(tree):
        if not isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = getattr(candidate, "end_lineno", candidate.lineno)
        if candidate.lineno <= node.lineno <= end and candidate.lineno > best_start:
            best, best_start = candidate.name, candidate.lineno
    return best


def _web_search_true_sites(path: pathlib.Path) -> list[tuple[str, str, int]]:
    """Every call in `path` that could originate a `web_search=True`.

    Returns (file, enclosing function, line). One shape is not an origin
    and is skipped: an explicit `web_search=False`, which is the thing we
    want. Anything else -- a different variable, an expression, a truthy
    literal -- is reported, because it cannot be proved False from here.
    """
    tree = ast.parse(path.read_text())
    found: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "web_search":
                continue
            value = keyword.value
            if isinstance(value, ast.Constant) and not value.value:
                continue
            found.append((str(path), _enclosing_function(tree, node), node.lineno))
    return found


def _web_search_defaults(path: pathlib.Path) -> list[tuple[str, str, int]]:
    """Every function in `path` whose `web_search` parameter is not False.

    A missing default counts as a violation too: a required parameter
    cannot be forgotten at a call site, but it also cannot be audited by
    the walk above, so the rule is simply "declare it False".
    """
    tree = ast.parse(path.read_text())
    found: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args
        pairs = list(zip(args.args[len(args.args) - len(args.defaults):], args.defaults))
        pairs += [(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None]
        declared = {a.arg for a in args.args + args.kwonlyargs}
        defaulted = {a.arg for a, _ in pairs}
        if "web_search" in declared and "web_search" not in defaulted:
            found.append((str(path), node.name, node.lineno))
            continue
        for arg, default in pairs:
            if arg.arg != "web_search":
                continue
            if not (isinstance(default, ast.Constant) and default.value is False):
                found.append((str(path), node.name, node.lineno))
    return found


def _app_modules() -> list[pathlib.Path]:
    return sorted(APP.rglob("*.py"))


def test_there_are_app_modules_to_check():
    """Guards the guard: a glob matching nothing makes the rest vacuous."""
    modules = _app_modules()
    assert len(modules) > 15
    names = {str(p) for p in modules}
    assert "app/tg/router.py" in names
    assert "app/core/turn.py" in names
    assert "app/core/outbound_send.py" in names
    assert "app/worker.py" in names


def test_no_call_site_asks_for_a_web_search():
    sites = [site for path in _app_modules() for site in _web_search_true_sites(path)]
    assert [(f, fn) for f, fn, _ in sites] == list(ALLOWED_SITES), (
        "web_search=True has no sanctioned call site today (4a). Found:\n  "
        + "\n  ".join(f"{f}:{line} in {fn}()" for f, fn, line in sites)
    )


def test_every_web_search_parameter_defaults_to_false():
    """Vacuously true today -- no function declares `web_search` at all --
    but it is cheap insurance for whatever milestone 4c adds back."""
    offenders = [site for path in _app_modules() for site in _web_search_defaults(path)]
    assert offenders == [], (
        "a web_search parameter must default to False:\n  "
        + "\n  ".join(f"{f}:{line} in {fn}()" for f, fn, line in offenders)
    )


def test_the_default_detector_actually_detects(tmp_path):
    sample = tmp_path / "defaults.py"
    sample.write_text(
        "def good(a, *, web_search: bool = False): ...\n"
        "def bad_true(a, *, web_search: bool = True): ...\n"
        "def bad_required(a, *, web_search: bool): ...\n"
        "def unrelated(a, *, other=True): ...\n"
    )
    assert [fn for _, fn, _ in _web_search_defaults(sample)] == ["bad_true", "bad_required"]


def test_the_detector_actually_detects(tmp_path):
    """A guard that cannot fail is not a guard."""
    sample = tmp_path / "offender.py"
    sample.write_text(
        "async def innocent():\n"
        "    await p.complete(m, conversation_id='a')\n"
        "async def explicit_false():\n"
        "    await p.complete(m, conversation_id='a', web_search=False)\n"
        "async def offender():\n"
        "    await p.complete(m, conversation_id='a', web_search=True)\n"
        "async def sneaky(flag):\n"
        "    await p.complete(m, conversation_id='a', web_search=flag)\n"
    )
    found = _web_search_true_sites(sample)
    functions = [fn for _, fn, _ in found]
    assert functions == ["offender", "sneaky"], found


def test_no_module_outside_the_provider_builds_a_tool_request():
    """`plugins`/`tools` are wire-format concerns. If they appear in
    app/core/ or app/tg/, some caller is shaping a request behind the
    provider's back."""
    offenders: list[str] = []
    for path in _app_modules():
        if path == TOOL_KEY_OWNER:
            continue
        source = path.read_text()
        for key in TOOL_KEYS:
            if f'"{key}"' in source or f"'{key}'" in source:
                offenders.append(f"{path}: {key}")
    assert offenders == [], (
        f"only {TOOL_KEY_OWNER} may build a tool/plugin request:\n  "
        + "\n  ".join(offenders)
    )
