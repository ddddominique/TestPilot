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
        ContextFile("pyproject.toml", "config", 'dependencies = ["pytest>=8"]'),
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
