"""Structured results produced by isolated test execution."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ExecutionStatus(str, Enum):
    PASSED = "PASSED"
    TEST_FAILED = "TEST_FAILED"
    COLLECTION_ERROR = "COLLECTION_ERROR"
    SYNTAX_ERROR = "SYNTAX_ERROR"
    DEPENDENCY_ERROR = "DEPENDENCY_ERROR"
    TIMEOUT = "TIMEOUT"
    INFRASTRUCTURE_ERROR = "INFRASTRUCTURE_ERROR"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class ExecutionResult:
    repository: str
    pr_number: int
    head_sha: str
    framework: str
    test_files: list[str]
    status: ExecutionStatus
    exit_code: Optional[int] = None
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    duration_seconds: float = 0.0
    output: str = ""
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    docker_started: bool = False

    @property
    def successful(self) -> bool:
        return self.status == ExecutionStatus.PASSED
