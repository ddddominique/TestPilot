"""Optional real PostgreSQL migration/API test, isolated in a temporary schema."""

import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError

from testpilot.api.app import create_app
from testpilot.api.database import create_session_factory, database_url
from testpilot.api.db_models import TestRunRecord


@pytest.mark.skipif(
    not os.getenv("TESTPILOT_TEST_DATABASE_URL"),
    reason="Set TESTPILOT_TEST_DATABASE_URL to a dedicated PostgreSQL test database",
)
def test_real_postgresql_migration_pending_run_jsonb_and_rollback():
    url = database_url(os.environ["TESTPILOT_TEST_DATABASE_URL"])
    schema = "testpilot_test_" + uuid4().hex
    admin = create_engine(url)
    engine = None
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={"options": f"-c search_path={schema}"})
        with engine.begin() as connection:
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        sessions = create_session_factory(engine)
        with TestClient(create_app(sessions=sessions)) as client:
            response = client.post("/api/runs", json={"pr_url": "https://github.com/a/b/pull/1"})
            assert response.status_code == 201
            record = response.json()
            assert record["status"] == "PENDING"
            assert client.get(f"/api/runs/{record['id']}").json() == record
        with sessions() as session:
            assert session.scalar(text("SELECT pg_typeof(generated_tests)::text FROM test_runs")) == "jsonb"
            assert session.scalar(select(TestRunRecord)).generated_tests == []
            session.rollback()
            with pytest.raises(IntegrityError):
                with session.begin():
                    session.add(TestRunRecord(
                        pr_url="https://github.com/a/b/pull/2", repository="a/b", pr_number=2, failed=-1,
                    ))
            assert len(session.scalars(select(TestRunRecord)).all()) == 1
    finally:
        if engine is not None:
            engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()
