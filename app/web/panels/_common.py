"""Small validators the newer panels share (notebook, orders, debts).

The same two rules app/web/panels/memory.py and checkin.py each keep
their own copy of: free text must be a string with no control
characters (a stray paste) and no lone UTF-16 surrogate (asyncpg's
UTF-8 encoding fails on one at the query boundary -- an unhandled 500
whose traceback logged the text as a SQL parameter repr, W3 finding);
a path id must be a positive bigint, anything else is a 404 before any
query runs.
"""

from __future__ import annotations

import re

from aiohttp import web

_DISALLOWED_CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f]")
_SURROGATE_RE = re.compile("[\ud800-\udfff]")
_MAX_BIGINT = 2**63 - 1


def valid_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and not _DISALLOWED_CONTROL_RE.search(value)
        and not _SURROGATE_RE.search(value)
    )


def path_id(request: web.Request) -> int | None:
    try:
        value = int(request.match_info["id"])
    except ValueError:
        return None
    return value if 0 < value <= _MAX_BIGINT else None
