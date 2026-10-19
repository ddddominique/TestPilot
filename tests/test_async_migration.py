"""Upgrade/downgrade Phase 4.2 records without requiring a database server."""

from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint, Column, DateTime, MetaData, String, Table, Text, Uuid, create_engine, select


def test_async_migration_preserves_records_and_downgrade_does_not_requeue(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    metadata = MetaData()
    legacy = Table(
        "test_runs", metadata, Column("id", Uuid(), primary_key=True),
        Column("status", String(32), nullable=False), Column("execution_outcome", String(32)),
        Column("completed_at", DateTime(timezone=True)), Column("error_details", Text),
        CheckConstraint("status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED')", name="ck_test_runs_status"),
    )
    metadata.create_all(engine)
    pending, running, completed = uuid4(), uuid4(), uuid4()
    try:
        with engine.begin() as connection:
            connection.execute(legacy.insert(), [
                {"id": pending, "status": "PENDING", "execution_outcome": None},
                {"id": running, "status": "RUNNING", "execution_outcome": None},
                {"id": completed, "status": "COMPLETED", "execution_outcome": "TEST_FAILED"},
            ])
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.stamp(config, "0002_run_lifecycle")
            command.upgrade(config, "head")
            upgraded = Table("test_runs", MetaData(), autoload_with=connection)
            rows = {UUID(str(row.id)): row for row in connection.execute(select(upgraded))}
            assert rows[pending].status == "PENDING" and rows[pending].task_id is None
            assert rows[running].status == "RUNNING"
            assert rows[completed].execution_outcome == "TEST_FAILED"
            job_id = uuid4()
            connection.execute(upgraded.update().where(upgraded.c.id == pending.hex).values(status="QUEUED", task_id=job_id.hex))
            outbox = Table("run_dispatches", MetaData(), autoload_with=connection)
            connection.execute(outbox.insert().values(run_id=pending.hex))
            command.downgrade(config, "0002_run_lifecycle")
            restored = Table("test_runs", MetaData(), autoload_with=connection)
            rows = {UUID(str(row.id)): row for row in connection.execute(select(restored))}
            assert rows[pending].status == "FAILED"
            assert rows[pending].execution_outcome == "INFRASTRUCTURE_ERROR"
            assert rows[running].status == "RUNNING"
            assert rows[completed].execution_outcome == "TEST_FAILED"
    finally:
        engine.dispose()
