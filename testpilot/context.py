"""Select and download a bounded, test-oriented repository context."""

from pathlib import PurePosixPath

from .github import GitHubAPIError, GitHubClient
from .models import CollectedContext, ContextFile, ContextPatch, PullRequest


SOURCE_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rb", ".rs",
    ".php", ".cs", ".cpp", ".cc", ".c", ".h", ".hpp", ".kt", ".swift",
}
CONFIG_NAMES = {
    "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "jest.config.js",
    "jest.config.ts", "vitest.config.js", "vitest.config.ts", "package.json",
    "requirements.txt", "requirements-dev.txt", "pom.xml", "build.gradle",
    "go.mod", "cargo.toml",
}
TEST_FILE_SUFFIXES = (
    ".test.js", ".spec.js", ".test.jsx", ".spec.jsx",
    ".test.ts", ".spec.ts", ".test.tsx", ".spec.tsx",
)


def _is_test(path: str) -> bool:
    lower = path.lower()
    name = PurePosixPath(lower).name
    parts = PurePosixPath(lower).parts
    return (
        "tests" in parts
        or "test" in parts
        or "__tests__" in parts
        or (name.startswith("test_") and name.endswith(".py"))
        or name.endswith("_test.py")
        or name.endswith(TEST_FILE_SUFFIXES)
    )


def _is_config(path: str) -> bool:
    lower = path.lower()
    return (
        PurePosixPath(lower).name in CONFIG_NAMES
        or lower.startswith(".github/workflows/")
    )


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
        # Configuration and representative tests come first. The fair-share
        # allocator below also reserves space for every selected item, so a
        # large early file cannot consume the complete budget.
        candidates = _prioritize_files(configs, tests, sources, self.max_files)

        contents: list[tuple[str, str, str]] = []
        omitted: list[str] = []
        for path in candidates:
            try:
                content = self.client.get_file_content(
                    pull_request.owner, pull_request.repository, path, pull_request.head_sha
                )
            except GitHubAPIError as exc:
                warnings.append(f"Could not collect {path}: {exc}")
                omitted.append(path)
                continue
            category = "test" if path in tests else "config" if path in configs else "source"
            contents.append((path, category, content))

        omitted.extend(path for path in (configs + tests + sources) if path not in candidates)

        # Budget all future LLM input, not just repository files. Missing
        # patches remain warnings and do not create empty budget fragments.
        fragments: list[tuple[str, str, str]] = [
            ("metadata", "title", pull_request.title),
            ("metadata", "description", pull_request.description),
        ]
        fragments.extend(
            ("patch", item.filename, item.patch)
            for item in pull_request.files
            if item.patch is not None
        )
        fragments.extend((category, path, content) for path, category, content in contents)

        allocated = _allocate_fairly(fragments, self.max_characters)
        title = ""
        description = ""
        patches: list[ContextPatch] = []
        collected: list[ContextFile] = []
        for category, name, selected, truncated in allocated:
            if category == "metadata":
                if name == "title":
                    title = selected
                else:
                    description = selected
                if truncated:
                    warnings.append(f"PR {name} truncated to fit the total context limit.")
            elif category == "patch":
                patches.append(ContextPatch(name, selected, truncated))
                if truncated:
                    warnings.append(f"Patch context truncated for {name} to fit the total context limit.")
            else:
                collected.append(ContextFile(name, category, selected, truncated))
                if truncated:
                    warnings.append(f"Content truncated for {name} to fit the total context limit.")

        if omitted:
            warnings.append(f"Omitted {len(omitted)} context file(s) because of limits or retrieval errors.")
        return CollectedContext(
            collected, sources, tests, configs, omitted, warnings,
            title, description, patches, self.max_characters,
        )


def _allocate_fairly(
    fragments: list[tuple[str, str, str]], max_characters: int
) -> list[tuple[str, str, str, bool]]:
    """Allocate one total character budget while reserving space for later items."""
    remaining = max_characters
    result: list[tuple[str, str, str, bool]] = []
    for index, (category, name, content) in enumerate(fragments):
        items_left = len(fragments) - index
        allowance = remaining // items_left if items_left else 0
        selected = content[:allowance]
        result.append((category, name, selected, len(selected) < len(content)))
        remaining -= len(selected)
    return result


def _prioritize_files(
    configs: list[str], tests: list[str], sources: list[str], limit: int
) -> list[str]:
    """Select deterministically, representing every available category first."""
    groups = (configs, tests, sources)
    selected: list[str] = []
    for group in groups:
        if group and len(selected) < limit and group[0] not in selected:
            selected.append(group[0])
    for group in groups:
        for path in group:
            if len(selected) >= limit:
                return selected
            if path not in selected:
                selected.append(path)
    return selected
