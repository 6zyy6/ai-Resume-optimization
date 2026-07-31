import type { AssistantMessage, Model, Models } from "@earendil-works/pi-ai";
import { createModels } from "@earendil-works/pi-ai";
import { anthropicProvider } from "@earendil-works/pi-ai/providers/anthropic";
import { deepseekProvider } from "@earendil-works/pi-ai/providers/deepseek";
import { googleProvider } from "@earendil-works/pi-ai/providers/google";
import { openaiProvider } from "@earendil-works/pi-ai/providers/openai";
import { qwenTokenPlanCnProvider } from "@earendil-works/pi-ai/providers/qwen-token-plan-cn";

import {
  MODEL_WORKFLOW_TYPES,
  type PiRuntime,
  type RuntimeFailure,
  type WorkflowInput,
} from "../contracts.js";
import type { ModelRouter } from "../model-router.js";
import { resolvePrompt } from "./prompt-registry.js";
import { asBudgetModel } from "./run-budget.js";
import { WorkflowError } from "./workflow-error.js";

function isAbort(error: unknown, signal: AbortSignal): boolean {
  return signal.aborted || (
    error instanceof Error && (error.name === "AbortError" || error.message === "aborted")
  );
}

function assistantText(message: AssistantMessage): string {
  return message.content
    .filter((content) => content.type === "text")
    .map((content) => content.text)
    .join("");
}

function requireModel(
  models: Models,
  router: ModelRouter,
  input: WorkflowInput,
  routeAttempt: 0 | 1,
): { model: Model<string>; route: NonNullable<ReturnType<ModelRouter["getRoute"]>> } {
  const route = router.getRoute(input.workflow_type);
  const selected = router.getModel(input.workflow_type, routeAttempt);
  if (!route?.enabled || !selected?.approved_data_policy) {
    throw new WorkflowError("model_route_unavailable");
  }
  const model = models.getModel(selected.provider, selected.model);
  if (!model) throw new WorkflowError("model_route_unavailable");
  return { model, route };
}

function safeFailure(
  kind: "provider" | "timeout",
  events: Record<string, unknown>[],
  maxCostUsd?: number,
): RuntimeFailure {
  return {
    status: "failure",
    failure_kind: kind,
    error_code: kind === "timeout" ? "provider_timeout" : "provider_error",
    events,
    max_cost_usd: maxCostUsd,
    budget_accounted: true,
  };
}

export function createPiRuntime({
  router,
  models = createApprovedModels(),
}: {
  router: ModelRouter;
  models?: Models;
}): PiRuntime {
  const runStructured: PiRuntime["runStructured"] = async ({
    input,
    route_attempt,
    schema_feedback,
    signal,
    budget,
  }) => {
    const events: Record<string, unknown>[] = [];
    let routeMaxCost: number | undefined;
    let timeoutSignal: AbortSignal | undefined;
    try {
      const { model, route } = requireModel(models, router, input, route_attempt);
      routeMaxCost = route.max_cost_usd;
      const prompt = resolvePrompt(input, schema_feedback).text;
      const payload = JSON.stringify(input.payload);
      budget.reserveTurn();
      budget.preflightProvider(
        asBudgetModel(model),
        `${prompt}\n${payload}`,
        route.max_tokens,
        route.max_cost_usd,
      );
      timeoutSignal = AbortSignal.timeout(route.timeout_ms);
      const stream = models.streamSimple(
        model,
        {
          systemPrompt: prompt,
          messages: [{ role: "user", content: payload, timestamp: Date.now() }],
        },
        {
          maxTokens: route.max_tokens,
          reasoning: route.thinking === "off" ? undefined : route.thinking,
          signal: AbortSignal.any([signal, timeoutSignal]),
          timeoutMs: route.timeout_ms,
          maxRetries: 0,
        },
      );
      for await (const event of stream) {
        const raw = event as unknown as Record<string, unknown>;
        events.push(raw);
        budget.recordPiEvent(raw);
      }
      const message = await stream.result();
      if (signal.aborted) throw new DOMException("aborted", "AbortError");
      if (timeoutSignal.aborted) return safeFailure("timeout", events, routeMaxCost);
      if (message.stopReason === "error" || message.stopReason === "aborted") {
        return safeFailure("provider", events, routeMaxCost);
      }
      try {
        return {
          status: "success",
          output: JSON.parse(assistantText(message)) as unknown,
          events,
          max_cost_usd: route.max_cost_usd,
          budget_accounted: true,
        };
      } catch {
        return {
          status: "failure",
          failure_kind: "json",
          error_code: "invalid_json",
          events,
          max_cost_usd: route.max_cost_usd,
          budget_accounted: true,
        };
      }
    } catch (error) {
      if (timeoutSignal?.aborted && !signal.aborted) {
        return safeFailure("timeout", events, routeMaxCost);
      }
      if (isAbort(error, signal) || error instanceof WorkflowError) throw error;
      return safeFailure("provider", events, routeMaxCost);
    }
  };

  return {
    mode: "production",
    getRetryCount(input) {
      return router.getRoute(input.workflow_type)?.retry_count ?? 0;
    },
    async isReady() {
      if (!router.isReady()) return false;
      const routes = MODEL_WORKFLOW_TYPES.map((workflowType) => router.getRoute(workflowType));
      const selected = routes.flatMap((route) => route?.enabled ? [route.primary, route.fallback] : []);
      if (selected.length === 0 || selected.some(({ provider, model }) => !models.getModel(provider, model))) {
        return false;
      }
      try {
        const providers = [...new Set(selected.map(({ provider }) => provider))];
        return (await Promise.all(providers.map((provider) => models.checkAuth(provider)))).every(Boolean);
      } catch {
        return false;
      }
    },
    runStructured,
    runAgent: runStructured,
  };
}

function createApprovedModels(): Models {
  const models = createModels();
  models.setProvider(openaiProvider());
  models.setProvider(anthropicProvider());
  models.setProvider(googleProvider());
  models.setProvider(deepseekProvider());
  models.setProvider(qwenTokenPlanCnProvider());
  return models;
}
