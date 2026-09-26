"""Token-shape secret patterns for the vault's note masking (milestone 8d).

Distinct from `app/core/redact.py`, which the extractor uses to refuse
*storing* a card/IBAN/email in a memory. This module finds the shapes a
credential vendor stamps onto its own tokens -- an AWS access key id, a
GitHub token, an `sk-` API key, a Slack token, a JWT, a PEM private-key
block -- which can turn up verbatim in a note you pasted a snippet
into, and which no length or digit check can tell apart from ordinary
text the way `redact.py`'s patterns can. Each pattern is deliberately
narrow (a fixed prefix plus a length long enough that no English or
Russian word collides with it), so that "desk-top" or "skeleton" is
never masked -- see tests/test_vault_notes.py.

`spans(text)` mirrors `redact.secret_spans`: positions, not labels, for
`app/vault/notes_text.py` to replace with `[скрыто]` before chunking.
It does not merge with `redact.secret_spans`'s output; the caller does,
since either list alone can already contain overlaps.

**The user reviews this list before it merges** (as with the phase-4
lists) -- report it verbatim.
"""

from __future__ import annotations

import re

# AWS access key ids: a 4-letter type prefix (AKIA = long-term user key,
# ASIA = temporary/STS key are the two that appear in the wild) followed
# by exactly 16 upper-case letters or digits -- the fixed shape AWS
# itself generates. `\b` on both sides so "AKIAoutside" style false
# joins are avoided.
AWS_ACCESS_KEY_ID = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")

# GitHub's prefixed personal/OAuth/app tokens: ghp_ (personal), gho_
# (OAuth), ghu_ (user-to-server app), ghs_ (server-to-server app), ghr_
# (refresh). GitHub's own tokens are 36 chars after the prefix; 30+ is
# used here to tolerate any future length change without opening the
# pattern up to ordinary underscored words.
GITHUB_TOKEN = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")

# GitHub's newer fine-grained "github_pat_" tokens: an 11-char prefix
# id and a long opaque suffix, base62 plus underscores.
GITHUB_PAT = re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")

# `sk-`-style API keys: OpenAI's `sk-...` and `sk-proj-...`, Anthropic's
# `sk-ant-...`. The 20-char minimum after the (optional) `proj-`/`ant-`
# marker is what keeps this from matching a short hyphenated word like
# "sk-8" while still catching every vendor's real key length.
API_KEY = re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}\b")

# Slack tokens: xoxa- (app), xoxb- (bot), xoxp- (user), xoxr- (refresh),
# xoxs- (workspace). Slack's own tokens are digit-dash-digit-dash-hex;
# 10+ chars after the dash is loose enough to survive a format tweak
# while still requiring far more than any ordinary "xox"-prefixed word
# (there are none in Russian or English).
SLACK_TOKEN = re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}\b")

# JWTs: three base64url segments, the first (the header) always
# starting `eyJ` -- the base64 encoding of `{"`. Requiring that prefix,
# rather than just "three dot-separated base64url runs", is what keeps
# this from matching a version string or a file path with dots.
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")

# PEM private-key blocks, e.g. `-----BEGIN RSA PRIVATE KEY-----` ...
# `-----END RSA PRIVATE KEY-----`, multi-line. DOTALL so the body
# between the fences (which is itself base64, wrapped at 64 chars) is
# consumed regardless of newlines. Non-greedy so two unrelated blocks in
# one note are matched separately rather than swallowed as one span.
PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)

_PATTERNS = (
    AWS_ACCESS_KEY_ID,
    GITHUB_TOKEN,
    GITHUB_PAT,
    API_KEY,
    SLACK_TOKEN,
    JWT,
    PRIVATE_KEY_BLOCK,
)


def spans(text: str) -> list[tuple[int, int]]:
    """Every `(start, end)` span any pattern above matched in `text`.

    Unordered and possibly overlapping (a JWT segment happens to also
    look like the tail of a PEM block, say). The caller sorts and
    merges, together with `redact.secret_spans`.
    """
    found: list[tuple[int, int]] = []
    for pattern in _PATTERNS:
        for match in pattern.finditer(text):
            found.append(match.span())
    return found
