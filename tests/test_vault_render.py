"""Rendering facts and days into files (phase-5 plan sections 4.1, 4.2, 4.4). Pure."""

from __future__ import annotations

import datetime

import yaml

from app.vault import frontmatter, render

D = datetime.date


def _view(**overrides) -> render.FactView:
    base = dict(
        memory_id=142,
        kind="preference",
        text="Любит работать по утрам, до 11.",
        pinned=False,
        source="extractor",
        created=D(2026, 9, 20),
    )
    base.update(overrides)
    return render.FactView(**base)


def test_names_are_ascii_and_carry_the_epoch():
    assert render.fact_path(142, "k3f9qa") == "Anchor/Memory/0142-k3f9qa.md"
    assert render.fact_path(12345, "k3f9qa") == "Anchor/Memory/12345-k3f9qa.md"
    assert render.journal_path(D(2026, 9, 25), "k3f9qa") == "Anchor/Journal/2026-09-25-k3f9qa.md"


def test_fact_file_shape_and_round_trip():
    out = render.render_fact(_view(), "k3f9qa")
    assert out.content.startswith("---\nanchor: fact\nanchor_epoch: k3f9qa\nanchor_id: 142\n")
    assert "\\u" not in out.content
    assert "Любит работать по утрам, до 11." in out.content
    meta = frontmatter.load(out.content)
    assert list(meta) == list(render.FACT_KEYS)
    assert meta["fact"] == "Любит работать по утрам, до 11."
    assert meta["created"] == "2026-09-20"
    assert meta["pinned"] is False
    assert "## Раньше" not in out.content


def test_texts_that_look_like_other_types_stay_text():
    for text in ("no", "yes", "2026-09-20", "123", "null", "~", "true", ": colon", "#hash", "- dash"):
        meta = frontmatter.load(render.render_fact(_view(text=text), "k3f9qa").content)
        assert meta["fact"] == text


def test_a_long_russian_fact_is_never_folded():
    text = "очень длинный факт " * 15
    out = render.render_fact(_view(text=text.strip()), "k3f9qa")
    fact_lines = [line for line in out.content.splitlines() if line.startswith("fact:")]
    assert len(fact_lines) == 1


def test_digest_is_stable_and_ignores_extras():
    first = render.render_fact(_view(), "k3f9qa")
    again = render.render_fact(_view(), "k3f9qa")
    with_extras = render.render_fact(_view(), "k3f9qa", "tags: [утро]\n")
    assert first.digest == again.digest == with_extras.digest
    assert first.content == again.content
    assert "tags: [утро]\n---\n" in with_extras.content


def test_digest_moves_with_anything_anchor_owns():
    base = render.render_fact(_view(), "k3f9qa").digest
    for change in (
        dict(text="другое"),
        dict(pinned=True),
        dict(kind="rule"),
        dict(history=((D(2026, 9, 12), "раньше"),)),
    ):
        assert render.render_fact(_view(**change), "k3f9qa").digest != base
    assert render.render_fact(_view(), "zzzzzz").digest != base


def test_history_newest_first():
    view = _view(history=((D(2026, 9, 12), "Любит работать по вечерам."), (D(2026, 9, 1), "Сова.")))
    content = render.render_fact(view, "k3f9qa").content
    body = content.split("## Раньше\n", 1)[1]
    assert body == "- 2026-09-12 — Любит работать по вечерам.\n- 2026-09-01 — Сова.\n"


def test_a_technique_names_its_source_and_quotes_it():
    view = _view(kind="technique", technique_source=("example.com", "Ложитесь спать в одно и то же время."))
    content = render.render_fact(view, "k3f9qa").content
    assert "Источник: example.com" in content
    assert "> Ложитесь спать в одно и то же время." in content


def test_newlines_in_stored_text_cannot_break_the_body():
    view = _view(history=((D(2026, 9, 12), "строка\n## Взлом\n- пункт"),))
    content = render.render_fact(view, "k3f9qa").content
    assert "\n## Взлом" not in content


def test_journal_day():
    view = render.JournalView(
        local_date=D(2026, 9, 25),
        day_rating=4,
        due_label="сделано",
        note="устал",
        entries=("Поговорили про отчёт.", "Вечером гуляли."),
    )
    out = render.render_journal(view, "k3f9qa")
    meta = frontmatter.load(out.content)
    assert meta == {"anchor": "journal", "anchor_epoch": "k3f9qa", "date": "2026-09-25"}
    assert "## Чек-ин\n- Оценка дня: 4/5\n- Главное действие: сделано\n- Заметка: устал\n" in out.content
    assert out.content.endswith("## Журнал\n- Поговорили про отчёт.\n- Вечером гуляли.\n")


def test_an_empty_checkin_renders_no_checkin_section():
    view = render.JournalView(local_date=D(2026, 9, 25), entries=("x",))
    assert "## Чек-ин" not in render.render_journal(view, "k3f9qa").content
    assert render.JournalView(local_date=D(2026, 9, 25)).is_empty


# --- frontmatter: the bot's copy of the strict rules -----------------------


def test_extra_segments_keep_the_users_keys_verbatim_and_in_order():
    content = (
        "---\n"
        "anchor: fact\n"
        "tags: [утро, работа]   # мои теги\n"
        "fact: старый текст\n"
        "aliases:\n"
        "- Жаворонок\n"
        "- early bird\n"
        "anchor_id: 1\n"
        "---\n"
        "body\n"
    )
    extras = frontmatter.extra_segments(content, render.FACT_KEYS)
    assert extras == "tags: [утро, работа]   # мои теги\naliases:\n- Жаворонок\n- early bird\n"


def test_extra_segments_refuse_properties_they_cannot_read():
    for bad in (
        "---\nfact: a\nfact: b\n---\n",
        "---\nx: &a [1]\ny: *a\n---\n",
        "---\nfact: [unclosed\n---\n",
    ):
        assert frontmatter.extra_segments(bad, render.FACT_KEYS) is None


def test_no_frontmatter_means_no_extras():
    assert frontmatter.extra_segments("just text\n", render.FACT_KEYS) == ""
    assert frontmatter.extra_segments("---\n---\nbody", render.FACT_KEYS) == ""


def test_the_loader_refuses_what_vaultd_refuses():
    for text in (
        "---\nanchor: fact\nanchor: fact\n---\n",
        "---\na: &x 1\nb: *x\n---\n",
        "---\n- a\n---\n",
        "---\nanchor: !!python/name:os.system x\n---\n",
        "---\nfiller: \"" + "x" * 5000 + "\"\n---\n",
    ):
        assert frontmatter.load(text) is None
    assert yaml.safe_load("a: 1") == {"a": 1}
