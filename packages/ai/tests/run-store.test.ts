import { describe, expect, it } from "vitest";

import type { AiExecutionReceipt } from "../src/contracts.js";
import { MemoryRunStore } from "../src/server/memory-run-store.js";
import { terminalReceipt } from "../src/server/run-store.js";

function runContext(aiRunId: string, inputHash: string) {
  return {
    ai_run_id: aiRunId,
    workflow_type: "parse_jd" as const,
    workflow_version: "2",
    prompt_template_version: "jd-parse@2",
    trace_id: "trace_1",
    task_id: "task_1",
    input_hash: inputHash,
    started_at: "2026-01-01T00:00:00.000Z",
  };
}

function auditedReceipt(
  aiRunId: string,
  status: "succeeded" | "failed" | "cancelled" = "succeeded",
): AiExecutionReceipt {
  const run = {
    ai_run_id: aiRunId, trace_id: "trace_1", task_id: "task_1",
    workflow_type: "parse_jd" as const, workflow_version: "2",
    prompt_template_version: "jd-parse@2", status,
    error_code: status === "failed" ? "runtime_failed" : null,
    provider: "faux", requested_model: "faux-1", response_model: "faux-1.1",
    started_at: "2026-01-01T00:00:00.000Z", first_token_at: "2026-01-01T00:00:00.050Z",
    finished_at: "2026-01-01T00:00:00.100Z",
    usage: { input: 17, output: 0, cache_read: 0, cache_write: 0, reasoning: 0, total_tokens: 17, cost_usd: 0.01 },
    events: [{ ai_run_id: aiRunId, trace_id: "trace_1", task_id: "task_1", event_seq: 1, event_type: "message_end", occurred_at: "2026-01-01T00:00:00.090Z" }],
    turn_count: 2, tool_call_count: 1, retry_count: 1, fallback_count: 0,
    schema_valid: true, facts_valid: true, input_hash: "hash_fenced",
    exportable: false, risk_flags: [],
  };
  return status === "succeeded" ? { run, result: { requirements: [] } } : { run };
}

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
      context: runContext("run_idempotent", "hash_a"),
    };

    expect(await store.createOrGet(record)).toMatchObject({ kind: "created" });
    expect(await store.createOrGet(record)).toMatchObject({ kind: "existing" });
    expect(await store.createOrGet({ ...record, input_hash: "hash_b" }))
      .toMatchObject({ kind: "conflict" });
  });

  it("creates a complete owner-lost receipt when a lease expires", async () => {
    let now = 1_000;
    const store = new MemoryRunStore({ now: () => now, leaseMs: 100 });
    await store.createOrGet({
      ai_run_id: "run_lost_receipt",
      input_hash: "hash_lost",
      status: "queued",
      owner_instance_id: "pi-owner",
      cancel_requested: false,
      lease_expires_at: 1_100,
      context: {
        ai_run_id: "run_lost_receipt",
        workflow_type: "parse_jd",
        workflow_version: "2",
        prompt_template_version: "jd-parse@2",
        trace_id: "trace_lost",
        task_id: "task_lost",
        input_hash: "hash_lost",
        started_at: "1970-01-01T00:00:01.000Z",
      },
    });
    now = 1_101;

    expect(await store.get("run_lost_receipt")).toMatchObject({
      status: "failed",
      receipt: {
        run: {
          status: "failed",
          error_code: "owner_instance_lost",
          provider: null,
          requested_model: null,
          response_model: null,
          first_token_at: null,
        },
      },
    });
  });

  it("lets a cancellation request win over a late successful completion", async () => {
    const store = new MemoryRunStore();
    await store.createOrGet({
      ai_run_id: "run_1",
      input_hash: "hash_1",
      status: "queued",
      owner_instance_id: "pi-a",
      cancel_requested: false,
      lease_expires_at: Date.now() + 15_000,
      context: runContext("run_1", "hash_1"),
    });
    await store.markRunning("run_1");

    expect(await store.requestCancel("run_1")).toEqual({
      outcome: "accepted",
      owner_instance_id: "pi-a",
    });
    const completed = await store.complete(
      "run_1",
      "pi-a",
      "succeeded",
      terminalReceipt(runContext("run_1", "hash_1"), "succeeded", null),
    );

    expect(completed?.status).toBe("cancelled");
    expect(completed?.receipt?.run.status).toBe("cancelled");
    expect(completed?.receipt?.run.events.at(-1)?.event_type).toBe("run_cancelled");
  });

  it("fails a run when its owner instance stops renewing the lease", async () => {
    let now = 1_000;
    const store = new MemoryRunStore({
      now: () => now,
      leaseMs: 100,
    });
    await store.createOrGet({
      ai_run_id: "run_owner_lost",
      input_hash: "hash_owner_lost",
      status: "queued",
      owner_instance_id: "pi-owner",
      cancel_requested: false,
      lease_expires_at: 1_100,
      context: runContext("run_owner_lost", "hash_owner_lost"),
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

  it("fences an expired owner from completing successfully", async () => {
    let now = 1_000;
    const store = new MemoryRunStore({ now: () => now, leaseMs: 100 });
    const context = runContext("run_fenced", "hash_fenced");
    await store.createOrGet({
      ai_run_id: "run_fenced", input_hash: "hash_fenced", status: "queued",
      owner_instance_id: "pi-a", cancel_requested: false, lease_expires_at: 1_100, context,
    });
    now = 1_101;
    const completed = await store.complete(
      "run_fenced", "pi-a", "succeeded", auditedReceipt("run_fenced"),
    );

    expect(completed).toMatchObject({
      status: "failed", error_code: "owner_instance_lost",
      receipt: { run: {
        status: "failed", error_code: "owner_instance_lost", provider: "faux",
        requested_model: "faux-1", response_model: "faux-1.1",
        usage: { total_tokens: 17 }, turn_count: 2, tool_call_count: 1,
        retry_count: 1, schema_valid: true, facts_valid: true,
      } },
    });
    expect(completed?.receipt).not.toHaveProperty("result");
    expect(completed?.receipt?.run.events.map(({ event_seq }) => event_seq)).toEqual([1, 2]);
    expect(completed?.receipt?.run.events.at(-1)?.event_type).toBe("run_failed");
  });

  it.each(["succeeded", "failed", "cancelled"] as const)(
    "preserves an unchanged %s receipt byte-for-byte",
    async (status) => {
      const store = new MemoryRunStore({ now: () => 1_000 });
      const context = runContext(`run_${status}`, "hash_fenced");
      await store.createOrGet({
        ai_run_id: `run_${status}`, input_hash: "hash_fenced", status: "queued",
        owner_instance_id: "pi-a", cancel_requested: false, lease_expires_at: 1_100, context,
      });
      const receipt = auditedReceipt(`run_${status}`, status);
      const completed = await store.complete(`run_${status}`, "pi-a", status, receipt);
      expect(completed?.receipt).toEqual(receipt);
    },
  );
});
