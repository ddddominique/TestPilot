"""Offline API and transactional persistence tests using an isolated SQLite DB."""

import io
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from testpilot.api.app import create_app
from testpilot.api.database import create_session_factory, database_url
from testpilot.api.db_models import Base, TestRunRecord
from testpilot.api.repository import RunRepository


PR_URL = "https://github.com/ddddominique/Coding-Exercise/pull/1"


@pytest.fixture
def sessions(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'api-test.db'}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture
def client(sessions):
    with TestClient(create_app(sessions=sessions)) as client:
        yield client


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_pending_run_without_running_pipeline(client, sessions, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("POST must not call the pipeline or network")

    monkeypatch.setattr("testpilot.github.GitHubClient.get_pull_request", forbidden)
    monkeypatch.setattr("testpilot.test_generator.TestGenerator.generate", forbidden)
    monkeypatch.setattr("testpilot.executor.TestExecutor.execute", forbidden)
    monkeypatch.setattr("requests.Session.get", forbidden)
    response = client.post("/api/runs", json={"pr_url": PR_URL})
    assert response.status_code == 201
    record = response.json()
    assert str(UUID(record["id"])) == record["id"]
    assert response.headers["Location"] == f"/api/runs/{record['id']}"
    assert record["status"] == "PENDING"
    assert record["repository"] == "ddddominique/Coding-Exercise"
    assert record["pr_number"] == 1
    assert record["head_sha"] is record["framework"] is None
    assert record["generated_tests"] == []
    assert record["passed"] == record["failed"] == record["skipped"] == 0
    assert record["execution_output"] is record["error_details"] is None
    assert record["created_at"] and record["updated_at"]
    with sessions() as session:
        persisted = session.get(TestRunRecord, UUID(record["id"]))
        assert persisted is not None
        assert persisted.pr_url == PR_URL


@pytest.mark.parametrize("pr_url", [
    "invalid", "https://example.com/a/b/pull/1", "https://github.com/a/b/issues/1",
    "https://github.com/a/b/pull/0", "https://github.com/a/b/pull/-1",
    "https://github.com/a/../pull/1", "https://github.com/a/b/pull/999999999999999999999",
    "https://github.com/a/b/pull/1/extra", "https://github.com/a/b/pull/not-a-number",
])
def test_invalid_pr_url_returns_422_without_persisting(client, sessions, pr_url):
    response = client.post("/api/runs", json={"pr_url": pr_url})
    assert response.status_code == 422
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(TestRunRecord)) == 0


def test_missing_url_and_unexpected_fields_are_rejected(client):
    assert client.post("/api/runs", json={}).status_code == 422
    assert client.post("/api/runs", json={"pr_url": PR_URL, "status": "PASSED"}).status_code == 422


def test_normalizes_supported_url_variants(client):
    response = client.post("/api/runs", json={"pr_url": " http://www.github.com/a/b/pull/001?diff=split "})
    assert response.status_code == 201
    assert response.json()["pr_url"] == "https://github.com/a/b/pull/1"


def test_get_run_and_persistence_across_application_instances(client, sessions):
    record = client.post("/api/runs", json={"pr_url": PR_URL}).json()
    with TestClient(create_app(sessions=sessions)) as restarted:
        fetched = restarted.get(f"/api/runs/{record['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == record


def test_missing_and_malformed_run_ids(client):
    assert client.get(f"/api/runs/{uuid4()}").status_code == 404
    assert client.get("/api/runs/not-a-uuid").status_code == 422


def test_history_newest_first_with_pagination(client, sessions):
    identifiers = []
    for number in range(1, 4):
        row = client.post("/api/runs", json={"pr_url": f"https://github.com/a/b/pull/{number}"}).json()
        identifiers.append(row["id"])
        with sessions.begin() as session:
            record = session.get(TestRunRecord, UUID(row["id"]))
            record.created_at = datetime(2026, 1, number, tzinfo=timezone.utc)
    response = client.get("/api/runs", params={"limit": 2, "offset": 1})
    assert response.status_code == 200
    page = response.json()
    assert (page["total"], page["limit"], page["offset"]) == (3, 2, 1)
    assert [record["id"] for record in page["items"]] == [identifiers[1], identifiers[0]]
    assert client.get("/api/runs?offset=100").json()["items"] == []


def test_history_ties_have_stable_uuid_order(client, sessions):
    identifiers = [client.post("/api/runs", json={"pr_url": PR_URL}).json()["id"] for _ in range(3)]
    with sessions.begin() as session:
        for identifier in identifiers:
            session.get(TestRunRecord, UUID(identifier)).created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    page = client.get("/api/runs").json()
    assert [record["id"] for record in page["items"]] == sorted(identifiers, reverse=True)


def test_persists_metadata_and_updates_timestamp_on_orm_changes(client, sessions):
    record = client.post("/api/runs", json={"pr_url": PR_URL}).json()
    identifier = UUID(record["id"])
    with sessions.begin() as session:
        session.get(TestRunRecord, identifier).updated_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    with sessions.begin() as session:
        row = session.get(TestRunRecord, identifier)
        row.generated_tests = [{"suggested_test_file": "tests/test_solution.py", "assumptions": []}]
        row.head_sha = "a" * 40
        row.framework = "pytest"
    fetched = client.get(f"/api/runs/{identifier}").json()
    assert fetched["generated_tests"][0]["suggested_test_file"] == "tests/test_solution.py"
    assert fetched["head_sha"] == "a" * 40
    assert fetched["updated_at"][:4] != "2020"


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "limit=x", "offset=1000001"])
def test_invalid_pagination(client, query):
    assert client.get(f"/api/runs?{query}").status_code == 422


def test_empty_history(client):
    assert client.get("/api/runs").json() == {"items": [], "total": 0, "limit": 20, "offset": 0}


def test_constraint_failure_rolls_back_entire_transaction(sessions):
    with sessions() as session:
        with pytest.raises(IntegrityError):
            with session.begin():
                session.add(TestRunRecord(pr_url=PR_URL, repository="a/b", pr_number=1))
                session.flush()
                session.add(TestRunRecord(pr_url=PR_URL, repository="a/b", pr_number=2, passed=-1))
                session.flush()
        # Failed writes leave the session usable and do not retain the first row.
        saved = RunRepository(session).create(PR_URL)
        assert saved.status == "PENDING"
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(TestRunRecord)) == 1


def test_commit_failure_returns_503_and_rolls_back_without_leaking_details(client, sessions):
    def fail_commit(session):
        raise SQLAlchemyError("postgresql://user:secret-password@host/database")

    event.listen(sessions.class_, "before_commit", fail_commit)
    try:
        response = client.post("/api/runs", json={"pr_url": PR_URL})
    finally:
        event.remove(sessions.class_, "before_commit", fail_commit)
    assert response.status_code == 503
    assert "secret-password" not in response.text
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(TestRunRecord)) == 0


def test_database_configuration_requires_psycopg(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="DATABASE_URL is required"):
        database_url()
    with pytest.raises(ValueError, match="postgresql\\+psycopg"):
        database_url("sqlite:///test.db")
    assert database_url("postgresql+psycopg://user:placeholder@localhost/testpilot").drivername == "postgresql+psycopg"


def test_metadata_compiles_to_postgresql_jsonb_and_uuid():
    table = TestRunRecord.__table__
    dialect = postgresql.dialect()
    assert table.c.generated_tests.type.compile(dialect=dialect) == "JSONB"
    assert table.c.id.type.compile(dialect=dialect) == "UUID"
    assert table.c.created_at.type.timezone


def test_initial_migration_generates_postgresql_sql_without_network(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:placeholder@localhost/testpilot")
    output = io.StringIO()
    config = Config("alembic.ini", output_buffer=output)
    command.upgrade(config, "head", sql=True)
    sql = output.getvalue()
    assert "CREATE TABLE test_runs" in sql
    assert "generated_tests JSONB" in sql
    assert "id UUID" in sql
    assert "ck_test_runs_counts_nonnegative" in sql
    assert "secret-password" not in sql
