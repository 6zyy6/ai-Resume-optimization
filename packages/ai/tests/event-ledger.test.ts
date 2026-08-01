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

  it("quantizes USD cost to eighteen decimals and rejects the shared upper bound", () => {
    const ledger = createEventLedger({
      ai_run_id: "run_cost",
      trace_id: "trace_cost",
      task_id: "task_cost",
    });

    ledger.appendPiEvent({
      type: "message_end",
      message: {
        role: "assistant",
        usage: {
          cost: { total: 0.12345678901234567 },
        },
      },
    });

    expect(ledger.usage.cost_usd).toBe(0.12345678901234567);
    expect(() => ledger.appendPiEvent({
      type: "message_end",
      message: {
        role: "assistant",
        usage: { cost: { total: 1_000_000 } },
      },
    })).toThrow(/cost_usd/i);
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
    ledger.append("model_fallback", {
      provider: "FULL JD / Resume John john@example.com",
      fallback_reason: "Resume John john@example.com",
      risk_flags: [
        "safe_flag",
        "FULL JD / Resume John john@example.com",
      ],
    });

    const serialized = JSON.stringify(ledger.events);
    expect(serialized).not.toContain("张三");
    expect(serialized).not.toContain("13800138000");
    expect(serialized).not.toContain("sk-secret");
    expect(serialized).not.toContain("result");
    expect(serialized).not.toContain("FULL JD");
    expect(serialized).not.toContain("FULL_JD");
    expect(serialized).not.toContain("Resume John");
    expect(serialized).not.toContain("Resume_John");
    expect(serialized).not.toContain("john@example.com");
    expect(serialized).not.toContain("john_example.com");
    expect(serialized).toContain("unsupported_numeric");
    expect(serialized).toContain("safe_flag");
  });

  it("never preserves PII-shaped values in any string trace field", () => {
    const ledger = createEventLedger({
      ai_run_id: "run_pii",
      trace_id: "trace_pii",
      task_id: "task_pii",
    });
    const sensitiveValues = [
      "13800138000",
      "11010519491231002X",
      "john.doe",
      "john-doe",
      "john_doe",
      "john@example.com",
      "gpt-13800138000",
      "deepseek-11010519491231002X-john@example.com",
      "faux-john_doe",
      "john-doe@2",
    ];
    const stringFields = [
      "provider",
      "model",
      "response_model",
      "response_id",
      "stop_reason",
      "tool_name",
      "status",
      "schema_path",
      "error_code",
      "fallback_reason",
      "input_hash",
      "prompt_template_version",
      "source_event_type_hash",
    ];

    for (const value of sensitiveValues) {
      ledger.append("model_fallback", {
        ...Object.fromEntries(stringFields.map((field) => [field, value])),
        risk_flags: [value],
      });
    }

    const serialized = JSON.stringify(ledger.events);
    for (const value of sensitiveValues) {
      expect(serialized).not.toContain(value);
    }
    for (const event of ledger.events) {
      expect(event.details?.model).toMatch(/^sha256:[a-f0-9]{16}$/);
      expect(event.details?.response_model).toMatch(/^sha256:[a-f0-9]{16}$/);
      expect(event.details?.response_id).toMatch(/^sha256:[a-f0-9]{16}$/);
      expect(event.details?.input_hash).toMatch(/^sha256:[a-f0-9]{16}$/);
      expect(event.details?.source_event_type_hash).toMatch(
        /^sha256:[a-f0-9]{16}$/,
      );
      expect(event.details?.risk_flags).toEqual([]);
    }

    const firstPass = ledger.events.map((event) => event.details);
    for (const details of firstPass) {
      ledger.append("model_fallback", details);
    }
    expect(ledger.events.slice(firstPass.length).map((event) => event.details))
      .toEqual(firstPass);
  });

  it("retains approved audit identifiers under the field-level policy", () => {
    const ledger = createEventLedger({
      ai_run_id: "run_safe",
      trace_id: "trace_safe",
      task_id: "task_safe",
    });

    const responseIdHash = "sha256:8f2a854afb5c4231";
    const deepseek = ledger.append("model_fallback", {
      provider: "deepseek",
      model: "deepseek-chat",
      response_model: "deepseek-chat-202607",
      response_id: responseIdHash,
      stop_reason: "stop",
      tool_name: "emit_question",
      schema_valid: true,
      status: "ok",
      schema_path: "$.atomic_claims[0].fact_refs",
      error_code: "UNSUPPORTED_CLAIM",
      fallback_reason: "provider_unavailable",
      risk_flags: ["unsupported_numeric", "safe_flag"],
      input_hash: "a".repeat(64),
      prompt_template_version: "jd-parse@2",
      source_event_type_hash: "b".repeat(16),
    });
    const faux = ledger.append("message_end", {
      provider: "faux",
      model: "faux-1",
    });
    const configured = ledger.append("message_end", {
      provider: "deepseek",
      model: "deepseek-v4-flash",
      response_model: "deepseek-v4-pro",
    });

    expect(deepseek.details).toEqual({
      provider: "deepseek",
      model: "deepseek-chat",
      response_model: "deepseek-chat-202607",
      response_id: responseIdHash,
      stop_reason: "stop",
      tool_name: "emit_question",
      schema_valid: true,
      status: "ok",
      schema_path: "$.atomic_claims[0].fact_refs",
      error_code: "UNSUPPORTED_CLAIM",
      fallback_reason: "provider_unavailable",
      risk_flags: ["unsupported_numeric", "safe_flag"],
      input_hash: "a".repeat(64),
      prompt_template_version: "jd-parse@2",
      source_event_type_hash: "b".repeat(16),
    });
    expect(faux.details).toEqual({ provider: "faux", model: "faux-1" });
    expect(configured.details).toEqual({
      provider: "deepseek",
      model: "deepseek-v4-flash",
      response_model: "deepseek-v4-pro",
    });
  });

  it("maps nested Agent assistant deltas to first_token then message_update", () => {
    const ledger = createEventLedger({
      ai_run_id: "run_1",
      trace_id: "trace_1",
      task_id: "task_1",
    });
    const partial = {
      role: "assistant",
      content: [{ type: "text", text: "" }],
      provider: "faux",
      model: "faux-1",
    };

    ledger.appendPiEvent({
      type: "message_update",
      assistantMessageEvent: {
        type: "text_delta",
        contentIndex: 0,
        delta: "第一段",
        partial,
      },
      message: partial,
    });
    ledger.appendPiEvent({
      type: "message_update",
      assistantMessageEvent: {
        type: "text_delta",
        contentIndex: 0,
        delta: "第二段",
        partial,
      },
      message: partial,
    });

    expect(ledger.events.map(({ event_type }) => event_type)).toEqual([
      "first_token",
      "message_update",
    ]);
  });
});
