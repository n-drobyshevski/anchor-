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
