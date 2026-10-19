"""Offline delivery, crash-window, fencing, and reconciliation regressions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import SQLAlchemyError

from testpilot.api.app import create_app
from testpilot.api.database import create_session_factory
from testpilot.api.db_models import Base, RunDispatch, TestRunRecord
from testpilot.api.repository import RunConflict, RunRepository
from testpilot.execution_models import ExecutionResult, ExecutionStatus
from testpilot.pipeline import PipelineResult
from testpilot.worker.celery_app import app, HARD_TIME_LIMIT, RECONCILE_MIN_AGE
from testpilot.worker.dispatcher import DispatchBackoff, dispatch_once, publish, still_queued
from testpilot.worker.reconcile import reconcile
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


def queue(sessions):
    with sessions() as session:
        identifier = RunRepository(session).create(URL).id
    with sessions() as session:
        row = RunRepository(session).queue(identifier)
        return row.id, row.task_id


def pipeline(status=ExecutionStatus.PASSED):
    service = Mock()
    service.run.return_value = PipelineResult(execution=ExecutionResult(
        "a/b", 1, "a" * 40, "pytest", [], status,
        passed=1 if status == ExecutionStatus.PASSED else 0,
        failed=1 if status == ExecutionStatus.TEST_FAILED else 0,
        exit_code=0 if status == ExecutionStatus.PASSED else 1,
        output="fixture evidence",
    ))
    return service


def test_api_queues_durably_without_network_or_pipeline(sessions, monkeypatch):
    forbidden = Mock(side_effect=AssertionError("request must not execute or publish"))
    monkeypatch.setattr("testpilot.pipeline.PipelineService.run", forbidden)
    monkeypatch.setattr(app, "send_task", forbidden)
    with TestClient(create_app(sessions=sessions)) as client:
        row = client.post("/api/runs", json={"pr_url": URL}).json()
        response = client.post(f"/api/runs/{row['id']}/execute")
        assert response.status_code == 202
        queued = response.json()
        assert queued["status"] == "QUEUED"
        assert queued["queued_at"] and UUID(queued["task_id"])
        assert queued["started_at"] is None
        assert response.headers["Location"] == f"/api/runs/{row['id']}"
        assert client.post(f"/api/runs/{row['id']}/execute").status_code == 409
        assert client.post(f"/api/runs/{uuid4()}/execute").status_code == 404
    forbidden.assert_not_called()
    with sessions() as session:
        assert len(session.scalars(select(RunDispatch)).all()) == 1


def test_concurrent_api_submissions_create_one_dispatch(sessions):
    with TestClient(create_app(sessions=sessions)) as client:
        row = client.post("/api/runs", json={"pr_url": URL}).json()
        barrier = Barrier(2)

        def submit():
            barrier.wait(timeout=5)
            return client.post(f"/api/runs/{row['id']}/execute").status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit) for _ in range(2)]
            assert sorted(f.result(timeout=5) for f in futures) == [202, 409]
    with sessions() as session:
        assert len(session.scalars(select(RunDispatch)).all()) == 1


def test_queue_and_outbox_commit_is_atomic(sessions):
    with sessions() as session:
        identifier = RunRepository(session).create(URL).id

    def fail(session):
        raise SQLAlchemyError("commit failure")

    event.listen(sessions.class_, "before_commit", fail)
    try:
        with sessions() as session, pytest.raises(SQLAlchemyError):
            RunRepository(session).queue(identifier)
    finally:
        event.remove(sessions.class_, "before_commit", fail)
    with sessions() as session:
        assert session.get(TestRunRecord, identifier).status == "PENDING"
        assert session.get(RunDispatch, identifier) is None


def test_concurrent_dispatchers_publish_once_per_lease(sessions):
    queue(sessions)
    barrier = Barrier(2)
    publisher = Mock()
    now = datetime.now(timezone.utc) + timedelta(seconds=1)

    def dispatch():
        barrier.wait(timeout=5)
        return dispatch_once(sessions, publisher, now=now)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(dispatch) for _ in range(2)]
        assert sorted(f.result(timeout=5) for f in futures) == [False, True]
    publisher.assert_called_once()


def test_broker_failure_leaves_durable_queued_job_and_bounded_diagnostic(sessions):
    identifier, task_id = queue(sessions)
    publisher = Mock(side_effect=ConnectionError("redis://user:private-password@host"))
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    assert dispatch_once(sessions, publisher, now=now)
    with sessions() as session:
        assert session.get(TestRunRecord, identifier).status == "QUEUED"
        intent = session.get(RunDispatch, identifier)
        assert intent.attempts == 1
        assert "private-password" not in intent.last_error
        assert intent.published_at is None
    publisher.side_effect = None
    assert not dispatch_once(sessions, publisher, now=now)
    assert dispatch_once(sessions, publisher, now=now + timedelta(seconds=61))
    assert publisher.call_args.args == (identifier, task_id)


@pytest.mark.parametrize("published", [False, True])
def test_crash_before_or_after_broker_publication_recovers_same_job(sessions, published):
    identifier, task_id = queue(sessions)
    sent = []
    now = datetime.now(timezone.utc) + timedelta(seconds=1)

    def crash(run_id, job_id):
        if published:
            sent.append((run_id, job_id))
        raise SystemExit("simulated dispatcher crash")

    with pytest.raises(SystemExit):
        dispatch_once(sessions, crash, now=now)
    assert not dispatch_once(sessions, Mock(), now=now)
    assert dispatch_once(sessions, lambda *args: sent.append(args), now=now + timedelta(seconds=61))
    assert sent == [(identifier, task_id)] * (2 if published else 1)
    service = pipeline()
    for run_id, job_id in sent:
        run_job(run_id, job_id, sessions, lambda: service)
    service.run.assert_called_once_with(URL)


def test_lost_redis_message_is_republished_until_worker_claims(sessions):
    identifier, task_id = queue(sessions)
    publisher = Mock()
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    assert dispatch_once(sessions, publisher, now=now)
    assert dispatch_once(sessions, publisher, now=now + timedelta(seconds=61))
    with sessions() as session:
        RunRepository(session).claim(identifier, task_id)
    assert not dispatch_once(sessions, publisher, now=now + timedelta(seconds=182))


@pytest.mark.parametrize("fails", [False, True])
def test_backoff_progression_and_cap_for_failures_and_unclaimed_publications(sessions, fails):
    identifier, task_id = queue(sessions)
    publisher = Mock(side_effect=ConnectionError("broker unavailable") if fails else None)
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    for attempt, delay in enumerate([60, 120, 240, 480, 900, 900], start=1):
        assert dispatch_once(sessions, publisher, now=now)
        with sessions() as session:
            intent = session.get(RunDispatch, identifier)
            assert intent.attempts == attempt
            assert intent.available_at.replace(tzinfo=timezone.utc) == now + timedelta(seconds=delay)
            assert (intent.last_error is not None) == fails
            assert (intent.published_at is None) == fails
            row = session.get(TestRunRecord, identifier)
            assert row.status == "QUEUED" and row.started_at is None
            assert row.task_id == task_id
        assert not dispatch_once(sessions, publisher, now=now + timedelta(seconds=delay - 1))
        now += timedelta(seconds=delay)
    assert publisher.call_args_list == [((identifier, task_id),)] * 6


def test_extended_worker_outage_limits_messages_but_recovers_lost_delivery(sessions):
    identifier, task_id = queue(sessions)
    publisher = Mock()
    start = datetime.now(timezone.utc) + timedelta(seconds=1)
    # One hour of minute-by-minute polling: 60 fixed-interval copies become 7.
    for seconds in range(0, 3_600, 60):
        dispatch_once(sessions, publisher, now=start + timedelta(seconds=seconds))
    assert publisher.call_count == 7
    with sessions() as session:
        intent = session.get(RunDispatch, identifier)
        next_attempt = intent.available_at.replace(tzinfo=timezone.utc)
    assert dispatch_once(sessions, publisher, now=next_attempt)
    service = pipeline()
    for call in publisher.call_args_list:
        assert call.args == (identifier, task_id)
        run_job(*call.args, sessions, lambda: service)
    service.run.assert_called_once_with(URL)
    assert not dispatch_once(sessions, publisher, now=next_attempt + timedelta(seconds=901))


@pytest.mark.parametrize("status", ["RUNNING", "COMPLETED", "FAILED"])
def test_terminal_or_running_jobs_are_not_published(sessions, status):
    identifier, task_id = queue(sessions)
    with sessions() as session:
        RunRepository(session).claim(identifier, task_id)
    if status != "RUNNING":
        with sessions() as session:
            RunRepository(session).finish(identifier, {"status": status})
    publisher = Mock()
    assert not dispatch_once(sessions, publisher, now=datetime.now(timezone.utc) + timedelta(days=1))
    publisher.assert_not_called()


def test_worker_claim_between_lease_and_publication_is_rechecked(sessions, monkeypatch):
    identifier, task_id = queue(sessions)

    def claim_then_check(factory, run_id, job_id, token):
        with sessions() as session:
            RunRepository(session).claim(identifier, task_id)
        return still_queued(factory, run_id, job_id, token)

    monkeypatch.setattr("testpilot.worker.dispatcher.still_queued", claim_then_check)
    publisher = Mock()
    assert dispatch_once(sessions, publisher, now=datetime.now(timezone.utc) + timedelta(seconds=1))
    publisher.assert_not_called()


def test_broker_io_has_no_checked_out_database_connection(sessions):
    queue(sessions)

    def publish_without_connection(*args):
        assert sessions.kw["bind"].pool.checkedout() == 0

    assert dispatch_once(sessions, publish_without_connection,
                         now=datetime.now(timezone.utc) + timedelta(seconds=1))


def test_configured_backoff_is_loaded_and_applied(sessions, monkeypatch):
    monkeypatch.setenv("TESTPILOT_DISPATCH_INTERVAL_SECONDS", "120")
    monkeypatch.setenv("TESTPILOT_DISPATCH_MAX_BACKOFF_SECONDS", "300")
    assert DispatchBackoff.from_environment() == DispatchBackoff(120, 300)
    identifier, _ = queue(sessions)
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    assert dispatch_once(sessions, Mock(), now=now)
    with sessions() as session:
        assert session.get(RunDispatch, identifier).available_at.replace(tzinfo=timezone.utc) == now + timedelta(seconds=120)
    assert [DispatchBackoff(120, 300).delay(i) for i in [1, 2, 3, 1_000_000_000]] == [120, 240, 300, 300]


@pytest.mark.parametrize("interval,maximum", [("0", "900"), ("-1", "900"), ("60", "59"),
                                               ("60", "86401"), ("1.5", "900"), ("60", "invalid")])
def test_invalid_backoff_configuration_fails_before_leasing(sessions, monkeypatch, interval, maximum):
    monkeypatch.setenv("TESTPILOT_DISPATCH_INTERVAL_SECONDS", interval)
    monkeypatch.setenv("TESTPILOT_DISPATCH_MAX_BACKOFF_SECONDS", maximum)
    identifier, _ = queue(sessions)
    publisher = Mock()
    with pytest.raises(ValueError):
        dispatch_once(sessions, publisher)
    publisher.assert_not_called()
    with sessions() as session:
        assert session.get(RunDispatch, identifier).attempts == 0


@pytest.mark.parametrize("status,lifecycle", [
    (ExecutionStatus.PASSED, "COMPLETED"), (ExecutionStatus.TEST_FAILED, "COMPLETED"),
    (ExecutionStatus.INFRASTRUCTURE_ERROR, "FAILED"),
])
def test_worker_stores_results_and_ignores_duplicate_or_wrong_task(sessions, status, lifecycle):
    identifier, task_id = queue(sessions)
    service = pipeline(status)
    assert run_job(identifier, uuid4(), sessions, lambda: service) == "IGNORED"
    assert run_job(identifier, task_id, sessions, lambda: service) == lifecycle
    assert run_job(identifier, task_id, sessions, lambda: service) == "IGNORED"
    service.run.assert_called_once_with(URL)
    with sessions() as session:
        row = session.get(TestRunRecord, identifier)
        assert row.execution_outcome == status.value
        assert row.started_at and row.completed_at and row.queued_at
        assert row.execution_output == "fixture evidence"


def test_worker_crash_and_restart_do_not_repeat_external_work(sessions):
    identifier, task_id = queue(sessions)
    service = Mock()
    service.run.side_effect = SystemExit("worker killed after external side effect")
    with pytest.raises(SystemExit):
        run_job(identifier, task_id, sessions, lambda: service)
    assert run_job(identifier, task_id, sessions, lambda: service) == "IGNORED"
    service.run.assert_called_once()
    with sessions() as session:
        assert session.get(TestRunRecord, identifier).status == "RUNNING"
    assert reconcile(sessions, now=datetime.now(timezone.utc) + timedelta(hours=2)) == 1
    with sessions() as session:
        row = session.get(TestRunRecord, identifier)
        assert row.status == "FAILED"
        assert row.execution_outcome == "INFRASTRUCTURE_ERROR"
        assert "unknown" in row.error_details
        assert row.passed == 0
    assert run_job(identifier, task_id, sessions, lambda: service) == "IGNORED"


def test_recovery_preserves_fresh_jobs_and_fences_late_results(sessions):
    identifier, task_id = queue(sessions)
    with sessions() as session:
        RunRepository(session).claim(identifier, task_id)
    assert reconcile(sessions) == 0
    with pytest.raises(ValueError):
        reconcile(sessions, age_seconds=HARD_TIME_LIMIT)
    assert RECONCILE_MIN_AGE > HARD_TIME_LIMIT
    assert reconcile(sessions, now=datetime.now(timezone.utc) + timedelta(hours=2)) == 1
    with sessions() as session, pytest.raises(RunConflict):
        RunRepository(session).finish(identifier, {"status": "COMPLETED", "execution_outcome": "PASSED"})


def test_result_commit_failure_is_never_an_execution_retry(sessions):
    identifier, task_id = queue(sessions)

    def fail(session):
        raise SQLAlchemyError("failed final commit")

    service = pipeline()

    def run(url):
        assert sessions.kw["bind"].pool.checkedout() == 0
        event.listen(sessions.class_, "before_commit", fail)
        return service.run.return_value

    service.run.side_effect = run
    try:
        with pytest.raises(SQLAlchemyError):
            run_job(identifier, task_id, sessions, lambda: service)
    finally:
        event.remove(sessions.class_, "before_commit", fail)
    assert run_job(identifier, task_id, sessions, lambda: service) == "IGNORED"
    assert reconcile(sessions, now=datetime.now(timezone.utc) + timedelta(hours=2)) == 1
    service.run.assert_called_once()


def test_celery_entrypoint_uses_service_and_disposes_engine(monkeypatch):
    engine, sessions = Mock(), Mock()
    monkeypatch.setattr("testpilot.worker.tasks.create_database_engine", lambda: engine)
    monkeypatch.setattr("testpilot.worker.tasks.create_session_factory", lambda value: sessions)
    job = Mock(side_effect=RuntimeError("task failed"))
    monkeypatch.setattr("testpilot.worker.tasks.run_job", job)
    with pytest.raises(RuntimeError):
        execute_task.apply(args=[str(uuid4())], task_id=str(uuid4()), throw=True)
    engine.dispose.assert_called_once()


def test_celery_configuration_and_publication_do_not_allow_implicit_retries(monkeypatch):
    assert app.conf.task_acks_late is False
    assert app.conf.worker_prefetch_multiplier == app.conf.worker_concurrency == 1
    assert app.conf.task_publish_retry is False
    assert app.conf.accept_content == ["json"]
    assert app.conf.result_backend is None
    assert execute_task.max_retries == 0
    sender = Mock()
    monkeypatch.setattr(app, "send_task", sender)
    identifier, job = uuid4(), uuid4()
    publish(identifier, job)
    sender.assert_called_once_with("testpilot.execute_run", args=[str(identifier)], task_id=str(job), retry=False)


def test_broker_credentials_are_redacted_from_persisted_diagnostics(monkeypatch):
    from testpilot.api.run_service import sanitized

    monkeypatch.setenv("CELERY_BROKER_URL", "redis://:private-broker-password@localhost/0")
    text = sanitized("redis://:private-broker-password@localhost/0 private-broker-password", 2_000)
    assert "private-broker-password" not in text
