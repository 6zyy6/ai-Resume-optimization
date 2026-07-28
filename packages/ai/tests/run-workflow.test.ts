import { describe, expect, it } from "vitest";

import type {
  PiRuntime,
  RuntimeCall,
  WorkflowInput,
  WorkflowType,
} from "../src/contracts.js";
import {
  WorkflowError,
  createFixtureRuntime,
  runWorkflow,
} from "../src/workflows/run-workflow.js";

const workflowOutputs: Record<WorkflowType, unknown> = {
  extract_facts: {
    facts: [
      {
        id: "candidate_1",
        kind: "action",
        value: "优化转化漏斗",
        source_refs: ["current_object"],
      },
    ],
  },
  next_question: {
    question: {
      question_id: "question_1",
      text: "这个结果覆盖了多长时间？",
      fact_refs: ["fact_1"],
    },
  },
  write_experience_bullet: {
    suggestion_text: "优化转化漏斗，将转化率提升 30%",
    atomic_claims: [
      {
        text: "将转化率提升 30%",
        fact_refs: ["fact_1"],
        status: "supported",
      },
    ],
    jd_requirement_refs: ["req_1"],
    reason: "突出可验证结果",
    risk_flags: [],
    requires_user_confirmation: false,
  },
  parse_jd: {
    requirements: [
      {
        id: "req_candidate_1",
        category: "must_have",
        value: "具备数据分析能力",
      },
    ],
  },
  match_resume_to_jd: {
    matches: [
      {
        category: "direct",
        fact_refs: ["fact_1"],
        requirement_refs: ["req_1"],
      },
    ],
  },
  generate_suggestion: {
    suggestion_text: "优化转化漏斗，将转化率提升 30%",
    atomic_claims: [
      {
        text: "将转化率提升 30%",
        fact_refs: ["fact_1"],
        status: "supported",
      },
    ],
    jd_requirement_refs: ["req_1"],
    reason: "突出可验证结果",
    risk_flags: [],
    requires_user_confirmation: false,
  },
  fact_check: {
    claims: [
      {
        text: "将转化率提升 30%",
        fact_refs: ["fact_1"],
        status: "supported",
      },
    ],
    exportable: true,
    risk_flags: [],
  },
  style_check: {
    issues: [],
    passed: true,
  },
};

function makeInput(workflow_type: WorkflowType): WorkflowInput {
  return {
    workflow_type,
    workflow_version: "1",
    trace_id: "trace_1",
    task_id: "task_1",
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
    jd_requirements: [
      {
        id: "req_1",
        category: "must_have",
        value: "具备数据分析能力",
      },
    ],
    current_object: { text: "caller supplied object" },
  };
}

function makeRuntime(call: RuntimeCall): PiRuntime {
  return {
    mode: "fixture",
    runStructured: call,
    runAgent: call,
  };
}

describe("runWorkflow", () => {
  it.each(Object.keys(workflowOutputs) as WorkflowType[])(
    "supports %s and uses pi-agent-core only for next_question",
    async (workflowType) => {
      let structuredCalls = 0;
      let agentCalls = 0;
      const runtime: PiRuntime = {
        mode: "fixture",
        runStructured: async () => {
          structuredCalls += 1;
          return { output: workflowOutputs[workflowType], events: [] };
        },
        runAgent: async () => {
          agentCalls += 1;
          return { output: workflowOutputs[workflowType], events: [] };
        },
      };

      const run = await runWorkflow(makeInput(workflowType), runtime);

      expect(run.status).toBe("succeeded");
      expect(run.output).toEqual(workflowOutputs[workflowType]);
      expect({ structuredCalls, agentCalls }).toEqual(
        workflowType === "next_question"
          ? { structuredCalls: 0, agentCalls: 1 }
          : { structuredCalls: 1, agentCalls: 0 },
      );
    },
  );

  it.each([
    {
      name: "turns",
      events: Array.from({ length: 5 }, () => ({ type: "turn_start" })),
      code: "turn_limit_exceeded",
    },
    {
      name: "tools",
      events: Array.from({ length: 7 }, () => ({
        type: "tool_execution_start",
        toolName: "get_confirmed_facts",
        args: { fact_ids: ["fact_1"] },
      })),
      code: "tool_limit_exceeded",
    },
    {
      name: "tokens",
      events: [
        {
          type: "message_end",
          message: {
            role: "assistant",
            content: [],
            api: "faux",
            provider: "provider_a",
            model: "model_a",
            stopReason: "stop",
            timestamp: 1,
            usage: {
              input: 6001,
              output: 6000,
              cacheRead: 0,
              cacheWrite: 0,
              totalTokens: 12001,
              cost: {
                input: 0,
                output: 0,
                cacheRead: 0,
                cacheWrite: 0,
                total: 0,
              },
            },
          },
        },
      ],
      code: "token_limit_exceeded",
    },
  ])("enforces the $name limit", async ({ events, code }) => {
    const runtime = makeRuntime(async () => ({
      output: workflowOutputs.style_check,
      events,
    }));

    await expect(
      runWorkflow(makeInput("style_check"), runtime),
    ).rejects.toMatchObject({
      code: code as WorkflowError["code"],
    } satisfies Partial<WorkflowError>);
  });

  it("enforces the 30 second deadline without waiting in real time", async () => {
    let now = 0;
    const runtime = makeRuntime(async () => ({
      output: workflowOutputs.style_check,
      events: [{ type: "message_start" }, { type: "message_end" }],
    }));

    await expect(
      runWorkflow(makeInput("style_check"), runtime, {
        now: () => {
          now += 15_001;
          return now;
        },
      }),
    ).rejects.toMatchObject({
      code: "timeout_exceeded",
    } satisfies Partial<WorkflowError>);
  });

  it("allows exactly one schema-correction fallback", async () => {
    let calls = 0;
    const runtime = makeRuntime(async () => {
      calls += 1;
      return {
        output:
          calls === 1
            ? { issues: "not-an-array" }
            : workflowOutputs.style_check,
        events: [],
      };
    });

    const run = await runWorkflow(makeInput("style_check"), runtime);

    expect(calls).toBe(2);
    expect(run.fallback_count).toBe(1);
    expect(run.events.some(({ event_type }) => event_type === "model_fallback"))
      .toBe(true);
  });

  it("maps unknown runtime events and preserves exact usage", async () => {
    const runtime = makeRuntime(async () => ({
      output: workflowOutputs.style_check,
      events: [
        { type: "future_event", payload: "private" },
        {
          type: "message_end",
          message: {
            role: "assistant",
            content: [],
            api: "faux",
            provider: "provider_a",
            model: "model_a",
            stopReason: "stop",
            timestamp: 1,
            usage: {
              input: 11,
              output: 7,
              cacheRead: 2,
              cacheWrite: 3,
              totalTokens: 23,
              cost: {
                input: 0.011,
                output: 0.014,
                cacheRead: 0.002,
                cacheWrite: 0.003,
                total: 0.03,
              },
            },
          },
        },
      ],
    }));

    const run = await runWorkflow(makeInput("style_check"), runtime);

    expect(run.events.some(({ event_type }) => event_type === "unknown")).toBe(
      true,
    );
    expect(run.usage).toEqual({
      input: 11,
      output: 7,
      cache_read: 2,
      cache_write: 3,
      reasoning: 0,
      total_tokens: 23,
      cost_usd: 0.03,
    });
    expect(run.events.map(({ event_seq }) => event_seq)).toEqual(
      Array.from({ length: run.events.length }, (_, index) => index + 1),
    );
  });

  it("propagates cancellation and does not append subsequent events", async () => {
    const controller = new AbortController();
    const runtime = makeRuntime(async () => ({
      output: workflowOutputs.style_check,
      events: [
        { type: "message_start" },
        { type: "first_token" },
        { type: "message_end" },
      ],
    }));

    const run = await runWorkflow(makeInput("style_check"), runtime, {
      signal: controller.signal,
      onEvent: (event) => {
        if (event.event_type === "message_start") {
          controller.abort();
        }
      },
    });

    expect(run.status).toBe("cancelled");
    expect(run.events.some(({ event_type }) => event_type === "first_token"))
      .toBe(false);
    expect(run.events.at(-1)?.event_type).toBe("run_cancelled");
  });

  it("blocks a severe unsupported claim from export", async () => {
    const runtime = makeRuntime(async () => ({
      output: {
        ...workflowOutputs.generate_suggestion as object,
        suggestion_text: "将转化率提升 80%",
        atomic_claims: [
          {
            text: "将转化率提升 80%",
            fact_refs: [],
            status: "supported",
          },
        ],
      },
      events: [],
    }));

    const run = await runWorkflow(makeInput("generate_suggestion"), runtime);

    expect(run.exportable).toBe(false);
    expect(run.risk_flags).toContain("unsupported_numeric");
    expect(
      run.events.some(
        ({ event_type }) => event_type === "fact_validation_failed",
      ),
    ).toBe(true);
  });

  it("does not trust a fact_check model that marks an unsupported claim exportable", async () => {
    const runtime = makeRuntime(async () => ({
      output: {
        claims: [
          {
            text: "将转化率提升 80%",
            fact_refs: [],
            status: "supported",
          },
        ],
        exportable: true,
        risk_flags: [],
      },
      events: [],
    }));

    const run = await runWorkflow(makeInput("fact_check"), runtime);

    expect(run.exportable).toBe(false);
    expect(run.output).toEqual({
      claims: [
        {
          text: "将转化率提升 80%",
          fact_refs: ["fact_1"],
          status: "unsupported",
        },
      ],
      exportable: false,
      risk_flags: ["unsupported_numeric"],
    });
  });

  it("fixture runtime is deterministic and never performs network I/O", async () => {
    const runtime = createFixtureRuntime(workflowOutputs);

    const first = await runWorkflow(makeInput("parse_jd"), runtime);
    const second = await runWorkflow(makeInput("parse_jd"), runtime);

    expect(first.output).toEqual(second.output);
    expect(runtime.mode).toBe("fixture");
  });
});
