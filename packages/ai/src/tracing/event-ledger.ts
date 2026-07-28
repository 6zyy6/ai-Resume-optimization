import { createHash } from "node:crypto";

import type {
  TraceEvent,
  TraceUsage,
} from "../contracts.js";
import { ALLOWED_TOOL_NAMES } from "../tools/guard.js";

const KNOWN_EVENT_TYPES = new Set([
  "run_queued",
  "agent_start",
  "turn_start",
  "message_start",
  "first_token",
  "message_end",
  "tool_execution_start",
  "tool_execution_end",
  "turn_end",
  "auto_retry_start",
  "auto_retry_end",
  "model_fallback",
  "schema_validation_failed",
  "fact_validation_failed",
  "agent_end",
  "agent_settled",
  "run_succeeded",
  "run_failed",
  "run_cancelled",
  "user_accepted",
  "user_edited",
  "user_ignored",
]);

const SAFE_DETAIL_KEYS = new Set([
  "provider",
  "model",
  "response_model",
  "response_id",
  "stop_reason",
  "tool_name",
  "schema_valid",
  "status",
  "duration_ms",
  "latency_ms",
  "schema_path",
  "error_code",
  "risk_flags",
  "fallback_reason",
  "input_hash",
  "prompt_template_version",
  "input_length",
  "output_length",
  "source_event_type_hash",
  "usage",
]);

function emptyUsage(): TraceUsage {
  return {
    input: 0,
    output: 0,
    cache_read: 0,
    cache_write: 0,
    reasoning: 0,
    total_tokens: 0,
    cost_usd: 0,
  };
}

function hash(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function safeString(value: unknown, maxLength = 256): string | undefined {
  if (typeof value !== "string") {
    return undefined;
  }
  return value.replace(/[^a-zA-Z0-9_.:/[\]-]/g, "_").slice(0, maxLength);
}

function safeDetails(
  raw: Record<string, unknown> | undefined,
): Record<string, unknown> | undefined {
  if (!raw) {
    return undefined;
  }
  const safe: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(raw)) {
    if (!SAFE_DETAIL_KEYS.has(key)) {
      continue;
    }
    if (key === "risk_flags" && Array.isArray(value)) {
      safe[key] = value
        .map((entry) => safeString(entry, 128))
        .filter((entry): entry is string => Boolean(entry));
    } else if (
      ["duration_ms", "latency_ms", "input_length", "output_length"].includes(
        key,
      ) &&
      typeof value === "number" &&
      Number.isFinite(value)
    ) {
      safe[key] = value;
    } else if (key === "schema_valid" && typeof value === "boolean") {
      safe[key] = value;
    } else if (key === "usage" && typeof value === "object" && value) {
      const usage = value as Record<string, unknown>;
      safe[key] = Object.fromEntries(
        Object.entries(usage).filter(
          ([usageKey, usageValue]) =>
            [
              "input",
              "output",
              "cache_read",
              "cache_write",
              "reasoning",
              "total_tokens",
              "cost_usd",
            ].includes(usageKey) &&
            typeof usageValue === "number" &&
            Number.isFinite(usageValue),
        ),
      );
    } else {
      const normalized = safeString(value);
      if (normalized !== undefined) {
        safe[key] = normalized;
      }
    }
  }
  return Object.keys(safe).length > 0 ? safe : undefined;
}

function readUsage(raw: Record<string, unknown>): {
  usage?: Record<string, unknown>;
  message?: Record<string, unknown>;
} {
  const message =
    typeof raw.message === "object" && raw.message
      ? raw.message as Record<string, unknown>
      : undefined;
  const usage =
    message?.role === "assistant" &&
    typeof message.usage === "object" &&
    message.usage
      ? message.usage as Record<string, unknown>
      : undefined;
  return { usage, message };
}

export function createEventLedger({
  ai_run_id,
  trace_id,
  task_id,
  now = () => new Date(),
}: {
  ai_run_id: string;
  trace_id: string;
  task_id: string;
  now?: () => Date;
}) {
  const events: TraceEvent[] = [];
  const usage = emptyUsage();
  let firstTokenSeen = false;

  function append(
    eventType: string,
    rawDetails?: Record<string, unknown>,
  ): TraceEvent {
    const event: TraceEvent = {
      ai_run_id,
      trace_id,
      task_id,
      event_seq: events.length + 1,
      event_type: KNOWN_EVENT_TYPES.has(eventType) ? eventType : "unknown",
      occurred_at: now().toISOString(),
    };
    const details = safeDetails(rawDetails);
    if (details) {
      event.details = details;
    }
    events.push(event);
    return event;
  }

  function appendPiEvent(raw: Record<string, unknown>): TraceEvent {
    const rawType = typeof raw.type === "string" ? raw.type : "invalid";
    let eventType = KNOWN_EVENT_TYPES.has(rawType) ? rawType : "unknown";
    if (rawType === "start") {
      eventType = "message_start";
    } else if (rawType === "done" || rawType === "error") {
      eventType = "message_end";
    } else if (rawType === "text_delta" && !firstTokenSeen) {
      eventType = "first_token";
      firstTokenSeen = true;
    }

    const { usage: rawUsage, message } = readUsage(raw);
    if (rawUsage) {
      const cost =
        typeof rawUsage.cost === "object" && rawUsage.cost
          ? rawUsage.cost as Record<string, unknown>
          : {};
      usage.input += number(rawUsage.input);
      usage.output += number(rawUsage.output);
      usage.cache_read += number(rawUsage.cacheRead);
      usage.cache_write += number(rawUsage.cacheWrite);
      usage.reasoning += number(rawUsage.reasoning);
      usage.total_tokens += number(rawUsage.totalTokens);
      usage.cost_usd += number(cost.total);
    }

    const toolName =
      typeof raw.toolName === "string" &&
      ALLOWED_TOOL_NAMES.includes(raw.toolName as never)
        ? raw.toolName
        : raw.toolName === undefined
          ? undefined
          : "unknown";
    const details: Record<string, unknown> = {
      provider: message?.provider,
      model: message?.model,
      response_model: message?.responseModel,
      response_id: message?.responseId,
      stop_reason: message?.stopReason,
      tool_name: toolName,
      duration_ms: raw.durationMs,
      status:
        raw.isError === true ? "error" : raw.isError === false ? "ok" : undefined,
    };
    if (eventType === "unknown") {
      details.source_event_type_hash = hash(rawType).slice(0, 16);
    }
    if (rawUsage) {
      details.usage = { ...usage };
    }
    return append(eventType, details);
  }

  return {
    events,
    usage,
    append,
    appendPiEvent,
  };
}

function number(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}
