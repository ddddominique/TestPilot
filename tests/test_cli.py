from unittest.mock import Mock

import pytest

from testpilot.cli import format_execution_summary, format_generation_summary, format_summary, main
from testpilot.execution_models import ExecutionResult, ExecutionStatus
from testpilot.generation_models import FrameworkDetection, GenerationResult, TestProposal
from testpilot.models import ChangedFile, CollectedContext, ContextFile, PullRequest


def test_cli_closes_owned_session_on_pipeline_failure(monkeypatch, capsys):
    from testpilot.github import GitHubAPIError

    client = Mock()
    client.get_pull_request.side_effect = GitHubAPIError("offline")
    monkeypatch.setattr("testpilot.cli.GitHubClient", lambda: client)
    assert main(["--pr", "https://github.com/a/b/pull/1"]) == 1
    client.session.close.assert_called_once_with()
    assert "offline" in capsys.readouterr().err


def test_summary_is_readable_and_reports_omissions():
    pr = PullRequest(
        "a", "b", 2, "A title", "", "base", "head",
        [ChangedFile("src/a.py", "modified", 2, 1, 3, None)],
    )
    context = CollectedContext(
        [ContextFile("src/a.py", "source", "code")], ["src/a.py"], [], [],
        ["tests/test_a.py"], ["one warning"], max_characters=100,
    )
    output = format_summary(pr, context)
    assert "a/b#2" in output
    assert "patch unavailable" in output
    assert "Omitted files (1)" in output
    assert "one warning" in output
    assert "4/100 characters" in output


def test_generation_summary_labels_code_as_unexecuted():
    result = GenerationResult(
        summary="summary",
        proposals=[TestProposal(
            suggested_test_file="tests/test_a.py", target_source_file="src/a.py",
            framework="pytest", test_code="def test_a(): pass", explanation="coverage",
            assumptions=[],
        )], warnings=[],
    )
    output = format_generation_summary(FrameworkDetection("python", "pytest", ["config"]), result)
    assert "proposal only; not executed" in output
    assert "tests/test_a.py" in output


def test_cli_without_generate_tests_preserves_phase_one(monkeypatch, capsys):
    pr = PullRequest("a", "b", 1, "title", "", "base", "head", [])
    context = CollectedContext([], [], [], [], [], [], max_characters=100)
    client = type("Client", (), {"get_pull_request": lambda self, url: pr, "session": Mock()})()
    monkeypatch.setattr("testpilot.cli.GitHubClient", lambda: client)
    monkeypatch.setattr("testpilot.cli.ContextCollector", lambda *args: type(
        "Collector", (), {"collect": lambda self, value: context}
    )())
    monkeypatch.setattr("testpilot.cli.TestGenerator", Mock(side_effect=AssertionError("not called")))
    assert main(["--pr", "https://github.com/a/b/pull/1"]) == 0
    client.session.close.assert_called_once_with()
    assert "Pull request: a/b#1" in capsys.readouterr().out


def test_cli_generate_tests_uses_generator(monkeypatch, capsys):
    pr = PullRequest("a", "b", 1, "title", "", "base", "head", [])
    context = CollectedContext([], ["src/a.py"], [], [], [], [], max_characters=100)
    detection = FrameworkDetection("python", "pytest", ["config"])
    result = GenerationResult(summary="done", proposals=[], warnings=[])
    client = type("Client", (), {"get_pull_request": lambda self, url: pr, "session": Mock()})()
    generator = Mock()
    generator.generate.return_value = result
    monkeypatch.setattr("testpilot.cli.GitHubClient", lambda: client)
    monkeypatch.setattr("testpilot.cli.ContextCollector", lambda *args: type(
        "Collector", (), {"collect": lambda self, value: context}
    )())
    monkeypatch.setattr("testpilot.cli.detect_framework", lambda value: detection)
    monkeypatch.setattr("testpilot.cli.TestGenerator", lambda **kwargs: generator)
    assert main(["--pr", "https://github.com/a/b/pull/1", "--generate-tests"]) == 0
    assert "Detected framework: pytest" in capsys.readouterr().out
    generator.generate.assert_called_once_with(pr, context, detection)


def test_execute_requires_generate_tests():
    with pytest.raises(SystemExit) as error:
        main(["--pr", "https://github.com/a/b/pull/1", "--execute"])
    assert error.value.code == 2


def test_cli_execute_prints_results_and_returns_nonzero_on_failure(monkeypatch, capsys):
    pr = PullRequest("a", "b", 1, "title", "", "base", "head", [])
    context = CollectedContext([], ["src/a.py"], [], [], [], [], max_characters=100)
    detection = FrameworkDetection("python", "pytest", ["config"])
    generated = GenerationResult(summary="done", proposals=[], warnings=[])
    execution = ExecutionResult(
        "a/b", 1, "head", "pytest", [], ExecutionStatus.TEST_FAILED,
        exit_code=1, failed=1, output="assertion failed",
        error="One or more generated tests failed.",
    )
    client = type("Client", (), {"get_pull_request": lambda self, url: pr, "session": Mock()})()
    generator = Mock()
    generator.generate.return_value = generated
    executor = Mock()
    executor.execute.return_value = execution
    monkeypatch.setattr("testpilot.cli.GitHubClient", lambda: client)
    monkeypatch.setattr("testpilot.cli.ContextCollector", lambda *args: type(
        "Collector", (), {"collect": lambda self, value: context}
    )())
    monkeypatch.setattr("testpilot.cli.detect_framework", lambda value: detection)
    monkeypatch.setattr("testpilot.cli.TestGenerator", lambda **kwargs: generator)
    monkeypatch.setattr("testpilot.cli.WorkspaceManager", lambda value: Mock())
    monkeypatch.setattr("testpilot.cli.DockerRunner", lambda: Mock())
    monkeypatch.setattr("testpilot.cli.TestExecutor", lambda *args: executor)
    code = main([
        "--pr", "https://github.com/a/b/pull/1", "--generate-tests", "--execute",
    ])
    output = capsys.readouterr().out
    assert code == 1
    assert "Generation summary" in output
    assert "Overall status: TEST_FAILED" in output
    assert "assertion failed" in output


def test_execution_summary_identifies_exact_head_and_counts():
    result = ExecutionResult(
        "a/b", 2, "abc123", "pytest", ["tests/test_x.py"], ExecutionStatus.PASSED,
        exit_code=0, passed=2, skipped=1, duration_seconds=0.5,
    )
    output = format_execution_summary(result)
    assert "a/b#2" in output
    assert "Head commit: abc123" in output
    assert "2 passed, 0 failed, 1 skipped" in output


@pytest.mark.parametrize("override,expected", [(None, "inferred"), ("pytest", "override")])
def test_cli_selects_framework_for_coding_exercise_pr(monkeypatch, capsys, override, expected):
    pr = PullRequest("ddddominique", "Coding-Exercise", 1, "Add solution", "", "base", "head", [])
    collected = CollectedContext(
        [ContextFile("leetcode/2089.py", "source", "class Solution: pass")],
        ["leetcode/2089.py"], [], [], [], repository_files=["old.cpp", "leetcode/2089.py"],
    )
    github = Mock()
    github.get_pull_request.return_value = pr
    collector = Mock()
    collector.collect.return_value = collected
    generator = Mock()
    generator.generate.return_value = GenerationResult(summary="proposal", proposals=[], warnings=[])
    monkeypatch.setattr("testpilot.cli.GitHubClient", lambda: github)
    monkeypatch.setattr("testpilot.cli.ContextCollector", lambda *args: collector)
    monkeypatch.setattr("testpilot.cli.TestGenerator", lambda **kwargs: generator)
    arguments = ["--pr", "https://github.com/ddddominique/Coding-Exercise/pull/1", "--generate-tests"]
    if override:
        arguments += ["--framework", override]
    assert main(arguments) == 0
    selected = generator.generate.call_args.args[2]
    assert selected.framework == "pytest"
    assert selected.selection == expected
    assert f"Framework selection: {expected}" in capsys.readouterr().out


def test_cli_rejects_incompatible_override_before_generation(monkeypatch, capsys):
    github = Mock()
    collector = Mock()
    collector.collect.return_value = CollectedContext([], ["app.py"], [], [], [])
    generator = Mock()
    monkeypatch.setattr("testpilot.cli.GitHubClient", lambda: github)
    monkeypatch.setattr("testpilot.cli.ContextCollector", lambda *args: collector)
    monkeypatch.setattr("testpilot.cli.TestGenerator", generator)
    assert main(["--pr", "https://github.com/a/b/pull/1", "--generate-tests", "--framework", "jest"]) == 1
    assert "incompatible" in capsys.readouterr().err
    generator.assert_not_called()
