from pathlib import Path
import sys
import json

import pytest

from testpilot.execution_models import ExecutionStatus
from testpilot.executor import BoundedCommandBackend, CommandResult, CommandTimeout, DockerRunner, TestExecutor
from testpilot.generation_models import FrameworkDetection, GenerationResult, TestProposal
from testpilot.models import PullRequest
from testpilot.workspace import ExecutionWorkspace, WorkspaceError


@pytest.fixture(autouse=True)
def trusted_record(monkeypatch):
    monkeypatch.setattr("testpilot.executor.trusted_image_id", lambda: "sha256:" + "a" * 64)


def pr():
    return PullRequest("a", "b", 4, "title", "", "base", "head", [])


def proposal(code="def test_ok():\n    assert True\n"):
    return TestProposal(
        suggested_test_file="tests/test_generated.py",
        target_source_file="app.py",
        framework="pytest",
        test_code=code,
        explanation="test",
        assumptions=[],
    )


def generation(*items):
    return GenerationResult(summary="generated", proposals=list(items), warnings=[])


class FakeWorkspace:
    def __init__(self):
        self.cleaned = False
        self.source = Path("/tmp/source")
        self.generated = Path("/tmp/generated")
        self.generated_files = [self.generated / "tests/test_generated.py"]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.cleaned = True


class FakeManager:
    def __init__(self, error=None):
        self.workspace = FakeWorkspace()
        self.error = error

    def prepare(self, pull_request, result):
        if self.error:
            raise self.error
        return self.workspace


class FakeDocker:
    def __init__(self, payload=None, unavailable=None):
        self.payload = payload or {
            "status": "PASSED", "exit_code": 0, "passed": 1, "failed": 0,
            "skipped": 0, "duration_seconds": 0.1, "output": "1 passed", "error": "", "warnings": [],
        }
        self.unavailable = unavailable
        self.ran = False

    def check_available(self):
        return self.unavailable

    def run(self, workspace):
        self.ran = True
        return self.payload


@pytest.mark.parametrize(
    ("payload_status", "expected"),
    [
        ("PASSED", ExecutionStatus.PASSED),
        ("TEST_FAILED", ExecutionStatus.TEST_FAILED),
        ("COLLECTION_ERROR", ExecutionStatus.COLLECTION_ERROR),
        ("DEPENDENCY_ERROR", ExecutionStatus.DEPENDENCY_ERROR),
        ("TIMEOUT", ExecutionStatus.TIMEOUT),
        ("INFRASTRUCTURE_ERROR", ExecutionStatus.INFRASTRUCTURE_ERROR),
    ],
)
def test_maps_structured_docker_results_and_always_cleans_up(payload_status, expected):
    manager = FakeManager()
    docker = FakeDocker({
        "status": payload_status,
        "exit_code": None if payload_status == "TIMEOUT" else (0 if payload_status == "PASSED" else 1),
        "passed": 1 if payload_status == "PASSED" else 0,
        "failed": 0 if payload_status == "PASSED" else 1,
        "skipped": 0, "duration_seconds": 1.2, "output": "evidence", "warnings": [],
        "error": "" if payload_status == "PASSED" else "fixture error",
    })
    result = TestExecutor(manager, docker).execute(
        pr(), FrameworkDetection("python", "pytest"), generation(proposal())
    )
    assert result.status == expected
    assert manager.workspace.cleaned


def test_missing_docker_is_actionable_and_does_not_prepare_workspace():
    manager = FakeManager()
    docker = FakeDocker(unavailable="Docker CLI was not found")
    result = TestExecutor(manager, docker).execute(
        pr(), FrameworkDetection("python", "pytest"), generation(proposal())
    )
    assert result.status == ExecutionStatus.INFRASTRUCTURE_ERROR
    assert "Docker" in result.error
    assert not docker.ran


def test_workspace_failure_is_infrastructure_error():
    result = TestExecutor(FakeManager(WorkspaceError("unsafe archive")), FakeDocker()).execute(
        pr(), FrameworkDetection("python", "pytest"), generation(proposal())
    )
    assert result.status == ExecutionStatus.INFRASTRUCTURE_ERROR
    assert "unsafe archive" in result.error


def test_invalid_python_syntax_is_reported_before_docker():
    invalid = proposal().model_copy(update={"test_code": "def bad(:"})
    docker = FakeDocker()
    result = TestExecutor(FakeManager(), docker).execute(
        pr(), FrameworkDetection("python", "pytest"), generation(invalid)
    )
    assert result.status == ExecutionStatus.SYNTAX_ERROR
    assert not docker.ran


def test_jest_and_vitest_are_unsupported_without_starting_docker():
    for framework in ("jest", "vitest"):
        docker = FakeDocker()
        result = TestExecutor(FakeManager(), docker).execute(
            pr(), FrameworkDetection("javascript/typescript", framework), generation()
        )
        assert result.status == ExecutionStatus.UNSUPPORTED
        assert not docker.ran


def test_empty_pytest_generation_is_not_passed():
    result = TestExecutor(FakeManager(), FakeDocker()).execute(
        pr(), FrameworkDetection("python", "pytest"), generation()
    )
    assert result.status == ExecutionStatus.COLLECTION_ERROR


class FakeBackend:
    def __init__(self, run_result=None, timeout=False):
        self.commands = []
        self.run_result = run_result
        self.timeout = timeout

    def run(self, command, timeout, max_output):
        self.commands.append(list(command))
        if command[:3] == ["docker", "version", "--format"]:
            return CommandResult(0, "25.0", 0.01)
        if command[:3] == ["docker", "image", "inspect"]:
            return CommandResult(0, "sha256:" + "a" * 64, 0.01)
        if command[:3] == ["docker", "run", "--name"]:
            if self.timeout:
                raise CommandTimeout("partial", 2.0)
            return self.run_result or CommandResult(1, "startup failed", 0.1)
        return CommandResult(0, "", 0.01)


def docker_workspace(tmp_path):
    source = tmp_path / "source"
    generated = tmp_path / "generated"
    source.mkdir()
    generated.mkdir()
    test = generated / "test_generated.py"
    test.write_text("def test_ok(): assert True")
    return ExecutionWorkspace(tmp_path, source, generated, [test])


def test_docker_command_enforces_required_isolation_flags(tmp_path):
    payload = {
        "status": "PASSED", "exit_code": 0, "passed": 1, "failed": 0,
        "skipped": 0, "duration_seconds": 0.1, "output": "ok", "error": "", "warnings": [],
    }
    backend = FakeBackend(CommandResult(0, "TESTPILOT_RESULT_JSON=" + __import__("json").dumps(payload), 0.2))
    runner = DockerRunner(backend=backend)
    assert runner.run(docker_workspace(tmp_path))["status"] == "PASSED"
    command = next(item for item in backend.commands if item[:2] == ["docker", "run"])
    for required in ("none", "65532:65532", "ALL", "no-new-privileges:true", "512m", "64", "--read-only"):
        assert required in command
    assert not any("TOKEN" in value or "KEY" in value for value in command)
    assert any(item[:3] == ["docker", "rm", "-f"] for item in backend.commands)


def test_docker_timeout_is_bounded_and_container_is_removed(tmp_path):
    backend = FakeBackend(timeout=True)
    payload = DockerRunner(backend=backend, timeout_seconds=1).run(docker_workspace(tmp_path))
    assert payload["status"] == "TIMEOUT"
    assert any(item[:3] == ["docker", "rm", "-f"] for item in backend.commands)


def test_docker_startup_failure_is_infrastructure_error(tmp_path):
    payload = DockerRunner(backend=FakeBackend()).run(docker_workspace(tmp_path))
    assert payload["status"] == "INFRASTRUCTURE_ERROR"
    assert "structured results" in payload["error"]


def test_host_docker_output_capture_is_bounded():
    # Only this trusted literal fixture executes on the host, never repository code.
    result = BoundedCommandBackend().run(
        [sys.executable, "-c", "print('x' * 10000)"], timeout=5, max_output=100
    )
    assert len(result.output) == 100
    assert result.truncated


@pytest.mark.parametrize("payload", [
    {"status": "PASSED", "passed": 0},
    {"status": "PASSED", "passed": "invalid"},
    {"status": "invented", "passed": 1},
])
def test_invalid_runner_payload_fails_closed(payload):
    result = TestExecutor(FakeManager(), FakeDocker(payload)).execute(
        pr(), FrameworkDetection("python", "pytest"), generation(proposal())
    )
    assert result.status == ExecutionStatus.INFRASTRUCTURE_ERROR


def valid_payload():
    return {
        "status": "PASSED", "exit_code": 0, "passed": 1, "failed": 0,
        "skipped": 0, "duration_seconds": 0.1, "output": "ok", "error": "", "warnings": [],
    }


@pytest.mark.parametrize("timeout,status", [(True, "TIMEOUT"), (False, "TEST_FAILED")])
@pytest.mark.parametrize("cleanup_failure", [OSError("daemon disconnected"), CommandTimeout("", 10)])
def test_cleanup_failure_preserves_execution_outcome(tmp_path, timeout, status, cleanup_failure):
    payload = dict(valid_payload(), status="TEST_FAILED", passed=0, failed=1, exit_code=1, error="assertion failed")

    class BrokenCleanup(FakeBackend):
        def run(self, command, timeout, max_output):
            if command[:3] == ["docker", "rm", "-f"]:
                self.commands.append(command)
                raise cleanup_failure
            return super().run(command, timeout, max_output)

    backend = BrokenCleanup(CommandResult(0, "TESTPILOT_RESULT_JSON=" + json.dumps(payload), 0.1), timeout=timeout)
    result = DockerRunner(backend=backend).run(docker_workspace(tmp_path))
    assert result["status"] == status
    container = next(cmd[3] for cmd in backend.commands if cmd[:3] == ["docker", "run", "--name"])
    assert f"docker rm -f {container}" in result["warnings"][0]
    assert result["error"] == ("Execution exceeded 60 seconds" if timeout else "assertion failed")


def test_provenance_uses_recorded_id_instead_of_local_tag(tmp_path):
    backend = FakeBackend(CommandResult(0, "TESTPILOT_RESULT_JSON=" + json.dumps(valid_payload()), 0.1))
    runner = DockerRunner(backend=backend)
    runner.run(docker_workspace(tmp_path))
    inspected = next(cmd for cmd in backend.commands if cmd[:3] == ["docker", "image", "inspect"])
    assert inspected[-1] == "sha256:" + "a" * 64
    executed = next(cmd for cmd in backend.commands if cmd[:2] == ["docker", "run"])
    assert "sha256:" + "a" * 64 in executed
    assert "testpilot-pytest-runner:0.1" not in executed


def test_unexpected_image_id_is_rejected():
    class UnexpectedImage(FakeBackend):
        def run(self, command, timeout, max_output):
            if command[:3] == ["docker", "image", "inspect"]:
                return CommandResult(0, "sha256:" + "b" * 64, 0.1)
            return super().run(command, timeout, max_output)

    assert "does not match" in DockerRunner(backend=UnexpectedImage()).check_available()


@pytest.mark.parametrize("updates", [
    {"exit_code": True}, {"exit_code": "0"}, {"passed": True},
    {"duration_seconds": "0.1"}, {"duration_seconds": float("nan")},
    {"duration_seconds": 10 ** 1000},
    {"output": []}, {"output": "x" * 64_001}, {"error": None},
    {"warnings": [5]}, {"warnings": "warning"}, {"warnings": ["x" * 2001]},
    {"status": "TEST_FAILED", "exit_code": 1, "failed": 0, "error": "failure"},
    {"status": "PASSED", "exit_code": 1}, {"status": "PASSED", "failed": 1},
    {"status": "COLLECTION_ERROR", "exit_code": 5, "error": "empty", "passed": 1},
])
def test_complete_payload_validation_rejects_invalid_fields(updates):
    payload = dict(valid_payload(), **updates)
    result = TestExecutor(FakeManager(), FakeDocker(payload)).execute(
        pr(), FrameworkDetection("python", "pytest"), generation(proposal())
    )
    assert result.status == ExecutionStatus.INFRASTRUCTURE_ERROR


@pytest.mark.parametrize("missing", list(valid_payload()))
def test_every_payload_field_is_required(missing):
    payload = valid_payload()
    del payload[missing]
    result = TestExecutor(FakeManager(), FakeDocker(payload)).execute(
        pr(), FrameworkDetection("python", "pytest"), generation(proposal())
    )
    assert result.status == ExecutionStatus.INFRASTRUCTURE_ERROR


def test_malformed_and_oversized_wire_results_are_rejected():
    assert DockerRunner._parse_result("TESTPILOT_RESULT_JSON={invalid") is None
    assert DockerRunner._parse_result("x" * 100_001) is None
    assert DockerRunner._parse_result('TESTPILOT_RESULT_JSON={"status":"PASSED","status":"TEST_FAILED"}') is None


def test_passed_result_with_full_warning_list_survives_nonzero_cleanup(tmp_path):
    payload = dict(valid_payload(), warnings=["fixture warning"] * 32)

    class FailedRemoval(FakeBackend):
        def run(self, command, timeout, max_output):
            if command[:3] == ["docker", "rm", "-f"]:
                self.commands.append(command)
                return CommandResult(1, "permission denied", 0.1)
            return super().run(command, timeout, max_output)

    backend = FailedRemoval(CommandResult(0, "TESTPILOT_RESULT_JSON=" + json.dumps(payload), 0.1))
    runner = DockerRunner(backend=backend)
    workspace = docker_workspace(tmp_path)

    class Manager:
        def prepare(self, *args):
            return workspace

    result = TestExecutor(Manager(), runner).execute(
        pr(), FrameworkDetection("python", "pytest"), generation(proposal())
    )
    assert result.status == ExecutionStatus.PASSED
    assert result.passed == 1
    assert len(result.warnings) == 33
    assert "docker rm -f testpilot-" in result.warnings[-1]


def test_missing_provenance_prevents_docker_run(tmp_path, monkeypatch):
    def missing_record():
        raise ValueError("Runner provenance missing; python -m testpilot.build_runner")

    monkeypatch.setattr("testpilot.executor.trusted_image_id", missing_record)
    backend = FakeBackend()
    runner = DockerRunner(backend=backend)
    with pytest.raises(OSError, match="provenance missing"):
        runner.run(docker_workspace(tmp_path))
    assert not any(cmd[:2] == ["docker", "run"] for cmd in backend.commands)


def test_execution_preserves_framework_selection_evidence_and_warnings():
    selected = FrameworkDetection(
        "python", "pytest", ["All changed sources are Python"],
        ["pytest inferred without existing framework evidence"], selection="inferred",
    )
    result = TestExecutor(FakeManager(), FakeDocker()).execute(pr(), selected, generation(proposal()))
    assert result.status == ExecutionStatus.PASSED
    assert result.framework == "pytest"
    assert result.framework_selection == "inferred"
    assert result.framework_evidence == selected.evidence
    assert selected.warnings[0] in result.warnings
