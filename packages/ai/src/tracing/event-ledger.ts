import { createHash } from "node:crypto";

import type {
  TraceEvent,
  TraceUsage,
} from "../contracts.js";
import {
  TRACE_COST_USD_DECIMAL_PLACES,
  TRACE_COST_USD_MAX,
} from "../contracts.js";
import { ALLOWED_TOOL_NAMES } from "../tools/guard.js";

const KNOWN_EVENT_TYPES = new Set([
  "run_queued",
  "agent_start",
  "turn_start",
  "message_start",
  "first_token",
  "message_update",
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
const APPROVED_PROVIDERS = new Set([
  "anthropic",
  "deepseek",
  "faux",
  "google",
  "openai",
  "qwen-token-plan-cn",
  "test-faux",
]);
const APPROVED_MODELS = new Set([
  "deepseek-chat",
  "deepseek-chat-202607",
  "deepseek-v4-flash",
  "deepseek-v4-pro",
  "faux-1",
  "faux-1.1",
]);
const APPROVED_STATUSES = new Set([
  "cancelled",
  "error",
  "failed",
  "ok",
  "queued",
  "running",
  "succeeded",
]);
const APPROVED_STOP_REASONS = new Set([
  "aborted",
  "error",
  "length",
  "stop",
  "tool_use",
]);
const APPROVED_CODES = new Set([
  "INVALID_JSON",
  "OUTPUT_REFERENCE_INVALID",
  "OUTPUT_SCHEMA_INVALID",
  "UNSUPPORTED_CLAIM",
  "absolute_claim",
  "already_terminal",
  "cost_limit_exceeded",
  "fact_validation_failed",
  "input_schema_invalid",
  "invalid_json",
  "model_route_unavailable",
  "owner_instance_lost",
  "output_reference_invalid",
  "output_schema_invalid",
  "prompt_version_unavailable",
  "provider_429",
  "provider_error",
  "provider_timeout",
  "provider_unavailable",
  "route_missing",
  "runtime_failed",
  "safe_flag",
  "schema_validation_failed",
  "timeout_exceeded",
  "token_limit_exceeded",
  "tool_limit_exceeded",
  "turn_limit_exceeded",
  "unknown_id",
  "unknown_tool",
  "unsupported_award",
  "unsupported_numeric",
  "unsupported_role",
  "unsupported_tool",
]);
const JSON_PATH = /^\$(?:(?:\.[A-Za-z_][A-Za-z0-9_]*)|(?:\[(?:0|[1-9]\d*)\]))*$/;
const CANONICAL_HASH = /^[a-f0-9]{16,64}$/;
const PROTECTED_HASH = /^sha256:[a-f0-9]{16}$/;
const APPROVED_PROMPT_TEMPLATE_VERSIONS = new Set([
  "intake-answer@2",
  "jd-parse@2",
  "resume-draft@2",
  "resume-match@2",
  "suggestions-batch@2",
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

function shortHash(value: string): string {
  if (PROTECTED_HASH.test(value)) {
    return value;
  }
  return `sha256:${hash(value).slice(0, 16)}`;
}

function safeStringField(key: string, value: unknown): string | undefined {
  if (typeof value !== "string" || value.length === 0) {
    return undefined;
  }
  if (key === "provider") {
    return APPROVED_PROVIDERS.has(value) ? value : undefined;
  }
  if (key === "model" || key === "response_model") {
    return APPROVED_MODELS.has(value) ? value : shortHash(value);
  }
  if (key === "response_id") {
    return shortHash(value);
  }
  if (key === "status") {
    return APPROVED_STATUSES.has(value) ? value : undefined;
  }
  if (key === "stop_reason") {
    return APPROVED_STOP_REASONS.has(value) ? value : undefined;
  }
  if (key === "tool_name") {
    return ALLOWED_TOOL_NAMES.includes(value as never) || value === "unknown"
      ? value
      : undefined;
  }
  if (key === "schema_path") {
    return JSON_PATH.test(value) ? value : undefined;
  }
  if (["error_code", "fallback_reason", "risk_flags"].includes(key)) {
    return APPROVED_CODES.has(value) ? value : undefined;
  }
  if (key === "input_hash" || key === "source_event_type_hash") {
    return CANONICAL_HASH.test(value) ? value : shortHash(value);
  }
  if (key === "prompt_template_version") {
    return APPROVED_PROMPT_TEMPLATE_VERSIONS.has(value)
      ? value
      : shortHash(value);
  }
  return undefined;
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
        .map((entry) => safeStringField(key, entry))
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
      const normalized = safeStringField(key, value);
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
  const usageEvent =
    raw.type === "message_end" ||
    raw.type === "done" ||
    raw.type === "error";
  const usage =
    usageEvent &&
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
    const assistantMessageEvent =
      rawType === "message_update" &&
      typeof raw.assistantMessageEvent === "object" &&
      raw.assistantMessageEvent
        ? raw.assistantMessageEvent as Record<string, unknown>
        : undefined;
    const effectiveType =
      typeof assistantMessageEvent?.type === "string"
        ? assistantMessageEvent.type
        : rawType;
    let eventType = KNOWN_EVENT_TYPES.has(rawType) ? rawType : "unknown";
    if (rawType === "start") {
      eventType = "message_start";
    } else if (rawType === "done" || rawType === "error") {
      eventType = "message_end";
    } else if (effectiveType.endsWith("_delta") && !firstTokenSeen) {
      eventType = "first_token";
      firstTokenSeen = true;
    } else if (rawType === "message_update") {
      eventType = "message_update";
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
      const rawCost = number(cost.total);
      usage.cost_usd = normalizeCostUsd(usage.cost_usd + rawCost);
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

function normalizeCostUsd(value: number): number {
  if (!Number.isFinite(value) || value < 0 || value > TRACE_COST_USD_MAX) {
    throw new RangeError("cost_usd is outside the receipt contract");
  }
  return Number(value.toFixed(TRACE_COST_USD_DECIMAL_PLACES));
}
