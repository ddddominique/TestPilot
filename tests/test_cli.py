from testpilot.cli import format_summary
from testpilot.models import ChangedFile, CollectedContext, ContextFile, PullRequest


def test_summary_is_readable_and_reports_omissions():
    pr = PullRequest(
        "a", "b", 2, "A title", "", "base", "head",
        [ChangedFile("src/a.py", "modified", 2, 1, 3, None)],
    )
    context = CollectedContext(
        [ContextFile("src/a.py", "source", "code")], ["src/a.py"], [], [],
        ["tests/test_a.py"], ["one warning"], max_characters=100,
    )
    output = format_summary(pr, context)
    assert "a/b#2" in output
    assert "patch unavailable" in output
    assert "Omitted files (1)" in output
    assert "one warning" in output
    assert "4/100 characters" in output
