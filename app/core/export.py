"""Building the /export file (plan section 11).

Section 11 named nine tables: state, messages, memories, scenes,
check-ins, proposals, journal, state_change, spend_ledger. Later
milestones added `outbound`, `safety_event`, 4a's three study tables
and 8a's `vault_file` and `vault_hold`, and they are here too -- see the comment on EXPORTED_MODELS.

What stays out is transport and queue plumbing -- telegram_update, job,
pending_memory -- whose only real content is message text that
`messages` already carries in full, plus persona_version, which is a
hash of a file in this repo, and the vault's note chunks (8e's
note_chunk_personal and note_chunk_knowledge) and vault_status. Including them would double the file with
Telegram's own envelope format and make it harder to read, not more
complete. tests/test_export.py keeps that list honest: a table
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
    BriefNote,
    Checkin,
    CheckinOrderResult,
    IdleChange,
    IdleRun,
    InterestTopic,
    Journal,
    NotebookEntry,
    Obligation,
    Outbound,
    Memory,
    Message,
    PersonaAmendment,
    PlannerAction,
    PlannerSnapshot,
    Proposal,
    ReviewProposal,
    SafetyEvent,
    Scene,
    SpendLedger,
    StandingOrder,
    StateChange,
    StudyCard,
    StudyClip,
    StudyJob,
    UserState,
    WeeklyReview,
    VaultFile,
    VaultHold,
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
    # 5b: Anchor's own working notes (phase-5 plan section 3). User data
    # by the same reasoning as everything above it -- the user's own
    # `/mind add` intentions live here, and so does whatever Anchor
    # wrote about them.
    NotebookEntry,
    # 5c: negotiated standing orders (phase-5 plan section 3) and their
    # check-in results. User data by the same reasoning again -- these
    # are commitments the user negotiated or authored themselves, plus
    # their own daily answers about them.
    StandingOrder,
    CheckinOrderResult,
    # Phase 5 (spec 2026-09-25): the debt queue. User data: what the
    # user owes, in their own or /due's words.
    Obligation,
    # 5d: the weekly review and persona amendments (phase-5 plan
    # sections 3, 8 and 9). weekly_review.analysis is the validated
    # summary of the user's own week; review_proposal is what it
    # suggested and how the user answered; persona_amendment is what the
    # user adopted and its (pass/fail only, no model text) eval_report.
    WeeklyReview,
    ReviewProposal,
    PersonaAmendment,
    # 6a: the idle framework (Phase 6 plan section 3, "/export covers
    # idle_run (metadata), idle_change, brief_note, and interest_topic").
    # backup_log is deliberately not here -- it names ciphertext object
    # keys, not user data, and the plan says so outright ("It doesn't
    # need backup_log"); tests/test_export.py's NOT_EXPORTED carries the
    # reason.
    IdleRun,
    IdleChange,
    BriefNote,
    InterestTopic,
    # P2: the cached agenda and (from P3) pending planner writes are user
    # data by the same reasoning as `outbound` and `safety_event` just
    # above. `planner_credential` is deliberately **not** here -- it
    # holds a live access/refresh token pair, and "give me all my data"
    # must never be the thing that puts a usable credential in a
    # downloadable file (design review section 3.2 item 6's "no tokens
    # exported", plan section 4's file-list note).
    PlannerSnapshot,
    PlannerAction,
    # 8a (phase-8 plan section 10): which files in the vault were
    # Anchor's and what state each is in, and any change still waiting
    # for a yes; from 8e a note's row carries its class. The two note
    # chunk tables (8e: derived copies of the user's own notes,
    # rebuildable from the vault) and vault_status (operational
    # timestamps) are the deliberate omissions, named in
    # tests/test_export.py.
    VaultFile,
    VaultHold,
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
