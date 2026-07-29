import { describe, expect, it } from "vitest";

import { MemoryRunStore } from "../src/server/memory-run-store.js";

describe("RunStore state fencing", () => {
  it("lets a cancellation request win over a late successful completion", async () => {
    const store = new MemoryRunStore();
    await store.create({
      ai_run_id: "run_1",
      status: "queued",
      owner_instance_id: "pi-a",
      cancel_requested: false,
      lease_expires_at: Date.now() + 15_000,
    });
    await store.markRunning("run_1");

    expect(await store.requestCancel("run_1")).toEqual({
      outcome: "accepted",
      owner_instance_id: "pi-a",
    });
    const completed = await store.complete("run_1", "succeeded", {
      ai_run_id: "run_1",
      trace_id: "trace_1",
      task_id: "task_1",
      workflow_type: "style_check",
      workflow_version: "1",
      status: "succeeded",
      output: { passed: true, issues: [] },
      usage: {
        input: 0,
        output: 0,
        cache_read: 0,
        cache_write: 0,
        reasoning: 0,
        total_tokens: 0,
        cost_usd: 0,
      },
      events: [],
      turn_count: 0,
      tool_call_count: 0,
      fallback_count: 0,
      exportable: true,
      risk_flags: [],
    });

    expect(completed?.status).toBe("cancelled");
    expect(completed?.result).toBeUndefined();
  });

  it("fails a run when its owner instance stops renewing the lease", async () => {
    let now = 1_000;
    const store = new MemoryRunStore({
      now: () => now,
      leaseMs: 100,
    });
    await store.create({
      ai_run_id: "run_owner_lost",
      status: "queued",
      owner_instance_id: "pi-owner",
      cancel_requested: false,
      lease_expires_at: 1_100,
    });
    await store.markRunning("run_owner_lost");
    now = 1_101;

    const expired = await store.get("run_owner_lost");

    expect(expired?.status).toBe("failed");
    expect(expired?.error_code).toBe("owner_instance_lost");
    expect(
      await store.heartbeat("run_owner_lost", "pi-owner"),
    ).toBe(false);
  });
});
