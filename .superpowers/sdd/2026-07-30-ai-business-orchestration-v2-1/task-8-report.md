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

## Review fix round 1/5

Reviewer result: `0 Critical / 5 Important / 1 Minor`. All findings were reproduced with failing tests and fixed within the existing Task 8 Web files.

### RED evidence

Draft and answer-analysis recovery:

```text
pnpm --filter @resume/web exec vitest run tests/create-page.test.tsx
12 tests: 7 passed, 5 failed
```

The failures proved that fallback used the queued response version instead of the server's post-failure version, appeared after uncertain network failures, could not be recovered after refresh, and that answer-analysis polling had no same-page network recovery action.

Job and suggestion state machines:

```text
pnpm --filter @resume/web exec vitest run tests/v2-workflows.test.tsx
22 tests: 17 passed, 5 failed
```

The failures reproduced unhandled `processing` Jobs, non-rotating terminal match keys, uncertain match replay loss, missing suggestion generation provenance, and action/state resurrection after navigation.

### Fix rationale

- Draft Task failure is now handled separately from transport/session-read failure. A terminal failed Task must be followed by a successful IntakeSession re-read before fallback is offered; the refreshed server version is then used in the fallback request.
- Task transport uncertainty and successful-Task/session-read failure never expose fallback. Refreshing an active session with a persisted failed draft Task rechecks that Task and restores the explicit fallback option.
- Answer-analysis polling uncertainty renders `重新检查整理进度`; re-polling stays on the current page. A re-read terminal `failed` session still exposes the original retry/continue actions.
- Restored `queued` and `processing` Jobs resume their existing parse Task and never enqueue another parse request.
- Match creation retains its idempotency key across uncertain failures. It clears the key only after a follow-up Task read confirms `failed` or `cancelled`.
- Suggestion decision status is stored per item from the generated `DecisionResponse`. Navigation cannot restore a stale pending state. Pending allows A/E/I, blocked allows I plus add-evidence, accepted/edited/ignored allow Z, and reverted exposes no decision action.
- Suggestion provenance is visible as neutral `模型建议` or explicit `基础解析`; no fallback path is labeled `AI 已完成`.

### GREEN evidence

Focused review regressions:

```text
pnpm --filter @resume/web exec vitest run tests/create-page.test.tsx tests/v2-workflows.test.tsx tests/v2-real-pages.test.tsx
42 passed
```

Full Web verification:

```text
pnpm --filter @resume/web test
67 passed

pnpm --filter @resume/web lint
PASS

pnpm --filter @resume/web build
PASS
```

The build retained only the existing non-fatal `baseline-browser-mapping` age warning. User-owned `AGENTS.md` and both root Chinese documents remain excluded.

## Review fix round 2

Reviewer result: `0 Critical / 1 Important`. The remaining draft-continuity gap was reproduced before implementation.

### RED evidence

```text
pnpm --filter @resume/web exec vitest run tests/create-page.test.tsx
13 tests: 10 passed, 3 failed
```

The failures proved that a Task transport error or a successful Task followed by an IntakeSession read error left draft generation enabled without a same-page recovery action, and that a completed session restored from the URL did not open its resume.

A final terminal-state review added two more tests before the state model changed:

```text
pnpm --filter @resume/web exec vitest run tests/create-page.test.tsx
15 tests: 13 passed, 2 failed
```

Those failures proved that a confirmed cancelled Task and a terminally failed rule fallback were incorrectly classified as transport uncertainty, permanently disabling normal generation.

### Fix rationale

- Draft uncertainty now exposes only `重新检查草稿状态`, which replays the existing Task read and then reloads the same IntakeSession. It never creates a second draft or exposes fallback while the terminal state is unknown.
- Transport uncertainty is explicit component state rather than inferred from `active`/`drafting` plus `task_id`. A confirmed cancellation or non-model terminal failure clears uncertainty, keeps the error visible, and allows a new model generation from the server's incremented version.
- The draft POST is disabled in the UI and guarded in the handler while a persisted draft Task remains uncertain. A terminal retry uses the latest `base_version` and a new idempotency key.
- The recovery action retains whether the original model path was fallback-eligible, so retrying a failed rule fallback cannot expose a second fallback.
- A restored `completed` IntakeSession with `resume_id` automatically opens the immutable resume version for editing. A ref prevents duplicate navigation when the same completion is observed through polling and session state.

### GREEN evidence

```text
pnpm --filter @resume/web exec vitest run tests/create-page.test.tsx
15 passed

pnpm --filter @resume/web exec vitest run tests/create-page.test.tsx tests/v2-workflows.test.tsx tests/v2-real-pages.test.tsx
45 passed

pnpm --filter @resume/web test
70 passed

pnpm --filter @resume/web lint
PASS

pnpm --filter @resume/web build
PASS
```

The build retained only the existing non-fatal `baseline-browser-mapping` age warning. No styles or generated contracts changed; user-owned `AGENTS.md` and both root Chinese documents remain excluded.
