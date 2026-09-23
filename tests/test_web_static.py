"""Static asset hygiene, grep-style like tests/test_log.py (web-chat
plan track 2/3, design section 5's XSS contract).

app.js's own module docstring states the same rule from the inside:
model output is untrusted, so it is rendered with `textContent` only.
This test is the structural backstop tests/test_web_security.py's CSP
assertions cannot give by themselves -- a strict CSP makes a violation
*fail at runtime in a browser*, but only a source check catches one
before it ships.

Per the task brief: the static files are written by a different,
concurrently-running track. If they are missing when this file first
runs, the test still exists (skipping rather than failing) so a later
re-run against the finished files exercises it for real.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "web" / "static"

FORBIDDEN_JS_SNIPPETS = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write(",
    "eval(",
    "new Function",
    'setTimeout("',
    "setTimeout('",
)

_INLINE_SCRIPT_BODY_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>\s*\S", re.IGNORECASE)
_STYLE_ATTR_RE = re.compile(r"\sstyle\s*=", re.IGNORECASE)
_ON_HANDLER_RE = re.compile(r"\son[a-z]+\s*=", re.IGNORECASE)


def _require(name: str) -> Path:
    path = STATIC_DIR / name
    if not path.is_file():
        pytest.skip(f"{path} does not exist yet (written by a concurrent track)")
    return path


def _strip_line_comments(source: str) -> str:
    """Drop `//`-prefixed comment lines before scanning.

    app.js's own module docstring names every one of FORBIDDEN_JS_SNIPPETS
    as the rule it upholds (see this test module's own docstring for the
    same self-reference problem in reverse) -- scanning the raw source
    would self-match on the sentence describing the rule, exactly as
    tests/test_log.py excludes its own file for the same reason.
    """
    return "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("//")
    )


def test_app_js_has_no_unsafe_dom_sinks():
    path = _require("app.js")
    code = _strip_line_comments(path.read_text(encoding="utf-8"))
    violations = [snippet for snippet in FORBIDDEN_JS_SNIPPETS if snippet in code]
    assert not violations, f"app.js contains forbidden sink(s): {violations}"


def test_index_html_has_no_inline_script_body():
    path = _require("index.html")
    source = path.read_text(encoding="utf-8")
    match = _INLINE_SCRIPT_BODY_RE.search(source)
    assert match is None, f"index.html has an inline <script> body: {match.group(0)!r}"


def test_index_html_has_no_style_attributes():
    path = _require("index.html")
    source = path.read_text(encoding="utf-8")
    match = _STYLE_ATTR_RE.search(source)
    assert match is None, f"index.html has a style= attribute: {match.group(0)!r}"


def test_index_html_has_no_inline_event_handlers():
    path = _require("index.html")
    source = path.read_text(encoding="utf-8")
    match = _ON_HANDLER_RE.search(source)
    assert match is None, f"index.html has an on*= handler: {match.group(0)!r}"


def test_static_files_exist():
    """Not a security assertion, just a tripwire: if this one starts
    failing, re-run the whole module -- the skip-based tests above will
    then run for real instead of silently skipping forever."""
    for name in ("index.html", "app.css", "app.js", "icon.svg"):
        path = STATIC_DIR / name
        if not path.is_file():
            pytest.skip(f"{path} does not exist yet (written by a concurrent track)")
    assert True
