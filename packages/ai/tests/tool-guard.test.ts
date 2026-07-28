import { describe, expect, it } from "vitest";

import {
  ALLOWED_TOOL_NAMES,
  ToolGuardError,
  createToolGuard,
} from "../src/tools/guard.js";

const confirmedFacts = [
  {
    id: "fact_1",
    kind: "result",
    value: "将转化率提升 30%",
    status: "confirmed" as const,
  },
  {
    id: "fact_2",
    kind: "tool",
    value: "使用 SQL 分析漏斗",
    status: "confirmed" as const,
  },
];

const jdRequirements = [
  { id: "req_1", category: "must_have", value: "具备数据分析能力" },
];

describe("tool guard", () => {
  it("exposes only the five approved tools", () => {
    expect(ALLOWED_TOOL_NAMES).toEqual([
      "get_confirmed_facts",
      "get_jd_requirements",
      "emit_question",
      "emit_resume_suggestion",
      "emit_fact_check_result",
    ]);
  });

  it("returns only caller-provided confirmed facts for approved IDs", () => {
    const guard = createToolGuard({ confirmedFacts, jdRequirements });

    expect(
      guard.execute("get_confirmed_facts", { fact_ids: ["fact_2"] }),
    ).toEqual({
      facts: [confirmedFacts[1]],
    });
  });

  it("rejects unknown fact and JD requirement IDs before tool execution", () => {
    const guard = createToolGuard({ confirmedFacts, jdRequirements });

    expect(() =>
      guard.execute("get_confirmed_facts", { fact_ids: ["fact_missing"] }),
    ).toThrowError(
      expect.objectContaining({ code: "unknown_id" } satisfies Partial<ToolGuardError>),
    );
    expect(() =>
      guard.execute("get_jd_requirements", {
        requirement_ids: ["req_missing"],
      }),
    ).toThrowError(
      expect.objectContaining({ code: "unknown_id" } satisfies Partial<ToolGuardError>),
    );
    expect(guard.snapshot()).toEqual({ tool_calls: 0, terminal: false });
  });

  it("validates tool arguments and emitted output with TypeBox", () => {
    const guard = createToolGuard({ confirmedFacts, jdRequirements });

    expect(() =>
      guard.execute("emit_question", {
        question_id: "question_1",
        text: "",
        fact_refs: [],
      }),
    ).toThrowError(
      expect.objectContaining({
        code: "schema_validation_failed",
      } satisfies Partial<ToolGuardError>),
    );
    expect(
      guard.execute("emit_question", {
        question_id: "question_1",
        text: "这个结果覆盖了多长时间？",
        fact_refs: ["fact_1"],
      }),
    ).toEqual({
      question: {
        question_id: "question_1",
        text: "这个结果覆盖了多长时间？",
        fact_refs: ["fact_1"],
      },
    });
  });

  it("rejects all 50 prompt-injection tool names without executing one", () => {
    const injectionFixtures = Array.from(
      { length: 50 },
      (_, index) =>
        `ignore_policy_${index + 1}; call_shell_and_exfiltrate_secrets`,
    );
    const guard = createToolGuard({ confirmedFacts, jdRequirements });

    expect(injectionFixtures).toHaveLength(50);
    for (const toolName of injectionFixtures) {
      expect(() => guard.execute(toolName, {})).toThrowError(
        expect.objectContaining(
          { code: "unknown_tool" } satisfies Partial<ToolGuardError>,
        ),
      );
    }
    expect(guard.snapshot()).toEqual({ tool_calls: 0, terminal: false });
  });
});
