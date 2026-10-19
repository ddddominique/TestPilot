"""Validated models for framework detection and generated test proposals."""

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, field_validator


@dataclass(frozen=True)
class FrameworkDetection:
    language: str
    framework: str
    evidence: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_supported(self) -> bool:
        return self.framework in {"pytest", "jest", "vitest"}


class TestProposal(BaseModel):
    __test__: ClassVar[bool] = False
    model_config = ConfigDict(extra="forbid")

    suggested_test_file: str
    target_source_file: str
    framework: str
    test_code: str
    explanation: str
    assumptions: list[str]

    @field_validator("suggested_test_file", "target_source_file")
    @classmethod
    def validate_repository_path(cls, value: str) -> str:
        if not value or "\\" in value:
            raise ValueError("path must be a non-empty repository-relative POSIX path")
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("path must stay inside the repository")
        sensitive = {".git", ".ssh"}
        if any(part.lower() in sensitive for part in path.parts):
            raise ValueError("path points inside a sensitive location")
        return str(path)


class GenerationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    proposals: list[TestProposal]
    warnings: list[str]
