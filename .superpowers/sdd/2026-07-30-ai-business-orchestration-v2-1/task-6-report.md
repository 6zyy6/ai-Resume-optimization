# Task 6 Report: Persist sourced JD candidates and parsing provenance

## Status

Implemented on `main` from task base `68ca8b7`.

JD parsing now uses an immutable, hashed `parse_jd@2` input snapshot. Model parsing accepts only a typed terminal `AiExecutionReceipt`, validates every value against its exact source range, persists the receipt/AiRun/trace/usage and public candidates in one terminal transaction, and fails closed without rule fallback. When AI is genuinely unconfigured, deterministic line parsing records occurrence-aware offsets and releases the unused AI reservation.

## RED evidence

Command:

```text
.venv/bin/python -m pytest packages/api/tests/test_job_ai_parse.py -q
```

Initial result on 2026-08-02 CST:

```text
FFFFF
5 failed in 0.41s
```

The failures were the intended missing behavior: the old service called AI with legacy keyword arguments, and `JdRequirement` had no `source_start`/`source_end` or provenance fields.

The migration/schema cycle was also observed red before migration implementation:

```text
FFF.....
3 failed, 5 passed in 0.80s
```

The failures were the missing `0015` revision and missing model metadata/owner-scoped AI-run foreign key.

## Implementation

- Added structured JD provenance columns, validation constraints, and owner-scoped `AiRun` foreign key.
- Added migration `0015`, including deterministic occurrence-aware legacy backfill, honest low-confidence full-source fallback for legacy values not found verbatim, and a pre-DDL downgrade guard for newly generated provenance.
- Added typed `ParseJdRequest`/`AiExecutionReceipt` processing with deterministic run identity and immutable outbox input hash.
- Added exact range/value validation and FastAPI-owned SHA-256 calculation; model output never provides database IDs or hashes.
- Added terminal failure recovery to keep provider/schema errors at zero public candidates and to settle or release reservations correctly.
- Added rule fallback with line-local offsets so repeated identical lines map to distinct source occurrences.
- Exposed `source_range`, `source_hash`, `explicitness`, `confidence_band`, `generation_mode`, `workflow_version`, `ai_run_id`, and `input_hash` through OpenAPI; regenerated shared artifacts with `pnpm generate`.
- Updated only JD-related existing fixtures/assertions required by the new non-null/public contract.

## GREEN evidence

Focused Task 6 tests:

```text
.venv/bin/python -m pytest packages/api/tests/test_job_ai_parse.py -q
13 passed in 1.35s
```

Relevant receipt/cancellation/matching/schema regression:

```text
.venv/bin/python -m pytest packages/api/tests/test_job_ai_parse.py packages/api/tests/test_matching.py packages/api/tests/test_ai_receipts.py packages/api/tests/test_ai_cancellation.py packages/api/tests/test_schema_constraints.py -q
88 passed in 7.07s
```

Contract and build checks:

```text
pnpm generate
PASS

pnpm lint
PASS

pnpm build
PASS
```

The build emitted existing non-fatal `baseline-browser-mapping` age and Taro cache-resolution warnings.

The first full `pnpm test` run reached `1349 passed` with two JD contract fixture failures in `test_task7_review_fixes.py`; both were corrected without changing Task 7 production behavior. Final full-suite evidence is recorded after the final verification run below.

Final full regression on 2026-08-02 14:01 CST:

```text
pnpm test
API: 1351 passed in 63.58s
AI: 93 passed, 1 skipped
Shared: 8 passed
Design tokens: 2 passed
Miniprogram: 12 passed
Web: 53 passed
Dev supervisor: 2 passed
```

The Task 7 test-file edits are contract compatibility only: one legacy fixture now supplies the new required non-null JD provenance columns, and one exact JSON assertion now validates the added public provenance instead of rejecting additional contract fields. No Task 7 production code changed.

## Coverage

- Happy model receipt and public provenance
- Repeated identical fallback lines and exact occurrence offsets/hashes
- Out-of-bounds and value-mismatched model source ranges
- AI-unconfigured rule fallback and released reservation
- Failed provider receipt with persisted audit and no fallback rows
- Provider transient retry success and retry exhaustion
- Owner mismatch isolation
- Final-transaction rollback and deterministic replay recovery
- Cancellation after AI run registration with zero published rows
- Migration backfill, empty/new schema behavior, downgrade round trip, and downgrade refusal

## Risks and limits

- Legacy rows whose text is no longer a literal substring of the retained raw JD are backfilled against the full raw source with `explicitness=implicit` and `confidence_band=low`; they remain unconfirmed and are not falsely represented as exact matches.
- No real model or cloud-provider call was made; typed deterministic receipts exercise the real service/Task/AiRun/usage transaction boundaries.
- User-owned `AGENTS.md` and root Chinese documents were preserved and excluded from the commit.
