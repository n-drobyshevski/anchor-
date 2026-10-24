"""a5d1c8e3f702 creates `interest_suggest_batch` and `interest_suggestion`.

The upgrade makes both tables with their CHECKs, the cascade from batch
to suggestion, the SET NULL from topic to suggestion, and two debug
views whose columns hide everything the model wrote (text, packet, why)
and the callback epoch; and `ck_idle_run_kind` gains `interest_suggest`,
the weekly batch's idle kind. The downgrade deletes those idle runs,
restores the check, drops views and tables and leaves `interest_topic`
alone. Runs on its own throwaway database, like the
other migration tests.
"""

from __future__ import annotations

import pytest

from tests.test_vault_notes_migration import _alembic, _run, scratch_database  # noqa: F401

BEFORE = "c2e7a9f4b1d6"
AFTER = "a5d1c8e3f702"


def _columns(url: str, schema: str, table: str) -> list[str]:
    rows = _run(
        url,
        "SELECT column_name FROM information_schema.columns "
        f"WHERE table_schema = '{schema}' AND table_name = '{table}' ORDER BY ordinal_position",
    )
    return [r["column_name"] for r in rows]


def _batch(url: str) -> int:
    rows = _run(
        url,
        "INSERT INTO interest_suggest_batch (local_date, epoch) "
        "VALUES ('2026-10-24', 'abcdef') RETURNING id",
    )
    return rows[0]["id"]


def _suggestion(url: str, batch_id: int, position: int) -> None:
    _run(
        url,
        "INSERT INTO interest_suggestion (batch_id, position, text, packet, why) "
        f"VALUES ({batch_id}, {position}, 'бессонница', 'forums', 'рядом с темой')",
    )


def test_the_tables_their_constraints_and_the_content_free_views(scratch_database):  # noqa: F811
    _alembic(scratch_database, "upgrade", BEFORE)
    _alembic(scratch_database, "upgrade", AFTER)

    batch = _batch(scratch_database)
    row = _run(scratch_database, f"SELECT origin, status, usd_cost FROM interest_suggest_batch WHERE id = {batch}")[0]
    assert (row["origin"], row["status"], float(row["usd_cost"])) == ("manual", "queued", 0.0)

    for bad in ("origin = 'daily'", "status = 'sent'", "epoch = 'ABCDEF'", "epoch = 'abc'"):
        with pytest.raises(Exception, match="ck_interest_suggest_batch"):
            _run(scratch_database, f"UPDATE interest_suggest_batch SET {bad} WHERE id = {batch}")
    # A message id needs the time it was sent.
    with pytest.raises(Exception, match="ck_interest_suggest_batch_sent"):
        _run(scratch_database, f"UPDATE interest_suggest_batch SET message_id = 7 WHERE id = {batch}")
    _run(
        scratch_database,
        f"UPDATE interest_suggest_batch SET message_id = 7, sent_at = now(), origin = 'weekly' "
        f"WHERE id = {batch}",
    )

    _suggestion(scratch_database, batch, 1)
    with pytest.raises(Exception, match="uq_interest_suggestion_position"):
        _suggestion(scratch_database, batch, 1)
    with pytest.raises(Exception, match="ck_interest_suggestion_packet"):
        _run(
            scratch_database,
            "INSERT INTO interest_suggestion (batch_id, position, text, packet, why) "
            f"VALUES ({batch}, 2, 'тема', 'lens', 'почему')",
        )
    with pytest.raises(Exception, match="ck_interest_suggestion_text_length"):
        _run(
            scratch_database,
            "INSERT INTO interest_suggestion (batch_id, position, text, packet, why) "
            f"VALUES ({batch}, 2, '{'я' * 101}', 'ref', 'почему')",
        )
    with pytest.raises(Exception, match="ck_interest_suggestion_why_length"):
        _run(
            scratch_database,
            "INSERT INTO interest_suggestion (batch_id, position, text, packet, why) "
            f"VALUES ({batch}, 2, 'тема', 'ref', '{'я' * 161}')",
        )
    with pytest.raises(Exception, match="ck_interest_suggestion_status"):
        _run(scratch_database, f"UPDATE interest_suggestion SET status = 'maybe' WHERE batch_id = {batch}")
    # An open suggestion is not decided; only an added one has a topic.
    with pytest.raises(Exception, match="ck_interest_suggestion_decided"):
        _run(scratch_database, f"UPDATE interest_suggestion SET decided_at = now() WHERE batch_id = {batch}")
    topic = _run(
        scratch_database,
        "INSERT INTO interest_topic (text, packet) VALUES ('бессонница', 'forums') RETURNING id",
    )[0]["id"]
    with pytest.raises(Exception, match="ck_interest_suggestion_topic"):
        _run(scratch_database, f"UPDATE interest_suggestion SET topic_id = {topic} WHERE batch_id = {batch}")
    _run(
        scratch_database,
        f"UPDATE interest_suggestion SET status = 'added', decided_at = now(), topic_id = {topic} "
        f"WHERE batch_id = {batch}",
    )

    # The views carry counts and states, never what the model wrote.
    assert _columns(scratch_database, "debug", "interest_suggest_batch") == [
        "id", "origin", "status", "error_code", "local_date", "usd_cost", "created_at",
        "finished_at", "sent_at",
    ]
    assert _columns(scratch_database, "debug", "interest_suggestion") == [
        "id", "batch_id", "position", "status", "created_at", "decided_at",
    ]

    # A retired topic's row is never deleted, but if it were, the
    # suggestion keeps its text and loses the pointer.
    _run(scratch_database, f"DELETE FROM interest_topic WHERE id = {topic}")
    assert _run(scratch_database, "SELECT topic_id FROM interest_suggestion")[0]["topic_id"] is None
    # Deleting a batch takes its suggestions.
    _run(scratch_database, f"DELETE FROM interest_suggest_batch WHERE id = {batch}")
    assert _run(scratch_database, "SELECT count(*) AS n FROM interest_suggestion")[0]["n"] == 0


def test_the_downgrade_drops_the_tables_and_views_and_upgrades_again(scratch_database):  # noqa: F811
    _alembic(scratch_database, "upgrade", AFTER)
    _run(scratch_database, "INSERT INTO interest_topic (text, packet) VALUES ('сон', 'ref')")
    _alembic(scratch_database, "downgrade", BEFORE)
    for schema, table in (
        ("public", "interest_suggest_batch"),
        ("public", "interest_suggestion"),
        ("debug", "interest_suggest_batch"),
        ("debug", "interest_suggestion"),
    ):
        assert _columns(scratch_database, schema, table) == []
    assert _run(scratch_database, "SELECT count(*) AS n FROM interest_topic")[0]["n"] == 1
    _alembic(scratch_database, "upgrade", "head")


IDLE_RUN = "INSERT INTO idle_run (kind, local_date) VALUES ('{kind}', '2026-10-24')"


def test_the_idle_kind_joins_the_check_and_leaves_with_the_downgrade(scratch_database):  # noqa: F811
    _alembic(scratch_database, "upgrade", BEFORE)
    with pytest.raises(Exception, match="ck_idle_run_kind"):
        _run(scratch_database, IDLE_RUN.format(kind="interest_suggest"))

    _alembic(scratch_database, "upgrade", AFTER)
    _run(scratch_database, IDLE_RUN.format(kind="interest_suggest"))
    _run(scratch_database, IDLE_RUN.format(kind="lens_garden"))
    with pytest.raises(Exception, match="ck_idle_run_kind"):
        _run(scratch_database, IDLE_RUN.format(kind="interest_suggestions"))

    _alembic(scratch_database, "downgrade", BEFORE)
    kinds = _run(scratch_database, "SELECT kind FROM idle_run ORDER BY id")
    assert [row["kind"] for row in kinds] == ["lens_garden"]
    with pytest.raises(Exception, match="ck_idle_run_kind"):
        _run(scratch_database, IDLE_RUN.format(kind="interest_suggest"))
