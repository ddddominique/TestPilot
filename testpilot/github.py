"""Minimal GitHub REST API client used by TestPilot."""

import base64
import binascii
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
                error_payload = response.json()
                detail = (
                    error_payload.get("message", response.text)
                    if isinstance(error_payload, dict) else response.text
                )
            except ValueError:
                detail = response.text or "unknown error"
            detail = str(detail or "unknown error")
            remaining = response.headers.get("X-RateLimit-Remaining")
            retry_after = response.headers.get("Retry-After")
            rate_limited = (
                response.status_code == 429
                or (response.status_code == 403 and remaining == "0")
                or (response.status_code == 403 and retry_after is not None)
                or (
                    response.status_code == 403
                    and ("rate limit" in detail.lower() or "abuse detection" in detail.lower())
                )
            )
            if rate_limited:
                reset = response.headers.get("X-RateLimit-Reset", "unknown")
                kind = "secondary" if retry_after or remaining != "0" else "primary"
                retry = f", retry after: {retry_after}s" if retry_after else ""
                raise GitHubRateLimitError(
                    f"GitHub {kind} API rate limit exceeded "
                    f"(reset timestamp: {reset}{retry}). "
                    "Wait before retrying; GITHUB_TOKEN raises the primary limit.",
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
        if not isinstance(data, dict):
            raise GitHubAPIError("Malformed pull request response: expected an object")
        try:
            title = data["title"]
            base_sha = data["base"]["sha"]
            head_sha = data["head"]["sha"]
        except (KeyError, TypeError) as exc:
            raise GitHubAPIError("Malformed pull request response: missing required fields") from exc
        if not all(isinstance(value, str) for value in (title, base_sha, head_sha)):
            raise GitHubAPIError("Malformed pull request response: invalid required fields")
        body = data.get("body")
        if body is not None and not isinstance(body, str):
            raise GitHubAPIError("Malformed pull request response: invalid body")
        file_data = list(self._paginate(f"{root}/pulls/{number}/files"))
        warnings: list[str] = []
        files: list[ChangedFile] = []
        for item in file_data:
            if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
                raise GitHubAPIError("Malformed changed-files response")
            patch = item.get("patch")
            filename = item["filename"]
            if patch is not None and not isinstance(patch, str):
                raise GitHubAPIError(f"Malformed patch for {filename}")
            if patch is None:
                warnings.append(
                    f"Patch unavailable for {filename}; it may be binary or omitted by GitHub."
                )
            elif _patch_may_be_incomplete(patch, item.get("additions"), item.get("deletions")):
                warnings.append(
                    f"Patch for {filename} may be incomplete; GitHub does not report patch truncation explicitly."
                )
            numeric_fields = (item.get("additions", 0), item.get("deletions", 0), item.get("changes", 0))
            if any(not isinstance(value, int) or isinstance(value, bool) for value in numeric_fields):
                raise GitHubAPIError(f"Malformed change counts for {filename}")
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
            title=title,
            description=body or "",
            base_sha=base_sha,
            head_sha=head_sha,
            files=files,
            warnings=warnings,
        )

    def get_tree(self, owner: str, repository: str, sha: str) -> tuple[list[str], bool]:
        payload = self._get(
            f"{API_ROOT}/repos/{owner}/{repository}/git/trees/{sha}",
            {"recursive": "1"},
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("tree"), list):
            raise GitHubAPIError("Malformed repository tree response")
        if "truncated" in payload and not isinstance(payload["truncated"], bool):
            raise GitHubAPIError("Malformed repository tree response: invalid truncated flag")
        paths: list[str] = []
        for item in payload["tree"]:
            if not isinstance(item, dict):
                raise GitHubAPIError("Malformed repository tree entry")
            if item.get("type") == "blob":
                if not isinstance(item.get("path"), str):
                    raise GitHubAPIError("Malformed repository tree path")
                paths.append(item["path"])
        return paths, bool(payload.get("truncated"))

    def get_file_content(self, owner: str, repository: str, path: str, ref: str) -> str:
        payload = self._get(
            f"{API_ROOT}/repos/{owner}/{repository}/contents/{path}", {"ref": ref}
        )
        if not isinstance(payload, dict) or payload.get("type") != "file":
            raise GitHubAPIError(f"{path} is not a file")
        if payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
            raise GitHubAPIError(f"Content unavailable for {path}")
        try:
            encoded = "".join(payload["content"].split())
            raw = base64.b64decode(encoded, validate=True)
            return raw.decode("utf-8")
        except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
            raise GitHubAPIError(f"Could not decode text content for {path}") from exc


def _patch_may_be_incomplete(patch: str, additions: Any, deletions: Any) -> bool:
    """Infer possible omission from diff counts, never from patch byte length alone."""
    if not isinstance(additions, int) or not isinstance(deletions, int):
        return False
    added = sum(line.startswith("+") and not line.startswith("+++") for line in patch.splitlines())
    deleted = sum(line.startswith("-") and not line.startswith("---") for line in patch.splitlines())
    return added < additions or deleted < deletions
