"""create study_job, study_clip, study_card

Phase 4 milestone 4a. The three tables the gated research loop writes
(phase-4 plan section 4). Nothing reads or writes them yet: 4a ships the
schema and the fetcher, 4b ships /read and the cards, and the whole
feature stays behind RESEARCH_ENABLED=false until 4d.

Created empty and in one revision because they are one structure: a card
without its clip has no source to point at, and a clip without its job
has no quota to count against. Both foreign keys cascade on delete, so
deleting a job takes its clips and cards with it -- which is what
/delete's purge and a cancelled job both want, and the alternative
(orphan rows referencing a job id that no longer exists) is a shape
nothing in the plan has a use for.

Three risk columns on study_card rather than one, because they answer
different questions: what the distill model claimed, what the code rules
found, and the max of the two that actually governs. Keeping the model's
claim is what makes "the rules caught something the model missed"
distinguishable from "both agreed" after the fact -- the same reason H4
added spend_ledger.cost_source.

Two constraints go beyond the plan's SQL, both stating an invariant from
plan section 12 that the schema is able to state on its own:

- ck_study_card_high_is_hidden: a risk_final='high' card must be
  status='hidden'. Section 12 says a high card is never shown and never
  adoptable; with this constraint a bug in app/research/ cannot leave
  one in a status that /notes would list.
- ck_study_card_adopted_has_memory: an adopted card must carry the
  memory id it wrote. Adoption's entire visible effect is that memory
  row, so an adopted card without one is a silent failure.

rule_hits is text[] holding rule ids only. Never the matched text: that
text comes from a fetched page, and /export dumps this table.

Reversible. A downgrade drops all three tables and everything in them,
which for this feature is the correct loss -- pending cards are
proposals the user has not accepted, and there is nowhere else to put
them.

Revision ID: c7f2e91a4b58
Revises: b2d5f8a13c47
Create Date: 2026-09-22 19:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c7f2e91a4b58'
down_revision: Union[str, Sequence[str], None] = 'b2d5f8a13c47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'study_job',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('packet', sa.String(), nullable=True),
        sa.Column('query', sa.String(), nullable=True),
        sa.Column('status', sa.String(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column('searches_used', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('pins_used', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column(
            'usd_cost', sa.Numeric(precision=10, scale=6), server_default=sa.text('0'), nullable=False
        ),
        sa.Column('error_code', sa.String(), nullable=True),
        sa.Column('local_date', sa.Date(), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind in ('study', 'read')", name='ck_study_job_kind'),
        sa.CheckConstraint(
            "status in ('queued', 'searching', 'fetching', 'distilling', "
            "'done', 'failed', 'cancelled')",
            name='ck_study_job_status',
        ),
        sa.CheckConstraint('char_length(query) <= 200', name='ck_study_job_query_length'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_study_job_local_date', 'study_job', ['local_date'], unique=False)

    op.create_table(
        'study_clip',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('job_id', sa.BigInteger(), nullable=False),
        sa.Column('url', sa.String(), nullable=False),
        sa.Column('domain', sa.String(), nullable=False),
        sa.Column('title', sa.String(), nullable=True),
        sa.Column('text', sa.String(), nullable=True),
        sa.Column('text_sha256', sa.String(), nullable=True),
        sa.Column('http_status', sa.Integer(), nullable=True),
        sa.Column('fetch_error', sa.String(), nullable=True),
        sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint('char_length(title) <= 300', name='ck_study_clip_title_length'),
        sa.ForeignKeyConstraint(['job_id'], ['study_job.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_study_clip_url_fetched_at', 'study_clip', ['url', 'fetched_at'], unique=False
    )
    op.create_index('ix_study_clip_job_id', 'study_clip', ['job_id'], unique=False)

    op.create_table(
        'study_card',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('job_id', sa.BigInteger(), nullable=False),
        sa.Column('clip_id', sa.BigInteger(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('text', sa.String(), nullable=False),
        sa.Column('quote', sa.String(), nullable=False),
        sa.Column('source_url', sa.String(), nullable=False),
        sa.Column('risk_model', sa.String(), nullable=False),
        sa.Column('risk_rules', sa.String(), nullable=False),
        sa.Column('risk_final', sa.String(), nullable=False),
        sa.Column(
            'rule_hits',
            postgresql.ARRAY(sa.String()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column('status', sa.String(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column('memory_id', sa.BigInteger(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind in ('technique', 'routine', 'checkin_format', 'definition')",
            name='ck_study_card_kind',
        ),
        sa.CheckConstraint('char_length("text") <= 300', name='ck_study_card_text_length'),
        sa.CheckConstraint('char_length(quote) <= 240', name='ck_study_card_quote_length'),
        sa.CheckConstraint(
            "risk_model in ('low', 'medium', 'high')", name='ck_study_card_risk_model'
        ),
        sa.CheckConstraint(
            "risk_rules in ('low', 'medium', 'high')", name='ck_study_card_risk_rules'
        ),
        sa.CheckConstraint(
            "risk_final in ('low', 'medium', 'high')", name='ck_study_card_risk_final'
        ),
        sa.CheckConstraint(
            "status in ('pending', 'adopted', 'rejected', 'hidden', 'expired')",
            name='ck_study_card_status',
        ),
        sa.CheckConstraint(
            "risk_final <> 'high' or status = 'hidden'", name='ck_study_card_high_is_hidden'
        ),
        sa.CheckConstraint(
            'status <> \'adopted\' or memory_id is not null',
            name='ck_study_card_adopted_has_memory',
        ),
        sa.ForeignKeyConstraint(['clip_id'], ['study_clip.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['job_id'], ['study_job.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['memory_id'], ['memory.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_study_card_status_created_at', 'study_card', ['status', 'created_at'], unique=False
    )
    op.create_index('ix_study_card_job_id', 'study_card', ['job_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_study_card_job_id', table_name='study_card')
    op.drop_index('ix_study_card_status_created_at', table_name='study_card')
    op.drop_table('study_card')
    op.drop_index('ix_study_clip_job_id', table_name='study_clip')
    op.drop_index('ix_study_clip_url_fetched_at', table_name='study_clip')
    op.drop_table('study_clip')
    op.drop_index('ix_study_job_local_date', table_name='study_job')
    op.drop_table('study_job')
