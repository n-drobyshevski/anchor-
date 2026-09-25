"""Logging for vaultd: JSON lines, and an allowlist of keys.

Plan section 10: vaultd's logs may carry methods, route templates,
statuses, latencies, exit codes and counts. **Never** a path, a file
name, a query string, a property value or note text -- a note's title
*is* content. The allowlist makes that structural: an `extra=` key that
is not listed here is dropped on the floor, so a future
`extra={"path": ...}` produces nothing rather than a leak.

Messages are literal strings at every call site; nothing formats a
value into one. `ob`'s own output never reaches a logger at all (its
stdout and stderr go to /dev/null, see supervisor.py).
"""

from __future__ import annotations

import json
import logging
import sys

SAFE_EXTRA_KEYS = frozenset(
    {
        "method",
        "route",
        "status",
        "latency_ms",
        "event",
        "exit_code",
        "restarts",
        "count",
        "backoff_s",
    }
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "level": record.levelname,
            "logger": record.name,
            "msg": record.msg if isinstance(record.msg, str) else "",
        }
        for key in SAFE_EXTRA_KEYS:
            if key in record.__dict__:
                out[key] = record.__dict__[key]
        return json.dumps(out, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # aiohttp's access log prints the request line, query string
    # included. It is also disabled at the runner; this is the belt.
    logging.getLogger("aiohttp.access").disabled = True
