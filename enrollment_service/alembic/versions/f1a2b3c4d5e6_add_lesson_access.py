"""add enrollments.is_manual and lesson_access

Revision ID: f1a2b3c4d5e6
Revises: 2c2d3e4f5a6b
Create Date: 2026-10-10

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "2c2d3e4f5a6b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "enrollments",
        sa.Column("is_manual", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "lesson_access",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("student_id", sa.Integer(), nullable=False),
        sa.Column("course_id", sa.Integer(), nullable=False),
        sa.Column("module_id", sa.Integer(), nullable=False),
        sa.Column("granted_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("student_id", "module_id", name="uq_lesson_access_student_module"),
    )
    op.create_index("ix_lesson_access_student_id", "lesson_access", ["student_id"])
    op.create_index("ix_lesson_access_course_id", "lesson_access", ["course_id"])


def downgrade() -> None:
    op.drop_index("ix_lesson_access_course_id", table_name="lesson_access")
    op.drop_index("ix_lesson_access_student_id", table_name="lesson_access")
    op.drop_table("lesson_access")
    op.drop_column("enrollments", "is_manual")
