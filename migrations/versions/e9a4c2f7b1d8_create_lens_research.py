"""lens research (L4): gap-seeded study jobs, lens cards, Echo's inbox
ledger, and the `lens_research` idle kind

anchor-lens-plan.md sections 9, 13 and 14.5, milestone L4, as the L4
spec settles it with the owner's amendments ((a) `PACKET_LENS` leaves
out archive.org; (b) a research's result is its own Telegram message,
sent as soon as the job finishes, not a section of the next garden
message). A tap on «исследовать» under a garden gap queues one research
of the web for it; the idle kind `lens_research` runs it; its cards come
back in their own message, and «в Inbox» writes them into the vault as
one knowledge note, through vaultd, undoable for 14 days.

- `study_job.lens_gap_id` (SET NULL, unique while set: one research per
  gap) and the check that only a `kind='study'`, `packet='lens'` job
  carries one; `study_job.offered_at`, when the result message went out
  (the cards expire from it).
- `study_card.kind` gains `lens`; `study_card.echo_changeset_id` points
  an adopted lens card at the inbox write, so
  `ck_study_card_adopted_has_memory` accepts that instead of a memory,
  and `ck_study_card_lens_target` pins that a lens card never has a
  memory and only a lens card has an inbox write.
- `lens_gap.research_requested_at`, set by the tap and never cleared
  (each gap is researched at most once), and
  `lens_gap.research_message_id`, the result message whose buttons act
  on the gap.
- `echo_changeset`: ids and times only (vaultd's changeset id, the gap,
  the job, the card ids, created/confirmed/undone) -- no path, name,
  text or hash, like `claude_changeset`. At most one unconfirmed row
  per gap.
- `idle_run.kind` gains `lens_research`.

**No debug view changes** (the L4 spec section 6): no new `debug.*`
column and no `echo_changeset` view, as for `claude_changeset`. A debug
`lens_gap_id` would tell `researched` apart from `resolved`, which L3
deliberately merged into `closed`; `lens.gaps()` keeps mapping
`researched` to `closed`.

Reversible. Downgrade deletes the lens research jobs (their clips and
cards cascade), `lens_research` idle runs and the ledger, moves any
`researched` gap back to `open`, and restores the constraints.

Revision ID: e9a4c2f7b1d8
Revises: b3e9f5a1c7d2
Create Date: 2026-09-30 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e9a4c2f7b1d8'
down_revision: Union[str, Sequence[str], None] = 'b3e9f5a1c7d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

IDLE_KINDS_BEFORE = (
    "'backfill', 'consolidate', 'reflect', 'prebrief', 'critique', 'lens_garden', "
    "'research', 'canary'"
)
IDLE_KINDS_AFTER = (
    "'backfill', 'consolidate', 'reflect', 'prebrief', 'critique', 'lens_garden', "
    "'lens_research', 'research', 'canary'"
)
CARD_KINDS_BEFORE = "kind in ('technique', 'routine', 'checkin_format', 'definition')"
CARD_KINDS_AFTER = "kind in ('technique', 'routine', 'checkin_format', 'definition', 'lens')"
ADOPTED_BEFORE = "status <> 'adopted' or memory_id is not null"
ADOPTED_AFTER = (
    "status <> 'adopted' or memory_id is not null "
    "or (kind = 'lens' and echo_changeset_id is not null)"
)
LENS_TARGET = (
    "(kind = 'lens' and memory_id is null) or (kind <> 'lens' and echo_changeset_id is null)"
)


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(name, table, type_='check')
    op.create_check_constraint(name, table, condition)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'echo_changeset',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('vault_ref', sa.String(), nullable=False),
        sa.Column('lens_gap_id', sa.Integer(), nullable=True),
        sa.Column('study_job_id', sa.BigInteger(), nullable=True),
        sa.Column(
            'card_ids',
            postgresql.ARRAY(sa.BigInteger()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('undone_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "vault_ref ~ '^[A-Za-z0-9_-]{1,64}$'", name='ck_echo_changeset_vault_ref'
        ),
        sa.CheckConstraint(
            'undone_at is null or confirmed_at is not null', name='ck_echo_changeset_undone'
        ),
        sa.ForeignKeyConstraint(['lens_gap_id'], ['lens_gap.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['study_job_id'], ['study_job.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('vault_ref', name='uq_echo_changeset_vault_ref'),
    )
    op.create_index(
        'ux_echo_changeset_open_gap',
        'echo_changeset',
        ['lens_gap_id'],
        unique=True,
        postgresql_where=sa.text('confirmed_at is null'),
    )

    op.add_column('study_job', sa.Column('lens_gap_id', sa.Integer(), nullable=True))
    op.add_column('study_job', sa.Column('offered_at', sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        'fk_study_job_lens_gap_id', 'study_job', 'lens_gap', ['lens_gap_id'], ['id'],
        ondelete='SET NULL',
    )
    op.create_check_constraint(
        'ck_study_job_lens', 'study_job',
        "lens_gap_id is null or (kind = 'study' and packet = 'lens')",
    )
    op.create_index(
        'ux_study_job_lens_gap_id',
        'study_job',
        ['lens_gap_id'],
        unique=True,
        postgresql_where=sa.text('lens_gap_id is not null'),
    )

    op.add_column('study_card', sa.Column('echo_changeset_id', sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        'fk_study_card_echo_changeset_id', 'study_card', 'echo_changeset',
        ['echo_changeset_id'], ['id'],
    )
    _replace_check('study_card', 'ck_study_card_kind', CARD_KINDS_AFTER)
    _replace_check('study_card', 'ck_study_card_adopted_has_memory', ADOPTED_AFTER)
    op.create_check_constraint('ck_study_card_lens_target', 'study_card', LENS_TARGET)

    op.add_column(
        'lens_gap', sa.Column('research_requested_at', sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column('lens_gap', sa.Column('research_message_id', sa.BigInteger(), nullable=True))

    _replace_check('idle_run', 'ck_idle_run_kind', f'kind in ({IDLE_KINDS_AFTER})')


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DELETE FROM idle_run WHERE kind = 'lens_research'")
    _replace_check('idle_run', 'ck_idle_run_kind', f'kind in ({IDLE_KINDS_BEFORE})')

    op.execute("UPDATE lens_gap SET status = 'open' WHERE status = 'researched'")
    op.drop_column('lens_gap', 'research_message_id')
    op.drop_column('lens_gap', 'research_requested_at')

    # The lens jobs go whole: their clips and cards cascade, and nothing
    # before L4 can read a `packet='lens'` job.
    op.execute("DELETE FROM study_job WHERE packet = 'lens' OR lens_gap_id IS NOT NULL")
    op.execute("DELETE FROM study_card WHERE kind = 'lens'")
    op.drop_constraint('ck_study_card_lens_target', 'study_card', type_='check')
    _replace_check('study_card', 'ck_study_card_adopted_has_memory', ADOPTED_BEFORE)
    _replace_check('study_card', 'ck_study_card_kind', CARD_KINDS_BEFORE)
    op.drop_constraint('fk_study_card_echo_changeset_id', 'study_card', type_='foreignkey')
    op.drop_column('study_card', 'echo_changeset_id')

    op.drop_index('ux_study_job_lens_gap_id', table_name='study_job')
    op.drop_constraint('ck_study_job_lens', 'study_job', type_='check')
    op.drop_constraint('fk_study_job_lens_gap_id', 'study_job', type_='foreignkey')
    op.drop_column('study_job', 'offered_at')
    op.drop_column('study_job', 'lens_gap_id')

    op.drop_index('ux_echo_changeset_open_gap', table_name='echo_changeset')
    op.drop_table('echo_changeset')
