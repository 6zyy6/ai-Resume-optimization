import type { WorkflowType } from "../contracts.js";
import {
  createModelRouterFromEnv,
} from "../model-router.js";
import {
  createFixtureRuntime,
  createPiRuntime,
} from "../workflows/run-workflow.js";
import { buildApp } from "./app.js";

const fixtureOutputs: Partial<Record<WorkflowType, unknown>> = {
  extract_facts: { facts: [] },
  next_question: {
    question: {
      question_id: "fixture_question",
      text: "请补充当前经历中尚未确认的信息。",
      fact_refs: [],
    },
  },
  write_experience_bullet: {
    suggestion_text: "待用户确认",
    atomic_claims: [],
    jd_requirement_refs: [],
    reason: "fixture",
    risk_flags: [],
    requires_user_confirmation: true,
  },
  parse_jd: { requirements: [] },
  match_resume_to_jd: { matches: [] },
  generate_suggestion: {
    suggestion_text: "待用户确认",
    atomic_claims: [],
    jd_requirement_refs: [],
    reason: "fixture",
    risk_flags: [],
    requires_user_confirmation: true,
  },
  fact_check: { claims: [], exportable: false, risk_flags: [] },
  style_check: { issues: [], passed: true },
};

const mode = process.env.AI_RUNTIME_MODE === "fixture"
  ? "fixture" as const
  : "production" as const;
const modelRouter = createModelRouterFromEnv();
const runtime =
  mode === "fixture"
    ? createFixtureRuntime(fixtureOutputs)
    : createPiRuntime({ router: modelRouter });
const app = buildApp({
  mode,
  runtime,
  modelRouter,
  serviceToken: process.env.AI_SERVICE_TOKEN,
});
const requestedPort = Number(process.env.AI_PORT ?? "3101");
const port = Number.isInteger(requestedPort) && requestedPort > 0
  ? requestedPort
  : 3101;

await app.listen({
  host: process.env.AI_HOST ?? "127.0.0.1",
  port,
});
