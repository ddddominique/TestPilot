# TestPilot

TestPilot is an AI-powered pull-request testing agent in development. This repository currently implements **Phase 1 only**: retrieving a GitHub pull request and collecting bounded, test-oriented repository context. It does not call an LLM, generate tests, or execute code.

## Setup

Python 3.9 or newer is recommended.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Public repositories work without authentication. For a higher API rate limit, copy `.env.example` to `.env` and export the token into your shell (the application deliberately does not load `.env` automatically):

```bash
export GITHUB_TOKEN="your_token"
```

Never commit `.env` or a real token.

## Usage

```bash
python main.py --pr https://github.com/owner/repo/pull/123
```

Optional bounds can be changed with `--max-context-chars` and `--max-context-files`.

## Architecture

- `testpilot/github.py` validates PR URLs and talks to the GitHub REST API. It retrieves PR metadata, paginated changed files and patches, repository trees, and file contents. It also normalizes network, API, and rate-limit errors.
- `testpilot/context.py` classifies changed source files, locates related existing tests and common test configuration, downloads content at the PR head SHA, and enforces file/count bounds.
- `testpilot/models.py` contains simple immutable data objects shared between modules.
- `testpilot/cli.py` coordinates retrieval and collection and renders a readable summary.
- `main.py` is the requested executable entry point.

## Tests

Tests mock GitHub HTTP calls; they do not consume API quota.

```bash
pytest -q
```

## Current limitations

- Related tests are selected with filename/path conventions and source-name matching, not dependency analysis.
- GitHub omits patches for binary or very large files and may truncate large patches. TestPilot reports this but cannot reconstruct the missing diff in Phase 1.
- The recursive Git tree API can truncate very large repositories. TestPilot reports this and uses the available tree.
- Only UTF-8 text files are collected. Context is bounded by characters rather than model tokens.
- Unauthenticated GitHub API access has a low hourly rate limit; set `GITHUB_TOKEN` when needed.
