"""REST routes for persistent runs and durable asynchronous submission."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from .database import get_session
from .repository import RunRepository, RunConflict, RunNotFound
from .schemas import CreateRunRequest, HealthResponse, RunPage, RunResponse
from testpilot.worker.celery_app import app as celery_app


router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Basic liveness; does not claim database readiness."""
    return HealthResponse()


@router.post("/api/runs", response_model=RunResponse, status_code=status.HTTP_201_CREATED)
def create_run(body: CreateRunRequest, response: Response, session: Session = Depends(get_session)):
    record = RunRepository(session).create(body.pr_url)
    response.headers["Location"] = f"/api/runs/{record.id}"
    return record


@router.get("/api/runs", response_model=RunPage)
def list_runs(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
    session: Session = Depends(get_session),
) -> RunPage:
    rows, total = RunRepository(session).list(limit, offset)
    return RunPage(items=[RunResponse.model_validate(row) for row in rows], total=total, limit=limit, offset=offset)


@router.get("/api/runs/{run_id}", response_model=RunResponse)
def get_run(run_id: UUID, session: Session = Depends(get_session)):
    record = RunRepository(session).get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return record


@router.post("/api/runs/{run_id}/execute", response_model=RunResponse, status_code=status.HTTP_202_ACCEPTED)
def execute_existing_run(run_id: UUID, response: Response, session: Session = Depends(get_session)):
    try:
        record = RunRepository(session).queue(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    except RunConflict:
        raise HTTPException(status_code=409, detail="Run has already been started; automatic retries are disabled")
    try:
        # send_task does not execute eagerly in the API. The queue transaction
        # has committed and released its connection before broker I/O.
        celery_app.send_task("testpilot.execute_run", args=[str(record.id)],
                             task_id=str(record.task_id), retry=False)
    except Exception:
        RunRepository(session).publication_failed(record.id, record.task_id)
        raise HTTPException(status_code=503, detail="Task publication failed or acknowledgement was uncertain. "
                            "Check the run status before creating a new run.") from None
    response.headers["Location"] = f"/api/runs/{record.id}"
    return record
