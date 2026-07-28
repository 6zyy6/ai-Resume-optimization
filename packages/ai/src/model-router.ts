import { Type, type Static } from "typebox";
import { Value } from "typebox/value";

import {
  WORKFLOW_TYPES,
  type WorkflowRoute,
  type WorkflowType,
} from "./contracts.js";

const ProviderModelRouteSchema = Type.Object(
  {
    provider: Type.String({ minLength: 1, maxLength: 128 }),
    model: Type.String({ minLength: 1, maxLength: 256 }),
    approved_data_policy: Type.Boolean(),
  },
  { additionalProperties: false },
);

const WorkflowRouteSchema = Type.Object(
  {
    primary: ProviderModelRouteSchema,
    fallback: ProviderModelRouteSchema,
    max_tokens: Type.Integer({ minimum: 1, maximum: 12_000 }),
    thinking: Type.Union([
      Type.Literal("off"),
      Type.Literal("minimal"),
      Type.Literal("low"),
      Type.Literal("medium"),
      Type.Literal("high"),
      Type.Literal("xhigh"),
      Type.Literal("max"),
    ]),
    timeout_ms: Type.Integer({ minimum: 1, maximum: 30_000 }),
    retry_count: Type.Integer({ minimum: 0, maximum: 1 }),
    max_cost_usd: Type.Number({ exclusiveMinimum: 0 }),
  },
  { additionalProperties: false },
);

const RoutesSchema = Type.Partial(
  Type.Object(
    Object.fromEntries(
      WORKFLOW_TYPES.map((workflowType) => [
        workflowType,
        WorkflowRouteSchema,
      ]),
    ) as Record<WorkflowType, typeof WorkflowRouteSchema>,
    { additionalProperties: false },
  ),
);

const PROVIDER_KEY_ENV: Record<string, string[]> = {
  openai: ["OPENAI_API_KEY"],
  anthropic: ["ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_TOKEN"],
  google: ["GEMINI_API_KEY"],
};

export interface ModelRouter {
  getRoute(workflowType: WorkflowType): WorkflowRoute | undefined;
  getModel(
    workflowType: WorkflowType,
    attempt: 0 | 1,
  ): WorkflowRoute["primary"] | undefined;
  isReady(env?: NodeJS.ProcessEnv): boolean;
  routes: Partial<Record<WorkflowType, WorkflowRoute>>;
}

export function createModelRouter({
  routes,
}: {
  routes: Partial<Record<WorkflowType, WorkflowRoute>>;
}): ModelRouter {
  if (!Value.Check(RoutesSchema, routes)) {
    throw new Error("invalid_model_routes");
  }

  return {
    routes,
    getRoute(workflowType) {
      return routes[workflowType];
    },
    getModel(workflowType, attempt) {
      const route = routes[workflowType];
      return route?.[attempt === 0 ? "primary" : "fallback"];
    },
    isReady(env = process.env) {
      return WORKFLOW_TYPES.every((workflowType) => {
        const route = routes[workflowType];
        if (
          !route?.primary.approved_data_policy ||
          !route.fallback.approved_data_policy
        ) {
          return false;
        }
        return [route.primary, route.fallback].every(({ provider }) => {
          const keys = PROVIDER_KEY_ENV[provider];
          return Boolean(keys?.some((key) => env[key]));
        });
      });
    },
  };
}

export function createModelRouterFromEnv(
  env: NodeJS.ProcessEnv = process.env,
): ModelRouter {
  const rawRoutes = env.AI_MODEL_ROUTES_JSON;
  if (!rawRoutes) {
    return createModelRouter({ routes: {} });
  }
  let routes: unknown;
  try {
    routes = JSON.parse(rawRoutes);
  } catch {
    throw new Error("invalid_model_routes_json");
  }
  if (!Value.Check(RoutesSchema, routes)) {
    throw new Error("invalid_model_routes");
  }
  return createModelRouter({
    routes: routes as Static<typeof RoutesSchema>,
  });
}
