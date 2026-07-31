import { describe, expect, it } from "vitest";

import type { PiRuntime, WorkflowInput } from "../src/contracts.js";
import { validateRuntimeOutput } from "../src/workflows/postflight.js";
import { createFixtureRuntime, runWorkflow } from "../src/workflows/run-workflow.js";

const common = {
  workflow_version: "2" as const,
  trace_id: "trace_1",
  task_id: "task_1",
  owner_scope_hash: "owner_hash",
  locale: "zh-CN" as const,
  input_version: 1,
  input_hash: "input_hash",
};

function input(workflowType: WorkflowInput["workflow_type"]): WorkflowInput {
  switch (workflowType) {
    case "analyze_intake_answer":
      return { ...common, workflow_type: workflowType, prompt_template_version: "intake-answer@2", payload: { session_id_hash: "session_hash", answer_id: "answer_1", question_id: "question_1", question_reason: "项目经历", answer_text: "我使用 Python。", answer_state: "answered", confirmed_facts: [{ id: "fact_1", kind: "skill", value: "Python" }], covered_slots: [], missing_slots: ["impact"], asked_question_ids: [] } };
    case "compose_resume_draft":
      return { ...common, workflow_type: workflowType, prompt_template_version: "resume-draft@2", payload: { resume_title: "简历", experience_groups: [{ title: "实习", fact_refs: ["fact_1"] }], confirmed_facts: [{ id: "fact_1", kind: "skill", value: "Python", source_hashes: ["source_hash"] }], allowed_section_types: ["experience"] } };
    case "parse_jd":
      return { ...common, workflow_type: workflowType, prompt_template_version: "jd-parse@2", payload: { jd_text: "Python 工程师", allowed_categories: ["must_have"] } };
    case "match_resume_to_jd":
      return { ...common, workflow_type: workflowType, prompt_template_version: "resume-match@2", payload: { resume_version_id: "resume_1", resume_snapshot_hash: "snapshot_hash", confirmed_facts: [{ id: "fact_1", kind: "skill", value: "Python" }], confirmed_requirements: [{ id: "requirement_1", category: "must_have", value: "Python" }] } };
    case "generate_suggestions_batch":
      return { ...common, workflow_type: workflowType, prompt_template_version: "suggestions-batch@2", payload: { matches: [{ requirement_ref: "requirement_1", category: "transferable", fact_refs: ["fact_1"], target_path: "sections[0].bullets[0]", original_hash: "bullet_hash", original_text: "开发服务" }], confirmed_facts: [{ id: "fact_1", kind: "skill", value: "Python" }], confirmed_requirements: [{ id: "requirement_1", category: "must_have", value: "Python" }] } };
  }
}

function output(workflowType: WorkflowInput["workflow_type"]): unknown {
  switch (workflowType) {
    case "analyze_intake_answer": return { fact_candidates: [{ kind: "skill", value: "Python", source_answer_id: "answer_1", source_range: { start: 2, end: 8 }, risk_flags: [] }], missing_slots: [], question_candidate: { reason: "补充成果", slot: "impact", text: "结果如何？", related_fact_refs: ["fact_1"] } };
    case "compose_resume_draft": return { sections: [{ type: "experience", title: "实习", bullets: [{ text: "使用 Python 开发服务", atomic_claims: [{ text: "使用 Python", fact_refs: ["fact_1"], claim_order: 0 }], risk_flags: [] }] }] };
    case "parse_jd": return { requirements: [{ category: "must_have", priority: 1, value: "Python", source_range: { start: 0, end: 6 }, explicitness: "explicit", confidence_band: "high" }] };
    case "match_resume_to_jd": return { matches: [{ requirement_ref: "requirement_1", category: "direct", fact_refs: ["fact_1"], resume_target_paths: ["sections[0]"], reason_code: "fact_match" }] };
    case "generate_suggestions_batch": return { suggestions: [{ target_path: "sections[0].bullets[0]", original_hash: "bullet_hash", suggested_text: "使用 Python 开发服务", atomic_claims: [{ text: "Python", fact_refs: ["fact_1"], claim_order: 0 }], requirement_ref: "requirement_1", reason: "补充技能", risk_flags: [], proposed_status: "pending" }] };
  }
}

describe("runWorkflow", () => {
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
      runStructured: async () => ({ status: "success", output: output(workflowType), events: [] }),
      runAgent: async () => { throw new Error("agent runtime must not be selected"); },
      getRetryCount: () => 0,
    };

    const run = await runWorkflow(input(workflowType), runtime);

    structuredCalls += 1;
    expect(run.status).toBe("succeeded");
    expect(structuredCalls).toBe(1);
  });

  it("rejects invalid output once, then sends schema feedback for correction", async () => {
    let calls = 0;
    const runtime: PiRuntime = {
      mode: "fixture",
      runAgent: async () => { throw new Error("not used"); },
      runStructured: async ({ schema_feedback }) => {
        calls += 1;
        return {
          status: "success",
          output: calls === 1 ? { requirements: [{ category: "must_have" }] } : output("parse_jd"),
          events: [],
        };
      },
    };

    await expect(runWorkflow(input("parse_jd"), runtime)).resolves.toMatchObject({ status: "succeeded" });
    expect(calls).toBe(2);
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

  it("requires complete unique matches and sourced suggestions", () => {
    const match = output("match_resume_to_jd") as any;
    match.matches = [];
    expect(validateRuntimeOutput(input("match_resume_to_jd"), match)[0]).toMatchObject({ type: "requirement_coverage_invalid" });

    const suggestion = output("generate_suggestions_batch") as any;
    suggestion.suggestions[0].original_hash = "wrong_hash";
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

  it("keeps fixture runs deterministic", async () => {
    const runtime = createFixtureRuntime({ parse_jd: output("parse_jd") });
    await expect(runWorkflow(input("parse_jd"), runtime)).resolves.toMatchObject({ output: output("parse_jd") });
  });
});
