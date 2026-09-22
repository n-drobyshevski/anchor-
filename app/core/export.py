"""Building the /export file (plan section 11).

Nine tables, named by section 11: state, messages, memories, scenes,
check-ins, proposals, journal, state_change, spend_ledger. The three the
database also holds -- telegram_update, job, pending_memory -- are
transport and queue plumbing, and their only real content is message
text that `messages` already carries in full. Including them would
double the file with Telegram's own envelope format and make it harder
to read, not more complete.

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

from app.core.spend import local_date_for
from app.db.models import (
    Checkin,
    Journal,
    Memory,
    Message,
    Proposal,
    Scene,
    SpendLedger,
    StateChange,
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


async def build_export(session: AsyncSession) -> dict:
    """Every row of the nine exported tables, keyed by table name."""
    tables: dict[str, list[dict]] = {}
    for model in EXPORTED_MODELS:
        result = await session.execute(select(model).order_by(*model.__table__.primary_key))
        tables[model.__tablename__] = [_row_to_dict(row) for row in result.scalars().all()]
    return {
        "exported_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "tables": tables,
    }


def to_bytes(payload: dict) -> bytes:
    """Serialize the export.

    ensure_ascii=False so Russian reads as Russian rather than as a wall
    of \\uXXXX escapes -- this file is meant to be opened and read, not
    only re-imported.
    """
    return json.dumps(payload, ensure_ascii=False, indent=2, default=encode).encode("utf-8")


def export_filename(timezone: str) -> str:
    """`anchor-export-YYYYMMDD.json` on the user's local date (plan section 11)."""
    return FILENAME_TEMPLATE.format(date=local_date_for(timezone).strftime("%Y%m%d"))


def row_counts(payload: dict) -> dict[str, int]:
    """Per-table row counts -- the only thing about an export that is safe to log."""
    return {name: len(rows) for name, rows in payload["tables"].items()}
