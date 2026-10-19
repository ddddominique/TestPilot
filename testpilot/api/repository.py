"""Persistence operations only; no GitHub, LLM, or Docker calls."""

from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from testpilot.github import GitHubClient

from .db_models import RunDispatch, TestRunRecord


class RunNotFound(Exception):
    pass


class RunConflict(Exception):
    pass


class RunRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, pr_url: str) -> TestRunRecord:
        owner, name, number = GitHubClient.parse_pr_url(pr_url)
        record = TestRunRecord(
            pr_url=pr_url, repository=f"{owner}/{name}", pr_number=number, status="PENDING",
        )
        # Commit before returning, so a failed transaction cannot produce 201.
        # The context manager rolls back both flush and commit failures.
        with self.session.begin():
            self.session.add(record)
            self.session.flush()
        return record

    def get(self, run_id: UUID) -> Optional[TestRunRecord]:
        return self.session.get(TestRunRecord, run_id)

    def queue(self, run_id: UUID) -> TestRunRecord:
        """Commit eligibility transition and durable dispatch intent together."""
        with self.session.begin():
            row = self.session.scalars(
                update(TestRunRecord)
                .where(TestRunRecord.id == run_id, TestRunRecord.status == "PENDING")
                .values(status="QUEUED", queued_at=func.now(), task_id=uuid4(), updated_at=func.now())
                .returning(TestRunRecord)
            ).first()
            if row is None:
                if self.session.get(TestRunRecord, run_id) is None:
                    raise RunNotFound()
                raise RunConflict("Run has already been queued or started")
            self.session.add(RunDispatch(run_id=run_id))
        return row

    def claim(self, run_id: UUID, task_id: UUID) -> TestRunRecord:
        with self.session.begin():
            row = self.session.scalars(
                update(TestRunRecord)
                .where(TestRunRecord.id == run_id, TestRunRecord.status == "QUEUED", TestRunRecord.task_id == task_id)
                .values(status="RUNNING", started_at=func.now(), updated_at=func.now())
                .returning(TestRunRecord)
            ).first()
            if row is None:
                if self.session.get(TestRunRecord, run_id) is None:
                    raise RunNotFound()
                raise RunConflict("Run has already been started")
        return row

    def finish(self, run_id: UUID, values: dict) -> TestRunRecord:
        with self.session.begin():
            row = self.session.scalars(
                update(TestRunRecord)
                .where(TestRunRecord.id == run_id, TestRunRecord.status == "RUNNING")
                .values(**values, completed_at=func.now(), updated_at=func.now())
                .returning(TestRunRecord)
            ).first()
            if row is None:
                raise RunConflict("Run is no longer running")
        return row

    def list(self, limit: int, offset: int) -> tuple[list[TestRunRecord], int]:
        rows = self.session.scalars(
            select(TestRunRecord)
            .order_by(TestRunRecord.created_at.desc(), TestRunRecord.id.desc())
            .limit(limit).offset(offset)
        ).all()
        total = self.session.scalar(select(func.count()).select_from(TestRunRecord))
        return list(rows), total
