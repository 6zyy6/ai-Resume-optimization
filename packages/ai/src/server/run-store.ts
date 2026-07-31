import type {
  AiExecutionReceipt,
  TraceEvent,
  TraceUsage,
  WorkflowRun,
  WorkflowType,
} from "../contracts.js";

export type RunStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled";

export const RUN_LEASE_MS = 15_000;

export interface StoredRunContext {
  ai_run_id: string;
  workflow_type: WorkflowType;
  workflow_version: string;
  prompt_template_version: string;
  trace_id: string;
  task_id: string;
  input_hash: string;
  started_at: string;
}

export interface RunRecord {
  ai_run_id: string;
  input_hash: string;
  status: RunStatus;
  owner_instance_id: string;
  cancel_requested: boolean;
  lease_expires_at: number;
  context: StoredRunContext;
  receipt?: AiExecutionReceipt;
  error_code?: string;
}

export type CreateRunResult =
  | { kind: "created"; run: RunRecord }
  | { kind: "existing"; run: RunRecord }
  | { kind: "conflict"; run: RunRecord };

export type CancelRequestResult =
  | { outcome: "not_found" }
  | { outcome: "terminal"; status: RunStatus }
  | { outcome: "accepted"; owner_instance_id: string };

export interface RunStore {
  isReady(): Promise<boolean>;
  createOrGet(record: RunRecord): Promise<CreateRunResult>;
  get(aiRunId: string): Promise<RunRecord | undefined>;
  markRunning(aiRunId: string): Promise<RunRecord | undefined>;
  heartbeat(aiRunId: string, instanceId: string): Promise<boolean>;
  complete(
    aiRunId: string,
    status: Extract<RunStatus, "succeeded" | "failed" | "cancelled">,
    receipt: AiExecutionReceipt,
    errorCode?: string,
  ): Promise<RunRecord | undefined>;
  requestCancel(aiRunId: string): Promise<CancelRequestResult>;
  subscribe(
    instanceId: string,
    onCancel: (aiRunId: string) => void,
  ): Promise<() => Promise<void>>;
}

const EMPTY_USAGE: TraceUsage = {
  input: 0,
  output: 0,
  cache_read: 0,
  cache_write: 0,
  reasoning: 0,
  total_tokens: 0,
  cost_usd: 0,
};

export function terminalReceipt(
  context: StoredRunContext,
  status: Extract<RunStatus, "succeeded" | "failed" | "cancelled">,
  errorCode: string | null,
  previous?: AiExecutionReceipt,
  now = new Date().toISOString(),
): AiExecutionReceipt {
  const prior = previous?.run;
  const eventType = status === "cancelled" ? "run_cancelled" : status === "failed" ? "run_failed" : "run_succeeded";
  const events = [...(prior?.events ?? [])];
  if (events.at(-1)?.event_type !== eventType) {
    events.push({
      ai_run_id: context.ai_run_id ?? prior?.ai_run_id ?? "",
      trace_id: context.trace_id,
      task_id: context.task_id,
      event_seq: events.length + 1,
      event_type: eventType,
      occurred_at: now,
      ...(status === "failed" && errorCode ? { details: { error_code: errorCode } } : {}),
    } as TraceEvent);
  }
  const run: WorkflowRun = {
    ai_run_id: prior?.ai_run_id ?? context.ai_run_id ?? "",
    trace_id: context.trace_id,
    task_id: context.task_id,
    workflow_type: context.workflow_type,
    workflow_version: context.workflow_version,
    prompt_template_version: context.prompt_template_version,
    status,
    error_code: errorCode,
    provider: prior?.provider ?? null,
    requested_model: prior?.requested_model ?? null,
    response_model: prior?.response_model ?? null,
    started_at: context.started_at,
    first_token_at: prior?.first_token_at ?? null,
    finished_at: now,
    usage: prior?.usage ?? { ...EMPTY_USAGE },
    events,
    turn_count: prior?.turn_count ?? 0,
    tool_call_count: prior?.tool_call_count ?? 0,
    retry_count: prior?.retry_count ?? 0,
    fallback_count: prior?.fallback_count ?? 0,
    schema_valid: prior?.schema_valid ?? false,
    facts_valid: prior?.facts_valid ?? false,
    input_hash: context.input_hash,
    exportable: prior?.exportable ?? false,
    risk_flags: prior?.risk_flags ?? [],
  };
  return status === "succeeded"
    ? { run, result: previous?.result }
    : { run };
}

export function isTerminalStatus(status: RunStatus): boolean {
  return status === "succeeded" ||
    status === "failed" ||
    status === "cancelled";
}
