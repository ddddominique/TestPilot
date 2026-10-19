"""Short persistence transactions around the synchronous shared pipeline."""

import json
import os
import re
from urllib.parse import unquote, urlsplit

from testpilot.execution_models import ExecutionStatus
from testpilot.pipeline import PipelineError, PipelineResult

from .repository import RunRepository


def sanitized(value: str, limit: int) -> str:
    """Bound UTF-8 diagnostics and redact configured credentials and common forms."""
    for key in ("OPENAI_API_KEY", "GITHUB_TOKEN", "DATABASE_URL"):
        secret = os.getenv(key)
        if secret:
            value = value.replace(secret, "[REDACTED]")
    try:
        password = urlsplit(os.getenv("DATABASE_URL", "")).password
        if password:
            value = value.replace(unquote(password), "[REDACTED]")
    except ValueError:
        pass
    value = re.sub(r"(?i)(Bearer\s+)\S+", r"\1[REDACTED]", value)
    value = re.sub(r"(?im)(Authorization\s*:\s*)[^\r\n]+", r"\1[REDACTED]", value)
    value = re.sub(r"(?:sk-[A-Za-z0-9_-]+|gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)", "[REDACTED]", value)
    value = re.sub(r"(postgres(?:ql)?(?:\+psycopg)?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", value)
    value = value.replace("\x00", "")
    raw = value.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return raw.decode("utf-8")
    marker = b"\n[TRUNCATED]"
    return raw[:limit - len(marker)].decode("utf-8", errors="ignore") + marker.decode()


def result_values(result: PipelineResult, failure_stage=None) -> dict:
    values = {"status": "COMPLETED", "warnings": []}
    if result.context is not None:
        values["warnings"].extend(result.context.warnings)
    if result.pull_request is not None:
        pr = result.pull_request
        values.update(repository=f"{pr.owner}/{pr.repository}", pr_number=pr.number, head_sha=pr.head_sha)
    if result.framework is not None:
        selected = result.framework
        values.update(
            framework=selected.framework, framework_selection=selected.selection,
            framework_evidence=[sanitized(item, 2_000) for item in selected.evidence[:32]],
        )
        values["warnings"].extend(selected.warnings)
    if result.generation is not None:
        generation = result.generation
        # Preserve complete code rather than silently truncating a test module.
        encoded = json.dumps([item.model_dump(mode="json") for item in generation.proposals], ensure_ascii=False)
        if len(encoded.encode("utf-8")) <= 400_000 and len(generation.proposals) <= 20:
            # Serialize strings individually: redaction must not corrupt JSON escapes.
            values["generated_tests"] = [
                {key: ([sanitized(entry, 4_000) for entry in item[:32]] if isinstance(item, list)
                       else sanitized(item, 400_000)) for key, item in proposal.model_dump().items()}
                for proposal in generation.proposals
            ]
            if len(json.dumps(values["generated_tests"], ensure_ascii=False).encode("utf-8")) > 400_000:
                values["generated_tests"] = []
                values["warnings"].append("Generated test metadata omitted after redaction exceeded its size limit.")
            elif json.dumps(values["generated_tests"], ensure_ascii=False) != encoded:
                values["warnings"].append("Stored generated-test metadata was sanitized; redacted code may differ from executed code.")
        else:
            values["generated_tests"] = []
            values["warnings"].append("Generated test metadata omitted because its persistence limit was exceeded.")
        values["generation_summary"] = sanitized(generation.summary, 8_000)
        values["warnings"].extend(generation.warnings)
    if result.execution is not None:
        execution = result.execution
        values.update(
            execution_outcome=execution.status.value, passed=execution.passed, failed=execution.failed,
            skipped=execution.skipped, exit_code=execution.exit_code,
            duration_seconds=execution.duration_seconds,
            execution_output=sanitized(execution.output, 64_000),
            error_details=sanitized(execution.error, 8_000) or None,
        )
        # Keep cleanup/security diagnostics ahead of potentially many context
        # omissions when the persisted warning list reaches its cap.
        values["warnings"] = list(execution.warnings) + values["warnings"]
        if execution.status == ExecutionStatus.INFRASTRUCTURE_ERROR:
            values["status"] = "FAILED"
    elif failure_stage is None:
        failure_stage = "execution"
    if failure_stage is not None:
        values["status"] = "FAILED"
        # Exception messages may contain tokens, HTTP headers, SQL, or source
        # snippets. Persist an allowlisted stage diagnostic, never raw exceptions.
        stage = failure_stage if failure_stage in {"github", "context", "framework", "generation", "execution"} else "pipeline"
        values["error_details"] = f"Pipeline failed during {stage}; check service configuration and availability."
        if stage == "execution":
            values["execution_outcome"] = ExecutionStatus.INFRASTRUCTURE_ERROR.value
    values["warnings"] = [sanitized(str(item), 2_000) for item in values["warnings"][:64]]
    return values


def execute_run(run_id, sessions, pipeline_factory):
    with sessions() as session:
        record = RunRepository(session).claim(run_id)
        pr_url = record.pr_url
    # No ORM object, session, or checked-out connection reaches the pipeline.
    try:
        result = pipeline_factory().run(pr_url)
        values = result_values(result)
    except PipelineError as exc:
        values = result_values(exc.partial, exc.stage)
    except Exception:
        values = result_values(PipelineResult(), "pipeline")
    with sessions() as session:
        return RunRepository(session).finish(run_id, values)
