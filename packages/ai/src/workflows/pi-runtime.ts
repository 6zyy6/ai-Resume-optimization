import {
  Agent,
  type AgentEvent,
  type AgentTool,
} from "@earendil-works/pi-agent-core";
import type {
  AssistantMessage,
  Models,
  Model,
} from "@earendil-works/pi-ai";
import { createModels } from "@earendil-works/pi-ai";
import { anthropicProvider } from "@earendil-works/pi-ai/providers/anthropic";
import { googleProvider } from "@earendil-works/pi-ai/providers/google";
import { openaiProvider } from "@earendil-works/pi-ai/providers/openai";

import {
  WORKFLOW_TYPES,
  type PiRuntime,
  type RuntimeFailure,
  type WorkflowInput,
} from "../contracts.js";
import type { ModelRouter } from "../model-router.js";
import {
  ALLOWED_TOOL_NAMES,
  TOOL_PARAMETER_SCHEMAS,
  ToolGuardError,
  createToolGuard,
} from "../tools/guard.js";
import { resolvePrompt } from "./prompt-registry.js";
import { asBudgetModel } from "./run-budget.js";
import { WorkflowError } from "./workflow-error.js";

function isAbort(error: unknown, signal: AbortSignal): boolean {
  return (
    signal.aborted ||
    (error instanceof Error &&
      (error.name === "AbortError" || error.message === "aborted"))
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
  if (!route || !selected?.approved_data_policy) {
    throw new WorkflowError("model_route_unavailable");
  }
  const model = models.getModel(selected.provider, selected.model);
  if (!model) {
    throw new WorkflowError("model_route_unavailable");
  }
  return { model, route };
}

function callerPayload(input: WorkflowInput): Record<string, unknown> {
  return {
    locale: input.locale,
    target: input.target,
    confirmed_facts: input.confirmed_facts,
    jd_requirements: input.jd_requirements,
    current_object: input.current_object,
  };
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
  return {
    mode: "production",
    getRetryCount(input) {
      return router.getRoute(input.workflow_type)?.retry_count ?? 0;
    },
    async isReady() {
      if (!router.isReady()) {
        return false;
      }
      const routes = WORKFLOW_TYPES.map((workflowType) =>
        router.getRoute(workflowType)
      );
      const selected = routes.flatMap((route) =>
        route ? [route.primary, route.fallback] : []
      );
      if (
        selected.length !== WORKFLOW_TYPES.length * 2 ||
        selected.some(({ provider, model }) => !models.getModel(provider, model))
      ) {
        return false;
      }
      try {
        const providers = [...new Set(selected.map(({ provider }) => provider))];
        const auth = await Promise.all(
          providers.map((provider) => models.checkAuth(provider)),
        );
        return auth.every(Boolean);
      } catch {
        return false;
      }
    },
    async runStructured({
      input,
      route_attempt,
      schema_feedback,
      signal,
      budget,
    }) {
      const events: Record<string, unknown>[] = [];
      let routeMaxCost: number | undefined;
      let timeoutSignal: AbortSignal | undefined;
      try {
        const { model, route } = requireModel(
          models,
          router,
          input,
          route_attempt,
        );
        routeMaxCost = route.max_cost_usd;
        const prompt = resolvePrompt(input, schema_feedback).text;
        const payload = JSON.stringify(callerPayload(input));
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
            messages: [
              {
                role: "user",
                content: payload,
                timestamp: Date.now(),
              },
            ],
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
        if (signal.aborted) {
          throw new DOMException("aborted", "AbortError");
        }
        if (timeoutSignal.aborted) {
          return safeFailure("timeout", events, routeMaxCost);
        }
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
        if (isAbort(error, signal) || error instanceof WorkflowError) {
          throw error;
        }
        return safeFailure("provider", events, routeMaxCost);
      }
    },
    async runAgent({
      input,
      route_attempt,
      schema_feedback,
      signal,
      budget,
    }) {
      const events: Record<string, unknown>[] = [];
      let routeMaxCost: number | undefined;
      let timeoutSignal: AbortSignal | undefined;
      try {
        const { model, route } = requireModel(
          models,
          router,
          input,
          route_attempt,
        );
        routeMaxCost = route.max_cost_usd;
        const guard = createToolGuard({
          confirmedFacts: input.confirmed_facts,
          jdRequirements: input.jd_requirements,
        });
        let output: unknown;
        let resourceError: WorkflowError | undefined;
        let agent: Agent;
        timeoutSignal = AbortSignal.timeout(route.timeout_ms);

        const tools = ALLOWED_TOOL_NAMES.map((name) => ({
          name,
          label: name,
          description: `Approved resume workflow tool: ${name}`,
          parameters: TOOL_PARAMETER_SCHEMAS[name],
          executionMode: "sequential",
          execute: async (
            _toolCallId: string,
            parameters: unknown,
            toolSignal?: AbortSignal,
          ) => {
            if (signal.aborted || toolSignal?.aborted) {
              throw new DOMException("aborted", "AbortError");
            }
            const result = guard.execute(name, parameters);
            if (
              name === "emit_question" ||
              name === "emit_resume_suggestion" ||
              name === "emit_fact_check_result"
            ) {
              const record = result as Record<string, unknown>;
              output =
                record.question ??
                record.suggestion ??
                record.fact_check_result;
              if (name === "emit_question") {
                output = { question: output };
              }
            }
            return {
              content: [{ type: "text", text: JSON.stringify(result) }],
              details: { schema_valid: true },
              terminate: guard.snapshot().terminal,
            };
          },
        })) as AgentTool[];

        agent = new Agent({
          initialState: {
            systemPrompt: resolvePrompt(input, schema_feedback).text,
            model,
            thinkingLevel: route.thinking,
            tools,
            messages: [],
          },
          streamFn: (selectedModel, context, options) => {
            if (resourceError) {
              throw resourceError;
            }
            budget.preflightProvider(
              asBudgetModel(selectedModel),
              JSON.stringify(context),
              route.max_tokens,
              route.max_cost_usd,
            );
            return models.streamSimple(selectedModel, context, {
              ...options,
              signal: options?.signal
                ? AbortSignal.any([signal, timeoutSignal!, options.signal])
                : AbortSignal.any([signal, timeoutSignal!]),
              maxTokens: route.max_tokens,
              timeoutMs: route.timeout_ms,
              maxRetries: 0,
            });
          },
          toolExecution: "sequential",
          beforeToolCall: async ({ toolCall, args }) => {
            if (resourceError) {
              return { block: true, reason: resourceError.code };
            }
            try {
              guard.preflight(toolCall.name, args);
              return undefined;
            } catch (error) {
              return {
                block: true,
                reason:
                  error instanceof ToolGuardError ? error.code : "tool_blocked",
              };
            }
          },
          afterToolCall: async ({ toolCall, isError }) => ({
            details: { schema_valid: !isError },
            content:
              toolCall.name.startsWith("emit_") && !isError
                ? [{ type: "text", text: "accepted" }]
                : undefined,
            terminate: guard.snapshot().terminal,
          }),
        });

        const unsubscribe = agent.subscribe((event: AgentEvent) => {
          const raw = event as unknown as Record<string, unknown>;
          events.push(raw);
          try {
            budget.recordPiEvent(raw);
          } catch (error) {
            resourceError =
              error instanceof WorkflowError
                ? error
                : new WorkflowError("runtime_failed");
            agent.abort();
          }
        });
        const abort = () => agent.abort();
        signal.addEventListener("abort", abort, { once: true });
        try {
          await agent.prompt(JSON.stringify({
            locale: input.locale,
            target: input.target,
            confirmed_fact_ids: input.confirmed_facts.map(({ id }) => id),
            jd_requirement_ids: input.jd_requirements.map(({ id }) => id),
            current_object: input.current_object,
          }));
          await agent.waitForIdle();
        } finally {
          signal.removeEventListener("abort", abort);
          unsubscribe();
        }
        if (resourceError) {
          throw resourceError;
        }
        if (signal.aborted) {
          throw new DOMException("aborted", "AbortError");
        }
        if (timeoutSignal.aborted) {
          return safeFailure("timeout", events, routeMaxCost);
        }
        if (output === undefined) {
          const lastAssistant = [...agent.state.messages]
            .reverse()
            .find(
              (message): message is AssistantMessage =>
                message.role === "assistant",
            );
          if (
            !lastAssistant ||
            lastAssistant.stopReason === "error" ||
            lastAssistant.stopReason === "aborted"
          ) {
            return safeFailure("provider", events, routeMaxCost);
          }
          try {
            output = JSON.parse(assistantText(lastAssistant)) as unknown;
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
        }
        return {
          status: "success",
          output,
          events,
          max_cost_usd: route.max_cost_usd,
          budget_accounted: true,
        };
      } catch (error) {
        if (timeoutSignal?.aborted && !signal.aborted) {
          return safeFailure("timeout", events, routeMaxCost);
        }
        if (isAbort(error, signal) || error instanceof WorkflowError) {
          throw error;
        }
        return safeFailure("provider", events, routeMaxCost);
      }
    },
  };
}

function createApprovedModels(): Models {
  const models = createModels();
  models.setProvider(openaiProvider());
  models.setProvider(anthropicProvider());
  models.setProvider(googleProvider());
  return models;
}
