import {
  isTerminalStatus,
  type CancelRequestResult,
  type CreateRunResult,
  type RunRecord,
  type RunStore,
  type RunStatus,
  terminalReceipt,
} from "./run-store.js";
import { RUN_LEASE_MS } from "./run-store.js";
import type { AiExecutionReceipt } from "../contracts.js";

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

  async createOrGet(record: RunRecord): Promise<CreateRunResult> {
    const existing = this.runs.get(record.ai_run_id);
    if (existing) {
      return {
        kind: existing.input_hash === record.input_hash ? "existing" : "conflict",
        run: structuredClone(existing),
      };
    }
    const run = structuredClone(record);
    this.runs.set(record.ai_run_id, run);
    return { kind: "created", run: structuredClone(run) };
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
    instanceId: string,
    status: Extract<RunStatus, "succeeded" | "failed" | "cancelled">,
    receipt: AiExecutionReceipt,
    errorCode?: string,
  ): Promise<RunRecord | undefined> {
    const record = this.runs.get(aiRunId);
    const now = this.now();
    if (!record || isTerminalStatus(record.status)) {
      return this.clone(record);
    }
    if (record.owner_instance_id !== instanceId) {
      return this.clone(record);
    }
    if (record.lease_expires_at <= now) {
      record.status = "failed";
      record.error_code = "owner_instance_lost";
      record.receipt = terminalReceipt(
        record.context,
        "failed",
        "owner_instance_lost",
        receipt,
        new Date(now).toISOString(),
      );
      return this.clone(record);
    }
    const finalStatus =
      record.cancel_requested && status === "succeeded"
        ? "cancelled"
        : status;
    record.status = finalStatus;
    record.receipt = finalStatus === status
      ? structuredClone(receipt)
      : terminalReceipt(record.context, "cancelled", null, receipt, new Date(now).toISOString());
    record.error_code = record.receipt.run.error_code ?? undefined;
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
      record.receipt = terminalReceipt(
        record.context,
        "failed",
        "owner_instance_lost",
        record.receipt,
        new Date(this.now()).toISOString(),
      );
    }
  }
}
