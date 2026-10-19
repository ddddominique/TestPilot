"""Opt-in Redis delivery with a real Celery worker and fake external pipeline."""

import os
import time
from unittest.mock import Mock
from uuid import uuid4

import pytest
from celery import Celery
from celery.contrib.testing.worker import start_worker
from sqlalchemy import create_engine

from testpilot.api.database import create_session_factory
from testpilot.api.db_models import Base, TestRunRecord
from testpilot.api.repository import RunRepository
from testpilot.execution_models import ExecutionResult, ExecutionStatus
from testpilot.pipeline import PipelineResult
from testpilot.worker.tasks import run_job


@pytest.mark.skipif(not os.getenv("TESTPILOT_TEST_REDIS_URL"), reason="Set TESTPILOT_TEST_REDIS_URL to a dedicated Redis test broker")
def test_real_redis_delivery_duplicate_task_and_durable_result(tmp_path):
    queue_name = "testpilot-test-" + uuid4().hex
    celery = Celery("testpilot-integration", broker=os.environ["TESTPILOT_TEST_REDIS_URL"])
    celery.conf.update(task_default_queue=queue_name, task_ignore_result=True, task_serializer="json",
                       accept_content=["json"], task_publish_retry=False, worker_prefetch_multiplier=1)
    engine = create_engine(f"sqlite:///{tmp_path / 'redis.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)
    pipeline = Mock()
    pipeline.run.return_value = PipelineResult(execution=ExecutionResult(
        "a/b", 1, "a" * 40, "pytest", [], ExecutionStatus.PASSED, passed=1, exit_code=0,
    ))

    @celery.task(bind=True, name=queue_name)
    def task(self, run_id):
        return run_job(run_id, self.request.id, sessions, lambda: pipeline)

    try:
        with sessions() as session:
            identifier = RunRepository(session).create("https://github.com/a/b/pull/1").id
        with sessions() as session:
            job_id = RunRepository(session).queue(identifier).task_id
        # solo is only for this embedded test; production uses prefork time limits.
        with start_worker(celery, perform_ping_check=False, pool="solo", concurrency=1, shutdown_timeout=10):
            for _ in range(2):
                task.apply_async(args=[str(identifier)], task_id=str(job_id), retry=False)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with sessions() as session:
                    row = session.get(TestRunRecord, identifier)
                    if row.status == "COMPLETED":
                        assert row.execution_outcome == "PASSED" and row.passed == 1
                        break
                time.sleep(0.05)
            else:
                pytest.fail("Worker did not persist a result before the deadline")
        pipeline.run.assert_called_once()
    finally:
        # Delete only this test's uniquely named queue, never flush shared Redis.
        with celery.connection_for_write() as connection:
            connection.default_channel.queue_delete(queue=queue_name)
        celery.close()
        engine.dispose()
