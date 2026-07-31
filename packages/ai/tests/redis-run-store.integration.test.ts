import { randomUUID } from "node:crypto";

import { describe, expect, it } from "vitest";

import { RedisRunStore } from "../src/server/redis-run-store.js";
import { terminalReceipt } from "../src/server/run-store.js";

function context(aiRunId: string, inputHash: string) {
  return {
    ai_run_id: aiRunId,
    workflow_type: "parse_jd" as const,
    workflow_version: "2",
    prompt_template_version: "jd-parse@2",
    trace_id: "trace_redis",
    task_id: "task_redis",
    input_hash: inputHash,
    started_at: new Date().toISOString(),
  };
}

const redisUrl = process.env.TEST_REDIS_URL;

describe.runIf(Boolean(redisUrl))("RedisRunStore integration", () => {
  it("shares one hundred runs across replicas and expires a lost owner", async () => {
    const first = await RedisRunStore.connect({
      url: redisUrl!,
      ttlSeconds: 60,
      leaseMs: 50,
    });
    const second = await RedisRunStore.connect({
      url: redisUrl!,
      ttlSeconds: 60,
      leaseMs: 50,
    });
    const received = new Set<string>();
    const unsubscribe = await first.subscribe("pi-redis-owner", (aiRunId) => {
      received.add(aiRunId);
    });
    try {
      for (let index = 0; index < 100; index += 1) {
        const aiRunId = `run_test_${randomUUID()}`;
        const record = {
          ai_run_id: aiRunId,
          input_hash: `hash_${index}`,
          status: "queued",
          owner_instance_id: "pi-redis-owner",
          cancel_requested: false,
          lease_expires_at: Date.now() + 50,
          context: context(aiRunId, `hash_${index}`),
        } as const;
        expect((await first.createOrGet(record)).kind).toBe("created");
        expect((await second.createOrGet(record)).kind).toBe("existing");
        expect((await second.createOrGet({ ...record, input_hash: "other" })).kind)
          .toBe("conflict");
        await first.markRunning(aiRunId);
        expect((await second.get(aiRunId))?.status).toBe("running");
        expect((await second.requestCancel(aiRunId)).outcome).toBe("accepted");
        await first.complete(
          aiRunId,
          "cancelled",
          terminalReceipt(record.context, "cancelled", null),
        );
        expect((await second.get(aiRunId))?.status).toBe("cancelled");
      }
      await expect.poll(() => received.size).toBe(100);

      const lostRunId = `run_test_${randomUUID()}`;
      await first.createOrGet({
        ai_run_id: lostRunId,
        input_hash: "lost_hash",
        status: "queued",
        owner_instance_id: "pi-lost-owner",
        cancel_requested: false,
        lease_expires_at: Date.now() + 25,
        context: context(lostRunId, "lost_hash"),
      });
      await first.markRunning(lostRunId);
      await new Promise((resolve) => setTimeout(resolve, 75));
      const lost = await second.get(lostRunId);
      expect(lost?.status).toBe("failed");
      expect(lost?.error_code).toBe("owner_instance_lost");
      expect(lost?.receipt).not.toHaveProperty("result");
      expect(Object.keys(lost?.receipt?.run ?? {}).sort()).toEqual([
        "ai_run_id", "error_code", "events", "exportable", "facts_valid",
        "fallback_count", "finished_at", "first_token_at", "input_hash",
        "prompt_template_version", "provider", "requested_model", "response_model",
        "retry_count", "risk_flags", "schema_valid", "started_at", "status",
        "task_id", "tool_call_count", "trace_id", "turn_count", "usage",
        "workflow_type", "workflow_version",
      ]);
      expect(lost?.receipt?.run).toMatchObject({
        ai_run_id: lostRunId,
        status: "failed",
        error_code: "owner_instance_lost",
      });
      expect(Array.isArray(lost?.receipt?.run.risk_flags)).toBe(true);
      expect(lost?.receipt?.run.risk_flags).toEqual([]);
      expect(Date.parse(lost!.receipt!.run.finished_at)).toBeGreaterThan(
        Date.parse(lost!.receipt!.run.started_at),
      );
      expect(lost?.receipt?.run.events.at(-1)).toMatchObject({
        event_type: "run_failed",
        occurred_at: lost?.receipt?.run.finished_at,
      });
    } finally {
      await unsubscribe();
      await first.close();
      await second.close();
    }
  }, 30_000);
});
