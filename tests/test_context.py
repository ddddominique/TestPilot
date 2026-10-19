import pytest

from testpilot.context import ContextCollector, _is_config, _is_test
from testpilot.github import GitHubAPIError
from testpilot.models import ChangedFile, PullRequest


class FakeClient:
    def __init__(self):
        self.contents = {
            "src/parser.py": "p" * 8,
            "tests/test_parser.py": "t" * 8,
            "pyproject.toml": "c" * 8,
        }

    def get_tree(self, owner, repository, sha):
        return list(self.contents), False

    def get_file_content(self, owner, repository, path, ref):
        return self.contents[path]


def make_pr():
    return PullRequest(
        "acme", "widget", 1, "Title", "Body", "base", "head",
        [ChangedFile("src/parser.py", "modified", 1, 1, 2, "@@")],
    )


def test_collects_source_related_test_and_config():
    context = ContextCollector(FakeClient()).collect(make_pr())
    assert context.relevant_source_files == ["src/parser.py"]
    assert context.existing_tests == ["tests/test_parser.py"]
    assert context.testing_config == ["pyproject.toml"]
    assert [item.category for item in context.files] == ["config", "test", "source"]


@pytest.mark.parametrize(
    "path",
    [
        "tests/unit/test_parser.py", "test/parser_test.py", "src/__tests__/widget.tsx",
        "components/button.test.tsx", "components/button.spec.tsx",
        "components/button.test.jsx", "components/button.spec.jsx",
        "lib/parser.test.js", "lib/parser.spec.js", "lib/parser.test.ts", "lib/parser.spec.ts",
    ],
)
def test_recognizes_common_test_file_patterns(path):
    assert _is_test(path)


@pytest.mark.parametrize("path", ["src/contest.py", "src/latest.ts", "src/testimony.js"])
def test_does_not_match_test_substrings(path):
    assert not _is_test(path)


def test_recognizes_configuration_inside_monorepo_package():
    assert _is_config("packages/ui/package.json")
    assert _is_config("packages/api/pyproject.toml")
    assert _is_config(".github/workflows/tests.yml")


def test_context_budget_truncates_and_reports_omissions():
    context = ContextCollector(FakeClient(), max_characters=10, max_files=2).collect(make_pr())
    assert context.total_characters == 10
    assert context.total_characters <= context.max_characters
    assert any(item.truncated for item in context.files)
    assert context.omitted_files == ["src/parser.py"]
    assert any("Omitted 1" in warning for warning in context.warnings)


def test_small_budget_prioritizes_config_and_test_before_source():
    context = ContextCollector(FakeClient(), max_characters=30, max_files=2).collect(make_pr())
    assert [item.category for item in context.files] == ["config", "test"]
    assert all(item.content for item in context.files)
    assert context.omitted_files == ["src/parser.py"]
    assert context.total_characters <= 30


def test_file_limit_represents_config_test_and_source_when_possible():
    context = ContextCollector(FakeClient(), max_characters=100, max_files=3).collect(make_pr())
    assert [item.category for item in context.files] == ["config", "test", "source"]


def test_total_budget_includes_metadata_patches_and_files():
    context = ContextCollector(FakeClient(), max_characters=25).collect(make_pr())
    assert context.pr_title
    assert context.patches
    assert context.files
    assert context.total_characters <= 25
    assert any("truncated" in warning.lower() for warning in context.warnings)


def test_tree_failure_falls_back_to_changed_files():
    client = FakeClient()
    client.get_tree = lambda *args: (_ for _ in ()).throw(GitHubAPIError("boom"))
    context = ContextCollector(client).collect(make_pr())
    assert [item.path for item in context.files] == ["src/parser.py"]
    assert any("Could not list" in warning for warning in context.warnings)
