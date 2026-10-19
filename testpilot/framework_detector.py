"""Evidence-based detection of supported repository test frameworks."""

import ast
import configparser
import json
import re
from pathlib import PurePosixPath
from typing import Any, Optional

try:
    import tomllib
except ImportError:  # pragma: no cover - used on supported Python 3.9/3.10
    import tomli as tomllib

from .generation_models import FrameworkDetection
from .models import CollectedContext, ContextFile


FRAMEWORK_LANGUAGE = {
    "pytest": "python",
    "jest": "javascript/typescript",
    "vitest": "javascript/typescript",
}
PYTHON_DEPENDENCY_FILES = {"requirements.txt", "requirements-dev.txt"}
PYTHON_SUFFIXES = {".py", ".pyi"}
JAVASCRIPT_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}


def detect_framework(context: CollectedContext) -> FrameworkDetection:
    scores = {"pytest": 0, "jest": 0, "vitest": 0}
    evidence: dict[str, list[str]] = {name: [] for name in scores}
    for item in context.files:
        relevance = min(_path_relevance(item.path, context.relevant_source_files), 3)
        for framework, weight, reason in _evidence_for_file(item):
            _add(scores, evidence, framework, weight + relevance, reason)

    best_score = max(scores.values())
    winners = [name for name, score in scores.items() if score == best_score and score > 0]
    if not winners:
        return FrameworkDetection(
            "unknown", "unknown", [],
            ["No supported test framework evidence was found in relevant configuration, dependencies, or imports."],
        )
    if len(winners) > 1:
        details = ", ".join(f"{name}={scores[name]}" for name in sorted(winners))
        return FrameworkDetection(
            "unknown", "unknown", [],
            [f"Conflicting framework evidence ({details}); refusing to guess."],
        )
    framework = winners[0]
    return FrameworkDetection(FRAMEWORK_LANGUAGE[framework], framework, evidence[framework])


def _evidence_for_file(item: ContextFile) -> list[tuple[str, int, str]]:
    path = PurePosixPath(item.path.lower())
    name = path.name
    suffix = path.suffix
    results: list[tuple[str, int, str]] = []

    if name == "pytest.ini":
        results.append(("pytest", 4, f"pytest configuration file {item.path}"))
    elif name in {"tox.ini", "setup.cfg"}:
        parser = _parse_ini(item.content)
        if parser and any(section.lower() in {"pytest", "tool:pytest"} for section in parser.sections()):
            results.append(("pytest", 4, f"pytest configuration section in {item.path}"))
        if parser and _ini_declares_dependency(parser, "pytest"):
            results.append(("pytest", 3, f"pytest dependency in {item.path}"))
    elif name == "pyproject.toml":
        document = _parse_toml(item.content)
        if document is not None:
            tool = document.get("tool", {})
            if isinstance(tool, dict) and isinstance(tool.get("pytest"), dict):
                results.append(("pytest", 4, f"pytest configuration section in {item.path}"))
            if _pyproject_declares_dependency(document, "pytest"):
                results.append(("pytest", 3, f"pytest dependency in {item.path}"))
    elif name in PYTHON_DEPENDENCY_FILES or (
        name.startswith("requirements-") and name.endswith(".txt")
    ):
        if _requirements_declares_dependency(item.content, "pytest"):
            results.append(("pytest", 3, f"pytest dependency in {item.path}"))

    if name == "package.json":
        package = _parse_json_object(item.content)
        if package is not None:
            for framework in ("jest", "vitest"):
                if _package_declares_dependency(package, framework):
                    results.append((framework, 3, f"{framework} dependency in {item.path}"))

    if re.fullmatch(r"jest\.config\.(?:js|jsx|ts|tsx|mjs|cjs)", name):
        results.append(("jest", 4, f"Jest configuration file {item.path}"))
    if re.fullmatch(r"vitest\.config\.(?:js|jsx|ts|tsx|mjs|cjs)", name):
        results.append(("vitest", 4, f"Vitest configuration file {item.path}"))

    if item.category in {"source", "test"} and suffix in PYTHON_SUFFIXES:
        if _python_imports(item.content, "pytest"):
            results.append(("pytest", 2, f"pytest import in {item.path}"))
    if item.category in {"source", "test"} and suffix in JAVASCRIPT_SUFFIXES:
        imports = _javascript_imports(item.content)
        if "@jest/globals" in imports:
            results.append(("jest", 2, f"Jest import in {item.path}"))
        if "vitest" in imports:
            results.append(("vitest", 2, f"Vitest import in {item.path}"))
    return results


def _parse_ini(content: str) -> Optional[configparser.ConfigParser]:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(content)
    except configparser.Error:
        return None
    return parser


def _parse_toml(content: str) -> Optional[dict[str, Any]]:
    try:
        parsed = tomllib.loads(content)
    except (tomllib.TOMLDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_json_object(content: str) -> Optional[dict[str, Any]]:
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _ini_declares_dependency(parser: configparser.ConfigParser, dependency: str) -> bool:
    for section in parser.sections():
        for option, value in parser.items(section):
            if option.lower() in {"deps", "install_requires", "tests_require"}:
                if _dependency_lines_contain(value, dependency):
                    return True
    return False


def _pyproject_declares_dependency(document: dict[str, Any], dependency: str) -> bool:
    project = document.get("project", {})
    if isinstance(project, dict):
        candidates = list(project.get("dependencies", []) or [])
        optional = project.get("optional-dependencies", {})
        if isinstance(optional, dict):
            for values in optional.values():
                if isinstance(values, list):
                    candidates.extend(values)
        if any(_dependency_name(value) == dependency for value in candidates if isinstance(value, str)):
            return True

    tool = document.get("tool", {})
    poetry = tool.get("poetry", {}) if isinstance(tool, dict) else {}
    if isinstance(poetry, dict):
        tables = [poetry.get("dependencies", {}), poetry.get("dev-dependencies", {})]
        groups = poetry.get("group", {})
        if isinstance(groups, dict):
            tables.extend(
                group.get("dependencies", {}) for group in groups.values() if isinstance(group, dict)
            )
        if any(
            isinstance(table, dict) and dependency in {str(key).lower() for key in table}
            for table in tables
        ):
            return True
    return False


def _requirements_declares_dependency(content: str, dependency: str) -> bool:
    lines = (
        line.split("#", 1)[0].strip() for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "-"))
    )
    return any(_dependency_name(line) == dependency for line in lines)


def _dependency_lines_contain(content: str, dependency: str) -> bool:
    return any(
        _dependency_name(line.strip()) == dependency
        for line in content.replace(",", "\n").splitlines() if line.strip()
    )


def _dependency_name(declaration: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9_.-]+)", declaration)
    return match.group(1).lower().replace("_", "-") if match else ""


def _package_declares_dependency(package: dict[str, Any], dependency: str) -> bool:
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        values = package.get(section, {})
        if isinstance(values, dict) and dependency in {str(key).lower() for key in values}:
            return True
    return False


def _python_imports(content: str, module: str) -> bool:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(alias.name.split(".", 1)[0] == module for alias in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".", 1)[0] == module:
            return True
    return False


def _javascript_imports(content: str) -> set[str]:
    modules: set[str] = set()
    patterns = (
        r"(?m)^\s*import(?:[^\n]*?\sfrom\s*)?['\"]([^'\"]+)['\"]\s*;?\s*$",
        r"(?m)^\s*(?:const|let|var)\s+[^=]+?=\s*require\(\s*['\"]([^'\"]+)['\"]\s*\)\s*;?\s*$",
    )
    code = "\n".join(_javascript_code_lines(content))
    for pattern in patterns:
        modules.update(match.group(1) for match in re.finditer(pattern, code))
    return modules


def _javascript_code_lines(content: str) -> list[str]:
    """Conservatively omit comment and template-literal bodies before regex parsing."""
    lines: list[str] = []
    in_block_comment = False
    in_template = False
    for line in content.splitlines():
        stripped = line.lstrip()
        if in_block_comment:
            if "*/" in stripped:
                in_block_comment = False
            continue
        if in_template:
            if _unescaped_backtick_count(line) % 2:
                in_template = False
            continue
        if stripped.startswith("//"):
            continue
        if stripped.startswith("/*"):
            if "*/" not in stripped[2:]:
                in_block_comment = True
            continue
        lines.append(line)
        if _unescaped_backtick_count(line) % 2:
            in_template = True
    return lines


def _unescaped_backtick_count(line: str) -> int:
    return sum(character == "`" and (index == 0 or line[index - 1] != "\\")
               for index, character in enumerate(line))


def _add(
    scores: dict[str, int], evidence: dict[str, list[str]], framework: str, score: int, reason: str
) -> None:
    scores[framework] += score
    if reason not in evidence[framework]:
        evidence[framework].append(reason)


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
