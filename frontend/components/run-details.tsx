"use client";

import Link from "next/link";
import { useState } from "react";
import { isTerminal, resultLabel } from "../lib/api";
import { useRun } from "../lib/use-run";
import { Status, timestamp } from "./status";

function Code({ proposal }: { proposal: Record<string, unknown> }) {
  const [copied, setCopied] = useState("");
  const text = (key: string) => typeof proposal[key] === "string" ? proposal[key] as string : "Not recorded";
  const code = typeof proposal.test_code === "string" ? proposal.test_code : "";
  async function copy() {
    try { await navigator.clipboard.writeText(code); setCopied("Copied"); }
    catch { setCopied("Copy unavailable. Select the code to copy manually."); }
  }
  return <article className="panel code-card"><div className="code-heading"><div><h3 className="mono">{text("suggested_test_file")}</h3><p>Target <code>{text("target_source_file")}</code> · {text("framework")}</p></div><button className="secondary" disabled={!code} onClick={copy}>Copy code</button></div>
    {copied && <p role="status" className="muted px-5 text-xs">{copied}</p>}
    {proposal.test_code_representation === "redacted_display" && <p className="notice warning">Redacted display only. This is not the original generated source and may not be executable.</p>}
    <pre tabIndex={0} aria-label={`Generated code for ${text("suggested_test_file")}`}><code>{code || "Module source was not stored."}</code></pre>
    {typeof proposal.test_code_sha256 === "string" && <p className="digest mono">Original generated source SHA-256: {proposal.test_code_sha256}</p>}
  </article>;
}

export function RunDetails({ id }: { id: string }) {
  const { run, error } = useRun(id);
  if (!run) return <><Link className="back" href="/">← All runs</Link><div className="panel empty" role={error ? "alert" : "status"}>{error || "Loading analysis…"}</div></>;
  const terminal = isTerminal(run.status);
  const bad = run.status === "FAILED" || (run.execution_outcome !== null && run.execution_outcome !== "PASSED");
  const safePr = /^https:\/\/github\.com\/[^/]+\/[^/]+\/pull\/\d+$/.test(run.pr_url);
  return <>
    <Link className="back" href="/">← All runs</Link>
    <div className="section-heading detail-title"><div><div className="eyebrow">PULL REQUEST ANALYSIS</div><h1>{run.repository} <span className="muted">#{run.pr_number}</span></h1><p className="mono text-xs">Run {run.id}</p></div>{safePr && <a className="secondary" href={run.pr_url} target="_blank" rel="noreferrer">View on GitHub ↗</a>}</div>
    {error && <p role="alert" className="notice error">{error} Displaying the last received status.</p>}
    <section className={`panel outcome ${bad ? "outcome-bad" : ""}`} aria-live="polite"><div><Status status={run.status} /><h2>{resultLabel(run)}</h2><p>{!terminal ? "Collecting context, generating tests, and executing in isolation. This page updates automatically." : run.status === "FAILED" ? "The pipeline could not finish successfully. Review the diagnostic below." : run.execution_outcome === "TEST_FAILED" ? "The pipeline completed. Generated tests reported failing assertions—review the evidence below." : "The pipeline finished. Review the recorded outcome and execution evidence below."}</p></div><div className="outcome-symbol" aria-hidden="true">{!terminal ? "◌" : bad ? "!" : "✓"}</div></section>
    <dl className="stats"><div><dt>Passed</dt><dd className="text-emerald-300">{run.passed}</dd></div><div><dt>Failed</dt><dd className="text-rose-300">{run.failed}</dd></div><div><dt>Skipped</dt><dd>{run.skipped}</dd></div><div><dt>Framework</dt><dd className="framework">{run.framework || "—"}<small>{run.framework_selection || "Not selected yet"}</small></dd></div></dl>
    <section className="panel metadata"><dl><div><dt>Created</dt><dd>{timestamp(run.created_at)}</dd></div><div><dt>Queued</dt><dd>{timestamp(run.queued_at)}</dd></div><div><dt>Started</dt><dd>{timestamp(run.started_at)}</dd></div><div><dt>Completed</dt><dd>{timestamp(run.completed_at)}</dd></div><div><dt>Execution outcome</dt><dd>{run.execution_outcome || "Not available"}</dd></div><div><dt>Exit code / duration</dt><dd>{run.exit_code ?? "—"} / {run.duration_seconds === null ? "—" : `${run.duration_seconds.toFixed(2)}s`}</dd></div></dl>{run.head_sha && <p className="mono muted text-xs break-all">Head {run.head_sha}</p>}</section>
    {run.error_details && <section role="alert" className="notice error"><h2>Diagnostic</h2><p className="whitespace-pre-wrap">{run.error_details}</p></section>}
    <section className="result-section"><h2>Summary</h2><div className="panel prose-panel"><p className="whitespace-pre-wrap">{run.generation_summary || (terminal ? "No generation summary was recorded." : "The summary will appear after generation completes.")}</p></div></section>
    <section className="result-section"><h2>Generated tests <span className="counter">{run.generated_tests.length}</span></h2><p className="muted text-sm mb-4">Stored proposals are shown as text, never executed in your browser. Code presence alone does not prove it ran.</p>{run.generated_tests.map((proposal, index) => <Code key={index} proposal={proposal} />)}{run.generated_tests.length === 0 && <p className="panel empty">{terminal ? "No generated modules were stored. Check warnings for omissions." : "Generated test modules will appear when results are saved."}</p>}</section>
    <section className="result-section"><div className="section-heading"><h2>Execution output</h2><span className="small-label">RUNNER LOG</span></div><div className="panel"><pre className="output" tabIndex={0} aria-label="Test runner output">{run.execution_output || (terminal ? "No execution output was recorded." : "Waiting for execution results…")}</pre></div></section>
    {(run.warnings.length > 0 || run.framework_evidence.length > 0) && <details className="panel warnings"><summary>Warnings & framework evidence <span className="counter">{run.warnings.length + run.framework_evidence.length}</span></summary>{run.warnings.length > 0 && <><h3>Warnings</h3><ul>{run.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></>}{run.framework_evidence.length > 0 && <><h3>Framework evidence</h3><ul>{run.framework_evidence.map((evidence, index) => <li key={index}>{evidence}</li>)}</ul></>}</details>}
  </>;
}
