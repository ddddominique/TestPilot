"""HTTP request/response contracts and GitHub URL validation."""

import re
from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from testpilot.execution_models import ExecutionStatus
from testpilot.github import GitHubClient, InvalidPullRequestURL


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pr_url: str = Field(min_length=1, max_length=2048)

    @field_validator("pr_url")
    @classmethod
    def validate_pr_url(cls, value: str) -> str:
        try:
            owner, repository, number = GitHubClient.parse_pr_url(value)
        except InvalidPullRequestURL as exc:
            raise ValueError(str(exc)) from exc
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", owner)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repository)
            or repository in {".", ".."}
            or not 0 < number <= 9_223_372_036_854_775_807
        ):
            raise ValueError("Expected a valid GitHub repository and positive pull request number")
        return f"https://github.com/{owner}/{repository}/pull/{number}"


class RunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    pr_url: str
    repository: str
    pr_number: int
    head_sha: Optional[str]
    status: Literal["PENDING", "QUEUED", "RUNNING", "COMPLETED", "FAILED"]
    queued_at: Optional[datetime]
    task_id: Optional[UUID]
    framework: Optional[str]
    execution_outcome: Optional[ExecutionStatus]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    exit_code: Optional[int]
    duration_seconds: Optional[float]
    generation_summary: Optional[str]
    framework_selection: Optional[str]
    framework_evidence: list[str]
    warnings: list[str]
    passed: int
    failed: int
    skipped: int
    generated_tests: list[dict[str, Any]]
    execution_output: Optional[str]
    error_details: Optional[str]
    created_at: datetime
    updated_at: datetime


class RunPage(BaseModel):
    items: list[RunResponse]
    total: int
    limit: int
    offset: int


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
