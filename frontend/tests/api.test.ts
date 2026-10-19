import { afterEach, describe, expect, it, vi } from "vitest";
import { api, isTerminal, normalizePrUrl, resultLabel } from "../lib/api";
import { run } from "./fixture";

afterEach(() => vi.unstubAllGlobals());
describe("PR validation", () => {
  it("normalizes supported URLs", () => expect(normalizePrUrl(" http://www.github.com/a/b/pull/001?diff=split ")).toBe("https://github.com/a/b/pull/1"));
  it.each(["https://example.com/a/b/pull/1", "https://github.com/a/b/issues/1", "https://github.com/a/b/pull/0", "https://github.com/a/../pull/1", "https://github.com/a/b/pull/9223372036854775808"])("rejects %s", url => expect(() => normalizePrUrl(url)).toThrow());
});
it("uses the existing create and execute endpoints", async () => {
  const fetcher = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(run), { status: 201 })));
  vi.stubGlobal("fetch", fetcher);
  expect((await api.create(run.pr_url)).id).toBe(run.id);
  await api.execute(run.id);
  expect(fetcher.mock.calls[0][0]).toBe("/backend/api/runs");
  expect(fetcher.mock.calls[0][1].body).toBe(JSON.stringify({ pr_url: run.pr_url }));
  expect(fetcher.mock.calls[1][0]).toBe(`/backend/api/runs/${run.id}/execute`);
});
it("reports unreachable APIs and HTTP errors", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("network")));
  await expect(api.get(run.id)).rejects.toThrow("Cannot reach the API");
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "Run not found" }), { status: 404 })));
  await expect(api.get(run.id)).rejects.toMatchObject({ status: 404, message: "Run not found" });
});
it("rejects malformed successful responses", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: run.id, status: "RUNNING" }))));
  await expect(api.get(run.id)).rejects.toThrow("invalid run response");
});
it("distinguishes completed failing tests from a failed pipeline", () => {
  expect(resultLabel({ status: "COMPLETED", execution_outcome: "TEST_FAILED" })).toBe("Tests detected failures");
  expect(resultLabel({ status: "FAILED", execution_outcome: "INFRASTRUCTURE_ERROR" })).toBe("Pipeline failed");
  expect(isTerminal("COMPLETED")).toBe(true);
  expect(isTerminal("FAILED")).toBe(true);
  expect(isTerminal("QUEUED")).toBe(false);
});
