import { buildApp } from "../../../packages/ai/dist/src/server/app.js";
import { MemoryRunStore } from "../../../packages/ai/dist/src/server/memory-run-store.js";
import { createModelRouterFromEnv } from "../../../packages/ai/dist/src/model-router.js";

function analyzeIntake(input) {
  const answer = input.payload.answer_text;
  const values = ["负责用户调研", "完成产品原型"];
  return {
    fact_candidates: values.map((value) => {
      const start = answer.indexOf(value);
      return {
        kind: "experience",
        value,
        source_answer_id: input.payload.answer_id,
        source_range: { start, end: start + value.length },
        risk_flags: [],
      };
    }),
    missing_slots: [],
    question_candidate: null,
  };
}

function composeDraft(input) {
  const facts = input.payload.confirmed_facts;
  return {
    sections: [{
      type: input.payload.allowed_section_types[0],
      title: "项目经历",
      bullets: facts.map((fact) => ({
        text: fact.value,
        atomic_claims: [{
          text: fact.value,
          fact_refs: [fact.id],
          claim_order: 0,
        }],
        risk_flags: [],
      })),
    }],
  };
}

function parseJob(input) {
  const values = input.payload.jd_text.split("\n").filter(Boolean);
  return {
    requirements: values.map((value, index) => {
      const start = input.payload.jd_text.indexOf(value);
      return {
        category: index === 0 ? "responsibility" : "nice_to_have",
        priority: index + 1,
        value,
        source_range: { start, end: start + value.length },
        explicitness: "explicit",
        confidence_band: "high",
      };
    }),
  };
}

function matchResume(input) {
  const factIds = input.payload.confirmed_facts.map(({ id }) => id);
  return {
    matches: input.payload.confirmed_requirements.map((requirement, index) => ({
      requirement_ref: requirement.id,
      category: index === 0 ? "transferable" : "needs_evidence",
      fact_refs: factIds.length ? [factIds[index % factIds.length]] : [],
      resume_target_paths: [`/sections/0/items/${index}/text`],
      reason_code: index === 0
        ? "evidence_underexpressed"
        : "evidence_needs_confirmation",
    })),
  };
}

function generateSuggestions(input) {
  const facts = new Map(
    input.payload.confirmed_facts.map((fact) => [fact.id, fact.value]),
  );
  return {
    suggestions: input.payload.matches.map((match) => {
      const factId = match.fact_refs[0];
      const text = facts.get(factId) ?? match.original_text;
      return {
        target_path: match.target_path,
        original_hash: match.original_hash,
        suggested_text: text,
        atomic_claims: [{ text, fact_refs: [factId], claim_order: 0 }],
        requirement_ref: match.requirement_ref,
        reason: "基于已确认事实补强表达",
        risk_flags: [],
        proposed_status: match.category === "transferable" ? "pending" : "blocked",
      };
    }),
  };
}

const call = async ({ input, signal }) => {
  if (signal.aborted) throw new DOMException("aborted", "AbortError");
  const outputs = {
    analyze_intake_answer: analyzeIntake,
    compose_resume_draft: composeDraft,
    parse_jd: parseJob,
    match_resume_to_jd: matchResume,
    generate_suggestions_batch: generateSuggestions,
  };
  const output = outputs[input.workflow_type](input);
  return { status: "success", output, events: [] };
};
const runtime = {
  mode: "fixture",
  runStructured: call,
  runAgent: call,
  getRetryCount: () => 0,
  isReady: async () => true,
};
const app = buildApp({
  mode: "fixture",
  runtime,
  modelRouter: createModelRouterFromEnv(),
  runStore: new MemoryRunStore(),
  serviceToken: process.env.AI_SERVICE_TOKEN,
  instanceId: "contract-pi",
});

await app.listen({
  host: "127.0.0.1",
  port: Number(process.env.AI_PORT),
});
