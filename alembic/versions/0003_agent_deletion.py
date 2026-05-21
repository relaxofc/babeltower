"""Allow deleted agents to disassociate GitHub user IDs.

Revision ID: 0003_agent_deletion
Revises: 0002_create_core_tables
Create Date: 2026-05-21 00:00:00.000000
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "0003_agent_deletion"
down_revision: Union[str, None] = "0002_create_core_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "agents",
        "github_user_id",
        existing_type=sa.BigInteger(),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "agents",
        "github_user_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
