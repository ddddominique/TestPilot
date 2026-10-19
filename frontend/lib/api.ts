export type RunStatus = "PENDING" | "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED";
export type ExecutionOutcome = "PASSED" | "TEST_FAILED" | "COLLECTION_ERROR" | "SYNTAX_ERROR" |
  "DEPENDENCY_ERROR" | "TIMEOUT" | "INFRASTRUCTURE_ERROR" | "UNSUPPORTED";

// Mirrors RunResponse in testpilot/api/schemas.py. JSONB proposals remain open
// records because the backend intentionally supports legacy metadata shapes.
export interface Run {
  id: string; pr_url: string; repository: string; pr_number: number; head_sha: string | null;
  status: RunStatus; queued_at: string | null; task_id: string | null;
  framework: string | null; execution_outcome: ExecutionOutcome | null;
  started_at: string | null; completed_at: string | null; exit_code: number | null;
  duration_seconds: number | null; generation_summary: string | null;
  framework_selection: string | null; framework_evidence: string[]; warnings: string[];
  passed: number; failed: number; skipped: number; generated_tests: Record<string, unknown>[];
  execution_output: string | null; error_details: string | null; created_at: string; updated_at: string;
}
export interface RunPage { items: Run[]; total: number; limit: number; offset: number }

export class ApiError extends Error {
  constructor(message: string, public status = 0) { super(message); }
}

export function normalizePrUrl(value: string): string {
  const match = /^https?:\/\/(?:www\.)?github\.com\/([A-Za-z0-9][A-Za-z0-9-]{0,38})\/([A-Za-z0-9_.-]{1,104})\/pull\/(\d+)\/?(?:[?#].*)?$/i.exec(value.trim());
  if (!match || value.trim().length > 2048) throw new Error("Enter a GitHub pull request URL, such as https://github.com/owner/repo/pull/123.");
  const [, owner, rawRepo, number] = match;
  const repo = rawRepo.replace(/\.git$/, "");
  if (!repo || repo === "." || repo === ".." || repo.length > 100 || BigInt(number) < 1n || BigInt(number) > 9223372036854775807n)
    throw new Error("Enter a valid repository and positive pull request number.");
  return `https://github.com/${owner}/${repo}/pull/${BigInt(number)}`;
}

export function isTerminal(status: RunStatus): boolean { return status === "COMPLETED" || status === "FAILED"; }
export function resultLabel(run: Pick<Run, "status" | "execution_outcome">): string {
  if (run.status === "FAILED") return "Pipeline failed";
  if (run.execution_outcome === "TEST_FAILED") return "Tests detected failures";
  if (run.execution_outcome === "PASSED") return "Tests passed";
  if (run.execution_outcome) return run.execution_outcome.replaceAll("_", " ").toLowerCase();
  return run.status === "COMPLETED" ? "Pipeline completed" : "Awaiting results";
}

async function request(path: string, method = "GET", body?: unknown, signal?: AbortSignal): Promise<unknown> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (signal?.aborted) controller.abort();
  signal?.addEventListener("abort", abort, { once: true });
  const timeout = setTimeout(abort, 15_000);
  try {
    const response = await fetch(`${(process.env.NEXT_PUBLIC_API_BASE_URL || "/backend").replace(/\/$/, "")}${path}`, {
      method, headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined, cache: "no-store", signal: controller.signal,
    });
    const data: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      const detail = data && typeof data === "object" && "detail" in data ? data.detail : null;
      throw new ApiError(typeof detail === "string" ? detail : `Request failed (${response.status}). Check that FastAPI is running.`, response.status);
    }
    return data;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (signal?.aborted) throw error;
    throw new ApiError(controller.signal.aborted ? "The API request timed out. Check the run before submitting again." : "Cannot reach the API. Check that FastAPI and the frontend proxy are running.");
  } finally { clearTimeout(timeout); signal?.removeEventListener("abort", abort); }
}

function parseRun(value: unknown): Run {
  if (!value || typeof value !== "object" || !("id" in value) || typeof value.id !== "string" ||
      !("status" in value) || !["PENDING", "QUEUED", "RUNNING", "COMPLETED", "FAILED"].includes(String(value.status)))
    throw new ApiError("The API returned an invalid run response.");
  const row = value as Record<string, unknown>;
  const strings = ["id", "pr_url", "repository", "created_at", "updated_at"];
  const nullableStrings = ["head_sha", "queued_at", "task_id", "framework", "execution_outcome", "started_at",
    "completed_at", "generation_summary", "framework_selection", "execution_output", "error_details"];
  const invalid = strings.some(key => typeof row[key] !== "string") ||
    nullableStrings.some(key => row[key] !== null && typeof row[key] !== "string") ||
    ["pr_number", "passed", "failed", "skipped"].some(key => typeof row[key] !== "number" || !Number.isInteger(row[key]) || (row[key] as number) < 0) ||
    ["exit_code", "duration_seconds"].some(key => row[key] !== null && (typeof row[key] !== "number" || !Number.isFinite(row[key]))) ||
    ["warnings", "framework_evidence"].some(key => !Array.isArray(row[key]) || row[key].some((item: unknown) => typeof item !== "string")) ||
    !Array.isArray(row.generated_tests) || row.generated_tests.some(item => !item || typeof item !== "object" || Array.isArray(item));
  if (invalid) throw new ApiError("The API returned an invalid run response.");
  return value as Run;
}
export const api = {
  create: async (pr_url: string) => parseRun(await request("/api/runs", "POST", { pr_url })),
  execute: async (id: string) => parseRun(await request(`/api/runs/${encodeURIComponent(id)}/execute`, "POST")),
  get: async (id: string, signal?: AbortSignal) => parseRun(await request(`/api/runs/${encodeURIComponent(id)}`, "GET", undefined, signal)),
  list: async (signal?: AbortSignal): Promise<RunPage> => {
    const value = await request("/api/runs?limit=10&offset=0", "GET", undefined, signal);
    if (!value || typeof value !== "object" || !("items" in value) || !Array.isArray(value.items) || !("total" in value) || typeof value.total !== "number")
      throw new ApiError("The API returned invalid run history.");
    return { ...(value as RunPage), items: value.items.map(parseRun) };
  },
};
