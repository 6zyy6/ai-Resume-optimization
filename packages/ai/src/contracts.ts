import { Type, type Static, type TSchema } from "typebox";

export const MODEL_WORKFLOW_TYPES = [
  "analyze_intake_answer",
  "compose_resume_draft",
  "parse_jd",
  "match_resume_to_jd",
  "generate_suggestions_batch",
] as const;

export type WorkflowType = (typeof MODEL_WORKFLOW_TYPES)[number];

export const WorkflowTypeSchema = Type.Union(
  MODEL_WORKFLOW_TYPES.map((workflowType) => Type.Literal(workflowType)),
);

const IdSchema = Type.String({ minLength: 1, maxLength: 128 });
const TextSchema = Type.String({ minLength: 1, maxLength: 20_000 });
const HashSchema = Type.String({
  minLength: 64,
  maxLength: 64,
  pattern: "^[a-f0-9]{64}$",
});
const StringListSchema = Type.Array(IdSchema, { maxItems: 1_000 });

export const ConfirmedFactSchema = Type.Object(
  {
    id: IdSchema,
    kind: Type.String({ minLength: 1, maxLength: 64 }),
    value: TextSchema,
    status: Type.Literal("confirmed"),
  },
  { additionalProperties: false },
);
export type ConfirmedFact = Static<typeof ConfirmedFactSchema>;

const FactProjectionSchema = Type.Object(
  {
    id: IdSchema,
    kind: Type.String({ minLength: 1, maxLength: 64 }),
    value: TextSchema,
  },
  { additionalProperties: false },
);

export const JdRequirementSchema = Type.Object(
  {
    id: IdSchema,
    category: Type.Union([
      Type.Literal("responsibility"),
      Type.Literal("must_have"),
      Type.Literal("nice_to_have"),
      Type.Literal("implicit_capability"),
    ]),
    value: TextSchema,
  },
  { additionalProperties: false },
);
export type JdRequirement = Static<typeof JdRequirementSchema>;

const SourceRangeSchema = Type.Object(
  {
    start: Type.Integer({ minimum: 0 }),
    end: Type.Integer({ minimum: 0 }),
  },
  { additionalProperties: false },
);

const CommonEnvelope = {
  workflow_version: Type.Literal("2"),
  prompt_template_version: Type.String({ minLength: 1, maxLength: 128 }),
  trace_id: IdSchema,
  task_id: IdSchema,
  owner_scope_hash: HashSchema,
  locale: Type.Literal("zh-CN"),
  input_version: Type.Integer({ minimum: 1 }),
  input_hash: HashSchema,
};

export const AnalyzeIntakePayloadSchema = Type.Object(
  {
    session_id_hash: HashSchema,
    answer_id: IdSchema,
    question_id: IdSchema,
    question_reason: Type.String({ minLength: 1, maxLength: 4_000 }),
    answer_text: TextSchema,
    answer_state: Type.String({ minLength: 1, maxLength: 64 }),
    confirmed_facts: Type.Array(FactProjectionSchema, { maxItems: 1_000 }),
    covered_slots: StringListSchema,
    missing_slots: StringListSchema,
    asked_question_ids: StringListSchema,
  },
  { additionalProperties: false },
);

const ComposeFactSchema = Type.Object(
  {
    id: IdSchema,
    kind: Type.String({ minLength: 1, maxLength: 64 }),
    value: TextSchema,
    source_hashes: Type.Array(HashSchema, { minItems: 1, maxItems: 1_000 }),
  },
  { additionalProperties: false },
);

export const ComposeResumeDraftPayloadSchema = Type.Object(
  {
    resume_title: Type.String({ minLength: 1, maxLength: 512 }),
    experience_groups: Type.Array(
      Type.Object(
        {
          title: Type.String({ minLength: 1, maxLength: 512 }),
          fact_refs: StringListSchema,
        },
        { additionalProperties: false },
      ),
      { maxItems: 1_000 },
    ),
    confirmed_facts: Type.Array(ComposeFactSchema, { maxItems: 1_000 }),
    allowed_section_types: Type.Array(
      Type.String({ minLength: 1, maxLength: 64 }),
      { minItems: 1, maxItems: 64 },
    ),
  },
  { additionalProperties: false },
);

const RequirementCategorySchema = Type.Union([
  Type.Literal("responsibility"),
  Type.Literal("must_have"),
  Type.Literal("nice_to_have"),
  Type.Literal("implicit_capability"),
]);

export const ParseJdPayloadSchema = Type.Object(
  {
    jd_text: TextSchema,
    job_title: Type.Optional(Type.String({ minLength: 1, maxLength: 512 })),
    allowed_categories: Type.Array(RequirementCategorySchema, {
      minItems: 1,
      maxItems: 4,
    }),
  },
  { additionalProperties: false },
);

export const MatchResumeToJdPayloadSchema = Type.Object(
  {
    resume_version_id: IdSchema,
    resume_snapshot_hash: HashSchema,
    confirmed_facts: Type.Array(FactProjectionSchema, { maxItems: 1_000 }),
    confirmed_requirements: Type.Array(JdRequirementSchema, { maxItems: 1_000 }),
  },
  { additionalProperties: false },
);

const SuggestionSourceSchema = Type.Object(
  {
    requirement_ref: IdSchema,
    category: Type.Union([
      Type.Literal("transferable"),
      Type.Literal("needs_evidence"),
    ]),
    fact_refs: StringListSchema,
    target_path: Type.String({ minLength: 1, maxLength: 512 }),
    original_hash: HashSchema,
    original_text: TextSchema,
  },
  { additionalProperties: false },
);

export const GenerateSuggestionsBatchPayloadSchema = Type.Object(
  {
    matches: Type.Array(SuggestionSourceSchema, { minItems: 1, maxItems: 1_000 }),
    confirmed_facts: Type.Array(FactProjectionSchema, { maxItems: 1_000 }),
    confirmed_requirements: Type.Array(JdRequirementSchema, { maxItems: 1_000 }),
  },
  { additionalProperties: false },
);

export const WorkflowInputSchema = Type.Union([
  Type.Object(
    {
      workflow_type: Type.Literal("analyze_intake_answer"),
      ...CommonEnvelope,
      payload: AnalyzeIntakePayloadSchema,
    },
    { additionalProperties: false },
  ),
  Type.Object(
    {
      workflow_type: Type.Literal("compose_resume_draft"),
      ...CommonEnvelope,
      payload: ComposeResumeDraftPayloadSchema,
    },
    { additionalProperties: false },
  ),
  Type.Object(
    {
      workflow_type: Type.Literal("parse_jd"),
      ...CommonEnvelope,
      payload: ParseJdPayloadSchema,
    },
    { additionalProperties: false },
  ),
  Type.Object(
    {
      workflow_type: Type.Literal("match_resume_to_jd"),
      ...CommonEnvelope,
      payload: MatchResumeToJdPayloadSchema,
    },
    { additionalProperties: false },
  ),
  Type.Object(
    {
      workflow_type: Type.Literal("generate_suggestions_batch"),
      ...CommonEnvelope,
      payload: GenerateSuggestionsBatchPayloadSchema,
    },
    { additionalProperties: false },
  ),
]);
export type WorkflowInput = Static<typeof WorkflowInputSchema>;

export const AtomicClaimSchema = Type.Object(
  {
    text: TextSchema,
    fact_refs: StringListSchema,
    claim_order: Type.Integer({ minimum: 0 }),
  },
  { additionalProperties: false },
);

export const QuestionSchema = Type.Object(
  {
    question_id: IdSchema,
    text: Type.String({ minLength: 1, maxLength: 4_000 }),
    fact_refs: StringListSchema,
  },
  { additionalProperties: false },
);

export interface FactCheckOutput {
  claims: Array<{
    text: string;
    fact_refs: string[];
    status: "supported" | "needs_confirmation" | "unsupported";
  }>;
  exportable: boolean;
  risk_flags: string[];
}

const AnalyzeIntakeAnswerOutputSchema = Type.Object(
  {
    fact_candidates: Type.Array(
      Type.Object(
        {
          kind: Type.String({ minLength: 1, maxLength: 64 }),
          value: TextSchema,
          source_answer_id: IdSchema,
          source_range: SourceRangeSchema,
          risk_flags: Type.Array(IdSchema, { maxItems: 100 }),
        },
        { additionalProperties: false },
      ),
      { maxItems: 1_000 },
    ),
    missing_slots: StringListSchema,
    question_candidate: Type.Union([
      Type.Object(
        {
          reason: Type.String({ minLength: 1, maxLength: 4_000 }),
          slot: Type.String({ minLength: 1, maxLength: 128 }),
          text: Type.String({ minLength: 1, maxLength: 4_000 }),
          related_fact_refs: StringListSchema,
        },
        { additionalProperties: false },
      ),
      Type.Null(),
    ]),
  },
  { additionalProperties: false },
);

const ComposeResumeDraftOutputSchema = Type.Object(
  {
    sections: Type.Array(
      Type.Object(
        {
          type: Type.String({ minLength: 1, maxLength: 64 }),
          title: Type.String({ minLength: 1, maxLength: 512 }),
          bullets: Type.Array(
            Type.Object(
              {
                text: TextSchema,
                atomic_claims: Type.Array(AtomicClaimSchema, { maxItems: 1_000 }),
                risk_flags: Type.Array(IdSchema, { maxItems: 100 }),
              },
              { additionalProperties: false },
            ),
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

const ParseJdOutputSchema = Type.Object(
  {
    requirements: Type.Array(
      Type.Object(
        {
          category: RequirementCategorySchema,
          priority: Type.Union([Type.Literal(1), Type.Literal(2), Type.Literal(3)]),
          value: TextSchema,
          source_range: SourceRangeSchema,
          explicitness: Type.Union([Type.Literal("explicit"), Type.Literal("implicit")]),
          confidence_band: Type.Union([
            Type.Literal("high"),
            Type.Literal("medium"),
            Type.Literal("low"),
          ]),
        },
        { additionalProperties: false },
      ),
      { maxItems: 1_000 },
    ),
  },
  { additionalProperties: false },
);

const MatchResumeToJdOutputSchema = Type.Object(
  {
    matches: Type.Array(
      Type.Object(
        {
          requirement_ref: IdSchema,
          category: Type.Union([
            Type.Literal("direct"),
            Type.Literal("transferable"),
            Type.Literal("needs_evidence"),
            Type.Literal("gap"),
          ]),
          fact_refs: StringListSchema,
          resume_target_paths: Type.Array(
            Type.String({ minLength: 1, maxLength: 512 }),
            { maxItems: 1_000 },
          ),
          reason_code: Type.String({ minLength: 1, maxLength: 128 }),
        },
        { additionalProperties: false },
      ),
      { maxItems: 1_000 },
    ),
  },
  { additionalProperties: false },
);

const GenerateSuggestionsBatchOutputSchema = Type.Object(
  {
    suggestions: Type.Array(
      Type.Object(
        {
          target_path: Type.String({ minLength: 1, maxLength: 512 }),
          original_hash: HashSchema,
          suggested_text: TextSchema,
          atomic_claims: Type.Array(AtomicClaimSchema, { maxItems: 1_000 }),
          requirement_ref: IdSchema,
          reason: Type.String({ minLength: 1, maxLength: 4_000 }),
          risk_flags: Type.Array(IdSchema, { maxItems: 100 }),
          proposed_status: Type.Union([Type.Literal("pending"), Type.Literal("blocked")]),
        },
        { additionalProperties: false },
      ),
      { maxItems: 1_000 },
    ),
  },
  { additionalProperties: false },
);

export interface WorkflowResultMap {
  analyze_intake_answer: Static<typeof AnalyzeIntakeAnswerOutputSchema>;
  compose_resume_draft: Static<typeof ComposeResumeDraftOutputSchema>;
  parse_jd: Static<typeof ParseJdOutputSchema>;
  match_resume_to_jd: Static<typeof MatchResumeToJdOutputSchema>;
  generate_suggestions_batch: Static<typeof GenerateSuggestionsBatchOutputSchema>;
}

export const WORKFLOW_OUTPUT_SCHEMAS: Record<WorkflowType, TSchema> = {
  analyze_intake_answer: AnalyzeIntakeAnswerOutputSchema,
  compose_resume_draft: ComposeResumeDraftOutputSchema,
  parse_jd: ParseJdOutputSchema,
  match_resume_to_jd: MatchResumeToJdOutputSchema,
  generate_suggestions_batch: GenerateSuggestionsBatchOutputSchema,
};

export interface ProviderModelRoute {
  provider: string;
  model: string;
  approved_data_policy: boolean;
}

export interface WorkflowRoute {
  enabled: boolean;
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

export type RuntimeCall = (call: RuntimeCallInput) => Promise<RuntimeResult>;

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

export const TRACE_COST_USD_MAX = 999_999;
export const TRACE_COST_USD_DECIMAL_PLACES = 18;

export const TRACE_EVENT_TYPES = [
  "run_queued",
  "agent_start",
  "turn_start",
  "message_start",
  "first_token",
  "message_update",
  "message_end",
  "tool_execution_start",
  "tool_execution_end",
  "turn_end",
  "auto_retry_start",
  "auto_retry_end",
  "model_fallback",
  "schema_validation_failed",
  "fact_validation_failed",
  "agent_end",
  "agent_settled",
  "run_succeeded",
  "run_failed",
  "run_cancelled",
  "user_accepted",
  "user_edited",
  "user_ignored",
  "unknown",
] as const;
export type TraceEventType = (typeof TRACE_EVENT_TYPES)[number];

export interface TraceEvent {
  ai_run_id: string;
  trace_id: string;
  task_id: string;
  event_seq: number;
  event_type: TraceEventType;
  occurred_at: string;
  details?: Record<string, unknown>;
}

export interface WorkflowRun {
  ai_run_id: string;
  trace_id: string;
  task_id: string;
  workflow_type: WorkflowType;
  workflow_version: string;
  prompt_template_version: string;
  status: "succeeded" | "failed" | "cancelled";
  error_code: string | null;
  provider: string | null;
  requested_model: string | null;
  response_model: string | null;
  started_at: string;
  first_token_at: string | null;
  finished_at: string;
  usage: TraceUsage;
  events: TraceEvent[];
  turn_count: number;
  tool_call_count: number;
  retry_count: number;
  fallback_count: number;
  schema_valid: boolean;
  facts_valid: boolean;
  input_hash: string;
  exportable: boolean;
  risk_flags: string[];
}

export interface AiExecutionReceipt<T = unknown> {
  run: WorkflowRun;
  result?: T;
}
