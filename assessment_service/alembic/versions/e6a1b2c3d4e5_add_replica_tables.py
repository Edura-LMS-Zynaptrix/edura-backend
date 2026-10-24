"""replica tables: the copies of other services' data this service keeps

This service no longer calls other services. It keeps small copies of what it
needs (people, courses, who can open what, ...), updated from events. Each table
is only created when it is missing, since startup also creates them.

Revision ID: e6a1b2c3d4e5
Revises: d4e5f6a7b8c9
Create Date: 2026-10-23

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e6a1b2c3d4e5"
down_revision: Union[str, None] = "d4e5f6a7b8c9"
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
    if "replica_courses" not in existing:
        op.create_table(
            "replica_courses",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column("grade", sa.Integer(), nullable=True),
            sa.Column("stream_ids", sa.JSON(), nullable=True),
            sa.Column("course_type", sa.String(length=20), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=True),
            sa.Column("price", sa.Numeric(precision=10, scale=2), nullable=False),
            sa.Column("instructor_id", sa.Integer(), nullable=True),
            sa.Column("thumbnail_url", sa.String(length=512), nullable=True),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
    if "replica_users" not in existing:
        op.create_table(
            "replica_users",
            sa.Column("user_id", sa.Integer(), primary_key=True, autoincrement=False),
            sa.Column("role", sa.String(length=20), nullable=True),
            sa.Column("email", sa.String(length=255), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("is_email_verified", sa.Boolean(), nullable=False),
            sa.Column("account_created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("first_name", sa.String(length=100), nullable=True),
            sa.Column("last_name", sa.String(length=100), nullable=True),
            sa.Column("full_name", sa.String(length=210), nullable=True),
            sa.Column("mobile_no", sa.String(length=20), nullable=True),
            sa.Column("school", sa.String(length=200), nullable=True),
            sa.Column("district", sa.String(length=100), nullable=True),
            sa.Column("grade", sa.Integer(), nullable=True),
            sa.Column("stream_id", sa.Integer(), nullable=True),
            sa.Column("avatar_url", sa.String(length=512), nullable=True),
            sa.Column("profile_created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )


def downgrade() -> None:
    op.drop_table("replica_users")
    op.drop_table("replica_courses")
    op.drop_table("replica_access")
