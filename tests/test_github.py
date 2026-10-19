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
    assert any("Patch unavailable" in warning for warning in result.warnings)
    assert session.get.call_args_list[2].kwargs["params"]["page"] == 2


def test_rate_limit_error_is_specific():
    session = Mock()
    session.get.return_value = response(
        {"message": "rate limit exceeded"}, 403,
        {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "123"},
    )
    with pytest.raises(GitHubRateLimitError, match="GITHUB_TOKEN"):
        GitHubClient(session=session).get_pull_request("https://github.com/a/b/pull/1")


def test_secondary_rate_limit_is_detected_without_zero_remaining():
    session = Mock()
    session.get.return_value = response(
        {"message": "You have exceeded a secondary rate limit."}, 403,
        {"Retry-After": "60", "X-RateLimit-Remaining": "42"},
    )
    with pytest.raises(GitHubRateLimitError, match="secondary.*60s"):
        GitHubClient(session=session).get_pull_request("https://github.com/a/b/pull/1")


def test_http_429_is_rate_limit_even_without_headers():
    session = Mock()
    session.get.return_value = response({"message": "slow down"}, 429)
    with pytest.raises(GitHubRateLimitError):
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


def test_get_file_content_rejects_invalid_base64():
    session = Mock()
    session.get.return_value = response({
        "type": "file", "encoding": "base64", "content": "not!base64",
    })
    with pytest.raises(GitHubAPIError, match="decode"):
        GitHubClient(session=session).get_file_content("a", "b", "x.py", "sha")


@pytest.mark.parametrize(
    "payload",
    [[], {"title": "x", "base": {}, "head": {"sha": "head"}},
     {"title": 7, "base": {"sha": "base"}, "head": {"sha": "head"}}],
)
def test_malformed_pull_request_data_is_rejected(payload):
    session = Mock()
    session.get.return_value = response(payload)
    with pytest.raises(GitHubAPIError, match="Malformed pull request"):
        GitHubClient(session=session).get_pull_request("https://github.com/a/b/pull/1")


def test_missing_patch_is_reported_and_large_complete_patch_is_not_assumed_truncated():
    session = Mock()
    pr = {"title": "x", "body": "", "base": {"sha": "base"}, "head": {"sha": "head"}}
    large_patch = "@@ -1 +1 @@\n-" + ("a" * 12_000) + "\n+" + ("b" * 12_000)
    files = [
        {"filename": "asset.bin", "status": "modified", "additions": 0,
         "deletions": 0, "changes": 0},
        {"filename": "large.txt", "status": "modified", "additions": 1,
         "deletions": 1, "changes": 2, "patch": large_patch},
    ]
    session.get.side_effect = [response(pr), response(files)]
    result = GitHubClient(session=session).get_pull_request("https://github.com/a/b/pull/1")
    assert any("Patch unavailable for asset.bin" in warning for warning in result.warnings)
    assert not any("large.txt" in warning for warning in result.warnings)


def test_patch_count_mismatch_reports_uncertain_completeness():
    session = Mock()
    pr = {"title": "x", "body": "", "base": {"sha": "base"}, "head": {"sha": "head"}}
    files = [{"filename": "x.py", "status": "modified", "additions": 4,
              "deletions": 0, "changes": 4, "patch": "@@ -1 +1 @@\n+one"}]
    session.get.side_effect = [response(pr), response(files)]
    result = GitHubClient(session=session).get_pull_request("https://github.com/a/b/pull/1")
    assert any("may be incomplete" in warning for warning in result.warnings)


def test_pull_request_records_fork_head_repository():
    session = Mock()
    pr = {
        "title": "x", "body": "", "base": {"sha": "base"},
        "head": {"sha": "head", "repo": {"full_name": "fork-owner/fork-repo"}},
    }
    session.get.side_effect = [response(pr), response([])]
    result = GitHubClient(session=session).get_pull_request("https://github.com/a/b/pull/1")
    assert (result.head_owner, result.head_repository) == ("fork-owner", "fork-repo")


def test_archive_download_is_streamed_and_bounded(tmp_path):
    session = Mock()
    archive_response = response({}, headers={"Content-Length": "6"})
    archive_response.iter_content.return_value = [b"abc", b"def"]
    session.get.return_value = archive_response
    destination = tmp_path / "snapshot.tar.gz"
    written = GitHubClient(session=session).download_archive(
        "a", "b", "headsha", destination, max_bytes=10
    )
    assert written == 6
    assert destination.read_bytes() == b"abcdef"
    assert "/tarball/headsha" in session.get.call_args.args[0]


def test_archive_download_rejects_oversized_stream_and_removes_partial_file(tmp_path):
    session = Mock()
    archive_response = response({})
    archive_response.iter_content.return_value = [b"12345", b"67890"]
    session.get.return_value = archive_response
    destination = tmp_path / "snapshot.tar.gz"
    with pytest.raises(GitHubAPIError, match="exceeds"):
        GitHubClient(session=session).download_archive(
            "a", "b", "headsha", destination, max_bytes=6
        )
    assert not destination.exists()
