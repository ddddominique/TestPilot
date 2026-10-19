"""Command-line interface for Phase 1."""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from .context import ContextCollector
from .github import GitHubClient, GitHubError
from .models import CollectedContext, PullRequest


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
        f"Collected context ({len(context.files)} files, {context.total_characters} characters):",
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = GitHubClient()
        pull_request = client.get_pull_request(args.pr)
        context = ContextCollector(
            client, args.max_context_chars, args.max_context_files
        ).collect(pull_request)
    except (GitHubError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(format_summary(pull_request, context))
    return 0
