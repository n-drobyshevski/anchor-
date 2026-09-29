"""/privacy (Phase 6 plan section 9.5): a fixed note, 8-10 lines."""

from __future__ import annotations

import pathlib

from app.tg.router import BOT_COMMANDS, PRIVACY_TEXT


def test_privacy_note_is_eight_to_ten_lines():
    lines = PRIVACY_TEXT.splitlines()
    assert 8 <= len(lines) <= 10
    assert all(line.strip() for line in lines)


def test_privacy_note_names_what_section_9_5_requires():
    for needle in ("Railway", "OpenRouter", "Telegram", "14", "8", "30 дней", "/export", "/delete"):
        assert needle in PRIVACY_TEXT


def test_privacy_command_is_registered():
    assert "privacy" in {command.command for command in BOT_COMMANDS}


def test_docs_privacy_carries_one_bullet_per_line_of_the_bot_text():
    """docs/privacy.md is the English version, kept in sync by hand. 8e
    found it a line short (the planner's); this keeps the count honest."""
    doc = (pathlib.Path(__file__).resolve().parent.parent / "docs" / "privacy.md").read_text(encoding="utf-8")
    summary = doc.split("## What this does not cover", 1)[0]
    bullets = [line for line in summary.splitlines() if line.startswith("- ")]
    assert len(bullets) == len(PRIVACY_TEXT.splitlines())


def test_the_notes_line_is_in_both_places():
    """8e plan section 6: personal notes only for the conversation."""
    doc = (pathlib.Path(__file__).resolve().parent.parent / "docs" / "privacy.md").read_text(encoding="utf-8")
    assert "Obsidian" in PRIVACY_TEXT and "Obsidian" in doc
    assert "никогда для поиска или исследований" in PRIVACY_TEXT
    assert "never for search or research" in doc


def test_the_lens_clause_is_in_both_places():
    """L1: Claude Code's read of the lens, and where it goes, in both copies."""
    doc = (pathlib.Path(__file__).resolve().parent.parent / "docs" / "privacy.md").read_text(encoding="utf-8")
    assert "/lens code on" in PRIVACY_TEXT and "/lens code on" in doc
    assert "прочитанное уходит в Anthropic" in PRIVACY_TEXT
    assert "what it reads goes to Anthropic" in doc


def test_the_lens_review_clause_is_in_both_places():
    """L2: which notes the review picked, which Claude Code can read, the
    rationale it cannot (written from the week, so only the user sees
    it), and what the weekly review sends the model while the lens is
    on, in both copies."""
    doc = (pathlib.Path(__file__).resolve().parent.parent / "docs" / "privacy.md").read_text(encoding="utf-8")
    assert "какие из них выбрал еженедельный разбор" in PRIVACY_TEXT
    assert "но не с объяснением почему" in PRIVACY_TEXT and "видно только тебе" in PRIVACY_TEXT
    assert "which of them the weekly review picked" in doc
    assert "but not its explanation of" in doc and "only you see" in doc
    assert "объяснениями еженедельного разбора" not in PRIVACY_TEXT
    assert "explanations of why it picked them" not in doc
    assert "еженедельный разбор отправляет модели каталог линзы" in PRIVACY_TEXT
    assert "review sends the model the lens catalog" in doc
    assert "начало текста, связи" in PRIVACY_TEXT and "start of the text, links" in doc
    assert "(и только их)" not in PRIVACY_TEXT and "and only they" not in doc


def test_the_lens_garden_clause_is_in_both_places():
    """L3 (spec section 8): what the weekly garden sends the model while
    it is on -- titles, summaries or the start of the text (as the L2
    catalog), links, and counts of links to knowledge notes, never
    their titles -- and that Claude Code can read its proposals, in both
    copies."""
    doc = (pathlib.Path(__file__).resolve().parent.parent / "docs" / "privacy.md").read_text(encoding="utf-8")
    assert "Пока включён сад линзы, раз в неделю модели уходят названия и краткие описания" in PRIVACY_TEXT
    assert "(или начало текста) заметок линзы" in PRIVACY_TEXT and "видит и Claude Code" in PRIVACY_TEXT
    assert "без текста заметок" not in PRIVACY_TEXT
    assert "While the lens garden is on, once a week the model gets the" in doc
    assert "summaries or the start of the text, the links" in doc and "Claude Code can read its proposals" in doc
    assert "not the\n  notes' text" not in doc
