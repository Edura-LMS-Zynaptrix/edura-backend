"""add status enum to courses and youtube/cloudinary fields to lessons

Revision ID: a1b2c3d4e5f6
Revises: cdc605ff547e
Create Date: 2026-06-28

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "cdc605ff547e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

coursestatusenum = sa.Enum("DRAFT", "PUBLISHED", "ARCHIVED", name="coursestatusenum")


def upgrade() -> None:
    coursestatusenum.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "courses",
        sa.Column(
            "status",
            sa.Enum("DRAFT", "PUBLISHED", "ARCHIVED", name="coursestatusenum"),
            nullable=False,
            server_default="DRAFT",
        ),
    )
    # Backfill: rows published before this migration
    op.execute("UPDATE courses SET status = 'PUBLISHED' WHERE is_published = TRUE")

    op.add_column(
        "lessons", sa.Column("youtube_video_id", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "lessons",
        sa.Column("cloudinary_asset_url", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("lessons", "cloudinary_asset_url")
    op.drop_column("lessons", "youtube_video_id")
    op.drop_column("courses", "status")
    coursestatusenum.drop(op.get_bind(), checkfirst=True)
