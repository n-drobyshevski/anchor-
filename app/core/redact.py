"""Code-side redaction for anything the model proposes storing (plan section 8).

The extractor's own prompt tells it never to record card numbers, document
numbers, passwords or health data. That instruction is worth having and is
worth nothing on its own: a prompt is a request, and this is the check.

Deliberately narrow. This rejects the three shapes plan section 8 names --
payment-card digits, IBANs, and email addresses -- and nothing else. A
broad "looks sensitive" heuristic would silently swallow ordinary facts
("пользователь живёт в доме 12 на улице..."), and a memory that vanishes
without explanation is worse than one that was never offered: the user
cannot tell the difference between the bot not noticing and the bot
discarding.

Health, crises and third-party detail are *not* pattern-matchable and are
not attempted here. They are the prompt's job, plus the user's own control
via /memories and /forget.
"""

from __future__ import annotations

import re

# 13-19 digits, optionally grouped by spaces or dashes: Visa/Mastercard/
# Amex and friends. Anchored on word boundaries so a long ordinary number
# (a year range, a phone number written solid) is less likely to trip it.
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")

# IBAN: two letters, two check digits, then up to 30 alphanumerics, with
# the optional four-character grouping banks print.
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}[ ]?[A-Z0-9]{1,4}\b", re.IGNORECASE)

_EMAIL = re.compile(r"\b[^\s@]+@[^\s@]+\.[a-z]{2,}\b", re.IGNORECASE)

CARD = "card"
IBAN = "iban"
EMAIL = "email"


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def find_secret(text: str) -> str | None:
    """The kind of secret `text` appears to contain, or None.

    Returns a *label*, never the matched substring: callers log this,
    and the whole point is that the value never reaches a log line.
    """
    if _EMAIL.search(text):
        return EMAIL
    if _IBAN.search(text):
        return IBAN
    # finditer, not findall: the pattern's repeated group makes findall
    # yield the last repetition rather than the whole span.
    for match in _CARD.finditer(text):
        if 13 <= len(_digits(match.group())) <= 19:
            return CARD
    return None


def is_safe_to_store(text: str) -> bool:
    return find_secret(text) is None
