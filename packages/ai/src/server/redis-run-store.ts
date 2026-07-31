import { createClient, type RedisClientType } from "redis";

import type { AiExecutionReceipt } from "../contracts.js";
import {
  type CancelRequestResult,
  type CreateRunResult,
  type RunRecord,
  type RunStatus,
  type RunStore,
  isTerminalStatus,
  terminalReceipt,
} from "./run-store.js";
import { RUN_LEASE_MS } from "./run-store.js";

const DEFAULT_TTL_SECONDS = 24 * 60 * 60;
const KEY_PREFIX = "pi:run:";
const CANCEL_CHANNEL_PREFIX = "pi:cancel:";
const OWNER_LOST_RECEIPT_LUA = `
local context = cjson.decode(redis.call("HGET", KEYS[1], "context_json"))
local occurred_at = ARGV[#ARGV]
local event = {
  ai_run_id = context.ai_run_id,
  trace_id = context.trace_id,
  task_id = context.task_id,
  event_seq = 1,
  event_type = "run_failed",
  occurred_at = occurred_at,
  details = { error_code = "owner_instance_lost" }
}
local run = {
  ai_run_id = context.ai_run_id,
  trace_id = context.trace_id,
  task_id = context.task_id,
  workflow_type = context.workflow_type,
  workflow_version = context.workflow_version,
  prompt_template_version = context.prompt_template_version,
  status = "failed",
  error_code = "owner_instance_lost",
  provider = cjson.null,
  requested_model = cjson.null,
  response_model = cjson.null,
  started_at = context.started_at,
  first_token_at = cjson.null,
  finished_at = occurred_at,
  usage = { input = 0, output = 0, cache_read = 0, cache_write = 0, reasoning = 0, total_tokens = 0, cost_usd = 0 },
  events = { event },
  turn_count = 0,
  tool_call_count = 0,
  retry_count = 0,
  fallback_count = 0,
  schema_valid = false,
  facts_valid = false,
  input_hash = context.input_hash,
  exportable = false,
  risk_flags = cjson.decode("[]")
}
redis.call("HSET", KEYS[1], "receipt_json", cjson.encode({ run = run }))
`;

const CREATE_SCRIPT = `
if redis.call("EXISTS", KEYS[1]) == 1 then
  if redis.call("HGET", KEYS[1], "input_hash") == ARGV[2] then
    return "existing"
  end
  return "conflict"
end
redis.call("HSET", KEYS[1],
  "ai_run_id", ARGV[1],
  "input_hash", ARGV[2],
  "status", ARGV[3],
  "owner_instance_id", ARGV[4],
  "cancel_requested", ARGV[5],
  "lease_expires_at", ARGV[6],
  "receipt_json", ARGV[7],
  "error_code", ARGV[8],
  "context_json", ARGV[9])
redis.call("EXPIRE", KEYS[1], ARGV[10])
return "created"
`;

const MARK_RUNNING_SCRIPT = `
local status = redis.call("HGET", KEYS[1], "status")
if not status then
  return 0
end
if status ~= "succeeded" and status ~= "failed" and status ~= "cancelled" then
  local lease = tonumber(redis.call("HGET", KEYS[1], "lease_expires_at") or "0")
  if lease <= tonumber(ARGV[1]) then
    redis.call("HSET", KEYS[1],
      "status", "failed",
      "error_code", "owner_instance_lost",
      "receipt_json", "")
    ${OWNER_LOST_RECEIPT_LUA}
  else
    redis.call("HSET", KEYS[1],
      "status", "running",
      "lease_expires_at", ARGV[2])
  end
  redis.call("EXPIRE", KEYS[1], ARGV[3])
end
return 1
`;

const EXPIRE_OWNER_SCRIPT = `
local status = redis.call("HGET", KEYS[1], "status")
if not status then
  return 0
end
local lease = tonumber(redis.call("HGET", KEYS[1], "lease_expires_at") or "0")
if (status == "queued" or status == "running") and lease <= tonumber(ARGV[1]) then
  redis.call("HSET", KEYS[1],
    "status", "failed",
    "error_code", "owner_instance_lost",
    "receipt_json", "")
  ${OWNER_LOST_RECEIPT_LUA}
  redis.call("EXPIRE", KEYS[1], ARGV[2])
end
return 1
`;

const HEARTBEAT_SCRIPT = `
local status = redis.call("HGET", KEYS[1], "status")
local owner = redis.call("HGET", KEYS[1], "owner_instance_id")
if not status or owner ~= ARGV[1] then
  return 0
end
if status == "succeeded" or status == "failed" or status == "cancelled" then
  return 0
end
local lease = tonumber(redis.call("HGET", KEYS[1], "lease_expires_at") or "0")
if lease <= tonumber(ARGV[2]) then
  redis.call("HSET", KEYS[1],
    "status", "failed",
    "error_code", "owner_instance_lost",
    "receipt_json", "")
  ${OWNER_LOST_RECEIPT_LUA}
  redis.call("EXPIRE", KEYS[1], ARGV[4])
  return 0
end
redis.call("HSET", KEYS[1], "lease_expires_at", ARGV[3])
redis.call("EXPIRE", KEYS[1], ARGV[4])
return 1
`;

const CANCEL_SCRIPT = `
local status = redis.call("HGET", KEYS[1], "status")
if not status then
  return {"not_found", ""}
end
if status == "succeeded" or status == "failed" or status == "cancelled" then
  return {"terminal", status}
end
redis.call("HSET", KEYS[1], "cancel_requested", "1")
redis.call("EXPIRE", KEYS[1], ARGV[1])
return {"accepted", redis.call("HGET", KEYS[1], "owner_instance_id")}
`;

const COMPLETE_SCRIPT = `
local status = redis.call("HGET", KEYS[1], "status")
if not status then
  return ""
end
if status == "succeeded" or status == "failed" or status == "cancelled" then
  return status
end
local final_status = ARGV[1]
local cancel_requested = redis.call("HGET", KEYS[1], "cancel_requested")
if cancel_requested == "1" and final_status == "succeeded" then
  final_status = "cancelled"
end
redis.call("HSET", KEYS[1],
  "status", final_status,
  "receipt_json", final_status == ARGV[1] and ARGV[2] or ARGV[5],
  "error_code", final_status == ARGV[1] and ARGV[3] or "")
redis.call("EXPIRE", KEYS[1], ARGV[4])
return final_status
`;

export interface RedisRunStoreOptions {
  url: string;
  ttlSeconds?: number;
  leaseMs?: number;
}

export class RedisRunStore implements RunStore {
  private readonly client: RedisClientType;
  private readonly ttlSeconds: number;
  private readonly leaseMs: number;

  private constructor(
    client: RedisClientType,
    ttlSeconds: number,
    leaseMs: number,
  ) {
    this.client = client;
    this.ttlSeconds = ttlSeconds;
    this.leaseMs = leaseMs;
  }

  static async connect(options: RedisRunStoreOptions): Promise<RedisRunStore> {
    if (!options.url) {
      throw new Error("ai_redis_url_required");
    }
    const client = createClient({ url: options.url });
    client.on("error", () => {
      // Readiness and request handlers surface Redis failures without logging secrets.
    });
    await client.connect();
    return new RedisRunStore(
      client,
      options.ttlSeconds ?? DEFAULT_TTL_SECONDS,
      options.leaseMs ?? RUN_LEASE_MS,
    );
  }

  async isReady(): Promise<boolean> {
    if (!this.client.isReady) {
      return false;
    }
    try {
      return await this.client.ping() === "PONG";
    } catch {
      return false;
    }
  }

  async createOrGet(record: RunRecord): Promise<CreateRunResult> {
    const key = this.key(record.ai_run_id);
    const created = await this.client.eval(CREATE_SCRIPT, {
      keys: [key],
      arguments: [
        record.ai_run_id,
        record.input_hash,
        record.status,
        record.owner_instance_id,
        record.cancel_requested ? "1" : "0",
        String(record.lease_expires_at),
        record.receipt ? JSON.stringify(record.receipt) : "",
        record.error_code ?? "",
        JSON.stringify(record.context),
        String(this.ttlSeconds),
      ],
    });
    const kind = String(created);
    if (!["created", "existing", "conflict"].includes(kind)) {
      throw new Error("invalid_create_response");
    }
    const run = await this.get(record.ai_run_id);
    if (!run) throw new Error("run_not_found_after_create");
    return { kind: kind as CreateRunResult["kind"], run } as CreateRunResult;
  }

  async get(aiRunId: string): Promise<RunRecord | undefined> {
    const key = this.key(aiRunId);
    await this.client.eval(EXPIRE_OWNER_SCRIPT, {
      keys: [key],
      arguments: [
        String(Date.now()),
        String(this.ttlSeconds),
        new Date().toISOString(),
      ],
    });
    const values = await this.client.hGetAll(key);
    if (!values.ai_run_id) {
      return undefined;
    }
    return this.deserialize(values);
  }

  async markRunning(aiRunId: string): Promise<RunRecord | undefined> {
    const key = this.key(aiRunId);
    const exists = await this.client.eval(MARK_RUNNING_SCRIPT, {
      keys: [key],
      arguments: [
        String(Date.now()),
        String(Date.now() + this.leaseMs),
        String(this.ttlSeconds),
        new Date().toISOString(),
      ],
    });
    if (Number(exists) !== 1) {
      return undefined;
    }
    return this.get(aiRunId);
  }

  async heartbeat(aiRunId: string, instanceId: string): Promise<boolean> {
    const result = await this.client.eval(HEARTBEAT_SCRIPT, {
      keys: [this.key(aiRunId)],
      arguments: [
        instanceId,
        String(Date.now()),
        String(Date.now() + this.leaseMs),
        String(this.ttlSeconds),
        new Date().toISOString(),
      ],
    });
    return Number(result) === 1;
  }

  async complete(
    aiRunId: string,
    status: Extract<RunStatus, "succeeded" | "failed" | "cancelled">,
    receipt: AiExecutionReceipt,
    errorCode?: string,
  ): Promise<RunRecord | undefined> {
    const stored = await this.get(aiRunId);
    if (!stored || isTerminalStatus(stored.status)) return stored;
    const cancelledReceipt = terminalReceipt(
      stored.context,
      "cancelled",
      null,
      receipt,
    );
    await this.client.eval(COMPLETE_SCRIPT, {
      keys: [this.key(aiRunId)],
      arguments: [
        status,
        receipt ? JSON.stringify(receipt) : "",
        errorCode ?? "",
        String(this.ttlSeconds),
        JSON.stringify(cancelledReceipt),
      ],
    });
    return this.get(aiRunId);
  }

  async requestCancel(aiRunId: string): Promise<CancelRequestResult> {
    const response = await this.client.eval(CANCEL_SCRIPT, {
      keys: [this.key(aiRunId)],
      arguments: [String(this.ttlSeconds)],
    });
    if (!Array.isArray(response)) {
      throw new Error("invalid_cancel_response");
    }
    const outcome = String(response[0]);
    const value = String(response[1] ?? "");
    if (outcome === "not_found") {
      return { outcome: "not_found" };
    }
    if (outcome === "terminal") {
      return { outcome: "terminal", status: value as RunStatus };
    }
    if (outcome !== "accepted" || !value) {
      throw new Error("invalid_cancel_response");
    }
    await this.client.publish(
      `${CANCEL_CHANNEL_PREFIX}${value}`,
      aiRunId,
    );
    return { outcome: "accepted", owner_instance_id: value };
  }

  async subscribe(
    instanceId: string,
    onCancel: (aiRunId: string) => void,
  ): Promise<() => Promise<void>> {
    const subscriber = this.client.duplicate();
    subscriber.on("error", () => {
      // The app also polls cancel_requested, so a dropped notification is recoverable.
    });
    await subscriber.connect();
    const channel = `${CANCEL_CHANNEL_PREFIX}${instanceId}`;
    await subscriber.subscribe(channel, onCancel);
    return async () => {
      if (subscriber.isOpen) {
        await subscriber.unsubscribe(channel);
        await subscriber.quit();
      }
    };
  }

  async close(): Promise<void> {
    if (this.client.isOpen) {
      await this.client.quit();
    }
  }

  private key(aiRunId: string): string {
    return `${KEY_PREFIX}${aiRunId}`;
  }

  private deserialize(values: Record<string, string>): RunRecord {
    return {
      ai_run_id: values.ai_run_id,
      input_hash: values.input_hash,
      status: values.status as RunStatus,
      owner_instance_id: values.owner_instance_id,
      cancel_requested: values.cancel_requested === "1",
      lease_expires_at: Number(values.lease_expires_at),
      context: JSON.parse(values.context_json),
      receipt: values.receipt_json
        ? JSON.parse(values.receipt_json) as AiExecutionReceipt
        : undefined,
      error_code: values.error_code || undefined,
    };
  }
}
