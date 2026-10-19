from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from openai import OpenAIError
from pydantic import ValidationError

from testpilot.generation_models import FrameworkDetection, GenerationResult, TestProposal
from testpilot.framework_detector import select_framework
from testpilot.models import ChangedFile, CollectedContext, ContextFile, ContextPatch, PullRequest
from testpilot.test_generator import (
    ContextFormattingError,
    GeneratedCodeValidationError,
    GenerationSchemaError,
    SYSTEM_PROMPT,
    TestGenerationError,
    TestGenerator,
    _truncate_prompt_content,
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
    if framework == "pytest":
        code = "from src.parser import parse\n\n\ndef test_empty():\n    assert parse('') == ''\n"
    elif framework == "jest":
        code = (
            "import { describe, test, expect } from '@jest/globals';\n"
            "describe('parse', () => { test('works', () => expect(true).toBe(true)); });"
        )
    else:
        code = (
            "import { describe, it, expect } from 'vitest';\n"
            "describe('parse', () => { it('works', () => expect(true).toBe(true)); });"
        )
    return TestProposal(
        suggested_test_file=path,
        target_source_file=target,
        framework=framework,
        test_code=code,
        explanation="Covers the empty-value regression.",
        assumptions=[],
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
    limited = format_generation_context(make_pr(), make_context(), detection, 600)
    assert len(limited) <= 600
    assert "TRUNCATED" in limited
    assert '"src/parser.py"' in limited


def test_allowed_target_list_is_never_silently_truncated():
    detection = FrameworkDetection("python", "pytest", ["config"])
    with pytest.raises(ContextFormattingError, match="mandatory allowed targets.*at least"):
        format_generation_context(make_pr(), make_context(), detection, 50)


def test_generated_paths_reject_unsafe_or_ambiguous_forms():
    for path in (
        "../evil.py", "/tmp/evil.py", ".git/hooks/test_evil.py", ".ssh/test_key.py",
        "tests\\test_evil.py", "C:/tests/test_evil.py", "tests//test_evil.py",
        "tests/./test_evil.py", "tests/unit/../test_evil.py", "tests/test_evil.py/",
        " tests/test_evil.py", "tests/test_evil.py\x00",
    ):
        with pytest.raises(ValidationError):
            proposal(path=path)


@pytest.mark.parametrize(
    ("framework", "path"),
    [
        ("pytest", "tests/parser.py"),
        ("jest", "tests/parser.ts"),
        ("vitest", "tests/test_parser.py"),
    ],
)
def test_suggested_paths_must_follow_framework_naming(framework, path):
    with pytest.raises(ValidationError, match="filename does not follow"):
        proposal(path=path, framework=framework)


@pytest.mark.parametrize(
    ("framework", "path"),
    [
        ("pytest", "tests/test_parser.py"),
        ("pytest", "tests/parser_test.py"),
        ("jest", "tests/parser.spec.jsx"),
        ("vitest", "src/parser.test.tsx"),
        ("jest", "src/__tests__/parser.ts"),
    ],
)
def test_supported_framework_test_paths_are_accepted(framework, path):
    assert proposal(path=path, framework=framework).suggested_test_file == path


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
    with pytest.raises(GenerationSchemaError, match="Structured response validation"):
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
        (proposal(path="tests/parser.test.ts", framework="jest"), "expected pytest"),
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


def test_system_prompt_requires_complete_standalone_modules():
    assert "complete contents of a NEW, standalone test module" in SYSTEM_PROMPT
    assert "including all required" in SYSTEM_PROMPT
    assert "Never invent APIs" in SYSTEM_PROMPT


def test_invalid_python_syntax_is_rejected_without_execution():
    invalid = proposal().model_copy(update={"test_code": "def broken(:\n    pass"})
    result = GenerationResult(summary="invalid", proposals=[invalid], warnings=[])
    client = Mock()
    client.chat.completions.parse.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(parsed=result, refusal=None))]
    )
    with pytest.raises(GeneratedCodeValidationError, match="not valid syntax.*line 1"):
        TestGenerator(client=client).generate(
            make_pr(), make_context(), FrameworkDetection("python", "pytest", ["config"])
        )


def test_javascript_syntax_is_not_claimed_or_locally_validated():
    js_context = replace(
        make_context(),
        relevant_source_files=["src/parser.ts"],
        files=[ContextFile("src/parser.ts", "source", "export const parse = () => 1")],
    )
    js_proposal = proposal(
        path="tests/parser.test.ts", target="src/parser.ts", framework="vitest"
    ).model_copy(update={"test_code": "this is not parsed locally {"})
    result = GenerationResult(summary="proposal", proposals=[js_proposal], warnings=[])
    client = Mock()
    client.chat.completions.parse.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(parsed=result, refusal=None))]
    )
    assert TestGenerator(client=client).generate(
        make_pr(), js_context, FrameworkDetection("javascript/typescript", "vitest", ["config"])
    ) == result


def test_existing_repository_path_cannot_be_overwritten():
    context = replace(make_context(), repository_files=["tests/test_parser_empty.py"])
    result = GenerationResult(summary="overwrite", proposals=[proposal()], warnings=[])
    client = Mock()
    client.chat.completions.parse.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(parsed=result, refusal=None))]
    )
    with pytest.raises(GeneratedCodeValidationError, match="already exists.*overwritten"):
        TestGenerator(client=client).generate(
            make_pr(), context, FrameworkDetection("python", "pytest", ["config"])
        )


def test_duplicate_suggested_test_paths_are_rejected():
    result = GenerationResult(
        summary="duplicates",
        proposals=[proposal(), proposal()],
        warnings=[],
    )
    client = Mock()
    client.chat.completions.parse.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(parsed=result, refusal=None))]
    )
    with pytest.raises(
        GeneratedCodeValidationError,
        match=r"Duplicate suggested test path.*tests/test_parser_empty\.py",
    ):
        TestGenerator(client=client).generate(
            make_pr(), make_context(), FrameworkDetection("python", "pytest", ["config"])
        )


def test_distinct_suggested_test_paths_are_accepted():
    result = GenerationResult(
        summary="distinct proposals",
        proposals=[
            proposal(path="tests/test_parser_empty.py"),
            proposal(path="tests/test_parser_none.py"),
        ],
        warnings=[],
    )
    client = Mock()
    client.chat.completions.parse.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(parsed=result, refusal=None))]
    )
    generated = TestGenerator(client=client).generate(
        make_pr(), make_context(), FrameworkDetection("python", "pytest", ["config"])
    )
    assert [item.suggested_test_file for item in generated.proposals] == [
        "tests/test_parser_empty.py",
        "tests/test_parser_none.py",
    ]


def test_mandatory_warnings_survive_truncation_and_budget_is_enforced():
    context = replace(
        make_context(),
        warnings=["Patch unavailable for src/parser.py", "Content truncated for src/parser.py"],
        patches=[ContextPatch("src/parser.py", "@@ -1 +1 @@\n" + "+line\n" * 200)],
    )
    output = format_generation_context(
        make_pr(), context, FrameworkDetection("python", "pytest", ["config"]), 750
    )
    assert len(output) <= 750
    assert "Patch unavailable for src/parser.py" in output
    assert "Content truncated for src/parser.py" in output
    assert '"src/parser.py"' in output
    assert "TRUNCATED" in output


def test_large_patch_truncation_uses_complete_hunk_boundary_when_practical():
    patch = "@@ -1 +1 @@\n+first\n@@ -20 +20 @@\n+" + ("second\n" * 20)
    selected = _truncate_prompt_content(patch, 60, "patch")
    assert selected == "@@ -1 +1 @@\n+first\n"


def test_large_patch_does_not_starve_source_config_and_test_sections():
    context = replace(
        make_context(),
        patches=[ContextPatch("src/parser.py", "@@ -1 +1 @@\n" + "+change\n" * 1_000)],
    )
    output = format_generation_context(
        make_pr(), context, FrameworkDetection("python", "pytest", ["config"]), 1_200
    )
    assert "PATCH path=src/parser.py" in output
    assert "category=source path=src/parser.py" in output
    assert "category=config path=pyproject.toml" in output
    assert "category=test path=tests/test_parser.py" in output
    assert len(output) <= 1_200


def test_inferred_pytest_selection_reaches_prompt_and_strict_validation():
    context = replace(
        make_context(),
        relevant_source_files=["leetcode/2089.py"],
        files=[ContextFile("leetcode/2089.py", "source", "class Solution: pass")],
    )
    selection = select_framework(context)
    expected = GenerationResult(
        summary="Python-only solution tests",
        proposals=[proposal(path="tests/test_2089.py", target="leetcode/2089.py")], warnings=[],
    )
    client = Mock()
    client.chat.completions.parse.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(parsed=expected, refusal=None))]
    )
    assert TestGenerator(client=client).generate(make_pr(), context, selection) == expected
    prompt = client.chat.completions.parse.call_args.kwargs["messages"][1]["content"]
    assert "selection: inferred" in prompt
    assert "framework: pytest" in prompt
    assert "All relevant changed source files are Python: leetcode/2089.py" in prompt
    assert "No supported framework evidence was found" in prompt

    wrong_framework = proposal(path="tests/generated.test.ts", target="leetcode/2089.py", framework="jest")
    with pytest.raises(GeneratedCodeValidationError, match="expected pytest"):
        TestGenerator(client=client)._validate_result(
            GenerationResult(summary="wrong", proposals=[wrong_framework], warnings=[]), selection, context
        )


@pytest.mark.parametrize("expression", [
    'Path(__file__).with_name("2089.py")',
    'Path(__file__).parent / "2089.py"',
    'Path(__file__).resolve().parent / "leetcode" / "2089.py"',
    'os.path.join(os.path.dirname(__file__), "2089.py")',
    'Path("/workspace/generated/leetcode/2089.py")',
    'HERE / SOURCE_NAME',
])
def test_rejects_source_paths_based_on_generated_test_location(expression):
    context = replace(make_context(), relevant_source_files=["leetcode/2089.py"])
    item = proposal(path="leetcode/test_2089.py", target="leetcode/2089.py").model_copy(update={
        "test_code": (
            'from pathlib import Path\nimport os\n'
            'HERE = Path(__file__).parent\nSOURCE_NAME = "2089.py"\n'
            f'source = {expression}\n'
            'def test_solution():\n    assert source.exists()\n'
        ),
    })
    with pytest.raises(GeneratedCodeValidationError, match="TESTPILOT_SOURCE_ROOT.*mounted separately"):
        TestGenerator(client=Mock())._validate_result(
            GenerationResult(summary="incorrect path", proposals=[item], warnings=[]),
            FrameworkDetection("python", "pytest"), context,
        )


@pytest.mark.parametrize("target", ["leetcode/2089.py", "solutions/python/leetcode/2089.py"])
def test_accepts_numeric_standalone_source_loaded_from_stable_repository_root(target):
    context = replace(make_context(), relevant_source_files=[target])
    code = (
        'import importlib.util\nimport os\nfrom pathlib import Path\nimport sys\n'
        f'source_path = Path(os.environ["TESTPILOT_SOURCE_ROOT"]) / {target!r}\n'
        'spec = importlib.util.spec_from_file_location("solution_under_test", source_path)\n'
        'module = importlib.util.module_from_spec(spec)\n'
        'sys.modules[spec.name] = module\n'
        'spec.loader.exec_module(module)\n'
        'def test_solution():\n    assert module.Solution is not None\n'
    )
    item = proposal(path="tests/generated/test_2089.py", target=target).model_copy(update={"test_code": code})
    expected = GenerationResult(summary="standalone solution", proposals=[item], warnings=[])
    client = Mock()
    client.chat.completions.parse.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(parsed=expected, refusal=None))]
    )
    assert TestGenerator(client=client).generate(
        make_pr(), context, select_framework(context)
    ) == expected
    instructions = client.chat.completions.parse.call_args.kwargs["messages"][0]["content"]
    assert "/workspace/repo" in instructions
    assert "/workspace/generated/<suggested_test_file>" in instructions
    assert "TESTPILOT_SOURCE_ROOT" in instructions
    assert "spec_from_file_location" in instructions


def test_preserves_package_imports_and_test_local_fixture_paths():
    item = proposal().model_copy(update={
        "test_code": (
            'from pathlib import Path\nfrom src.parser import parse\n'
            'FIXTURE = Path(__file__).with_name("input.json")\n'
            'def test_parse():\n    assert parse("") == ""\n'
        ),
    })
    TestGenerator(client=Mock())._validate_result(
        GenerationResult(summary="package and fixture", proposals=[item], warnings=[]),
        FrameworkDetection("python", "pytest"), make_context(),
    )
