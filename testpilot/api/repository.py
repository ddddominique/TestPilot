"""Persistence operations only; no GitHub, LLM, or Docker calls."""

from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from testpilot.github import GitHubClient

from .db_models import TestRunRecord


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

    def list(self, limit: int, offset: int) -> tuple[list[TestRunRecord], int]:
        rows = self.session.scalars(
            select(TestRunRecord)
            .order_by(TestRunRecord.created_at.desc(), TestRunRecord.id.desc())
            .limit(limit).offset(offset)
        ).all()
        total = self.session.scalar(select(func.count()).select_from(TestRunRecord))
        return list(rows), total
