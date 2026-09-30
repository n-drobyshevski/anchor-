"""The checks every note the bot asks vaultd to create passes first.

Two writers put new notes into the user's vault through vaultd: Claude's
knowledge tools (app/web/claude_write.py, W2b; anchor-claude-write-plan.md
sections 3 and 6.5) and, from lens L4, Echo's inbox writer
(app/core/echo_write.py; anchor-lens-plan.md sections 9 and 14.5, the
L4 spec section 5). Both need the same two things before a byte leaves:

- **`sanitize_title`**: a title made into a safe file name -- no `/`,
  `\\` or control character, no leading `.`, at most MAX_TITLE_CHARS,
  `.md` appended. vaultd checks the name again; this keeps the obvious
  refusals on the bot's side.
- **`check_content`**: the text refused if it carries an instruction
  (the injection rules in REFUSE_INJECTION_IDS) or a secret (the
  redactor's and the vault's own patterns). A knowledge note may hold
  links, handles and code, so those rules are left out.

Moved here from claude_write.py (the L4 spec section 5) so the two
writers share one copy; `Refused` is the same class claude_write.py
re-exports, and its `code` is for a log line only. Pure: no database,
no settings, no logging.
"""

from __future__ import annotations

import re

from app.core import redact
from app.research import injection
from app.vault import secrets as vault_secrets

# plan section 6.5: the instruction ids that refuse a write. `url`,
# `handle` and `code_fence` are deliberately excluded -- a knowledge
# note legitimately holds links and code -- and there is no rule
# exemption for the rest, unlike app/vault's own memory-write path.
# Spelled out literally, not derived from injection.RULE_IDS, so a
# future addition to that list does not silently start refusing
# knowledge writes.
REFUSE_INJECTION_IDS = frozenset(
    {
        "override_previous",
        "override_previous_en",
        "override_previous_fr",
        "system_prompt",
        "developer_mode",
        "role_tag",
        "exfiltrate",
        "role_reassign",
        "speak_as_assistant",
    }
)

_BAD_TITLE_CHARS = re.compile(r"[/\\\x00-\x1f]")
MAX_TITLE_CHARS = 120


class Refused(Exception):
    """Every refusal on the write/undo tool surface. `code` is a reason
    for the log line only -- never shown to Claude, never a path."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def sanitize_title(title: str) -> str:
    """A safe file name: no `/`, no leading `.`, at most
    MAX_TITLE_CHARS, `.md` appended. Raises `Refused("bad_title")` for
    a title that sanitises to nothing."""
    cleaned = _BAD_TITLE_CHARS.sub("_", title.strip())
    while cleaned.startswith("."):
        cleaned = cleaned[1:]
    cleaned = cleaned.strip()[:MAX_TITLE_CHARS].strip()
    if not cleaned:
        raise Refused("bad_title")
    return cleaned + ".md"


def check_content(new_body: str) -> None:
    """Raise `Refused("instruction")` or `Refused("secret")` (module docstring)."""
    if set(injection.hits(new_body)) & REFUSE_INJECTION_IDS:
        raise Refused("instruction")
    if redact.secret_spans(new_body) or vault_secrets.spans(new_body):
        raise Refused("secret")
