"""Keep the lightweight dashboard's TypeScript contract aligned with FastAPI."""

from pathlib import Path
import re
from typing import get_args

from testpilot.api.schemas import RunResponse
from testpilot.execution_models import ExecutionStatus


def test_frontend_run_fields_and_status_vocabularies_match_fastapi():
    source = (Path(__file__).resolve().parents[1] / "frontend/lib/api.ts").read_text()
    interface = source.split("export interface Run {", 1)[1].split("\n}", 1)[0]
    fields = set(re.findall(r"\b(\w+)\s*:", interface))
    assert fields == set(RunResponse.model_fields)
    lifecycle = source.split("export type RunStatus =", 1)[1].split(";", 1)[0]
    outcomes = source.split("export type ExecutionOutcome =", 1)[1].split(";", 1)[0]
    assert set(re.findall(r'"([A-Z_]+)"', lifecycle)) == set(get_args(RunResponse.model_fields["status"].annotation))
    assert set(re.findall(r'"([A-Z_]+)"', outcomes)) == {status.value for status in ExecutionStatus}
