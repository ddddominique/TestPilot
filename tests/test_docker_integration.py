"""Explicitly enabled trusted-fixture Docker demonstration; no network or LLM."""

import io
import json
import os
import tarfile
from dataclasses import asdict

import pytest

from testpilot.executor import DockerRunner, TestExecutor
from testpilot.execution_models import ExecutionStatus
from testpilot.generation_models import FrameworkDetection, GenerationResult, TestProposal
from testpilot.models import PullRequest
from testpilot.workspace import WorkspaceManager


pytestmark = pytest.mark.skipif(
    os.getenv("TESTPILOT_DOCKER_INTEGRATION") != "1",
    reason="Set TESTPILOT_DOCKER_INTEGRATION=1 to execute the trusted Docker fixture",
)


class FixtureSnapshot:
    def download_archive(self, owner, repository, sha, destination, max_bytes):
        assert sha == "a" * 40
        # This snapshot stands in for a GitHub archive at a fixed head SHA.
        with tarfile.open(destination, "w:gz") as archive:
            data = b"def add(a, b):\n    return a + b\n"
            entry = tarfile.TarInfo("trusted-fixture/app.py")
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))


@pytest.mark.parametrize("expected_value,status", [(3, ExecutionStatus.PASSED), (99, ExecutionStatus.TEST_FAILED)])
def test_trusted_snapshot_in_docker(expected_value, status):
    docker = DockerRunner()
    unavailable = docker.check_available()
    if unavailable:
        pytest.skip(unavailable)
    pr = PullRequest("fixture", "addition", 1, "trusted demo", "", "b" * 40, "a" * 40, [])
    proposal = TestProposal(
        suggested_test_file="tests/test_add_generated.py",
        target_source_file="app.py", framework="pytest",
        test_code=f"from app import add\n\ndef test_add():\n    assert add(1, 2) == {expected_value}\n",
        explanation="Trusted addition fixture", assumptions=[],
    )
    result = TestExecutor(WorkspaceManager(FixtureSnapshot()), docker).execute(
        pr, FrameworkDetection("python", "pytest"),
        GenerationResult(summary="trusted fixture", proposals=[proposal], warnings=[]),
    )
    print(json.dumps(asdict(result), indent=2))
    assert result.status == status
    assert result.passed == (1 if status == ExecutionStatus.PASSED else 0)
    assert result.failed == (1 if status == ExecutionStatus.TEST_FAILED else 0)
