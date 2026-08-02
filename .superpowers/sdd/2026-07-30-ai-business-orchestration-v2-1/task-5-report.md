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

## Round 5: fail closed without one covering evidence item

Base commit: `ff00ec4 fix(resumes): require covering responsibility evidence`

### Finding and root cause

Directional subject coverage from Round 4 selected only evidence facts whose high-risk terms covered the complete responsibility subject, but `responsibility_claim_supported` rejected only when that selection was non-empty and too weak. For `负责用户调研和市场分析`, the two weak facts `参与用户调研和需求访谈` and `参与竞品调研和市场分析` jointly covered all high-risk terms downstream while neither fact individually covered the complete subject. The empty strength selection therefore returned success and both policy and export accepted a responsibility upgrade.

### RED

The exact production counterexample was first added to both the deterministic policy and persisted real export path.

Command:

```bash
.venv/bin/python -m pytest packages/api/tests/test_intake_ai_draft.py::test_fact_policy_does_not_borrow_strong_responsibility_across_topics packages/api/tests/test_exports.py::test_export_service_blocks_written_responsibility_inflation -q
```

Output:

```text
......F...........F..                                                    [100%]
2 failed, 19 passed in 1.52s
```

The policy failure returned a `SupportedClaim`; the real `ExportService.process_export` failure completed without raising `EXPORT_BLOCKED_BY_FACTS`.

The helper contract was also changed from allowing unrelated evidence to requiring a covering evidence item and observed RED independently:

```text
....F                                                                    [100%]
1 failed, 4 passed in 0.06s
```

### GREEN and rationale

The helper now returns false when a responsibility-bearing subject has no single covering evidence fragment, as well as when covering evidence exists only at a weaker responsibility level. This is the smallest fail-closed change and prevents high-risk term unioning in either caller from converting multiple partial weak facts into strong responsibility support. Responsibility-free claims still bypass this strength check and continue to use the existing relation policy.

Command:

```bash
.venv/bin/python -m pytest packages/api/tests/test_intake_ai_draft.py::test_fact_policy_does_not_borrow_strong_responsibility_across_topics packages/api/tests/test_intake_ai_draft.py::test_responsibility_helper_requires_covering_subject_strength packages/api/tests/test_exports.py::test_export_service_blocks_written_responsibility_inflation -q
```

Output:

```text
..........................                                               [100%]
26 passed in 1.43s
```

### Focused regression evidence

Command:

```bash
.venv/bin/python -m pytest packages/api/tests/test_intake_ai_draft.py packages/api/tests/test_exports.py packages/api/tests/test_v2_intake.py packages/api/tests/test_task4_sql_integration.py::test_quality_rejects_responsibility_strength_inflation packages/api/tests/test_task_recovery.py packages/api/tests/test_task_state.py packages/api/tests/test_intake_ai_analysis.py::test_outbox_exhaustion_atomically_unblocks_intake_recovery -q
```

Output:

```text
........................................................................ [ 59%]
..................................................                       [100%]
122 passed, 3 warnings in 7.92s
```

The focused run emitted `aiosqlite` worker-thread teardown warnings from `test_model_draft_with_no_supported_claims_has_no_dangling_result_ref`; no focused assertion failed, and the later complete project test run passed without this pytest warning.

Project regression command:

```bash
pnpm test
```

Output summary:

```text
packages/ai: 93 passed, 1 skipped
packages/shared: 8 passed
packages/design-tokens: 2 passed
app/miniprogram: 12 passed
app/web: 53 passed
scripts/dev-supervisor: 2 passed
packages/api: 1338 passed in 58.91s
exit 0
```

The Web Vitest run emitted its existing Node experimental `localStorage` warning.

### Files changed

- `packages/api/app/modules/resumes/quality.py`
- `packages/api/tests/test_intake_ai_draft.py`
- `packages/api/tests/test_exports.py`
- `.superpowers/sdd/2026-07-30-ai-business-orchestration-v2-1/task-5-report.md`

### Risks and limits

- Responsibility-bearing claims with only partial or unrelated evidence now fail at the responsibility gate, which is intentionally conservative; responsibility-free unrelated claims still reach the downstream relation policy.
- The fix does not change subject tokenization, delimiter handling, cancellation, dispatch, or persistence architecture.
- Verification is local and focused; it does not claim real provider, cloud queue, or production PostgreSQL acceptance.
