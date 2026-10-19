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
    def __init__(self, files=None):
        self.files = files or {"app.py": "def add(a, b):\n    return a + b\n"}

    def download_archive(self, owner, repository, sha, destination, max_bytes):
        assert sha == "a" * 40
        # This snapshot stands in for a GitHub archive at a fixed head SHA.
        with tarfile.open(destination, "w:gz") as archive:
            for path, content in self.files.items():
                data = content.encode("utf-8")
                entry = tarfile.TarInfo("trusted-fixture/" + path)
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


@pytest.mark.parametrize("target", ["leetcode/2089.py", "solutions/python/leetcode/2089.py"])
def test_numeric_standalone_module_from_separate_test_mount(target):
    docker = DockerRunner()
    unavailable = docker.check_available()
    if unavailable:
        pytest.skip(unavailable)
    snapshot = FixtureSnapshot({
        target: (
            "class Solution:\n"
            "    def targetIndices(self, nums, target):\n"
            "        return [i for i, value in enumerate(sorted(nums)) if value == target]\n"
        ),
    })
    code = (
        'import importlib.util\nimport os\nfrom pathlib import Path\nimport sys\n'
        f'source = Path(os.environ["TESTPILOT_SOURCE_ROOT"]) / {target!r}\n'
        'spec = importlib.util.spec_from_file_location("solution_2089", source)\n'
        'module = importlib.util.module_from_spec(spec)\n'
        'sys.modules[spec.name] = module\n'
        'spec.loader.exec_module(module)\n'
        'def test_target_indices():\n'
        '    assert module.Solution().targetIndices([1, 2, 5, 2, 3], 2) == [1, 2]\n'
    )
    item = TestProposal(
        suggested_test_file="tests/generated/test_2089.py", target_source_file=target,
        framework="pytest", test_code=code, explanation="Numeric standalone fixture", assumptions=[],
    )
    result = TestExecutor(WorkspaceManager(snapshot), docker).execute(
        PullRequest("fixture", "numeric", 1, "solution", "", "b" * 40, "a" * 40, []),
        FrameworkDetection("python", "pytest", selection="inferred"),
        GenerationResult(summary="numeric module", proposals=[item], warnings=[]),
    )
    assert result.status == ExecutionStatus.PASSED, result
    assert result.passed == 1
