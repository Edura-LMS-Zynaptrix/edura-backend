"""add materials table, lesson-group fields, and the columns previously added at startup

Brings the Alembic chain level with models.py: the public_id, grade and
stream_ids columns were only ever created by main.py's startup ALTERs.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-10-09

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "courses",
        sa.Column("thumbnail_public_id", sa.String(length=255), nullable=True),
    )
    op.add_column("courses", sa.Column("grade", sa.Integer(), nullable=True))
    op.add_column("courses", sa.Column("stream_ids", sa.JSON(), nullable=True))

    op.add_column(
        "modules", sa.Column("image_url", sa.String(length=512), nullable=True)
    )
    op.add_column(
        "modules", sa.Column("image_public_id", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "modules",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )

    op.add_column("lessons", sa.Column("description", sa.Text(), nullable=True))
    op.add_column(
        "lessons",
        sa.Column("cloudinary_public_id", sa.String(length=255), nullable=True),
    )

    op.create_table(
        "materials",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("course_id", sa.Integer(), nullable=False),
        sa.Column("module_id", sa.Integer(), nullable=False),
        sa.Column("lesson_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("file_url", sa.String(length=512), nullable=False),
        sa.Column("file_type", sa.String(length=50), nullable=False),
        sa.Column("file_public_id", sa.String(length=255), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_materials_module_id", "materials", ["module_id"])
    op.create_index("ix_materials_lesson_id", "materials", ["lesson_id"])


def downgrade() -> None:
    op.drop_index("ix_materials_lesson_id", table_name="materials")
    op.drop_index("ix_materials_module_id", table_name="materials")
    op.drop_table("materials")

    op.drop_column("lessons", "cloudinary_public_id")
    op.drop_column("lessons", "description")

    op.drop_column("modules", "is_active")
    op.drop_column("modules", "image_public_id")
    op.drop_column("modules", "image_url")

    op.drop_column("courses", "stream_ids")
    op.drop_column("courses", "grade")
    op.drop_column("courses", "thumbnail_public_id")
