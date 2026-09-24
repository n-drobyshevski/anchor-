"""create debug schema: content-free views and the anchor_debug role

Debugging production (is the queue stuck? what did yesterday cost? how
many turns went out?) should not require reading the conversation. This
adds a `debug` schema of views over the operational columns only --
ids, timestamps, statuses, counters, tokens, cost, error codes -- and a
role, `anchor_debug`, that can read those views and nothing else.

No view exposes a free-text column: message content, update payloads,
scene summaries, memory/journal/check-in text, proposal values,
state_change values, tick notes, research queries and page text are
all left out. Where the *size* of a text is diagnostic it appears as a
length, never the text itself.

The views run with their owner's privileges (the Postgres default, not
security_invoker), so `anchor_debug` needs no grant on any `public`
table -- and has none: `select content from public.message` is a
permission error for it, which is the whole point.

The role is created NOLOGIN and with no password. Nothing secret lives
in the repo; enabling it is a manual step on the production database
(docs/claude-access.md). Roles are cluster-wide, so creation is
idempotent, and skipped with a NOTICE when the migrating user may not
create roles.

Reversible. Downgrade drops the schema; the role is dropped only when
it owns nothing else and no other database still grants to it.

Revision ID: 9e4b2c7a1f05
Revises: d1b83f6c204e
Create Date: 2026-09-24 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '9e4b2c7a1f05'
down_revision: Union[str, Sequence[str], None] = 'd1b83f6c204e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEBUG_ROLE = 'anchor_debug'

# view name -> select list over the same-named public table. Explicit
# column lists, never `*`: a text column added to a table later must not
# leak into its view by default.
DEBUG_VIEWS: dict[str, str] = {
    'telegram_update': 'update_id, status, attempts, locked_at, error, created_at',
    'job': 'id, kind, dedup_key, run_after, status, attempts, locked_at, error, created_at',
    'scene': (
        'id, started_at, ended_at, message_count, '
        'summary is not null as has_summary'
    ),
    'message': (
        'id, role, kind, ooc, scene_id, update_id, reply_to_update, outbound_id, '
        'model, tokens_in, tokens_cached, tokens_out, usd_cost, created_at, sent_at, '
        'char_length(content) as content_len'
    ),
    'user_state': (
        'id, persona_active, intensity, timezone, updated_at, focus_on, focus_since, '
        'due_action is not null as has_due_action, due_set_at, streak, last_checkin_at, '
        'awaiting, awaiting_ref, quiet_until, last_user_msg_at, last_outbound_at, '
        'ignored_in_row, welfare_at'
    ),
    'state_change': 'id, field, source, created_at',
    'spend_ledger': (
        'id, ts, local_date, category, model, tokens_in, tokens_cached, tokens_out, '
        'usd_cost, cost_source'
    ),
    'safety_event': 'id, ts, local_date, kind, outcome, model',
    'memory': (
        'id, kind, pinned, source, confidence, superseded_by, last_used_at, use_count, '
        'created_at, char_length(text) as text_len'
    ),
    'pending_memory': 'id, created_at, char_length(text) as text_len',
    'journal': 'id, local_date, created_at, char_length(text) as text_len',
    'proposal': 'id, field, status, tg_message_id, created_at, decided_at',
    'checkin': (
        'id, local_date, day_rating, due_result, note is not null as has_note, '
        'tg_message_id, created_at'
    ),
    'outbound': (
        'id, kind, local_date, bucket, planned_for, status, skip_reason, message_id, '
        'created_at, sent_at'
    ),
    'study_job': (
        'id, kind, status, searches_used, pins_used, usd_cost, error_code, local_date, '
        'created_at, finished_at'
    ),
    'study_clip': (
        'id, job_id, domain, http_status, fetch_error, fetched_at, '
        'char_length(text) as text_len'
    ),
    'study_card': (
        'id, job_id, clip_id, kind, risk_model, risk_rules, risk_final, rule_hits, '
        'status, memory_id, created_at, decided_at'
    ),
}


def upgrade() -> None:
    """Upgrade schema."""
    op.execute('CREATE SCHEMA debug')
    for name, columns in DEBUG_VIEWS.items():
        op.execute(f'CREATE VIEW debug.{name} AS SELECT {columns} FROM public.{name}')

    op.execute(f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{DEBUG_ROLE}') THEN
                IF (SELECT rolsuper OR rolcreaterole FROM pg_roles
                    WHERE rolname = current_user) THEN
                    CREATE ROLE {DEBUG_ROLE} NOLOGIN;
                ELSE
                    RAISE NOTICE 'cannot create role {DEBUG_ROLE}; create it by hand';
                END IF;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{DEBUG_ROLE}') THEN
                EXECUTE format('GRANT CONNECT ON DATABASE %I TO {DEBUG_ROLE}',
                               current_database());
                GRANT USAGE ON SCHEMA debug TO {DEBUG_ROLE};
                GRANT SELECT ON ALL TABLES IN SCHEMA debug TO {DEBUG_ROLE};
            END IF;
        END
        $$
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute('DROP SCHEMA debug CASCADE')
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{DEBUG_ROLE}') THEN
                EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM {DEBUG_ROLE}',
                               current_database());
                BEGIN
                    DROP ROLE {DEBUG_ROLE};
                EXCEPTION WHEN dependent_objects_still_exist THEN
                    RAISE NOTICE 'role {DEBUG_ROLE} still used elsewhere; kept';
                END;
            END IF;
        END
        $$
    """)
