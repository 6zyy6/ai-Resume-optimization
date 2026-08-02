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

## Round 3: conservative responsibility delimiters

Base commit: `5f3ef73 fix(intake): close draft quality recovery gaps`

### Finding

The fragment regex treated the Chinese characters `和`, `及`, and `与` as delimiters anywhere inside a word. It also skipped an empty marker-only fragment before updating inherited responsibility strength, and did not recognize the explicit list separators `&` and `/`. These paths let stronger responsibility be borrowed across topics or erase the claim's responsibility level.

### RED

Command:

```bash
.venv/bin/python -m pytest packages/api/tests/test_intake_ai_draft.py::test_fact_policy_does_not_borrow_strong_responsibility_across_topics packages/api/tests/test_exports.py::test_export_service_blocks_written_responsibility_inflation -q
```

Output:

```text
..FFF.....FFF                                                            [100%]
6 failed, 7 passed in 1.06s
```

The failures reproduced all three bypasses through both `fact_policy_check` and a persisted version processed by the real `ExportService`:

- `负责和平项目` backed by `参与和平项目`;
- `managed customer research & market analysis` backed by weak research plus strong market evidence;
- `负责用户调研/市场分析` backed by weak research plus strong market evidence.

### GREEN

The splitter now recognizes only explicit punctuation/list delimiters (`、，,;；。`, `&/＆`, and `/／`), whitespace-delimited English `and`, and the complete Chinese conjunction `以及`. It no longer splits arbitrary Chinese words on a single character. Responsibility strength is updated before a marker-only empty subject is skipped, so a following listed topic inherits the declared level.

Command:

```bash
.venv/bin/python -m pytest packages/api/tests/test_intake_ai_draft.py::test_fact_policy_does_not_borrow_strong_responsibility_across_topics packages/api/tests/test_exports.py::test_export_service_blocks_written_responsibility_inflation -q
```

Output:

```text
.............                                                            [100%]
13 passed in 0.97s
```

Focused regression command:

```bash
.venv/bin/python -m pytest packages/api/tests/test_intake_ai_draft.py packages/api/tests/test_exports.py packages/api/tests/test_task4_sql_integration.py::test_quality_rejects_responsibility_strength_inflation -q
```

Output:

```text
.............................................                            [100%]
45 passed in 1.58s
```

Risk: ambiguous unspaced single-character Chinese conjunctions are intentionally not split. This avoids corrupting ordinary words such as `和平`; explicit punctuation, `以及`, `&`, or `/` remains supported for deterministic multi-topic validation.

## Round 4: directional responsibility coverage

Base commit: `23fe6c6 fix(resumes): harden responsibility fragment parsing`

### Finding

An unsplit Chinese compound subject still used symmetric subject equivalence when selecting responsibility-strength evidence. For `负责用户调研和市场分析`, the weak compound fact `参与用户调研和市场分析` and the strong partial fact `负责市场分析` were both treated as equivalent; taking their maximum let the market-analysis fact authorize responsibility for user research. `及` and `与` had the same bypass through both `fact_policy_check` and persisted-version `ExportService.process_export`.

### RED

Command:

```bash
.venv/bin/python -m pytest packages/api/tests/test_intake_ai_draft.py::test_fact_policy_does_not_borrow_strong_responsibility_across_topics packages/api/tests/test_exports.py::test_export_service_blocks_written_responsibility_inflation -q
```

Output:

```text
.....FFF........FFF                                                      [100%]
FAILED packages/api/tests/test_intake_ai_draft.py::test_fact_policy_does_not_borrow_strong_responsibility_across_topics[负责用户调研和市场分析-facts5]
FAILED packages/api/tests/test_intake_ai_draft.py::test_fact_policy_does_not_borrow_strong_responsibility_across_topics[负责用户调研及市场分析-facts6]
FAILED packages/api/tests/test_intake_ai_draft.py::test_fact_policy_does_not_borrow_strong_responsibility_across_topics[负责用户调研与市场分析-facts7]
FAILED packages/api/tests/test_exports.py::test_export_service_blocks_written_responsibility_inflation[负责用户调研和市场分析-evidences8-zh-he-conjunction]
FAILED packages/api/tests/test_exports.py::test_export_service_blocks_written_responsibility_inflation[负责用户调研及市场分析-evidences9-zh-ji-conjunction]
FAILED packages/api/tests/test_exports.py::test_export_service_blocks_written_responsibility_inflation[负责用户调研与市场分析-evidences10-zh-yu-conjunction]
6 failed, 13 passed in 1.39s
```

The policy failures returned a persisted `SupportedClaim`; the export failures completed without raising `EXPORT_BLOCKED_BY_FACTS`.

### GREEN

Command:

```bash
.venv/bin/python -m pytest packages/api/tests/test_intake_ai_draft.py::test_fact_policy_does_not_borrow_strong_responsibility_across_topics packages/api/tests/test_exports.py::test_export_service_blocks_written_responsibility_inflation -q
```

Output:

```text
...................                                                      [100%]
19 passed in 1.29s
```

### Implementation rationale

Responsibility strength now uses directional semantic coverage: an evidence subject contributes its strength only when its high-risk semantic terms cover the complete claim subject. A strong fact for only `市场分析` therefore cannot contribute strength to the compound subject `用户调研和市场分析`; the covering compound fact remains at the weaker `参与` level and the claim is rejected.

This changes the evidence-selection rule rather than adding `和`, `及`, or `与` to the delimiter regex. Existing explicit punctuation, whitespace-delimited `and`, `&`, `/`, inherited marker strength, single-fact inflation, `和平项目`, and safe responsibility downgrade behavior remain on their existing paths.

Files changed:

- `packages/api/app/modules/resumes/quality.py`
- `packages/api/tests/test_intake_ai_draft.py`
- `packages/api/tests/test_exports.py`
- `.superpowers/sdd/2026-07-30-ai-business-orchestration-v2-1/task-5-report.md`

### Focused regression evidence

Command:

```bash
.venv/bin/python -m pytest packages/api/tests/test_intake_ai_draft.py packages/api/tests/test_exports.py packages/api/tests/test_v2_intake.py packages/api/tests/test_task4_sql_integration.py::test_quality_rejects_responsibility_strength_inflation packages/api/tests/test_task_recovery.py packages/api/tests/test_task_state.py packages/api/tests/test_intake_ai_analysis.py::test_outbox_exhaustion_atomically_unblocks_intake_recovery -q
```

Output:

```text
........................................................................ [ 60%]
...............................................                          [100%]
119 passed in 7.68s
```

### Risks and limits

- The rule intentionally fails closed: an unspaced Chinese compound subject is not upgraded by strong evidence that covers only part of that subject. This avoids word-boundary guesses and preserves ordinary words such as `和平`.
- Strong support distributed across separate facts for an unspaced `和`/`及`/`与` compound may be conservatively rejected unless the claim uses an already explicit separator. This is preferable to cross-topic authorization at the central export boundary.
- Evidence is focused and local; it does not claim provider, cloud queue, or production PostgreSQL acceptance.
