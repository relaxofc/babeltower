"""Add sessions.active_at for durable wall-clock enforcement.

Revision ID: 0004_session_active_at
Revises: 0003_agent_deletion
Create Date: 2026-05-22 00:00:00.000000
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "0004_session_active_at"
down_revision: Union[str, None] = "0003_agent_deletion"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("active_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sessions", "active_at")
