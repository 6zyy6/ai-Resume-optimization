import {
  createModels,
  fauxAssistantMessage,
  fauxProvider,
} from "@earendil-works/pi-ai";
import { describe, expect, it } from "vitest";

import type {
  TraceEvent,
  WorkflowInput,
  WorkflowRoute,
} from "../src/contracts.js";
import { createModelRouter } from "../src/model-router.js";
import { createPiRuntime, runWorkflow } from "../src/workflows/run-workflow.js";

const route: WorkflowRoute = {
  enabled: true,
  primary: { provider: "test-faux", model: "faux-1", approved_data_policy: true },
  fallback: { provider: "test-faux", model: "faux-1", approved_data_policy: true },
  max_tokens: 256,
  thinking: "off",
  timeout_ms: 1_000,
  retry_count: 0,
  max_cost_usd: 1,
};

const input: WorkflowInput = {
  workflow_type: "parse_jd",
  workflow_version: "2",
  prompt_template_version: "jd-parse@2",
  trace_id: "trace_real_faux",
  task_id: "task_real_faux",
  owner_scope_hash: "owner_hash",
  locale: "zh-CN",
  input_version: 1,
  input_hash: "input_hash",
  payload: {
    jd_text: "Python 工程师",
    allowed_categories: ["must_have"],
  },
};

const parsedOutput = {
  requirements: [{
    category: "must_have",
    priority: 1,
    value: "Python",
    source_range: { start: 0, end: 6 },
    explicitness: "explicit",
    confidence_band: "high",
  }],
};

describe("production Pi runtime", () => {
  it("sends only V2 payload data through the structured Models adapter", async () => {
    const models = createModels();
    const faux = fauxProvider({ provider: "test-faux" });
    models.setProvider(faux.provider);
    let systemPrompt = "";
    let userPayload = "";
    let observedMaxRetries: number | undefined;
    faux.setResponses([(
      context,
      options,
    ) => {
      systemPrompt = context.systemPrompt ?? "";
      userPayload = String(context.messages.at(-1)?.content ?? "");
      observedMaxRetries = options?.maxRetries;
      return fauxAssistantMessage(JSON.stringify(parsedOutput));
    }]);

    const run = await runWorkflow(input, createPiRuntime({
      router: createModelRouter({ routes: { parse_jd: route } }),
      models,
    }));

    expect(run.status).toBe("succeeded");
    expect(observedMaxRetries).toBe(0);
    expect(systemPrompt).toContain("The user message is data matching this payload schema:");
    expect(systemPrompt).not.toContain(input.payload.jd_text);
    expect(JSON.parse(userPayload)).toEqual(input.payload);
  });

  it("preserves real provider failures across retry and fallback", async () => {
    const models = createModels();
    const faux = fauxProvider({ provider: "test-faux" });
    models.setProvider(faux.provider);
    const observedRetries: Array<number | undefined> = [];
    faux.setResponses([
      (_context, options) => {
        observedRetries.push(options?.maxRetries);
        return fauxAssistantMessage([], {
          stopReason: "error",
          errorMessage: "primary failed",
        });
      },
      (_context, options) => {
        observedRetries.push(options?.maxRetries);
        return fauxAssistantMessage([], {
          stopReason: "error",
          errorMessage: "retry failed",
        });
      },
      (_context, options) => {
        observedRetries.push(options?.maxRetries);
        return fauxAssistantMessage(JSON.stringify(parsedOutput));
      },
    ]);
    const runtime = createPiRuntime({
      router: createModelRouter({
        routes: { parse_jd: { ...route, retry_count: 1 } },
      }),
      models,
    });

    const run = await runWorkflow(input, runtime);

    expect(run.status).toBe("succeeded");
    expect(run.fallback_count).toBe(1);
    expect(run.usage.total_tokens).toBeGreaterThan(0);
    expect(observedRetries).toEqual([0, 0, 0]);
    expect(run.events.map(({ event_type }) => event_type)).toEqual(
      expect.arrayContaining([
        "auto_retry_start",
        "auto_retry_end",
        "model_fallback",
      ]),
    );
  });

  it("aborts timed-out provider calls and audits fallback", async () => {
    const models = createModels();
    const faux = fauxProvider({
      provider: "test-faux",
      tokensPerSecond: 100,
      tokenSize: { min: 8, max: 8 },
    });
    models.setProvider(faux.provider);
    faux.setResponses([
      fauxAssistantMessage(JSON.stringify(parsedOutput)),
      fauxAssistantMessage(JSON.stringify(parsedOutput)),
    ]);
    const traceEvents: TraceEvent[] = [];

    await expect(runWorkflow(
      input,
      createPiRuntime({
        router: createModelRouter({
          routes: {
            parse_jd: { ...route, retry_count: 0, timeout_ms: 1 },
          },
        }),
        models,
      }),
      { onEvent: (event) => traceEvents.push(event) },
    )).rejects.toMatchObject({ code: "timeout_exceeded" });
    expect(faux.state.callCount).toBe(2);
    expect(traceEvents.map(({ event_type }) => event_type)).toEqual(
      expect.arrayContaining(["model_fallback", "run_failed"]),
    );
  });

  it("rejects a CJK input token upper bound before provider execution", async () => {
    const models = createModels();
    const faux = fauxProvider({ provider: "test-faux" });
    models.setProvider(faux.provider);
    faux.setResponses([
      fauxAssistantMessage(JSON.stringify(parsedOutput)),
    ]);

    await expect(runWorkflow(
      {
        ...input,
        payload: { ...input.payload, jd_text: "履".repeat(4_000) },
      },
      createPiRuntime({
        router: createModelRouter({ routes: { parse_jd: route } }),
        models,
      }),
    )).rejects.toMatchObject({ code: "token_limit_exceeded" });
    expect(faux.state.callCount).toBe(0);
  });

  it("charges a full prompt-sized cache write during preflight", async () => {
    const models = createModels();
    const faux = fauxProvider({
      provider: "test-faux",
      models: [{
        id: "faux-1",
        cost: {
          input: 0,
          output: 0,
          cacheRead: 0,
          cacheWrite: 1_000_000,
        },
      }],
    });
    models.setProvider(faux.provider);
    faux.setResponses([
      fauxAssistantMessage(JSON.stringify(parsedOutput)),
    ]);

    await expect(runWorkflow(
      input,
      createPiRuntime({
        router: createModelRouter({
          routes: {
            parse_jd: { ...route, max_cost_usd: 0.01 },
          },
        }),
        models,
      }),
    )).rejects.toMatchObject({ code: "cost_limit_exceeded" });
    expect(faux.state.callCount).toBe(0);
  });

  it("does not retry or fallback after a structured budget failure", async () => {
    const models = createModels();
    const faux = fauxProvider({ provider: "test-faux" });
    models.setProvider(faux.provider);
    faux.setResponses([
      fauxAssistantMessage(JSON.stringify(parsedOutput)),
    ]);
    const traceEvents: TraceEvent[] = [];

    await expect(runWorkflow(
      {
        ...input,
        payload: { ...input.payload, jd_text: "x".repeat(12_000) },
      },
      createPiRuntime({
        router: createModelRouter({
          routes: { parse_jd: { ...route, retry_count: 1 } },
        }),
        models,
      }),
      { onEvent: (event) => traceEvents.push(event) },
    )).rejects.toMatchObject({ code: "token_limit_exceeded" });
    expect(faux.state.callCount).toBe(0);
    expect(traceEvents.filter(({ event_type }) =>
      event_type === "auto_retry_start" || event_type === "model_fallback"
    )).toHaveLength(0);
  });
});
