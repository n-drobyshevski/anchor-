"""README's command table keeps up with the bot (package C).

README.md once said "Phase 4 complete" long after phases 5 and 6
shipped. The table under "## Commands" is the part a new reader relies
on, so every registered command must appear in it.
"""

from __future__ import annotations

import pathlib
import re

from app.tg.router import BOT_COMMANDS

README = pathlib.Path(__file__).resolve().parent.parent / "README.md"


def _commands_section() -> str:
    text = README.read_text(encoding="utf-8")
    start = text.index("## Commands")
    end = text.index("\n## ", start + 1)
    return text[start:end]


def test_every_registered_command_is_in_the_readme_table():
    listed = set(re.findall(r"`/([a-z_]+)", _commands_section()))
    registered = {command.command for command in BOT_COMMANDS} | {"weblogout"}
    assert registered - listed == set()


def test_readme_no_longer_claims_phase_4_is_the_latest():
    assert "Phase 4 complete" not in README.read_text(encoding="utf-8")
