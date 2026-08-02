# Task 8 Report: Connect Web AI orchestration state machines

## Status

Implemented on `main` from task base `6f77535`.

The Web now follows FastAPI-owned AI Tasks instead of treating answer analysis, JD parsing, match generation, or draft generation as synchronous UI work. Public provenance and decision states are generated from the FastAPI OpenAPI contract and consumed through shared generated types.

## RED evidence

Create-page answer orchestration:

```text
pnpm --filter @resume/web exec vitest run tests/create-page.test.tsx
6 tests: 1 failed
```

The existing page never displayed `正在整理这段经历` because it treated the answer response as a completed Fact update.

After adding failure and explicit fallback cases:

```text
8 tests: 2 failed
```

The missing controls were `重试整理` / `继续回答下一题` and `使用事实原文创建基础草稿`.

JD, match, suggestion, and task routing:

```text
pnpm --filter @resume/web exec vitest run tests/v2-workflows.test.tsx tests/v2-real-pages.test.tsx
25 tests: 21 passed, 4 failed
```

The four failures precisely identified missing suggestion navigation, JD provenance, match provenance, and answer/match Task result routes.

## Implementation

- Answer submission now renders queued/running analysis, polls the returned Task, reloads the server session after success, and resumes an in-flight analysis after reload.
- Candidate review renders the proposed value, exact source excerpt/range, and generated decision mode. `edit_only` candidates never expose accept; accept/edit/reject use generated request/response schemas and stable idempotency keys.
- Failed analysis offers explicit same-input retry or deterministic rule-question continuation without discarding the saved answer.
- Model draft failure offers an explicit user-controlled `rule_fallback`; the UI labels it as a basic draft and never presents it as AI success.
- JD requirements expose confidence, source character range, and `模型解析` / `基础解析` provenance after the parsing Task reaches success and the Job is re-read.
- Match reports expose `模型匹配` / `基础解析` and the persisted update time after Task completion and analysis re-read.
- Suggestions support previous/next navigation and retain all six evidence surfaces: requirement, original text, proposed text, reason, linked Facts, and risk flags. Blocked suggestions expose only `补充事实` and `忽略建议`; keyboard A/E/Z are gated for blocked rows.
- Task result routing sends private answer-analysis references back to `/create` and public match analysis references to `/suggestions/{analysis_id}`.
- FastAPI response schemas now use explicit Literals for requirement types, Job/Match/Suggestion states, match categories, and generation modes. Hashes and source/claim ranges are constrained.
- `packages/shared/generated/openapi.json` and `schema.ts` were regenerated only through `pnpm generate`; Web-local duplicate decision types were removed.

## GREEN evidence

Focused UI state-machine coverage:

```text
pnpm --filter @resume/web exec vitest run tests/v2-workflows.test.tsx tests/v2-real-pages.test.tsx tests/create-page.test.tsx
33 passed
```

API schema and affected workflow regression:

```text
.venv/bin/python -m pytest packages/api/tests/test_v2_intake.py packages/api/tests/test_matching.py packages/api/tests/test_suggestions.py -q
57 passed
```

The focused API run emitted three non-fatal existing aiosqlite event-loop-close warnings.

Contract and Web verification:

```text
pnpm generate
PASS

pnpm --filter @resume/web test
58 passed

pnpm --filter @resume/web lint
PASS

pnpm --filter @resume/web build
PASS
```

The build emitted the existing non-fatal `baseline-browser-mapping` age warning.

Responsive browser regression:

```text
pnpm exec playwright test tests/e2e-web/zero-to-resume.spec.ts --grep 'critical pages preserve layout' --reporter=line
7 passed
```

The test rendered critical pages at 320, 375, 390, 414, 768, 1024, and 1440 CSS pixels and asserted no horizontal overflow, clipped display heading, or wrapped clickable control.

Final full regression on 2026-08-02 CST:

```text
pnpm test
API: 1386 passed
AI: 94 passed, 1 skipped
Shared: 8 passed
Design tokens: 2 passed
Miniprogram: 12 passed
Web: 58 passed
Dev supervisor: 2 passed
```

## Hallmark review

The existing modern-minimal `Cobalt · Guided Ledger` visual system was preserved; no parallel colors, spacing, fonts, or component abstractions were introduced. The post-build Hallmark 58-gate sweep passed with the existing CSS stamp intact. Interactive controls retain focus-visible, active, disabled, loading, error, and success handling, reduced-motion coverage, named-token usage, and mobile non-wrapping safeguards.

## Risks and limits

- The responsive Playwright check proves rendered geometry at all required widths, but Task 8 does not claim production/cloud, real-provider, or user-research acceptance. Those remain `BLOCKED` until the corresponding external evidence is captured.
- Chrome CDP was not started because the required setup would terminate the user's running Chrome process. The isolated project Playwright browser was used instead, so no user browser state was modified.
- Model-provider failure is represented through deterministic Task fixtures; no live DeepSeek request was made in this task.
- User-owned `AGENTS.md` and the two root Chinese documents were preserved and excluded from the commit.
