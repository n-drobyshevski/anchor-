"""Structured logging with content redaction.

Invariant (non-negotiable): no message text, prompt, completion, or raw
update payload is ever logged. Only IDs, counts, and latency go to logs.

A logging.Filter cannot scrub record.exc_info — a rendered traceback can
carry a Telegram Message object (and therefore its .text) into the log.
So the rule is enforced at the source instead: nothing in this codebase
renders a traceback or attaches exception info to a log record. The
worker logs only type(exc).__name__ and update_id on failure.
tests/test_log.py greps the source tree for violations of this rule
(see that file for the exact forbidden spellings).
"""

from __future__ import annotations

import json
import logging
import sys

# Keys that must never reach a log record, whether they arrive as extras
# (record.__dict__) or as a dict-shaped record.args.
_REDACTED_KEYS = {"text", "content", "payload"}


class RedactionFilter(logging.Filter):
    """Drops redacted keys from log record extras before formatting.

    Attached at the handler level so it runs before format(), for every
    record that reaches that handler.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        for key in _REDACTED_KEYS:
            if hasattr(record, key):
                delattr(record, key)
        if isinstance(record.args, dict):
            record.args = {k: v for k, v in record.args.items() if k not in _REDACTED_KEYS}
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Only include known-safe extras; never dump __dict__ wholesale,
        # since that could reintroduce a redacted key under a new spelling.
        for key in (
            "update_id",
            "chat_id",
            "latency_ms",
            "attempts",
            "event",
            "count",
            "tokens_in",
            "tokens_cached",
            "tokens_out",
            "usd_cost",
        ):
            if hasattr(record, key):
                base[key] = getattr(record, key)
        return json.dumps(base, default=str)


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging with a JSON formatter and the redaction filter."""
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(_JsonFormatter())
    handler.addFilter(RedactionFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
