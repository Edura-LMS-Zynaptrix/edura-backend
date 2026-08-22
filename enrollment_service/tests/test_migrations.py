import os

import models  # noqa: F401 — imported so Base.metadata is fully populated
import pytest
from alembic import command
from alembic.config import Config
from database import Base
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.schema import MetaData

DATABASE_URL = os.getenv("DATABASE_URL")


@pytest.fixture(scope="module")
def engine():
    if not DATABASE_URL or not DATABASE_URL.startswith("postgresql"):
        pytest.skip("PostgreSQL DATABASE_URL not set in environment")
    engine = create_engine(DATABASE_URL)
    yield engine
    engine.dispose()


def clean_database(engine):
    # Drop all existing tables to start fresh
    metadata = MetaData()
    metadata.reflect(bind=engine)
    metadata.drop_all(bind=engine)

    # Also drop alembic_version table if it exists
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))

        # Drop all custom PostgreSQL enum types in public schema
        result = conn.execute(
            text(
                "SELECT typname FROM pg_type JOIN pg_namespace ON pg_type.typnamespace = pg_namespace.oid "
                "WHERE typtype = 'e' AND nspname = 'public'"
            )
        )
        enum_names = [row[0] for row in result]
        for enum_name in enum_names:
            conn.execute(text(f"DROP TYPE IF EXISTS {enum_name} CASCADE"))


def test_migration_lifecycle(engine):
    # 1. Clean the database first
    clean_database(engine)

    # 2. Check no tables exist
    inspector = inspect(engine)
    assert len(inspector.get_table_names()) == 0, "Database is not clean"

    # 3. Configure Alembic dynamically
    test_dir = os.path.dirname(os.path.abspath(__file__))
    service_dir = os.path.dirname(test_dir)
    alembic_ini_path = os.path.join(service_dir, "alembic.ini")

    alembic_cfg = Config(alembic_ini_path)
    alembic_cfg.set_main_option("script_location", os.path.join(service_dir, "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", DATABASE_URL)

    # 4. Upgrade to head
    command.upgrade(alembic_cfg, "head")

    # 5. Verify all expected tables exist
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    expected_tables = set(Base.metadata.tables.keys())

    # Check that each expected table is created
    for table in expected_tables:
        assert (
            table in existing_tables
        ), f"Expected table '{table}' was not created by migration"

    # 6. Downgrade to base
    command.downgrade(alembic_cfg, "base")

    # Drop custom PostgreSQL enums after downgrade so the second upgrade doesn't fail on duplicate types
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "SELECT typname FROM pg_type JOIN pg_namespace ON pg_type.typnamespace = pg_namespace.oid "
                "WHERE typtype = 'e' AND nspname = 'public'"
            )
        )
        enum_names = [row[0] for row in result]
        for enum_name in enum_names:
            conn.execute(text(f"DROP TYPE IF EXISTS {enum_name} CASCADE"))

    # 7. Verify all tables are removed (except possibly alembic_version, but usually even alembic_version is empty or gone)
    inspector = inspect(engine)
    remaining_tables = [
        t for t in inspector.get_table_names() if t != "alembic_version"
    ]
    assert (
        len(remaining_tables) == 0
    ), f"Some tables remained after downgrade: {remaining_tables}"

    # 8. Upgrade back to head to verify idempotency
    command.upgrade(alembic_cfg, "head")
    inspector = inspect(engine)
    existing_tables_retry = set(inspector.get_table_names())
    for table in expected_tables:
        assert (
            table in existing_tables_retry
        ), f"Expected table '{table}' was not created on second upgrade"
