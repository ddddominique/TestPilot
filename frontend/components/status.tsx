import type { RunStatus } from "../lib/api";

export function Status({ status }: { status: RunStatus }) {
  return <span className={`badge status-${status.toLowerCase()}`}><span aria-hidden="true">●</span> {status.toLowerCase()}</span>;
}
export function timestamp(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "—";
}
