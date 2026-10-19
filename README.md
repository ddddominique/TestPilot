# TestPilot

TestPilot is an AI-powered pull-request testing agent. It currently implements:

- **Phase 1:** retrieve GitHub PR metadata, changed files, patches, and bounded repository context.
- **Phase 2:** detect pytest, Jest, or Vitest and optionally use OpenAI Structured Outputs to produce validated test proposals.

Generated tests are proposals only. TestPilot does not execute them, write them to the repository, create commits, or post PR comments. Phase 3 execution, webhooks, and a frontend are not implemented.

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

Unknown or equally conflicting evidence stops generation with a clear error.

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
- Repository-relative string validation is not a filesystem containment guarantee. Phase 3 must perform real-path and symlink containment checks before writing or executing anything.
- Overwrite detection uses the GitHub tree returned in Phase 1. If GitHub truncates that tree, only known paths can be rejected.
- Structured outputs reduce malformed responses but cannot guarantee that proposed test logic is correct or non-duplicative.
- Fair sharing prevents starvation but may truncate several inputs instead of preserving one large input in full.
- Unauthenticated GitHub API access has a low hourly rate limit; set `GITHUB_TOKEN` when needed.
