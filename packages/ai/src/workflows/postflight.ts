import { Value } from "typebox/value";

import {
  WORKFLOW_OUTPUT_SCHEMAS,
  type SchemaFeedback,
  type WorkflowInput,
} from "../contracts.js";
import { factCheck } from "./fact-check.js";

function normalizePath(path: string): string {
  if (!path || path === "/") {
    return "$";
  }
  if (path.startsWith("$")) {
    return path.slice(0, 512);
  }
  return `$${path.replaceAll("/", ".")}`.slice(0, 512);
}

function shapeFeedback(input: WorkflowInput, output: unknown): SchemaFeedback[] {
  const schema = WORKFLOW_OUTPUT_SCHEMAS[input.workflow_type];
  return [...Value.Errors(schema, output)].slice(0, 20).map((error) => {
    const details = error as unknown as Record<string, unknown>;
    const path =
      typeof details.instancePath === "string"
        ? details.instancePath
        : typeof details.path === "string"
          ? details.path
          : "";
    return { path: normalizePath(path), type: "schema" };
  }).sort((left, right) => right.path.length - left.path.length);
}

function referenceFeedback(
  input: WorkflowInput,
  output: unknown,
): SchemaFeedback[] {
  const record = output as Record<string, unknown>;
  const factIds = new Set(input.confirmed_facts.map(({ id }) => id));
  const requirementIds = new Set(input.jd_requirements.map(({ id }) => id));
  const failures: SchemaFeedback[] = [];
  const check = (
    refs: unknown,
    ids: Set<string>,
    path: string,
  ) => {
    if (!Array.isArray(refs)) {
      return;
    }
    refs.forEach((ref, index) => {
      if (typeof ref === "string" && !ids.has(ref)) {
        failures.push({
          path: `${path}[${index}]`.slice(0, 512),
          type: "unknown_reference",
        });
      }
    });
  };

  if (input.workflow_type === "next_question") {
    check(
      (record.question as Record<string, unknown>)?.fact_refs,
      factIds,
      "$.question.fact_refs",
    );
  } else if (
    input.workflow_type === "write_experience_bullet" ||
    input.workflow_type === "generate_suggestion"
  ) {
    const claims = record.atomic_claims;
    if (Array.isArray(claims)) {
      claims.forEach((claim, index) => check(
        (claim as Record<string, unknown>).fact_refs,
        factIds,
        `$.atomic_claims[${index}].fact_refs`,
      ));
    }
    check(
      record.jd_requirement_refs,
      requirementIds,
      "$.jd_requirement_refs",
    );
  } else if (input.workflow_type === "fact_check") {
    const claims = record.claims;
    if (Array.isArray(claims)) {
      claims.forEach((claim, index) => check(
        (claim as Record<string, unknown>).fact_refs,
        factIds,
        `$.claims[${index}].fact_refs`,
      ));
    }
  } else if (input.workflow_type === "match_resume_to_jd") {
    const matches = record.matches;
    if (Array.isArray(matches)) {
      matches.forEach((match, index) => {
        const item = match as Record<string, unknown>;
        check(item.fact_refs, factIds, `$.matches[${index}].fact_refs`);
        check(
          item.requirement_refs,
          requirementIds,
          `$.matches[${index}].requirement_refs`,
        );
      });
    }
  } else if (input.workflow_type === "extract_facts") {
    const allowedSources = new Set([
      "current_object",
      ...input.confirmed_facts.map(({ id }) => id),
    ]);
    const facts = record.facts;
    if (Array.isArray(facts)) {
      facts.forEach((fact, index) => check(
        (fact as Record<string, unknown>).source_refs,
        allowedSources,
        `$.facts[${index}].source_refs`,
      ));
    }
  }
  return failures.slice(0, 20);
}

export function validateRuntimeOutput(
  input: WorkflowInput,
  output: unknown,
): SchemaFeedback[] {
  const shapeFailures = shapeFeedback(input, output);
  return shapeFailures.length > 0
    ? shapeFailures
    : referenceFeedback(input, output);
}

export function enforceEvidence(
  input: WorkflowInput,
  modelOutput: unknown,
): {
  output: unknown;
  exportable: boolean;
  risk_flags: string[];
  failure_path?: string;
} {
  if (
    input.workflow_type === "write_experience_bullet" ||
    input.workflow_type === "generate_suggestion"
  ) {
    const suggestion = modelOutput as Record<string, unknown> & {
      suggestion_text: string;
    };
    const checked = factCheck(
      suggestion.suggestion_text,
      input.confirmed_facts,
    );
    return {
      output: {
        ...suggestion,
        atomic_claims: checked.claims,
        risk_flags: checked.risk_flags,
        requires_user_confirmation:
          suggestion.requires_user_confirmation === true ||
          !checked.exportable,
        exportable: checked.exportable,
      },
      exportable: checked.exportable,
      risk_flags: checked.risk_flags,
      failure_path: checked.exportable ? undefined : "$.atomic_claims",
    };
  }
  if (input.workflow_type === "fact_check") {
    const text = input.current_object.text;
    const checked = factCheck(
      typeof text === "string" ? text : "",
      input.confirmed_facts,
    );
    return {
      output: checked,
      exportable: checked.exportable,
      risk_flags: checked.risk_flags,
      failure_path: checked.exportable ? undefined : "$.claims",
    };
  }
  return {
    output: modelOutput,
    exportable: false,
    risk_flags: [],
  };
}
