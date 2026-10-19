import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import Overview from "../app/page";
import { RunDetails } from "../components/run-details";
import { api } from "../lib/api";
import { run } from "./fixture";

const { push } = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
afterEach(cleanup);
it("creates then queues a run and navigates to its details", async () => {
  vi.spyOn(api, "list").mockResolvedValue({ items: [], total: 0, limit: 10, offset: 0 });
  const create = vi.spyOn(api, "create").mockResolvedValue(run);
  const execute = vi.spyOn(api, "execute").mockResolvedValue(run);
  render(<Overview />);
  expect(screen.getByRole("status").textContent).toContain("Loading recent runs");
  fireEvent.change(screen.getByLabelText("GitHub pull request URL"), { target: { value: run.pr_url } });
  fireEvent.click(screen.getByRole("button", { name: "Analyze PR →" }));
  await waitFor(() => expect(push).toHaveBeenCalledWith(`/runs/${run.id}`));
  expect(create).toHaveBeenCalledWith(run.pr_url);
  expect(execute).toHaveBeenCalledWith(run.id);
  push.mockClear();
});
it("preserves the created run link when queuing fails without automatic retry", async () => {
  vi.spyOn(api, "list").mockResolvedValue({ items: [], total: 0, limit: 10, offset: 0 });
  vi.spyOn(api, "create").mockResolvedValue(run);
  const execute = vi.spyOn(api, "execute").mockRejectedValue(new Error("Broker unavailable"));
  render(<Overview />);
  fireEvent.change(screen.getByLabelText("GitHub pull request URL"), { target: { value: run.pr_url } });
  fireEvent.click(screen.getByRole("button", { name: "Analyze PR →" }));
  await screen.findByText("Broker unavailable", { exact: false });
  expect(screen.getByRole("link", { name: "Inspect the created run →" }).getAttribute("href")).toBe(`/runs/${run.id}`);
  expect(execute).toHaveBeenCalledTimes(1);
  expect(push).not.toHaveBeenCalled();
});
it("shows test failure evidence and labels redacted modules explicitly", async () => {
  vi.spyOn(api, "get").mockResolvedValue({ ...run, status: "COMPLETED", execution_outcome: "TEST_FAILED", failed: 1,
    execution_output: "assert 2 == 3", generated_tests: [{ suggested_test_file: "test_a.py", target_source_file: "a.py", framework: "pytest", test_code: "# [REDACTED]", test_code_representation: "redacted_display" }] });
  render(<RunDetails id={run.id} />);
  await screen.findByText("Tests detected failures");
  expect(screen.queryByText("Pipeline failed")).toBeNull();
  expect(screen.getByText(/Redacted display only/)).toBeTruthy();
  expect(screen.getByLabelText("Test runner output").textContent).toContain("assert 2 == 3");
  Object.defineProperty(navigator, "clipboard", { value: { writeText: vi.fn().mockResolvedValue(undefined) }, configurable: true });
  fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
  await waitFor(() => expect(navigator.clipboard.writeText).toHaveBeenCalledWith("# [REDACTED]"));
});
