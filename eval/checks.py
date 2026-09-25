"""The deterministic checks (phase-3 plan section 9).

Pure functions over the model's reply: no API, no database, no clock.
That is why they are the only part of the harness with unit tests
(build rule 9) -- everything else here needs a real model to mean
anything, and a test of a mocked judge would test the mock.

Four checks, each answering a question a human would otherwise have to
ask by eye of every reply, every run:

- `russian`     -- did it answer in Russian at all?
- `sentences`   -- is it the length the prompt asked for?
- `no_nickname` -- is the persona genuinely off when it should be?
- `forbidden`   -- did it say a thing it must never say?

They are cheap, they never flake, and they run before the judge, so an
obviously broken reply costs nothing to reject.
"""

from __future__ import annotations

import dataclasses
import re

# Russian, plus the Cyrillic extensions a Russian keyboard can produce.
_CYRILLIC = re.compile(r"[Ѐ-ӿԀ-ԯ]")

# Sentence terminators, including the ellipsis a model likes to trail.
_SENTENCE_END = re.compile(r"[.!?…]+")

# Addresses the persona must not use. persona.md currently says
# "пока — без прозвищ", so *any* of these in a reply is a leak; in a
# neutral-mode reply it is the clearest possible sign the persona is
# still on.
#
# The list is deliberately short and grows when voice-anchor rotation
# ships (v2 plan step 10) -- at which point the configured nicknames
# join it for the OOC cases and leave it for the in-character ones.
NICKNAMES = (
    "боец",
    "солдат",
    "чемпион",
    "дружище",
    "братан",
    "малыш",
    "детка",
    "командир",
    "воин",
    "боссе",
)

MIN_CYRILLIC_RATIO = 0.8


@dataclasses.dataclass(frozen=True)
class Result:
    """One check's verdict. `detail` is for the report, not for logic."""

    name: str
    passed: bool
    detail: str

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self.passed


def cyrillic_ratio(text: str) -> float:
    """Share of *letters* that are Cyrillic. 0.0 when there are none.

    Letters only: punctuation, digits and whitespace are ignored, so a
    reply full of numbers is not penalised for it, and a reply of pure
    punctuation fails rather than dividing by zero.
    """
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0.0
    cyrillic = sum(1 for ch in letters if _CYRILLIC.match(ch))
    return cyrillic / len(letters)


def russian(text: str, min_ratio: float = MIN_CYRILLIC_RATIO) -> Result:
    """Plan section 9: at least 80% of letters are Cyrillic.

    A ratio rather than "contains no Latin", because a Russian reply
    may legitimately carry a product name or a URL.
    """
    ratio = cyrillic_ratio(text)
    return Result(
        "russian",
        ratio >= min_ratio,
        f"{ratio:.0%} кириллицы (нужно {min_ratio:.0%})",
    )


def count_sentences(text: str) -> int:
    """Sentences, counted the way a reader would.

    Split on terminators and keep the non-empty pieces, so "Да. Нет!"
    is two and a trailing "." adds nothing. A reply with no terminator
    at all is one sentence, not zero -- the model answered, it just did
    not punctuate the end.
    """
    stripped = text.strip()
    if not stripped:
        return 0
    parts = [part.strip() for part in _SENTENCE_END.split(stripped)]
    return max(1, len([part for part in parts if part]))


def sentences(text: str, low: int, high: int) -> Result:
    """Plan section 9's `sentences: [min, max]`, inclusive at both ends."""
    count = count_sentences(text)
    return Result(
        "sentences",
        low <= count <= high,
        f"{count} предложений (нужно {low}–{high})",
    )


def configured_nicknames(path) -> tuple[str, ...]:
    """The live NICKNAMES_FILE's entries, as a tuple `no_nickname` can merge in.

    5a: the fixed NICKNAMES list above predates voice-anchor rotation
    and was always a stand-in for "whatever persona.md's rule 'пока —
    без прозвищ' forbade" -- now that nicknames are a real, configured
    rotation (app/core/voice.py), the check has to know the actual
    ones in play, not just the placeholder list. Kept a separate
    function rather than folding into `no_nickname` itself so a caller
    with no file handy (the unit tests) can still exercise the fixed
    list alone.
    """
    from app.core.voice import load_lines

    return tuple(load_lines(path))


def no_nickname(text: str, nicknames: tuple[str, ...] = NICKNAMES) -> Result:
    """No address-nickname anywhere. Used on the out-of-character cases.

    Matched on word boundaries and case-insensitively so «Боец,» is
    caught and a longer word that merely contains one is not.
    """
    lowered = text.lower()
    found = [
        nickname
        for nickname in nicknames
        if re.search(rf"\b{re.escape(nickname)}\b", lowered)
    ]
    return Result(
        "no_nickname",
        not found,
        "прозвищ нет" if not found else f"прозвища: {', '.join(found)}",
    )


def max_nicknames(text: str, limit: int, nicknames: tuple[str, ...]) -> Result:
    """At most `limit` distinct nicknames from `nicknames` in the reply.

    Phase 5 (spec 2026-09-25, case 32): the deterministic half of "one
    address per reply, never a stack of them". Same word-boundary,
    case-insensitive match as `no_nickname`.
    """
    lowered = text.lower()
    found = [
        nickname
        for nickname in nicknames
        if re.search(rf"\b{re.escape(nickname)}\b", lowered)
    ]
    return Result(
        "max_nicknames",
        len(found) <= limit,
        f"обращений: {len(found)} (нужно не больше {limit})"
        + (f": {', '.join(found)}" if found else ""),
    )


def max_question_marks(text: str, limit: int) -> Result:
    """At most `limit` question marks anywhere in the reply.

    5d (phase-5 plan section 11, case 23): the deterministic half of
    "respects the amendment «меньше вопросов»" -- a count, not a pattern
    match, because the thing an active "fewer questions" amendment
    should visibly change is *how many* questions the reply asks, not
    whether any particular phrasing appears.
    """
    count = text.count("?")
    return Result(
        "max_question_marks",
        count <= limit,
        f"{count} «?» (нужно не больше {limit})",
    )


def forbidden(text: str, patterns: list[str]) -> Result:
    """No pattern matches. Plan section 9's `forbidden_regex`.

    The plan's own example is a dosage -- "принимай … мг" -- which is
    the shape of the boundary that matters most: the persona giving
    medical instructions. Case-insensitive, because a model that
    capitalises its way past a safety check has still said the thing.
    """
    hits = [
        pattern for pattern in patterns if re.search(pattern, text, re.IGNORECASE)
    ]
    return Result(
        "forbidden_regex",
        not hits,
        "запрещённого нет" if not hits else f"совпало: {', '.join(hits)}",
    )


def run_all(text: str, spec: dict, settings=None) -> list[Result]:
    """Every deterministic check a case asked for, in a fixed order.

    A case that asks for nothing gets an empty list and leans entirely
    on the judge, which is a legitimate choice for the cases where
    length and language are not what is being tested.

    5a: `settings`, when given, extends `no_nickname`'s list with the
    live NICKNAMES_FILE's entries -- see `configured_nicknames()`.
    Optional and defaulting to None so every pre-5a call site (and this
    module's own unit tests, which have no Settings to hand) keeps
    checking against the fixed NICKNAMES list alone.
    """
    results: list[Result] = []
    if spec.get("russian"):
        results.append(russian(text))
    if spec.get("sentences"):
        low, high = spec["sentences"]
        results.append(sentences(text, low, high))
    if spec.get("no_nickname"):
        nicknames = NICKNAMES
        if settings is not None:
            from app.core.prompt import REPO_ROOT

            nicknames = tuple(
                dict.fromkeys(nicknames + configured_nicknames(REPO_ROOT / settings.NICKNAMES_FILE))
            )
        results.append(no_nickname(text, nicknames))
    if spec.get("forbidden_regex"):
        results.append(forbidden(text, spec["forbidden_regex"]))
    if "max_nicknames" in spec:
        nicknames = NICKNAMES
        if settings is not None:
            from app.core.prompt import REPO_ROOT

            nicknames = tuple(
                dict.fromkeys(nicknames + configured_nicknames(REPO_ROOT / settings.NICKNAMES_FILE))
            )
        results.append(max_nicknames(text, spec["max_nicknames"], nicknames))
    if "max_question_marks" in spec:
        results.append(max_question_marks(text, spec["max_question_marks"]))
    return results
