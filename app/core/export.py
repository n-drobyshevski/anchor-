"""Building the /export file (plan section 11).

Section 11 named nine tables: state, messages, memories, scenes,
check-ins, proposals, journal, state_change, spend_ledger. Later
milestones added `outbound`, `safety_event` and 4a's three study
tables, and they are here too -- see the comment on EXPORTED_MODELS.

What stays out is transport and queue plumbing -- telegram_update, job,
pending_memory -- whose only real content is message text that
`messages` already carries in full, plus persona_version, which is a
hash of a file in this repo. Including them would double the file with
Telegram's own envelope format and make it harder to read, not more
complete. tests/test_export.py keeps that list of four honest: a table
is exported or it is named there, and nothing may be neither.

**Nothing here is ever logged.** The caller records byte counts and row
counts; the rows themselves go into the file and nowhere else.
"""

from __future__ import annotations

import datetime
import decimal
import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clock as clock_module
from app.core.clock import Clock
from app.db.models import (
    Checkin,
    Journal,
    Outbound,
    Memory,
    Message,
    Proposal,
    SafetyEvent,
    Scene,
    SpendLedger,
    StateChange,
    StudyCard,
    StudyClip,
    StudyJob,
    UserState,
)

logger = logging.getLogger(__name__)

# Plan section 11's list, in the order it gives them.
EXPORTED_MODELS = (
    UserState,
    Message,
    Memory,
    Scene,
    Checkin,
    Proposal,
    Journal,
    StateChange,
    SpendLedger,
    # 4a: the research loop (phase-4 plan section 4, which says /export
    # includes all three). Unlike telegram_update and job, these are not
    # plumbing whose content another table already carries: study_clip
    # holds page text that exists nowhere else, and study_card holds the
    # proposals the user accepted or refused.
    # 3a and H2. Both were purged by /delete from the day they were
    # added and neither reached /export, because EXPORTED_MODELS was
    # only ever checked against itself -- see
    # tests/test_export.py::test_every_table_is_either_exported_or_
    # deliberately_omitted, the 4a test that found this.
    #
    # Both are user data by the repo's own reasoning: purge.py calls
    # `outbound` "a record of what the bot said to this user and when"
    # and `safety_event` "a record of when this user was talked to".
    # "Give me all my data" is the mirror of "delete all my data", so a
    # table cannot be user data for one and plumbing for the other.
    # `outbound` also holds what `message` cannot: the proactive
    # messages that were planned and then skipped or cancelled.
    Outbound,
    SafetyEvent,
    StudyJob,
    StudyClip,
    StudyCard,
)

FILENAME_TEMPLATE = "anchor-export-{date}.json"


def encode(value):
    """JSON-encode a column value that json.dumps cannot take directly.

    `Decimal` becomes a **string**, not a float. usd_cost is
    Numeric(10, 6), and putting it through a float would silently change
    the number in a file whose whole purpose is to be an accurate
    record -- 0.000108 is exactly representable as a decimal string and
    is not as a float.

    Datetimes and dates go out as ISO-8601. The codebase has no existing
    convention to match (app/log.py uses json.dumps(default=str), which
    renders a datetime space-separated rather than with a T), so this
    sets one.
    """
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    return value


def _row_to_dict(row) -> dict:
    return {
        column.name: encode(getattr(row, column.name))
        for column in row.__table__.columns
    }


async def build_export(session: AsyncSession, clock: Clock) -> dict:
    """Every row of the nine exported tables, keyed by table name."""
    tables: dict[str, list[dict]] = {}
    for model in EXPORTED_MODELS:
        result = await session.execute(select(model).order_by(*model.__table__.primary_key))
        tables[model.__tablename__] = [_row_to_dict(row) for row in result.scalars().all()]
    return {
        "exported_at": clock.now_utc().isoformat(),
        "tables": tables,
    }


def to_bytes(payload: dict) -> bytes:
    """Serialize the export.

    ensure_ascii=False so Russian reads as Russian rather than as a wall
    of \\uXXXX escapes -- this file is meant to be opened and read, not
    only re-imported.
    """
    return json.dumps(payload, ensure_ascii=False, indent=2, default=encode).encode("utf-8")


def export_filename(clock: Clock, timezone: str) -> str:
    """`anchor-export-YYYYMMDD.json` on the user's local date (plan section 11)."""
    return FILENAME_TEMPLATE.format(
        date=clock_module.local_date(clock, timezone).strftime("%Y%m%d")
    )


def row_counts(payload: dict) -> dict[str, int]:
    """Per-table row counts -- the only thing about an export that is safe to log."""
    return {name: len(rows) for name, rows in payload["tables"].items()}
