import { calculateCost, type Model } from "@earendil-works/pi-ai";

import type {
  ModelBudgetRates,
  ResourceBudget,
} from "../contracts.js";
import { WorkflowError } from "./workflow-error.js";

const MAX_TURNS = 4;
const MAX_TOOLS = 6;
const MAX_TOKENS = 12_000;

function number(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function assistantUsage(event: Record<string, unknown>) {
  if (
    event.type !== "message_end" &&
    event.type !== "done" &&
    event.type !== "error"
  ) {
    return undefined;
  }
  const message =
    typeof event.message === "object" && event.message
      ? event.message as Record<string, unknown>
      : undefined;
  return message?.role === "assistant" &&
    typeof message.usage === "object" &&
    message.usage
    ? message.usage as Record<string, unknown>
    : undefined;
}

export function createRunBudget(): ResourceBudget {
  let turns = 0;
  let tools = 0;
  let totalTokens = 0;
  let costUsd = 0;
  let costLimitUsd: number | undefined;

  const checkRemaining = () => {
    if (turns >= MAX_TURNS) {
      throw new WorkflowError("turn_limit_exceeded");
    }
    if (tools >= MAX_TOOLS) {
      throw new WorkflowError("tool_limit_exceeded");
    }
    if (totalTokens >= MAX_TOKENS) {
      throw new WorkflowError("token_limit_exceeded");
    }
    if (costLimitUsd !== undefined && costUsd >= costLimitUsd) {
      throw new WorkflowError("cost_limit_exceeded");
    }
  };

  return {
    preflightAttempt: checkRemaining,
    setCostLimit(maxCostUsd) {
      costLimitUsd = Math.min(costLimitUsd ?? maxCostUsd, maxCostUsd);
      if (costUsd > costLimitUsd) {
        throw new WorkflowError("cost_limit_exceeded");
      }
    },
    reserveTurn() {
      if (turns >= MAX_TURNS) {
        throw new WorkflowError("turn_limit_exceeded");
      }
      turns += 1;
    },
    reserveTool() {
      if (tools >= MAX_TOOLS) {
        throw new WorkflowError("tool_limit_exceeded");
      }
      tools += 1;
    },
    preflightProvider(model, inputText, maxOutputTokens, maxCostUsd) {
      if (tools >= MAX_TOOLS) {
        throw new WorkflowError("tool_limit_exceeded");
      }
      if (totalTokens >= MAX_TOKENS) {
        throw new WorkflowError("token_limit_exceeded");
      }
      this.setCostLimit(maxCostUsd);
      const inputTokens = Math.ceil(Buffer.byteLength(inputText, "utf8") / 4);
      if (totalTokens + inputTokens + maxOutputTokens > MAX_TOKENS) {
        throw new WorkflowError("token_limit_exceeded");
      }
      const usage = {
        input: inputTokens,
        output: maxOutputTokens,
        cacheRead: 0,
        cacheWrite: 0,
        totalTokens: inputTokens + maxOutputTokens,
        cost: {
          input: 0,
          output: 0,
          cacheRead: 0,
          cacheWrite: 0,
          total: 0,
        },
      };
      const estimatedCost = calculateCost(
        model as Model<string>,
        usage,
      ).total;
      if (costUsd + estimatedCost > (costLimitUsd ?? maxCostUsd)) {
        throw new WorkflowError("cost_limit_exceeded");
      }
    },
    recordPiEvent(event) {
      if (event.type === "turn_start") {
        this.reserveTurn();
      } else if (event.type === "tool_execution_start") {
        this.reserveTool();
      }
      const usage = assistantUsage(event);
      if (usage) {
        const cost =
          typeof usage.cost === "object" && usage.cost
            ? usage.cost as Record<string, unknown>
            : {};
        totalTokens += number(usage.totalTokens);
        costUsd += number(cost.total);
        if (totalTokens > MAX_TOKENS) {
          throw new WorkflowError("token_limit_exceeded");
        }
        if (costLimitUsd !== undefined && costUsd > costLimitUsd) {
          throw new WorkflowError("cost_limit_exceeded");
        }
      }
    },
    snapshot: () => ({
      turns,
      tools,
      total_tokens: totalTokens,
      cost_usd: costUsd,
    }),
  };
}

export function asBudgetModel(model: Model<string>): ModelBudgetRates {
  return model;
}
