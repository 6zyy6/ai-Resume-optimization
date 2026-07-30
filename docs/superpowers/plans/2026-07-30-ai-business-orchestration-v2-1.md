# AI Business Orchestration V2.1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the isolated eight-workflow AI engine with five strictly typed, recoverable business workflows whose runs, costs, evidence, fallbacks, and user decisions are traceable end to end.

**Architecture:** FastAPI remains the only business state owner. It derives stable Pi run IDs per Task stage, reserves quota in the enqueue transaction, invokes Pi through strict workflow envelopes, and publishes business results only after deterministic policy checks. Pi owns model execution and short-lived run coordination; PostgreSQL owns Task, AiRun, Trace, candidates, provenance, and immutable resume state.

**Tech Stack:** Next.js 16, React 19, TypeScript, TypeBox, Fastify, Python 3.12, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL/SQLite tests, Redis RunStore, Celery, Vitest, Pytest, Playwright.

## Global Constraints

- Work directly on `main` as explicitly authorized by the user; do not create a worktree.
- Preserve the user's unstaged `AGENTS.md`, `PRD-AI-大学生简历助手.md`, and `竞品调研-AI大学生简历助手.md`.
- Follow strict TDD: add one observable failing test, run it and confirm the expected failure, implement the minimum behavior, then rerun it.
- FastAPI is the only public business entry and the only owner of authentication, authorization, idempotency, quota, Fact, ResumeVersion, Job, Task, export, and final policy state.
- Pi must not connect to PostgreSQL, manipulate user files, create confirmed Facts, or decide exportability.
- Owner filtering must be part of every SQL query, including idempotent replay.
- A Task, reservation UsageLedger row, business resource, TaskEvent, and Outbox must be created in one transaction.
- Stable run IDs use exactly `run_` plus the first 40 lowercase hex characters of `sha256(task_id + ":" + workflow_stage + ":" + input_hash)`.
- Pi categories map exactly: `direct→proved`, `transferable→underexpressed`, `needs_evidence→needs_confirmation`, `gap→real_gap`.
- Unknown, duplicate, or missing requirement classifications fail validation; they never silently become `real_gap`.
- Candidate acceptance or editing creates a sourced `confirmed` Fact. Candidate editing creates a separate `fact_candidate_edit` source.
- Usage reservation states are exactly `reserved`, `consumed`, and `released`. Admission counts `reserved + consumed`; user-visible usage counts only `consumed`.
- Resume generation modes are exactly `manual`, `model`, and `rule_fallback`; JD/Match/Suggestion generation modes are exactly `model` and `rule_fallback`.
- Generated OpenAPI files under `packages/shared/generated/` are changed only by `pnpm generate`.
- Do not add a production dependency without first reporting why it is necessary.
- Do not persist prompts, chain-of-thought, provider response bodies, access tokens, complete resumes, or complete JDs in logs or trace payloads.
- P0 code can be locally verified with fixture providers, but real-model, cloud, external-browser, and user-study gates remain `BLOCKED` until their required evidence exists.

---

### Task 1: Replace the Pi workflow contract with a strict five-workflow union

**Files:**
- Modify: `packages/ai/src/contracts.ts`
- Modify: `packages/ai/src/workflows/prompt-registry.ts`
- Modify: `packages/ai/src/workflows/postflight.ts`
- Modify: `packages/ai/src/workflows/pi-runtime.ts`
- Modify: `packages/ai/src/workflows/run-workflow.ts`
- Modify: `packages/ai/src/model-router.ts`
- Modify: `packages/ai/src/server/index.ts`
- Create: `packages/ai/tests/contracts-v2.test.ts`
- Modify: `packages/ai/tests/run-workflow.test.ts`
- Modify: `packages/ai/tests/pi-runtime.integration.test.ts`
- Modify: `packages/ai/tests/model-router.test.ts`

**Interfaces:**
- Produces `MODEL_WORKFLOW_TYPES = ["analyze_intake_answer", "compose_resume_draft", "parse_jd", "match_resume_to_jd", "generate_suggestions_batch"]`.
- Produces `WorkflowInput` as a TypeBox discriminated union with the common fields `workflow_type`, `workflow_version="2"`, `prompt_template_version`, `trace_id`, `task_id`, `owner_scope_hash`, `locale="zh-CN"`, `input_version`, `input_hash`, and workflow-specific `payload`.
- Produces `WorkflowResultMap` and `WORKFLOW_OUTPUT_SCHEMAS` keyed by the same five workflow names.
- `fact_policy_check` and `style_quality_check` are not Pi workflow types or model routes.

- [ ] **Step 1: Add strict contract tests and confirm the old contract fails**

Create table-driven tests with literal inputs:

```ts
const validInputs = [
  makeAnalyzeIntakeInput(),
  makeComposeDraftInput(),
  makeParseJdInput(),
  makeMatchInput(),
  makeSuggestionBatchInput(),
];

it.each(validInputs)("accepts each V2 workflow envelope", (input) => {
  expect(Value.Check(WorkflowInputSchema, input)).toBe(true);
});

it("rejects the removed generic current_object payload", () => {
  expect(Value.Check(WorkflowInputSchema, {
    ...makeParseJdInput(),
    current_object: { raw: "不得被接受" },
  })).toBe(false);
});

it("rejects model-generated database ids and unknown payload keys", () => {
  const output = { requirements: [{
    id: "req_model",
    category: "must_have",
    priority: 1,
    value: "Python",
    source_range: { start: 0, end: 6 },
    explicitness: "explicit",
    confidence_band: "high",
  }] };
  expect(Value.Check(WORKFLOW_OUTPUT_SCHEMAS.parse_jd, output)).toBe(false);
});
```

Run: `pnpm --filter @resume/ai exec vitest run tests/contracts-v2.test.ts`
Expected: FAIL because V2 workflow names and payload schemas do not exist.

- [ ] **Step 2: Implement the strict common envelope and five payload/result schemas**

Use `Type.Object(..., { additionalProperties: false })` at every object level. Exact result contracts:

```text
analyze_intake_answer:
  fact_candidates[{kind,value,source_answer_id,source_range{start,end},risk_flags[]}]
  missing_slots[]
  question_candidate{reason,slot,text,related_fact_refs[]}|null

compose_resume_draft:
  sections[{type,title,bullets[{text,atomic_claims[{text,fact_refs[],claim_order}],risk_flags[]}]}]

parse_jd:
  requirements[{category,priority,value,source_range{start,end},explicitness,confidence_band}]

match_resume_to_jd:
  matches[{requirement_ref,category,fact_refs[],resume_target_paths[],reason_code}]

generate_suggestions_batch:
  suggestions[{target_path,original_hash,suggested_text,atomic_claims[],requirement_ref,reason,risk_flags[],proposed_status}]
```

Allowed output literals:

```text
parse category: responsibility|must_have|nice_to_have|implicit_capability
priority: 1|2|3
explicitness: explicit|implicit
confidence_band: high|medium|low
match category: direct|transferable|needs_evidence|gap
proposed_status: pending|blocked
```

- [ ] **Step 3: Update prompt, runtime, postflight, router defaults, and fixtures**

`prompt-registry.ts` must resolve by `prompt_template_version`, include the selected payload schema, and never interpolate Prompt reasoning instructions from caller data. `postflight.ts` must:

```text
analyze: validate source_answer_id and related_fact_refs
compose: validate every atomic claim fact ref
parse: validate range bounds against payload.jd_text
match: require exactly one unique match per input requirement
suggestions: validate requirement refs, fact refs, original_hash, and allowed source categories
```

All five routes default to `enabled: true`. Deleted workflow names must have zero runtime branches and zero fixture entries.

- [ ] **Step 4: Run focused and package tests**

Run: `pnpm --filter @resume/ai exec vitest run tests/contracts-v2.test.ts tests/run-workflow.test.ts tests/pi-runtime.integration.test.ts tests/model-router.test.ts`
Expected: PASS with zero old-workflow TypeScript references outside migration documentation.

Run: `rg -n '"(extract_facts|next_question|write_experience_bullet|generate_suggestion|fact_check|style_check)"' packages/ai/src`
Expected: no matches.

- [ ] **Step 5: Commit**

```bash
git add packages/ai/src packages/ai/tests
git commit -m "feat(ai): enforce V2 workflow contracts"
```

---

### Task 2: Make Pi run creation idempotent and return complete terminal receipts

**Files:**
- Modify: `packages/ai/src/contracts.ts`
- Modify: `packages/ai/src/workflows/run-workflow.ts`
- Modify: `packages/ai/src/workflows/pi-runtime.ts`
- Modify: `packages/ai/src/server/run-store.ts`
- Modify: `packages/ai/src/server/memory-run-store.ts`
- Modify: `packages/ai/src/server/redis-run-store.ts`
- Modify: `packages/ai/src/server/app.ts`
- Modify: `packages/ai/tests/run-workflow.test.ts`
- Modify: `packages/ai/tests/run-store.test.ts`
- Modify: `packages/ai/tests/redis-run-store.integration.test.ts`
- Modify: `packages/ai/tests/server.test.ts`

**Interfaces:**
- `POST /internal/v1/runs` consumes `{ ai_run_id, input }`.
- Same `ai_run_id` plus same `input_hash` returns the existing run with HTTP 202.
- Same `ai_run_id` plus a different `input_hash` returns HTTP 409 and `AI_RUN_ID_REUSED`.
- `GET /internal/v1/runs/{id}` returns `{request_id, receipt}` for all terminal states.
- `AiExecutionReceipt<T>` contains `run` metadata plus optional typed `result`.

- [ ] **Step 1: Add failing server and RunStore idempotency tests**

Add observable tests:

```ts
it("replays the same run id without executing the provider twice", async () => {
  const first = await app.inject(runRequest("run_stable", "hash_a"));
  const replay = await app.inject(runRequest("run_stable", "hash_a"));
  expect(first.statusCode).toBe(202);
  expect(replay.statusCode).toBe(202);
  expect(runtimeCalls).toBe(1);
});

it("rejects reuse of a run id with a different input hash", async () => {
  await app.inject(runRequest("run_stable", "hash_a"));
  const conflict = await app.inject(runRequest("run_stable", "hash_b"));
  expect(conflict.statusCode).toBe(409);
  expect(conflict.json().error.code).toBe("AI_RUN_ID_REUSED");
});
```

Also assert a failed receipt retains `run_failed`, usage, timestamps, schema flags, model metadata, and `error_code`.

Run: `pnpm --filter @resume/ai exec vitest run tests/server.test.ts tests/run-store.test.ts tests/run-workflow.test.ts`
Expected: FAIL because the server generates its own ID and failure drops the receipt.

- [ ] **Step 2: Implement atomic create-or-replay in both RunStores**

`RunStore.createOrGet` returns:

```ts
type CreateRunResult =
  | { kind: "created"; run: StoredRun }
  | { kind: "existing"; run: StoredRun }
  | { kind: "conflict"; run: StoredRun };
```

Memory uses a single Map mutation. Redis uses one Lua operation that compares stored `input_hash` before creating. No GET-then-SET race is allowed.

- [ ] **Step 3: Make runWorkflow return receipts instead of throwing away failed state**

The terminal receipt run metadata must include:

```text
ai_run_id, workflow_type, workflow_version, prompt_template_version,
trace_id, task_id, status, error_code, provider, requested_model,
response_model, started_at, first_token_at, finished_at,
usage, turn_count, tool_call_count, retry_count, fallback_count,
schema_valid, facts_valid, input_hash, events
```

`runWorkflow` catches workflow errors after appending `run_failed` and returns `status="failed"`. Cancellation returns `status="cancelled"`. The server must always store the complete receipt.

- [ ] **Step 4: Scope readiness to enabled routes and mark runtime mode**

`ModelRouter.isReady()` and Pi runtime readiness inspect only routes with `enabled=true`. `/ready` and `/version` include `runtime_mode: "fixture"|"production"`. A disabled route without credentials must not fail readiness.

- [ ] **Step 5: Run focused, package, and Redis tests**

Run: `pnpm --filter @resume/ai test`
Expected: all AI tests pass.

Run: `pnpm --filter @resume/ai build`
Expected: TypeScript build exits 0.

Run: `pnpm --filter @resume/ai exec vitest run tests/redis-run-store.integration.test.ts`
Expected: PASS when Redis integration is available; otherwise the existing environment-gated test remains explicitly skipped.

- [ ] **Step 6: Commit**

```bash
git add packages/ai/src packages/ai/tests
git commit -m "feat(ai): persist recoverable run receipts"
```

---

### Task 3: Add the typed FastAPI AI adapter, audit persistence, and quota reservations

**Files:**
- Modify: `packages/api/app/db/models.py`
- Create: `packages/api/migrations/versions/0012_ai_receipts_and_usage_reservations.py`
- Rewrite: `packages/api/app/integrations/ai_client.py`
- Create: `packages/api/app/modules/ai_runs/__init__.py`
- Create: `packages/api/app/modules/ai_runs/service.py`
- Modify: `packages/api/app/modules/tasks/service.py`
- Modify: `packages/api/app/modules/usage/service.py`
- Modify: `packages/api/app/workers/pipeline.py`
- Create: `packages/api/tests/test_ai_receipts.py`
- Modify: `packages/api/tests/test_ai_cancellation.py`
- Modify: `packages/api/tests/test_task_state.py`
- Modify: `packages/api/tests/test_usage.py`
- Modify: `packages/api/tests/test_schema_constraints.py`

**Interfaces:**
- `derive_ai_run_id(task_id: str, workflow_stage: str, input_hash: str) -> str`.
- `AiClient.run(input: AiWorkflowRequest, cancellation: AiCancellation | None) -> AiExecutionReceipt`.
- `AiRunService.persist_in_session(session, owner_id, receipt, *, workflow_stage, result_ref=None)`.
- `TaskService.consume_ai_reservation(...)`, `release_ai_reservation(...)`, and `settle_ai_run(...)`.

- [ ] **Step 1: Add failing tests for the stable ID, typed receipt, atomic trace, and reservation visibility**

Use literal assertions:

```py
def test_stable_run_id_is_rederived_for_the_same_task_stage_and_hash():
    first = derive_ai_run_id("tsk_1", "match", "abc")
    second = derive_ai_run_id("tsk_1", "match", "abc")
    assert first == second
    assert first.startswith("run_")
    assert len(first) == 44

def test_reserved_usage_blocks_admission_but_is_hidden_from_display(...):
    # insert 20 reserved ai_task rows
    # assert next admission is denied
    # assert GET usage reports quantity 0
```

Add DB assertions that one failed receipt creates one `AiRun`, all sequential `AiTraceEvent` rows, and no prompt or user body fields.

Run: `.venv/bin/python -m pytest packages/api/tests/test_ai_receipts.py packages/api/tests/test_usage.py -q`
Expected: FAIL because these interfaces and columns do not exist.

- [ ] **Step 2: Add migration 0012 and SQLAlchemy fields**

`ai_runs` gains non-null `status`, nullable `error_code`, and non-null `workflow_stage`; add unique `(task_id, workflow_stage, input_hash)`.

`usage_ledger` gains:

```text
state reserved|consumed|released
task_id nullable owner-scoped FK
ai_run_id nullable
updated_at non-null
```

Existing ledger rows migrate to `consumed`. Add checks for state literals. Keep quantity `1` for one business AI Task; a two-workflow match does not add a second quantity row.

- [ ] **Step 3: Implement typed Python request and receipt models**

Use frozen dataclasses or strict Pydantic models, never a public `dict[str, Any]` return. The HTTP body is:

```py
{
    "ai_run_id": derive_ai_run_id(task_id, workflow_stage, input_hash),
    "input": workflow_specific_envelope,
}
```

The adapter polls `receipt`, returns failed and cancelled receipts without losing metadata, and raises transport errors only when no terminal receipt exists.

- [ ] **Step 4: Implement receipt persistence and sequential active-run settlement**

`AiRunService.persist_in_session` owner-filters Task, upserts the stable run by its unique key, validates continuous event sequences starting at 1, and inserts no raw user body. `settle_ai_run` clears `Task.active_ai_run_id` only when the task is still running and the ID matches. For a cancelled Task it acknowledges cancellation without reopening the Task.

- [ ] **Step 5: Implement reservation lifecycle**

At Task creation, `TaskAdmission.ai()` writes `reserved`. Admission counts reserved and consumed. The first accepted Pi run consumes the row and attaches `ai_run_id`; later receipts only add cost. A rule-only path releases the row. Usage API sums only consumed quantity and cost.

- [ ] **Step 6: Run migration, focused tests, and the complete backend suite**

Run: `node scripts/run-python.mjs migrate`
Expected: migration reaches revision `0012`.

Run: `.venv/bin/python -m pytest packages/api/tests/test_ai_receipts.py packages/api/tests/test_ai_cancellation.py packages/api/tests/test_task_state.py packages/api/tests/test_usage.py packages/api/tests/test_schema_constraints.py -q`
Expected: PASS.

Run: `node scripts/run-python.mjs test`
Expected: all backend tests pass.

- [ ] **Step 7: Commit**

```bash
git add packages/api/app packages/api/migrations/versions/0012_ai_receipts_and_usage_reservations.py packages/api/tests
git commit -m "feat(api): persist AI receipts and reservations"
```

---

### Task 4: Replace direct Intake Facts with asynchronous FactCandidates

**Files:**
- Modify: `packages/api/app/db/models.py`
- Create: `packages/api/migrations/versions/0013_intake_fact_candidates.py`
- Modify: `packages/api/app/modules/intake/schemas.py`
- Modify: `packages/api/app/modules/intake/router.py`
- Modify: `packages/api/app/modules/intake/service.py`
- Modify: `packages/api/app/workers/pipeline.py`
- Modify: `packages/api/app/main.py`
- Modify: `packages/api/tests/test_v2_intake.py`
- Create: `packages/api/tests/test_intake_ai_analysis.py`

**Interfaces:**
- Positive `POST /v1/intake-sessions/{id}/answers` returns HTTP 202 with `analysis_task_id`; negative and skipped answers remain synchronous and unmetered.
- `POST /v1/intake-sessions/{session_id}/fact-candidates/{candidate_id}/decision` accepts `{decision, value, base_version}`.
- `POST /v1/intake-sessions/{session_id}/analysis/retry` reuses the saved answer input version.
- `POST /v1/intake-sessions/{session_id}/analysis/continue` releases any unused reservation and advances with a rule question.

- [ ] **Step 1: Replace the old direct-Fact expectation with failing candidate tests**

The positive-answer test must assert:

```py
assert response.status_code == 202
assert await count(Fact) == 0
assert await count(FactCandidate) == 0
assert await count(Task) == 1
assert await count(Outbox) == 1
assert response.json()["analysis_status"] == "queued"
```

Add parameterized negative inputs `["没有", "不知道", "不清楚"]` and skipped input; assert zero Task, zero UsageLedger, zero FactCandidate, and zero Fact.

Run: `.venv/bin/python -m pytest packages/api/tests/test_v2_intake.py packages/api/tests/test_intake_ai_analysis.py -q`
Expected: FAIL because a positive answer still creates a Fact synchronously.

- [ ] **Step 2: Add migration 0013**

Create `fact_candidates` with the fields and owner constraints from PRD 7.6.1, including `decision_mode=accept_or_edit|edit_only` and optional `decision_source_id`.

Extend `intake_answers` with:

```text
analysis_status idle|queued|running|waiting_for_confirmation|failed|completed
analysis_task_id
analysis_input_version
next_question_source rule|model|fallback
```

Add owner-scoped FKs to Task and AiRun. Add the query indexes used by session response and Worker claim.

- [ ] **Step 3: Queue positive answer analysis in the answer transaction**

Save `IntakeAnswer`, Task, reserved UsageLedger, TaskEvent, and Outbox in the existing idempotency transaction. Do not create SourceRecord or Fact yet. Clear the current question while analysis is queued so the answer cannot be submitted twice.

- [ ] **Step 4: Implement `process_answer_analysis`**

Call `analyze_intake_answer` with the immutable answer version. Validate every result by:

```text
source_answer_id equals the saved answer
0 <= start < end <= len(answer)
answer[start:end] hashes to source_hash
value numbers are present in the source slice
negative answers yield zero positive candidates
duplicate kind/value/source tuples collapse to one
```

Persist the receipt, valid candidates, next question, answer status, and Task terminal state in one transaction. Invalid model entries become trace validation events only.

- [ ] **Step 5: Implement candidate decision semantics**

`accept` is allowed only for `decision_mode=accept_or_edit`; it creates `SourceRecord(source_type="question_answer")`, a confirmed Fact, and FactSource using the original range.

`edit` requires a changed non-empty value; it creates `SourceRecord(source_type="fact_candidate_edit")` whose content is the edited value and a full-range FactSource.

`reject` creates no SourceRecord or Fact. All decisions are idempotent and owner-filtered.

- [ ] **Step 6: Implement failed-analysis retry and rule continuation**

Retry requires the same answer ID, input version, and hash; it creates no second answer. Rule continuation preserves the saved answer, releases an unused reservation, records `next_question_source=fallback`, and advances deterministically.

- [ ] **Step 7: Run tests and commit**

Run: `.venv/bin/python -m pytest packages/api/tests/test_v2_intake.py packages/api/tests/test_intake_ai_analysis.py -q`
Expected: PASS.

Run: `node scripts/run-python.mjs test`
Expected: complete backend suite passes.

```bash
git add packages/api/app packages/api/migrations/versions/0013_intake_fact_candidates.py packages/api/tests
git commit -m "feat(intake): analyze answers into fact candidates"
```

---

### Task 5: Generate sourced Intake drafts through compose_resume_draft

**Files:**
- Modify: `packages/api/app/db/models.py`
- Create: `packages/api/migrations/versions/0014_resume_generation_provenance.py`
- Create: `packages/api/app/modules/resumes/fact_policy.py`
- Modify: `packages/api/app/modules/intake/schemas.py`
- Modify: `packages/api/app/modules/intake/router.py`
- Modify: `packages/api/app/modules/intake/service.py`
- Modify: `packages/api/app/workers/pipeline.py`
- Modify: `packages/api/tests/test_v2_intake.py`
- Create: `packages/api/tests/test_intake_ai_draft.py`

**Interfaces:**
- `IntakeDraftRequest` includes `generation_mode: "model"|"rule_fallback"`.
- Model mode calls `compose_resume_draft`; rule fallback is available only after model failure or when AI is not configured.
- `ResumeVersion` exposes `generation_mode`, `workflow_version`, `ai_run_id`, and `input_hash`.
- `fact_policy_check(text, claims, confirmed_fact_projection)` returns supported claims and deterministic issues.

- [ ] **Step 1: Add failing draft behavior tests**

Assert model output controls bullet text, every written claim has one or more confirmed fact refs, unsupported claims are omitted, and a failed model creates zero Resume and zero ResumeVersion.

Add a separate test that explicitly submits `generation_mode="rule_fallback"` after failure and asserts literal Fact text plus `ResumeVersion.generation_mode == "rule_fallback"`.

Run: `.venv/bin/python -m pytest packages/api/tests/test_intake_ai_draft.py -q`
Expected: FAIL because draft processing copies facts and has no provenance.

- [ ] **Step 2: Add migration 0014**

Add nullable provenance columns to `resume_versions`; backfill existing rows to `generation_mode="manual"` and then make generation mode non-null with a check over `manual|model|rule_fallback`.

- [ ] **Step 3: Centralize the deterministic fact policy**

Move the reusable high-risk entity, numeric, claim-range, confirmed status, and source coverage checks into `fact_policy.py`. Export and suggestion services may continue calling their current helpers until Task 7, but the new function must be behavior-tested with hand-derived fixtures.

- [ ] **Step 4: Implement model draft generation**

Pass only confirmed, sourced facts grouped by source/experience. Generate FastAPI section, bullet, and operation IDs. For every supported atomic claim, create BulletFactLink with the exact claim range and immutable fact/source snapshots. The receipt, Resume, ResumeVersion, VersionOperation, BulletFactLinks, IntakeSession, and Task success commit together.

- [ ] **Step 5: Implement explicit fallback**

Never silently fall back after a provider or schema error. The failed Task leaves the session recoverable. A later explicit rule-fallback request creates a new Task with an unmetered/released reservation path and records provenance.

- [ ] **Step 6: Verify and commit**

Run: `.venv/bin/python -m pytest packages/api/tests/test_intake_ai_draft.py packages/api/tests/test_v2_intake.py packages/api/tests/test_exports.py -q`
Expected: PASS.

```bash
git add packages/api/app packages/api/migrations/versions/0014_resume_generation_provenance.py packages/api/tests
git commit -m "feat(intake): compose sourced resume drafts"
```

---

### Task 6: Persist sourced JD candidates and parsing provenance

**Files:**
- Modify: `packages/api/app/db/models.py`
- Create: `packages/api/migrations/versions/0015_jd_provenance.py`
- Modify: `packages/api/app/modules/jobs/router.py`
- Modify: `packages/api/app/modules/jobs/service.py`
- Modify: `packages/api/app/workers/pipeline.py`
- Modify: `packages/api/tests/test_matching.py`
- Create: `packages/api/tests/test_job_ai_parse.py`

**Interfaces:**
- Requirement response includes `source_range`, `source_hash`, `explicitness`, `confidence_band`, `generation_mode`, `workflow_version`, `ai_run_id`, and `input_hash`.
- Model results never supply database IDs.
- Rule fallback line parsing computes exact offsets into the original JD and records `generation_mode="rule_fallback"`.

- [ ] **Step 1: Add failing range and provenance tests**

Test a JD containing repeated text so the implementation cannot use a naive first `str.find` for every requirement. Assert each stored slice equals the expected occurrence and hashes correctly.

Test an AI requirement whose range does not contain its value; assert Task failure and zero public requirements.

Run: `.venv/bin/python -m pytest packages/api/tests/test_job_ai_parse.py -q`
Expected: FAIL because requirements lack ranges and provenance.

- [ ] **Step 2: Add migration 0015**

Add the fields from the interface to `jd_requirements`, with checks for explicitness, confidence, and generation mode. Existing rows backfill as rule fallback with deterministic hashes where raw JD still exists.

- [ ] **Step 3: Implement typed parse_jd and rule fallback paths**

Model path persists one receipt and validated candidate rows in the Task final transaction. AI-unconfigured path performs deterministic line parsing, releases the reservation, and writes rule-fallback provenance. Provider/schema failure remains a failed AI Task and does not silently claim rule success.

- [ ] **Step 4: Verify and commit**

Run: `.venv/bin/python -m pytest packages/api/tests/test_job_ai_parse.py packages/api/tests/test_matching.py -q`
Expected: PASS.

```bash
git add packages/api/app packages/api/migrations/versions/0015_jd_provenance.py packages/api/tests
git commit -m "feat(jobs): persist sourced JD candidates"
```

---

### Task 7: Orchestrate match and batch suggestions without partial publication

**Files:**
- Modify: `packages/api/app/db/models.py`
- Create: `packages/api/migrations/versions/0016_match_suggestion_provenance.py`
- Modify: `packages/api/app/modules/matching/router.py`
- Modify: `packages/api/app/modules/matching/service.py`
- Modify: `packages/api/app/modules/suggestions/service.py`
- Modify: `packages/api/app/workers/pipeline.py`
- Modify: `packages/api/tests/test_matching.py`
- Modify: `packages/api/tests/test_suggestions.py`
- Create: `packages/api/tests/test_match_ai_orchestration.py`

**Interfaces:**
- Match response uses business categories only and includes generation provenance and `updated_at`.
- Suggestion response includes `original_hash`, generation provenance, requirement, facts, reason, risk flags, and decision status.
- One Task uses workflow stages exactly `match` and `suggestions`.

- [ ] **Step 1: Add failing two-run and no-partial-result tests**

Use a fixture client that returns a succeeded match receipt and a failed suggestion receipt. Assert:

```py
assert await count(AiRun) == 2
assert await count(MatchItem) == 0
assert await count(Suggestion) == 0
assert await count(SuggestionFactLink) == 0
assert task.status == "failed"
```

For success, assert both runs share `trace_id`, have distinct stable IDs, and each confirmed requirement maps to exactly one MatchItem.

Run: `.venv/bin/python -m pytest packages/api/tests/test_match_ai_orchestration.py -q`
Expected: FAIL because only match is called and suggestions are locally concatenated.

- [ ] **Step 2: Add migration 0016**

Add generation provenance and input hashes to MatchAnalysis, MatchItem, and Suggestion. Add `resume_target_paths` and `reason_code` to MatchItem. Existing rows backfill `rule_fallback`.

- [ ] **Step 3: Implement stage one**

Load only confirmed requirements and the immutable ResumeVersion evidence projection. Call `match_resume_to_jd`, validate exact requirement coverage and refs, persist its receipt without publishing MatchItems, settle the active run, and report Task progress below 100.

- [ ] **Step 4: Implement stage two and final publication**

Pass only transferable and needs-evidence items to `generate_suggestions_batch`. Validate original hashes, target paths, requirements, fact refs, claims, and proposed status. Apply the fixed category mapping.

In one final transaction persist the second receipt, MatchItems, Suggestions, SuggestionFactLinks, TaskEvent, and Task success. Enforce:

```text
real_gap -> no suggestion
needs_confirmation -> blocked only
underexpressed + complete confirmed evidence -> pending
unknown refs or unsupported claims -> Task failed, no public results
```

- [ ] **Step 5: Reuse fact policy at decision time**

Blocked suggestions cannot be accepted or edited. Pending suggestions are revalidated against confirmed Facts and original hash before creating an immutable job-targeted ResumeVersion.

- [ ] **Step 6: Verify and commit**

Run: `.venv/bin/python -m pytest packages/api/tests/test_match_ai_orchestration.py packages/api/tests/test_matching.py packages/api/tests/test_suggestions.py -q`
Expected: PASS.

Run: `node scripts/run-python.mjs test`
Expected: complete backend suite passes.

```bash
git add packages/api/app packages/api/migrations/versions/0016_match_suggestion_provenance.py packages/api/tests
git commit -m "feat(matching): publish AI suggestions atomically"
```

---

### Task 8: Generate the public contract and connect the Web state machines

**Files:**
- Modify: `packages/api/app/modules/intake/schemas.py`
- Modify: `packages/api/app/modules/jobs/router.py`
- Modify: `packages/api/app/modules/matching/router.py`
- Generate: `packages/shared/generated/openapi.json`
- Generate: `packages/shared/generated/schema.ts`
- Modify: `app/web/app/create/page.tsx`
- Modify: `app/web/app/jobs/new/page.tsx`
- Modify: `app/web/app/jobs/[id]/match/page.tsx`
- Modify: `app/web/app/suggestions/[analysisId]/page.tsx`
- Modify: `app/web/app/tasks/page.tsx`
- Modify: `app/web/tests/create-page.test.tsx`
- Modify: `app/web/tests/v2-workflows.test.tsx`
- Modify: `app/web/tests/v2-real-pages.test.tsx`

**Interfaces:**
- Web consumes only generated FastAPI schemas.
- Create-page server states include queued/running/waiting-for-confirmation/failed and candidate decisions.
- Rule fallback is always labeled `基础整理` or `基础解析`, never `AI 已完成`.

- [ ] **Step 1: Add failing create-page state tests**

Test the exact visible sequence:

```text
POST positive answer returns 202
page displays “正在整理这段经历”
Task succeeds
session reload displays candidate source excerpt
accept creates a confirmed Fact summary
```

Also test analysis failure buttons `重试整理` and `继续回答下一题`, and explicit draft fallback `使用事实原文创建基础草稿`.

Run: `pnpm --filter @resume/web exec vitest run tests/create-page.test.tsx`
Expected: FAIL because the page treats answer submission as synchronous Fact creation.

- [ ] **Step 2: Add failing JD, match, suggestion, and task tests**

Assert JD confidence/range and generation label, match generation mode/update time, batch navigation, and all six suggestion evidence fields. For blocked suggestions:

```ts
expect(screen.queryByRole("button", { name: "接受建议" })).not.toBeInTheDocument();
expect(screen.queryByRole("button", { name: "编辑后接受" })).not.toBeInTheDocument();
expect(screen.getByRole("button", { name: "补充事实" })).toBeEnabled();
expect(screen.getByRole("button", { name: "忽略建议" })).toBeEnabled();
```

Run: `pnpm --filter @resume/web exec vitest run tests/v2-workflows.test.tsx tests/v2-real-pages.test.tsx`
Expected: FAIL on missing provenance and blocked decision behavior.

- [ ] **Step 3: Generate OpenAPI and implement the Web state machines**

Run `pnpm generate` after the FastAPI schemas are complete. Do not edit generated files by hand.

Create page polls the returned analysis Task, reloads the server session, and renders candidate decision controls. JD and match pages render provenance from the API. Suggestions support next/previous selection; blocked items only expose add-evidence and ignore actions. Task result routing includes answer analysis and match Tasks.

- [ ] **Step 4: Run Web verification**

Run: `pnpm --filter @resume/web test`
Expected: all Web tests pass.

Run: `pnpm --filter @resume/web lint`
Expected: zero lint errors.

Run: `pnpm --filter @resume/web build`
Expected: production build exits 0.

- [ ] **Step 5: Commit**

```bash
git add packages/api/app/modules packages/shared/generated app/web
git commit -m "feat(web): connect AI orchestration states"
```

---

### Task 9: Add cross-service acceptance evidence and complete verification

**Files:**
- Modify: `scripts/dev-supervisor.mjs`
- Modify: `scripts/tests/dev-supervisor.test.mjs`
- Create: `tests/contract/ai-orchestration-v2.test.ts`
- Create: `tests/e2e/ai-orchestration-v2.spec.ts`
- Create: `scripts/acceptance/capture-ai-orchestration-v2.mjs`
- Create: `scripts/acceptance/hash-ai-orchestration-v2.mjs`
- Create: `docs/superpowers/specs/ai-resume-assistant-v2/evidence/ai-orchestration-v2/verification.md`
- Modify: `docs/superpowers/specs/ai-resume-assistant-v2/04-delivery-scope-and-status.md`
- Modify: `docs/superpowers/specs/ai-resume-assistant-v2/05-acceptance-and-evidence.md`

**Interfaces:**
- Dev supervisor waits for Pi, API, and Web readiness before reporting local readiness and names a failed subprocess with a safe error code.
- Contract test sends real HTTP between FastAPI and fixture-mode Pi; it does not mock field sets.
- Evidence status is only `PASS`, `FAIL`, or `BLOCKED`.

- [ ] **Step 1: Add failing supervisor and cross-service tests**

Supervisor test asserts no ready message before Pi readiness and an `ai` service name on exit. Contract tests exercise all five strict payloads and query Task→AiRun→Trace relations.

Run: `node --test scripts/tests/dev-supervisor.test.mjs`
Expected: FAIL on readiness sequencing.

Run: `pnpm test:contract`
Expected: FAIL because the V2 contract suite does not exist.

- [ ] **Step 2: Implement diagnostics and the real-service fixture harness**

Start the real Next, FastAPI, Worker, PostgreSQL/SQLite test DB, Redis, and Pi fixture processes. Browser tests must not intercept `/api/v1/**`. Use unique visible data for each run.

- [ ] **Step 3: Capture required local screenshots**

Capture create analysis, candidate confirmation, model draft provenance, JD provenance, match categories, pending suggestion, blocked suggestion, task success, and recoverable failure at 390×844, 1024×768, and 1440×900.

Store command, UTC time, commit, environment, API response, DB assertions, and SHA-256 manifest. Mark real-model accuracy, cloud, external browser, and user-study evidence `BLOCKED`.

- [ ] **Step 4: Run Hallmark and full repository gates**

Run: `pnpm lint`
Expected: exit 0.

Run: `pnpm test`
Expected: exit 0 with zero failures.

Run: `pnpm build`
Expected: exit 0.

Run: `pnpm acceptance`
Expected: all locally satisfiable acceptance checks pass; external gates remain explicitly BLOCKED.

Run the Hallmark 58-item slop test using the repository's `.hallmark` workflow. Expected: zero failed applicable items.

- [ ] **Step 5: Update status from evidence only**

Change a delivery row to `DONE-LOCAL` only when its code, focused tests, real-service screenshot, API/DB/trace assertions, and manifest share the same commit. Do not mark real-model or external gates PASS from fixture evidence.

- [ ] **Step 6: Commit**

```bash
git add scripts tests docs/superpowers/specs/ai-resume-assistant-v2
git commit -m "test: verify AI orchestration V2.1"
```
