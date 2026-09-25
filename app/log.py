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


# Extras that may reach a log line. This tuple is what actually
# enforces the privacy rule -- RedactionFilter above only strips the
# three literal spellings text/content/payload, so a key like
# "memory_text" would sail straight through it. Everything here must be
# an id, a kind, a count, a duration, a domain or a flag. Never a
# preview, never a truncated string, never a URL path or query.
# (2a's scene_id/job_id/kind were missing and were therefore silently
# dropped from every 2a log line.)
#
# Module-level and public since 4b, so a caller's log keys can be
# checked against it by a test rather than by grepping this file --
# tests/test_research_isolation.py does exactly that, and a substring
# search over this module would have matched _REDACTED_KEYS and passed
# a log line carrying `text`.
SAFE_EXTRA_KEYS: tuple[str, ...] = (

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
    # 2a
    "scene_id",
    "job_id",
    "kind",
    # 2b
    "memory_id",
    "superseded_id",
    "pinned",
    # 4b
    "clip_id",
    "domain",
    "error_code",
    "cards",
    "dropped",
    # Grok access (app/web/mcp.py)
    "grant_id",
    # web-chat plan track 2 (design section 5). "source" is
    # 'telegram'/'web', derived from the sign of telegram_update.
    # update_id (negative == web, app/db/queue.py) rather than a stored
    # column, never which text or command a request carried; "route" is
    # a fixed handler name (e.g. "auth_passphrase", "send"), never a URL
    # path or query string, which -- unlike this closed, hand-written
    # set of route names -- could carry a memory id, a card id or a
    # search term. Every web-chat log line uses only these two plus the
    # existing "event" key, with event values fixed to web_login_ok/
    # web_login_fail/web_code_sent/web_lockout/web_logout/web_rejected
    # (app/web/routes.py, app/tg/router.py's /weblogout) -- never the
    # passphrase, the code, a session token, or an IP address.
    "source",
    "route",
    # 5b: notebook reflect/expiry counts only, never text (app/core/
    # notebook.py).
    "added",
    "closed",
    "updated",
    # 5c: standing order ids only, never order text (app/core/orders.py,
    # app/tg/orders.py).
    "order_id",
    # 5d: weekly review / amendment ids and pass/fail counts only, never
    # review, proposal or amendment text or model output (app/core/
    # review.py, app/core/amendments.py).
    "review_id",
    "proposal_id",
    "amendment_id",
    "passed",
    "reason",
    # 6e: backup/retention housekeeping (app/ops/backup.py,
    # app/core/retention.py) -- a ciphertext object's size, never its
    # key or contents.
    "bytes",
)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Only include known-safe extras; never dump __dict__ wholesale,
        # since that could reintroduce a redacted key under a new
        # spelling. See SAFE_EXTRA_KEYS.
        for key in SAFE_EXTRA_KEYS:
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
