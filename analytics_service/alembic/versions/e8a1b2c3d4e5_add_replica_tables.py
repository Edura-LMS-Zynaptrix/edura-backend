"""replica tables: the copies of other services' data this service keeps

This service no longer calls other services. It keeps small copies of what it
needs (people, courses, who can open what, ...), updated from events. Each table
is only created when it is missing, since startup also creates them.

Revision ID: e8a1b2c3d4e5
Revises: fbe47ba0323e
Create Date: 2026-10-23

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e8a1b2c3d4e5"
down_revision: Union[str, None] = "fbe47ba0323e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:  # noqa: C901 - one step per table
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
    if "replica_attempts" not in existing:
        op.create_table(
            "replica_attempts",
            sa.Column(
                "submission_id", sa.Integer(), primary_key=True, autoincrement=False
            ),
            sa.Column("student_id", sa.Integer(), nullable=False),
            sa.Column("exam_id", sa.Integer(), nullable=False),
            sa.Column("course_id", sa.Integer(), nullable=True),
            sa.Column("module_id", sa.Integer(), nullable=True),
            sa.Column("score", sa.Float(), nullable=False),
            sa.Column("passed", sa.Boolean(), nullable=False),
            sa.Column("marks_obtained", sa.Integer(), nullable=False),
            sa.Column("total_questions", sa.Integer(), nullable=False),
            sa.Column("time_seconds", sa.Integer(), nullable=False),
            sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_replica_attempts_exam_id", "replica_attempts", ["exam_id"])
        op.create_index(
            "ix_replica_attempts_student_exam",
            "replica_attempts",
            ["student_id", "exam_id"],
        )
        op.create_index(
            "ix_replica_attempts_student_id", "replica_attempts", ["student_id"]
        )
    if "replica_badges" not in existing:
        op.create_table(
            "replica_badges",
            sa.Column(
                "student_id", sa.Integer(), primary_key=True, autoincrement=False
            ),
            sa.Column("badge_key", sa.String(length=60), primary_key=True),
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
    if "replica_enrollments" not in existing:
        op.create_table(
            "replica_enrollments",
            sa.Column(
                "student_id", sa.Integer(), primary_key=True, autoincrement=False
            ),
            sa.Column("course_id", sa.Integer(), primary_key=True, autoincrement=False),
            sa.Column("status", sa.String(length=20), nullable=False),
        )
    if "replica_exams" not in existing:
        op.create_table(
            "replica_exams",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
            sa.Column("course_id", sa.Integer(), nullable=False),
            sa.Column("module_id", sa.Integer(), nullable=True),
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("is_published", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_replica_exams_course_id", "replica_exams", ["course_id"])
        op.create_index("ix_replica_exams_module_id", "replica_exams", ["module_id"])
    if "replica_modules" not in existing:
        op.create_table(
            "replica_modules",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
            sa.Column("course_id", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False),
        )
        op.create_index(
            "ix_replica_modules_course_id", "replica_modules", ["course_id"]
        )
    if "replica_payments" not in existing:
        op.create_table(
            "replica_payments",
            sa.Column("order_id", sa.String(length=80), primary_key=True),
            sa.Column("student_id", sa.Integer(), nullable=False),
            sa.Column("course_id", sa.Integer(), nullable=True),
            sa.Column("amount", sa.Numeric(precision=10, scale=2), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "replica_receipts" not in existing:
        op.create_table(
            "replica_receipts",
            sa.Column(
                "receipt_id", sa.Integer(), primary_key=True, autoincrement=False
            ),
            sa.Column("student_id", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
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
    op.drop_table("replica_receipts")
    op.drop_table("replica_payments")
    op.drop_table("replica_modules")
    op.drop_table("replica_exams")
    op.drop_table("replica_enrollments")
    op.drop_table("replica_courses")
    op.drop_table("replica_badges")
    op.drop_table("replica_attempts")
    op.drop_table("replica_access")
