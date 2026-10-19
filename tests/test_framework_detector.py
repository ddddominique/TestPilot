from testpilot.framework_detector import detect_framework
from testpilot.models import CollectedContext, ContextFile


def context(files, sources=None):
    return CollectedContext(files, sources or ["src/app.py"], [], [], [], [])


def test_detects_pytest_from_configuration_and_import():
    result = detect_framework(context([
        ContextFile("pyproject.toml", "config", "[tool.pytest.ini_options]"),
        ContextFile("tests/test_app.py", "test", "import pytest"),
    ]))
    assert (result.language, result.framework) == ("python", "pytest")
    assert len(result.evidence) == 2


def test_detects_pytest_from_pyproject_dependency_declaration():
    result = detect_framework(context([
        ContextFile("pyproject.toml", "config", '[project]\ndependencies = ["pytest>=8"]'),
    ]))
    assert result.framework == "pytest"


def test_detects_jest_from_package_dependencies():
    result = detect_framework(context([
        ContextFile("package.json", "config", '{"devDependencies":{"jest":"^30"}}'),
    ], ["src/app.ts"]))
    assert result.framework == "jest"


def test_detects_vitest_from_config_and_import():
    result = detect_framework(context([
        ContextFile("vitest.config.ts", "config", "export default {}"),
        ContextFile("src/app.test.ts", "test", "import { describe } from 'vitest'"),
    ], ["src/app.ts"]))
    assert result.framework == "vitest"


def test_unknown_when_no_evidence_exists():
    result = detect_framework(context([ContextFile("src/app.py", "source", "pass")]))
    assert result.framework == "unknown"
    assert result.warnings


def test_conflicting_equal_evidence_returns_unknown():
    result = detect_framework(context([
        ContextFile("jest.config.js", "config", "module.exports = {}"),
        ContextFile("vitest.config.js", "config", "export default {}"),
    ], ["src/app.ts"]))
    assert result.framework == "unknown"
    assert "Conflicting" in result.warnings[0]


def test_monorepo_prefers_framework_near_changed_source():
    result = detect_framework(context([
        ContextFile("jest.config.js", "config", "module.exports = {}"),
        ContextFile("packages/ui/vitest.config.ts", "config", "export default {}"),
    ], ["packages/ui/src/button.tsx"]))
    assert result.framework == "vitest"


def test_python_source_config_string_is_not_framework_evidence():
    result = detect_framework(context([
        ContextFile("src/app.py", "source", 'EXAMPLE = "[tool.pytest.ini_options]"'),
    ]))
    assert result.framework == "unknown"


def test_package_description_is_not_dependency_evidence():
    result = detect_framework(context([
        ContextFile("package.json", "config", '{"description":"pytest jest vitest"}'),
    ], ["src/app.ts"]))
    assert result.framework == "unknown"


def test_source_mentions_without_imports_are_not_evidence():
    result = detect_framework(context([
        ContextFile(
            "src/app.ts", "source",
            '// migrate from Jest to Vitest\nconst name = "vitest";\n'
            'const docs = `\nimport { test } from "vitest";\n`;\n'
            '/* import { test } from "@jest/globals"; */',
        ),
    ], ["src/app.ts"]))
    assert result.framework == "unknown"


def test_actual_ini_and_requirements_declarations_detect_pytest():
    result = detect_framework(context([
        ContextFile("setup.cfg", "config", "[tool:pytest]\naddopts = -q"),
        ContextFile("requirements-dev.txt", "config", "pytest>=8\n"),
    ]))
    assert result.framework == "pytest"


def test_malformed_configuration_does_not_invent_dependency_evidence():
    result = detect_framework(context([
        ContextFile("pyproject.toml", "config", '[project\ndependencies = ["pytest"]'),
        ContextFile("package.json", "config", '{"devDependencies":{"jest":'),
    ]))
    assert result.framework == "unknown"
