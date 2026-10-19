"""Test trusted runner classification by mocking pytest; no test code executes."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def runner():
    path = Path(__file__).parents[1] / "runner" / "pytest_runner.py"
    spec = importlib.util.spec_from_file_location("trusted_pytest_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("outcome,expected", [
    ("passed", "PASSED"), ("failed", "TEST_FAILED"),
    ("dependency", "DEPENDENCY_ERROR"), ("collection", "COLLECTION_ERROR"),
    ("skipped", "COLLECTION_ERROR"), ("empty", "COLLECTION_ERROR"),
])
def test_trusted_runner_classification(runner, monkeypatch, capsys, outcome, expected):
    def fake_pytest(args, plugins):
        assert "--noconftest" in args
        assert args[args.index("-c") + 1] == "/dev/null"
        assert args[-1] == "/workspace/generated/test_fixture.py"
        plugin = plugins[0]
        if outcome in {"dependency", "collection"}:
            plugin.pytest_collectreport(SimpleNamespace(
                failed=True,
                longrepr=SimpleNamespace(reprcrash=SimpleNamespace(
                    message="ModuleNotFoundError: missing" if outcome == "dependency" else "bad collection"
                )),
            ))
            return 2
        if outcome == "empty":
            return 5
        plugin.pytest_runtest_logreport(SimpleNamespace(
            when="call", passed=outcome == "passed", failed=outcome == "failed",
            skipped=outcome == "skipped", longrepr=None,
        ))
        return 1 if outcome == "failed" else 0

    monkeypatch.setattr(runner.pytest, "main", fake_pytest)
    monkeypatch.setattr(runner.sys, "argv", ["runner", "/workspace/generated/test_fixture.py"])
    # Restore sys.path after the trusted runner adds fixed container paths.
    monkeypatch.setattr(runner.sys, "path", list(runner.sys.path))
    assert runner.main() == 0
    output = capsys.readouterr().out
    payload = json.loads(output.split(runner.MARKER, 1)[1])
    assert payload["status"] == expected


def test_runner_writer_bounds_output(runner):
    writer = runner.BoundedWriter(20)
    writer.write("x" * 100)
    assert writer.size == 20
    assert writer.truncated
    assert writer.getvalue().startswith("x" * 20)
