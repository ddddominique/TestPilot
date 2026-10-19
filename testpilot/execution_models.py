"""Structured results produced by isolated test execution."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import math


def validate_runner_payload(payload: object, max_warnings: int = 32) -> dict:
    """Validate the entire bounded wire format without coercing values."""
    required = {"status", "exit_code", "passed", "failed", "skipped", "duration_seconds", "output", "error", "warnings"}
    if type(payload) is not dict or set(payload) != required:
        raise ValueError("Runner payload must contain exactly the required fields")
    if type(payload["status"]) is not str:
        raise ValueError("Runner status must be a string")
    status = ExecutionStatus(payload["status"])
    code = payload["exit_code"]
    if code is not None and (type(code) is not int or not 0 <= code <= 255):
        raise ValueError("Invalid runner exit code")
    counts = [payload[key] for key in ("passed", "failed", "skipped")]
    if any(type(value) is not int or not 0 <= value <= 1_000_000 for value in counts):
        raise ValueError("Invalid runner test counts")
    duration = payload["duration_seconds"]
    if (
        type(duration) not in {int, float}
        or not 0 <= duration <= 86_400
        or not math.isfinite(duration)
    ):
        raise ValueError("Invalid runner duration")
    for key, limit in (("output", 64_000), ("error", 4_000)):
        if type(payload[key]) is not str or len(payload[key].encode("utf-8")) > limit:
            raise ValueError(f"Invalid or oversized runner {key}")
    warnings = payload["warnings"]
    if type(warnings) is not list or len(warnings) > max_warnings or any(
        type(item) is not str or len(item.encode("utf-8")) > 2_000 for item in warnings
    ):
        raise ValueError("Invalid or oversized runner warnings")
    passed, failed, skipped = counts
    if status == ExecutionStatus.PASSED:
        valid = code == 0 and passed > 0 and failed == 0 and payload["error"] == ""
    elif status == ExecutionStatus.TEST_FAILED:
        valid = code == 1 and failed > 0 and bool(payload["error"])
    elif status == ExecutionStatus.DEPENDENCY_ERROR:
        valid = code in {1, 2} and bool(payload["error"])
    elif status == ExecutionStatus.COLLECTION_ERROR:
        valid = code in {0, 1, 2, 3, 4, 5} and bool(payload["error"])
        if code == 0:
            valid = valid and passed == failed == 0
        if code == 5:
            valid = valid and passed == failed == skipped == 0
    elif status == ExecutionStatus.TIMEOUT:
        valid = code is None and bool(payload["error"])
    elif status == ExecutionStatus.INFRASTRUCTURE_ERROR:
        valid = bool(payload["error"])
    else:
        valid = False  # Syntax/unsupported are host-side outcomes, not runner outcomes.
    if not valid:
        raise ValueError("Runner status, counts, error, and exit code are inconsistent")
    return payload


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
    framework_selection: str = "detected"
    framework_evidence: list[str] = field(default_factory=list)

    @property
    def successful(self) -> bool:
        return self.status == ExecutionStatus.PASSED
