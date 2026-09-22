"""core/pause.py tests (plan section 7 / 16).

- every section 7 example, both HARD and SOFT
- every section 16 negative
- HARD beats SOFT in one message
- normalization: case, ё->е, punctuation, embedded digits
- "пурпрный" (mid-word letter drop) matches per the broader rule
  (plan decision 1 -- see app/core/pause.py's module docstring)
"""

from __future__ import annotations

import pytest

from app.core.pause import levenshtein, match, normalize


@pytest.mark.parametrize(
    "text",
    ["пурпурный", "Пурпурный!!", "пурпурнй", "пурпрный", "красный", "КРАСНЫЙ."],
)
def test_hard_examples_match_hard(text):
    assert match(text) == "hard"


@pytest.mark.parametrize("text", ["жёлтый", "желтый"])
def test_soft_examples_match_soft(text):
    assert match(text) == "soft"


@pytest.mark.parametrize(
    "text",
    ["красивый", "прекрасно", "пурпур", "желание"],
)
def test_section_16_negatives_do_not_match(text):
    assert match(text) is None


def test_bare_purple_stem_alone_does_not_match():
    """пурпур is exactly distance 1 from пурпурн, but len < 7 excludes it
    (plan decision 1 -- the length gate that resolves the section 7 /
    section 16 contradiction)."""
    assert match("пурпур") is None


def test_hard_beats_soft_in_one_message():
    assert match("пурпурный и жёлтый") == "hard"
    assert match("жёлтый, но вообще-то красный") == "hard"


def test_normalize_lowercases_folds_yo_and_blanks_punctuation_and_digits():
    assert normalize("ЖЁЛТЫЙ!!!") == ["желтый"]
    assert normalize("Пурпурный,   красный2 - жёлтый?") == ["пурпурный", "красный", "желтый"]
    assert normalize("крас2ный") == ["крас", "ный"]


def test_normalize_splits_on_whitespace_and_punctuation():
    assert normalize("привет, как дела?") == ["привет", "как", "дела"]


def test_match_is_none_for_empty_or_unrelated_text():
    assert match("") is None
    assert match("привет, как дела?") is None


def test_levenshtein_basic_distances():
    assert levenshtein("", "") == 0
    assert levenshtein("а", "") == 1
    assert levenshtein("", "а") == 1
    assert levenshtein("кот", "кот") == 0
    assert levenshtein("кот", "код") == 1
    assert levenshtein("пурпур", "пурпурн") == 1  # insertion
    assert levenshtein("пурпурн", "пурпурный") == 2


