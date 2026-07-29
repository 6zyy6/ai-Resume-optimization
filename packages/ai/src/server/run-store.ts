import type { WorkflowRun } from "../contracts.js";

export type RunStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled";

export const RUN_LEASE_MS = 15_000;

export interface RunRecord {
  ai_run_id: string;
  status: RunStatus;
  owner_instance_id: string;
  cancel_requested: boolean;
  lease_expires_at: number;
  result?: WorkflowRun;
  error_code?: string;
}

export type CancelRequestResult =
  | { outcome: "not_found" }
  | { outcome: "terminal"; status: RunStatus }
  | { outcome: "accepted"; owner_instance_id: string };

export interface RunStore {
  isReady(): Promise<boolean>;
  create(record: RunRecord): Promise<void>;
  get(aiRunId: string): Promise<RunRecord | undefined>;
  markRunning(aiRunId: string): Promise<RunRecord | undefined>;
  heartbeat(aiRunId: string, instanceId: string): Promise<boolean>;
  complete(
    aiRunId: string,
    status: Extract<RunStatus, "succeeded" | "failed" | "cancelled">,
    result?: WorkflowRun,
    errorCode?: string,
  ): Promise<RunRecord | undefined>;
  requestCancel(aiRunId: string): Promise<CancelRequestResult>;
  subscribe(
    instanceId: string,
    onCancel: (aiRunId: string) => void,
  ): Promise<() => Promise<void>>;
}

export function isTerminalStatus(status: RunStatus): boolean {
  return status === "succeeded" ||
    status === "failed" ||
    status === "cancelled";
}
