import { Type, type Static, type TSchema } from "typebox";

export const WORKFLOW_TYPES = [
  "extract_facts",
  "next_question",
  "write_experience_bullet",
  "parse_jd",
  "match_resume_to_jd",
  "generate_suggestion",
  "fact_check",
  "style_check",
] as const;

export type WorkflowType = (typeof WORKFLOW_TYPES)[number];

export const WorkflowTypeSchema = Type.Union([
  Type.Literal("extract_facts"),
  Type.Literal("next_question"),
  Type.Literal("write_experience_bullet"),
  Type.Literal("parse_jd"),
  Type.Literal("match_resume_to_jd"),
  Type.Literal("generate_suggestion"),
  Type.Literal("fact_check"),
  Type.Literal("style_check"),
]);

export const ConfirmedFactSchema = Type.Object(
  {
    id: Type.String({ minLength: 1, maxLength: 128 }),
    kind: Type.String({ minLength: 1, maxLength: 64 }),
    value: Type.String({ minLength: 1, maxLength: 20_000 }),
    status: Type.Literal("confirmed"),
  },
  { additionalProperties: false },
);

export type ConfirmedFact = Static<typeof ConfirmedFactSchema>;

export const JdRequirementSchema = Type.Object(
  {
    id: Type.String({ minLength: 1, maxLength: 128 }),
    category: Type.String({ minLength: 1, maxLength: 64 }),
    value: Type.String({ minLength: 1, maxLength: 20_000 }),
  },
  { additionalProperties: false },
);

export type JdRequirement = Static<typeof JdRequirementSchema>;

export const WorkflowInputSchema = Type.Object(
  {
    workflow_type: WorkflowTypeSchema,
    workflow_version: Type.String({ minLength: 1, maxLength: 32 }),
    trace_id: Type.String({ minLength: 1, maxLength: 128 }),
    task_id: Type.String({ minLength: 1, maxLength: 128 }),
    locale: Type.String({ minLength: 2, maxLength: 32 }),
    target: Type.String({ minLength: 1, maxLength: 64 }),
    confirmed_facts: Type.Array(ConfirmedFactSchema, { maxItems: 1_000 }),
    jd_requirements: Type.Array(JdRequirementSchema, { maxItems: 1_000 }),
    current_object: Type.Record(Type.String(), Type.Unknown()),
  },
  { additionalProperties: false },
);

export type WorkflowInput = Static<typeof WorkflowInputSchema>;

export const AtomicClaimSchema = Type.Object(
  {
    text: Type.String({ minLength: 1, maxLength: 20_000 }),
    fact_refs: Type.Array(Type.String({ minLength: 1, maxLength: 128 }), {
      maxItems: 1_000,
    }),
    status: Type.Union([
      Type.Literal("supported"),
      Type.Literal("needs_confirmation"),
      Type.Literal("unsupported"),
    ]),
  },
  { additionalProperties: false },
);

export const FactCheckOutputSchema = Type.Object(
  {
    claims: Type.Array(AtomicClaimSchema, { maxItems: 1_000 }),
    exportable: Type.Boolean(),
    risk_flags: Type.Array(Type.String({ minLength: 1, maxLength: 128 }), {
      maxItems: 100,
    }),
  },
  { additionalProperties: false },
);

export type FactCheckOutput = Static<typeof FactCheckOutputSchema>;

export const SuggestionOutputSchema = Type.Object(
  {
    suggestion_text: Type.String({ minLength: 1, maxLength: 20_000 }),
    atomic_claims: Type.Array(AtomicClaimSchema, { maxItems: 1_000 }),
    jd_requirement_refs: Type.Array(
      Type.String({ minLength: 1, maxLength: 128 }),
      { maxItems: 1_000 },
    ),
    reason: Type.String({ minLength: 1, maxLength: 4_000 }),
    risk_flags: Type.Array(Type.String({ minLength: 1, maxLength: 128 }), {
      maxItems: 100,
    }),
    requires_user_confirmation: Type.Boolean(),
    exportable: Type.Boolean(),
  },
  { additionalProperties: false },
);

export type SuggestionOutput = Static<typeof SuggestionOutputSchema>;

const ExtractFactsOutputSchema = Type.Object(
  {
    facts: Type.Array(
      Type.Object(
        {
          id: Type.String({ minLength: 1, maxLength: 128 }),
          kind: Type.String({ minLength: 1, maxLength: 64 }),
          value: Type.String({ minLength: 1, maxLength: 20_000 }),
          source_refs: Type.Array(
            Type.String({ minLength: 1, maxLength: 128 }),
            { minItems: 1, maxItems: 1_000 },
          ),
        },
        { additionalProperties: false },
      ),
      { maxItems: 1_000 },
    ),
  },
  { additionalProperties: false },
);

export const QuestionSchema = Type.Object(
  {
    question_id: Type.String({ minLength: 1, maxLength: 128 }),
    text: Type.String({ minLength: 1, maxLength: 4_000 }),
    fact_refs: Type.Array(Type.String({ minLength: 1, maxLength: 128 }), {
      maxItems: 1_000,
    }),
  },
  { additionalProperties: false },
);

const NextQuestionOutputSchema = Type.Object(
  { question: QuestionSchema },
  { additionalProperties: false },
);

const ParseJdOutputSchema = Type.Object(
  {
    requirements: Type.Array(
      Type.Object(
        {
          id: Type.String({ minLength: 1, maxLength: 128 }),
          category: Type.Union([
            Type.Literal("responsibility"),
            Type.Literal("must_have"),
            Type.Literal("nice_to_have"),
            Type.Literal("implicit_capability"),
          ]),
          value: Type.String({ minLength: 1, maxLength: 20_000 }),
        },
        { additionalProperties: false },
      ),
      { maxItems: 1_000 },
    ),
  },
  { additionalProperties: false },
);

const MatchOutputSchema = Type.Object(
  {
    matches: Type.Array(
      Type.Object(
        {
          category: Type.Union([
            Type.Literal("direct"),
            Type.Literal("transferable"),
            Type.Literal("gap"),
            Type.Literal("needs_evidence"),
          ]),
          fact_refs: Type.Array(
            Type.String({ minLength: 1, maxLength: 128 }),
            { maxItems: 1_000 },
          ),
          requirement_refs: Type.Array(
            Type.String({ minLength: 1, maxLength: 128 }),
            { maxItems: 1_000 },
          ),
        },
        { additionalProperties: false },
      ),
      { maxItems: 1_000 },
    ),
  },
  { additionalProperties: false },
);

const StyleCheckOutputSchema = Type.Object(
  {
    issues: Type.Array(
      Type.Object(
        {
          code: Type.String({ minLength: 1, maxLength: 128 }),
          severity: Type.Union([
            Type.Literal("info"),
            Type.Literal("warning"),
            Type.Literal("error"),
          ]),
          schema_path: Type.String({ minLength: 1, maxLength: 512 }),
        },
        { additionalProperties: false },
      ),
      { maxItems: 1_000 },
    ),
    passed: Type.Boolean(),
  },
  { additionalProperties: false },
);

export const WORKFLOW_OUTPUT_SCHEMAS: Record<WorkflowType, TSchema> = {
  extract_facts: ExtractFactsOutputSchema,
  next_question: NextQuestionOutputSchema,
  write_experience_bullet: SuggestionOutputSchema,
  parse_jd: ParseJdOutputSchema,
  match_resume_to_jd: MatchOutputSchema,
  generate_suggestion: SuggestionOutputSchema,
  fact_check: FactCheckOutputSchema,
  style_check: StyleCheckOutputSchema,
};

export interface ProviderModelRoute {
  provider: string;
  model: string;
  approved_data_policy: boolean;
}

export interface WorkflowRoute {
  primary: ProviderModelRoute;
  fallback: ProviderModelRoute;
  max_tokens: number;
  thinking: "off" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max";
  timeout_ms: number;
  retry_count: number;
  max_cost_usd: number;
}

export interface RuntimeCallInput {
  input: WorkflowInput;
  attempt: number;
  route_attempt: 0 | 1;
  phase: "initial" | "retry" | "correction" | "fallback";
  schema_feedback?: SchemaFeedback[];
  signal: AbortSignal;
  budget: ResourceBudget;
}

export interface SchemaFeedback {
  path: string;
  type: string;
}

export interface RuntimeSuccess {
  status: "success";
  output: unknown;
  events: Iterable<Record<string, unknown>>;
  max_cost_usd?: number;
  budget_accounted?: boolean;
}

export interface RuntimeFailure {
  status: "failure";
  failure_kind: "provider" | "json" | "timeout" | "budget" | "route";
  error_code: string;
  events: Iterable<Record<string, unknown>>;
  max_cost_usd?: number;
  budget_accounted?: boolean;
}

export type RuntimeResult = RuntimeSuccess | RuntimeFailure;

export interface ResourceBudget {
  preflightAttempt(): void;
  setCostLimit(maxCostUsd: number): void;
  reserveTurn(): void;
  reserveTool(): void;
  preflightProvider(
    model: ModelBudgetRates,
    inputText: string,
    maxOutputTokens: number,
    maxCostUsd: number,
  ): void;
  recordPiEvent(event: Record<string, unknown>): void;
  snapshot(): {
    turns: number;
    tools: number;
    total_tokens: number;
    cost_usd: number;
  };
}

export interface ModelBudgetRates {
  cost: {
    input: number;
    output: number;
    cacheRead: number;
    cacheWrite: number;
    tiers?: Array<{
      inputTokensAbove: number;
      input: number;
      output: number;
      cacheRead: number;
      cacheWrite: number;
    }>;
  };
}

export type RuntimeCall = (
  call: RuntimeCallInput,
) => Promise<RuntimeResult>;

export interface PiRuntime {
  mode: "fixture" | "production";
  runStructured: RuntimeCall;
  runAgent: RuntimeCall;
  getRetryCount?(input: WorkflowInput): number;
  isReady?(): Promise<boolean>;
}

export interface TraceUsage {
  input: number;
  output: number;
  cache_read: number;
  cache_write: number;
  reasoning: number;
  total_tokens: number;
  cost_usd: number;
}

export interface TraceEvent {
  ai_run_id: string;
  trace_id: string;
  task_id: string;
  event_seq: number;
  event_type: string;
  occurred_at: string;
  details?: Record<string, unknown>;
}

export interface WorkflowRun {
  ai_run_id: string;
  trace_id: string;
  task_id: string;
  workflow_type: WorkflowType;
  workflow_version: string;
  status: "succeeded" | "failed" | "cancelled";
  output?: unknown;
  usage: TraceUsage;
  events: TraceEvent[];
  turn_count: number;
  tool_call_count: number;
  fallback_count: number;
  exportable: boolean;
  risk_flags: string[];
}
