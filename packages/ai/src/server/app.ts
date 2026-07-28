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
  type WorkflowRun,
} from "../contracts.js";
import type { ModelRouter } from "../model-router.js";
import { runWorkflow } from "../workflows/run-workflow.js";

const BODY_LIMIT = 512 * 1024;

interface StoredRun {
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  controller: AbortController;
  result?: WorkflowRun;
  error_code?: string;
}

interface BuildAppOptions {
  mode: "fixture" | "production";
  runtime: PiRuntime;
  modelRouter: ModelRouter;
  serviceToken?: string;
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
  const runs = new Map<string, StoredRun>();

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

  app.get("/internal/v1/health/ready", async (_request, reply) => {
    const ready =
      options.mode === "fixture" ||
      (Boolean(options.serviceToken) && options.modelRouter.isReady());
    return reply.status(ready ? 200 : 503).send({
      request_id: requestId(),
      status: ready ? "ready" : "not_ready",
    });
  });

  app.post<{ Body: WorkflowInput }>(
    "/internal/v1/runs",
    {
      preHandler: requireAuth,
      schema: { body: WorkflowInputSchema },
    },
    async (request, reply) => {
      const aiRunId = `run_${randomUUID()}`;
      const stored: StoredRun = {
        status: "queued",
        controller: new AbortController(),
      };
      runs.set(aiRunId, stored);
      void Promise.resolve().then(async () => {
        stored.status = "running";
        try {
          const result = await runWorkflow(request.body, options.runtime, {
            signal: stored.controller.signal,
            aiRunId,
          });
          stored.result = result;
          stored.status = result.status;
        } catch (error) {
          stored.status = stored.controller.signal.aborted
            ? "cancelled"
            : "failed";
          stored.error_code = safeErrorCode(error);
        }
      });
      return reply.status(202).send({
        request_id: requestId(),
        ai_run_id: aiRunId,
        status: "queued",
      });
    },
  );

  const RunParamsSchema = Type.Object(
    {
      ai_run_id: Type.String({ minLength: 1, maxLength: 128 }),
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
      const stored = runs.get(request.params.ai_run_id);
      if (!stored) {
        return reply.status(404).send({
          request_id: requestId(),
          error: { code: "run_not_found" },
        });
      }
      return {
        request_id: requestId(),
        run: stored.result ?? {
          ai_run_id: request.params.ai_run_id,
          status: stored.status,
          error_code: stored.error_code,
        },
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
      const stored = runs.get(request.params.ai_run_id);
      if (!stored) {
        return reply.status(404).send({
          request_id: requestId(),
          error: { code: "run_not_found" },
        });
      }
      stored.controller.abort();
      if (stored.status === "queued") {
        stored.status = "cancelled";
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
