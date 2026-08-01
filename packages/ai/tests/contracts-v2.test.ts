import { describe, expect, it } from "vitest";
import { Value } from "typebox/value";

import {
  MODEL_WORKFLOW_TYPES,
  WORKFLOW_OUTPUT_SCHEMAS,
  WorkflowInputSchema,
} from "../src/contracts.js";

const common = {
  workflow_version: "2",
  trace_id: "trace_1",
  task_id: "task_1",
  owner_scope_hash: "a".repeat(64),
  locale: "zh-CN",
  input_version: 1,
  input_hash: "b".repeat(64),
};

function makeAnalyzeIntakeInput() {
  return {
    ...common,
    workflow_type: "analyze_intake_answer",
    prompt_template_version: "intake-answer@2",
    payload: {
      session_id_hash: "c".repeat(64),
      answer_id: "answer_1",
      question_id: "question_1",
      question_reason: "了解项目经历",
      answer_text: "我负责了 Python 服务。",
      answer_state: "answered",
      confirmed_facts: [{ id: "fact_1", kind: "skill", value: "Python" }],
      covered_slots: ["skills"],
      missing_slots: ["impact"],
      asked_question_ids: ["question_1"],
    },
  };
}

function makeComposeDraftInput() {
  return {
    ...common,
    workflow_type: "compose_resume_draft",
    prompt_template_version: "resume-draft@2",
    payload: {
      resume_title: "产品经理简历",
      experience_groups: [{ title: "实习", fact_refs: ["fact_1"] }],
      confirmed_facts: [{
        id: "fact_1",
        kind: "skill",
        value: "Python",
        source_hashes: ["d".repeat(64)],
      }],
      allowed_section_types: ["experience"],
    },
  };
}

function makeParseJdInput() {
  return {
    ...common,
    workflow_type: "parse_jd",
    prompt_template_version: "jd-parse@2",
    payload: {
      jd_text: "需要 Python 能力",
      job_title: "后端工程师",
      allowed_categories: ["must_have"],
    },
  };
}

function makeMatchInput() {
  return {
    ...common,
    workflow_type: "match_resume_to_jd",
    prompt_template_version: "resume-match@2",
    payload: {
      resume_version_id: "resume_1",
      resume_snapshot_hash: "e".repeat(64),
      confirmed_facts: [{ id: "fact_1", kind: "skill", value: "Python" }],
      confirmed_requirements: [{
        id: "requirement_1",
        category: "must_have",
        value: "Python",
      }],
    },
  };
}

function makeSuggestionBatchInput() {
  return {
    ...common,
    workflow_type: "generate_suggestions_batch",
    prompt_template_version: "suggestions-batch@2",
    payload: {
      matches: [{
        requirement_ref: "requirement_1",
        category: "transferable",
        fact_refs: ["fact_1"],
        target_path: "sections[0].bullets[0]",
        original_hash: "f".repeat(64),
        original_text: "负责服务开发",
      }],
      confirmed_facts: [{ id: "fact_1", kind: "skill", value: "Python" }],
      confirmed_requirements: [{
        id: "requirement_1",
        category: "must_have",
        value: "Python",
      }],
    },
  };
}

const validInputs = [
  makeAnalyzeIntakeInput(),
  makeComposeDraftInput(),
  makeParseJdInput(),
  makeMatchInput(),
  makeSuggestionBatchInput(),
];

describe("V2 workflow contracts", () => {
  it("exposes exactly the five model workflows", () => {
    expect(MODEL_WORKFLOW_TYPES).toEqual([
      "analyze_intake_answer",
      "compose_resume_draft",
      "parse_jd",
      "match_resume_to_jd",
      "generate_suggestions_batch",
    ]);
  });

  it.each(validInputs)("accepts each V2 workflow envelope", (input) => {
    expect(Value.Check(WorkflowInputSchema, input)).toBe(true);
  });

  it.each([
    { input: { ...makeParseJdInput(), owner_scope_hash: "john@example.com" } },
    { input: { ...makeParseJdInput(), input_hash: "arbitrary-string" } },
    { input: { ...makeParseJdInput(), input_hash: "A".repeat(64) } },
    {
      input: {
        ...makeAnalyzeIntakeInput(),
        payload: {
          ...makeAnalyzeIntakeInput().payload,
          session_id_hash: "john@example.com",
        },
      },
    },
    {
      input: {
        ...makeComposeDraftInput(),
        payload: {
          ...makeComposeDraftInput().payload,
          confirmed_facts: [{
            ...makeComposeDraftInput().payload.confirmed_facts[0],
            source_hashes: ["arbitrary-string"],
          }],
        },
      },
    },
    {
      input: {
        ...makeMatchInput(),
        payload: {
          ...makeMatchInput().payload,
          resume_snapshot_hash: "john@example.com",
        },
      },
    },
    {
      input: {
        ...makeSuggestionBatchInput(),
        payload: {
          ...makeSuggestionBatchInput().payload,
          matches: [{
            ...makeSuggestionBatchInput().payload.matches[0],
            original_hash: "arbitrary-string",
          }],
        },
      },
    },
  ])("rejects a noncanonical hash in every hash-bearing workflow", ({ input }) => {
    expect(Value.Check(WorkflowInputSchema, input)).toBe(false);
  });

  it("rejects the removed generic current_object payload", () => {
    expect(Value.Check(WorkflowInputSchema, {
      ...makeParseJdInput(),
      current_object: { raw: "不得被接受" },
    })).toBe(false);
  });

  it("rejects model-generated database ids and unknown payload keys", () => {
    const output = { requirements: [{
      id: "req_model",
      category: "must_have",
      priority: 1,
      value: "Python",
      source_range: { start: 0, end: 6 },
      explicitness: "explicit",
      confidence_band: "high",
    }] };
    expect(Value.Check(WORKFLOW_OUTPUT_SCHEMAS.parse_jd, output)).toBe(false);
  });
});
