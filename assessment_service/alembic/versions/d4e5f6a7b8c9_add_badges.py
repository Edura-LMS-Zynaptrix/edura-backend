"""add badges and user_badges

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-10-09

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "badges",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=100), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("criteria_key", sa.String(length=50), nullable=False, unique=True),
        sa.Column("category", sa.String(length=20), nullable=False),
        sa.Column("icon_name", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "user_badges",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("student_id", sa.Integer(), nullable=False),
        sa.Column("badge_id", sa.Integer(), nullable=False),
        sa.Column("awarded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_user_badges_student_id", "user_badges", ["student_id"])
    op.create_index("uq_user_badges_student_badge", "user_badges", ["student_id", "badge_id"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_user_badges_student_badge", table_name="user_badges")
    op.drop_index("ix_user_badges_student_id", table_name="user_badges")
    op.drop_table("user_badges")
    op.drop_table("badges")
