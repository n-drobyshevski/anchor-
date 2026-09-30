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
    # L5 amends the sentence (both consumers' picks), keeping these words.
    assert "но не с объяснением почему" in PRIVACY_TEXT and "видно только тебе" in PRIVACY_TEXT
    assert "which of them the weekly review" in doc
    assert "picked, but not why: the" in doc and "only you see it" in doc
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


def test_the_lens_research_clause_is_in_both_places():
    """L4 (spec section 7): after a tap on «исследовать и написать», what goes where
    -- the gap and its notes' titles and summaries to the model, the
    query to Exa, pages only from PACKET_LENS -- and that an accepted
    result becomes a knowledge note in Echo/Inbox, undoable for 14 days,
    in both copies."""
    doc = (pathlib.Path(__file__).resolve().parent.parent / "docs" / "privacy.md").read_text(encoding="utf-8")
    assert "Если нажать «исследовать и написать» под пунктом сада" in PRIVACY_TEXT
    assert "названия и краткие описания его заметок" in PRIVACY_TEXT
    assert "в Exa" in PRIVACY_TEXT and "PACKET_LENS" in PRIVACY_TEXT
    assert "заметкой знаний в Echo/Inbox" in PRIVACY_TEXT and "14 дней (/lens undo)" in PRIVACY_TEXT
    assert "If you tap «исследовать и написать» under a garden gap" in doc
    assert "titles and summaries of its notes" in doc
    assert "goes\n  to Exa" in doc and "`PACKET_LENS`" in doc
    assert "knowledge note in\n  `Echo/Inbox`" in doc and "14 days (`/lens undo`)" in doc


def test_the_lens_reflect_clause_is_in_both_places():
    """L5 (the L5 spec section 5; owner decisions on its risks 2 and 5):
    Claude Code sees which notes the reflection picked too, never why;
    what the reflection sends the model while its lens is on (the
    catalog, Echo's draft notebook changes and the picked notes); that
    only open threads are rephrased, never observations about the user;
    that rephrased threads stay in the notes the persona reads, even
    with the lens off, until they close or expire; that the per-scene
    reflection and critique send no lens text; and that critique's ids
    show only in /export -- in both copies."""
    doc = (pathlib.Path(__file__).resolve().parent.parent / "docs" / "privacy.md").read_text(encoding="utf-8")
    assert "выбрал еженедельный разбор или рефлексия заметок, но не с объяснением почему" in PRIVACY_TEXT
    assert "у разбора оно написано по твоей неделе и видно только тебе, у рефлексии не хранится вовсе" in PRIVACY_TEXT
    assert "which of them the weekly review or the notebook reflection picked" in doc
    assert "and the reflection\n  keeps none" in doc
    assert "Пока включена линза в рефлексии заметок, модели уходят каталог линзы, черновик изменений" in PRIVACY_TEXT
    assert "рабочих заметок Echo и выбранные заметки целиком" in PRIVACY_TEXT
    assert "While the lens is on in the notebook reflection, the model" in doc
    assert "Echo's draft changes to its working notes" in doc
    assert "переформулируются только незакрытые темы, наблюдения о тебе — никогда" in PRIVACY_TEXT
    assert "never observations about you" in doc
    assert "пока не закроются или не истекут, даже если линзу выключить" in PRIVACY_TEXT
    assert "until they are closed or expire, even if the\n  lens is turned off" in doc
    assert "Рефлексия по отдельному разговору и оценка ответов текста линзы модели не отправляют" in PRIVACY_TEXT
    assert "The per-conversation reflection and the rating of\n  replies send the model no lens text" in doc
    assert "видно только в /export" in PRIVACY_TEXT and "shows only in `/export`" in doc
