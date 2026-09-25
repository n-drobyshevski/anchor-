"""/privacy (Phase 6 plan section 9.5): a fixed note, 8-10 lines."""

from __future__ import annotations

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
