# Independent release review

Candidate reviewed: `709d8f53ef5aed90189e4c7fc5979b614046090e`
Reviewer: independent read-only checker
Scope: `origin/main...709d8f5`, implementation plan, acceptance specification and local evidence.

## Resolved findings

| ID | Severity | Finding | Fix and verification |
| --- | --- | --- | --- |
| C1 / I4 | Critical | Web login did not call the authentication API and cross-origin cookies were not configured. | Login now calls email start/verify, shared requests use `credentials: include`, and API CORS is allow-listed by environment. Web tests and build pass. |
| C2 | Critical | Web and mini-program version writes used empty `claim_evidence`, which the API rejects. | Both clients create a confirmed `user_edit` fact and provide full-range evidence; imported facts are linked by returned `fact_ids`. Shared workflow tests pass. |
| C3 / I3 | Critical | Async import, job, match and export flows advanced before task completion and displayed synthetic success content. | Shared task polling gates the next operation; affected Web and mini-program pages now wait for terminal success and present pending/failed states. |
| C4 | Critical | CloudBase workers had no Outbox dispatcher. | Added a one-replica `outbox-dispatcher` group and deployment contract assertion. |
| C5 | Critical | CloudBase API/workers omitted production, COS and internal Pi configuration. | API/workers now share explicit production/COS/internal-AI environment settings; contract test passes. |
| C6 | Critical | Pi CloudBase service defaulted to loopback-only binding. | Added `AI_HOST=0.0.0.0` and `AI_PORT=3101`, with a deployment assertion. |
| I1 | Important | Local command success could be recorded as an incomplete acceptance PASS. | The harness now marks every acceptance item `BLOCKED` until its complete, same-commit evidence set is present; it no longer promotes partial scans to PASS. |

RED evidence retained during this task:

- `pnpm exec vitest run tests/contract/deployment.test.ts` initially failed two assertions for the missing dispatcher and production runtime settings.
- `pnpm --filter @resume/shared exec vitest run tests/workflows.test.ts` initially failed because the workflow guards did not exist.

Both focused test suites pass after the consolidated fix wave.

## Open finding

| ID | Severity | Finding | Release impact |
| --- | --- | --- |
| I2 | Sev2 | A user cancellation marks the database task terminal, but does not yet signal an in-flight Pi/provider run to stop within five seconds. | Blocks public release and AI-12 PASS. It requires persisted Pi run identity plus a cancellable worker/Pi operation contract and a live-provider cancellation test. |

No other independent Critical or Important code finding was reported. Missing cloud, real-device, provider-billing and user-study evidence is not listed as a code defect; it remains an acceptance blocker below.
