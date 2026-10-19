from unittest.mock import Mock

from testpilot.cli import format_generation_summary, format_summary, main
from testpilot.generation_models import FrameworkDetection, GenerationResult, TestProposal
from testpilot.models import ChangedFile, CollectedContext, ContextFile, PullRequest


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
    client = type("Client", (), {"get_pull_request": lambda self, url: pr})()
    monkeypatch.setattr("testpilot.cli.GitHubClient", lambda: client)
    monkeypatch.setattr("testpilot.cli.ContextCollector", lambda *args: type(
        "Collector", (), {"collect": lambda self, value: context}
    )())
    monkeypatch.setattr("testpilot.cli.TestGenerator", Mock(side_effect=AssertionError("not called")))
    assert main(["--pr", "https://github.com/a/b/pull/1"]) == 0
    assert "Pull request: a/b#1" in capsys.readouterr().out


def test_cli_generate_tests_uses_generator(monkeypatch, capsys):
    pr = PullRequest("a", "b", 1, "title", "", "base", "head", [])
    context = CollectedContext([], ["src/a.py"], [], [], [], [], max_characters=100)
    detection = FrameworkDetection("python", "pytest", ["config"])
    result = GenerationResult(summary="done", proposals=[], warnings=[])
    client = type("Client", (), {"get_pull_request": lambda self, url: pr})()
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
