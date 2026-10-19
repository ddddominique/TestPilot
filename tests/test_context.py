from testpilot.context import ContextCollector
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
    assert [item.category for item in context.files] == ["source", "test", "config"]


def test_context_budget_truncates_and_reports_omissions():
    context = ContextCollector(FakeClient(), max_characters=10, max_files=2).collect(make_pr())
    assert context.total_characters == 10
    assert context.files[1].truncated
    assert context.omitted_files == ["pyproject.toml"]
    assert any("Omitted 1" in warning for warning in context.warnings)


def test_tree_failure_falls_back_to_changed_files():
    client = FakeClient()
    client.get_tree = lambda *args: (_ for _ in ()).throw(GitHubAPIError("boom"))
    context = ContextCollector(client).collect(make_pr())
    assert [item.path for item in context.files] == ["src/parser.py"]
    assert any("Could not list" in warning for warning in context.warnings)
