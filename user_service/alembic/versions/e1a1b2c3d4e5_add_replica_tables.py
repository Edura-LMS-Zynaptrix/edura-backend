"""replica tables: the copies of other services' data this service keeps

This service no longer calls other services. It keeps small copies of what it
needs (people, courses, who can open what, ...), updated from events. Each table
is only created when it is missing, since startup also creates them.

Revision ID: e1a1b2c3d4e5
Revises: f6a7b8c9d0e1
Create Date: 2026-10-23

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e1a1b2c3d4e5"
down_revision: Union[str, None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "replica_accounts" not in existing:
        op.create_table(
            "replica_accounts",
            sa.Column("user_id", sa.Integer(), primary_key=True, autoincrement=False),
            sa.Column("role", sa.String(length=20), nullable=True),
            sa.Column("email", sa.String(length=255), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("is_email_verified", sa.Boolean(), nullable=False),
            sa.Column("account_created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )


def downgrade() -> None:
    op.drop_table("replica_accounts")
