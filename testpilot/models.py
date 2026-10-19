"""Data models shared by the GitHub client and context collector."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ChangedFile:
    filename: str
    status: str
    additions: int
    deletions: int
    changes: int
    patch: Optional[str] = None
    previous_filename: Optional[str] = None


@dataclass(frozen=True)
class PullRequest:
    owner: str
    repository: str
    number: int
    title: str
    description: str
    base_sha: str
    head_sha: str
    files: list[ChangedFile]
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ContextFile:
    path: str
    category: str
    content: str
    truncated: bool = False


@dataclass(frozen=True)
class ContextPatch:
    path: str
    content: str
    truncated: bool = False


@dataclass(frozen=True)
class CollectedContext:
    files: list[ContextFile]
    relevant_source_files: list[str]
    existing_tests: list[str]
    testing_config: list[str]
    omitted_files: list[str]
    warnings: list[str] = field(default_factory=list)
    pr_title: str = ""
    pr_description: str = ""
    patches: list[ContextPatch] = field(default_factory=list)
    max_characters: int = 0

    @property
    def total_characters(self) -> int:
        return (
            len(self.pr_title)
            + len(self.pr_description)
            + sum(len(item.content) for item in self.patches)
            + sum(len(item.content) for item in self.files)
        )
