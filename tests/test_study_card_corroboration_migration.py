"""d4f8a2c6e1b3 adds the cross-check verdict to `study_card`.

The upgrade adds three nullable columns (existing cards read as never
checked) and appends the verdict and its page's id to
`debug.study_card`, never the quote; the constraints tie a quote to a
verdict that needs one. The downgrade puts the view and the table back.
Runs on its own throwaway database, like the other migration tests.
"""

from __future__ import annotations

import pytest

from tests.test_vault_notes_migration import _alembic, _run, scratch_database  # noqa: F401

BEFORE = "a7c3e1f9d2b4"
AFTER = "d4f8a2c6e1b3"
SEED = (
    "INSERT INTO study_job (id, kind, status, local_date) VALUES (1, 'read', 'done', '2026-10-18')",
    "INSERT INTO study_clip (id, job_id, url, domain) VALUES (1, 1, 'https://example.com/a', 'example.com')",
    "INSERT INTO study_card (id, job_id, clip_id, kind, text, quote, source_url, risk_model, "
    "risk_rules, risk_final) VALUES (1, 1, 1, 'definition', 'Тезис.', 'a quote of the page', "
    "'https://example.com/a', 'low', 'low', 'low')"
)


def _columns(url: str, schema: str) -> list[str]:
    rows = _run(
        url,
        "SELECT column_name FROM information_schema.columns "
        f"WHERE table_schema = '{schema}' AND table_name = 'study_card' ORDER BY ordinal_position",
    )
    return [r["column_name"] for r in rows]


def test_cards_read_as_unchecked_the_view_gains_the_verdict_and_it_downgrades(scratch_database):  # noqa: F811
    _alembic(scratch_database, "upgrade", BEFORE)
    _run(scratch_database, *SEED)
    _alembic(scratch_database, "upgrade", AFTER)
    rows = _run(scratch_database, "SELECT corroboration, corroboration_quote FROM study_card")
    assert [(r["corroboration"], r["corroboration_quote"]) for r in rows] == [(None, None)]
    assert _columns(scratch_database, "debug")[-2:] == ["corroboration", "corroboration_clip_id"]
    assert "corroboration_quote" not in _columns(scratch_database, "debug")

    _run(
        scratch_database,
        "UPDATE study_card SET corroboration = 'supported', corroboration_clip_id = 1, "
        "corroboration_quote = 'a quote of the other page'",
    )
    for bad in (
        "UPDATE study_card SET corroboration = 'supported', corroboration_quote = NULL",
        "UPDATE study_card SET corroboration = 'unverified'",
        "UPDATE study_card SET corroboration = 'maybe', corroboration_quote = NULL",
        "UPDATE study_card SET corroboration = NULL",
    ):
        with pytest.raises(Exception, match="ck_study_card_corroboration"):
            _run(scratch_database, bad)

    _alembic(scratch_database, "downgrade", BEFORE)
    assert "corroboration" not in _columns(scratch_database, "public")
    assert "corroboration" not in _columns(scratch_database, "debug")
    assert _run(scratch_database, "SELECT count(*) AS n FROM study_card")[0]["n"] == 1
    _alembic(scratch_database, "upgrade", "head")
