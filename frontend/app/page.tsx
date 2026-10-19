"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { api, normalizePrUrl, resultLabel, type RunPage } from "../lib/api";
import { Status, timestamp } from "../components/status";

export default function Overview() {
  const router = useRouter();
  const [url, setUrl] = useState("");
  const [stage, setStage] = useState("");
  const submitting = useRef(false);
  const [error, setError] = useState("");
  const [created, setCreated] = useState<string | null>(null);
  const [history, setHistory] = useState<RunPage | null>(null);
  const [historyError, setHistoryError] = useState("");
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    api.list(controller.signal).then(setHistory).catch(error => { if (!controller.signal.aborted) setHistoryError(error.message); });
    return () => controller.abort();
  }, [refresh]);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (submitting.current) return;
    setError("");
    try {
      const normalized = normalizePrUrl(url);
      submitting.current = true;
      setStage("Creating run…");
      const run = await api.create(normalized);
      setCreated(run.id);
      setStage("Queuing analysis…");
      await api.execute(run.id);
      router.push(`/runs/${run.id}`);
    } catch (error) { setError(error instanceof Error ? error.message : "Could not submit this PR."); }
    finally { submitting.current = false; setStage(""); }
  }
  return <>
    <div className="eyebrow">YOUR PULL REQUESTS, UNDER TEST</div>
    <section className="hero"><div><h1>Confidence in every change<span className="accent">.</span></h1><p>Turn a pull request into regression tests and evidence.<br className="hidden sm:block" /> Let TestPilot check what changed.</p></div><span className="hero-mark" aria-hidden="true">⌁</span></section>
    <section className="panel submit-panel" aria-labelledby="new-run"><div className="section-heading"><div><h2 id="new-run">Analyze a pull request</h2><p>Generate tests. Run them in an isolated Docker sandbox.</p></div><span className="small-label">GITHUB → PYTEST</span></div>
      <form onSubmit={submit} aria-busy={Boolean(stage)}><label htmlFor="pr-url">GitHub pull request URL</label><div className="input-row"><input id="pr-url" type="url" required maxLength={2048} placeholder="https://github.com/owner/repo/pull/123" value={url} onChange={e => setUrl(e.target.value)} aria-describedby="pr-help" disabled={Boolean(stage)} /><button className="primary" disabled={Boolean(stage)}>{stage || (created && error ? "Create another run →" : "Analyze PR →")}</button></div><p id="pr-help" className="muted text-xs">Public repositories supported · No changes are written to your repository</p></form>
      {error && <div role="alert" className="notice error">{error}{created && <p><Link href={`/runs/${created}`}>Inspect the created run →</Link> before submitting another. Queuing may have succeeded even if its response was lost.</p>}</div>}
      {stage && <p role="status" className="muted">{stage}</p>}
    </section>
    <section className="history" aria-labelledby="recent"><div className="section-heading"><div><h2 id="recent">Recent runs <span className="counter">{history?.total ?? "—"}</span></h2><p>Your latest pull request analyses</p></div><button className="secondary" onClick={() => { setHistoryError(""); setRefresh(v => v + 1); }}>Refresh</button></div>
      {historyError && <p role="alert" className="notice error">{historyError}</p>}
      {!history && !historyError && <p role="status" className="empty">Loading recent runs…</p>}
      {history && history.items.length === 0 && <div className="panel empty"><h3>A clear runway</h3><p>Your first analysis will appear here. Paste a pull request URL above to get started.</p></div>}
      {history && history.items.length > 0 && <div className="table-wrap panel"><table><thead><tr><th>Pull request</th><th>Pipeline</th><th>Result</th><th>Created</th></tr></thead><tbody>{history.items.map(run => <tr key={run.id}><td><Link className="repo-link" href={`/runs/${run.id}`}>{run.repository} <span className="muted">#{run.pr_number}</span></Link><div className="mono muted text-xs">{run.id.slice(0, 8)}</div></td><td><Status status={run.status} /></td><td>{resultLabel(run)}</td><td className="muted text-xs">{timestamp(run.created_at)}</td></tr>)}</tbody></table></div>}
    </section><div className="workflow"><span>01 <strong>Collect PR context</strong></span><span>02 <strong>Generate regression tests</strong></span><span>03 <strong>Execute in isolation</strong></span></div>
  </>;
}
