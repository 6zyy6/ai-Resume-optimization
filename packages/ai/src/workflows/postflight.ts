import { Value } from "typebox/value";

import {
  WORKFLOW_OUTPUT_SCHEMAS,
  type SchemaFeedback,
  type WorkflowInput,
} from "../contracts.js";

function normalizePath(path: string): string {
  if (!path || path === "/") return "$";
  return (path.startsWith("$") ? path : `$${path.replaceAll("/", ".")}`).slice(0, 512);
}

function shapeFeedback(input: WorkflowInput, output: unknown): SchemaFeedback[] {
  return [...Value.Errors(WORKFLOW_OUTPUT_SCHEMAS[input.workflow_type], output)]
    .slice(0, 20)
    .map((error) => {
      const details = error as unknown as Record<string, unknown>;
      return {
        path: normalizePath(
          typeof details.instancePath === "string"
            ? details.instancePath
            : typeof details.path === "string" ? details.path : "",
        ),
        type: "schema",
      };
    });
}

function unknownReference(path: string): SchemaFeedback {
  return { path, type: "unknown_reference" };
}

function isValidSourceRange(
  range: { start: number; end: number },
  source: string,
): boolean {
  return (
    range.start >= 0 &&
    range.start < range.end &&
    range.end <= Array.from(source).length
  );
}

function checkRefs(
  refs: string[],
  allowed: Set<string>,
  path: string,
  failures: SchemaFeedback[],
) {
  refs.forEach((ref, index) => {
    if (!allowed.has(ref)) failures.push(unknownReference(`${path}[${index}]`));
  });
}

function suggestionSourceKey(
  requirementRef: string,
  targetPath: string,
  originalHash: string,
): string {
  return JSON.stringify([requirementRef, targetPath, originalHash]);
}

function referenceFeedback(input: WorkflowInput, output: any): SchemaFeedback[] {
  const failures: SchemaFeedback[] = [];
  switch (input.workflow_type) {
    case "analyze_intake_answer": {
      const factIds = new Set(input.payload.confirmed_facts.map(({ id }) => id));
      output.fact_candidates.forEach((candidate: any, index: number) => {
        if (candidate.source_answer_id !== input.payload.answer_id) {
          failures.push(unknownReference(`$.fact_candidates[${index}].source_answer_id`));
        }
        if (!isValidSourceRange(candidate.source_range, input.payload.answer_text)) {
          failures.push({
            path: `$.fact_candidates[${index}].source_range`,
            type: "range_invalid",
          });
        }
      });
      if (output.question_candidate) {
        checkRefs(output.question_candidate.related_fact_refs, factIds, "$.question_candidate.related_fact_refs", failures);
      }
      break;
    }
    case "compose_resume_draft": {
      const factIds = new Set(input.payload.confirmed_facts.map(({ id }) => id));
      const allowedSectionTypes = new Set(input.payload.allowed_section_types);
      output.sections.forEach((section: any, sectionIndex: number) => {
        if (!allowedSectionTypes.has(section.type)) {
          failures.push({
            path: `$.sections[${sectionIndex}].type`,
            type: "not_allowed",
          });
        }
        section.bullets.forEach((bullet: any, bulletIndex: number) => {
          bullet.atomic_claims.forEach((claim: any, claimIndex: number) =>
            checkRefs(claim.fact_refs, factIds, `$.sections[${sectionIndex}].bullets[${bulletIndex}].atomic_claims[${claimIndex}].fact_refs`, failures));
        });
      });
      break;
    }
    case "parse_jd": {
      const allowedCategories = new Set(input.payload.allowed_categories);
      output.requirements.forEach((requirement: any, index: number) => {
        if (!allowedCategories.has(requirement.category)) {
          failures.push({
            path: `$.requirements[${index}].category`,
            type: "not_allowed",
          });
        }
        if (!isValidSourceRange(requirement.source_range, input.payload.jd_text)) {
          failures.push({ path: `$.requirements[${index}].source_range`, type: "range_invalid" });
        }
      });
      break;
    }
    case "match_resume_to_jd": {
      const requirementIds = input.payload.confirmed_requirements.map(({ id }) => id);
      const factIds = new Set(input.payload.confirmed_facts.map(({ id }) => id));
      const seen = new Set<string>();
      output.matches.forEach((match: any, index: number) => {
        if (!requirementIds.includes(match.requirement_ref) || seen.has(match.requirement_ref)) {
          failures.push(unknownReference(`$.matches[${index}].requirement_ref`));
        }
        seen.add(match.requirement_ref);
        checkRefs(match.fact_refs, factIds, `$.matches[${index}].fact_refs`, failures);
      });
      if (seen.size !== requirementIds.length) failures.push({ path: "$.matches", type: "requirement_coverage_invalid" });
      break;
    }
    case "generate_suggestions_batch": {
      const factIds = new Set(input.payload.confirmed_facts.map(({ id }) => id));
      const requirementIds = new Set(
        input.payload.confirmed_requirements.map(({ id }) => id),
      );
      input.payload.matches.forEach((match, index) => {
        if (!requirementIds.has(match.requirement_ref)) {
          failures.push(
            unknownReference(`$.payload.matches[${index}].requirement_ref`),
          );
        }
        checkRefs(
          match.fact_refs,
          factIds,
          `$.payload.matches[${index}].fact_refs`,
          failures,
        );
      });
      const sources = new Map(input.payload.matches.map((match) => [
        suggestionSourceKey(
          match.requirement_ref,
          match.target_path,
          match.original_hash,
        ),
        match,
      ]));
      output.suggestions.forEach((suggestion: any, index: number) => {
        const source = sources.get(suggestionSourceKey(
          suggestion.requirement_ref,
          suggestion.target_path,
          suggestion.original_hash,
        ));
        if (
          !requirementIds.has(suggestion.requirement_ref) ||
          !source ||
          !["transferable", "needs_evidence"].includes(source.category)
        ) {
          failures.push(unknownReference(`$.suggestions[${index}]`));
        }
        suggestion.atomic_claims.forEach((claim: any, claimIndex: number) =>
          checkRefs(claim.fact_refs, factIds, `$.suggestions[${index}].atomic_claims[${claimIndex}].fact_refs`, failures));
      });
      break;
    }
  }
  return failures.slice(0, 20);
}

export function validateRuntimeOutput(input: WorkflowInput, output: unknown): SchemaFeedback[] {
  const shapeFailures = shapeFeedback(input, output);
  return shapeFailures.length > 0 ? shapeFailures : referenceFeedback(input, output as any);
}

export function enforceEvidence(_input: WorkflowInput, output: unknown) {
  return { output, exportable: false, risk_flags: [] as string[] };
}
