"""Evidence-based detection of supported repository test frameworks."""

import json
import re
from pathlib import PurePosixPath

from .generation_models import FrameworkDetection
from .models import CollectedContext


FRAMEWORK_LANGUAGE = {"pytest": "python", "jest": "javascript/typescript", "vitest": "javascript/typescript"}


def detect_framework(context: CollectedContext) -> FrameworkDetection:
    scores = {"pytest": 0, "jest": 0, "vitest": 0}
    evidence: dict[str, list[str]] = {name: [] for name in scores}
    changed_paths = context.relevant_source_files

    for item in context.files:
        lower_path = item.path.lower()
        content = item.content.lower()
        relevance = min(_path_relevance(item.path, changed_paths), 3)

        if lower_path.endswith(("pytest.ini", "tox.ini")) or "[tool.pytest" in content:
            _add(scores, evidence, "pytest", 3 + relevance, f"pytest configuration in {item.path}")
        if _contains_dependency(content, "pytest") or re.search(r"(?:from|import)\s+pytest\b", content):
            _add(scores, evidence, "pytest", 2 + relevance, f"pytest dependency/import in {item.path}")

        if PurePosixPath(lower_path).name.startswith("jest.config"):
            _add(scores, evidence, "jest", 3 + relevance, f"Jest configuration in {item.path}")
        if _contains_dependency(content, "jest") or "@jest/globals" in content:
            _add(scores, evidence, "jest", 2 + relevance, f"Jest dependency/import in {item.path}")

        if PurePosixPath(lower_path).name.startswith("vitest.config"):
            _add(scores, evidence, "vitest", 3 + relevance, f"Vitest configuration in {item.path}")
        if _contains_dependency(content, "vitest") or re.search(r"from\s+['\"]vitest['\"]", content):
            _add(scores, evidence, "vitest", 2 + relevance, f"Vitest dependency/import in {item.path}")

    best_score = max(scores.values())
    winners = [name for name, score in scores.items() if score == best_score and score > 0]
    if not winners:
        return FrameworkDetection("unknown", "unknown", [], ["No supported test framework evidence was found."])
    if len(winners) > 1:
        details = ", ".join(f"{name}={scores[name]}" for name in sorted(winners))
        return FrameworkDetection(
            "unknown", "unknown", [],
            [f"Conflicting framework evidence ({details}); refusing to guess."],
        )
    framework = winners[0]
    return FrameworkDetection(FRAMEWORK_LANGUAGE[framework], framework, evidence[framework])


def _add(
    scores: dict[str, int], evidence: dict[str, list[str]], framework: str, score: int, reason: str
) -> None:
    scores[framework] += score
    if reason not in evidence[framework]:
        evidence[framework].append(reason)


def _contains_dependency(content: str, dependency: str) -> bool:
    # JSON parsing avoids matching dependency names in unrelated prose when a
    # complete package.json is available; regex also supports truncated files
    # and Python dependency declarations.
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        for section in ("dependencies", "devdependencies", "peerdependencies"):
            values = parsed.get(section, {})
            if isinstance(values, dict) and dependency in {str(key).lower() for key in values}:
                return True
    declaration = (
        rf"{re.escape(dependency)}(?:\[[^]]+\])?\s*"
        r"(?:[<>=~!^@:]|['\",\s]|$)"
    )
    return bool(
        re.search(rf"(?m)^\s*['\"]?{declaration}", content)
        or re.search(rf"['\"]{declaration}", content)
    )


def _path_relevance(path: str, changed_paths: list[str]) -> int:
    parent_parts = PurePosixPath(path).parent.parts
    best = 0
    for changed in changed_paths:
        changed_parts = PurePosixPath(changed).parent.parts
        common = 0
        for left, right in zip(parent_parts, changed_parts):
            if left != right:
                break
            common += 1
        best = max(best, common)
    return best
