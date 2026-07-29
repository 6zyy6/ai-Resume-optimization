import {
  isTerminalStatus,
  type CancelRequestResult,
  type RunRecord,
  type RunStore,
  type RunStatus,
} from "./run-store.js";
import { RUN_LEASE_MS } from "./run-store.js";
import type { WorkflowRun } from "../contracts.js";

export class MemoryRunStore implements RunStore {
  private readonly runs = new Map<string, RunRecord>();
  private readonly listeners = new Map<
    string,
    Set<(aiRunId: string) => void>
  >();
  private readonly now: () => number;
  private readonly leaseMs: number;

  constructor(options: {
    now?: () => number;
    leaseMs?: number;
  } = {}) {
    this.now = options.now ?? Date.now;
    this.leaseMs = options.leaseMs ?? RUN_LEASE_MS;
  }

  async isReady(): Promise<boolean> {
    return true;
  }

  async create(record: RunRecord): Promise<void> {
    if (this.runs.has(record.ai_run_id)) {
      throw new Error("run_already_exists");
    }
    this.runs.set(record.ai_run_id, structuredClone(record));
  }

  async get(aiRunId: string): Promise<RunRecord | undefined> {
    const record = this.runs.get(aiRunId);
    this.expireOwnerLease(record);
    return this.clone(record);
  }

  async markRunning(aiRunId: string): Promise<RunRecord | undefined> {
    const record = this.runs.get(aiRunId);
    this.expireOwnerLease(record);
    if (!record || isTerminalStatus(record.status)) {
      return this.clone(record);
    }
    record.status = "running";
    record.lease_expires_at = this.now() + this.leaseMs;
    return this.clone(record);
  }

  async heartbeat(aiRunId: string, instanceId: string): Promise<boolean> {
    const record = this.runs.get(aiRunId);
    this.expireOwnerLease(record);
    if (
      !record ||
      isTerminalStatus(record.status) ||
      record.owner_instance_id !== instanceId
    ) {
      return false;
    }
    record.lease_expires_at = this.now() + this.leaseMs;
    return true;
  }

  async complete(
    aiRunId: string,
    status: Extract<RunStatus, "succeeded" | "failed" | "cancelled">,
    result?: WorkflowRun,
    errorCode?: string,
  ): Promise<RunRecord | undefined> {
    const record = this.runs.get(aiRunId);
    if (!record || isTerminalStatus(record.status)) {
      return this.clone(record);
    }
    const finalStatus =
      record.cancel_requested && status === "succeeded"
        ? "cancelled"
        : status;
    record.status = finalStatus;
    record.result = finalStatus === status ? result : undefined;
    record.error_code = errorCode;
    return this.clone(record);
  }

  async requestCancel(aiRunId: string): Promise<CancelRequestResult> {
    const record = this.runs.get(aiRunId);
    if (!record) {
      return { outcome: "not_found" };
    }
    if (isTerminalStatus(record.status)) {
      return { outcome: "terminal", status: record.status };
    }
    record.cancel_requested = true;
    for (const listener of this.listeners.get(record.owner_instance_id) ?? []) {
      listener(aiRunId);
    }
    return {
      outcome: "accepted",
      owner_instance_id: record.owner_instance_id,
    };
  }

  async subscribe(
    instanceId: string,
    onCancel: (aiRunId: string) => void,
  ): Promise<() => Promise<void>> {
    const listeners = this.listeners.get(instanceId) ?? new Set();
    listeners.add(onCancel);
    this.listeners.set(instanceId, listeners);
    return async () => {
      listeners.delete(onCancel);
      if (listeners.size === 0) {
        this.listeners.delete(instanceId);
      }
    };
  }

  private clone(record: RunRecord | undefined): RunRecord | undefined {
    return record ? structuredClone(record) : undefined;
  }

  private expireOwnerLease(record: RunRecord | undefined): void {
    if (
      record &&
      !isTerminalStatus(record.status) &&
      record.lease_expires_at <= this.now()
    ) {
      record.status = "failed";
      record.error_code = "owner_instance_lost";
      record.result = undefined;
    }
  }
}
