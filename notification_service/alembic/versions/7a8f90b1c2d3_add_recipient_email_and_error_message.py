"""add recipient_email and error_message to notification_logs

Revision ID: 7a8f90b1c2d3
Revises: e51ca3e470a6
Create Date: 2026-08-24 18:15:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7a8f90b1c2d3"
down_revision: Union[str, None] = "e51ca3e470a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "notification_logs",
        sa.Column("recipient_email", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "notification_logs",
        sa.Column("error_message", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("notification_logs", "error_message")
    op.drop_column("notification_logs", "recipient_email")
