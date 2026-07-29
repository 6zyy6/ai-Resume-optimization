import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  PiRuntime,
  RuntimeCall,
  WorkflowInput,
} from "../src/contracts.js";
import { createModelRouter } from "../src/model-router.js";
import { buildApp } from "../src/server/app.js";
import { MemoryRunStore } from "../src/server/memory-run-store.js";

const apps: Array<ReturnType<typeof buildApp>> = [];

function input(): WorkflowInput {
  return {
    workflow_type: "style_check",
    workflow_version: "1",
    trace_id: "trace_1",
    task_id: "task_1",
    locale: "zh-CN",
    target: "web",
    confirmed_facts: [],
    jd_requirements: [],
    current_object: { text: "负责产品整体战略" },
  };
}

function runtime(call?: RuntimeCall): PiRuntime {
  const execute: RuntimeCall = call ?? (async () => ({
    status: "success",
    output: { issues: [], passed: true },
    events: [],
  }));
  return {
    mode: "fixture",
    runStructured: execute,
    runAgent: execute,
  };
}

async function waitForTerminal(
  app: ReturnType<typeof buildApp>,
  runId: string,
  token = "service-token",
) {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    const response = await app.inject({
      method: "GET",
      url: `/internal/v1/runs/${runId}`,
      headers: { authorization: `Bearer ${token}` },
    });
    const body = response.json();
    if (["succeeded", "failed", "cancelled"].includes(body.run.status)) {
      return body.run;
    }
    await new Promise<void>((resolve) => setImmediate(resolve));
  }
  throw new Error("run did not settle");
}

afterEach(async () => {
  await Promise.all(apps.splice(0).map((app) => app.close()));
  vi.unstubAllEnvs();
});

describe("Pi internal API", () => {
  it("keeps liveness public but requires a Bearer service token for runs", async () => {
    const app = buildApp({
      mode: "production",
      runtime: runtime(),
      serviceToken: "service-token",
      modelRouter: createModelRouter({
        routes: {},
      }),
    });
    apps.push(app);

    expect(
      (await app.inject({
        method: "GET",
        url: "/internal/v1/health/live",
      })).statusCode,
    ).toBe(200);
    expect(
      (await app.inject({
        method: "POST",
        url: "/internal/v1/runs",
        payload: input(),
      })).statusCode,
    ).toBe(401);
    expect(
      (await app.inject({
        method: "POST",
        url: "/internal/v1/runs",
        headers: { authorization: "Bearer wrong-token" },
        payload: input(),
      })).statusCode,
    ).toBe(401);
    const created = await app.inject({
        method: "POST",
        url: "/internal/v1/runs",
        headers: { authorization: "Bearer service-token" },
        payload: input(),
      });
    expect(created.statusCode).toBe(202);
    const runId = created.json().ai_run_id;
    expect(
      (await app.inject({
        method: "GET",
        url: `/internal/v1/runs/${runId}`,
      })).statusCode,
    ).toBe(401);
    expect(
      (await app.inject({
        method: "POST",
        url: `/internal/v1/runs/${runId}/cancel`,
        headers: { authorization: "Bearer wrong-token" },
      })).statusCode,
    ).toBe(401);
  });

  it("reports an immutable deployment version without authentication", async () => {
    vi.stubEnv("APP_COMMIT_SHA", "commit-test");
    const app = buildApp({
      mode: "fixture",
      runtime: runtime(),
      serviceToken: "secret",
      modelRouter: createModelRouter({ routes: {} }),
    });
    apps.push(app);
    const response = await app.inject({
      method: "GET",
      url: "/internal/v1/version",
    });
    expect(response.statusCode).toBe(200);
    expect(response.json()).toEqual({ commit_sha: "commit-test", service: "ai" });
  });

  it("creates, retrieves and settles an internal run", async () => {
    const app = buildApp({
      mode: "fixture",
      runtime: runtime(),
      serviceToken: "service-token",
      modelRouter: createModelRouter({ routes: {} }),
    });
    apps.push(app);

    const created = await app.inject({
      method: "POST",
      url: "/internal/v1/runs",
      headers: { authorization: "Bearer service-token" },
      payload: input(),
    });
    const createdBody = created.json();
    const run = await waitForTerminal(app, createdBody.ai_run_id);

    expect(created.statusCode).toBe(202);
    expect(createdBody.request_id).toMatch(/^req_/);
    expect(run.status).toBe("succeeded");
    expect(run.output).toEqual({ issues: [], passed: true });
  });

  it("propagates cancel to the active runtime AbortSignal", async () => {
    let observedAbort = false;
    const app = buildApp({
      mode: "fixture",
      serviceToken: "service-token",
      modelRouter: createModelRouter({ routes: {} }),
      runtime: runtime(async ({ signal }) => {
        await new Promise<void>((resolve) => {
          signal.addEventListener("abort", () => {
            observedAbort = true;
            resolve();
          }, { once: true });
        });
        return {
          status: "success",
          output: { issues: [], passed: true },
          events: [],
        };
      }),
    });
    apps.push(app);

    const created = await app.inject({
      method: "POST",
      url: "/internal/v1/runs",
      headers: { authorization: "Bearer service-token" },
      payload: input(),
    });
    const runId = created.json().ai_run_id;
    const cancelled = await app.inject({
      method: "POST",
      url: `/internal/v1/runs/${runId}/cancel`,
      headers: { authorization: "Bearer service-token" },
    });
    const run = await waitForTerminal(app, runId);

    expect(cancelled.statusCode).toBe(202);
    expect(observedAbort).toBe(true);
    expect(run.status).toBe("cancelled");
  });

  it("retrieves and cancels a run through a different Pi replica", async () => {
    const runStore = new MemoryRunStore();
    let observedAbort = false;
    const first = buildApp({
      mode: "fixture",
      instanceId: "pi-a",
      runStore,
      serviceToken: "service-token",
      modelRouter: createModelRouter({ routes: {} }),
      runtime: runtime(async ({ signal }) => {
        await new Promise<void>((resolve) => {
          signal.addEventListener("abort", () => {
            observedAbort = true;
            resolve();
          }, { once: true });
        });
        return {
          status: "success",
          output: { issues: [], passed: true },
          events: [],
        };
      }),
    });
    const second = buildApp({
      mode: "fixture",
      instanceId: "pi-b",
      runStore,
      serviceToken: "service-token",
      modelRouter: createModelRouter({ routes: {} }),
      runtime: runtime(),
    });
    apps.push(first, second);

    const created = await first.inject({
      method: "POST",
      url: "/internal/v1/runs",
      headers: { authorization: "Bearer service-token" },
      payload: input(),
    });
    const runId = created.json().ai_run_id;
    const visibleFromSecond = await second.inject({
      method: "GET",
      url: `/internal/v1/runs/${runId}`,
      headers: { authorization: "Bearer service-token" },
    });
    const cancelledFromSecond = await second.inject({
      method: "POST",
      url: `/internal/v1/runs/${runId}/cancel`,
      headers: { authorization: "Bearer service-token" },
    });
    const terminal = await waitForTerminal(second, runId);

    expect(visibleFromSecond.statusCode).toBe(200);
    expect(cancelledFromSecond.statusCode).toBe(202);
    expect(observedAbort).toBe(true);
    expect(terminal.status).toBe("cancelled");
  });

  it("cancels twenty cross-replica runs within five seconds", async () => {
    const runStore = new MemoryRunStore();
    let abortCount = 0;
    const first = buildApp({
      mode: "fixture",
      instanceId: "pi-batch-owner",
      runStore,
      serviceToken: "service-token",
      modelRouter: createModelRouter({ routes: {} }),
      runtime: runtime(async ({ signal }) => {
        await new Promise<void>((resolve) => {
          signal.addEventListener("abort", () => {
            abortCount += 1;
            resolve();
          }, { once: true });
        });
        return {
          status: "success",
          output: { issues: [], passed: true },
          events: [],
        };
      }),
    });
    const second = buildApp({
      mode: "fixture",
      instanceId: "pi-batch-canceller",
      runStore,
      serviceToken: "service-token",
      modelRouter: createModelRouter({ routes: {} }),
      runtime: runtime(),
    });
    apps.push(first, second);
    const headers = { authorization: "Bearer service-token" };
    const created = await Promise.all(
      Array.from({ length: 20 }, (_, index) =>
        first.inject({
          method: "POST",
          url: "/internal/v1/runs",
          headers,
          payload: {
            ...input(),
            task_id: `task_${index}`,
            trace_id: `trace_${index}`,
          },
        })
      ),
    );
    const runIds = created.map((response) => response.json().ai_run_id);

    const startedAt = Date.now();
    const cancelled = await Promise.all(
      runIds.map((runId) =>
        second.inject({
          method: "POST",
          url: `/internal/v1/runs/${runId}/cancel`,
          headers,
        })
      ),
    );
    const terminal = await Promise.all(
      runIds.map((runId) => waitForTerminal(second, runId)),
    );

    expect(cancelled.every(({ statusCode }) => statusCode === 202)).toBe(true);
    expect(terminal.every(({ status }) => status === "cancelled")).toBe(true);
    expect(abortCount).toBe(20);
    expect(Date.now() - startedAt).toBeLessThanOrEqual(5_000);
  });

  it("rejects cancellation after a run is already terminal", async () => {
    const app = buildApp({
      mode: "fixture",
      runtime: runtime(),
      serviceToken: "service-token",
      modelRouter: createModelRouter({ routes: {} }),
    });
    apps.push(app);
    const created = await app.inject({
      method: "POST",
      url: "/internal/v1/runs",
      headers: { authorization: "Bearer service-token" },
      payload: input(),
    });
    const runId = created.json().ai_run_id;
    await waitForTerminal(app, runId);

    const cancelled = await app.inject({
      method: "POST",
      url: `/internal/v1/runs/${runId}/cancel`,
      headers: { authorization: "Bearer service-token" },
    });

    expect(cancelled.statusCode).toBe(409);
    expect(cancelled.json().error.code).toBe("run_already_terminal");
  });

  it("rejects request bodies larger than 512 KB", async () => {
    const app = buildApp({
      mode: "fixture",
      runtime: runtime(),
      serviceToken: "service-token",
      modelRouter: createModelRouter({ routes: {} }),
    });
    apps.push(app);

    const response = await app.inject({
      method: "POST",
      url: "/internal/v1/runs",
      headers: {
        authorization: "Bearer service-token",
        "content-type": "application/json",
      },
      payload: JSON.stringify({
        ...input(),
        current_object: { text: "x".repeat(512 * 1024) },
      }),
    });

    expect(response.statusCode).toBe(413);
  });

  it("fails production readiness without token or approved model routes", async () => {
    const missingToken = buildApp({
      mode: "production",
      runtime: runtime(),
      modelRouter: createModelRouter({ routes: {} }),
    });
    const missingRoutes = buildApp({
      mode: "production",
      runtime: runtime(),
      serviceToken: "service-token",
      modelRouter: createModelRouter({ routes: {} }),
    });
    const fixture = buildApp({
      mode: "fixture",
      runtime: runtime(),
      modelRouter: createModelRouter({ routes: {} }),
    });
    apps.push(missingToken, missingRoutes, fixture);

    expect(
      (await missingToken.inject({
        method: "GET",
        url: "/internal/v1/health/ready",
      })).statusCode,
    ).toBe(503);
    expect(
      (await missingRoutes.inject({
        method: "GET",
        url: "/internal/v1/health/ready",
      })).statusCode,
    ).toBe(503);
    expect(
      (await fixture.inject({
        method: "GET",
        url: "/internal/v1/health/ready",
      })).statusCode,
    ).toBe(200);
  });

  it("uses the production runtime model and credential readiness check", async () => {
    vi.stubEnv("OPENAI_API_KEY", "configured-for-check-only");
    const modelRoute = {
      primary: {
        provider: "openai",
        model: "missing-model",
        approved_data_policy: true,
      },
      fallback: {
        provider: "openai",
        model: "missing-model",
        approved_data_policy: true,
      },
      max_tokens: 100,
      thinking: "off" as const,
      timeout_ms: 1_000,
      retry_count: 0,
      max_cost_usd: 1,
    };
    const checkedRuntime = {
      ...runtime(),
      mode: "production" as const,
      isReady: vi.fn(async () => false),
    };
    const app = buildApp({
      mode: "production",
      runtime: checkedRuntime,
      serviceToken: "service-token",
      modelRouter: createModelRouter({
        routes: Object.fromEntries(
          [
            "extract_facts",
            "next_question",
            "write_experience_bullet",
            "parse_jd",
            "match_resume_to_jd",
            "generate_suggestion",
            "fact_check",
            "style_check",
          ].map((workflowType) => [workflowType, modelRoute]),
        ),
      }),
    });
    apps.push(app);

    const response = await app.inject({
      method: "GET",
      url: "/internal/v1/health/ready",
    });

    expect(response.statusCode).toBe(503);
    expect(checkedRuntime.isReady).toHaveBeenCalledOnce();
  });
});
