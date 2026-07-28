import { describe, expect, it } from "vitest";

import { createEventLedger } from "../src/tracing/event-ledger.js";

describe("event ledger", () => {
  it("maps unknown Pi events to unknown and keeps event_seq continuous", () => {
    const ledger = createEventLedger({
      ai_run_id: "run_1",
      trace_id: "trace_1",
      task_id: "task_1",
    });

    ledger.append("run_queued");
    ledger.appendPiEvent({ type: "message_start" });
    ledger.appendPiEvent({
      type: "future_pi_event",
      prompt: "private resume text",
    });

    expect(ledger.events.map(({ event_seq, event_type }) => ({
      event_seq,
      event_type,
    }))).toEqual([
      { event_seq: 1, event_type: "run_queued" },
      { event_seq: 2, event_type: "message_start" },
      { event_seq: 3, event_type: "unknown" },
    ]);
  });

  it("aggregates every usage and cost field exactly across messages", () => {
    const ledger = createEventLedger({
      ai_run_id: "run_1",
      trace_id: "trace_1",
      task_id: "task_1",
    });

    ledger.appendPiEvent({
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
          input: 10,
          output: 5,
          cacheRead: 2,
          cacheWrite: 4,
          totalTokens: 21,
          cost: {
            input: 0.01,
            output: 0.02,
            cacheRead: 0.003,
            cacheWrite: 0.004,
            total: 0.037,
          },
        },
      },
    });
    ledger.appendPiEvent({
      type: "message_end",
      message: {
        role: "assistant",
        content: [],
        api: "faux",
        provider: "provider_b",
        model: "model_b",
        stopReason: "stop",
        timestamp: 2,
        usage: {
          input: 7,
          output: 3,
          cacheRead: 1,
          cacheWrite: 2,
          totalTokens: 13,
          cost: {
            input: 0.007,
            output: 0.006,
            cacheRead: 0.001,
            cacheWrite: 0.002,
            total: 0.016,
          },
        },
      },
    });

    expect(ledger.usage).toEqual({
      input: 17,
      output: 8,
      cache_read: 3,
      cache_write: 6,
      reasoning: 0,
      total_tokens: 34,
      cost_usd: 0.053,
    });
  });

  it("does not count tool-result usage as model token usage", () => {
    const ledger = createEventLedger({
      ai_run_id: "run_1",
      trace_id: "trace_1",
      task_id: "task_1",
    });

    ledger.appendPiEvent({
      type: "message_end",
      message: {
        role: "toolResult",
        toolCallId: "tool_1",
        toolName: "get_confirmed_facts",
        content: [{ type: "text", text: "private tool result" }],
        details: {},
        isError: false,
        timestamp: 1,
        usage: {
          input: 100,
          output: 100,
          cacheRead: 0,
          cacheWrite: 0,
          totalTokens: 200,
          cost: {
            input: 1,
            output: 1,
            cacheRead: 0,
            cacheWrite: 0,
            total: 2,
          },
        },
      },
    });

    expect(ledger.usage.total_tokens).toBe(0);
    expect(ledger.usage.cost_usd).toBe(0);
  });

  it("persists only privacy-safe trace metadata", () => {
    const ledger = createEventLedger({
      ai_run_id: "run_1",
      trace_id: "trace_1",
      task_id: "task_1",
    });

    ledger.appendPiEvent({
      type: "tool_execution_end",
      toolName: "emit_question",
      result: {
        text: "姓名张三，电话 13800138000",
        api_key: "sk-secret",
      },
      durationMs: 12,
      isError: false,
    });
    ledger.append("fact_validation_failed", {
      schema_path: "$.atomic_claims[0].fact_refs",
      risk_flags: ["unsupported_numeric"],
      error_code: "UNSUPPORTED_CLAIM",
    });

    const serialized = JSON.stringify(ledger.events);
    expect(serialized).not.toContain("张三");
    expect(serialized).not.toContain("13800138000");
    expect(serialized).not.toContain("sk-secret");
    expect(serialized).not.toContain("result");
    expect(serialized).toContain("unsupported_numeric");
  });
});
