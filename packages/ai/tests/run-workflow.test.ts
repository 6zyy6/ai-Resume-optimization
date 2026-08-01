import { describe, expect, it } from "vitest";

import type {
  PiRuntime,
  RuntimeCall,
  RuntimeResult,
  TraceEvent,
  WorkflowInput,
} from "../src/contracts.js";
import { resolvePrompt } from "../src/workflows/prompt-registry.js";
import { validateRuntimeOutput } from "../src/workflows/postflight.js";
import { createFixtureRuntime, runWorkflow } from "../src/workflows/run-workflow.js";

const common = {
  workflow_version: "2" as const,
  trace_id: "trace_1",
  task_id: "task_1",
  owner_scope_hash: "a".repeat(64),
  locale: "zh-CN" as const,
  input_version: 1,
  input_hash: "b".repeat(64),
};

function input(workflowType: WorkflowInput["workflow_type"]): WorkflowInput {
  switch (workflowType) {
    case "analyze_intake_answer":
      return { ...common, workflow_type: workflowType, prompt_template_version: "intake-answer@2", payload: { session_id_hash: "c".repeat(64), answer_id: "answer_1", question_id: "question_1", question_reason: "项目经历", answer_text: "我使用 Python。", answer_state: "answered", confirmed_facts: [{ id: "fact_1", kind: "skill", value: "Python" }], covered_slots: [], missing_slots: ["impact"], asked_question_ids: [] } };
    case "compose_resume_draft":
      return { ...common, workflow_type: workflowType, prompt_template_version: "resume-draft@2", payload: { resume_title: "简历", experience_groups: [{ title: "实习", fact_refs: ["fact_1"] }], confirmed_facts: [{ id: "fact_1", kind: "skill", value: "Python", source_hashes: ["d".repeat(64)] }], allowed_section_types: ["experience"] } };
    case "parse_jd":
      return { ...common, workflow_type: workflowType, prompt_template_version: "jd-parse@2", payload: { jd_text: "Python 工程师", allowed_categories: ["must_have"] } };
    case "match_resume_to_jd":
      return { ...common, workflow_type: workflowType, prompt_template_version: "resume-match@2", payload: { resume_version_id: "resume_1", resume_snapshot_hash: "e".repeat(64), confirmed_facts: [{ id: "fact_1", kind: "skill", value: "Python" }], confirmed_requirements: [{ id: "requirement_1", category: "must_have", value: "Python" }] } };
    case "generate_suggestions_batch":
      return { ...common, workflow_type: workflowType, prompt_template_version: "suggestions-batch@2", payload: { matches: [{ requirement_ref: "requirement_1", category: "transferable", fact_refs: ["fact_1"], target_path: "sections[0].bullets[0]", original_hash: "f".repeat(64), original_text: "开发服务" }], confirmed_facts: [{ id: "fact_1", kind: "skill", value: "Python" }], confirmed_requirements: [{ id: "requirement_1", category: "must_have", value: "Python" }] } };
  }
}

function output(workflowType: WorkflowInput["workflow_type"]): unknown {
  switch (workflowType) {
    case "analyze_intake_answer": return { fact_candidates: [{ kind: "skill", value: "Python", source_answer_id: "answer_1", source_range: { start: 2, end: 8 }, risk_flags: [] }], missing_slots: [], question_candidate: { reason: "补充成果", slot: "impact", text: "结果如何？", related_fact_refs: ["fact_1"] } };
    case "compose_resume_draft": return { sections: [{ type: "experience", title: "实习", bullets: [{ text: "使用 Python 开发服务", atomic_claims: [{ text: "使用 Python", fact_refs: ["fact_1"], claim_order: 0 }], risk_flags: [] }] }] };
    case "parse_jd": return { requirements: [{ category: "must_have", priority: 1, value: "Python", source_range: { start: 0, end: 6 }, explicitness: "explicit", confidence_band: "high" }] };
    case "match_resume_to_jd": return { matches: [{ requirement_ref: "requirement_1", category: "direct", fact_refs: ["fact_1"], resume_target_paths: ["sections[0]"], reason_code: "fact_match" }] };
    case "generate_suggestions_batch": return { suggestions: [{ target_path: "sections[0].bullets[0]", original_hash: "f".repeat(64), suggested_text: "使用 Python 开发服务", atomic_claims: [{ text: "Python", fact_refs: ["fact_1"], claim_order: 0 }], requirement_ref: "requirement_1", reason: "补充技能", risk_flags: [], proposed_status: "pending" }] };
  }
}

type FixtureCall = (
  call: Parameters<RuntimeCall>[0],
) => Promise<Record<string, unknown>>;

function makeRuntime(call: FixtureCall): PiRuntime {
  const normalized: RuntimeCall = async (runtimeInput) => {
    const result = await call(runtimeInput);
    return (
      "status" in result
        ? result
        : { ...result, status: "success" }
    ) as RuntimeResult;
  };
  return {
    mode: "fixture",
    runStructured: normalized,
    runAgent: normalized,
  };
}

function usageEvent(
  totalTokens: number,
  costUsd = 0,
): Record<string, unknown> {
  return {
    type: "message_end",
    message: {
      role: "assistant",
      content: [],
      provider: "faux",
      model: "faux-1",
      stopReason: "error",
      usage: {
        input: totalTokens,
        output: 0,
        cacheRead: 0,
        cacheWrite: 0,
        totalTokens,
        cost: {
          input: costUsd,
          output: 0,
          cacheRead: 0,
          cacheWrite: 0,
          total: costUsd,
        },
      },
    },
  };
}

describe("runWorkflow", () => {
  it("asks intake analysis for complete positive atomic-clause evidence", () => {
    const prompt = resolvePrompt(input("analyze_intake_answer")).text;

    expect(prompt).toContain("complete positive atomic clause");
    expect(prompt).toContain("value and source_range");
    expect(prompt).not.toContain("source_hash must");
  });

  it.each([
    "analyze_intake_answer",
    "compose_resume_draft",
    "parse_jd",
    "match_resume_to_jd",
    "generate_suggestions_batch",
  ] as const)("runs %s through the structured runtime", async (workflowType) => {
    let structuredCalls = 0;
    const runtime: PiRuntime = {
      mode: "fixture",
      runStructured: async () => {
        structuredCalls += 1;
        return { status: "success", output: output(workflowType), events: [] };
      },
      runAgent: async () => { throw new Error("agent runtime must not be selected"); },
      getRetryCount: () => 0,
    };

    const run = await runWorkflow(input(workflowType), runtime);

    expect(run.run.status).toBe("succeeded");
    expect(structuredCalls).toBe(1);
  });

  it("allows one schema correction with typed feedback and no fallback", async () => {
    const calls: Array<Parameters<RuntimeCall>[0]> = [];
    const runtime = makeRuntime(async (call) => {
      calls.push(call);
      return {
        output: calls.length === 1
          ? { requirements: "not-an-array" }
          : output("parse_jd"),
        events: [],
      };
    });

    const run = await runWorkflow(input("parse_jd"), runtime);

    expect(calls.map(({ phase }) => phase)).toEqual(["initial", "correction"]);
    expect(calls[1]?.schema_feedback).toEqual([
      { path: "$.requirements", type: "schema" },
    ]);
    expect(run.run.fallback_count).toBe(0);
    expect(run.run.events.some(({ event_type }) => event_type === "model_fallback"))
      .toBe(false);
  });

  it("fails after the single schema correction is still invalid", async () => {
    const runtime = makeRuntime(async () => ({
      output: { requirements: "still-not-an-array" },
      events: [],
    }));

    await expect(runWorkflow(input("parse_jd"), runtime)).resolves
      .toMatchObject({ run: { error_code: "output_schema_invalid", status: "failed" } });
  });

  it("returns a complete failed receipt instead of discarding workflow evidence", async () => {
    const run = await runWorkflow(
      input("parse_jd"),
      makeRuntime(async () => ({
        status: "failure",
        failure_kind: "route",
        error_code: "route_missing",
        events: [usageEvent(17)],
      })),
      { aiRunId: "run_failed_receipt", now: () => 1_000 },
    );

    expect(Object.keys(run.run).sort()).toEqual([
      "ai_run_id", "error_code", "events", "exportable", "facts_valid",
      "fallback_count", "finished_at", "first_token_at", "input_hash",
      "prompt_template_version", "provider", "requested_model", "response_model",
      "retry_count", "risk_flags", "schema_valid", "started_at", "status",
      "task_id", "tool_call_count", "trace_id", "turn_count", "usage",
      "workflow_type", "workflow_version",
    ]);
    expect(run).not.toHaveProperty("result");
    expect(run.run).toMatchObject({
      ai_run_id: "run_failed_receipt",
      workflow_type: "parse_jd",
      workflow_version: "2",
      prompt_template_version: "jd-parse@2",
      trace_id: "trace_1",
      task_id: "task_1",
      status: "failed",
      error_code: "model_route_unavailable",
      provider: "faux",
      requested_model: "faux-1",
      response_model: null,
      started_at: "1970-01-01T00:00:01.000Z",
      first_token_at: null,
      finished_at: "1970-01-01T00:00:01.000Z",
      usage: { input: 17, output: 0, cache_read: 0, cache_write: 0, reasoning: 0, total_tokens: 17, cost_usd: 0 },
      turn_count: 0,
      tool_call_count: 0,
      retry_count: 0,
      fallback_count: 0,
      schema_valid: false,
      facts_valid: false,
      input_hash: "b".repeat(64),
      exportable: false,
      risk_flags: [],
    });
    expect(run.run.events.at(-1)).toMatchObject({ event_type: "run_failed" });
  });

  it("returns a failed receipt when the prompt version is unavailable", async () => {
    const unavailable = {
      ...input("parse_jd"),
      prompt_template_version: "missing@2",
    } as WorkflowInput;

    await expect(runWorkflow(unavailable, createFixtureRuntime({}))).resolves
      .toMatchObject({
        run: {
          status: "failed",
          error_code: "prompt_version_unavailable",
          events: expect.arrayContaining([
            expect.objectContaining({ event_type: "run_failed" }),
          ]),
        },
      });
  });

  it("enforces the 30 second deadline without waiting in real time", async () => {
    let now = 0;
    const runtime = makeRuntime(async () => ({
      output: output("parse_jd"),
      events: [{ type: "message_start" }, { type: "message_end" }],
    }));

    await expect(runWorkflow(input("parse_jd"), runtime, {
      now: () => {
        now += 15_001;
        return now;
      },
    })).resolves.toMatchObject({ run: { error_code: "timeout_exceeded", status: "failed" } });
  });

  it("preserves JSON failure usage and sends invalid-json correction feedback", async () => {
    const calls: Array<Parameters<RuntimeCall>[0]> = [];
    const runtime = makeRuntime(async (call) => {
      calls.push(call);
      return calls.length === 1
        ? {
            status: "failure",
            failure_kind: "json",
            error_code: "invalid_json",
            events: [usageEvent(17)],
          }
        : { output: output("parse_jd"), events: [] };
    });

    const run = await runWorkflow(input("parse_jd"), runtime);

    expect(run.run.usage.total_tokens).toBe(17);
    expect(calls.map(({ phase }) => phase)).toEqual(["initial", "correction"]);
    expect(calls[1]?.schema_feedback).toEqual([
      { path: "$", type: "invalid_json" },
    ]);
  });

  it.each([
    {
      name: "turn",
      events: Array.from({ length: 4 }, () => ({ type: "turn_start" })),
      code: "turn_limit_exceeded",
    },
    {
      name: "tool",
      events: Array.from({ length: 6 }, () => ({
        type: "tool_execution_start",
        toolName: "get_confirmed_facts",
      })),
      code: "tool_limit_exceeded",
    },
    {
      name: "token",
      events: [usageEvent(12_000)],
      code: "token_limit_exceeded",
    },
  ])(
    "does not enter schema correction after the $name budget is exhausted",
    async ({ events, code }) => {
      const calls: Array<Parameters<RuntimeCall>[0]> = [];
      const traceEvents: TraceEvent[] = [];
      const runtime = makeRuntime(async (call) => {
        calls.push(call);
        return {
          output: { requirements: "invalid" },
          events,
        };
      });

      await expect(runWorkflow(input("parse_jd"), runtime, {
        onEvent: (event) => traceEvents.push(event),
      })).resolves.toMatchObject({ run: { error_code: code, status: "failed" } });
      expect(calls.map(({ phase }) => phase)).toEqual(["initial"]);
      expect(traceEvents.at(-1)).toMatchObject({
        event_type: "run_failed",
        details: { error_code: code },
      });
    },
  );

  it("applies the runtime cost gate before schema correction", async () => {
    const calls: Array<Parameters<RuntimeCall>[0]> = [];
    const runtime = makeRuntime(async (call) => {
      calls.push(call);
      return {
        output: { requirements: "invalid" },
        events: [usageEvent(10, 0.06)],
        max_cost_usd: 0.05,
      };
    });

    await expect(runWorkflow(input("parse_jd"), runtime)).resolves
      .toMatchObject({ run: { error_code: "cost_limit_exceeded", status: "failed" } });
    expect(calls.map(({ phase }) => phase)).toEqual(["initial"]);
  });

  it("preserves provider failure usage through an audited retry", async () => {
    const calls: Array<Parameters<RuntimeCall>[0]> = [];
    const runtime = {
      ...makeRuntime(async (call) => {
        calls.push(call);
        return calls.length === 1
          ? {
              status: "failure",
              failure_kind: "provider",
              error_code: "provider_429",
              events: [usageEvent(23)],
            }
          : { output: output("parse_jd"), events: [] };
      }),
      getRetryCount: () => 1,
    };

    const run = await runWorkflow(input("parse_jd"), runtime);

    expect(calls.map(({ phase }) => phase)).toEqual(["initial", "retry"]);
    expect(run.run.usage.total_tokens).toBe(23);
    expect(run.run.fallback_count).toBe(0);
    expect(run.run.events.map(({ event_type }) => event_type)).toEqual(
      expect.arrayContaining(["auto_retry_start", "auto_retry_end"]),
    );
  });

  it("uses fallback only after the audited provider retry is exhausted", async () => {
    const calls: Array<Parameters<RuntimeCall>[0]> = [];
    const runtime = {
      ...makeRuntime(async (call) => {
        calls.push(call);
        if (calls.length < 3) {
          return {
            status: "failure",
            failure_kind: "provider",
            error_code: "provider_unavailable",
            events: [],
          };
        }
        return { output: output("parse_jd"), events: [] };
      }),
      getRetryCount: () => 1,
    };

    const run = await runWorkflow(input("parse_jd"), runtime);

    expect(calls.map(({ phase }) => phase)).toEqual([
      "initial",
      "retry",
      "fallback",
    ]);
    expect(run.run.fallback_count).toBe(1);
    expect(run.run.events.find(({ event_type }) => event_type === "model_fallback")
      ?.details?.fallback_reason).toBe("provider_unavailable");
  });

  it("maps unknown runtime events and preserves exact usage", async () => {
    const runtime = makeRuntime(async () => ({
      output: output("parse_jd"),
      events: [
        { type: "future_event", payload: "private" },
        {
          ...usageEvent(23, 0.03),
          message: {
            ...(usageEvent(23, 0.03).message as object),
            usage: {
              input: 11,
              output: 7,
              cacheRead: 2,
              cacheWrite: 3,
              reasoning: 5,
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

    const run = await runWorkflow(input("parse_jd"), runtime);

    expect(run.run.events.some(({ event_type }) => event_type === "unknown"))
      .toBe(true);
    expect(run.run.usage).toEqual({
      input: 11,
      output: 7,
      cache_read: 2,
      cache_write: 3,
      reasoning: 5,
      total_tokens: 23,
      cost_usd: 0.03,
    });
    expect(run.run.events.map(({ event_seq }) => event_seq)).toEqual(
      Array.from({ length: run.run.events.length }, (_, index) => index + 1),
    );
  });

  it("propagates runtime cancellation without appending later events", async () => {
    const controller = new AbortController();
    const runtime = makeRuntime(async () => ({
      output: output("parse_jd"),
      events: [
        { type: "message_start" },
        { type: "first_token" },
        { type: "message_end" },
      ],
    }));

    const run = await runWorkflow(input("parse_jd"), runtime, {
      signal: controller.signal,
      onEvent: (event) => {
        if (event.event_type === "message_start") controller.abort();
      },
    });

    expect(run.run.status).toBe("cancelled");
    expect(run.run.events.some(({ event_type }) => event_type === "first_token"))
      .toBe(false);
    expect(run.run.events.at(-1)?.event_type).toBe("run_cancelled");
  });

  it("validates workflow-specific evidence references and ranges", () => {
    const analyze = output("analyze_intake_answer") as any;
    analyze.fact_candidates[0].source_answer_id = "wrong_answer";
    expect(validateRuntimeOutput(input("analyze_intake_answer"), analyze)[0]).toMatchObject({ type: "unknown_reference" });

    const compose = output("compose_resume_draft") as any;
    compose.sections[0].bullets[0].atomic_claims[0].fact_refs = ["unknown_fact"];
    expect(validateRuntimeOutput(input("compose_resume_draft"), compose)[0]).toMatchObject({ type: "unknown_reference" });

    const parsed = output("parse_jd") as any;
    parsed.requirements[0].source_range.end = 99;
    expect(validateRuntimeOutput(input("parse_jd"), parsed)[0]).toMatchObject({ type: "range_invalid" });
  });

  it("validates intake answer ranges against Unicode code point length", () => {
    const analyzeInput = input("analyze_intake_answer") as any;
    analyzeInput.payload.answer_text = "😀中";
    const valid = output("analyze_intake_answer") as any;
    valid.fact_candidates[0].source_range = { start: 1, end: 2 };
    const outOfBounds = structuredClone(valid);
    outOfBounds.fact_candidates[0].source_range.end = 3;

    expect(validateRuntimeOutput(analyzeInput, valid)).toEqual([]);
    expect(validateRuntimeOutput(analyzeInput, outOfBounds)[0]).toEqual({
      path: "$.fact_candidates[0].source_range",
      type: "range_invalid",
    });
  });

  it("validates JD ranges against Unicode code point length", () => {
    const parseInput = input("parse_jd") as any;
    parseInput.payload.jd_text = "😀中";
    const valid = output("parse_jd") as any;
    valid.requirements[0].source_range = { start: 0, end: 2 };
    const outOfBounds = structuredClone(valid);
    outOfBounds.requirements[0].source_range.end = 3;

    expect(validateRuntimeOutput(parseInput, valid)).toEqual([]);
    expect(validateRuntimeOutput(parseInput, outOfBounds)[0]).toEqual({
      path: "$.requirements[0].source_range",
      type: "range_invalid",
    });
  });

  it("rejects draft section types outside the caller allowlist", () => {
    const draft = output("compose_resume_draft") as any;
    draft.sections[0].type = "education";

    expect(validateRuntimeOutput(input("compose_resume_draft"), draft)[0])
      .toEqual({ path: "$.sections[0].type", type: "not_allowed" });
  });

  it("rejects JD requirement categories outside the caller allowlist", () => {
    const parsed = output("parse_jd") as any;
    parsed.requirements[0].category = "nice_to_have";

    expect(validateRuntimeOutput(input("parse_jd"), parsed)[0]).toEqual({
      path: "$.requirements[0].category",
      type: "not_allowed",
    });
  });

  it("requires complete unique matches and sourced suggestions", () => {
    const match = output("match_resume_to_jd") as any;
    match.matches = [];
    expect(validateRuntimeOutput(input("match_resume_to_jd"), match)[0]).toMatchObject({ type: "requirement_coverage_invalid" });

    const suggestion = output("generate_suggestions_batch") as any;
    suggestion.suggestions[0].original_hash = "0".repeat(64);
    expect(validateRuntimeOutput(input("generate_suggestions_batch"), suggestion)[0]).toMatchObject({ type: "unknown_reference" });
  });

  it("rejects suggestion sources and outputs that share an unknown requirement ref", () => {
    const suggestionInput = input("generate_suggestions_batch") as any;
    suggestionInput.payload.matches[0].requirement_ref = "unknown_requirement";
    const suggestion = output("generate_suggestions_batch") as any;
    suggestion.suggestions[0].requirement_ref = "unknown_requirement";

    expect(validateRuntimeOutput(suggestionInput, suggestion)).toEqual([
      {
        path: "$.payload.matches[0].requirement_ref",
        type: "unknown_reference",
      },
      { path: "$.suggestions[0]", type: "unknown_reference" },
    ]);
  });

  it("rejects unknown fact refs in suggestion match inputs", () => {
    const suggestionInput = input("generate_suggestions_batch") as any;
    suggestionInput.payload.matches[0].fact_refs = ["unknown_fact"];

    expect(
      validateRuntimeOutput(
        suggestionInput,
        output("generate_suggestions_batch"),
      )[0],
    ).toEqual({
      path: "$.payload.matches[0].fact_refs[0]",
      type: "unknown_reference",
    });
  });

  it("does not combine independently mismatched suggestion source fields", () => {
    const suggestionInput = input("generate_suggestions_batch") as any;
    suggestionInput.payload.matches[0].target_path = "sections:0";
    suggestionInput.payload.matches[0].original_hash = "f".repeat(64);
    const suggestion = output("generate_suggestions_batch") as any;
    suggestion.suggestions[0].target_path = "sections";
    suggestion.suggestions[0].original_hash = "0".repeat(64);

    expect(validateRuntimeOutput(suggestionInput, suggestion)[0]).toEqual({
      path: "$.suggestions[0]",
      type: "unknown_reference",
    });
  });

  it("keeps fixture runs deterministic", async () => {
    const runtime = createFixtureRuntime({ parse_jd: output("parse_jd") });
    await expect(runWorkflow(input("parse_jd"), runtime)).resolves.toMatchObject({ result: output("parse_jd") });
  });
});
