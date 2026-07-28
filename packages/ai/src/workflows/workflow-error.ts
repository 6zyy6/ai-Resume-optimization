export class WorkflowError extends Error {
  constructor(
    readonly code:
      | "input_schema_invalid"
      | "output_schema_invalid"
      | "output_reference_invalid"
      | "prompt_version_unavailable"
      | "turn_limit_exceeded"
      | "tool_limit_exceeded"
      | "token_limit_exceeded"
      | "timeout_exceeded"
      | "cost_limit_exceeded"
      | "model_route_unavailable"
      | "runtime_failed",
  ) {
    super(code);
    this.name = "WorkflowError";
  }
}
