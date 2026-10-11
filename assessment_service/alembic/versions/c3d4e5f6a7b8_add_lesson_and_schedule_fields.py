"""add module_id, position, image and scheduled_start to assessments; explanation to questions

An exam belongs to a lesson (course_service module) and opens at
scheduled_start. These were previously only created by main.py's startup
ALTERs, so a freshly migrated database lacked them.

Revision ID: c3d4e5f6a7b8
Revises: 5736d245a352
Create Date: 2026-10-09

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "5736d245a352"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("assessments", sa.Column("module_id", sa.Integer(), nullable=True))
    op.add_column(
        "assessments", sa.Column("image_url", sa.String(length=512), nullable=True)
    )
    op.add_column(
        "assessments",
        sa.Column("image_public_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "assessments",
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "assessments",
        sa.Column("scheduled_start", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_assessments_module_id", "assessments", ["module_id"])

    op.add_column("questions", sa.Column("explanation", sa.Text(), nullable=True))
    op.add_column(
        "questions",
        sa.Column("question_image_url", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "questions",
        sa.Column("question_image_public_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "questions", sa.Column("option_images_json", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("questions", "option_images_json")
    op.drop_column("questions", "question_image_public_id")
    op.drop_column("questions", "question_image_url")
    op.drop_column("questions", "explanation")
    op.drop_index("ix_assessments_module_id", table_name="assessments")
    op.drop_column("assessments", "scheduled_start")
    op.drop_column("assessments", "position")
    op.drop_column("assessments", "image_public_id")
    op.drop_column("assessments", "image_url")
    op.drop_column("assessments", "module_id")
