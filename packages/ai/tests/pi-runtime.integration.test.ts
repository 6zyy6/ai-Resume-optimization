import {
  createModels,
  fauxAssistantMessage,
  fauxProvider,
  fauxToolCall,
} from "@earendil-works/pi-ai";
import { openaiProvider } from "@earendil-works/pi-ai/providers/openai";
import { describe, expect, it, vi } from "vitest";

import type {
  TraceEvent,
  WorkflowInput,
  WorkflowRoute,
} from "../src/contracts.js";
import { createModelRouter } from "../src/model-router.js";
import {
  createPiRuntime,
  runWorkflow,
} from "../src/workflows/run-workflow.js";

const route: WorkflowRoute = {
  primary: {
    provider: "test-faux",
    model: "faux-1",
    approved_data_policy: true,
  },
  fallback: {
    provider: "test-faux",
    model: "faux-1",
    approved_data_policy: true,
  },
  max_tokens: 256,
  thinking: "off",
  timeout_ms: 1_000,
  retry_count: 1,
  max_cost_usd: 1,
};

function input(workflow_type: WorkflowInput["workflow_type"]): WorkflowInput {
  return {
    workflow_type,
    workflow_version: "1",
    trace_id: "trace_real_faux",
    task_id: "task_real_faux",
    locale: "zh-CN",
    target: "web",
    confirmed_facts: [
      {
        id: "fact_1",
        kind: "result",
        value: "优化转化漏斗，将转化率提升 30%",
        status: "confirmed",
      },
    ],
    jd_requirements: [],
    current_object: { text: "负责产品策略" },
  };
}

describe("production Pi runtime with faux provider", () => {
  it("streams through the real Models adapter and disables hidden Pi retries", async () => {
    const models = createModels();
    const faux = fauxProvider({ provider: "test-faux" });
    models.setProvider(faux.provider);
    let observedMaxRetries: number | undefined;
    faux.setResponses([
      (_context, options) => {
        observedMaxRetries = options?.maxRetries;
        return fauxAssistantMessage(
          JSON.stringify({ issues: [], passed: true }),
        );
      },
    ]);
    const router = createModelRouter({
      routes: { style_check: route },
    });
    const runtime = createPiRuntime({ router, models });

    const run = await runWorkflow(input("style_check"), runtime);

    expect(run.status).toBe("succeeded");
    expect(observedMaxRetries).toBe(0);
    expect(run.usage.total_tokens).toBeGreaterThan(0);
    expect(run.events.some(({ event_type }) => event_type === "first_token"))
      .toBe(true);
  });

  it("runs real Agent hooks for blocked ids, unknown tools, and terminal output", async () => {
    const models = createModels();
    const faux = fauxProvider({ provider: "test-faux" });
    models.setProvider(faux.provider);
    faux.setResponses([
      fauxAssistantMessage(
        [
          fauxToolCall("get_confirmed_facts", {
            fact_ids: ["missing_fact"],
          }),
          fauxToolCall("shell", { command: "echo forbidden" }),
          fauxToolCall("emit_question", {
            question_id: "question_1",
            text: "这个结果覆盖了多长时间？",
            fact_refs: ["fact_1"],
          }),
        ],
        { stopReason: "toolUse" },
      ),
    ]);
    const router = createModelRouter({
      routes: { next_question: route },
    });

    const run = await runWorkflow(
      input("next_question"),
      createPiRuntime({ router, models }),
    );

    expect(run.status).toBe("succeeded");
    expect(run.output).toEqual({
      question: {
        question_id: "question_1",
        text: "这个结果覆盖了多长时间？",
        fact_refs: ["fact_1"],
      },
    });
    expect(run.tool_call_count).toBe(3);
    expect(
      run.events.filter(
        ({ event_type, details }) =>
          event_type === "tool_execution_end" &&
          details?.status === "error",
      ),
    ).toHaveLength(2);
    expect(run.events.some(({ event_type }) => event_type === "first_token"))
      .toBe(true);
  });

  it("preserves real provider failures across outer retry and fallback", async () => {
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
        return fauxAssistantMessage(
          JSON.stringify({ issues: [], passed: true }),
        );
      },
    ]);
    const router = createModelRouter({
      routes: { style_check: route },
    });

    const run = await runWorkflow(
      input("style_check"),
      createPiRuntime({ router, models }),
    );

    expect(faux.state.callCount).toBe(3);
    expect(observedRetries).toEqual([0, 0, 0]);
    expect(run.fallback_count).toBe(1);
    expect(run.usage.total_tokens).toBeGreaterThan(0);
    expect(run.events.map(({ event_type }) => event_type)).toEqual(
      expect.arrayContaining([
        "auto_retry_start",
        "auto_retry_end",
        "model_fallback",
      ]),
    );
  });

  it("aborts timed-out faux provider calls and audits the fallback", async () => {
    const models = createModels();
    const faux = fauxProvider({
      provider: "test-faux",
      tokensPerSecond: 100,
      tokenSize: { min: 8, max: 8 },
    });
    models.setProvider(faux.provider);
    faux.setResponses([
      fauxAssistantMessage(JSON.stringify({ issues: [], passed: true })),
      fauxAssistantMessage(JSON.stringify({ issues: [], passed: true })),
    ]);
    const timeoutRoute = {
      ...route,
      retry_count: 0,
      timeout_ms: 1,
    };
    const router = createModelRouter({
      routes: { style_check: timeoutRoute },
    });

    await expect(
      runWorkflow(
        input("style_check"),
        createPiRuntime({ router, models }),
      ),
    ).rejects.toMatchObject({ code: "timeout_exceeded" });
    expect(faux.state.callCount).toBe(2);
  });

  it("rejects worst-case provider cost before making a faux call", async () => {
    const models = createModels();
    const faux = fauxProvider({
      provider: "test-faux",
      models: [
        {
          id: "faux-1",
          cost: {
            input: 1_000_000,
            output: 1_000_000,
            cacheRead: 1_000_000,
            cacheWrite: 1_000_000,
          },
        },
      ],
    });
    models.setProvider(faux.provider);
    faux.setResponses([
      fauxAssistantMessage(JSON.stringify({ issues: [], passed: true })),
    ]);
    const expensiveRoute = {
      ...route,
      retry_count: 0,
      max_cost_usd: 0.01,
    };
    const router = createModelRouter({
      routes: { style_check: expensiveRoute },
    });

    await expect(
      runWorkflow(
        input("style_check"),
        createPiRuntime({ router, models }),
      ),
    ).rejects.toMatchObject({ code: "cost_limit_exceeded" });
    expect(faux.state.callCount).toBe(0);
  });

  it("rejects a CJK input token upper bound before making a faux call", async () => {
    const models = createModels();
    const faux = fauxProvider({ provider: "test-faux" });
    models.setProvider(faux.provider);
    faux.setResponses([
      fauxAssistantMessage(JSON.stringify({ issues: [], passed: true })),
    ]);
    const router = createModelRouter({
      routes: { style_check: route },
    });

    await expect(
      runWorkflow(
        {
          ...input("style_check"),
          current_object: { text: "履".repeat(4_000) },
        },
        createPiRuntime({ router, models }),
      ),
    ).rejects.toMatchObject({ code: "token_limit_exceeded" });
    expect(faux.state.callCount).toBe(0);
  });

  it("charges a full prompt-sized cache write in worst-case preflight", async () => {
    const models = createModels();
    const faux = fauxProvider({
      provider: "test-faux",
      models: [
        {
          id: "faux-1",
          cost: {
            input: 0,
            output: 0,
            cacheRead: 0,
            cacheWrite: 1_000_000,
          },
        },
      ],
    });
    models.setProvider(faux.provider);
    faux.setResponses([
      fauxAssistantMessage(JSON.stringify({ issues: [], passed: true })),
    ]);
    const router = createModelRouter({
      routes: {
        style_check: {
          ...route,
          retry_count: 0,
          max_cost_usd: 0.01,
        },
      },
    });

    await expect(
      runWorkflow(
        input("style_check"),
        createPiRuntime({ router, models }),
      ),
    ).rejects.toMatchObject({ code: "cost_limit_exceeded" });
    expect(faux.state.callCount).toBe(0);
  });

  it("returns an Agent budget failure without provider retry or fallback", async () => {
    const models = createModels();
    const faux = fauxProvider({ provider: "test-faux" });
    models.setProvider(faux.provider);
    faux.setResponses([
      fauxAssistantMessage(
        JSON.stringify({
          question: {
            question_id: "question_1",
            text: "请补充时间范围",
            fact_refs: ["fact_1"],
          },
        }),
      ),
    ]);
    const router = createModelRouter({
      routes: { next_question: route },
    });
    const runtime = createPiRuntime({ router, models });
    let caught: unknown;
    const traceEvents: TraceEvent[] = [];

    try {
      await runWorkflow(
        {
          ...input("next_question"),
          current_object: { text: "x".repeat(50_000) },
        },
        runtime,
        { onEvent: (event) => traceEvents.push(event) },
      );
    } catch (error) {
      caught = error;
    }

    expect(caught).toMatchObject({ code: "token_limit_exceeded" });
    expect(faux.state.callCount).toBe(0);
    expect(
      traceEvents.filter(({ event_type }) =>
        event_type === "auto_retry_start" ||
        event_type === "model_fallback"
      ),
    ).toHaveLength(0);
  });

  it("checks a real registered model and credential without making a request", async () => {
    vi.stubEnv("OPENAI_API_KEY", "configured-for-readiness-only");
    try {
      const models = createModels();
      models.setProvider(openaiProvider());
      const model = models.getModels("openai")[0]!;
      const openAiRoute = {
        ...route,
        primary: {
          provider: "openai",
          model: model.id,
          approved_data_policy: true,
        },
        fallback: {
          provider: "openai",
          model: model.id,
          approved_data_policy: true,
        },
      };
      const routes = Object.fromEntries(
        [
          "extract_facts",
          "next_question",
          "write_experience_bullet",
          "parse_jd",
          "match_resume_to_jd",
          "generate_suggestion",
          "fact_check",
          "style_check",
        ].map((workflowType) => [workflowType, openAiRoute]),
      );
      const runtime = createPiRuntime({
        router: createModelRouter({ routes }),
        models,
      });

      await expect(runtime.isReady?.()).resolves.toBe(true);
    } finally {
      vi.unstubAllEnvs();
    }
  });
});
