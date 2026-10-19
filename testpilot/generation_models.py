"""Validated models for framework detection and generated test proposals."""

import ast
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def validate_python_source_paths(tree: ast.AST, target_source_file: str) -> None:
    """Reject recognizable source/test colocation mistakes without executing code.

    This is deliberately a small static check, not a Python data-flow analyzer.
    Resolve single-assignment aliases to catch common HERE / SOURCE_NAME patterns;
    leave dynamic expressions and ambiguous reassignments to isolated execution.
    """
    assignments: dict[str, ast.AST] = {}
    repeated: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and value is not None:
                if target.id in assignments:
                    repeated.add(target.id)
                assignments[target.id] = value
    for name in repeated:
        assignments.pop(name, None)

    def references(node: ast.AST) -> tuple[bool, set[str]]:
        uses_test_location = False
        strings: set[str] = set()
        pending = [node]
        seen: set[str] = set()
        while pending:
            for child in ast.walk(pending.pop()):
                if isinstance(child, ast.Constant) and isinstance(child.value, str):
                    strings.add(child.value)
                elif isinstance(child, ast.Name):
                    if child.id == "__file__":
                        uses_test_location = True
                    elif child.id in assignments and child.id not in seen:
                        seen.add(child.id)
                        pending.append(assignments[child.id])
        return uses_test_location, strings

    basename = PurePosixPath(target_source_file).name
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Call, ast.BinOp)):
            continue
        uses_test_location, strings = references(node)
        targets_source = any(
            value == basename or value == target_source_file or value.endswith("/" + basename)
            for value in strings
        )
        generated_source = any(
            value.startswith("/workspace/generated/") and PurePosixPath(value).name == basename
            for value in strings
        )
        if generated_source or (uses_test_location and targets_source):
            raise ValueError(
                f"source {target_source_file!r} must be resolved from "
                'Path(os.environ["TESTPILOT_SOURCE_ROOT"]) using its full repository-relative path; '
                "generated tests and repository sources are mounted separately"
            )


@dataclass(frozen=True)
class FrameworkDetection:
    language: str
    framework: str
    evidence: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    selection: str = "detected"

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
