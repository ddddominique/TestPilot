import json
from pathlib import Path

import pytest

from testpilot import build_runner


@pytest.fixture
def build_paths(tmp_path, monkeypatch):
    source = tmp_path / "runner"
    source.mkdir()
    (source / "Dockerfile").write_text("FROM trusted@sha256:fixture\n")
    (source / "pytest_runner.py").write_text("# trusted fixture\n")
    record = tmp_path / "approval" / "runner-image.json"
    monkeypatch.setattr(build_runner, "RUNNER_DIRECTORY", source)
    monkeypatch.setattr(build_runner, "TRUST_RECORD", record)
    return source, record


def test_build_records_docker_image_id_and_reviewed_sources(build_paths, monkeypatch):
    source, record = build_paths

    def fake_build(command, check):
        context = Path(command[-1])
        assert sorted(path.name for path in context.iterdir()) == ["Dockerfile", "pytest_runner.py"]
        Path(command[command.index("--iidfile") + 1]).write_text("sha256:" + "a" * 64)

    monkeypatch.setattr(build_runner.subprocess, "run", fake_build)
    assert build_runner.main() == 0
    assert build_runner.trusted_image_id() == "sha256:" + "a" * 64
    (source / "pytest_runner.py").write_text("# changed\n")
    with pytest.raises(ValueError, match="stale.*Review"):
        build_runner.trusted_image_id()


def test_missing_or_malformed_record_fails_closed(build_paths):
    source, record = build_paths
    with pytest.raises(ValueError, match="provenance"):
        build_runner.trusted_image_id()
    record.parent.mkdir()
    record.write_text(json.dumps({"image_id": "mutable:tag", "source_sha256": build_runner.source_fingerprint()}))
    with pytest.raises(ValueError, match="provenance"):
        build_runner.trusted_image_id()
