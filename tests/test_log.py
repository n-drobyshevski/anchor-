"""Logging privacy tests (plan section 14 / hard requirement 4).

- the redaction filter strips text/content/payload from log records
- no source file anywhere uses exc_info=True, logger.exception(, or
  traceback.format_exc( — a logging.Filter cannot scrub record.exc_info,
  so this is enforced by rule, checked here by grepping the tree.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.log import RedactionFilter

REPO_ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_SNIPPETS = ("exc_info=True", "logger.exception(", "traceback.format_exc(")
SKIP_DIR_NAMES = {".git", ".venv", "__pycache__", ".pytest_cache", "uv.lock"}


def test_redaction_filter_strips_text_content_payload():
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1, msg="msg", args=None, exc_info=None
    )
    record.text = "message text that must never be logged"
    record.content = "completion content"
    record.payload = {"update_id": 1, "message": {"text": "secret"}}
    record.update_id = 42
    record.latency_ms = 12

    result = RedactionFilter().filter(record)

    assert result is True
    assert not hasattr(record, "text")
    assert not hasattr(record, "content")
    assert not hasattr(record, "payload")
    # Safe extras survive.
    assert record.update_id == 42
    assert record.latency_ms == 12


def test_redaction_filter_strips_dict_args():
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="msg %(text)s",
        args={"text": "should be dropped", "update_id": 7},
        exc_info=None,
    )
    RedactionFilter().filter(record)
    assert "text" not in record.args
    assert record.args["update_id"] == 7


def _iter_source_files():
    for path in REPO_ROOT.rglob("*.py"):
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        if path.resolve() == Path(__file__).resolve():
            # This file legitimately mentions the forbidden snippets as
            # data (the tuple above), which would otherwise self-match.
            continue
        yield path


def test_no_exc_info_or_traceback_usage_anywhere():
    violations = []
    for path in _iter_source_files():
        source = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN_SNIPPETS:
            if needle in source:
                violations.append(f"{path.relative_to(REPO_ROOT)}: contains {needle!r}")

    assert not violations, "Forbidden logging patterns found:\n" + "\n".join(violations)
