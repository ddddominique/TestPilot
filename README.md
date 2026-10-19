# TestPilot

TestPilot is an AI-powered pull-request testing agent. It currently implements:

- **Phase 1:** retrieve GitHub PR metadata, changed files, patches, and bounded repository context.
- **Phase 2:** detect pytest, Jest, or Vitest and optionally use OpenAI Structured Outputs to produce validated test proposals.
- **Phase 3:** opt-in execution of generated pytest modules against an exact PR head snapshot inside Docker.

Without `--execute`, generated tests remain proposals only. Execution materializes them in temporary storage outside your project. TestPilot never modifies the target repository, creates commits, or posts PR comments. Webhooks and a frontend are not implemented.

## Setup

Python 3.9 or newer is recommended.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Public repositories work without authentication. For a higher GitHub API rate limit, copy `.env.example` to `.env` and export the token into your shell (the application deliberately does not load `.env` automatically):

```bash
export GITHUB_TOKEN="your_token"
```

Phase 2 also requires an OpenAI API key. `OPENAI_MODEL` is optional and defaults to `gpt-4o-mini`; choose a model that supports Structured Outputs.

```bash
export OPENAI_API_KEY="your_openai_api_key"
export OPENAI_MODEL="gpt-4o-mini"
```

Never commit `.env`, GitHub tokens, or OpenAI API keys.

## Usage

```bash
python main.py --pr https://github.com/owner/repo/pull/123
```

Optional bounds can be changed with `--max-context-chars` and `--max-context-files`. The character limit applies to the combined PR title, description, available patches, source files, tests, and testing configuration—not only downloaded files. The summary reports truncation and omission explicitly.

Generate structured test proposals with:

```bash
python main.py --pr https://github.com/owner/repo/pull/123 --generate-tests
```

`--max-llm-input-chars` controls the approximate combined system-instruction and formatted-context character budget. Character counts are not exact model token counts.

## Context selection

TestPilot recognizes common Python and JavaScript/TypeScript test conventions, including `tests/`, `test/`, `__tests__/`, `test_*.py`, `*_test.py`, and `.test`/`.spec` files using JS, JSX, TS, or TSX.

Selection is deterministic:

1. Changed source files and their diff hunks.
2. Common testing configuration such as `pyproject.toml`, `pytest.ini`, `package.json`, and Jest/Vitest configuration.
3. Changed tests and representative existing tests whose names correspond to changed source files.

When the file-count limit permits, at least one file from every available category is selected before remaining slots are filled in priority order. Fair-share character allocation prevents a large diff or source file from starving every later section. Truncation prefers complete lines and complete diff hunks when practical.

The LLM formatter always preserves the complete allowed-target list, detected framework evidence, and critical collection warnings. If those mandatory sections cannot fit, generation fails with the minimum required character budget instead of silently removing them. Optional patches, source, configuration, tests, and PR metadata remain deterministic and bounded.

## Error handling

The client validates PR URLs and required API response fields, paginates changed files, strictly decodes base64 file content, and converts network and HTTP failures into readable errors. Primary, secondary, and HTTP 429 rate limits are identified separately where GitHub provides enough information. Missing patches are reported. Potential patch incompleteness is inferred from GitHub's addition/deletion counts rather than patch length, and is described as uncertain because the API has no explicit truncation flag.

## Architecture

- `testpilot/github.py` validates PR URLs and talks to the GitHub REST API. It retrieves PR metadata, paginated changed files and patches, repository trees, and file contents. It also normalizes network, API, and rate-limit errors.
- `testpilot/context.py` classifies changed source files, locates related tests and testing configuration, prioritizes representative context, and enforces one reusable total-input budget.
- `testpilot/models.py` contains simple immutable data objects shared between modules.
- `testpilot/framework_detector.py` parses recognized TOML, INI, requirements, and `package.json` dependency locations and syntax-aware imports. It does not treat arbitrary comments, documentation, or string literals as evidence. Path proximity favors the changed package in monorepos; tied or absent evidence produces `unknown` rather than a guess.
- `testpilot/generation_models.py` defines Pydantic structured-output models and repository-relative path validation.
- `testpilot/test_generator.py` formats deterministic untrusted context and calls the official OpenAI SDK's structured-output parser. API access is isolated from GitHub retrieval and is injectable for tests.
- `testpilot/cli.py` preserves Phase 1 behavior and coordinates detection/generation only when `--generate-tests` is supplied.
- `main.py` is the requested executable entry point.

The prompt includes bounded PR metadata, diff patches, testing configuration, representative existing tests, changed source, collection warnings, and framework evidence. Patches precede file bodies. Repository content is explicitly delimited as untrusted data and cannot provide system instructions.

## Proposal validation

Every proposal is defined as a complete new test module, not content to append to an existing file. The prompt requires required imports, repository-supported public APIs, and explicit limitations when context is insufficient.

Validation currently enforces:

- exact target matching against changed source files;
- raw repository-relative paths with no absolute paths, backslashes, repeated separators, `.`/`..`, control characters, `.git`, or `.ssh` components;
- pytest, Jest, or Vitest test-file naming conventions;
- no overwrite of a path found in the retrieved repository tree;
- valid Python syntax for pytest proposals using `ast.parse`;
- structured-response and output-size limits.

These are non-executing checks. They do not prove that imports resolve or tests pass.

## Supported frameworks

- Python: pytest
- JavaScript/TypeScript: Jest
- JavaScript/TypeScript: Vitest

Framework selection first preserves a compatible evidence-based detection. When no supported framework evidence exists, it examines only the relevant changed source paths: Python-only PRs infer pytest, explicitly labeled `inferred` in prompts and CLI reports. Other languages elsewhere in the repository do not affect that fallback. For example, a multilingual repository whose PR only adds `leetcode/2089.py` can generate pytest proposals without prior test configuration.

Use `--framework pytest`, `--framework jest`, or `--framework vitest` to override selection explicitly. Pytest requires all relevant changed sources to be Python; Jest/Vitest require JavaScript/TypeScript. Mixed-language, unsupported, empty-source, and incompatible changes fail clearly. Without an override, conflicting framework evidence still fails; JS/TS changes with no evidence require an explicit choice between Jest and Vitest. The selected framework and its `detected`, `inferred`, or `override` origin and evidence are carried through generation, validation, execution, and reports. Execution remains pytest-only.

```bash
python main.py --pr https://github.com/ddddominique/Coding-Exercise/pull/1 --generate-tests
python main.py --pr https://github.com/ddddominique/Coding-Exercise/pull/1 --generate-tests --framework pytest
```

An inferred framework does not imply that imports, dependencies, or test proposals are verified. Existing validation and opt-in execution restrictions still apply.

## Source paths in generated Python tests

Repository sources and generated tests are mounted separately: `/workspace/repo/<source path>` and `/workspace/generated/<test path>`. A generated test's `__file__` belongs to the generated-test mount, so `Path(__file__).with_name("2089.py")` does not locate `leetcode/2089.py` in the snapshot.

The trusted pytest runner sets `TESTPILOT_SOURCE_ROOT=/workspace/repo` before collecting generated tests. Ordinary supported package imports continue to use the repository root and `src/` import paths. For standalone files with names that cannot be normal Python module identifiers, generated modules must use the complete repository-relative source path against this root. For example:

```python
import importlib.util
import os
from pathlib import Path
import sys

source_path = Path(os.environ["TESTPILOT_SOURCE_ROOT"]) / "leetcode/2089.py"
spec = importlib.util.spec_from_file_location("solution_2089", source_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

def test_target_indices():
    assert module.Solution().targetIndices([1, 2, 5, 2, 3], 2) == [1, 2]
```

The generation prompt specifies this contract and includes a loader example. Syntax-only validation also rejects recognizable target-source paths derived from `__file__` (including common single-assignment aliases) or rooted in `/workspace/generated`. The check runs again before materialization. Test-local fixture paths using `__file__` remain supported when they do not refer to the target source. Dynamic path construction cannot be fully proved correct by these checks; import behavior and assertions still require isolated execution. Source files are never copied alongside generated tests or rewritten to make incorrect paths work.

Changing the trusted runner requires rebuilding its provenance record with `python -m testpilot.build_runner` before execution. If you manually run exported proposals elsewhere, supply `TESTPILOT_SOURCE_ROOT` pointing to that checkout; TestPilot itself continues to execute proposals only in Docker.

## Tests

Tests mock GitHub HTTP calls; they do not consume API quota.

```bash
python -m pytest -q
```

## Current limitations

- Related tests are selected with filename/path conventions and source-name matching, not dependency or semantic analysis.
- GitHub can omit patches for binary or large files and does not expose a definitive patch-truncation flag. TestPilot reports missing patches and count-based signs of incompleteness but cannot reconstruct omitted diff data.
- The recursive Git tree API can truncate very large repositories. TestPilot reports this and uses the available tree.
- Only recognized source types and UTF-8 text files are collected.
- Budgets are measured in characters, not model-specific tokens. Token-aware accounting remains a future improvement.
- Framework detection is evidence-based but intentionally conservative; custom runners and unusual configuration may return `unknown`.
- Generated proposals are schema- and path-validated but have not been compiled, executed, or checked against a live repository checkout.
- Python proposals receive syntax-only validation using the Python version running TestPilot. JavaScript and TypeScript are not syntax-validated because no JS/TS parser is included.
- Repository-relative string validation alone is not a filesystem containment guarantee. Phase 3 also checks resolved paths and rejects archive symlinks before materialization.
- Overwrite detection uses the GitHub tree returned in Phase 1. If GitHub truncates that tree, only known paths can be rejected.
- Structured outputs reduce malformed responses but cannot guarantee that proposed test logic is correct or non-duplicative.
- Fair sharing prevents starvation but may truncate several inputs instead of preserving one large input in full.
- Unauthenticated GitHub API access has a low hourly rate limit; set `GITHUB_TOKEN` when needed.

## Phase 3: opt-in Docker execution

Install Docker Desktop or Docker Engine and start its daemon. Build the trusted runner from this repository before first execution:

```bash
python -m testpilot.build_runner
python main.py --pr https://github.com/owner/repo/pull/123 --generate-tests --execute
```

`--execute` requires `--generate-tests`. The runner uses Python 3.12.8 and pytest 8.3.5, with pytest's dependencies and Python's standard library. The Dockerfile pins the official Python multi-platform manifest to `sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0`. This digest was verified against Docker Hub's registry by comparing the response's `Docker-Content-Digest` with SHA-256 of the retrieved manifest bytes for `library/python:3.12.8-slim-bookworm`.

Review `runner/Dockerfile` and `runner/pytest_runner.py`, then run the build module above. It copies only those two files into a temporary build context, invokes Docker with `--iidfile`, and records the returned immutable image ID together with a hash of both reviewed source files in `.testpilot/runner-image.json` (gitignored). Execution verifies the record and current source hash, inspects the recorded image ID, and runs exactly that ID with `--pull never`. Retagging another local image does not change the image selected. Missing, invalid, or stale records fail closed and require rebuilding. A direct `docker build` alone does not establish this trust record.

This is local build provenance: the host, Docker daemon, reviewed checkout, build dependencies, and trust record must be trusted. A user able to modify the trust record or Docker daemon can replace that trust. The record is not a cryptographic publisher signature. Build-time package registries remain a supply-chain dependency; test containers have no networking.

Execution flow:

1. `workspace.py` downloads the GitHub tarball using the full PR head SHA and fork repository metadata when available. Downloads are capped at 100 MB; extraction is capped at 250 MB and 20,000 entries. It rejects traversal, links, special files, sensitive directories, and duplicate files.
2. Tests are syntax checked and created exclusively in a separate temporary directory. Existing snapshot paths, unsafe paths, symlink traversal, and duplicate proposal paths are rejected. Snapshot and test mounts are read-only.
3. `executor.py` starts the trusted image with a fixed command, no network, UID/GID 65532, all capabilities dropped, `no-new-privileges`, a read-only root filesystem, 512 MB memory, one CPU, 64 processes, and a 64 MB temporary filesystem. No credentials, Docker socket, or host project directory are mounted. The default deadline is 60 seconds, followed by forced container removal. Docker log storage is disabled and host output capture is bounded to 100 KB.
4. `runner/pytest_runner.py` runs only the explicit generated paths. It disables repository configuration, `conftest.py`, and automatic plugin loading. Python starts in isolated mode before the runner adds the snapshot and its `src/` directory to the import path. No setup scripts or dependency installation run.
5. `execution_models.py` validates every required payload field without coercion, enforces bounded output/diagnostics, and checks status/count/exit-code consistency. Malformed, missing, oversized, truncated, or inconsistent reports fail as infrastructure errors. It carries status, commit, files, counts, duration, output, and errors back to the CLI.

Container cleanup is always attempted. If removal fails, overall status becomes `INFRASTRUCTURE_ERROR` and the CLI exits nonzero, even if tests passed. Counts, output, exit code, duration, and any original execution error remain intact. A warning records the original status, names the container, and gives `docker rm -f <container-name>` for recovery. An incomplete removal may leave the restricted container running. Temporary workspace cleanup is still attempted.

Example report:

```text
Execution results:
Repository: owner/repo#123
Head commit: <40-character head SHA>
Framework: pytest
Docker execution started: yes
Generated test files: tests/test_add_generated.py
Overall status: TEST_FAILED
Tests: 0 passed, 1 failed, 0 skipped
Details: One or more generated tests failed. This may indicate a regression or an incorrect generated test.
```

Statuses include `PASSED`, `TEST_FAILED`, `COLLECTION_ERROR`, `SYNTAX_ERROR`, `DEPENDENCY_ERROR`, `TIMEOUT`, `INFRASTRUCTURE_ERROR`, and `UNSUPPORTED`. Jest/Vitest execution is unsupported, while generation still works. Zero proposals, zero collected tests, and all-skipped runs are not reported as passed. CLI exit code is 0 for successful execution and 1 for unsuccessful execution; invalid CLI flag combinations exit 2.

Import classification uses the exception type and missing module name, not matches in printed text. A `ModuleNotFoundError` whose top-level module is absent from both snapshot import roots produces `DEPENDENCY_ERROR`, with an explicit warning that a generated import may be incorrect. A missing submodule of a known project module, or a generic `ImportError` such as an invented imported function, retains collection/test failure classification. Diagnostics include the test node, exception type, and original import message. A completely absent project module cannot always be distinguished from an external dependency; review the evidence. No dependencies are installed from repository declarations. Existing fixtures and repository plugins are deliberately unavailable unless the generated module itself imports code.

Docker reduces risk but is not a perfect security boundary. This is a local development MVP, not a production multi-tenant sandbox. Untrusted tests execute in the same Python process as pytest and can interfere with reporting; results are evidence for review, not tamper-proof attestations. GitHub snapshots omit submodule contents and do not hydrate Git LFS. The archive is selected by immutable SHA through GitHub's API, not independently verified against a Git object database. Generated modules run under Python 3.12 in the container even if the host uses another version.

## Optional Docker fixture demonstration

Normal unit tests mock GitHub, OpenAI, and Docker, and require no daemon or credentials:

```bash
python -m pytest -q
```

The optional integration tests prepare trusted archives, verify immutable SHA selection, and materialize complete generated modules. They exercise passing and failing `add()` assertions plus source-root-based loading of `leetcode/2089.py` and a deeper nested numeric filename from a separate generated-test mount. They require no live GitHub PR or OpenAI request:

```bash
python -m testpilot.build_runner
TESTPILOT_DOCKER_INTEGRATION=1 python -m pytest -q -s tests/test_docker_integration.py
```

The integration scenarios are skipped unless explicitly enabled and Docker plus the runner image are available. A skipped test does not demonstrate container execution.
