"""referral wallet columns, referral_events and wallet_transactions

Revision ID: e5f6a7b8c9d0
Revises: d3c4c6beef21
Create Date: 2026-10-09

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, None] = "d3c4c6beef21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for column in (
        "wallet_credits",
        "total_credits_earned",
        "total_successful_referrals",
    ):
        op.add_column(
            "user_profiles",
            sa.Column(column, sa.Integer(), nullable=False, server_default="0"),
        )
    op.create_table(
        "referral_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("referrer_user_id", sa.Integer(), nullable=False),
        sa.Column("referred_user_id", sa.Integer(), nullable=False, unique=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("credit_amount", sa.Integer(), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("qualified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("credited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_referral_events_referrer_user_id", "referral_events", ["referrer_user_id"]
    )
    op.create_table(
        "wallet_transactions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("transaction_type", sa.String(length=30), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column("reference_id", sa.Integer(), nullable=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_wallet_transactions_user_id", "wallet_transactions", ["user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_wallet_transactions_user_id", table_name="wallet_transactions")
    op.drop_table("wallet_transactions")
    op.drop_index("ix_referral_events_referrer_user_id", table_name="referral_events")
    op.drop_table("referral_events")
    for column in (
        "total_successful_referrals",
        "total_credits_earned",
        "wallet_credits",
    ):
        op.drop_column("user_profiles", column)
