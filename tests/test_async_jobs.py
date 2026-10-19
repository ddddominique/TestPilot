"""Focused direct-publication and database-backed worker claim tests."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.exc import SQLAlchemyError

from testpilot.api.app import create_app
from testpilot.api.database import create_session_factory
from testpilot.api.db_models import Base, TestRunRecord
from testpilot.api.repository import RunRepository
from testpilot.execution_models import ExecutionResult, ExecutionStatus
from testpilot.pipeline import PipelineResult
from testpilot.worker.celery_app import app
from testpilot.worker.tasks import execute_task, run_job

URL = "https://github.com/a/b/pull/1"


@pytest.fixture
def sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'jobs.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    try:
        yield create_session_factory(engine)
    finally:
        engine.dispose()


def test_api_publishes_stable_identity_without_executing_pipeline(sessions, broker, monkeypatch):
    forbidden = Mock(side_effect=AssertionError("API must not execute the pipeline"))
    monkeypatch.setattr("testpilot.pipeline.PipelineService.run", forbidden)

    def publish(name, args, task_id, retry):
        assert sessions.kw["bind"].pool.checkedout() == 0
        with sessions() as session:
            row = session.get(TestRunRecord, UUID(args[0]))
            assert row.status == "QUEUED" and str(row.task_id) == task_id
        assert name == "testpilot.execute_run" and retry is False

    broker.side_effect = publish
    with TestClient(create_app(sessions=sessions)) as client:
        identifier = client.post("/api/runs", json={"pr_url": URL}).json()["id"]
        response = client.post(f"/api/runs/{identifier}/execute")
        assert response.status_code == 202
        assert response.headers["Location"] == f"/api/runs/{identifier}"
        assert response.json()["status"] == "QUEUED"
        assert response.json()["queued_at"] and response.json()["started_at"] is None
        assert client.post(f"/api/runs/{identifier}/execute").status_code == 409
        assert client.post(f"/api/runs/{uuid4()}/execute").status_code == 404
    broker.assert_called_once()
    forbidden.assert_not_called()


def test_broker_failure_is_persisted_and_late_delivery_cannot_execute(sessions, broker):
    broker.side_effect = ConnectionError("redis://user:secret-password@host")
    with TestClient(create_app(sessions=sessions)) as client:
        identifier = client.post("/api/runs", json={"pr_url": URL}).json()["id"]
        response = client.post(f"/api/runs/{identifier}/execute")
        assert response.status_code == 503 and "secret-password" not in response.text
        row = client.get(f"/api/runs/{identifier}").json()
        assert row["status"] == "FAILED" and row["execution_outcome"] == "INFRASTRUCTURE_ERROR"
        assert row["completed_at"] and "publication" in row["error_details"]
        assert "secret-password" not in str(row)
        assert client.post(f"/api/runs/{identifier}/execute").status_code == 409
        service = Mock()
        assert run_job(identifier, row["task_id"], sessions, lambda: service) == "IGNORED"
        service.run.assert_not_called()


@pytest.mark.parametrize("finished", [False, True])
def test_uncertain_publication_does_not_overwrite_worker_owned_status(sessions, broker, finished):
    def accepted_but_response_lost(name, args, task_id, retry):
        with sessions() as session:
            RunRepository(session).claim(UUID(args[0]), UUID(task_id))
        if finished:
            with sessions() as session:
                RunRepository(session).finish(UUID(args[0]), {"status": "COMPLETED", "execution_outcome": "PASSED"})
        raise ConnectionError("acknowledgement lost")

    broker.side_effect = accepted_but_response_lost
    with TestClient(create_app(sessions=sessions)) as client:
        identifier = client.post("/api/runs", json={"pr_url": URL}).json()["id"]
        assert client.post(f"/api/runs/{identifier}/execute").status_code == 503
        row = client.get(f"/api/runs/{identifier}").json()
        assert row["status"] == ("COMPLETED" if finished else "RUNNING")
        assert row["execution_outcome"] == ("PASSED" if finished else None)


def test_concurrent_requests_publish_once(sessions, broker):
    with TestClient(create_app(sessions=sessions)) as client:
        identifier = client.post("/api/runs", json={"pr_url": URL}).json()["id"]
        barrier = Barrier(2)

        def submit():
            barrier.wait(timeout=5)
            return client.post(f"/api/runs/{identifier}/execute").status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit) for _ in range(2)]
            assert sorted(f.result(timeout=5) for f in futures) == [202, 409]
    broker.assert_called_once()


@pytest.mark.parametrize("outcome,status", [(ExecutionStatus.PASSED, "COMPLETED"),
                                           (ExecutionStatus.TEST_FAILED, "COMPLETED"),
                                           (ExecutionStatus.INFRASTRUCTURE_ERROR, "FAILED")])
def test_worker_results_and_duplicate_deliveries(sessions, outcome, status):
    with sessions() as session:
        identifier = RunRepository(session).create(URL).id
    with sessions() as session:
        task_id = RunRepository(session).queue(identifier).task_id
    service = Mock()
    service.run.return_value = PipelineResult(execution=ExecutionResult(
        "a/b", 1, "a" * 40, "pytest", [], outcome, output="evidence",
        passed=int(outcome == ExecutionStatus.PASSED), failed=int(outcome == ExecutionStatus.TEST_FAILED),
    ))
    assert run_job(identifier, uuid4(), sessions, lambda: service) == "IGNORED"
    assert run_job(identifier, task_id, sessions, lambda: service) == status
    assert run_job(identifier, task_id, sessions, lambda: service) == "IGNORED"
    service.run.assert_called_once_with(URL)
    with sessions() as session:
        row = session.get(TestRunRecord, identifier)
        assert row.execution_outcome == outcome.value
        assert row.started_at and row.completed_at and row.task_id == task_id
        assert row.execution_output == "evidence"


def test_queue_commit_failure_never_publishes(sessions, broker):
    with sessions() as session:
        identifier = RunRepository(session).create(URL).id

    def fail(session):
        raise SQLAlchemyError("commit failed")

    event.listen(sessions.class_, "before_commit", fail)
    try:
        with TestClient(create_app(sessions=sessions)) as client:
            assert client.post(f"/api/runs/{identifier}/execute").status_code == 503
    finally:
        event.remove(sessions.class_, "before_commit", fail)
    broker.assert_not_called()
    with sessions() as session:
        assert session.get(TestRunRecord, identifier).status == "PENDING"


def test_task_entrypoint_disposes_engine_on_failure(monkeypatch):
    engine = Mock()
    monkeypatch.setattr("testpilot.worker.tasks.create_database_engine", lambda: engine)
    monkeypatch.setattr("testpilot.worker.tasks.create_session_factory", lambda engine: Mock())
    monkeypatch.setattr("testpilot.worker.tasks.run_job", Mock(side_effect=RuntimeError("failed")))
    with pytest.raises(RuntimeError):
        execute_task.apply(args=[str(uuid4())], task_id=str(uuid4()), throw=True)
    engine.dispose.assert_called_once()
    assert execute_task.max_retries == 0
    assert app.conf.task_publish_retry is False
