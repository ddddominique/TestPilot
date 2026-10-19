"""Short persistence transactions around the synchronous shared pipeline."""

import hashlib
import json
import os
import re
from typing import Optional
from urllib.parse import unquote, urlsplit

from testpilot.execution_models import ExecutionStatus
from testpilot.pipeline import PipelineError, PipelineResult

from .repository import RunRepository


def sanitized(value: str, limit: Optional[int]) -> str:
    """Redact text; None disables truncation for whole-module size checks."""
    if not isinstance(value, str):
        value = "[INVALID DIAGNOSTIC]" if value is not None else ""
    for key in ("OPENAI_API_KEY", "GITHUB_TOKEN", "DATABASE_URL", "CELERY_BROKER_URL"):
        secret = os.getenv(key)
        if secret:
            value = value.replace(secret, "[REDACTED]")
    for key in ("DATABASE_URL", "CELERY_BROKER_URL"):
        try:
            password = urlsplit(os.getenv(key, "")).password
            if password:
                value = value.replace(unquote(password), "[REDACTED]")
        except ValueError:
            pass
    value = re.sub(r"(?i)(Bearer\s+)\S+", r"\1[REDACTED]", value)
    value = re.sub(r"(?im)(Authorization\s*:\s*)[^\r\n]+", r"\1[REDACTED]", value)
    value = re.sub(r"(?:sk-[A-Za-z0-9_-]+|gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)", "[REDACTED]", value)
    value = re.sub(r"(postgres(?:ql)?(?:\+psycopg)?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", value)
    value = re.sub(r"(rediss?://)[^\s/@]*:[^\s/@]+@", r"\1[REDACTED]@", value)
    value = value.replace("\x00", "")
    raw = value.encode("utf-8", errors="replace")
    if limit is None or len(raw) <= limit:
        return raw.decode("utf-8")
    marker = b"\n[TRUNCATED]"
    if limit < len(marker):
        return marker[:max(0, limit)].decode()
    return raw[:limit - len(marker)].decode("utf-8", errors="ignore") + marker.decode()


def diagnostics(items, count):
    """Do not stringify malformed objects or expand malformed warning strings."""
    if not isinstance(items, (list, tuple)):
        return ["[INVALID DIAGNOSTIC LIST]"]
    return [sanitized(item, 2_000) for item in items[:count]]


def generated_metadata(proposals):
    """Retain whole modules only, identifying any display-only transformation."""
    omitted = "Generated test metadata omitted because its persistence limit or encoding was invalid."
    if len(proposals) > 20:
        return [], [omitted]
    originals = [proposal.model_dump(mode="json") for proposal in proposals]
    try:
        if len(json.dumps(originals, ensure_ascii=False).encode("utf-8")) > 400_000:
            return [], [omitted]
    except UnicodeError:
        return [], [omitted]
    stored = []
    modified = False
    for original in originals:
        code = original["test_code"]
        # The total original payload already fits. Redaction can expand it;
        # reject that case rather than ever truncating an executable module.
        display = sanitized(code, None)
        if len(display.encode("utf-8")) > 400_000:
            return [], [omitted]
        item = {key: (diagnostics(value, 32) if isinstance(value, list)
                      else sanitized(value, 4_000))
                for key, value in original.items() if key != "test_code"}
        item.update(test_code=display, test_code_sha256=hashlib.sha256(code.encode("utf-8")).hexdigest(),
                    test_code_representation="original" if display == code else "redacted_display")
        modified = modified or display != code or any(item[key] != value for key, value in original.items())
        stored.append(item)
    if len(json.dumps(stored, ensure_ascii=False).encode("utf-8")) > 400_000:
        return [], [omitted]
    warnings = (["Stored generated-test metadata was sanitized; redacted_display code is not the original source."]
                if modified else [])
    return stored, warnings


def result_values(result: PipelineResult, failure_stage=None) -> dict:
    values = {"status": "COMPLETED", "warnings": []}
    notices = []
    if result.context is not None:
        values["warnings"].extend(diagnostics(result.context.warnings, 64))
    if result.pull_request is not None:
        pr = result.pull_request
        values.update(repository=f"{pr.owner}/{pr.repository}", pr_number=pr.number, head_sha=pr.head_sha)
    if result.framework is not None:
        selected = result.framework
        values.update(
            framework=selected.framework, framework_selection=selected.selection,
            framework_evidence=diagnostics(selected.evidence, 32),
        )
        values["warnings"].extend(diagnostics(selected.warnings, 64))
    if result.generation is not None:
        generation = result.generation
        values["generated_tests"], notices = generated_metadata(generation.proposals)
        values["warnings"] = notices + values["warnings"]
        values["generation_summary"] = sanitized(generation.summary, 8_000)
        values["warnings"].extend(diagnostics(generation.warnings, 64))
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
        values["warnings"] = diagnostics(execution.warnings, 64 - len(notices)) + values["warnings"]
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
    values["warnings"] = diagnostics(values["warnings"], 64)
    return values


def execute_run(run_id, sessions, pipeline_factory, task_id):
    with sessions() as session:
        record = RunRepository(session).claim(run_id, task_id)
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
