"""add video_checkpoints and checkpoint_questions

Revision ID: c4d5e6f7a8b9
Revises: b2c3d4e5f6a7
Create Date: 2026-10-10

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "video_checkpoints",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("course_id", sa.Integer(), nullable=False),
        sa.Column("lesson_id", sa.Integer(), nullable=False),
        sa.Column("timestamp_seconds", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_video_checkpoints_lesson_id", "video_checkpoints", ["lesson_id"])
    op.create_table(
        "checkpoint_questions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("checkpoint_id", sa.Integer(), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("option_a", sa.String(length=500), nullable=False),
        sa.Column("option_b", sa.String(length=500), nullable=False),
        sa.Column("option_c", sa.String(length=500), nullable=True),
        sa.Column("option_d", sa.String(length=500), nullable=True),
        sa.Column("option_e", sa.String(length=500), nullable=True),
        sa.Column("correct_option", sa.String(length=1), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("order_number", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_checkpoint_questions_checkpoint_id", "checkpoint_questions", ["checkpoint_id"])


def downgrade() -> None:
    op.drop_index("ix_checkpoint_questions_checkpoint_id", table_name="checkpoint_questions")
    op.drop_table("checkpoint_questions")
    op.drop_index("ix_video_checkpoints_lesson_id", table_name="video_checkpoints")
    op.drop_table("video_checkpoints")
