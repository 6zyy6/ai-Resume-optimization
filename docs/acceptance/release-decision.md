# Release decision

## NOT READY FOR PUBLIC RELEASE

Candidate: `709d8f53ef5aed90189e4c7fc5979b614046090e`
Decision date: 2026-07-29
Evidence directory: `artifacts/acceptance/20260729033807-709d8f53/`

Locally proven for this candidate:

- `pnpm lint` exited 0.
- `pnpm test` exited 0 (all workspace suites and 435 API tests).
- `pnpm build` exited 0 for Web, Taro mini-program, API and Pi packages.
- Deployment contract (9 tests), shared workflow tests (8 tests), Web tests (16 tests), mini-program tests (12 tests), and focused API auth/trace tests (21 tests) exited 0.
- The acceptance manifest has exactly 146 IDs and passes the evidence verifier against the candidate SHA.

The manifest deliberately records all 146 acceptance items as `BLOCKED`, not `PASS`: the complete required evidence sets were not produced in this local environment. This is intentional evidence hygiene, not a test failure waiver.

Open defects:

| Severity | Count | Detail |
| --- | ---: | --- |
| Sev1 | 0 | None open in the independent code review. |
| Sev2 | 1 | In-flight Pi/provider cancellation is not yet propagated within the AI-12 five-second limit. |
| Sev3 | 0 | None recorded. |
| Sev4 | 0 | None recorded. |

External/P0 blockers:

- No Docker image build, image secret scan, or container topology test was available.
- Tencent Cloud deployment, TLS/network isolation, COS shared-storage flow, alerts, backup restore and rollout evidence were not run.
- No Safari/Edge browser matrix, WeChat developer-tools run, or iPhone/Android real-device evidence exists.
- No real-provider billing reconciliation, AI evaluation set, cancellation trace, or 30-student user validation exists.
- Every P0 acceptance item therefore remains `BLOCKED`; public release is prohibited by the acceptance specification.

Release may be reconsidered only after resolving the Sev2 cancellation contract and collecting the required same-commit external evidence. The final acceptance manifest must then move each P0 item to `PASS` with its required hashed evidence; no status may be inferred from this local run.
