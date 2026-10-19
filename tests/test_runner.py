"""Test trusted runner classification by mocking pytest; no test code executes."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.delenv("TESTPILOT_SOURCE_ROOT", raising=False)
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
        assert runner.os.environ["TESTPILOT_SOURCE_ROOT"] == "/workspace/repo"
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
            if outcome == "dependency":
                plugin.pytest_exception_interact(None, SimpleNamespace(
                    excinfo=SimpleNamespace(value=ModuleNotFoundError("No module named 'external_pkg'", name="external_pkg"))
                ), SimpleNamespace(nodeid="test_fixture.py"))
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


def test_generic_import_error_is_not_classified_as_dependency(runner):
    plugin = runner.ResultPlugin()
    error = ImportError("cannot import name 'invented_function' from 'app'", name="app")
    plugin.pytest_exception_interact(None, SimpleNamespace(excinfo=SimpleNamespace(value=error)),
                                    SimpleNamespace(nodeid="test_generated.py::test_app"))
    assert plugin.dependency_errors == 0
    assert "invented_function" in plugin.import_diagnostics[0]
    assert "test_generated.py::test_app" in plugin.import_diagnostics[0]


def test_missing_project_submodule_preserves_project_diagnostic(runner, monkeypatch):
    monkeypatch.setattr(runner.Path, "exists", lambda path: str(path) == "/workspace/repo/app")
    plugin = runner.ResultPlugin()
    error = ModuleNotFoundError("No module named 'app.invented'", name="app.invented")
    plugin.pytest_exception_interact(None, SimpleNamespace(excinfo=SimpleNamespace(value=error)),
                                    SimpleNamespace(nodeid="test_generated.py"))
    assert plugin.dependency_errors == 0
    assert "project module/import error" in plugin.import_diagnostics[0]
    assert "app.invented" in plugin.import_diagnostics[0]


def test_external_missing_module_keeps_generated_import_uncertainty(runner):
    plugin = runner.ResultPlugin()
    error = ModuleNotFoundError("No module named 'unavailable_pkg'", name="unavailable_pkg")
    plugin.pytest_exception_interact(None, SimpleNamespace(excinfo=SimpleNamespace(value=error)),
                                    SimpleNamespace(nodeid="test_generated.py"))
    assert plugin.dependency_errors == 1
    assert "dependency or incorrect generated import" in plugin.import_diagnostics[0]
