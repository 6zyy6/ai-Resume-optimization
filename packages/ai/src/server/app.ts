import { createHash, randomUUID, timingSafeEqual } from "node:crypto";

import Fastify, {
  type FastifyInstance,
  type FastifyReply,
  type FastifyRequest,
} from "fastify";
import { Type } from "typebox";

import {
  WorkflowInputSchema,
  type PiRuntime,
  type WorkflowInput,
} from "../contracts.js";
import type { ModelRouter } from "../model-router.js";
import { runWorkflow } from "../workflows/run-workflow.js";
import { MemoryRunStore } from "./memory-run-store.js";
import {
  isTerminalStatus,
  RUN_LEASE_MS,
  terminalReceipt,
  type RunStore,
} from "./run-store.js";

const BODY_LIMIT = 512 * 1024;
const CANCEL_POLL_INTERVAL_MS = 250;
const RUN_HEARTBEAT_INTERVAL_MS = 5_000;
const AiRunIdSchema = Type.String({ minLength: 1, maxLength: 128 });
const RunRequestSchema = Type.Object(
  {
    ai_run_id: AiRunIdSchema,
    input: WorkflowInputSchema,
  },
  { additionalProperties: false },
);

interface BuildAppOptions {
  mode: "fixture" | "production";
  runtime: PiRuntime;
  modelRouter: ModelRouter;
  serviceToken?: string;
  runStore?: RunStore;
  instanceId?: string;
}

function requestId(): string {
  return `req_${randomUUID()}`;
}

function digest(value: string): Buffer {
  return createHash("sha256").update(value).digest();
}

function hasValidBearerToken(
  authorization: string | undefined,
  expectedToken: string | undefined,
): boolean {
  if (!expectedToken || !authorization?.startsWith("Bearer ")) {
    return false;
  }
  return timingSafeEqual(
    digest(authorization.slice("Bearer ".length)),
    digest(expectedToken),
  );
}

function safeErrorCode(error: unknown): string {
  if (
    typeof error === "object" &&
    error &&
    "code" in error &&
    typeof error.code === "string"
  ) {
    return error.code.replace(/[^a-z0-9_.-]/gi, "_").slice(0, 128);
  }
  return "runtime_failed";
}

export function buildApp(options: BuildAppOptions): FastifyInstance {
  const app = Fastify({
    bodyLimit: BODY_LIMIT,
    logger: false,
  });
  const runStore = options.runStore ?? new MemoryRunStore();
  const instanceId = options.instanceId ?? `pi_${randomUUID()}`;
  const controllers = new Map<string, AbortController>();
  let unsubscribe: (() => Promise<void>) | undefined;
  let checkingCancellations = false;

  const abortLocalRun = (aiRunId: string) => {
    controllers.get(aiRunId)?.abort();
  };
  const runSummary = (run: { ai_run_id: string; status: string; error_code?: string }) => ({
    ai_run_id: run.ai_run_id,
    status: run.status,
    error_code: run.error_code ?? null,
  });

  const checkCancellationRequests = async () => {
    if (checkingCancellations || controllers.size === 0) {
      return;
    }
    checkingCancellations = true;
    try {
      await Promise.all(
        [...controllers.entries()].map(async ([aiRunId, controller]) => {
          const stored = await runStore.get(aiRunId);
          if (stored?.cancel_requested) {
            controller.abort();
          } else if (stored && isTerminalStatus(stored.status)) {
            controller.abort();
          }
        }),
      );
    } catch {
      // A transient store failure is surfaced by readiness. Local timeouts still apply.
    } finally {
      checkingCancellations = false;
    }
  };
  const cancellationTimer = setInterval(() => {
    void checkCancellationRequests();
  }, CANCEL_POLL_INTERVAL_MS);
  cancellationTimer.unref();
  const heartbeatTimer = setInterval(() => {
    void Promise.all(
      [...controllers.keys()].map((aiRunId) =>
        runStore.heartbeat(aiRunId, instanceId)
      ),
    ).catch(() => {
      // Readiness reports store failures; the owner lease then fails closed.
    });
  }, RUN_HEARTBEAT_INTERVAL_MS);
  heartbeatTimer.unref();

  app.addHook("onReady", async () => {
    unsubscribe = await runStore.subscribe(instanceId, abortLocalRun);
  });

  app.addHook("onClose", async () => {
    clearInterval(cancellationTimer);
    clearInterval(heartbeatTimer);
    for (const controller of controllers.values()) {
      controller.abort();
    }
    await unsubscribe?.();
  });

  app.setErrorHandler((error, _request, reply) => {
    const candidateStatus =
      typeof error === "object" &&
      error &&
      "statusCode" in error &&
      typeof error.statusCode === "number"
        ? error.statusCode
        : 400;
    const statusCode = candidateStatus === 413 ? 413 : candidateStatus;
    void reply.status(statusCode).send({
      request_id: requestId(),
      error: {
        code:
          statusCode === 413
            ? "request_body_too_large"
            : "request_validation_failed",
      },
    });
  });

  const requireAuth = (
    request: FastifyRequest,
    reply: FastifyReply,
    done: () => void,
  ) => {
    if (
      hasValidBearerToken(
        request.headers.authorization,
        options.serviceToken,
      )
    ) {
      done();
      return;
    }
    void reply.status(401).send({
      request_id: requestId(),
      error: { code: "unauthorized" },
    });
  };

  app.get("/internal/v1/health/live", async () => ({
    request_id: requestId(),
    status: "live",
  }));

  app.get("/internal/v1/version", async () => ({
    commit_sha: process.env.APP_COMMIT_SHA ?? "development",
    service: "ai",
    runtime_mode: options.mode,
  }));

  app.get("/internal/v1/health/ready", async (_request, reply) => {
    const ready =
      options.mode === "fixture" ||
      (Boolean(options.serviceToken) &&
        options.modelRouter.isReady() &&
        await options.runtime.isReady?.() === true &&
        await runStore.isReady());
    return reply.status(ready ? 200 : 503).send({
      request_id: requestId(),
      status: ready ? "ready" : "not_ready",
      runtime_mode: options.mode,
    });
  });

  app.post<{ Body: { ai_run_id: string; input: WorkflowInput } }>(
    "/internal/v1/runs",
    {
      preHandler: requireAuth,
      schema: { body: RunRequestSchema },
    },
    async (request, reply) => {
      const aiRunId = request.body.ai_run_id;
      const startedAt = new Date().toISOString();
      const context = {
        ai_run_id: aiRunId,
        workflow_type: request.body.input.workflow_type,
        workflow_version: request.body.input.workflow_version,
        prompt_template_version: request.body.input.prompt_template_version,
        trace_id: request.body.input.trace_id,
        task_id: request.body.input.task_id,
        input_hash: request.body.input.input_hash,
        started_at: startedAt,
      };
      const controller = new AbortController();
      let createResult;
      try {
        createResult = await runStore.createOrGet({
          ai_run_id: aiRunId,
          input_hash: request.body.input.input_hash,
          status: "queued",
          owner_instance_id: instanceId,
          cancel_requested: false,
          lease_expires_at: Date.now() + RUN_LEASE_MS,
          context,
        });
      } catch {
        return reply.status(503).send({
          request_id: requestId(),
          error: { code: "run_store_unavailable" },
        });
      }
      if (createResult.kind === "conflict") {
        return reply.status(409).send({
          request_id: requestId(),
          error: { code: "AI_RUN_ID_REUSED" },
        });
      }
      if (createResult.kind === "existing") {
        return reply.status(202).send({
          request_id: requestId(),
          ...(isTerminalStatus(createResult.run.status)
            ? { receipt: createResult.run.receipt }
            : { run: runSummary(createResult.run) }),
        });
      }
      controllers.set(aiRunId, controller);
      void Promise.resolve().then(async () => {
        try {
          const stored = await runStore.markRunning(aiRunId);
          if (!stored) {
            controller.abort();
            return;
          }
          if (stored.cancel_requested) {
            controller.abort();
          }
          const receipt = await runWorkflow(request.body.input, options.runtime, {
            signal: controller.signal,
            aiRunId,
          });
          await runStore.complete(
            aiRunId,
            receipt.run.status,
            receipt,
            receipt.run.error_code ?? undefined,
          );
        } catch (error) {
          try {
            await runStore.complete(
              aiRunId,
              controller.signal.aborted ? "cancelled" : "failed",
              terminalReceipt(
                context,
                controller.signal.aborted ? "cancelled" : "failed",
                controller.signal.aborted ? null : safeErrorCode(error),
              ),
              safeErrorCode(error),
            );
          } catch {
            controller.abort();
          }
        } finally {
          controllers.delete(aiRunId);
        }
      });
      return reply.status(202).send({
        request_id: requestId(),
        run: runSummary({ ai_run_id: aiRunId, status: "queued" }),
      });
    },
  );

  const RunParamsSchema = Type.Object(
    {
      ai_run_id: AiRunIdSchema,
    },
    { additionalProperties: false },
  );

  app.get<{ Params: { ai_run_id: string } }>(
    "/internal/v1/runs/:ai_run_id",
    {
      preHandler: requireAuth,
      schema: { params: RunParamsSchema },
    },
    async (request, reply) => {
      let stored;
      try {
        stored = await runStore.get(request.params.ai_run_id);
      } catch {
        return reply.status(503).send({
          request_id: requestId(),
          error: { code: "run_store_unavailable" },
        });
      }
      if (!stored) {
        return reply.status(404).send({
          request_id: requestId(),
          error: { code: "run_not_found" },
        });
      }
      if (isTerminalStatus(stored.status)) {
        return {
          request_id: requestId(),
          receipt: stored.receipt,
        };
      }
      return {
        request_id: requestId(),
        run: runSummary(stored),
      };
    },
  );

  app.post<{ Params: { ai_run_id: string } }>(
    "/internal/v1/runs/:ai_run_id/cancel",
    {
      preHandler: requireAuth,
      schema: { params: RunParamsSchema },
    },
    async (request, reply) => {
      let outcome;
      try {
        outcome = await runStore.requestCancel(request.params.ai_run_id);
      } catch {
        return reply.status(503).send({
          request_id: requestId(),
          error: { code: "run_store_unavailable" },
        });
      }
      if (outcome.outcome === "not_found") {
        return reply.status(404).send({
          request_id: requestId(),
          error: { code: "run_not_found" },
        });
      }
      if (
        outcome.outcome === "terminal" &&
        isTerminalStatus(outcome.status)
      ) {
        return reply.status(409).send({
          request_id: requestId(),
          error: { code: "run_already_terminal" },
        });
      }
      return reply.status(202).send({
        request_id: requestId(),
        ai_run_id: request.params.ai_run_id,
        status: "cancelling",
      });
    },
  );

  return app;
}
