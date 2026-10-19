import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { api, ApiError, type RunStatus } from "../lib/api";
import { useRun } from "../lib/use-run";
import { run } from "./fixture";

afterEach(() => { cleanup(); vi.useRealTimers(); });
it.each<RunStatus>(["COMPLETED", "FAILED"])("stops polling for %s", async status => {
  vi.useFakeTimers();
  const getter = vi.spyOn(api, "get").mockResolvedValue({ ...run, status });
  const hook = renderHook(() => useRun(run.id));
  await act(async () => {});
  expect(hook.result.current.run?.status).toBe(status);
  await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
  expect(getter).toHaveBeenCalledTimes(1);
});
it("recovers transient errors, polls active jobs, and aborts on unmount", async () => {
  vi.useFakeTimers();
  const getter = vi.spyOn(api, "get").mockRejectedValueOnce(new ApiError("offline")).mockResolvedValue(run);
  const hook = renderHook(() => useRun(run.id));
  await act(async () => {});
  expect(hook.result.current.error).toBe("offline");
  await act(async () => { await vi.advanceTimersByTimeAsync(2_500); });
  expect(hook.result.current.run?.status).toBe("QUEUED");
  expect(hook.result.current.error).toBe("");
  const signal = getter.mock.calls[1][1];
  hook.unmount();
  expect(signal?.aborted).toBe(true);
  await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
  expect(getter).toHaveBeenCalledTimes(2);
});
it("does not poll missing runs indefinitely", async () => {
  vi.useFakeTimers();
  const getter = vi.spyOn(api, "get").mockRejectedValue(new ApiError("Run not found", 404));
  renderHook(() => useRun(run.id));
  await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
  expect(getter).toHaveBeenCalledTimes(1);
});
