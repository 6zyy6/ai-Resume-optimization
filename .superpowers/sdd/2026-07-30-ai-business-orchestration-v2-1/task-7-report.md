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

## Review fix round 1/5

Reviewer result: `0 Critical / 4 Important / 2 Minor`. All four Important findings were fixed. The two Minor findings remain explicitly deferred and were not broadened into this round.

### RED evidence

Confirmed/unconfirmed requirement isolation:

```text
.venv/bin/python -m pytest packages/api/tests/test_match_ai_orchestration.py::test_unconfirmed_requirements_are_excluded_from_both_model_stages -q
1 failed in 1.34s
```

The create path rejected a job containing any unconfirmed requirement instead of selecting only confirmed rows.

Final publication drift, using a stage-2 client that changed Fact status, the FactSource set, requirement confirmation, or requirement value before returning a valid receipt:

```text
.venv/bin/python -m pytest packages/api/tests/test_match_ai_orchestration.py::test_final_publication_revalidates_current_policy_state -q
4 failed in 0.45s
```

All four cases incorrectly published one MatchItem and succeeded.

Decision-time read/commit drift:

```text
.venv/bin/python -m pytest packages/api/tests/test_suggestions.py::test_decision_rechecks_locked_fact_state_before_version_commit -q
1 failed in 0.20s
```

A Fact changed after evidence loading, but the stale projection still created a ResumeVersion.

Repeated atomic claims and migration key shape:

```text
.venv/bin/python -m pytest packages/api/tests/test_match_ai_orchestration.py::test_repeated_fact_claims_keep_exact_ranges_through_acceptance -q
1 failed in 0.17s

.venv/bin/python -m pytest packages/api/tests/test_match_ai_orchestration.py::test_migration_0016_backfills_existing_business_rows_as_rule_fallback -q
1 failed in 0.43s
```

Only the first range survived for a repeated Fact, and migration `0016` still exposed the old `(suggestion_id, fact_id)` primary key.

### Fix rationale

- Both create and processing queries now filter `confirmed = true` in SQL. Mixed jobs proceed with only confirmed requirements; zero confirmed requirements still fail closed.
- The final stage-2 transaction locks and reloads every job requirement in stable order, filters the current confirmed set, locks/reloads immutable evidence links, Facts, and FactSource rows, then recomputes both workflow inputs and hashes. Any drift fails as `MATCH_PUBLICATION_STATE_CHANGED` after both receipts and the single usage settlement are persisted, with zero public rows.
- Suggestion decisions lock evidence links, linked Facts, and source rows in stable order. Fact policy evaluation, a same-transaction current-state recheck, and immutable ResumeVersion creation all occur while those locks are held.
- `SuggestionFactLink` now has exact `claim_start`/`claim_end` columns and a composite primary key `(suggestion_id, fact_id, claim_start, claim_end)` with a valid-range constraint. Migration `0016` backfills the existing JSON range exactly, rebuilds the key, and refuses lossy downgrade when duplicate fact ranges exist.
- Publication persists one link for every atomic claim/fact pair. Decision validation rebuilds atomic claims from those saved ranges, requires complete text coverage, and copies the revalidated exact ranges into the new `BulletFactLink` rows rather than expanding them to the full bullet.
- Suggestion responses now expose every audited `{fact_id, claim_range}` link; OpenAPI and shared TypeScript artifacts were regenerated through `pnpm generate`.

### GREEN evidence

Individual fixes:

```text
I1 confirmed-only stage inputs: 1 passed in 0.87s
I2 final state drift variants: 4 passed in 0.38s
I3 + I4 + migration round trip: 3 passed in 0.81s
```

One focused compatibility run found that edited text could append an uncovered tool after all stored atomic claims. The existing regression correctly failed (`1 failed, 106 passed`); complete claim coverage was restored, after which the focused suite passed:

```text
.venv/bin/python -m pytest packages/api/tests/test_match_ai_orchestration.py packages/api/tests/test_matching.py packages/api/tests/test_suggestions.py packages/api/tests/test_ai_cancellation.py packages/api/tests/test_task7_review_fixes.py packages/api/tests/test_schema_constraints.py -q
107 passed in 11.58s
```

Final verification on 2026-08-02 16:19 CST:

```text
pnpm generate
PASS

pnpm lint
PASS

pnpm build
PASS

pnpm test
API: 1384 passed in 68.41s
AI: 94 passed, 1 skipped
Shared: 8 passed
Design tokens: 2 passed
Miniprogram: 12 passed
Web: 53 passed
Dev supervisor: 2 passed
```

Build retained the existing non-fatal `baseline-browser-mapping` age and Taro cache-resolution warnings.

### Remaining limits

- Real provider, PostgreSQL row-lock behavior, Redis/Celery multi-process behavior, and cloud deployment evidence remain `BLOCKED`; local SQLite and typed deterministic receipt tests do not replace those external checks.
- The two reviewer Minor findings (broad-string policy matching and the large matching service) remain deferred as requested.
- User-owned `AGENTS.md` and both root Chinese documents remain untouched and excluded from this round.
