from unittest.mock import Mock

import pytest

from testpilot.execution_models import ExecutionResult, ExecutionStatus
from testpilot.generation_models import FrameworkDetection, GenerationResult
from testpilot.github import GitHubAPIError
from testpilot.models import CollectedContext, PullRequest
from testpilot.pipeline import PipelineError, PipelineService


def components():
    pr = PullRequest("a", "b", 1, "title", "", "base", "a" * 40, [])
    context = CollectedContext([], ["app.py"], [], [], [])
    github, collector, generator, executor = Mock(), Mock(), Mock(), Mock()
    github.get_pull_request.return_value = pr
    collector.collect.return_value = context
    selected = FrameworkDetection("python", "pytest", ["Python changes"], selection="inferred")
    generation = GenerationResult(summary="tests", proposals=[], warnings=[])
    generator.generate.return_value = generation
    execution = ExecutionResult("a/b", 1, "a" * 40, "pytest", [], ExecutionStatus.PASSED, passed=1)
    executor.execute.return_value = execution
    selector = Mock(return_value=selected)
    pipeline = PipelineService(github, lambda *args: collector, lambda **kwargs: generator, lambda client: executor, selector)
    return pipeline, github, collector, generator, executor, pr, context, selected, generation, execution


def test_shared_pipeline_invokes_existing_components_and_returns_results():
    pipeline, github, collector, generator, executor, pr, context, selected, generation, execution = components()
    callback = Mock()
    result = pipeline.run("https://github.com/a/b/pull/1", on_generated=callback)
    github.get_pull_request.assert_called_once_with("https://github.com/a/b/pull/1")
    collector.collect.assert_called_once_with(pr)
    generator.generate.assert_called_once_with(pr, context, selected)
    callback.assert_called_once_with(selected, generation)
    executor.execute.assert_called_once_with(pr, selected, generation)
    assert result.execution == execution
    assert result.pull_request == pr


def test_shared_pipeline_context_only_does_not_generate_or_execute():
    pipeline, github, collector, generator, executor, *_ = components()
    result = pipeline.run("https://github.com/a/b/pull/1", generate_tests=False, execute=False)
    assert result.context is not None
    generator.generate.assert_not_called()
    executor.execute.assert_not_called()


@pytest.mark.parametrize("stage", ["github", "context", "framework", "generation", "execution"])
def test_pipeline_failures_preserve_partial_metadata_and_stage(stage):
    pipeline, github, collector, generator, executor, *_ = components()
    if stage == "github":
        github.get_pull_request.side_effect = GitHubAPIError("offline")
    elif stage == "context":
        collector.collect.side_effect = RuntimeError("failed")
    elif stage == "framework":
        pipeline.selector.side_effect = ValueError("ambiguous")
    elif stage == "generation":
        generator.generate.side_effect = RuntimeError("no credentials")
    else:
        executor.execute.side_effect = RuntimeError("Docker unavailable")
    with pytest.raises(PipelineError) as error:
        pipeline.run("https://github.com/a/b/pull/1")
    assert error.value.stage == stage
    assert (error.value.partial.pull_request is None) == (stage == "github")
    if stage == "execution":
        assert error.value.partial.generation is not None
