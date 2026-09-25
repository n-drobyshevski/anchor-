"""The closed set of vault codes (phase-8 plan sections 6 and 10).

Same rule as app/research/errors.py: every way a call to vaultd can
fail resolves to one of these short codes, and a code is the only
detail of a failure that may reach a log line or the database. Never
the URL, never a path, never a response body -- vaultd's bodies are
codes too, but the bot does not trust that and does not relay them.

8a has only the client's codes. 8c adds the quarantine reasons
(`vault_file.reason`) to this module, where the schema's
`ck_vault_file_reason_code` expects them: short snake_case, never text.
This is their one source of truth -- app/vault/sync.py names them from
here rather than keeping its own copies, so a code cannot drift between
what is written and what a test or `/vault` label expects.
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


# --- quarantine reasons: vault_file.reason, ck_vault_file_reason_code -----

NAME_TAKEN = "name_taken"
"""8b: a create-only PUT for a fresh fact or journal file got 412 --
practically impossible given the epoch in every Anchor-written name, but
the row still needs a reason to show in /vault."""

BAD_YAML = "bad_yaml"
"""8b (a file vaultd could not even hash meaningfully) and 8c's own
parser: the file's frontmatter did not parse per plan section 4.4 --
no fence, over 4 KB, a non-mapping top level, an alias/anchor, or
duplicate keys."""

BAD_TYPE = "bad_type"
"""8c: an Anchor key parsed with the wrong YAML type (plan section
4.4) -- e.g. an unquoted `fact: no` loading as the boolean False rather
than the two-letter word."""

BAD_KIND = "bad_kind"
"""8c: `kind` is not one of identity, preference, event or rule."""

TECHNIQUE = "technique"
"""8c: the file tried to create a `kind: technique` fact, or convert an
existing one to or from `technique` -- only an adopted study card may
make one."""

TOO_LONG = "too_long"
"""8c: `fact`, after stripping and collapsing whitespace, is over
app/vault/limits.py's FACT_MAX_CHARS."""

EMPTY = "empty"
"""8c: `fact` is empty after stripping and collapsing whitespace."""

UNSAFE = "unsafe"
"""8c: `app.core.redact.is_safe_to_store` refused the text."""

INSTRUCTION = "instruction"
"""8c: the text matches one of app/core/injection.py's instruction ids
(plan section 7.1), the `rule`-kind exemption for role_reassign and
speak_as_assistant aside."""

PIN_CAP = "pin_cap"
"""8c: a `pinned: true` edit would push the pinned count over
MEMORY_PINNED_MAX. render_digest is cleared so the next render puts the
property back."""

DUPLICATE_FILE = "duplicate_file"
"""8c: a second file names an `anchor_id` a tracked, present row already
answers to (plan section 7.1, identity case c)."""

DUPLICATE_FACT = "duplicate_fact"
"""8c: `write_memory` refused the edit as a near-duplicate of another
active fact."""

PROTECTED = "protected"
"""8c: `forget_lineage` refused with FORGET_PROTECTED -- an adopted
technique sits somewhere in the lineage. Nothing changed; the file
stays exactly as it was."""

# The 8b codes plus 8c's quarantine reasons -- one closed set, checked
# against the schema's ck_vault_file_reason_code regex by
# tests/test_vault_errors.py, and against every code app/vault/sync.py
# actually writes.
QUARANTINE_CODES = frozenset(
    {
        NAME_TAKEN,
        BAD_YAML,
        BAD_TYPE,
        BAD_KIND,
        TECHNIQUE,
        TOO_LONG,
        EMPTY,
        UNSAFE,
        INSTRUCTION,
        PIN_CAP,
        DUPLICATE_FILE,
        DUPLICATE_FACT,
        PROTECTED,
    }
)
