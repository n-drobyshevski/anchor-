"""The closed set of vault codes (phase-5 plan sections 6 and 10).

Same rule as app/research/errors.py: every way a call to vaultd can
fail resolves to one of these short codes, and a code is the only
detail of a failure that may reach a log line or the database. Never
the URL, never a path, never a response body -- vaultd's bodies are
codes too, but the bot does not trust that and does not relay them.

5a has only the client's codes. 5c adds the quarantine reasons
(`vault_file.reason`) to this module, where the schema's
`ck_vault_file_reason_code` expects them: short snake_case, never text.
"""

from __future__ import annotations

UNAVAILABLE = "unavailable"
"""No answer: connection refused, DNS, timeout, or a 5xx. The next pass
(or the next /vault) simply tries again."""

UNAUTHORIZED = "unauthorized"
"""401. VAULT_API_TOKEN differs between the bot and the vault service."""

NOT_FOUND = "not_found"
"""404. Missing and "not opted in" are deliberately the same answer."""

CONFLICT = "conflict"
"""412. Compare-and-swap lost: the file is not what we last saw."""

REFUSED = "refused"
"""400, 403, 413 or 422: vaultd refused the request itself. A bug on our
side, since the bot never asks for a path outside the rules."""

BAD_RESPONSE = "bad_response"
"""A 2xx whose body is too large, not JSON, or the wrong shape."""

CLIENT_ERROR_CODES = frozenset(
    {UNAVAILABLE, UNAUTHORIZED, NOT_FOUND, CONFLICT, REFUSED, BAD_RESPONSE}
)


class VaultError(Exception):
    """A vault call failed. `code` is always one of CLIENT_ERROR_CODES."""

    def __init__(self, code: str) -> None:
        if code not in CLIENT_ERROR_CODES:
            raise ValueError("unknown vault error code")
        super().__init__(code)
        self.code = code
