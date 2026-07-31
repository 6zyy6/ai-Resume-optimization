import type { WorkflowType } from "../contracts.js";
import {
  createModelRouterFromEnv,
} from "../model-router.js";
import {
  createFixtureRuntime,
  createPiRuntime,
} from "../workflows/run-workflow.js";
import { buildApp } from "./app.js";
import { MemoryRunStore } from "./memory-run-store.js";
import { RedisRunStore } from "./redis-run-store.js";

const fixtureOutputs: Partial<Record<WorkflowType, unknown>> = {
  analyze_intake_answer: { fact_candidates: [], missing_slots: [], question_candidate: null },
  compose_resume_draft: { sections: [] },
  parse_jd: { requirements: [] },
  match_resume_to_jd: { matches: [] },
  generate_suggestions_batch: { suggestions: [] },
};

const mode = process.env.AI_RUNTIME_MODE === "fixture"
  ? "fixture" as const
  : "production" as const;
const modelRouter = createModelRouterFromEnv();
const runtime =
  mode === "fixture"
    ? createFixtureRuntime(fixtureOutputs)
    : createPiRuntime({ router: modelRouter });
const runStore =
  mode === "fixture"
    ? new MemoryRunStore()
    : await RedisRunStore.connect({
        url: process.env.AI_REDIS_URL ?? process.env.REDIS_URL ?? "",
      });
const app = buildApp({
  mode,
  runtime,
  modelRouter,
  runStore,
  instanceId: process.env.AI_INSTANCE_ID ?? process.env.HOSTNAME,
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
