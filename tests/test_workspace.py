import io
import tarfile
from pathlib import Path

import pytest

from testpilot.generation_models import GenerationResult, TestProposal
from testpilot.models import PullRequest
from testpilot.workspace import WorkspaceError, WorkspaceManager


def archive_bytes(entries, symlink=None):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        root = tarfile.TarInfo("repo-head/")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        for name, content in entries.items():
            encoded = content.encode()
            member = tarfile.TarInfo(f"repo-head/{name}")
            member.size = len(encoded)
            archive.addfile(member, io.BytesIO(encoded))
        if symlink:
            member = tarfile.TarInfo(f"repo-head/{symlink}")
            member.type = tarfile.SYMTYPE
            member.linkname = "/etc/passwd"
            archive.addfile(member)
    return data.getvalue()


class FakeGitHub:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def download_archive(self, owner, repository, sha, destination, max_bytes):
        self.calls.append((owner, repository, sha, max_bytes))
        destination.write_bytes(self.payload)
        return len(self.payload)


def pr():
    return PullRequest(
        "base-owner", "repo", 2, "title", "", "base", "a" * 40, [], [],
        "fork-owner", "fork-repo",
    )


def proposal(path="tests/test_app_generated.py", code="from app import value\n\ndef test_value():\n    assert value == 1\n"):
    return TestProposal(
        suggested_test_file=path,
        target_source_file="app.py",
        framework="pytest",
        test_code=code,
        explanation="checks value",
        assumptions=[],
    )


def generation(*proposals):
    return GenerationResult(summary="tests", proposals=list(proposals), warnings=[])


def test_prepares_exact_fork_head_snapshot_and_cleans_up():
    github = FakeGitHub(archive_bytes({"app.py": "value = 1\n"}))
    manager = WorkspaceManager(github)
    with manager.prepare(pr(), generation(proposal())) as workspace:
        root = workspace.root
        assert (workspace.source / "app.py").read_text() == "value = 1\n"
        assert workspace.generated_files[0].read_text().startswith("from app")
        assert not bool((workspace.source / "app.py").stat().st_mode & 0o222)
        assert not bool(workspace.generated.stat().st_mode & 0o222)
    assert github.calls[0][:3] == ("fork-owner", "fork-repo", "a" * 40)
    assert not root.exists()


@pytest.mark.parametrize(
    "payload",
    [
        archive_bytes({}, symlink="escape"),
        archive_bytes({"../../escape.py": "bad"}),
    ],
)
def test_rejects_unsafe_snapshot_entries(payload):
    with pytest.raises(WorkspaceError, match="Unsafe|special|escapes"):
        WorkspaceManager(FakeGitHub(payload)).prepare(pr(), generation(proposal()))


def test_rejects_snapshot_size_limit():
    payload = archive_bytes({"large.py": "x" * 100})
    with pytest.raises(WorkspaceError, match="extraction limits"):
        WorkspaceManager(FakeGitHub(payload), max_extracted_bytes=20).prepare(
            pr(), generation(proposal())
        )


def test_rejects_existing_file_collision():
    payload = archive_bytes({"tests/test_app_generated.py": "existing"})
    with pytest.raises(WorkspaceError, match="overwrite repository file"):
        WorkspaceManager(FakeGitHub(payload)).prepare(pr(), generation(proposal()))


def test_rejects_duplicate_materialization_even_if_schema_was_bypassed():
    payload = archive_bytes({"app.py": "value = 1"})
    with pytest.raises(WorkspaceError, match="Duplicate generated test path"):
        WorkspaceManager(FakeGitHub(payload)).prepare(
            pr(), generation(proposal(), proposal())
        )


def test_rejects_unsafe_generated_path_if_schema_was_bypassed():
    unsafe = proposal().model_copy(update={"suggested_test_file": "../test_escape.py"})
    with pytest.raises(WorkspaceError, match="unsafe path"):
        WorkspaceManager(FakeGitHub(archive_bytes({"app.py": "value=1"}))).prepare(
            pr(), generation(unsafe)
        )


def test_rejects_symlink_traversal_during_materialization(tmp_path):
    manager = WorkspaceManager(FakeGitHub(b""))
    source = tmp_path / "source"
    generated = tmp_path / "generated"
    outside = tmp_path / "outside"
    source.mkdir()
    generated.mkdir()
    outside.mkdir()
    (generated / "tests").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceError, match="symlink"):
        manager._materialize_tests(source, generated, generation(proposal()))


def test_materialization_rejects_colocated_source_path_before_execution():
    item = proposal(code='from pathlib import Path\nsource = Path(__file__).with_name("app.py")\n')
    with pytest.raises(WorkspaceError, match="TESTPILOT_SOURCE_ROOT"):
        WorkspaceManager(FakeGitHub(archive_bytes({"app.py": "value = 1"}))).prepare(
            pr(), generation(item)
        )
