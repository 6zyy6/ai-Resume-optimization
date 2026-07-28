import { randomUUID } from "node:crypto";

import { Value } from "typebox/value";

import {
  WorkflowInputSchema,
  type PiRuntime,
  type RuntimeFailure,
  type RuntimeResult,
  type SchemaFeedback,
  type TraceEvent,
  type WorkflowInput,
  type WorkflowRun,
  type WorkflowType,
} from "../contracts.js";
import { createEventLedger } from "../tracing/event-ledger.js";
import {
  enforceEvidence,
  validateRuntimeOutput,
} from "./postflight.js";
import { resolvePrompt } from "./prompt-registry.js";
import { createRunBudget } from "./run-budget.js";
import { WorkflowError } from "./workflow-error.js";

const MAX_DURATION_MS = 30_000;
const MAX_FALLBACKS = 1;

export { createPiRuntime } from "./pi-runtime.js";
export { WorkflowError };

interface RunWorkflowOptions {
  signal?: AbortSignal;
  now?: () => number;
  onEvent?: (event: TraceEvent) => void;
  aiRunId?: string;
}

function isAbort(error: unknown, signal: AbortSignal): boolean {
  return (
    signal.aborted ||
    (error instanceof Error &&
      (error.name === "AbortError" || error.message === "aborted"))
  );
}

function failureCode(result: RuntimeFailure): WorkflowError["code"] {
  if (result.failure_kind === "timeout") {
    return "timeout_exceeded";
  }
  if (result.failure_kind === "budget") {
    return result.error_code as WorkflowError["code"];
  }
  if (result.failure_kind === "route") {
    return "model_route_unavailable";
  }
  return "runtime_failed";
}

export async function runWorkflow(
  input: WorkflowInput,
  runtime: PiRuntime,
  options: RunWorkflowOptions = {},
): Promise<WorkflowRun> {
  if (!Value.Check(WorkflowInputSchema, input)) {
    throw new WorkflowError("input_schema_invalid");
  }
  resolvePrompt(input);

  const now = options.now ?? Date.now;
  const startedAt = now();
  const deadlineController = new AbortController();
  const deadlineTimer = setTimeout(
    () => deadlineController.abort(new Error("timeout_exceeded")),
    MAX_DURATION_MS,
  );
  deadlineTimer.unref();
  const signal = options.signal
    ? AbortSignal.any([options.signal, deadlineController.signal])
    : deadlineController.signal;
  const aiRunId = options.aiRunId ?? `run_${randomUUID()}`;
  const ledger = createEventLedger({
    ai_run_id: aiRunId,
    trace_id: input.trace_id,
    task_id: input.task_id,
  });
  const budget = createRunBudget();
  let fallbackCount = 0;

  const append = (
    eventType: string,
    details?: Record<string, unknown>,
  ): TraceEvent => {
    const event = ledger.append(eventType, details);
    options.onEvent?.(event);
    return event;
  };
  const counts = () => {
    const snapshot = budget.snapshot();
    return {
      turn_count: snapshot.turns,
      tool_call_count: snapshot.tools,
    };
  };
  const cancelledRun = (): WorkflowRun => {
    if (ledger.events.at(-1)?.event_type !== "run_cancelled") {
      append("run_cancelled");
    }
    return {
      ai_run_id: aiRunId,
      trace_id: input.trace_id,
      task_id: input.task_id,
      workflow_type: input.workflow_type,
      workflow_version: input.workflow_version,
      status: "cancelled",
      usage: { ...ledger.usage },
      events: [...ledger.events],
      ...counts(),
      fallback_count: fallbackCount,
      exportable: false,
      risk_flags: [],
    };
  };
  const checkSignal = () => {
    if (deadlineController.signal.aborted && !options.signal?.aborted) {
      throw new WorkflowError("timeout_exceeded");
    }
    if (signal.aborted) {
      return false;
    }
    if (now() - startedAt > MAX_DURATION_MS) {
      throw new WorkflowError("timeout_exceeded");
    }
    return true;
  };

  append("run_queued");
  append("agent_start");

  let phase: "initial" | "retry" | "correction" | "fallback" = "initial";
  let routeAttempt: 0 | 1 = 0;
  let attempt = 0;
  let providerRetries = 0;
  const maxProviderRetries = Math.min(
    1,
    Math.max(0, runtime.getRetryCount?.(input) ?? 0),
  );
  let corrected = false;
  let feedback: SchemaFeedback[] | undefined;
  let retryOpen = false;

  try {
    while (true) {
      if (!checkSignal()) {
        return cancelledRun();
      }
      budget.preflightAttempt();

      const call =
        input.workflow_type === "next_question"
          ? runtime.runAgent
          : runtime.runStructured;
      let result: RuntimeResult;
      try {
        result = await call({
          input,
          attempt,
          route_attempt: routeAttempt,
          phase,
          schema_feedback: feedback,
          signal,
          budget,
        });
      } catch (error) {
        if (!checkSignal()) {
          return cancelledRun();
        }
        if (error instanceof WorkflowError) {
          throw error;
        }
        if (isAbort(error, signal)) {
          return cancelledRun();
        }
        result = {
          status: "failure",
          failure_kind: "provider",
          error_code: "provider_error",
          events: [],
        };
      }
      attempt += 1;

      if (!checkSignal()) {
        return cancelledRun();
      }
      if (result.max_cost_usd !== undefined) {
        budget.setCostLimit(result.max_cost_usd);
      }
      for (const rawEvent of result.events) {
        if (!checkSignal()) {
          return cancelledRun();
        }
        const event = ledger.appendPiEvent(rawEvent);
        options.onEvent?.(event);
        if (!result.budget_accounted) {
          budget.recordPiEvent(rawEvent);
        }
      }
      if (retryOpen) {
        append("auto_retry_end", {
          status: result.status === "failure" ? "error" : "ok",
          error_code:
            result.status === "failure" ? result.error_code : undefined,
        });
        retryOpen = false;
      }

      if (result.status === "failure") {
        if (result.failure_kind === "json") {
          const jsonFeedback = [{ path: "$", type: "invalid_json" }];
          append("schema_validation_failed", {
            schema_path: "$",
            error_code: "INVALID_JSON",
          });
          if (corrected) {
            throw new WorkflowError("output_schema_invalid");
          }
          corrected = true;
          feedback = jsonFeedback;
          phase = "correction";
          continue;
        }
        if (
          result.failure_kind === "provider" &&
          providerRetries < maxProviderRetries &&
          routeAttempt === 0
        ) {
          providerRetries += 1;
          phase = "retry";
          append("auto_retry_start", { error_code: result.error_code });
          retryOpen = true;
          continue;
        }
        if (
          (result.failure_kind === "provider" ||
            result.failure_kind === "timeout") &&
          routeAttempt === 0 &&
          fallbackCount < MAX_FALLBACKS
        ) {
          fallbackCount += 1;
          routeAttempt = 1;
          phase = "fallback";
          append("model_fallback", {
            fallback_reason: result.error_code,
          });
          continue;
        }
        throw new WorkflowError(failureCode(result));
      }

      const validationFailures = validateRuntimeOutput(input, result.output);
      if (validationFailures.length > 0) {
        const first = validationFailures[0]!;
        append("schema_validation_failed", {
          schema_path: first.path,
          error_code:
            first.type === "unknown_reference"
              ? "OUTPUT_REFERENCE_INVALID"
              : "OUTPUT_SCHEMA_INVALID",
        });
        if (corrected) {
          throw new WorkflowError(
            first.type === "unknown_reference"
              ? "output_reference_invalid"
              : "output_schema_invalid",
          );
        }
        corrected = true;
        feedback = validationFailures;
        phase = "correction";
        continue;
      }

      const enforced = enforceEvidence(input, result.output);
      if (enforced.failure_path) {
        append("fact_validation_failed", {
          schema_path: enforced.failure_path,
          risk_flags: enforced.risk_flags,
          error_code: "UNSUPPORTED_CLAIM",
        });
      }

      append("agent_end");
      append("agent_settled");
      append("run_succeeded");
      return {
        ai_run_id: aiRunId,
        trace_id: input.trace_id,
        task_id: input.task_id,
        workflow_type: input.workflow_type,
        workflow_version: input.workflow_version,
        status: "succeeded",
        output: enforced.output,
        usage: { ...ledger.usage },
        events: [...ledger.events],
        ...counts(),
        fallback_count: fallbackCount,
        exportable: enforced.exportable,
        risk_flags: enforced.risk_flags,
      };
    }
  } catch (error) {
    if (isAbort(error, signal)) {
      return cancelledRun();
    }
    append("run_failed", {
      error_code:
        error instanceof WorkflowError ? error.code : "RUNTIME_FAILED",
    });
    throw error;
  } finally {
    clearTimeout(deadlineTimer);
  }
}

export function createFixtureRuntime(
  outputs: Partial<Record<WorkflowType, unknown>>,
): PiRuntime {
  const call: PiRuntime["runStructured"] = async ({ input, signal }) => {
    if (signal.aborted) {
      throw new DOMException("aborted", "AbortError");
    }
    return {
      status: "success",
      output: structuredClone(outputs[input.workflow_type]),
      events: [],
    };
  };
  return {
    mode: "fixture",
    runStructured: call,
    runAgent: call,
    getRetryCount: () => 0,
    isReady: async () => true,
  };
}
