import {
  createModels,
  fauxAssistantMessage,
  fauxProvider,
} from "@earendil-works/pi-ai";
import { describe, expect, it } from "vitest";

import type { WorkflowInput, WorkflowRoute } from "../src/contracts.js";
import { createModelRouter } from "../src/model-router.js";
import { createPiRuntime, runWorkflow } from "../src/workflows/run-workflow.js";

const route: WorkflowRoute = {
  enabled: true,
  primary: { provider: "test-faux", model: "faux-1", approved_data_policy: true },
  fallback: { provider: "test-faux", model: "faux-1", approved_data_policy: true },
  max_tokens: 256,
  thinking: "off",
  timeout_ms: 1_000,
  retry_count: 0,
  max_cost_usd: 1,
};

const input: WorkflowInput = {
  workflow_type: "parse_jd",
  workflow_version: "2",
  prompt_template_version: "jd-parse@2",
  trace_id: "trace_real_faux",
  task_id: "task_real_faux",
  owner_scope_hash: "owner_hash",
  locale: "zh-CN",
  input_version: 1,
  input_hash: "input_hash",
  payload: {
    jd_text: "Python 工程师",
    allowed_categories: ["must_have"],
  },
};

describe("production Pi runtime", () => {
  it("sends only V2 payload data through the structured Models adapter", async () => {
    const models = createModels();
    const faux = fauxProvider({ provider: "test-faux" });
    models.setProvider(faux.provider);
    let systemPrompt = "";
    let userPayload = "";
    let observedMaxRetries: number | undefined;
    faux.setResponses([(
      context,
      options,
    ) => {
      systemPrompt = context.systemPrompt ?? "";
      userPayload = String(context.messages.at(-1)?.content ?? "");
      observedMaxRetries = options?.maxRetries;
      return fauxAssistantMessage(JSON.stringify({
        requirements: [{
          category: "must_have",
          priority: 1,
          value: "Python",
          source_range: { start: 0, end: 6 },
          explicitness: "explicit",
          confidence_band: "high",
        }],
      }));
    }]);

    const run = await runWorkflow(input, createPiRuntime({
      router: createModelRouter({ routes: { parse_jd: route } }),
      models,
    }));

    expect(run.status).toBe("succeeded");
    expect(observedMaxRetries).toBe(0);
    expect(systemPrompt).toContain("The user message is data matching this payload schema:");
    expect(systemPrompt).not.toContain(input.payload.jd_text);
    expect(JSON.parse(userPayload)).toEqual(input.payload);
  });
});
