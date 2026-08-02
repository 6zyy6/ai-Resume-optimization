# Task 5 quality review report

Date: 2026-08-02 (Asia/Shanghai)

Base commit: `2eee903 fix(intake): enforce draft safety boundaries`

## Scope

This round closes three Important findings:

1. Responsibility strength could be borrowed across unrelated semantic topics when one claim referenced multiple facts.
2. A cancelled in-flight draft whose cancellation receipt was lost could leave the Intake session, claim, active run, and private draft snapshot stranded.
3. Outbox dispatch exhaustion bypassed TaskService terminal cleanup and left a draft Intake session and private snapshot stranded.

## TDD evidence

### I1: per-topic responsibility evidence

RED command:

```bash
.venv/bin/python -m pytest packages/api/tests/test_intake_ai_draft.py::test_fact_policy_does_not_borrow_strong_responsibility_across_topics packages/api/tests/test_exports.py::test_export_service_blocks_written_responsibility_inflation -q
```

RED output:

```text
FF...FF                                                                  [100%]
4 failed, 3 passed in 0.99s
```

The Chinese and English `fact_policy_check` cases incorrectly persisted the claim, and both real `ExportService` cases completed instead of raising `EXPORT_BLOCKED_BY_FACTS`.

GREEN command:

```bash
.venv/bin/python -m pytest packages/api/tests/test_intake_ai_draft.py::test_fact_policy_does_not_borrow_strong_responsibility_across_topics packages/api/tests/test_exports.py::test_export_service_blocks_written_responsibility_inflation -q
```

GREEN output:

```text
.......                                                                  [100%]
7 passed in 0.89s
```

### I2: cancelled draft with lost receipt

### I3: draft Outbox exhaustion

The two independent real-path regressions were run together after both tests were added and before either production fix.

RED command:

```bash
.venv/bin/python -m pytest packages/api/tests/test_v2_intake.py::test_cancelled_draft_timeout_reconciles_intake_without_receipt packages/api/tests/test_v2_intake.py::test_draft_outbox_exhaustion_recovers_intake_and_scrubs_snapshot -q
```

RED output:

```text
FF                                                                       [100%]
2 failed in 0.36s
```

Both failures observed `('drafting', 1)` instead of the required recoverable `('active', 2)` state. The I2 test uses the real `TaskExecutor` and a client that registers the run, requests cancellation, then raises `TimeoutError`. The I3 test uses the real `OutboxDispatcher` and exhausts all three publisher attempts.

GREEN command:

```bash
.venv/bin/python -m pytest packages/api/tests/test_v2_intake.py::test_cancelled_draft_timeout_reconciles_intake_without_receipt packages/api/tests/test_v2_intake.py::test_draft_outbox_exhaustion_recovers_intake_and_scrubs_snapshot -q
```

GREEN output:

```text
..                                                                       [100%]
2 passed in 0.26s
```

## Implementation

- Responsibility evidence is split into coordinated semantic fragments. Each claim fragment inherits the declared responsibility level and must find sufficient strength only in evidence fragments about the same topic.
- The draft-only cancelled-operation reconciliation restores Intake, removes `draft_snapshot`, clears the Task claim and `active_ai_run_id`, and preserves a consumed UsageLedger for a run that was registered before the lost receipt.
- Outbox exhaustion now restores an undispatched draft Intake session and removes only `draft_snapshot`; `generation_mode` and `draft_input_hash` remain available for explicit fallback provenance.
- Generic cancelled timeout behavior remains unchanged for non-draft tasks.

## Focused regression evidence

Command:

```bash
.venv/bin/python -m pytest packages/api/tests/test_intake_ai_draft.py packages/api/tests/test_exports.py packages/api/tests/test_v2_intake.py packages/api/tests/test_task4_sql_integration.py::test_quality_rejects_responsibility_strength_inflation packages/api/tests/test_task_recovery.py packages/api/tests/test_task_state.py packages/api/tests/test_intake_ai_analysis.py::test_outbox_exhaustion_atomically_unblocks_intake_recovery -q
```

Output:

```text
........................................................................ [ 67%]
...................................                                      [100%]
107 passed in 7.13s
```

Formatting check:

```bash
git diff --check
```

Output: clean (no output).

## Risks and limits

- Responsibility fragments are deterministically split on common Chinese and English coordination punctuation/conjunctions. This is intentionally conservative and does not attempt general-purpose linguistic parsing.
- A registered run with a lost cancellation receipt remains charged as consumed in UsageLedger, while the Task's active coordination pointer is cleared so business recovery is not blocked.
- This is focused local evidence. It does not claim real provider, cloud queue, or production PostgreSQL acceptance.
