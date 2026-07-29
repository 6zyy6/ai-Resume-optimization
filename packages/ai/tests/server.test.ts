import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  PiRuntime,
  RuntimeCall,
  WorkflowInput,
} from "../src/contracts.js";
import { createModelRouter } from "../src/model-router.js";
import { buildApp } from "../src/server/app.js";

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
