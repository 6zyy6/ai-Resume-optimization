import { randomUUID } from "node:crypto";

import { describe, expect, it } from "vitest";

import { RedisRunStore } from "../src/server/redis-run-store.js";

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
        } as const;
        expect((await first.createOrReplay(record)).kind).toBe("created");
        expect((await second.createOrReplay(record)).kind).toBe("existing");
        expect((await second.createOrReplay({ ...record, input_hash: "other" })).kind)
          .toBe("conflict");
        await first.markRunning(aiRunId);
        expect((await second.get(aiRunId))?.status).toBe("running");
        expect((await second.requestCancel(aiRunId)).outcome).toBe("accepted");
        await first.complete(aiRunId, "cancelled");
        expect((await second.get(aiRunId))?.status).toBe("cancelled");
      }
      await expect.poll(() => received.size).toBe(100);

      const lostRunId = `run_test_${randomUUID()}`;
      await first.createOrReplay({
        ai_run_id: lostRunId,
        input_hash: "lost_hash",
        status: "queued",
        owner_instance_id: "pi-lost-owner",
        cancel_requested: false,
        lease_expires_at: Date.now() + 25,
      });
      await first.markRunning(lostRunId);
      await new Promise((resolve) => setTimeout(resolve, 75));
      const lost = await second.get(lostRunId);
      expect(lost?.status).toBe("failed");
      expect(lost?.error_code).toBe("owner_instance_lost");
    } finally {
      await unsubscribe();
      await first.close();
      await second.close();
    }
  }, 30_000);
});
