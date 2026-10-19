"""Build the reviewed runner and record its immutable ID and source fingerprint."""

import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNNER_DIRECTORY = PROJECT_ROOT / "runner"
TRUST_RECORD = PROJECT_ROOT / ".testpilot" / "runner-image.json"
RUNNER_TAG = "testpilot-pytest-runner:0.1"


def source_fingerprint() -> str:
    digest = hashlib.sha256()
    for name in ("Dockerfile", "pytest_runner.py"):
        digest.update(name.encode("ascii") + b"\0")
        digest.update((RUNNER_DIRECTORY / name).read_bytes())
    return digest.hexdigest()


def trusted_image_id() -> str:
    """Trust local build approval, never the current value of a Docker tag."""
    try:
        record = json.loads(TRUST_RECORD.read_text(encoding="utf-8"))
        if set(record) != {"image_id", "source_sha256"}:
            raise ValueError("invalid trust record")
        image_id = record["image_id"]
        if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise ValueError("invalid image ID")
        if record["source_sha256"] != source_fingerprint():
            raise ValueError("runner source changed since the trusted build")
        return image_id
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError(
            "Runner provenance is missing, invalid, or stale. Review runner/ and rebuild with: "
            "python -m testpilot.build_runner"
        ) from exc


def main() -> int:
    fingerprint = source_fingerprint()
    # Copy only reviewed inputs so unrelated files cannot enter the build context.
    with tempfile.TemporaryDirectory(prefix="testpilot-runner-build-") as temporary:
        root = Path(temporary)
        context = root / "context"
        context.mkdir()
        for name in ("Dockerfile", "pytest_runner.py"):
            (context / name).write_bytes((RUNNER_DIRECTORY / name).read_bytes())
        iidfile = root / "image-id"
        subprocess.run(
            ["docker", "build", "--iidfile", str(iidfile), "--tag", RUNNER_TAG, str(context)],
            check=True,
        )
        image_id = iidfile.read_text().strip()
    if source_fingerprint() != fingerprint:
        raise RuntimeError("Runner inputs changed during build; review and rebuild")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise RuntimeError("Docker returned an invalid build image ID")
    TRUST_RECORD.parent.mkdir(mode=0o700, exist_ok=True)
    record = {"image_id": image_id, "source_sha256": fingerprint}
    staged = TRUST_RECORD.with_suffix(".tmp")
    staged.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    staged.replace(TRUST_RECORD)
    print(f"Trusted runner recorded: {image_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
