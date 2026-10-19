"""Shared synchronous pipeline, independent of HTTP and database sessions."""

from dataclasses import dataclass
from typing import Callable, Optional

from .context import ContextCollector
from .execution_models import ExecutionResult
from .executor import DockerRunner, TestExecutor
from .framework_detector import select_framework
from .generation_models import FrameworkDetection, GenerationResult
from .github import GitHubClient
from .models import CollectedContext, PullRequest
from .test_generator import TestGenerator
from .workspace import WorkspaceManager


@dataclass
class PipelineResult:
    pull_request: Optional[PullRequest] = None
    context: Optional[CollectedContext] = None
    framework: Optional[FrameworkDetection] = None
    generation: Optional[GenerationResult] = None
    execution: Optional[ExecutionResult] = None


class PipelineError(RuntimeError):
    def __init__(self, stage: str, partial: PipelineResult, cause: Exception) -> None:
        super().__init__(str(cause))
        self.stage = stage
        self.partial = partial


class PipelineService:
    def __init__(self, github=None, collector_factory=None, generator_factory=None, executor_factory=None, selector=None):
        self._owns_github = github is None
        self.github = github if github is not None else GitHubClient()
        self.collector_factory = collector_factory or ContextCollector
        self.generator_factory = generator_factory or TestGenerator
        self.executor_factory = executor_factory or (
            lambda client: TestExecutor(WorkspaceManager(client), DockerRunner())
        )
        self.selector = selector or select_framework

    def run(
        self,
        pr_url: str,
        generate_tests: bool = True,
        execute: bool = True,
        framework: Optional[str] = None,
        max_context_chars: int = 100_000,
        max_context_files: int = 30,
        max_llm_input_chars: int = 100_000,
        on_generated: Optional[Callable] = None,
    ) -> PipelineResult:
        result = PipelineResult()
        stage = "github"
        try:
            if execute and not generate_tests:
                raise ValueError("Execution requires test generation")
            result.pull_request = self.github.get_pull_request(pr_url)
            stage = "context"
            result.context = self.collector_factory(
                self.github, max_context_chars, max_context_files
            ).collect(result.pull_request)
            if generate_tests or framework:
                stage = "framework"
                result.framework = self.selector(result.context, framework)
            if generate_tests:
                stage = "generation"
                result.generation = self.generator_factory(
                    max_input_characters=max_llm_input_chars
                ).generate(result.pull_request, result.context, result.framework)
                if on_generated:
                    on_generated(result.framework, result.generation)
                if execute:
                    stage = "execution"
                    result.execution = self.executor_factory(self.github).execute(
                        result.pull_request, result.framework, result.generation
                    )
            return result
        except Exception as exc:
            raise PipelineError(stage, result, exc) from exc
        finally:
            if self._owns_github:
                self.github.session.close()
