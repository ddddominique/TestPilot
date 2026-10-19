"""Local launcher checks without starting Docker or application services."""

import importlib.util
from pathlib import Path
import signal
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dev_launcher", ROOT / "scripts/dev.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def test_zsh_wrapper_loads_root_env_and_uses_project_python(tmp_path):
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts/dev.zsh"
    script.write_text((ROOT / "scripts/dev.zsh").read_text())
    (tmp_path / ".env").write_text("DATABASE_URL='configured database'\nGITHUB_TOKEN=test-placeholder\n")
    (tmp_path / ".venv/bin").mkdir(parents=True)
    python = tmp_path / ".venv/bin/python"
    python.write_text('#!/bin/sh\nprintf "%s|%s|%s" "$DATABASE_URL" "$GITHUB_TOKEN" "$1"\n')
    python.chmod(0o700)
    result = subprocess.run(["/bin/zsh", str(script)], cwd="/", text=True, capture_output=True, check=True)
    assert result.stdout == "configured database|test-placeholder|scripts/dev.py"


def test_zsh_missing_environment_is_actionable(tmp_path):
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts/dev.zsh"
    script.write_text((ROOT / "scripts/dev.zsh").read_text())
    result = subprocess.run(["/bin/zsh", str(script)], text=True, capture_output=True)
    assert result.returncode == 1
    assert "Missing .env" in result.stderr


def test_launch_commands_environment_and_process_groups(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    (tmp_path / "frontend/node_modules/.bin").mkdir(parents=True)
    (tmp_path / "frontend/node_modules/.bin/next").touch()
    monkeypatch.setenv("DATABASE_URL", "test-database")
    monkeypatch.setenv("POSTGRES_PASSWORD", "test-password")
    monkeypatch.setenv("OPENAI_API_KEY", "test-api-key")
    monkeypatch.setattr(launcher.shutil, "which", lambda command: command)
    processes = [Mock(pid=10 + i) for i in range(5)]
    for i, process in enumerate(processes):
        process.poll.return_value = 0 if i == 0 else None
        process.returncode = 0
    popen = Mock(side_effect=processes)
    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    dev = launcher.Dev()
    monkeypatch.setattr(launcher.time, "sleep", lambda delay: setattr(dev, "stopping", True))
    dev.run()
    commands = [call.args[0] for call in popen.call_args_list]
    assert commands[0] == ["docker", "compose", "up", "-d", "--wait", "postgres", "redis"]
    assert commands[1][:4] == ["docker", "compose", "logs", "--follow"]
    assert commands[2][0] == str(tmp_path / ".venv/bin/python")
    assert "uvicorn" in commands[2] and "celery" in commands[3]
    assert commands[4] == ["npm", "run", "dev", "--", "--port", "3000"]
    assert all(call.kwargs["start_new_session"] for call in popen.call_args_list)
    assert popen.call_args_list[2].kwargs["env"] is None  # Inherit exported root .env.
    assert popen.call_args_list[3].kwargs["env"] is None
    assert "OPENAI_API_KEY" not in popen.call_args_list[4].kwargs["env"]
    assert "DATABASE_URL" not in popen.call_args_list[4].kwargs["env"]
    assert popen.call_args_list[4].kwargs["cwd"] == tmp_path / "frontend"


def test_shutdown_warms_worker_before_stopping_docker_and_kills_web_descendants(monkeypatch):
    monkeypatch.setattr(launcher.signal, "signal", Mock())
    events = []
    worker, web = Mock(pid=100), Mock(pid=101)
    worker.poll.return_value = None
    worker.terminate.side_effect = lambda: events.append("worker-term")
    worker.wait.side_effect = lambda: events.append("worker-finished")
    web.wait.side_effect = [subprocess.TimeoutExpired("npm", 10), 0]
    monkeypatch.setattr(launcher.os, "killpg", lambda pid, sig: events.append((pid, sig)))
    docker = Mock(return_value=Mock(returncode=0))
    docker.side_effect = lambda *args, **kwargs: events.append("docker-stop") or Mock(returncode=0)
    monkeypatch.setattr(launcher.subprocess, "run", docker)
    dev = launcher.Dev()
    dev.children = [("Celery", worker), ("Next.js", web)]
    dev.compose_started = True
    dev.shutdown()
    assert "worker-term" in events
    assert events.index("worker-finished") < events.index("docker-stop")
    assert (web.pid, signal.SIGTERM) in events and (web.pid, signal.SIGKILL) in events
    assert events.index("worker-finished") < events.index((worker.pid, signal.SIGKILL))
    docker.assert_called_once_with(["docker", "compose", "stop", "postgres", "redis"], cwd=launcher.ROOT)


def test_startup_failure_still_cleans_up(monkeypatch):
    dev = launcher.Dev()
    monkeypatch.setattr(launcher, "Dev", lambda: dev)
    monkeypatch.setattr(launcher.signal, "signal", Mock())
    monkeypatch.setattr(dev, "run", Mock(side_effect=RuntimeError("startup failed")))
    cleanup = Mock()
    monkeypatch.setattr(dev, "shutdown", cleanup)
    assert launcher.main() == 1
    cleanup.assert_called_once()


def test_interrupt_prevents_additional_children():
    dev = launcher.Dev()
    dev.interrupted(signal.SIGINT, None)
    with pytest.raises(InterruptedError):
        dev.start("not started", ["irrelevant"])


def test_missing_dependency_is_reported_before_docker_start(monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
    dev = launcher.Dev()
    with pytest.raises(RuntimeError, match="docker is not on PATH"):
        dev.run()
    assert not dev.compose_started


def test_shutdown_reaches_real_descendant_processes(monkeypatch, tmp_path):
    # No ports or services: verify POSIX process-group behavior on the host.
    monkeypatch.setattr(launcher.signal, "signal", Mock())
    child_ready, stopped = tmp_path / "ready", tmp_path / "stopped"
    child = (
        "import signal,time,sys; from pathlib import Path; "
        f"signal.signal(signal.SIGTERM, lambda *_: (Path({str(stopped)!r}).touch(), sys.exit(0))); "
        f"Path({str(child_ready)!r}).touch(); time.sleep(60)"
    )
    parent = (
        "import subprocess,signal,time,sys; "
        f"child=subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "signal.signal(signal.SIGTERM, lambda *_: (child.wait(timeout=3), sys.exit(0))); time.sleep(60)"
    )
    dev = launcher.Dev()
    process = dev.start("Next.js", [sys.executable, "-c", parent])
    try:
        deadline = time.monotonic() + 5
        while not child_ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert child_ready.exists()
        dev.shutdown()
        assert process.poll() is not None
        assert stopped.exists(), "grandchild did not receive graceful termination"
    finally:
        dev.group_signal(process, signal.SIGKILL)
        process.wait(timeout=5)
