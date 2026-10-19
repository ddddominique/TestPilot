"""Validated models for framework detection and generated test proposals."""

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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

    suggested_test_file: str = Field(
        description="New repository-relative test file path; never an existing file or append target."
    )
    target_source_file: str = Field(
        description="Exact changed-source path copied from the allowed-target list."
    )
    framework: str = Field(description="Detected framework: pytest, jest, or vitest.")
    test_code: str = Field(
        description="Complete standalone test-module contents, including all required imports."
    )
    explanation: str = Field(description="Brief description of the changed behavior covered.")
    assumptions: list[str] = Field(
        description="Explicit limitations or assumptions required because context is incomplete."
    )

    @field_validator("suggested_test_file", "target_source_file")
    @classmethod
    def validate_repository_path(cls, value: str) -> str:
        if not value or value != value.strip() or "\\" in value or "\x00" in value:
            raise ValueError("path must be a non-empty repository-relative POSIX path")
        if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
            raise ValueError("absolute paths are not allowed")
        raw_parts = value.split("/")
        if any(part == "" for part in raw_parts):
            raise ValueError("path must not contain empty components or repeated separators")
        if any(part in {".", ".."} for part in raw_parts):
            raise ValueError("path must not contain '.' or '..' components")
        if any(any(ord(character) < 32 for character in part) for part in raw_parts):
            raise ValueError("path contains control characters")
        sensitive = {".git", ".ssh"}
        if any(part.lower() in sensitive for part in raw_parts):
            raise ValueError("path points inside a sensitive location")
        path = PurePosixPath(value)
        return str(path)

    @model_validator(mode="after")
    def validate_test_filename(self) -> "TestProposal":
        name = PurePosixPath(self.suggested_test_file).name.lower()
        framework = self.framework.lower()
        if framework == "pytest":
            valid = name.endswith(".py") and (
                name.startswith("test_") or name.endswith("_test.py")
            )
        elif framework in {"jest", "vitest"}:
            suffix = PurePosixPath(name).suffix
            valid = bool(re.search(r"\.(?:test|spec)\.(?:js|jsx|ts|tsx)$", name)) or (
                "__tests__" in {part.lower() for part in PurePosixPath(self.suggested_test_file).parts}
                and suffix in {".js", ".jsx", ".ts", ".tsx"}
            )
        else:
            valid = False
        if not valid:
            raise ValueError(
                f"suggested test filename does not follow {self.framework} conventions"
            )
        return self


class GenerationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    proposals: list[TestProposal]
    warnings: list[str]
