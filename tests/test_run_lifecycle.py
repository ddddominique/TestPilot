from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import SQLAlchemyError

from testpilot.api.app import create_app
from testpilot.api.database import create_session_factory
from testpilot.api.db_models import Base, TestRunRecord
from testpilot.api.repository import RunConflict, RunRepository
from testpilot.api.run_service import result_values
from testpilot.execution_models import ExecutionResult, ExecutionStatus
from testpilot.generation_models import FrameworkDetection, GenerationResult, TestProposal
from testpilot.models import PullRequest
from testpilot.pipeline import PipelineError, PipelineResult


URL = "https://github.com/a/b/pull/1"


@pytest.fixture
def sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'runs.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    try:
        yield create_session_factory(engine)
    finally:
        engine.dispose()


def pipeline_result(status=ExecutionStatus.PASSED):
    proposal = TestProposal(
        suggested_test_file="tests/test_generated.py", target_source_file="app.py", framework="pytest",
        test_code="from app import value\n\ndef test_value():\n    assert value == 1\n",
        explanation="checks changed behavior", assumptions=[],
    )
    return PipelineResult(
        pull_request=PullRequest("a", "b", 1, "title", "", "base", "a" * 40, []),
        framework=FrameworkDetection("python", "pytest", ["Python only"], selection="inferred"),
        generation=GenerationResult(summary="regression test", proposals=[proposal], warnings=[]),
        execution=ExecutionResult(
            "a/b", 1, "a" * 40, "pytest", [proposal.suggested_test_file], status,
            exit_code=0 if status == ExecutionStatus.PASSED else 1,
            passed=3 if status == ExecutionStatus.PASSED else 0,
            failed=1 if status == ExecutionStatus.TEST_FAILED else 0,
            output="test evidence", error="" if status == ExecutionStatus.PASSED else "execution diagnostic",
        ),
    )


@pytest.mark.parametrize("outcome,lifecycle", [
    (ExecutionStatus.PASSED, "COMPLETED"), (ExecutionStatus.TEST_FAILED, "COMPLETED"),
    (ExecutionStatus.COLLECTION_ERROR, "COMPLETED"), (ExecutionStatus.DEPENDENCY_ERROR, "COMPLETED"),
    (ExecutionStatus.TIMEOUT, "COMPLETED"), (ExecutionStatus.UNSUPPORTED, "COMPLETED"),
    (ExecutionStatus.INFRASTRUCTURE_ERROR, "FAILED"),
])
def test_execute_persists_lifecycle_results_and_survives_restart(sessions, outcome, lifecycle):
    calls = []

    def run(pr_url):
        # All claim transactions are committed and their connections returned.
        assert sessions.kw["bind"].pool.checkedout() == 0
        with sessions() as session:
            row = session.scalar(select(TestRunRecord))
            assert row.status == "RUNNING"
            assert row.started_at is not None
        calls.append(pr_url)
        return pipeline_result(outcome)

    pipeline = Mock()
    pipeline.run.side_effect = run
    with TestClient(create_app(sessions=sessions, pipeline_factory=lambda: pipeline)) as client:
        pending = client.post("/api/runs", json={"pr_url": URL}).json()
        assert pending["status"] == "PENDING"
        assert calls == []
        response = client.post(f"/api/runs/{pending['id']}/execute")
        assert response.status_code == 200
        saved = response.json()
        assert saved["status"] == lifecycle
        assert saved["execution_outcome"] == outcome.value
        assert saved["head_sha"] == "a" * 40
        assert saved["framework"] == "pytest"
        assert saved["framework_selection"] == "inferred"
        assert saved["generated_tests"][0]["test_code"].startswith("from app")
        assert saved["execution_output"] == "test evidence"
        assert saved["started_at"] is not None and saved["completed_at"] is not None
        assert client.post(f"/api/runs/{pending['id']}/execute").status_code == 409
        assert client.get("/api/runs").json()["items"][0]["execution_outcome"] == outcome.value
    with TestClient(create_app(sessions=sessions)) as restarted:
        assert restarted.get(f"/api/runs/{pending['id']}").json() == saved
    assert calls == [URL]


def test_exception_persists_sanitized_failure_and_partial_metadata(sessions, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-private-secret")
    partial = pipeline_result()
    partial.generation = partial.execution = None
    pipeline = Mock()
    pipeline.run.side_effect = PipelineError("generation", partial, RuntimeError("Authorization: Bearer sk-private-secret"))
    with TestClient(create_app(sessions=sessions, pipeline_factory=lambda: pipeline)) as client:
        identifier = client.post("/api/runs", json={"pr_url": URL}).json()["id"]
        response = client.post(f"/api/runs/{identifier}/execute")
        assert response.status_code == 200
        assert response.json()["status"] == "FAILED"
        assert response.json()["execution_outcome"] is None
        assert response.json()["head_sha"] == "a" * 40
        assert "generation" in response.json()["error_details"]
        assert "sk-private-secret" not in response.text
        assert client.post(f"/api/runs/{identifier}/execute").status_code == 409


def test_unknown_run_never_starts_pipeline(sessions):
    pipeline = Mock()
    with TestClient(create_app(sessions=sessions, pipeline_factory=lambda: pipeline)) as client:
        assert client.post(f"/api/runs/{uuid4()}/execute").status_code == 404
    pipeline.run.assert_not_called()


def test_concurrent_execute_requests_claim_run_once(sessions):
    started, release = Event(), Event()
    pipeline = Mock()

    def run(pr_url):
        started.set()
        assert release.wait(5), "test did not release pipeline"
        return pipeline_result()

    pipeline.run.side_effect = run
    with TestClient(create_app(sessions=sessions, pipeline_factory=lambda: pipeline)) as client:
        identifier = client.post("/api/runs", json={"pr_url": URL}).json()["id"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(client.post, f"/api/runs/{identifier}/execute")
            try:
                assert started.wait(5)
                assert client.post(f"/api/runs/{identifier}/execute").status_code == 409
                assert client.get(f"/api/runs/{identifier}").json()["status"] == "RUNNING"
            finally:
                release.set()
            assert first.result(timeout=5).json()["execution_outcome"] == "PASSED"
    pipeline.run.assert_called_once()


def test_simultaneous_database_claims_have_exactly_one_winner(sessions):
    with sessions() as session:
        identifier = RunRepository(session).create(URL).id
    barrier = Barrier(2)

    def claim():
        with sessions() as session:
            barrier.wait(timeout=5)
            try:
                RunRepository(session).claim(identifier)
                return "claimed"
            except RunConflict:
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(claim), pool.submit(claim)
        assert sorted([first.result(timeout=5), second.result(timeout=5)]) == ["claimed", "conflict"]


def test_claim_commit_failure_rolls_back_and_prevents_execution(sessions):
    pipeline = Mock()
    with TestClient(create_app(sessions=sessions, pipeline_factory=lambda: pipeline)) as client:
        identifier = client.post("/api/runs", json={"pr_url": URL}).json()["id"]

        def fail(session):
            raise SQLAlchemyError("commit failed")

        event.listen(sessions.class_, "before_commit", fail)
        try:
            assert client.post(f"/api/runs/{identifier}/execute").status_code == 503
        finally:
            event.remove(sessions.class_, "before_commit", fail)
        assert client.get(f"/api/runs/{identifier}").json()["status"] == "PENDING"
        pipeline.run.assert_not_called()


def test_final_commit_failure_leaves_running_and_does_not_allow_rerun(sessions):
    def fail(session):
        raise SQLAlchemyError("finish failed")

    def run(pr_url):
        event.listen(sessions.class_, "before_commit", fail)
        return pipeline_result()

    pipeline = Mock()
    pipeline.run.side_effect = run
    with TestClient(create_app(sessions=sessions, pipeline_factory=lambda: pipeline)) as client:
        identifier = client.post("/api/runs", json={"pr_url": URL}).json()["id"]
        try:
            assert client.post(f"/api/runs/{identifier}/execute").status_code == 503
        finally:
            event.remove(sessions.class_, "before_commit", fail)
        row = client.get(f"/api/runs/{identifier}").json()
        assert row["status"] == "RUNNING"
        assert row["execution_outcome"] is None
        assert client.post(f"/api/runs/{identifier}/execute").status_code == 409
    pipeline.run.assert_called_once()


def test_persistence_bounds_output_and_redacts_credentials(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "private-token-value")
    result = pipeline_result()
    result.execution = replace(result.execution, output="private-token-value " + "x" * 100_000)
    values = result_values(result)
    assert "private-token-value" not in values["execution_output"]
    assert "[REDACTED]" in values["execution_output"]
    assert len(values["execution_output"].encode()) <= 64_000
    assert "TRUNCATED" in values["execution_output"]


def test_generated_tests_are_redacted_and_oversized_modules_are_not_partially_saved(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-private-code-value")
    result = pipeline_result()
    result.generation.proposals[0].test_code = '# sk-private-code-value\ndef test_ok(): pass\n'
    values = result_values(result)
    assert "sk-private-code-value" not in str(values)
    assert "[REDACTED]" in values["generated_tests"][0]["test_code"]
    assert any("sanitized" in item for item in values["warnings"])
    result.generation.proposals[0].test_code = "x" * 400_001
    oversized = result_values(result)
    assert oversized["generated_tests"] == []
    assert any("omitted" in warning for warning in oversized["warnings"])


def test_generic_factory_error_is_persisted_without_exposing_exception(sessions):
    def fail():
        raise RuntimeError("Authorization: Basic sensitive-credential-value")

    with TestClient(create_app(sessions=sessions, pipeline_factory=fail)) as client:
        identifier = client.post("/api/runs", json={"pr_url": URL}).json()["id"]
        response = client.post(f"/api/runs/{identifier}/execute")
        assert response.status_code == 200
        assert response.json()["status"] == "FAILED"
        assert "sensitive-credential-value" not in response.text
