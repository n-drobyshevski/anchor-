"""c2e7a9f4b1d6 adds `study_job.manual`, the flag `/interests run` sets.

The upgrade adds a not-null boolean that defaults to false (every job
that exists reads as not manual, so the shared /study quota counts them
as before), a CHECK tying the flag to `kind = 'study'`, and appends the
flag to `debug.study_job`; the view still hides `packet` and `query`.
The downgrade puts the view and the table back. Runs on its own
throwaway database, like the other migration tests.
"""

from __future__ import annotations

import pytest

from tests.test_vault_notes_migration import _alembic, _run, scratch_database  # noqa: F401

BEFORE = "d4f8a2c6e1b3"
AFTER = "c2e7a9f4b1d6"
SEED = (
    (
        "INSERT INTO study_job (id, kind, status, local_date, packet, query) "
        "VALUES (1, 'study', 'done', '2026-10-18', 'forums', 'бессонница')"
    ),
    "INSERT INTO study_job (id, kind, status, local_date) VALUES (2, 'read', 'done', '2026-10-18')",
)


def _columns(url: str, schema: str) -> list[str]:
    rows = _run(
        url,
        "SELECT column_name FROM information_schema.columns "
        f"WHERE table_schema = '{schema}' AND table_name = 'study_job' ORDER BY ordinal_position",
    )
    return [r["column_name"] for r in rows]


def test_jobs_read_as_not_manual_the_view_gains_the_flag_and_it_downgrades(scratch_database):  # noqa: F811
    _alembic(scratch_database, "upgrade", BEFORE)
    _run(scratch_database, *SEED)
    _alembic(scratch_database, "upgrade", AFTER)
    rows = _run(scratch_database, "SELECT id, manual FROM study_job ORDER BY id")
    assert [(r["id"], r["manual"]) for r in rows] == [(1, False), (2, False)]
    assert _columns(scratch_database, "debug")[-1] == "manual"
    assert not {"packet", "query"} & set(_columns(scratch_database, "debug"))

    # A study job can be manual; a read job cannot.
    _run(scratch_database, "UPDATE study_job SET manual = true WHERE id = 1")
    with pytest.raises(Exception, match="ck_study_job_manual"):
        _run(scratch_database, "UPDATE study_job SET manual = true WHERE id = 2")
    with pytest.raises(Exception, match="ck_study_job_manual"):
        _run(
            scratch_database,
            "INSERT INTO study_job (kind, status, local_date, manual) "
            "VALUES ('read', 'queued', '2026-10-18', true)",
        )

    _alembic(scratch_database, "downgrade", BEFORE)
    assert "manual" not in _columns(scratch_database, "public")
    assert "manual" not in _columns(scratch_database, "debug")
    assert _run(scratch_database, "SELECT count(*) AS n FROM study_job")[0]["n"] == 2
    _alembic(scratch_database, "upgrade", "head")
