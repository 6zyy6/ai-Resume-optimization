import { describe, expect, it, vi } from "vitest";

import { claimEvidenceForText, waitForTask } from "../src/workflows";

describe("shared workflow guards", () => {
  it("covers every non-empty bullet with its confirmed fact", () => {
    expect(claimEvidenceForText("bullet-1", "完成访谈", "fact-1")).toEqual([{
      bullet_id: "bullet-1", start: 0, end: 4, fact_refs: ["fact-1"],
    }]);
    expect(claimEvidenceForText("bullet-2", "", "fact-2")).toEqual([]);
  });

  it("waits for a task terminal state before allowing the next workflow step", async () => {
    const readTask = vi.fn()
      .mockResolvedValueOnce({ status: "queued" })
      .mockResolvedValueOnce({ status: "running" })
      .mockResolvedValueOnce({ status: "succeeded" });
    await expect(waitForTask(readTask, "task-1", { intervalMs: 0 })).resolves.toMatchObject({ status: "succeeded" });
    expect(readTask).toHaveBeenCalledTimes(3);
  });

  it("stops a workflow on a failed or cancelled task", async () => {
    await expect(waitForTask(async () => ({ status: "failed", error_code: "MODEL_UNAVAILABLE" }), "task-1", { intervalMs: 0 }))
      .rejects.toThrow("MODEL_UNAVAILABLE");
    await expect(waitForTask(async () => ({ status: "cancelled" }), "task-1", { intervalMs: 0 }))
      .rejects.toThrow("cancelled");
  });
});
