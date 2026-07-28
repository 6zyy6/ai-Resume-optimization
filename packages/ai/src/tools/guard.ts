import { Type, type Static, type TSchema } from "typebox";
import { Value } from "typebox/value";

import {
  AtomicClaimSchema,
  QuestionSchema,
  type ConfirmedFact,
  type JdRequirement,
} from "../contracts.js";

export const ALLOWED_TOOL_NAMES = [
  "get_confirmed_facts",
  "get_jd_requirements",
  "emit_question",
  "emit_resume_suggestion",
  "emit_fact_check_result",
] as const;

export type AllowedToolName = (typeof ALLOWED_TOOL_NAMES)[number];

const GetConfirmedFactsParameters = Type.Object(
  {
    fact_ids: Type.Array(Type.String({ minLength: 1, maxLength: 128 }), {
      minItems: 1,
      maxItems: 1_000,
    }),
  },
  { additionalProperties: false },
);

const GetJdRequirementsParameters = Type.Object(
  {
    requirement_ids: Type.Array(
      Type.String({ minLength: 1, maxLength: 128 }),
      { minItems: 1, maxItems: 1_000 },
    ),
  },
  { additionalProperties: false },
);

const EmitQuestionParameters = QuestionSchema;

const EmitSuggestionParameters = Type.Object(
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
  },
  { additionalProperties: false },
);

const EmitFactCheckParameters = Type.Object(
  {
    claim: Type.String({ minLength: 1, maxLength: 20_000 }),
    fact_refs: Type.Array(Type.String({ minLength: 1, maxLength: 128 }), {
      maxItems: 1_000,
    }),
    status: Type.Union([
      Type.Literal("supported"),
      Type.Literal("needs_confirmation"),
      Type.Literal("unsupported"),
    ]),
    risk_flags: Type.Array(Type.String({ minLength: 1, maxLength: 128 }), {
      maxItems: 100,
    }),
  },
  { additionalProperties: false },
);

export const TOOL_PARAMETER_SCHEMAS: Record<AllowedToolName, TSchema> = {
  get_confirmed_facts: GetConfirmedFactsParameters,
  get_jd_requirements: GetJdRequirementsParameters,
  emit_question: EmitQuestionParameters,
  emit_resume_suggestion: EmitSuggestionParameters,
  emit_fact_check_result: EmitFactCheckParameters,
};

const ToolOutputSchemas: Record<AllowedToolName, TSchema> = {
  get_confirmed_facts: Type.Object(
    {
      facts: Type.Array(
        Type.Object(
          {
            id: Type.String(),
            kind: Type.String(),
            value: Type.String(),
            status: Type.Literal("confirmed"),
          },
          { additionalProperties: false },
        ),
      ),
    },
    { additionalProperties: false },
  ),
  get_jd_requirements: Type.Object(
    {
      requirements: Type.Array(
        Type.Object(
          {
            id: Type.String(),
            category: Type.String(),
            value: Type.String(),
          },
          { additionalProperties: false },
        ),
      ),
    },
    { additionalProperties: false },
  ),
  emit_question: Type.Object(
    { question: EmitQuestionParameters },
    { additionalProperties: false },
  ),
  emit_resume_suggestion: Type.Object(
    { suggestion: EmitSuggestionParameters },
    { additionalProperties: false },
  ),
  emit_fact_check_result: Type.Object(
    { fact_check_result: EmitFactCheckParameters },
    { additionalProperties: false },
  ),
};

export class ToolGuardError extends Error {
  constructor(
    readonly code:
      | "unknown_tool"
      | "unknown_id"
      | "schema_validation_failed"
      | "tool_limit_exceeded"
      | "already_terminal",
  ) {
    super(code);
    this.name = "ToolGuardError";
  }
}

export interface ToolGuard {
  preflight(toolName: string, args: unknown): void;
  execute(toolName: string, args: unknown): unknown;
  snapshot(): { tool_calls: number; terminal: boolean };
}

function validate<T extends TSchema>(schema: T, value: unknown): Static<T> {
  if (!Value.Check(schema, value)) {
    throw new ToolGuardError("schema_validation_failed");
  }
  return value;
}

function assertKnownIds(ids: string[], knownIds: ReadonlySet<string>): void {
  if (ids.some((id) => !knownIds.has(id))) {
    throw new ToolGuardError("unknown_id");
  }
}

export function createToolGuard({
  confirmedFacts,
  jdRequirements,
  maxToolCalls = 6,
}: {
  confirmedFacts: ConfirmedFact[];
  jdRequirements: JdRequirement[];
  maxToolCalls?: number;
}): ToolGuard {
  const factsById = new Map(confirmedFacts.map((fact) => [fact.id, fact]));
  const requirementsById = new Map(
    jdRequirements.map((requirement) => [requirement.id, requirement]),
  );
  let toolCalls = 0;
  let terminal = false;

  function preflight(toolName: string, rawArgs: unknown): void {
    if (!ALLOWED_TOOL_NAMES.includes(toolName as AllowedToolName)) {
      throw new ToolGuardError("unknown_tool");
    }
    if (terminal) {
      throw new ToolGuardError("already_terminal");
    }
    if (toolCalls >= maxToolCalls) {
      throw new ToolGuardError("tool_limit_exceeded");
    }

    const name = toolName as AllowedToolName;
    const args = validate(TOOL_PARAMETER_SCHEMAS[name], rawArgs) as Record<
      string,
      unknown
    >;
    if (name === "get_confirmed_facts") {
      assertKnownIds(args.fact_ids as string[], new Set(factsById.keys()));
    } else if (name === "get_jd_requirements") {
      assertKnownIds(
        args.requirement_ids as string[],
        new Set(requirementsById.keys()),
      );
    } else if (name === "emit_question") {
      assertKnownIds(args.fact_refs as string[], new Set(factsById.keys()));
    } else if (name === "emit_resume_suggestion") {
      for (const claim of args.atomic_claims as Array<{
        fact_refs: string[];
      }>) {
        assertKnownIds(claim.fact_refs, new Set(factsById.keys()));
      }
      assertKnownIds(
        args.jd_requirement_refs as string[],
        new Set(requirementsById.keys()),
      );
    } else {
      assertKnownIds(args.fact_refs as string[], new Set(factsById.keys()));
    }
  }

  return {
    preflight,
    execute(toolName, rawArgs) {
      preflight(toolName, rawArgs);
      const name = toolName as AllowedToolName;
      const args = rawArgs as Record<string, unknown>;
      let output: unknown;

      switch (name) {
        case "get_confirmed_facts": {
          const factIds = args.fact_ids as string[];
          output = { facts: factIds.map((id) => factsById.get(id)!) };
          break;
        }
        case "get_jd_requirements": {
          const requirementIds = args.requirement_ids as string[];
          output = {
            requirements: requirementIds.map(
              (id) => requirementsById.get(id)!,
            ),
          };
          break;
        }
        case "emit_question":
          output = { question: args };
          terminal = true;
          break;
        case "emit_resume_suggestion":
          output = { suggestion: args };
          terminal = true;
          break;
        case "emit_fact_check_result":
          output = { fact_check_result: args };
          terminal = true;
          break;
      }

      validate(ToolOutputSchemas[name], output);
      toolCalls += 1;
      return output;
    },
    snapshot() {
      return { tool_calls: toolCalls, terminal };
    },
  };
}
