"""Command-line interface for context collection and optional test generation."""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from .context import ContextCollector
from .execution_models import ExecutionResult, ExecutionStatus
from .executor import DockerRunner, TestExecutor
from .framework_detector import detect_framework
from .generation_models import FrameworkDetection, GenerationResult
from .github import GitHubClient, GitHubError
from .models import CollectedContext, PullRequest
from .test_generator import TestGenerationError, TestGenerator
from .workspace import WorkspaceManager


def format_summary(pr: PullRequest, context: CollectedContext) -> str:
    lines = [
        f"Pull request: {pr.owner}/{pr.repository}#{pr.number}",
        f"Title: {pr.title}",
        f"Description: {pr.description or '(none)'}",
        f"Base SHA: {pr.base_sha}",
        f"Head SHA: {pr.head_sha}",
        f"Changed files ({len(pr.files)}):",
    ]
    for item in pr.files:
        patch_state = "patch available" if item.patch is not None else "patch unavailable"
        lines.append(
            f"  - {item.filename} [{item.status}, +{item.additions}/-{item.deletions}, {patch_state}]"
        )
    lines.extend([
        "",
        f"Budgeted context ({context.total_characters}/{context.max_characters} characters):",
        f"  - PR metadata: {len(context.pr_title) + len(context.pr_description)} chars",
        f"  - Patches: {len(context.patches)} files, {sum(len(item.content) for item in context.patches)} chars",
        f"Collected repository context ({len(context.files)} files):",
    ])
    for item in context.files:
        suffix = ", truncated" if item.truncated else ""
        lines.append(f"  - {item.path} [{item.category}{suffix}; {len(item.content)} chars]")
    if context.omitted_files:
        lines.append(f"Omitted files ({len(context.omitted_files)}):")
        lines.extend(f"  - {path}" for path in context.omitted_files)
    if context.warnings:
        lines.append("Warnings:")
        lines.extend(f"  - {warning}" for warning in context.warnings)
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect testing context from a GitHub PR")
    parser.add_argument("--pr", required=True, help="GitHub pull request URL")
    parser.add_argument("--max-context-chars", type=int, default=100_000)
    parser.add_argument("--max-context-files", type=int, default=30)
    parser.add_argument(
        "--generate-tests", action="store_true",
        help="Use OpenAI to produce structured test proposals without writing or executing them",
    )
    parser.add_argument("--max-llm-input-chars", type=int, default=100_000)
    parser.add_argument(
        "--execute", action="store_true",
        help="Run generated pytest proposals in the restricted Docker runner",
    )
    return parser


def format_generation_summary(
    detection: FrameworkDetection, result: GenerationResult
) -> str:
    lines = [
        f"Detected framework: {detection.framework} ({detection.language})",
        "Evidence:",
        *(f"  - {item}" for item in detection.evidence),
        f"Generation summary: {result.summary}",
        f"Proposed tests ({len(result.proposals)}):",
    ]
    for index, proposal in enumerate(result.proposals, 1):
        lines.extend([
            "",
            f"Proposal {index}: {proposal.suggested_test_file}",
            f"Target: {proposal.target_source_file}",
            f"Framework: {proposal.framework}",
            f"Explanation: {proposal.explanation}",
            "Assumptions/limitations:",
            *(f"  - {item}" for item in proposal.assumptions),
            "Generated code (proposal only; not executed):",
            "```",
            proposal.test_code,
            "```",
        ])
    warnings = detection.warnings + result.warnings
    if warnings:
        lines.append("Warnings:")
        lines.extend(f"  - {item}" for item in warnings)
    return "\n".join(lines)


def format_execution_summary(result: ExecutionResult) -> str:
    lines = [
        "",
        "Execution results:",
        f"Repository: {result.repository}#{result.pr_number}",
        f"Head commit: {result.head_sha}",
        f"Framework: {result.framework}",
        f"Docker execution started: {'yes' if result.docker_started else 'no'}",
        f"Generated test files: {', '.join(result.test_files) or '(none)'}",
        f"Overall status: {result.status.value}",
        f"Tests: {result.passed} passed, {result.failed} failed, {result.skipped} skipped",
        f"Duration: {result.duration_seconds:.2f}s",
    ]
    if result.exit_code is not None:
        lines.append(f"Exit code: {result.exit_code}")
    if result.error:
        lines.append(f"Details: {result.error}")
    if result.output:
        lines.extend(["Bounded runner output:", result.output.rstrip()])
    if result.warnings:
        lines.append("Execution warnings:")
        lines.extend(f"  - {warning}" for warning in result.warnings)
    if result.status == ExecutionStatus.TEST_FAILED:
        lines.append(
            "A failure may indicate a PR regression or an incorrect generated test; review the evidence."
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.execute and not args.generate_tests:
        parser.error("--execute requires --generate-tests")
    try:
        client = GitHubClient()
        pull_request = client.get_pull_request(args.pr)
        context = ContextCollector(
            client, args.max_context_chars, args.max_context_files
        ).collect(pull_request)
        if args.generate_tests:
            detection = detect_framework(context)
            result = TestGenerator(max_input_characters=args.max_llm_input_chars).generate(
                pull_request, context, detection
            )
            print(format_generation_summary(detection, result))
            if args.execute:
                execution = TestExecutor(
                    WorkspaceManager(client), DockerRunner()
                ).execute(pull_request, detection, result)
                print(format_execution_summary(execution))
                return 0 if execution.successful else 1
            return 0
    except (GitHubError, TestGenerationError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(format_summary(pull_request, context))
    return 0
