"""SQLAlchemy models; PostgreSQL is the runtime database."""

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Integer, JSON, String, Text, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from testpilot.execution_models import ExecutionStatus


RUN_STATUSES = ["PENDING", *(status.value for status in ExecutionStatus)]


class Base(DeclarativeBase):
    pass


class TestRunRecord(Base):
    __test__ = False
    __tablename__ = "test_runs"
    __table_args__ = (
        CheckConstraint("pr_number > 0", name="ck_test_runs_pr_number_positive"),
        CheckConstraint("passed >= 0 AND failed >= 0 AND skipped >= 0", name="ck_test_runs_counts_nonnegative"),
        CheckConstraint(
            "status IN (" + ", ".join(repr(value) for value in RUN_STATUSES) + ")",
            name="ck_test_runs_status",
        ),
        Index("ix_test_runs_created_at_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    pr_url: Mapped[str] = mapped_column(String(2048))
    repository: Mapped[str] = mapped_column(String(256))
    pr_number: Mapped[int] = mapped_column(BigInteger)
    head_sha: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", server_default="PENDING")
    framework: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    passed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    skipped: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # SQLite variant is only for fast offline tests. PostgreSQL uses JSONB.
    generated_tests: Mapped[list] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=list, server_default=text("'[]'"),
    )
    execution_output: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )
