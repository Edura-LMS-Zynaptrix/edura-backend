"""update enrollmentstatus enum values to uppercase

Revision ID: 2c2d3e4f5a6b
Revises: 1b1ca65f6a6c
Create Date: 2026-08-22 10:15:00.000000

"""

from typing import Sequence, Union

from alembic import op

revision: str = "2c2d3e4f5a6b"
down_revision: Union[str, None] = "1b1ca65f6a6c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Update PostgreSQL enum values for enrollmentstatus
    op.execute("ALTER TYPE enrollmentstatus ADD VALUE IF NOT EXISTS 'ACTIVE'")
    op.execute("ALTER TYPE enrollmentstatus ADD VALUE IF NOT EXISTS 'SUSPENDED'")
    op.execute("ALTER TYPE enrollmentstatus ADD VALUE IF NOT EXISTS 'EXPIRED'")
    op.execute("ALTER TYPE enrollmentstatus ADD VALUE IF NOT EXISTS 'CANCELLED'")


def downgrade() -> None:
    pass
