"""Docker-isolated orchestration for generated pytest proposals."""

import ast
import json
import math
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from .execution_models import ExecutionResult, ExecutionStatus
from .generation_models import FrameworkDetection, GenerationResult
from .models import PullRequest
from .workspace import ExecutionWorkspace, WorkspaceError, WorkspaceManager


DEFAULT_RUNNER_IMAGE = "testpilot-pytest-runner:0.1"
RESULT_MARKER = "TESTPILOT_RESULT_JSON="


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    output: str
    duration_seconds: float
    truncated: bool = False


class CommandTimeout(RuntimeError):
    def __init__(self, output: str, duration_seconds: float) -> None:
        super().__init__("command timed out")
        self.output = output
        self.duration_seconds = duration_seconds


class BoundedCommandBackend:
    """Run fixed host-side Docker CLI commands while draining bounded output."""

    def run(self, command: Sequence[str], timeout: float, max_output: int) -> CommandResult:
        started = time.monotonic()
        process = subprocess.Popen(
            list(command), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0
        )
        chunks: list[bytes] = []
        captured = 0
        truncated = False

        def drain() -> None:
            nonlocal captured, truncated
            assert process.stdout is not None
            while True:
                chunk = process.stdout.read(8192)
                if not chunk:
                    break
                remaining = max_output - captured
                if remaining > 0:
                    selected = chunk[:remaining]
                    chunks.append(selected)
                    captured += len(selected)
                if len(chunk) > max(remaining, 0):
                    truncated = True

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        try:
            exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            reader.join(timeout=2)
            output = b"".join(chunks).decode("utf-8", errors="replace")
            raise CommandTimeout(output, time.monotonic() - started) from exc
        reader.join(timeout=2)
        output = b"".join(chunks).decode("utf-8", errors="replace")
        return CommandResult(exit_code, output, time.monotonic() - started, truncated)


class DockerRunner:
    def __init__(
        self,
        backend: Optional[BoundedCommandBackend] = None,
        image: str = DEFAULT_RUNNER_IMAGE,
        timeout_seconds: float = 60.0,
        max_output_bytes: int = 100_000,
    ) -> None:
        self.backend = backend or BoundedCommandBackend()
        self.image = image
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self._image_id: Optional[str] = None

    def check_available(self) -> Optional[str]:
        if shutil.which("docker") is None and isinstance(self.backend, BoundedCommandBackend):
            return "Docker CLI was not found. Install Docker and build the trusted TestPilot runner image."
        try:
            version = self.backend.run(["docker", "version", "--format", "{{.Server.Version}}"], 10, 4_096)
            if version.exit_code != 0:
                return f"Docker daemon is unavailable: {version.output.strip()}"
            image = self.backend.run(
                ["docker", "image", "inspect", "--format", "{{.Id}}", self.image], 10, 4_096
            )
            if image.exit_code != 0:
                return (
                    f"Trusted runner image {self.image!r} is missing. "
                    "Build it with: docker build -t testpilot-pytest-runner:0.1 runner"
                )
            image_id = image.output.strip()
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
                return "Docker could not resolve the trusted runner to an immutable local image ID"
            self._image_id = image_id
        except (OSError, CommandTimeout) as exc:
            return f"Docker is unavailable: {exc}"
        return None

    def run(self, workspace: ExecutionWorkspace) -> dict:
        container_name = f"testpilot-{uuid.uuid4().hex[:12]}"
        test_paths = [
            f"/workspace/generated/{path.relative_to(workspace.generated).as_posix()}"
            for path in workspace.generated_files
        ]
        command = [
            "docker", "run", "--name", container_name,
            "--pull", "never",
            "--log-driver", "none",
            "--network", "none",
            "--user", "65532:65532",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--memory", "512m",
            "--cpus", "1.0",
            "--pids-limit", "64",
            "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--mount", f"type=bind,src={workspace.source},dst=/workspace/repo,readonly",
            "--mount", f"type=bind,src={workspace.generated},dst=/workspace/generated,readonly",
            "--workdir", "/workspace/repo",
            "--env", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
            "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", "PYTHONNOUSERSITE=1",
            "--env", "PYTHONSAFEPATH=1",
            self._image_id or self.image,
            *test_paths,
        ]
        try:
            result = self.backend.run(command, self.timeout_seconds, self.max_output_bytes)
        except CommandTimeout as exc:
            return {
                "status": ExecutionStatus.TIMEOUT.value,
                "exit_code": None,
                "passed": 0,
                "failed": 0,
                "skipped": 0,
                "duration_seconds": exc.duration_seconds,
                "output": exc.output,
                "error": f"Execution exceeded {self.timeout_seconds:.0f} seconds",
                "warnings": [],
            }
        finally:
            self._remove(container_name)

        payload = self._parse_result(result.output)
        if payload is None or result.exit_code != 0:
            return {
                "status": ExecutionStatus.INFRASTRUCTURE_ERROR.value,
                "exit_code": result.exit_code,
                "passed": 0,
                "failed": 0,
                "skipped": 0,
                "duration_seconds": result.duration_seconds,
                "output": result.output,
                "error": "Trusted runner did not return structured results",
                "warnings": ["Docker output was truncated"] if result.truncated else [],
            }
        if result.truncated:
            payload.setdefault("warnings", []).append("Docker output was truncated")
        return payload

    def _remove(self, container_name: str) -> None:
        try:
            removed = self.backend.run(["docker", "rm", "-f", container_name], 10, 4_096)
            if removed.exit_code != 0 and "No such container" not in removed.output:
                raise OSError(f"Docker removal failed for {container_name}")
        except (OSError, CommandTimeout):
            raise OSError(f"Could not remove Docker container {container_name}; run docker rm -f {container_name}")

    @staticmethod
    def _parse_result(output: str) -> Optional[dict]:
        for line in reversed(output.splitlines()):
            if line.startswith(RESULT_MARKER):
                try:
                    payload = json.loads(line[len(RESULT_MARKER):])
                except json.JSONDecodeError:
                    return None
                return payload if isinstance(payload, dict) else None
        return None


class TestExecutor:
    __test__ = False

    def __init__(self, workspace_manager: WorkspaceManager, docker: DockerRunner) -> None:
        self.workspace_manager = workspace_manager
        self.docker = docker

    def execute(
        self,
        pull_request: PullRequest,
        detection: FrameworkDetection,
        generation: GenerationResult,
    ) -> ExecutionResult:
        files = [proposal.suggested_test_file for proposal in generation.proposals]
        if detection.framework != "pytest":
            return self._result(
                pull_request, detection.framework, files, ExecutionStatus.UNSUPPORTED,
                error=f"Execution for {detection.framework} is not supported; Phase 3 executes pytest only.",
            )
        if not generation.proposals:
            return self._result(
                pull_request, detection.framework, files, ExecutionStatus.COLLECTION_ERROR,
                error="No generated test proposals were available to execute.",
            )
        for proposal in generation.proposals:
            try:
                ast.parse(proposal.test_code, filename=proposal.suggested_test_file)
            except SyntaxError as exc:
                return self._result(
                    pull_request, detection.framework, files, ExecutionStatus.SYNTAX_ERROR,
                    error=f"{proposal.suggested_test_file}: {exc.msg} at line {exc.lineno}",
                )
        unavailable = self.docker.check_available()
        if unavailable:
            return self._result(
                pull_request, detection.framework, files, ExecutionStatus.INFRASTRUCTURE_ERROR,
                error=unavailable,
            )
        try:
            with self.workspace_manager.prepare(pull_request, generation) as workspace:
                payload = self.docker.run(workspace)
        except (WorkspaceError, OSError, CommandTimeout) as exc:
            return self._result(
                pull_request, detection.framework, files, ExecutionStatus.INFRASTRUCTURE_ERROR,
                error=str(exc),
            )
        try:
            status = ExecutionStatus(payload.get("status"))
            counts = [payload.get(name, 0) for name in ("passed", "failed", "skipped")]
            if any(type(count) is not int or count < 0 for count in counts):
                raise ValueError("invalid counts")
            if status == ExecutionStatus.PASSED and (counts[0] == 0 or counts[1] != 0):
                raise ValueError("no passing tests or inconsistent counts")
            duration = float(payload.get("duration_seconds", 0.0))
            if not math.isfinite(duration) or duration < 0:
                raise ValueError("invalid duration")
            if not isinstance(payload.get("warnings", []), list):
                raise ValueError("invalid warnings")
        except (ValueError, TypeError):
            status = ExecutionStatus.INFRASTRUCTURE_ERROR
            payload = {"error": "Runner returned invalid or inconsistent structured results"}
            counts = [0, 0, 0]
            duration = 0.0
        return self._result(
            pull_request,
            detection.framework,
            files,
            status,
            exit_code=payload.get("exit_code"),
            passed=counts[0],
            failed=counts[1],
            skipped=counts[2],
            duration_seconds=duration,
            output=str(payload.get("output", ""))[:100_000],
            error=str(payload.get("error", "")),
            warnings=list(payload.get("warnings", [])),
            docker_started=True,
        )

    @staticmethod
    def _result(
        pull_request: PullRequest,
        framework: str,
        files: list[str],
        status: ExecutionStatus,
        **values,
    ) -> ExecutionResult:
        return ExecutionResult(
            repository=f"{pull_request.owner}/{pull_request.repository}",
            pr_number=pull_request.number,
            head_sha=pull_request.head_sha,
            framework=framework,
            test_files=files,
            status=status,
            **values,
        )
