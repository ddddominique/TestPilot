"""Bounded preparation of immutable PR snapshots and generated test files."""

import ast
import gzip
import os
import re
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional

from pydantic import ValidationError

from .generation_models import GenerationResult, TestProposal
from .github import GitHubClient, GitHubError
from .models import PullRequest


class WorkspaceError(RuntimeError):
    """A repository snapshot or generated path could not be prepared safely."""


class _BoundedArchiveReader:
    """Also bound decompressed headers/PAX metadata, not only extracted files."""

    def __init__(self, stream, limit: int) -> None:
        self.stream = stream
        self.remaining = limit

    def read(self, size: int = -1) -> bytes:
        size = self.remaining + 1 if size < 0 else min(size, self.remaining + 1)
        chunk = self.stream.read(size)
        self.remaining -= len(chunk)
        if self.remaining < 0:
            raise WorkspaceError("Snapshot exceeds decompression limits")
        return chunk


@dataclass
class ExecutionWorkspace:
    root: Path
    source: Path
    generated: Path
    generated_files: list[Path]
    _temporary_directory: Optional[tempfile.TemporaryDirectory] = None

    def cleanup(self) -> None:
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()
            self._temporary_directory = None

    def __enter__(self) -> "ExecutionWorkspace":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.cleanup()


class WorkspaceManager:
    def __init__(
        self,
        github: GitHubClient,
        max_archive_bytes: int = 100_000_000,
        max_extracted_bytes: int = 250_000_000,
        max_archive_files: int = 20_000,
    ) -> None:
        self.github = github
        self.max_archive_bytes = max_archive_bytes
        self.max_extracted_bytes = max_extracted_bytes
        self.max_archive_files = max_archive_files

    def prepare(self, pull_request: PullRequest, generation: GenerationResult) -> ExecutionWorkspace:
        if not re.fullmatch(r"[0-9a-fA-F]{40}", pull_request.head_sha):
            raise WorkspaceError("Execution requires a full immutable 40-character PR head SHA")
        temporary = tempfile.TemporaryDirectory(prefix="testpilot-execution-")
        root = Path(temporary.name)
        source = root / "source"
        generated = root / "generated"
        archive = root / "snapshot.tar.gz"
        source.mkdir(mode=0o700)
        generated.mkdir(mode=0o700)
        owner = pull_request.head_owner or pull_request.owner
        repository = pull_request.head_repository or pull_request.repository
        try:
            self.github.download_archive(
                owner, repository, pull_request.head_sha, archive, self.max_archive_bytes
            )
            self._extract_archive(archive, source)
            archive.unlink(missing_ok=True)
            generated_files = self._materialize_tests(source, generated, generation)
            self._make_snapshot_read_only(source)
            self._make_generated_read_only(generated)
            return ExecutionWorkspace(root, source, generated, generated_files, temporary)
        except Exception as exc:
            temporary.cleanup()
            if isinstance(exc, WorkspaceError):
                raise
            raise WorkspaceError(f"Could not prepare exact PR snapshot: {exc}") from exc

    def _extract_archive(self, archive: Path, destination: Path) -> None:
        total_size = 0
        file_count = 0
        root_component: Optional[str] = None
        # Header allowance is bounded as well, including tar metadata that
        # tarfile consumes internally before yielding individual members.
        expanded_limit = self.max_extracted_bytes + self.max_archive_files * 1024
        with gzip.open(archive, "rb") as stream, tarfile.open(
            fileobj=_BoundedArchiveReader(stream, expanded_limit), mode="r|"
        ) as tar:
            for member in tar:
                file_count += 1
                if file_count > self.max_archive_files:
                    raise WorkspaceError("Snapshot exceeds extraction limits")
                if "\\" in member.name or any(
                    part in {"", ".", ".."} for part in member.name.rstrip("/").split("/")
                ):
                    raise WorkspaceError(f"Unsafe archive path: {member.name}")
                if not (member.isdir() or member.isfile()):
                    raise WorkspaceError(f"Snapshot contains unsupported special file: {member.name}")
                pure = PurePosixPath(member.name)
                if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
                    raise WorkspaceError(f"Unsafe archive path: {member.name}")
                if root_component is None:
                    root_component = pure.parts[0]
                if pure.parts[0] != root_component:
                    raise WorkspaceError("Snapshot archive contains multiple top-level roots")
                if any(part.lower() in {".git", ".ssh"} for part in pure.parts[1:]):
                    raise WorkspaceError(f"Snapshot contains sensitive directory: {member.name}")
                relative_parts = pure.parts[1:]
                if not relative_parts:
                    continue
                if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                    raise WorkspaceError(f"Snapshot contains unsupported special file: {member.name}")
                if not (member.isdir() or member.isfile()):
                    raise WorkspaceError(f"Snapshot contains unsupported entry: {member.name}")
                total_size += member.size
                if file_count > self.max_archive_files or total_size > self.max_extracted_bytes:
                    raise WorkspaceError("Snapshot exceeds extraction limits")
                target = destination.joinpath(*relative_parts)
                if not target.resolve(strict=False).is_relative_to(destination.resolve()):
                    raise WorkspaceError(f"Archive path escapes workspace: {member.name}")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source_file = tar.extractfile(member)
                if source_file is None:
                    raise WorkspaceError(f"Could not read archive file: {member.name}")
                with target.open("xb") as output:
                    shutil.copyfileobj(source_file, output, length=64 * 1024)
        if root_component is None:
            raise WorkspaceError("Snapshot archive is empty")

    def _materialize_tests(
        self, source: Path, generated: Path, generation: GenerationResult
    ) -> list[Path]:
        seen: set[str] = set()
        created: list[Path] = []
        generated_root = generated.resolve()
        for proposal in generation.proposals:
            try:
                proposal = TestProposal.model_validate(proposal.model_dump())
            except ValidationError as exc:
                raise WorkspaceError("Generated proposal has an unsafe path or invalid schema") from exc
            path = proposal.suggested_test_file
            if path in seen:
                raise WorkspaceError(f"Duplicate generated test path: {path}")
            seen.add(path)
            raw_parts = path.split("/")
            if any(part in {"", ".", ".."} for part in raw_parts) or "\\" in path:
                raise WorkspaceError(f"Unsafe generated test path: {path}")
            if source.joinpath(*raw_parts).exists():
                raise WorkspaceError(f"Generated test would overwrite repository file: {path}")
            source_component = source
            for part in raw_parts:
                source_component = source_component / part
                if source_component.is_symlink():
                    raise WorkspaceError(f"Repository path traverses a symlink: {path}")
            target = generated.joinpath(*raw_parts)
            relative_parent = generated
            for part in raw_parts[:-1]:
                relative_parent = relative_parent / part
                if relative_parent.is_symlink():
                    raise WorkspaceError(f"Generated test traverses a symlink: {path}")
                relative_parent.mkdir(exist_ok=True)
            if not target.resolve(strict=False).is_relative_to(generated_root):
                raise WorkspaceError(f"Generated test escapes execution workspace: {path}")
            ast.parse(proposal.test_code, filename=path)
            with target.open("x", encoding="utf-8", newline="\n") as test_file:
                test_file.write(proposal.test_code)
            target.chmod(0o444)
            created.append(target)
        return created

    @staticmethod
    def _make_snapshot_read_only(source: Path) -> None:
        for current_root, directories, files in os.walk(source, topdown=False):
            root = Path(current_root)
            for filename in files:
                (root / filename).chmod(0o444)
            for directory in directories:
                (root / directory).chmod(0o555)
            root.chmod(0o555)

    @staticmethod
    def _make_generated_read_only(generated: Path) -> None:
        for current_root, directories, files in os.walk(generated, topdown=False):
            root = Path(current_root)
            for filename in files:
                (root / filename).chmod(0o444)
            for directory in directories:
                (root / directory).chmod(0o555)
            root.chmod(0o555)
