"use client";

import { useEffect, useState } from "react";
import { api, ApiError, isTerminal, type Run } from "./api";

export function useRun(id: string) {
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function poll() {
      let again = true;
      try {
        const next = await api.get(id, controller.signal);
        if (controller.signal.aborted) return;
        setRun(next); setError("");
        again = !isTerminal(next.status);
      } catch (error) {
        if (controller.signal.aborted) return;
        setError(error instanceof Error ? error.message : "Could not load the run.");
        again = !(error instanceof ApiError && [404, 422].includes(error.status));
      }
      // Schedule only after a request finishes: no overlapping requests or
      // stale responses, and transient network errors can recover automatically.
      if (again && !controller.signal.aborted) timer = setTimeout(poll, 2_500);
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [id]);
  return { run, error };
}
