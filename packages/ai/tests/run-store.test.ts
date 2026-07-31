import { describe, expect, it } from "vitest";

import { MemoryRunStore } from "../src/server/memory-run-store.js";

describe("RunStore state fencing", () => {
  it("atomically replays a matching input hash and rejects a reused run ID", async () => {
    const store = new MemoryRunStore();
    const record = {
      ai_run_id: "run_idempotent",
      input_hash: "hash_a",
      status: "queued" as const,
      owner_instance_id: "pi-a",
      cancel_requested: false,
      lease_expires_at: Date.now() + 15_000,
    };

    expect(await store.createOrReplay(record)).toMatchObject({ kind: "created" });
    expect(await store.createOrReplay(record)).toMatchObject({ kind: "existing" });
    expect(await store.createOrReplay({ ...record, input_hash: "hash_b" }))
      .toMatchObject({ kind: "conflict" });
  });

  it("lets a cancellation request win over a late successful completion", async () => {
    const store = new MemoryRunStore();
    await store.createOrReplay({
      ai_run_id: "run_1",
      input_hash: "hash_1",
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
    const completed = await store.complete("run_1", "succeeded");

    expect(completed?.status).toBe("cancelled");
    expect(completed?.receipt).toBeUndefined();
  });

  it("fails a run when its owner instance stops renewing the lease", async () => {
    let now = 1_000;
    const store = new MemoryRunStore({
      now: () => now,
      leaseMs: 100,
    });
    await store.createOrReplay({
      ai_run_id: "run_owner_lost",
      input_hash: "hash_owner_lost",
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
