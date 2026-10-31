"""student profile columns, streams and grade_subjects

The student registration fields, the referral code and the stream/subject
catalogues were added to the models without a revision. Each step checks what
already exists first: service startup also adds these, so a database may have
them before this revision runs.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-10-12

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PROFILE_COLUMNS = (
    ("avatar_public_id", sa.String(length=255)),
    ("school", sa.String(length=200)),
    ("district", sa.String(length=100)),
    ("grade", sa.Integer()),
    ("stream_id", sa.Integer()),
    ("nic_number", sa.String(length=20)),
    ("selected_subjects", sa.JSON()),
    ("referral_code", sa.String(length=20)),
    ("referred_by_user_id", sa.Integer()),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    existing = {c["name"] for c in inspector.get_columns("user_profiles")}
    for name, column_type in PROFILE_COLUMNS:
        if name not in existing:
            op.add_column("user_profiles", sa.Column(name, column_type, nullable=True))
    has_unique = any(
        u["column_names"] == ["referral_code"]
        for u in inspector.get_unique_constraints("user_profiles")
    ) or any(
        i["column_names"] == ["referral_code"] and i.get("unique")
        for i in inspector.get_indexes("user_profiles")
    )
    if not has_unique:
        op.create_unique_constraint(
            "uq_user_profiles_referral_code", "user_profiles", ["referral_code"]
        )

    tables = set(inspector.get_table_names())
    if "streams" not in tables:
        op.create_table(
            "streams",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("subjects", sa.JSON(), nullable=False),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_index("ix_streams_name", "streams", ["name"], unique=True)
    if "grade_subjects" not in tables:
        op.create_table(
            "grade_subjects",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("grade", sa.Integer(), nullable=False),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint("name", "grade", name="uq_grade_subjects_name_grade"),
        )
        op.create_index("ix_grade_subjects_grade", "grade_subjects", ["grade"])


def downgrade() -> None:
    op.drop_index("ix_grade_subjects_grade", table_name="grade_subjects")
    op.drop_table("grade_subjects")
    op.drop_index("ix_streams_name", table_name="streams")
    op.drop_table("streams")
    op.drop_constraint(
        "uq_user_profiles_referral_code", "user_profiles", type_="unique"
    )
    for name, _ in reversed(PROFILE_COLUMNS):
        op.drop_column("user_profiles", name)
