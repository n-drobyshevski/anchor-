"""Pause-word matching (plan section 7) -- the off-switch, in code,
never the model.

This module is intentionally dumb: no LLM, no fuzzy config, nothing
that could be talked around mid-conversation. `match()` is the single
entry point core/turn.py calls before anything else happens.

**The length-7 gate, explained** (plan decision 1): section 7 asks for
a fuzzy match within Levenshtein distance 1 of "пурпурн", but section
16 requires the bare stem "пурпур" to NOT match -- and "пурпур" is
itself exactly distance 1 from "пурпурн" (one trailing "н" inserted).
Gating fuzzy matching on `len(token) >= 7` excludes the 6-letter bare
stem while still catching real typos of the 7+-letter word. Checking
the token's first 6, 7, *and* 8 characters (not just a literal
"first 7 chars") additionally catches a dropped letter mid-word, e.g.
"пурпрный" (missing the second "у"): its first 6 chars, "пурпрн", are
distance 1 from "пурпурн" by a single insertion, even though its first
7 chars are not. A missed safeword is the dangerous failure mode --
section 7 explicitly accepts false positives as fail-safe -- so this
rule is built to lean toward matching, not away from it.
"""

from __future__ import annotations

from typing import Literal

Level = Literal["hard", "soft"]

_PURPLE_STEM = "пурпурн"  # HARD, fuzzy-eligible (see module docstring)
_RED_STEM = "красн"  # HARD, exact prefix only -- fuzzy would catch "красивый"
_YELLOW_STEM = "желт"  # SOFT, exact prefix

_YO_TO_YE = str.maketrans({"ё": "е"})


def normalize(text: str) -> list[str]:
    """Lowercase, fold ё->е, blank out every non-letter, and tokenize.

    Digits and punctuation become spaces rather than being dropped
    outright, so "красный2" still tokenizes to ["красный"] while
    "крас2ный" correctly splits into two non-matching halves instead of
    silently gluing into a match.
    """
    lowered = text.lower().translate(_YO_TO_YE)
    letters_only = "".join(ch if ch.isalpha() else " " for ch in lowered)
    return letters_only.split()


def levenshtein(a: str, b: str) -> int:
    """Edit distance between `a` and `b`. Hand-rolled two-row DP, no dependency."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    previous_row = list(range(len(b) + 1))
    current_row = [0] * (len(b) + 1)

    for i, char_a in enumerate(a, start=1):
        current_row[0] = i
        for j, char_b in enumerate(b, start=1):
            cost = 0 if char_a == char_b else 1
            current_row[j] = min(
                previous_row[j] + 1,  # deletion from a
                current_row[j - 1] + 1,  # insertion into a
                previous_row[j - 1] + cost,  # substitution
            )
        previous_row, current_row = current_row, previous_row

    return previous_row[len(b)]


def _is_hard_purple(token: str) -> bool:
    if token.startswith(_PURPLE_STEM):
        return True
    if len(token) >= 7:
        return min(levenshtein(token[:k], _PURPLE_STEM) for k in (6, 7, 8)) <= 1
    return False


def match(text: str) -> Level | None:
    """The pause level for `text`, or None. HARD beats SOFT when both appear."""
    tokens = normalize(text)

    if any(_is_hard_purple(token) or token.startswith(_RED_STEM) for token in tokens):
        return "hard"
    if any(token.startswith(_YELLOW_STEM) for token in tokens):
        return "soft"
    return None
