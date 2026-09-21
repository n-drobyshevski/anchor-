"""core/split.py tests (plan section 11 / 16).

- under the limit: returned unchanged as a single chunk
- exactly at the limit: still a single chunk
- a long paragraph with no punctuation at all: hard-cut fallback, no
  infinite loop, no empty chunks, every chunk fits
- Cyrillic text splits the same way (characters, not bytes)
"""

from __future__ import annotations

from app.core.split import split


def test_empty_text_returns_no_chunks():
    assert split("") == []


def test_under_limit_returned_as_single_chunk():
    text = "Коротко и по делу."
    assert split(text, limit=4000) == [text]


def test_exactly_at_limit_returned_as_single_chunk():
    text = "x" * 4000
    chunks = split(text, limit=4000)
    assert chunks == [text]


def test_one_over_limit_splits_into_two_chunks():
    text = "x" * 4001
    chunks = split(text, limit=4000)
    assert len(chunks) == 2
    assert all(chunk for chunk in chunks)
    assert all(len(chunk) <= 4000 for chunk in chunks)
    assert "".join(chunks) == text


def test_prefers_paragraph_break_over_sentence_or_whitespace():
    first = "a" * 100
    second = "b" * 100
    text = first + "\n\n" + second
    chunks = split(text, limit=105)
    assert chunks[0] == first
    assert chunks[1] == second


def test_prefers_sentence_end_over_whitespace():
    text = "Раз. " + ("x" * 100) + " Два. " + ("y" * 100)
    chunks = split(text, limit=10)
    # The first cut must land right after a sentence-ending punctuation
    # mark, not at an arbitrary space.
    assert chunks[0].endswith(".")


def test_falls_back_to_whitespace_when_no_sentence_end():
    text = ("a" * 10) + " " + ("b" * 10)
    chunks = split(text, limit=12)
    assert chunks[0] == "a" * 10
    assert chunks[1] == "b" * 10


def test_long_paragraph_without_punctuation_hard_cuts_with_forward_progress():
    """No paragraph breaks, no sentence ends, no whitespace anywhere."""
    text = "a" * 12000
    chunks = split(text, limit=4000)

    assert len(chunks) == 3
    assert all(chunk for chunk in chunks)  # no empty chunks
    assert all(len(chunk) <= 4000 for chunk in chunks)
    assert "".join(chunks) == text


def test_cyrillic_text_splits_by_character_not_byte():
    # Each Cyrillic letter here is one Python character but multiple
    # UTF-8 bytes; splitting must count characters.
    text = "ё" * 9000
    chunks = split(text, limit=4000)

    assert len(chunks) == 3
    assert all(len(chunk) <= 4000 for chunk in chunks)
    assert "".join(chunks) == text


def test_no_chunk_is_ever_empty_across_many_shapes():
    samples = [
        "a" * 4000 + "\n\n" + "b" * 4000,
        ("Предложение. " * 1000),
        " ".join(["слово"] * 2000),
        "a" * 8001,
    ]
    for text in samples:
        chunks = split(text, limit=4000)
        assert all(chunk for chunk in chunks), f"empty chunk produced for sample starting {text[:20]!r}"
        assert all(len(chunk) <= 4000 for chunk in chunks)
