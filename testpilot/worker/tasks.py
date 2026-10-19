"""Only run/task IDs cross the broker; no source, results, or credentials."""

from uuid import UUID

from testpilot.api.database import create_database_engine, create_session_factory
from testpilot.api.repository import RunConflict, RunNotFound
from testpilot.api.run_service import execute_run
from testpilot.pipeline import PipelineService

from .celery_app import app


def run_job(run_id, task_id, sessions, pipeline_factory=PipelineService):
    try:
        record = execute_run(UUID(str(run_id)), sessions, pipeline_factory, UUID(str(task_id)))
        return record.status
    except (RunConflict, RunNotFound):
        # Duplicate, stale, or forged identity: never repeat external work.
        return "IGNORED"


@app.task(bind=True, name="testpilot.execute_run", max_retries=0, ignore_result=True)
def execute_task(self, run_id):
    engine = create_database_engine()
    try:
        return run_job(run_id, self.request.id, create_session_factory(engine))
    finally:
        engine.dispose()
