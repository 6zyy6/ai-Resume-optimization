# Task 7 Report: Publish AI match suggestions atomically

## Status

Implemented on `main` from task base `c2f672b`.

Matching now executes `match_resume_to_jd@2` followed by `generate_suggestions_batch@2` inside one claimed business Task and one AI usage reservation. The first terminal receipt is durably audited without exposing decision rows; the second receipt, match items, suggestions, evidence links, and Task success are committed atomically. Failure, cancellation, policy rejection, malformed output, or evidence drift leaves zero public match/suggestion rows.

## Success criteria

- Exactly two typed workflow stages share one Task trace and one usage reservation.
- Stage 1 may persist only audit/progress state; no public business rows are visible before stage 2 validates.
- Stage 2 receipt and all public rows publish in one transaction.
- Requirement coverage is exact and the fixed classification mapping is enforced.
- Suggestions are derived only from immutable ResumeVersion evidence and currently confirmed, sourced facts.
- Accept/edit revalidates the original hash, current facts, and final proposed claims; blocked suggestions cannot be accepted or edited.
- Public provenance is queryable, migration/backfill is honest, and private model bodies never enter Outbox or trace payloads.

## RED evidence

Initial two-stage orchestration tests:

```text
.venv/bin/python -m pytest packages/api/tests/test_match_ai_orchestration.py -q
2 failed
```

The old matching service called the V1 adapter with `workflow_type=...`; the typed fake rejected that interface, proving neither staged typed invocation nor atomic publication existed.

Blocked decision test:

```text
.venv/bin/python -m pytest packages/api/tests/test_suggestions.py::test_blocked_suggestion_rejects_accept_as_unconfirmed_evidence -q
1 failed
```

The old transition guard returned the generic `SUGGESTION_TRANSITION_INVALID` instead of the evidence-specific `FACT_NOT_CONFIRMED` failure.

Public provenance contract test:

```text
.venv/bin/python -m pytest packages/api/tests/test_matching.py -q
1 failed
```

The response omitted `generation_mode`, `ai_run_id`, `input_hash`, and `updated_at`.

Blocked-ignore regression test:

```text
.venv/bin/python -m pytest packages/api/tests/test_suggestions.py::test_blocked_suggestion_can_be_ignored -q
1 failed
```

The original state machine also rejected the allowed blocked-to-ignored transition.

## Implementation

- Added typed `match_resume_to_jd@2` and `generate_suggestions_batch@2` orchestration with deterministic input hashes/run IDs, one shared trace, stable stage-2 retry identity, and one reservation/usage settlement.
- Persisted stage-1 `AiRun` audit plus sub-100 progress without public rows; the terminal transaction persists stage-2 audit, match items, suggestions, evidence links, and Task success together.
- Added fail-closed validation for exact requirement coverage, duplicate/unknown references, allowed resume target paths, fixed class/status mapping, original hashes, evidence links, and atomically covered claims.
- Made the empty second-stage batch a valid typed invocation so the two-workflow invariant still holds when no suggestions are eligible.
- Bound matching to immutable ResumeVersion bullet/fact snapshots and current confirmed facts with real sources. Snapshot-declared `fact_refs` are not trusted as evidence.
- Reused the fact policy checker at decision time; fact drift, partial source drift, original-text drift, or unsupported edited claims create no new ResumeVersion.
- Kept deterministic no-AI operation explicitly labeled `rule_fallback` and unmetered; model execution never silently falls back after provider/schema/policy failure.
- Added public provenance fields to models/router/OpenAPI and regenerated shared artifacts only through `pnpm generate`.
- Added migration `0016_match_suggestion_provenance`, deterministic legacy backfill labeled `rule_fallback`, owner-scoped AI-run foreign keys, constraints/indexes, and guarded downgrade behavior.
- Limited Outbox payloads to IDs, mode, input hash, and Task ID; no complete prompt, model response, or private evidence snapshot is retained there or in trace events.
- Updated worker cancellation/terminal failure handling so all non-success terminal paths retain valid audit receipts when available but remove public rows.

## GREEN evidence

Focused Task 7 orchestration and migration coverage:

```text
.venv/bin/python -m pytest packages/api/tests/test_match_ai_orchestration.py -q
10 passed

.venv/bin/python -m pytest packages/api/tests/test_match_ai_orchestration.py::test_migration_0016_backfills_existing_business_rows_as_rule_fallback -q
1 passed
```

Contract regeneration and static verification:

```text
pnpm generate
PASS

pnpm lint
PASS

pnpm build
PASS
```

Final full regression on 2026-08-02 CST:

```text
pnpm test
API: 1377 passed in 68.22s
AI: 94 passed, 1 skipped
Shared: 8 passed
Design tokens: 2 passed
Miniprogram: 12 passed
Web: 53 passed
Dev supervisor: 2 passed
```

The API run emitted five non-fatal, pre-existing aiosqlite event-loop-close warnings. Build emitted the existing `baseline-browser-mapping` age warning and Taro cache-resolution warning; compilation completed successfully.

## Coverage

- Stage-1 success/stage-2 failure with two receipts and zero public rows
- Full two-stage success with shared trace, distinct stable run IDs, exact requirement coverage, one usage count, and atomic publication
- Stage-1 provider failure, stage-2 transport retry, unsupported claim, cancellation, malformed/duplicate/missing references, and empty suggestion batch
- Blocked accept/edit rejection and blocked ignore allowance
- Decision-time original-hash and fact/source drift
- Public provenance response and generated TypeScript schema
- Migration legacy backfill, fresh schema, downgrade round trip, and downgrade guard
- Outbox minimization and absence of private prompt/response bodies

## Risks and limits

- `matching/service.py` remains materially larger (net increase about 660 lines) because it now owns both typed stages, transaction boundaries, validation, deterministic fallback, and retry recovery. This is the main maintainability concern; no further refactor was attempted in this task because it would expand scope after correctness was established.
- No live model-provider, production PostgreSQL, Redis/Celery multi-process, or cloud deployment evidence was produced. Deterministic typed receipts and the real local SQL/Task/AiRun/usage code paths passed, but external-environment acceptance remains `BLOCKED` until those environments are exercised.
- Existing owner-alias records are authorized for reading by current repository rules, while new evidence link rows remain canonical-owner scoped by schema. Task 7 preserves that boundary rather than broadening the ownership model.
- User-owned `AGENTS.md` and the two root Chinese documents were preserved and excluded from the commit.
