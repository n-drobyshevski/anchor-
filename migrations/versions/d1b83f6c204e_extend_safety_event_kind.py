"""extend ck_safety_event_kind with distill and search

The blind spot H2 built this table to prevent, reopened by phase 4 and
closed here.

`safety_event` exists because a safety-model call can fail in a way that
leaves no trace: a timeout writes no spend_ledger row, an unparseable
reply is a silent no-op, and a check that has stopped working looks
exactly like one with nothing to report. Phase 4 added two more calls
of exactly that shape -- distill (strict JSON, the thing that turns a
page into cards) and search (the `web` plugin) -- and recorded neither,
because `ck_safety_event_kind` admitted only the three H2 knew about.

The consequence was concrete: a distill model that started returning
unparseable JSON produced `done` jobs with zero cards, over and over,
which is indistinguishable from a run of genuinely unhelpful pages.
`study_job.error_code` carried the per-job signal but nothing
aggregated it, so /state could not say "the extractor is fine and the
distiller has failed forty times this week".

docs/decisions.md's 4b entry promised to revisit this in 4c. 4c instead
made it bigger -- /study adds a search plus a second distill per job --
and the revisit is this revision.

Two kinds, not one. A search is a different call with a different
failure mode and folding it into `distill` would make the one number
worth watching unreadable. The outcome vocabulary is unchanged and
needs no widening: a distill that will not parse is `parse_fail`, and a
search that comes back with nothing usable is `error` -- the plugin
did not do its job, which is what that outcome has always meant here.

Nullable-safe and additive: the constraint only widens, so every
existing row stays valid and a downgrade is refused only if rows with
the new kinds exist. The downgrade deletes them rather than failing,
because they are observability rows with no referents -- losing them
costs a week of a rollup nobody had before this revision.

Revision ID: d1b83f6c204e
Revises: c7f2e91a4b58
Create Date: 2026-09-22 22:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd1b83f6c204e'
down_revision: Union[str, Sequence[str], None] = 'c7f2e91a4b58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = "kind in ('welfare', 'extractor', 'tick')"
_NEW = "kind in ('welfare', 'extractor', 'tick', 'distill', 'search')"


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint('ck_safety_event_kind', 'safety_event', type_='check')
    op.create_check_constraint('ck_safety_event_kind', 'safety_event', _NEW)


def downgrade() -> None:
    """Downgrade schema."""
    # The new kinds cannot satisfy the old constraint, so they go first.
    # They are observability rows: nothing references them, and their
    # only value is a rollup that did not exist before this revision.
    op.execute(sa.text("delete from safety_event where kind in ('distill', 'search')"))
    op.drop_constraint('ck_safety_event_kind', 'safety_event', type_='check')
    op.create_check_constraint('ck_safety_event_kind', 'safety_event', _OLD)
