"""app/vault/notes_text.py: turning raw note content into chunks (milestone 8d, phase 1).

Pure and synchronous -- no fixtures, no database, no event loop.
"""

from __future__ import annotations

from app.vault.notes_text import MASK, NOTE_CHUNK_CHARS, prepare


def _texts(content: str, title: str = "Заметка") -> list[str]:
    return [chunk.text for chunk in prepare(content, title)]


def test_frontmatter_is_stripped():
    content = "---\nanchor: personal\ntitle: x\n---\nТело заметки.\n"
    chunks = prepare(content, "Заметка")
    assert len(chunks) == 1
    assert "anchor" not in chunks[0].text
    assert "Тело заметки." in chunks[0].text


def test_content_with_no_frontmatter_fence_is_indexed_whole():
    content = "Просто текст без фронтматтера."
    assert _texts(content) == ["Просто текст без фронтматтера."]


def test_frontmatter_that_never_closes_is_treated_as_no_frontmatter():
    # No closing `---` within 4 KB: frontmatter.split returns None, so
    # this module follows vaultd's own rule and indexes it as body text
    # rather than refusing the file.
    content = "---\nanchor: personal\nБез закрывающей строки."
    assert _texts(content) == [content]


def test_inline_comment_is_removed():
    assert _texts("До %% скрытый комментарий %% после.") == ["До  после."]


def test_multiline_comment_is_removed():
    content = "Раньше.\n%%\nэто секрет\nна нескольких строках\n%%\nПотом."
    chunks = _texts(content)
    assert "секрет" not in " ".join(chunks)
    assert "Раньше." in chunks[0]
    assert "Потом." in chunks[0]


def test_fenced_code_block_is_removed():
    content = "Текст.\n```python\n" + "AK" + "IA1234567890ABCDEF" + "\n```\nЕщё текст."
    joined = " ".join(_texts(content))
    assert "```" not in joined
    assert "AKIA" not in joined
    assert "Текст." in joined
    assert "Ещё текст." in joined


def test_embed_is_dropped():
    assert _texts("Смотри ![[Картинка.png]] тут.") == ["Смотри  тут."]


def test_piped_wikilink_becomes_its_label():
    assert _texts("См. [[Заметки/Бег|бег по утрам]].") == ["См. бег по утрам."]


def test_plain_wikilink_becomes_last_path_segment_without_heading():
    assert _texts("См. [[Заметки/Проекты/CCRU#История]].") == ["См. CCRU."]


def test_headings_become_the_chunk_heading():
    content = "## Бег\nБегаю по утрам.\n\n## Сон\nЛожусь поздно."
    chunks = prepare(content, "Дневник")
    headings = {c.heading for c in chunks}
    assert headings == {"Дневник › Бег", "Дневник › Сон"}


def test_no_heading_falls_back_to_the_title():
    chunks = prepare("Просто текст.", "Дневник")
    assert chunks[0].heading == "Дневник"


def test_nested_headings_build_a_path_and_drop_deeper_levels_on_a_sibling():
    content = "# A\n## B\nТекст B.\n## C\nТекст C."
    chunks = prepare(content, "N")
    by_text = {c.text: c.heading for c in chunks}
    assert by_text["Текст B."] == "N › A › B"
    assert by_text["Текст C."] == "N › A › C"


def test_long_heading_path_is_truncated():
    content = "## Заголовок " + "x" * 250 + "\nТекст."
    chunks = prepare(content, "T")
    assert len(chunks[0].heading) == 200
    assert chunks[0].heading.endswith("…")


def test_chunks_stay_at_or_under_the_char_limit():
    paragraph = "Слово. " * 300  # comfortably over NOTE_CHUNK_CHARS
    chunks = prepare(paragraph, "T")
    assert len(chunks) > 1
    assert all(len(c.text) <= NOTE_CHUNK_CHARS for c in chunks)


def test_paragraphs_are_packed_together_under_the_limit():
    content = "Абзац один.\n\nАбзац два.\n\nАбзац три."
    chunks = prepare(content, "T")
    assert len(chunks) == 1
    assert chunks[0].text == "Абзац один.\n\nАбзац два.\n\nАбзац три."


def test_a_single_oversized_paragraph_is_hard_split():
    long_word_run = "а" * (NOTE_CHUNK_CHARS + 50)
    chunks = prepare(long_word_run, "T")
    assert len(chunks) == 2
    assert len(chunks[0].text) == NOTE_CHUNK_CHARS
    assert len(chunks[1].text) == 50


def test_empty_chunks_are_dropped():
    content = "## Пусто\n\n## Заполнено\nЕсть текст."
    chunks = prepare(content, "T")
    assert len(chunks) == 1
    assert chunks[0].heading == "T › Заполнено"


def test_entirely_empty_note_yields_no_chunks():
    assert prepare("", "T") == []
    assert prepare("   \n\n  ", "T") == []


# --- masking: each secret shape from redact.py and vault/secrets.py ------


def test_aws_key_is_masked():
    chunks = _texts("Ключ: " + "AK" + "IAABCDEFGHIJ1234KL" + ", не теряй.")
    assert "AKIA" not in chunks[0]
    assert MASK in chunks[0]


def test_github_token_is_masked():
    token = "gh" + "p_" + "a" * 36
    chunks = _texts(f"Токен: {token} тут.")
    assert token not in chunks[0]
    assert MASK in chunks[0]


def test_github_pat_is_masked():
    token = "github" + "_pat_" + "a" * 40
    chunks = _texts(f"Новый {token} формат.")
    assert token not in chunks[0]
    assert MASK in chunks[0]


def test_sk_api_key_is_masked():
    key = "sk" + "-" + "x" * 40
    chunks = _texts(f"Ключ {key} openai.")
    assert key not in chunks[0]
    assert MASK in chunks[0]


def test_sk_ant_and_sk_proj_variants_are_masked():
    for key in ("sk" + "-ant-" + "b" * 30, "sk" + "-proj-" + "c" * 30):
        chunks = _texts(f"Ключ {key} .")
        assert key not in chunks[0]
        assert MASK in chunks[0]


def test_slack_token_is_masked():
    # Built from pieces so no literal token shape is committed (GitHub
    # push protection scans for them).
    token = "xo" + "xb-" + "1234567890-1234567890123-" + "abcdefghijklmnop"
    chunks = _texts(f"Slack: {token} готово.")
    assert token not in chunks[0]
    assert MASK in chunks[0]


def test_jwt_is_masked():
    jwt = "ey" + "JhbGciOiJIUzI1NiJ9." + "ey" + "JzdWIiOiIxMjM0NTY3ODkwIn0." + "dQw4w9WgXcQ_examplesig123"
    chunks = _texts(f"Токен сессии: {jwt} истёк.")
    assert jwt not in chunks[0]
    assert MASK in chunks[0]


def test_pem_private_key_block_is_masked_including_multiline_body():
    pem = (
        "-----BEGIN " + "RSA PRIVATE KEY" + "-----\n"
        "MIIBOgIBAAJBAK...\nZmFrZSBrZXkgYm9keQ==\n"
        "-----END " + "RSA PRIVATE KEY" + "-----"
    )
    chunks = _texts(f"Ключ сервера:\n{pem}\nконец.")
    joined = " ".join(chunks)
    assert "BEGIN" not in joined
    assert "MIIBOgIBAAJBAK" not in joined
    assert MASK in joined


def test_card_number_is_masked():
    chunks = _texts("Карта: 4111 1111 1111 1111, храни в секрете.")
    assert "4111" not in chunks[0]
    assert MASK in chunks[0]


def test_email_is_masked():
    chunks = _texts("Пиши на secret.person@example.com по делу.")
    assert "secret.person@example.com" not in chunks[0]
    assert MASK in chunks[0]


def test_iban_is_masked():
    chunks = _texts("IBAN: DE89 3704 0044 0532 0130 00, не публикуй.")
    assert "DE89" not in chunks[0]
    assert MASK in chunks[0]


def test_secret_split_across_a_paragraph_boundary_cannot_escape():
    # Masking runs on the whole note before chunking decides any
    # boundary, so there is no ordinary way to construct this case --
    # this test instead proves the two edges of a long masked run never
    # straddle a hard split by using a run just over the chunk limit
    # that consists *entirely* of one token-shaped secret.
    token = "gh" + "p_" + "a" * (NOTE_CHUNK_CHARS + 10)
    chunks = _texts(token)
    assert len(chunks) == 1
    assert chunks[0] == MASK


# --- ordinary text must not be masked ------------------------------------


def test_ordinary_words_that_resemble_prefixes_are_not_masked():
    content = "Собираю skeleton проекта. Работаю за desk-top-компьютером. sk-8 не ключ."
    chunks = _texts(content)
    assert MASK not in chunks[0]
    assert "skeleton" in chunks[0]
    assert "desk-top" in chunks[0]


def test_ordinary_urls_are_not_masked():
    content = "Читай https://example.com/path/to/page?x=1&y=2 сегодня."
    chunks = _texts(content)
    assert MASK not in chunks[0]


def test_ordinary_year_range_is_not_masked_as_a_card():
    content = "Годы 2020-2024 были насыщенными для проекта."
    chunks = _texts(content)
    assert MASK not in chunks[0]
