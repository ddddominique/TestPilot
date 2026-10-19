import base64
from unittest.mock import Mock

import pytest
import requests

from testpilot.github import (
    GitHubAPIError,
    GitHubClient,
    GitHubRateLimitError,
    InvalidPullRequestURL,
)


def response(payload, status=200, headers=None):
    item = Mock()
    item.status_code = status
    item.headers = headers or {}
    item.json.return_value = payload
    item.text = str(payload)
    return item


def test_parse_pr_url():
    assert GitHubClient.parse_pr_url("https://github.com/acme/widget/pull/42") == (
        "acme", "widget", 42
    )
    with pytest.raises(InvalidPullRequestURL):
        GitHubClient.parse_pr_url("https://example.com/acme/widget/pull/42")


def test_get_pull_request_paginates_and_warns_for_missing_patch():
    session = Mock()
    pr = {"title": "Improve parser", "body": None, "base": {"sha": "base"}, "head": {"sha": "head"}}
    first_page = [
        {"filename": f"src/f{i}.py", "status": "modified", "additions": 1,
         "deletions": 0, "changes": 1, "patch": "@@"}
        for i in range(100)
    ]
    second_page = [{"filename": "logo.png", "status": "modified", "additions": 0,
                    "deletions": 0, "changes": 0}]
    session.get.side_effect = [response(pr), response(first_page), response(second_page)]

    result = GitHubClient(session=session).get_pull_request(
        "https://github.com/acme/widget/pull/7"
    )

    assert len(result.files) == 101
    assert result.description == ""
    assert "Patch unavailable" in result.warnings[0]
    assert session.get.call_args_list[2].kwargs["params"]["page"] == 2


def test_rate_limit_error_is_specific():
    session = Mock()
    session.get.return_value = response(
        {"message": "rate limit exceeded"}, 403,
        {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "123"},
    )
    with pytest.raises(GitHubRateLimitError, match="GITHUB_TOKEN"):
        GitHubClient(session=session).get_pull_request("https://github.com/a/b/pull/1")


def test_api_and_network_errors_are_wrapped():
    session = Mock()
    session.get.return_value = response({"message": "Not Found"}, 404)
    with pytest.raises(GitHubAPIError, match="404"):
        GitHubClient(session=session).get_pull_request("https://github.com/a/b/pull/1")
    session.get.side_effect = requests.ConnectionError("offline")
    with pytest.raises(GitHubAPIError, match="Could not reach"):
        GitHubClient(session=session).get_pull_request("https://github.com/a/b/pull/1")


def test_get_file_content_decodes_base64():
    session = Mock()
    session.get.return_value = response({
        "type": "file", "encoding": "base64",
        "content": base64.b64encode(b"hello\n").decode(),
    })
    content = GitHubClient(session=session).get_file_content("a", "b", "x.py", "sha")
    assert content == "hello\n"
    assert session.get.call_args.kwargs["params"] == {"ref": "sha"}
