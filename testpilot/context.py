"""Select and download a bounded, test-oriented repository context."""

from pathlib import PurePosixPath

from .github import GitHubAPIError, GitHubClient
from .models import CollectedContext, ContextFile, PullRequest


SOURCE_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rb", ".rs",
    ".php", ".cs", ".cpp", ".cc", ".c", ".h", ".hpp", ".kt", ".swift",
}
CONFIG_NAMES = {
    "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "jest.config.js",
    "jest.config.ts", "vitest.config.js", "vitest.config.ts", "package.json",
    "requirements.txt", "requirements-dev.txt", "pom.xml", "build.gradle",
    "go.mod", "cargo.toml", ".github/workflows",
}


def _is_test(path: str) -> bool:
    lower = path.lower()
    name = PurePosixPath(lower).name
    parts = PurePosixPath(lower).parts
    return (
        "tests" in parts
        or "test" in parts
        or "__tests__" in parts
        or name.startswith("test_")
        or name.endswith(("_test.py", ".test.js", ".test.ts", ".spec.js", ".spec.ts"))
    )


def _is_config(path: str) -> bool:
    lower = path.lower()
    return any(lower == name or lower.startswith(name + "/") for name in CONFIG_NAMES)


def _is_source(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in SOURCE_SUFFIXES and not _is_test(path)


class ContextCollector:
    def __init__(
        self, client: GitHubClient, max_characters: int = 100_000, max_files: int = 30
    ) -> None:
        if max_characters <= 0 or max_files <= 0:
            raise ValueError("Context limits must be positive")
        self.client = client
        self.max_characters = max_characters
        self.max_files = max_files

    def collect(self, pull_request: PullRequest) -> CollectedContext:
        warnings = list(pull_request.warnings)
        try:
            tree, tree_truncated = self.client.get_tree(
                pull_request.owner, pull_request.repository, pull_request.head_sha
            )
        except GitHubAPIError as exc:
            tree = [item.filename for item in pull_request.files]
            tree_truncated = True
            warnings.append(f"Could not list the repository tree: {exc}")
        if tree_truncated:
            warnings.append("Repository tree was incomplete; some tests or configuration may be absent.")

        changed = [item.filename for item in pull_request.files if item.status != "removed"]
        sources = [path for path in changed if _is_source(path)]
        changed_tests = [path for path in changed if _is_test(path)]
        stems = {PurePosixPath(path).stem.lower().removeprefix("test_") for path in sources}
        discovered_tests = [
            path for path in tree
            if _is_test(path) and any(stem and stem in PurePosixPath(path).stem.lower() for stem in stems)
        ]
        tests = list(dict.fromkeys(changed_tests + discovered_tests))
        configs = [path for path in tree if _is_config(path)]
        candidates = [
            *(path for path in sources),
            *(path for path in tests if path not in sources),
            *(path for path in configs if path not in sources and path not in tests),
        ]

        collected: list[ContextFile] = []
        omitted: list[str] = []
        remaining = self.max_characters
        for path in candidates:
            if len(collected) >= self.max_files or remaining <= 0:
                omitted.append(path)
                continue
            try:
                content = self.client.get_file_content(
                    pull_request.owner, pull_request.repository, path, pull_request.head_sha
                )
            except GitHubAPIError as exc:
                warnings.append(f"Could not collect {path}: {exc}")
                omitted.append(path)
                continue
            truncated = len(content) > remaining
            selected = content[:remaining]
            category = "test" if path in tests else "config" if path in configs else "source"
            collected.append(ContextFile(path, category, selected, truncated))
            remaining -= len(selected)
            if truncated:
                warnings.append(f"Content truncated for {path} to fit the context limit.")

        if omitted:
            warnings.append(f"Omitted {len(omitted)} context file(s) because of limits or retrieval errors.")
        return CollectedContext(collected, sources, tests, configs, omitted, warnings)
