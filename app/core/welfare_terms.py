"""The deterministic backstop under the welfare classifier (H2).

app/core/welfare.py fails open: a classifier that times out, errors, or
returns something unparseable produces a `none` verdict and the normal
persona reply goes out. That is the right default for a check whose
false positives interrupt a conversation -- but it means the one case
the check exists for, an explicit mention of self-harm or suicide, can
be missed because a provider was slow.

So when the classifier produces no usable verdict, this module gets a
second look. It is not a classifier and does not try to be: no scoring,
no context, no cleverness. It asks one question -- does this text
contain a word that nobody types by accident -- and a hit is treated
exactly as `level="real"`.

**Precision is deliberately traded away.** This runs only after the
model already failed, so the alternative is no check at all, and the
two errors are not symmetric: a false positive is a warm message and a
button the user dismisses; a false negative is the persona pushing
someone who has just said they want to die.

**Privacy.** `hit()` returns a bool and nothing else. There is no API
here that reveals *which* term matched, so no caller can log one even by
mistake -- the same discipline app/core/welfare.py applies to the text
it classifies (plan sections 10 and 13).

**Three languages** because the user writes in Russian, lives in
France, and the model will sometimes be addressed in English. A crisis
is exactly when someone reverts to whichever language comes first.
"""

from __future__ import annotations

import re

# Stems, matched as a prefix at a word boundary: "суицид" catches
# суицид, суицидальный, суицидом. Kept as stems rather than full words
# because Russian and French inflect, and a list of every form would be
# both longer and easier to leave a gap in.
STEMS: tuple[str, ...] = (
    # --- Russian ---
    "суицид",
    "самоубийств",
    "самоповрежд",
    "передозировк",
    "вскрыться",
    "повеситься",
    "повешусь",
    # --- French ---
    "suicid",  # suicide, suicider, suicidaire
    "automutil",
    "scarifi",
    # --- English ---
    "selfharm",
    "overdose",
)

# Multi-word phrases, matched literally. A phrase carries its own
# context, which is what makes it safe to match without one: "убить
# себя" is not something the persona's usual register produces.
PHRASES: tuple[str, ...] = (
    # --- Russian ---
    "покончить с собой",
    "покончу с собой",
    "счеты с жизнью",
    "свести счеты",
    "уйти из жизни",
    "убить себя",
    "убью себя",
    "не хочу жить",
    "не хочется жить",
    "жить не хочу",
    "хочу умереть",
    "хочу сдохнуть",
    "лучше бы я умер",
    "вскрыть вены",
    "вскрыл вены",
    "режу себя",
    "порезать себя",
    # --- French ---
    "me tuer",
    "me suicider",
    "envie de mourir",
    "veux mourir",
    "en finir avec la vie",
    "me faire du mal",
    "me pendre",
    "plus envie de vivre",
    # --- English ---
    "self-harm",
    "self harm",
    "kill myself",
    "killing myself",
    "end my life",
    "end it all",
    "want to die",
    "wanna die",
    "cut myself",
    "cutting myself",
    "hang myself",
    "better off dead",
    "don't want to live",
    "dont want to live",
)


def _normalize(text: str) -> str:
    """Lowercase, and fold ё to е.

    Russian keyboards and autocorrect disagree about ё, so «счёты» and
    «счеты» are the same word for this purpose. Every term above is
    written with е, so the folding only ever has to run one way.
    """
    return text.casefold().replace("ё", "е")


def _build_pattern() -> re.Pattern[str]:
    """One alternation, compiled once at import.

    Stems get a leading \\b and no trailing one, so they match prefixes.
    Phrases are escaped and matched literally -- several contain a
    hyphen or an apostrophe, which \\b would treat as a boundary and
    quietly change the match.
    """
    parts = [rf"\b{re.escape(stem)}" for stem in STEMS]
    parts += [re.escape(phrase) for phrase in PHRASES]
    return re.compile("|".join(parts))


_PATTERN = _build_pattern()


def hit(*texts: str | None) -> bool:
    """True if any of `texts` contains a self-harm or suicide term.

    Variadic so the caller can pass the user's message and the last
    couple of turns in one call without building a list. `None` entries
    are skipped, which lets a caller splice in a context slice that may
    be short without checking its length first.

    Returns only a bool, on purpose -- see the module docstring.
    """
    for text in texts:
        if text and _PATTERN.search(_normalize(text)):
            return True
    return False
