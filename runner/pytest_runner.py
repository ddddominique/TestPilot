"""Trusted in-container pytest wrapper emitting one bounded JSON result."""

import contextlib
import io
import json
import sys
import time

import pytest


MAX_OUTPUT = 10_000
MARKER = "TESTPILOT_RESULT_JSON="


class BoundedWriter(io.TextIOBase):
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.parts = []
        self.size = 0
        self.truncated = False

    def write(self, value: str) -> int:
        remaining = self.limit - self.size
        if remaining > 0:
            selected = value[:remaining]
            self.parts.append(selected)
            self.size += len(selected)
        if len(value) > max(remaining, 0):
            self.truncated = True
        return len(value)

    def getvalue(self) -> str:
        value = "".join(self.parts)
        return value + ("\n[OUTPUT TRUNCATED]" if self.truncated else "")


class ResultPlugin:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.collection_errors = 0
        self.setup_errors = 0
        self.dependency_errors = 0
        self.teardown_errors = 0

    def pytest_collectreport(self, report) -> None:
        if report.failed:
            self.collection_errors += 1
            self._check_dependency(report)

    def _check_dependency(self, report) -> None:
        crash = getattr(report.longrepr, "reprcrash", None)
        message = getattr(crash, "message", "")
        if "ModuleNotFoundError" in message or "ImportError" in message:
            self.dependency_errors += 1

    def pytest_runtest_logreport(self, report) -> None:
        if report.failed:
            self._check_dependency(report)
        if report.when == "call":
            if report.passed:
                self.passed += 1
            elif report.failed:
                self.failed += 1
            elif report.skipped:
                self.skipped += 1
        elif report.when == "setup" and report.failed:
            self.setup_errors += 1
        elif report.when == "setup" and report.skipped:
            self.skipped += 1
        elif report.when == "teardown" and report.failed:
            self.teardown_errors += 1


def main() -> int:
    # Python isolated mode prevents repository sitecustomize startup hooks.
    # Add project imports only after loading this trusted runner and pytest.
    sys.path.extend(["/workspace/repo", "/workspace/repo/src"])
    tests = sys.argv[1:]
    writer = BoundedWriter(MAX_OUTPUT)
    plugin = ResultPlugin()
    started = time.monotonic()
    args = [
        "-c", "/dev/null",
        "-p", "no:cacheprovider",
        "--noconftest",
        "--import-mode=importlib",
        "-s",
        "--confcutdir=/workspace/generated",
        "--tb=short",
        "-q",
        *tests,
    ]
    with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
        exit_code = int(pytest.main(args, plugins=[plugin]))
    output = writer.getvalue()
    if not tests or exit_code == 5:
        status = "COLLECTION_ERROR"
        error = "No generated tests were collected."
    elif plugin.dependency_errors:
        status = "DEPENDENCY_ERROR"
        error = "Import failed; check unavailable dependencies and generated import paths."
    elif plugin.collection_errors or plugin.setup_errors or plugin.teardown_errors or exit_code in {2, 3, 4}:
        status = "COLLECTION_ERROR"
        error = "Pytest could not collect or set up the generated tests."
    elif plugin.failed or exit_code == 1:
        status = "TEST_FAILED"
        error = "One or more generated tests failed. This may indicate a regression or an incorrect generated test."
    elif exit_code == 0 and plugin.passed > 0:
        status = "PASSED"
        error = ""
    else:
        status = "COLLECTION_ERROR"
        error = "No tests passed (tests may all have been skipped), or pytest returned an unexpected result."
    payload = {
        "status": status,
        "exit_code": exit_code,
        "passed": plugin.passed,
        "failed": plugin.failed,
        "skipped": plugin.skipped,
        "duration_seconds": time.monotonic() - started,
        "output": output,
        "error": error,
        "warnings": ["Runner output was truncated"] if writer.truncated else [],
    }
    print(MARKER + json.dumps(payload, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
