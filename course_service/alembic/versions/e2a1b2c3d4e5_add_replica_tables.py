"""replica tables: the copies of other services' data this service keeps

This service no longer calls other services. It keeps small copies of what it
needs (people, courses, who can open what, ...), updated from events. Each table
is only created when it is missing, since startup also creates them.

Revision ID: e2a1b2c3d4e5
Revises: c4d5e6f7a8b9
Create Date: 2026-10-23

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e2a1b2c3d4e5"
down_revision: Union[str, None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "replica_access" not in existing:
        op.create_table(
            "replica_access",
            sa.Column(
                "student_id", sa.Integer(), primary_key=True, autoincrement=False
            ),
            sa.Column("courses", sa.JSON(), nullable=False),
            sa.Column("lessons", sa.JSON(), nullable=False),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )


def downgrade() -> None:
    op.drop_table("replica_access")
