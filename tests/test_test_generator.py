from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from openai import OpenAIError
from pydantic import ValidationError

from testpilot.generation_models import FrameworkDetection, GenerationResult, TestProposal
from testpilot.models import ChangedFile, CollectedContext, ContextFile, ContextPatch, PullRequest
from testpilot.test_generator import (
    SYSTEM_PROMPT,
    TestGenerationError,
    TestGenerator,
    format_generation_context,
)


def make_pr():
    return PullRequest(
        "acme", "widget", 1, "Fix parsing", "Handle empty values", "base", "head",
        [ChangedFile("src/parser.py", "modified", 1, 1, 2, "+return value")],
    )


def make_context():
    return CollectedContext(
        [
            ContextFile("pyproject.toml", "config", "[tool.pytest.ini_options]"),
            ContextFile("tests/test_parser.py", "test", "def test_old(): pass"),
            ContextFile("src/parser.py", "source", "def parse(value): return value"),
        ],
        ["src/parser.py"], ["tests/test_parser.py"], ["pyproject.toml"], [],
        ["one file was omitted"], "Fix parsing", "Handle empty values",
        [ContextPatch("src/parser.py", "+return value")], 10_000,
    )


def proposal(path="tests/test_parser_empty.py", target="src/parser.py", framework="pytest"):
    return TestProposal(
        suggested_test_file=path,
        target_source_file=target,
        framework=framework,
        test_code="def test_empty(): assert parse('') == ''",
        explanation="Covers the empty-value regression.",
        assumptions=["parse is imported using the existing project convention"],
    )


def test_context_formatter_is_deterministic_bounded_and_patch_first():
    detection = FrameworkDetection("python", "pytest", ["pytest config"])
    first = format_generation_context(make_pr(), make_context(), detection, 2_000)
    second = format_generation_context(make_pr(), make_context(), detection, 2_000)
    assert first == second
    assert len(first) <= 2_000
    assert first.index("ALLOWED TARGET SOURCE PATHS") < first.index("FRAMEWORK")
    assert 'target_source_file must exactly equal one of these JSON strings' in first
    assert '"src/parser.py"' in first
    assert first.index("PATCH path=src/parser.py") < first.index("category=config")
    assert "UNTRUSTED DATA" in first
    limited = format_generation_context(make_pr(), make_context(), detection, 450)
    assert len(limited) <= 450
    assert "TRUNCATED" in limited
    assert '"src/parser.py"' in limited


def test_allowed_target_list_is_never_silently_truncated():
    detection = FrameworkDetection("python", "pytest", ["config"])
    with pytest.raises(ValueError, match="complete allowed-target list.*at least"):
        format_generation_context(make_pr(), make_context(), detection, 50)


def test_generated_paths_reject_escape_absolute_and_sensitive_locations():
    for path in ("../evil.py", "/tmp/evil.py", ".git/hooks/test.py", "tests\\evil.py"):
        with pytest.raises(ValidationError):
            proposal(path=path)


def test_missing_api_key_is_clear(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(TestGenerationError, match="OPENAI_API_KEY"):
        TestGenerator()


def test_structured_response_is_returned_and_sdk_schema_is_used():
    expected = GenerationResult(summary="One regression test", proposals=[proposal()], warnings=[])
    client = Mock()
    client.chat.completions.parse.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(parsed=expected, refusal=None))]
    )
    result = TestGenerator(client=client).generate(
        make_pr(), make_context(), FrameworkDetection("python", "pytest", ["config"])
    )
    assert result == expected
    kwargs = client.chat.completions.parse.call_args.kwargs
    assert kwargs["response_format"] is GenerationResult
    assert kwargs["messages"][0]["content"] == SYSTEM_PROMPT


def test_malformed_structured_response_is_rejected():
    client = Mock()
    client.chat.completions.parse.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(parsed={"summary": "missing fields"}, refusal=None))]
    )
    with pytest.raises(TestGenerationError, match="Malformed structured"):
        TestGenerator(client=client).generate(
            make_pr(), make_context(), FrameworkDetection("python", "pytest", ["config"])
        )


def test_api_failure_is_wrapped():
    client = Mock()
    client.chat.completions.parse.side_effect = OpenAIError("offline")
    with pytest.raises(TestGenerationError, match="API request failed"):
        TestGenerator(client=client).generate(
            make_pr(), make_context(), FrameworkDetection("python", "pytest", ["config"])
        )


def test_unknown_target_framework_mismatch_and_oversized_output_are_rejected():
    detection = FrameworkDetection("python", "pytest", ["config"])
    for item, message in [
        (proposal(target="src/missing.py"), "unknown source"),
        (proposal(framework="jest"), "expected pytest"),
    ]:
        result = GenerationResult(summary="x", proposals=[item], warnings=[])
        client = Mock()
        client.chat.completions.parse.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(parsed=result, refusal=None))]
        )
        with pytest.raises(TestGenerationError, match=message) as error:
            TestGenerator(client=client).generate(make_pr(), make_context(), detection)
        if "unknown source" in message:
            assert "Allowed target paths: src/parser.py" in str(error.value)

    result = GenerationResult(summary="x" * 20, proposals=[], warnings=[])
    client = Mock()
    client.chat.completions.parse.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(parsed=result, refusal=None))]
    )
    with pytest.raises(TestGenerationError, match="size limit"):
        TestGenerator(client=client, max_output_characters=10).generate(
            make_pr(), make_context(), detection
        )


def test_new_test_generator_module_is_accepted_when_listed_as_changed_source():
    pr = PullRequest(
        "acme", "widget", 2, "Add Phase 2", "", "base", "head",
        [ChangedFile("testpilot/test_generator.py", "added", 10, 0, 10, "+code")],
    )
    context = CollectedContext(
        [ContextFile("testpilot/test_generator.py", "source", "class TestGenerator: pass")],
        ["testpilot/test_generator.py"], [], ["pyproject.toml"], [], [],
        "Add Phase 2", "", [ContextPatch("testpilot/test_generator.py", "+code")], 10_000,
    )
    expected = GenerationResult(
        summary="Add generator coverage",
        proposals=[proposal(
            path="tests/test_test_generator.py",
            target="testpilot/test_generator.py",
        )],
        warnings=[],
    )
    client = Mock()
    client.chat.completions.parse.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(parsed=expected, refusal=None))]
    )

    result = TestGenerator(client=client).generate(
        pr, context, FrameworkDetection("python", "pytest", ["config"])
    )

    assert result.proposals[0].target_source_file == "testpilot/test_generator.py"
    prompt = client.chat.completions.parse.call_args.kwargs["messages"][1]["content"]
    assert '"testpilot/test_generator.py"' in prompt
