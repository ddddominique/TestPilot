"""Minimal GitHub REST API client used by TestPilot."""

import base64
import os
import re
from typing import Any, Iterator, Optional

import requests

from .models import ChangedFile, PullRequest


API_ROOT = "https://api.github.com"
PR_URL_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/([^/]+)/([^/]+)/pull/(\d+)/?(?:[?#].*)?$",
    re.IGNORECASE,
)


class GitHubError(RuntimeError):
    """Base exception for GitHub client failures."""


class InvalidPullRequestURL(GitHubError):
    """Raised when a URL is not a canonical GitHub pull request URL."""


class GitHubAPIError(GitHubError):
    """Raised for unsuccessful GitHub API responses."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubRateLimitError(GitHubAPIError):
    """Raised when GitHub reports an exhausted API rate limit."""


class GitHubClient:
    def __init__(
        self,
        token: Optional[str] = None,
        session: Optional[requests.Session] = None,
        timeout: float = 20.0,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        token = token if token is not None else os.getenv("GITHUB_TOKEN")
        self.headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "TestPilot/0.1",
        }
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    @staticmethod
    def parse_pr_url(url: str) -> tuple[str, str, int]:
        match = PR_URL_RE.match(url.strip())
        if not match:
            raise InvalidPullRequestURL(
                "Expected a URL like https://github.com/owner/repository/pull/123"
            )
        owner, repository, number = match.groups()
        return owner, repository.removesuffix(".git"), int(number)

    def _get(self, url: str, params: Optional[dict[str, Any]] = None) -> Any:
        try:
            response = self.session.get(
                url, headers=self.headers, params=params, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise GitHubAPIError(f"Could not reach GitHub: {exc}") from exc

        if response.status_code >= 400:
            try:
                detail = response.json().get("message", response.text)
            except (ValueError, AttributeError):
                detail = response.text or "unknown error"
            remaining = response.headers.get("X-RateLimit-Remaining")
            if response.status_code in (403, 429) and remaining == "0":
                reset = response.headers.get("X-RateLimit-Reset", "unknown")
                raise GitHubRateLimitError(
                    f"GitHub API rate limit exceeded (reset timestamp: {reset}). "
                    "Set GITHUB_TOKEN for a higher limit.",
                    response.status_code,
                )
            raise GitHubAPIError(
                f"GitHub API returned {response.status_code}: {detail}",
                response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise GitHubAPIError("GitHub returned an invalid JSON response") from exc

    def _paginate(self, url: str, params: Optional[dict[str, Any]] = None) -> Iterator[Any]:
        page = 1
        while True:
            page_params = dict(params or {})
            page_params.update({"per_page": 100, "page": page})
            payload = self._get(url, page_params)
            if not isinstance(payload, list):
                raise GitHubAPIError("Expected a list from a paginated GitHub endpoint")
            yield from payload
            if len(payload) < 100:
                break
            page += 1

    def get_pull_request(self, pr_url: str) -> PullRequest:
        owner, repository, number = self.parse_pr_url(pr_url)
        root = f"{API_ROOT}/repos/{owner}/{repository}"
        data = self._get(f"{root}/pulls/{number}")
        file_data = list(self._paginate(f"{root}/pulls/{number}/files"))
        warnings: list[str] = []
        files: list[ChangedFile] = []
        for item in file_data:
            patch = item.get("patch")
            filename = item["filename"]
            if patch is None:
                warnings.append(
                    f"Patch unavailable for {filename} (binary or too large for the API)."
                )
            elif item.get("changes", 0) > 0 and len(patch) >= 10000:
                warnings.append(f"Patch may be truncated for {filename}.")
            files.append(
                ChangedFile(
                    filename=filename,
                    status=item.get("status", "unknown"),
                    additions=item.get("additions", 0),
                    deletions=item.get("deletions", 0),
                    changes=item.get("changes", 0),
                    patch=patch,
                    previous_filename=item.get("previous_filename"),
                )
            )
        return PullRequest(
            owner=owner,
            repository=repository,
            number=number,
            title=data.get("title", ""),
            description=data.get("body") or "",
            base_sha=data["base"]["sha"],
            head_sha=data["head"]["sha"],
            files=files,
            warnings=warnings,
        )

    def get_tree(self, owner: str, repository: str, sha: str) -> tuple[list[str], bool]:
        payload = self._get(
            f"{API_ROOT}/repos/{owner}/{repository}/git/trees/{sha}",
            {"recursive": "1"},
        )
        paths = [item["path"] for item in payload.get("tree", []) if item.get("type") == "blob"]
        return paths, bool(payload.get("truncated"))

    def get_file_content(self, owner: str, repository: str, path: str, ref: str) -> str:
        payload = self._get(
            f"{API_ROOT}/repos/{owner}/{repository}/contents/{path}", {"ref": ref}
        )
        if not isinstance(payload, dict) or payload.get("type") != "file":
            raise GitHubAPIError(f"{path} is not a file")
        if payload.get("encoding") != "base64" or "content" not in payload:
            raise GitHubAPIError(f"Content unavailable for {path}")
        try:
            raw = base64.b64decode(payload["content"], validate=False)
            return raw.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise GitHubAPIError(f"Could not decode text content for {path}") from exc
