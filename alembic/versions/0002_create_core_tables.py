"""create core tables

Revision ID: 0002_create_core_tables
Revises: 0001_enable_vector_extension
Create Date: 2026-05-21 00:01:00
"""

import sqlalchemy as sa
from pgvector.sqlalchemy import HALFVEC
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0002_create_core_tables"
down_revision = "0001_enable_vector_extension"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("pubkey", sa.String(length=128), nullable=False),
        sa.Column("github_user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("soft_ban_lifts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("intents_created_total", sa.Integer(), nullable=False),
        sa.Column("connection_requests_sent_total", sa.Integer(), nullable=False),
        sa.Column("connection_requests_received_total", sa.Integer(), nullable=False),
        sa.Column("connection_requests_accepted_total", sa.Integer(), nullable=False),
        sa.Column("sessions_started_total", sa.Integer(), nullable=False),
        sa.Column("matches_confirmed_total", sa.Integer(), nullable=False),
        sa.Column("blocks_received_total", sa.Integer(), nullable=False),
    )
    op.create_index("ix_agents_pubkey", "agents", ["pubkey"], unique=True)
    op.create_index("ix_agents_github_user_id", "agents", ["github_user_id"])
    op.create_index("ix_agents_status", "agents", ["status"])

    op.create_table(
        "intents",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "agent_id",
            sa.String(length=32),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("match_type", sa.String(length=64), nullable=False),
        sa.Column("seeking", sa.Text(), nullable=False),
        sa.Column("offering", sa.Text(), nullable=False),
        sa.Column("constraints", sa.String(length=500), nullable=False),
        sa.Column("filters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("embedding", HALFVEC(1024), nullable=False),
        sa.Column("ttl_days", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
    )
    op.create_index("ix_intents_agent_id", "intents", ["agent_id"])
    op.create_index("ix_intents_match_type", "intents", ["match_type"])
    op.create_index("ix_intents_created_at", "intents", ["created_at"])
    op.create_index("ix_intents_expires_at", "intents", ["expires_at"])
    op.create_index("ix_intents_status", "intents", ["status"])
    op.create_index(
        "intents_active_match_type_idx",
        "intents",
        ["match_type"],
        postgresql_where=sa.text("status = 'active'"),
    )
    op.execute(
        """
        CREATE INDEX intents_embedding_hnsw_idx ON intents
        USING hnsw (embedding halfvec_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        """
    )

    op.create_table(
        "connection_requests",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "from_agent_id",
            sa.String(length=32),
            sa.ForeignKey("agents.id"),
            nullable=False,
        ),
        sa.Column("to_agent_id", sa.String(length=32), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column(
            "target_intent_id",
            sa.String(length=32),
            sa.ForeignKey("intents.id"),
            nullable=False,
        ),
        sa.Column(
            "from_intent_id",
            sa.String(length=32),
            sa.ForeignKey("intents.id"),
            nullable=False,
        ),
        sa.Column("opening_message", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.String(length=200), nullable=True),
    )
    op.create_index(
        "ix_connection_requests_from_agent_id",
        "connection_requests",
        ["from_agent_id"],
    )
    op.create_index("ix_connection_requests_to_agent_id", "connection_requests", ["to_agent_id"])
    op.create_index(
        "ix_connection_requests_target_intent_id",
        "connection_requests",
        ["target_intent_id"],
    )
    op.create_index(
        "ix_connection_requests_from_intent_id",
        "connection_requests",
        ["from_intent_id"],
    )
    op.create_index("ix_connection_requests_status", "connection_requests", ["status"])
    op.create_index("ix_connection_requests_created_at", "connection_requests", ["created_at"])
    op.create_index("ix_connection_requests_expires_at", "connection_requests", ["expires_at"])

    op.create_table(
        "sessions",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("agent_a_id", sa.String(length=32), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("agent_b_id", sa.String(length=32), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column(
            "connection_request_id",
            sa.String(length=32),
            sa.ForeignKey("connection_requests.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.String(length=64), nullable=True),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.Column(
            "match_proposed_by_id",
            sa.String(length=32),
            sa.ForeignKey("agents.id"),
            nullable=True,
        ),
        sa.Column("match_proposed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("match_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_sessions_agent_a_id", "sessions", ["agent_a_id"])
    op.create_index("ix_sessions_agent_b_id", "sessions", ["agent_b_id"])
    op.create_index("ix_sessions_status", "sessions", ["status"])
    op.create_index("ix_sessions_created_at", "sessions", ["created_at"])
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])

    op.create_table(
        "blocks",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "blocker_id",
            sa.String(length=32),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "blocked_id",
            sa.String(length=32),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reason", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("blocker_id", "blocked_id", name="uq_blocks_pair"),
    )
    op.create_index("ix_blocks_blocker_id", "blocks", ["blocker_id"])
    op.create_index("ix_blocks_blocked_id", "blocks", ["blocked_id"])

    op.create_table(
        "abuse_events",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "agent_id",
            sa.String(length=32),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_abuse_events_agent_id", "abuse_events", ["agent_id"])
    op.create_index("ix_abuse_events_event_type", "abuse_events", ["event_type"])
    op.create_index("ix_abuse_events_created_at", "abuse_events", ["created_at"])


def downgrade() -> None:
    op.drop_table("abuse_events")
    op.drop_table("blocks")
    op.drop_table("sessions")
    op.drop_table("connection_requests")
    op.execute("DROP INDEX IF EXISTS intents_embedding_hnsw_idx")
    op.drop_table("intents")
    op.drop_table("agents")
