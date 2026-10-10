"""add class_packages and class_enrollments

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-10-10

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a2b3c4d5e6f7"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "class_packages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("grade", sa.Integer(), nullable=False),
        sa.Column("stream_id", sa.Integer(), nullable=True),
        sa.Column("price", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_class_packages_grade", "class_packages", ["grade"])
    op.create_index("ix_class_packages_stream_id", "class_packages", ["stream_id"])
    op.create_table(
        "class_enrollments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("class_package_id", sa.Integer(), nullable=False),
        sa.Column("payment_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("enrolled_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "class_package_id", name="uq_class_enrollment_user_package"),
    )
    op.create_index("ix_class_enrollments_user_id", "class_enrollments", ["user_id"])
    op.create_index("ix_class_enrollments_class_package_id", "class_enrollments", ["class_package_id"])


def downgrade() -> None:
    op.drop_index("ix_class_enrollments_class_package_id", table_name="class_enrollments")
    op.drop_index("ix_class_enrollments_user_id", table_name="class_enrollments")
    op.drop_table("class_enrollments")
    op.drop_index("ix_class_packages_stream_id", table_name="class_packages")
    op.drop_index("ix_class_packages_grade", table_name="class_packages")
    op.drop_table("class_packages")
