"""Exercise the new data migration against legacy records without PostgreSQL."""

from datetime import datetime
import io
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import Column, CheckConstraint, DateTime, MetaData, String, Table, Text, Uuid, create_engine, select


def test_new_migration_preserves_legacy_pending_and_execution_outcomes(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    metadata = MetaData()
    legacy = Table(
        "test_runs", metadata,
        Column("id", Uuid(), primary_key=True), Column("status", String(32), nullable=False),
        Column("created_at", DateTime(), nullable=False), Column("updated_at", DateTime(), nullable=False),
        Column("error_details", Text),
        CheckConstraint("status IN ('PENDING', 'PASSED', 'TEST_FAILED', 'INFRASTRUCTURE_ERROR')", name="ck_test_runs_status"),
    )
    metadata.create_all(engine)
    identifiers = {status: uuid4() for status in ("PENDING", "PASSED", "TEST_FAILED", "INFRASTRUCTURE_ERROR")}
    try:
        with engine.begin() as connection:
            connection.execute(legacy.insert(), [
                {"id": identifier, "status": status, "created_at": datetime(2026, 1, 1), "updated_at": datetime(2026, 1, 2)}
                for status, identifier in identifiers.items()
            ])
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.stamp(config, "0001_create_test_runs")
            command.upgrade(config, "head")
            upgraded = Table("test_runs", MetaData(), autoload_with=connection)
            rows = {UUID(str(row.id)): row for row in connection.execute(select(upgraded))}
            assert rows[identifiers["PENDING"]].status == "PENDING"
            assert rows[identifiers["PENDING"]].execution_outcome is None
            assert rows[identifiers["PASSED"]].status == "COMPLETED"
            assert rows[identifiers["PASSED"]].execution_outcome == "PASSED"
            assert rows[identifiers["TEST_FAILED"]].status == "COMPLETED"
            assert rows[identifiers["INFRASTRUCTURE_ERROR"]].status == "FAILED"
            assert rows[identifiers["PASSED"]].warnings == []
            command.downgrade(config, "0001_create_test_runs")
            restored = Table("test_runs", MetaData(), autoload_with=connection)
            assert {row.status for row in connection.execute(select(restored))} == set(identifiers)
    finally:
        engine.dispose()


def test_postgresql_lifecycle_upgrade_sql_is_generated_without_network(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:placeholder@localhost/testpilot")
    output = io.StringIO()
    config = Config("alembic.ini", output_buffer=output)
    command.upgrade(config, "head", sql=True)
    sql = output.getvalue()
    assert "ALTER TABLE test_runs ADD COLUMN execution_outcome" in sql
    assert "UPDATE test_runs SET execution_outcome = status" in sql
    assert "'RUNNING', 'COMPLETED', 'FAILED'" in sql
