"""interest_suggest_batch and interest_suggestion: `/interests suggest`

The model may propose research topics; only a tap on a button creates an
`interest_topic` row (anchor-interest-suggestions-plan.md sections 1, 4
and 5a). Everything the feature keeps is here:

- `interest_suggest_batch`: one row per run of the suggestion call.
  `origin` is `manual` (a `/interests suggest`) or `weekly` (the idle
  kind `interest_suggest`); a manual run counts against
  `INTEREST_SUGGEST_PER_DAY` by `local_date` and `origin`, a weekly one
  does not, and either pushes the next weekly batch back. `status` is
  `queued` -> `done` | `failed`; `error_code` is a code from the
  research errors, never a message. `usd_cost` is what the call cost
  (also in `spend_ledger` under `interest_suggest`). `epoch` is six
  random base32 characters, the lens garden's idea: it rides every
  button's callback data, so a tap on an older batch's message is
  refused as stale. `message_id` and `sent_at` record the batch's one
  Telegram message -- a done batch with `sent_at` null has not gone out
  yet (quiet hours, a pause), and the send hook delivers it later; a
  message id without a `sent_at` is not a thing (`ck_..._sent`).
- `interest_suggestion`: one row per proposed topic. `batch_id` is
  CASCADE; `position` is the number the message shows, unique within the
  batch. `text` (at most 100, as `interest_topic.text`), `packet` and
  `why` (at most 160) are the model's words, validated in code before
  they are stored. `status` is `open` -> `added` | `dismissed` on a tap,
  or `open` -> `expired` when a newer batch is made (or an unsent batch
  goes stale), so old buttons go stale with no sweep. `topic_id` is the
  topic a tap created, SET NULL (topics are retired, never deleted, but
  an FK that blocks a delete would be the wrong default); it is only set
  on an `added` suggestion.

**Debug views** (granted to `anchor_debug`, as 9e4b2c7a1f05 explains),
content-free columns only: `debug.interest_suggest_batch` has ids,
origin, status, error code, local date, cost and times -- not the epoch
(a callback token) or the Telegram message id. `debug.interest_suggestion`
has ids, position, status and times. The text, packet and why are never
in a view: what the model proposed is a guess at what the user cares
about.

`idle_run.kind` gains `interest_suggest`, the weekly batch's idle kind
(app/core/idle/interest_suggest.py); `ck_idle_run_kind` still equals
app/core/idle's `KINDS`.

Indexes: the quota and the weekly gate count batches by day and origin;
the send hook looks for the newest done batch not yet sent on every
heartbeat tick, which a partial index keeps to one cheap probe; the
suggestion's status index serves "expire every open suggestion" and
"the last dismissed ones".

Reversible. Downgrade deletes the `interest_suggest` idle runs and
restores `ck_idle_run_kind`, then drops the views, then both tables (the
suggestions first).

Revision ID: a5d1c8e3f702
Revises: c2e7a9f4b1d6
Create Date: 2026-10-24 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a5d1c8e3f702'
down_revision: Union[str, Sequence[str], None] = 'c2e7a9f4b1d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEBUG_ROLE = 'anchor_debug'

IDLE_KINDS_BEFORE = (
    "'backfill', 'consolidate', 'reflect', 'prebrief', 'critique', 'lens_garden', "
    "'lens_research', 'research', 'canary'"
)
IDLE_KINDS_AFTER = (
    "'backfill', 'consolidate', 'reflect', 'prebrief', 'critique', 'lens_garden', "
    "'lens_research', 'interest_suggest', 'research', 'canary'"
)

# Explicit column lists, never `*` (9e4b2c7a1f05's rule).
DEBUG_VIEWS: dict[str, str] = {
    'interest_suggest_batch': (
        'id, origin, status, error_code, local_date, usd_cost, created_at, finished_at, sent_at'
    ),
    'interest_suggestion': 'id, batch_id, position, status, created_at, decided_at',
}


def _grant_debug(views: Sequence[str]) -> None:
    grants = '\n'.join(f'GRANT SELECT ON debug.{name} TO {DEBUG_ROLE};' for name in views)
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{DEBUG_ROLE}') THEN
                {grants}
            END IF;
        END
        $$
    """)


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(name, table, type_='check')
    op.create_check_constraint(name, table, condition)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'interest_suggest_batch',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('origin', sa.String(), server_default=sa.text("'manual'"), nullable=False),
        sa.Column('status', sa.String(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column('error_code', sa.String(), nullable=True),
        sa.Column('local_date', sa.Date(), nullable=False),
        sa.Column(
            'usd_cost', sa.Numeric(precision=10, scale=6), server_default=sa.text('0'), nullable=False
        ),
        sa.Column('epoch', sa.String(), nullable=False),
        sa.Column('message_id', sa.BigInteger(), nullable=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "origin in ('manual', 'weekly')", name='ck_interest_suggest_batch_origin'
        ),
        sa.CheckConstraint(
            "status in ('queued', 'done', 'failed')", name='ck_interest_suggest_batch_status'
        ),
        sa.CheckConstraint("epoch ~ '^[a-z2-7]{6}$'", name='ck_interest_suggest_batch_epoch'),
        sa.CheckConstraint(
            'message_id is null or sent_at is not null', name='ck_interest_suggest_batch_sent'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_interest_suggest_batch_local_date_origin',
        'interest_suggest_batch',
        ['local_date', 'origin'],
    )
    op.create_index(
        'ix_interest_suggest_batch_unsent',
        'interest_suggest_batch',
        ['created_at'],
        postgresql_where=sa.text("status = 'done' and sent_at is null"),
    )

    op.create_table(
        'interest_suggestion',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('batch_id', sa.Integer(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('text', sa.String(), nullable=False),
        sa.Column('packet', sa.String(), nullable=False),
        sa.Column('why', sa.String(), nullable=False),
        sa.Column('status', sa.String(), server_default=sa.text("'open'"), nullable=False),
        sa.Column('topic_id', sa.BigInteger(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint('position >= 0', name='ck_interest_suggestion_position'),
        sa.CheckConstraint(
            'char_length("text") <= 100', name='ck_interest_suggestion_text_length'
        ),
        sa.CheckConstraint('char_length(why) <= 160', name='ck_interest_suggestion_why_length'),
        sa.CheckConstraint(
            "packet in ('forums', 'guides', 'ref')", name='ck_interest_suggestion_packet'
        ),
        sa.CheckConstraint(
            "status in ('open', 'added', 'dismissed', 'expired')",
            name='ck_interest_suggestion_status',
        ),
        sa.CheckConstraint(
            "status <> 'open' or decided_at is null", name='ck_interest_suggestion_decided'
        ),
        sa.CheckConstraint(
            "topic_id is null or status = 'added'", name='ck_interest_suggestion_topic'
        ),
        sa.ForeignKeyConstraint(['batch_id'], ['interest_suggest_batch.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['topic_id'], ['interest_topic.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('batch_id', 'position', name='uq_interest_suggestion_position'),
    )
    op.create_index(
        'ix_interest_suggestion_status_batch_id', 'interest_suggestion', ['status', 'batch_id']
    )

    for name, columns in DEBUG_VIEWS.items():
        op.execute(f'CREATE VIEW debug.{name} AS SELECT {columns} FROM public.{name}')
    _grant_debug(list(DEBUG_VIEWS))

    _replace_check('idle_run', 'ck_idle_run_kind', f'kind in ({IDLE_KINDS_AFTER})')


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DELETE FROM idle_run WHERE kind = 'interest_suggest'")
    _replace_check('idle_run', 'ck_idle_run_kind', f'kind in ({IDLE_KINDS_BEFORE})')

    for name in DEBUG_VIEWS:
        op.execute(f'DROP VIEW IF EXISTS debug.{name}')
    op.drop_index('ix_interest_suggestion_status_batch_id', table_name='interest_suggestion')
    op.drop_table('interest_suggestion')
    op.drop_index('ix_interest_suggest_batch_unsent', table_name='interest_suggest_batch')
    op.drop_index(
        'ix_interest_suggest_batch_local_date_origin', table_name='interest_suggest_batch'
    )
    op.drop_table('interest_suggest_batch')
