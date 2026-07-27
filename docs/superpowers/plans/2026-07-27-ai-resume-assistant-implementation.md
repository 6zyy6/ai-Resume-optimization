# AI 大学生简历助手 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付一个 Web 与微信小程序业务能力对等、FastAPI 与 Pi 分工明确、可本地运行并能按验收 ID 生成证据的公开运营 MVP 候选版本。

**Architecture:** pnpm Monorepo 管理 Next.js Web、Taro 微信小程序、Pi Node 内部服务、共享契约和设计 Token；Python 3.12 FastAPI 模块化单体拥有业务事实、版本、任务与权限。PostgreSQL 是生产事实源，Redis/Celery 负责异步执行；测试环境使用同一业务接口的内存适配器，使无云凭据的开发机也能跑完确定性验收，真实云、真机、模型账单和用户研究仍必须在发布环境复验。

**Tech Stack:** Node.js 26、pnpm 11、TypeScript、Next.js App Router、React、Taro、FastAPI、Pydantic 2、SQLAlchemy async、Alembic、Celery、Redis、PostgreSQL、Fastify、TypeBox、`@earendil-works/pi-ai`、`@earendil-works/pi-agent-core`、Vitest、pytest、Playwright、k6。

## Global Constraints

- 公开 MVP 容量基线是 1,000 DAU、100 峰值在线、50 RPS、10–20 AI 并发、5–10 文件任务并发。
- Web 与微信小程序同时上线且业务结果对等；页面 UI 不跨端共享。
- FastAPI 是唯一公开业务入口；Pi 不直连业务数据库，不执行 Shell、文件系统、浏览器或任意网络工具。
- 事实状态只允许 `unconfirmed`、`confirmed`、`rejected`；未确认、无来源或新增数字的声明不得导出。
- 每用户每日最多 20 个 AI 任务、同时运行 2 个；全局每日 AI 成本上限 100 元，70%/90%/100% 执行告警、降级、停止新 AI 任务。
- 写请求使用 `Idempotency-Key`；相同键与相同请求 24 小时内复用响应，相同键不同请求体返回 `409 IDEMPOTENCY_KEY_REUSED`。
- 自动保存停止输入 800 ms 后提交，15 秒兜底；冲突使用 `base_version` 并返回 `409 RESUME_VERSION_CONFLICT`，不得静默最后写入覆盖。
- 导入仅 PDF、DOCX、TXT，≤10 MB、≤10 页；不做 OCR；导出仅 PDF。
- 原上传文件在解析确认后最长保留 24 小时；导出文件保留 7 天；AI 完整 Prompt 和 thinking/reasoning 文本不持久化。
- 所有公开错误通过 `createApiError` 返回 `{ "error": { "code", "message", "request_id", "details" } }`。
- 根命令 `pnpm install`、`pnpm dev`、`pnpm lint`、`pnpm test`、`pnpm build` 必须覆盖 TypeScript 与 Python 子项目。
- UI 优先复用 `app/web/components/ui`；平台 API 只允许出现在 `app/miniprogram/src/platform`；共享类型只放在 `packages/shared`。
- 禁止提交 `.env`、密钥、真实用户简历、邮箱、手机号、微信标识、模型原始思维或真实供应商响应正文。
- 所有验收状态只允许 `PASS`、`FAIL`、`BLOCKED`；缺乏真实环境证据的项目不得标记为 `PASS`。

---

### Task 1: Monorepo、共享契约与设计 Token

**Files:**
- Create: `package.json`
- Create: `pnpm-workspace.yaml`
- Create: `tsconfig.base.json`
- Create: `eslint.config.mjs`
- Create: `packages/shared/package.json`
- Create: `packages/shared/src/contracts.ts`
- Create: `packages/shared/src/client.ts`
- Create: `packages/shared/src/index.ts`
- Create: `packages/shared/tests/contracts.test.ts`
- Create: `packages/design-tokens/package.json`
- Create: `packages/design-tokens/src/tokens.ts`
- Create: `packages/design-tokens/src/tokens.css`
- Create: `packages/test-fixtures/package.json`
- Create: `packages/test-fixtures/src/index.ts`
- Create: `scripts/run-python.mjs`
- Create: `docs/dependencies.md`

**Interfaces:**
- Produces: `FactStatus`, `TaskStatus`, `MatchCategory`, `SuggestionStatus`, `ResumeSnapshot`, `ApiErrorEnvelope`, `TaskRecord` and `createApiClient({ baseUrl, request })`.
- Produces: `tokens` with exact brand, ink, fact, pending, risk and gap colors from the visual specification.
- Consumes: no application code.

- [ ] **Step 1: Write the failing shared-contract test**

```ts
import { describe, expect, it } from "vitest";
import { createApiClient, isInternalReturnTo, tokens } from "../src/index";

describe("shared contracts", () => {
  it("rejects an external return URL", () => {
    expect(isInternalReturnTo("/resumes")).toBe(true);
    expect(isInternalReturnTo("https://evil.example")).toBe(false);
    expect(isInternalReturnTo("//evil.example")).toBe(false);
    expect(isInternalReturnTo("javascript:alert(1)")).toBe(false);
  });

  it("adds trace and idempotency headers to writes", async () => {
    const calls: RequestInit[] = [];
    const client = createApiClient({
      baseUrl: "https://api.example.test/v1",
      request: async (_url, init) => {
        calls.push(init);
        return new Response(JSON.stringify({ request_id: "req_1" }), { status: 201 });
      },
    });
    await client.post("/facts", { kind: "action", value: "完成调研" }, "idem_1");
    expect(new Headers(calls[0].headers).get("Idempotency-Key")).toBe("idem_1");
    expect(new Headers(calls[0].headers).get("X-Trace-Id")).toMatch(/^tr_/);
  });

  it("exports the approved brand token", () => {
    expect(tokens.color.brand600).toBe("#4F46E5");
  });
});
```

- [ ] **Step 2: Run the test and confirm RED**

Run: `pnpm --filter @resume/shared test`

Expected: FAIL because the package and exports do not exist.

- [ ] **Step 3: Add the workspace and minimal shared implementation**

Implement literal string unions for the specified enums, strict DTOs for facts, resume snapshots, tasks and API errors, an injectable fetch-compatible client, and:

```ts
export function isInternalReturnTo(value: string): boolean {
  return value.startsWith("/") && !value.startsWith("//") && !value.includes("\\");
}
```

The root scripts invoke package scripts plus `python3.12 scripts/python_task.py <command>` through `scripts/run-python.mjs`; `docs/dependencies.md` records every production dependency, purpose, version source and license.

- [ ] **Step 4: Run shared tests and typecheck**

Run: `pnpm --filter @resume/shared test && pnpm --filter @resume/shared build`

Expected: all tests pass and TypeScript emits declarations.

- [ ] **Step 5: Commit**

```bash
git add package.json pnpm-workspace.yaml tsconfig.base.json eslint.config.mjs packages scripts docs/dependencies.md
git commit -m "chore: establish monorepo contracts"
```

### Task 2: FastAPI Foundation, Errors, Trace and Persistence Ports

**Files:**
- Create: `packages/api/pyproject.toml`
- Create: `packages/api/requirements.lock`
- Create: `packages/api/app/main.py`
- Create: `packages/api/app/core/config.py`
- Create: `packages/api/app/core/errors.py`
- Create: `packages/api/app/core/ids.py`
- Create: `packages/api/app/core/middleware.py`
- Create: `packages/api/app/core/security.py`
- Create: `packages/api/app/db/models.py`
- Create: `packages/api/app/db/session.py`
- Create: `packages/api/app/db/repositories.py`
- Create: `packages/api/app/db/memory.py`
- Create: `packages/api/migrations/env.py`
- Create: `packages/api/migrations/versions/0001_initial.py`
- Create: `packages/api/tests/conftest.py`
- Create: `packages/api/tests/test_errors.py`
- Create: `packages/api/tests/test_trace.py`
- Create: `packages/api/tests/test_schema_constraints.py`

**Interfaces:**
- Produces: `createApiError(code, message, request_id, status_code, details=None) -> HTTPException`.
- Produces: `RequestContext(trace_id, request_id, actor_id)` and middleware headers `X-Request-Id`, `X-Trace-Id`.
- Produces: async repository ports for users, facts, resumes, jobs, suggestions, tasks, idempotency and usage, with SQLAlchemy and deterministic in-memory implementations.
- Consumes: enum values and response shapes defined in Task 1.

- [ ] **Step 1: Write failing API contract tests**

```py
def test_api_error_has_stable_envelope(client):
    response = client.get("/v1/testing/error")
    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "RESUME_VERSION_CONFLICT",
            "message": "简历已在其他设备更新",
            "request_id": response.headers["x-request-id"],
            "details": {},
        }
    }

def test_trace_id_is_propagated(client):
    response = client.get("/v1/health/ready", headers={"X-Trace-Id": "tr_test"})
    assert response.status_code == 200
    assert response.headers["x-trace-id"] == "tr_test"
```

Add SQL constraint tests proving a confirmed fact without a source is rejected, `ai_trace_events(ai_run_id,event_seq)` is unique, and historical usage rows cannot be updated through the repository.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `python3.12 -m pytest packages/api/tests/test_errors.py packages/api/tests/test_trace.py packages/api/tests/test_schema_constraints.py -q`

Expected: collection fails because the FastAPI app does not exist.

- [ ] **Step 3: Implement the minimal app and persistence ports**

Create `/v1/health/live`, `/v1/health/ready`, the request middleware and a test-only error route enabled only when `APP_ENV=test`. Define SQLAlchemy 2 async models for every table named by the data specification, including ownership, immutable snapshot hash, idempotency body hash, task terminal state and trace sequence constraints. Use UTC timestamps and UUIDv7-compatible random IDs prefixed by resource type.

- [ ] **Step 4: Verify foundation and migration**

Run: `python3.12 -m pytest packages/api/tests/test_errors.py packages/api/tests/test_trace.py packages/api/tests/test_schema_constraints.py -q`

Run: `python3.12 -m alembic -c packages/api/alembic.ini upgrade head`

Expected: tests pass and an empty database reaches schema version `0001`.

- [ ] **Step 5: Commit**

```bash
git add packages/api
git commit -m "feat(api): add traced persistence foundation"
```

### Task 3: Authentication, Consent, Usage and Privacy

**Files:**
- Create: `packages/api/app/modules/auth/router.py`
- Create: `packages/api/app/modules/auth/schemas.py`
- Create: `packages/api/app/modules/auth/service.py`
- Create: `packages/api/app/modules/users/service.py`
- Create: `packages/api/app/modules/usage/service.py`
- Create: `packages/api/app/modules/privacy/router.py`
- Create: `packages/api/app/modules/privacy/service.py`
- Create: `packages/api/tests/test_auth.py`
- Create: `packages/api/tests/test_usage.py`
- Create: `packages/api/tests/test_privacy.py`
- Modify: `packages/api/app/main.py`

**Interfaces:**
- Produces: email OTP start/verify, WeChat code exchange port, secure session cookie, identity binding, logout, usage query, data-export task and deletion task.
- Produces: `UsageDecision(allowed, reason, retry_after)` with 20/user/day, 2/user concurrent and 100 CNY/day thresholds.
- Consumes: repositories, errors, request context and task creation from Task 2.

- [ ] **Step 1: Write failing behavior tests**

Cover 6-digit OTP with 10-minute expiry, 60-second resend wait, IP 5/minute, email 5/hour, unknown request fields returning 422, logout revocation, no implicit WeChat account creation, consent required on first login, 70/90/100 cost behavior, and deletion revoking every session immediately.

```py
def test_global_cost_limit_keeps_non_ai_features_available(client, auth_headers, usage_repo):
    usage_repo.set_daily_cost("100.00")
    ai = client.post("/v1/resumes/r1/quality-checks", headers={**auth_headers, "Idempotency-Key": "k1"})
    facts = client.get("/v1/facts", headers=auth_headers)
    assert ai.status_code == 429
    assert ai.json()["error"]["code"] == "AI_LIMIT_REACHED"
    assert facts.status_code == 200
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python3.12 -m pytest packages/api/tests/test_auth.py packages/api/tests/test_usage.py packages/api/tests/test_privacy.py -q`

Expected: FAIL because routes and services are missing.

- [ ] **Step 3: Implement minimal auth, limits and deletion state machine**

Hash OTPs and sessions, normalize and encrypt email through injected crypto/key ports, never log external identifiers, set HttpOnly/Secure/SameSite cookies outside tests, require recent-auth proof for deletion, append usage rows, and create privacy tasks rather than doing slow deletion in the request.

- [ ] **Step 4: Run tests**

Run: `python3.12 -m pytest packages/api/tests/test_auth.py packages/api/tests/test_usage.py packages/api/tests/test_privacy.py -q`

Expected: all pass with no warnings.

- [ ] **Step 5: Commit**

```bash
git add packages/api/app/modules packages/api/app/main.py packages/api/tests
git commit -m "feat(api): add authentication usage and privacy"
```

### Task 4: Facts and Immutable Resume Versioning

**Files:**
- Create: `packages/api/app/modules/facts/router.py`
- Create: `packages/api/app/modules/facts/schemas.py`
- Create: `packages/api/app/modules/facts/service.py`
- Create: `packages/api/app/modules/resumes/router.py`
- Create: `packages/api/app/modules/resumes/schemas.py`
- Create: `packages/api/app/modules/resumes/service.py`
- Create: `packages/api/app/modules/resumes/quality.py`
- Create: `packages/api/app/modules/idempotency/service.py`
- Create: `packages/api/tests/test_facts.py`
- Create: `packages/api/tests/test_resume_versions.py`
- Create: `packages/api/tests/test_idempotency.py`
- Modify: `packages/api/app/main.py`

**Interfaces:**
- Produces: the Facts and Resumes public endpoints from the API specification.
- Produces: `save_resume_version(owner_id, resume_id, base_version, snapshot, operation, idempotency_key)`.
- Produces: `check_exportable(snapshot, facts) -> list[QualityIssue]`.
- Consumes: auth actor, repository transaction, `createApiError`, `ResumeSnapshot`.

- [ ] **Step 1: Write failing tests for ownership, source gates and versions**

Test user A cannot read/write user B facts or resumes; confirming without a source fails; identical snapshot hashes do not create a new version; a stale `base_version` returns 409; targeted resumes keep the base hash unchanged; restore copies into a new row; and 10 identical idempotent writes produce one result.

```py
def test_stale_resume_write_returns_visible_conflict(client, user_headers, resume):
    first = client.post(
        f"/v1/resumes/{resume.id}/versions",
        json={"base_version": 1, "snapshot": {"sections": []}},
        headers={**user_headers, "Idempotency-Key": "v1"},
    )
    second = client.post(
        f"/v1/resumes/{resume.id}/versions",
        json={"base_version": 1, "snapshot": {"sections": [{"id": "s1", "items": []}]}},
        headers={**user_headers, "Idempotency-Key": "v2"},
    )
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "RESUME_VERSION_CONFLICT"
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python3.12 -m pytest packages/api/tests/test_facts.py packages/api/tests/test_resume_versions.py packages/api/tests/test_idempotency.py -q`

Expected: FAIL because routes are absent.

- [ ] **Step 3: Implement the transaction-scoped services**

Store source records separately from facts; enforce owner filters in every repository method; create immutable canonical JSON snapshots with SHA-256; update only the resume head pointer; accept, restore and merge by creating new versions; require every bullet atomic claim to reference confirmed facts.

- [ ] **Step 4: Run focused and mutation-oriented tests**

Run: `python3.12 -m pytest packages/api/tests/test_facts.py packages/api/tests/test_resume_versions.py packages/api/tests/test_idempotency.py -q`

Expected: all pass; changing the owner filter, base-version comparison or source check makes at least one named test fail.

- [ ] **Step 5: Commit**

```bash
git add packages/api/app/modules packages/api/app/main.py packages/api/tests
git commit -m "feat(api): add fact-backed resume versions"
```

### Task 5: Durable Tasks, Outbox and Progress

**Files:**
- Create: `packages/api/app/modules/tasks/router.py`
- Create: `packages/api/app/modules/tasks/service.py`
- Create: `packages/api/app/modules/tasks/state.py`
- Create: `packages/api/app/workers/celery_app.py`
- Create: `packages/api/app/workers/dispatcher.py`
- Create: `packages/api/app/workers/execution.py`
- Create: `packages/api/tests/test_task_state.py`
- Create: `packages/api/tests/test_outbox.py`
- Create: `packages/api/tests/test_task_recovery.py`
- Modify: `packages/api/app/main.py`

**Interfaces:**
- Produces: `create_task`, `claim_task`, `report_progress`, `request_cancel`, `complete_task`, `fail_task`, SSE-compatible task events and queue names.
- Produces: transactional Outbox rows and a Redis/Celery dispatcher adapter.
- Consumes: usage decision, idempotency service, trace context and repositories.

- [ ] **Step 1: Write failing state and recovery tests**

Test only the documented transitions; terminal state cannot regress; event sequence starts at 1; cancel prevents a later business result; dispatcher retry does not duplicate the task; worker crash then claim completes one result; Redis failure returns `TASK_QUEUE_BUSY` while existing synchronous facts remain readable.

- [ ] **Step 2: Run tests and confirm RED**

Run: `python3.12 -m pytest packages/api/tests/test_task_state.py packages/api/tests/test_outbox.py packages/api/tests/test_task_recovery.py -q`

Expected: FAIL because the task service is missing.

- [ ] **Step 3: Implement state machine, outbox and adapters**

Use queues `ai.interactive`, `ai.batch`, `file.parse`, `file.export`, `privacy`; default retries 3 with exponential backoff and jitter only for timeout, 429/5xx and transient storage errors. Persist final task state in the repository before emitting completion events.

- [ ] **Step 4: Run tests**

Run: `python3.12 -m pytest packages/api/tests/test_task_state.py packages/api/tests/test_outbox.py packages/api/tests/test_task_recovery.py -q`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add packages/api
git commit -m "feat(api): add durable asynchronous tasks"
```

### Task 6: Pi AI Workflows, Tool Guard and Trace Ledger

**Files:**
- Create: `packages/ai/package.json`
- Create: `packages/ai/tsconfig.json`
- Create: `packages/ai/src/contracts.ts`
- Create: `packages/ai/src/model-router.ts`
- Create: `packages/ai/src/tools/guard.ts`
- Create: `packages/ai/src/tracing/event-ledger.ts`
- Create: `packages/ai/src/workflows/fact-check.ts`
- Create: `packages/ai/src/workflows/run-workflow.ts`
- Create: `packages/ai/src/server/app.ts`
- Create: `packages/ai/src/server/index.ts`
- Create: `packages/ai/tests/tool-guard.test.ts`
- Create: `packages/ai/tests/event-ledger.test.ts`
- Create: `packages/ai/tests/fact-check.test.ts`
- Create: `packages/ai/tests/run-workflow.test.ts`
- Create: `packages/ai/tests/server.test.ts`

**Interfaces:**
- Produces: `POST /internal/v1/runs`, get/cancel and live/ready endpoints.
- Produces: `runWorkflow(input, runtime)` for seven workflow types, with a deterministic fixture runtime for tests and a Pi runtime for configured production.
- Produces: continuous event sequence, token/cost summary and privacy-safe trace metadata.
- Consumes: only caller-provided confirmed facts, JD requirements, workflow config and Pi APIs; no business database.

- [ ] **Step 1: Write failing workflow and safety tests**

Test the five-tool exact whitelist; unknown fact IDs and tool names are rejected; 4 turn/6 tool/12,000 token/30-second limits; unknown events become `unknown`; event sequence stays continuous; usage sums exactly; cancellation stops subsequent events; severe unsupported claims are blocked; 50 injection fixtures never execute an unknown tool.

```ts
it("blocks a number not present in confirmed facts", () => {
  const result = factCheck(
    "将转化率提升 30%",
    [{ id: "fact_1", kind: "result", value: "改进了转化流程", status: "confirmed" }],
  );
  expect(result.exportable).toBe(false);
  expect(result.claims.some((claim) => claim.status === "unsupported")).toBe(true);
});
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `pnpm --filter @resume/ai test`

Expected: FAIL because the package and workflow code do not exist.

- [ ] **Step 3: Implement deterministic core before provider integration**

Implement schema validation, atomic-claim rule checks, tool preflight/postflight sanitization, event mapping and usage aggregation as pure functions. Adapt `@earendil-works/pi-ai` for single calls and `@earendil-works/pi-agent-core` only for `next_question`; production refuses readiness when no approved model route exists, while fixture mode never calls a network.

- [ ] **Step 4: Run tests and build**

Run: `pnpm --filter @resume/ai test && pnpm --filter @resume/ai build`

Expected: all tests pass and no source file imports coding-agent tools.

- [ ] **Step 5: Commit**

```bash
git add packages/ai docs/dependencies.md pnpm-lock.yaml
git commit -m "feat(ai): add guarded Pi workflow service"
```

### Task 7: Import, JD, Matching, Suggestions and PDF Export

**Files:**
- Create: `packages/api/app/modules/imports/router.py`
- Create: `packages/api/app/modules/imports/service.py`
- Create: `packages/api/app/modules/imports/parsers.py`
- Create: `packages/api/app/modules/jobs/router.py`
- Create: `packages/api/app/modules/jobs/service.py`
- Create: `packages/api/app/modules/matching/router.py`
- Create: `packages/api/app/modules/matching/service.py`
- Create: `packages/api/app/modules/suggestions/router.py`
- Create: `packages/api/app/modules/suggestions/service.py`
- Create: `packages/api/app/modules/exports/router.py`
- Create: `packages/api/app/modules/exports/service.py`
- Create: `packages/api/app/modules/exports/templates.py`
- Create: `packages/api/app/integrations/ai_client.py`
- Create: `packages/api/app/integrations/storage.py`
- Create: `packages/api/tests/test_imports.py`
- Create: `packages/api/tests/test_matching.py`
- Create: `packages/api/tests/test_suggestions.py`
- Create: `packages/api/tests/test_exports.py`
- Create: `packages/test-fixtures/files/valid.txt`
- Modify: `packages/api/app/main.py`

**Interfaces:**
- Produces: every public Import, JD, Matching, Suggestion and Export endpoint in the API spec.
- Produces: storage port with memory/local and COS implementations; AI client port with fixture and internal HTTP implementations.
- Consumes: confirmed facts, immutable resume versions, durable tasks and Pi structured results.

- [ ] **Step 1: Write failing boundary tests**

Cover type/size/page rejection, magic-byte mismatch, pre-confirmation fact count zero, scanned/encrypted/corrupt fallback reasons, exactly four match categories, complete suggestion explanation, accept/edit/ignore/revert transitions, base hash isolation, export block for pending claims, two templates preserving snapshot hash, sanitized filename and 10-minute download expiry.

- [ ] **Step 2: Run tests and confirm RED**

Run: `python3.12 -m pytest packages/api/tests/test_imports.py packages/api/tests/test_matching.py packages/api/tests/test_suggestions.py packages/api/tests/test_exports.py -q`

Expected: FAIL because modules are missing.

- [ ] **Step 3: Implement parsers, decisions and deterministic PDF**

Extract PDF text layers, DOCX paragraphs/tables and UTF-8 TXT in a non-networked parser port; never create confirmed facts until explicit import confirmation. Apply suggestion decisions in one transaction that writes the decision and a new targeted resume version. Render “清晰标准” and “现代留白” from the same canonical snapshot; run fact and pagination checks before persisting an export.

- [ ] **Step 4: Run tests**

Run: `python3.12 -m pytest packages/api/tests/test_imports.py packages/api/tests/test_matching.py packages/api/tests/test_suggestions.py packages/api/tests/test_exports.py -q`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add packages/api packages/test-fixtures
git commit -m "feat(api): add optimization and export pipeline"
```

### Task 8: Web Application and Both User Flows

**Files:**
- Create: `app/web/package.json`
- Create: `app/web/next.config.ts`
- Create: `app/web/app/layout.tsx`
- Create: `app/web/app/globals.css`
- Create: `app/web/app/page.tsx`
- Create: `app/web/app/login/page.tsx`
- Create: `app/web/app/home/page.tsx`
- Create: `app/web/app/create/page.tsx`
- Create: `app/web/app/imports/[id]/confirm/page.tsx`
- Create: `app/web/app/resumes/page.tsx`
- Create: `app/web/app/resumes/[id]/edit/page.tsx`
- Create: `app/web/app/resumes/[id]/versions/page.tsx`
- Create: `app/web/app/jobs/new/page.tsx`
- Create: `app/web/app/jobs/[id]/match/page.tsx`
- Create: `app/web/app/suggestions/[analysisId]/page.tsx`
- Create: `app/web/app/facts/page.tsx`
- Create: `app/web/app/tasks/page.tsx`
- Create: `app/web/app/exports/[id]/page.tsx`
- Create: `app/web/app/settings/page.tsx`
- Create: `app/web/components/ui/Button.tsx`
- Create: `app/web/components/ui/StatusTag.tsx`
- Create: `app/web/components/ui/Field.tsx`
- Create: `app/web/features/editor/editor-reducer.ts`
- Create: `app/web/features/editor/use-auto-save.ts`
- Create: `app/web/features/session/return-to.ts`
- Create: `app/web/tests/editor-reducer.test.ts`
- Create: `app/web/tests/auto-save.test.tsx`
- Create: `app/web/tests/accessibility.test.tsx`
- Create: `tests/e2e-web/zero-to-resume.spec.ts`
- Create: `tests/e2e-web/optimize-existing.spec.ts`

**Interfaces:**
- Produces: all specified Web routes and responsive desktop/tablet/mobile layouts.
- Produces: 20-step local undo, 800 ms auto-save, 15-second fallback, explicit offline/saving/saved/conflict states.
- Consumes: generated shared API client, design tokens and public FastAPI only.

- [ ] **Step 1: Write failing reducer, save and accessibility tests**

Test 20 undo operations and safe underflow; debounce and fallback timers; dirty draft retained on API failure; conflict stops autosave; malicious return URLs resolve to `/home`; every interactive control has a name; status uses text and icon, not color alone.

- [ ] **Step 2: Run tests and confirm RED**

Run: `pnpm --filter @resume/web test`

Expected: FAIL because Web package and components do not exist.

- [ ] **Step 3: Implement reusable UI and every route**

Use one primary action per page; desktop editor columns are 240 px / min 520 px / 360 px, tablet two columns, mobile one column. Implement the two complete flows against the shared client, but keep fixture API injection for deterministic E2E. Add keyboard suggestion actions A/E/I/Z only when focus is outside inputs.

- [ ] **Step 4: Run component tests and responsive E2E**

Run: `pnpm --filter @resume/web test`

Run: `pnpm exec playwright test tests/e2e-web --project=chromium`

Expected: tests pass at 1440×900, 1024×768 and 390×844 with no horizontal overflow.

- [ ] **Step 5: Commit**

```bash
git add app/web tests/e2e-web pnpm-lock.yaml
git commit -m "feat(web): deliver resume creation and optimization"
```

### Task 9: WeChat Mini Program and Platform Adapters

**Files:**
- Create: `app/miniprogram/package.json`
- Create: `app/miniprogram/config/index.ts`
- Create: `app/miniprogram/src/app.config.ts`
- Create: `app/miniprogram/src/app.tsx`
- Create: `app/miniprogram/src/app.scss`
- Create: `app/miniprogram/src/platform/request.ts`
- Create: `app/miniprogram/src/platform/auth.ts`
- Create: `app/miniprogram/src/platform/files.ts`
- Create: `app/miniprogram/src/platform/storage.ts`
- Create: `app/miniprogram/src/features/draft-store.ts`
- Create: `app/miniprogram/src/components/ui/PrimaryAction.tsx`
- Create: `app/miniprogram/src/components/ui/StatusTag.tsx`
- Create: `app/miniprogram/src/pages/home/index.tsx`
- Create: `app/miniprogram/src/pages/resumes/index.tsx`
- Create: `app/miniprogram/src/pages/facts/index.tsx`
- Create: `app/miniprogram/src/pages/me/index.tsx`
- Create: `app/miniprogram/src/subpackages/create/index.tsx`
- Create: `app/miniprogram/src/subpackages/resume/editor.tsx`
- Create: `app/miniprogram/src/subpackages/resume/preview.tsx`
- Create: `app/miniprogram/src/subpackages/optimize/import.tsx`
- Create: `app/miniprogram/src/subpackages/optimize/job.tsx`
- Create: `app/miniprogram/src/subpackages/optimize/match.tsx`
- Create: `app/miniprogram/src/subpackages/optimize/suggestions.tsx`
- Create: `app/miniprogram/src/subpackages/settings/privacy.tsx`
- Create: `app/miniprogram/tests/platform-boundary.test.ts`
- Create: `app/miniprogram/tests/draft-store.test.ts`
- Create: `app/miniprogram/tests/flow.test.tsx`

**Interfaces:**
- Produces: active WeChat login, four-tab shell, five subpackages and full feature parity through mobile interaction.
- Produces: platform request/auth/files/secure storage adapters; no `wx.*` outside `platform`.
- Consumes: shared API client and design tokens.

- [ ] **Step 1: Write failing platform and lifecycle tests**

Test static import boundary, active-click login, every write gets an idempotency key, local draft ≤200 KB, 7-day expiry, successful sync removes local draft, `onHide` flushes dirty edits, `onShow` refreshes task/version, and unavailable save-file capability displays an alternative without reporting success.

- [ ] **Step 2: Run tests and confirm RED**

Run: `pnpm --filter @resume/miniprogram test`

Expected: FAIL because the package is missing.

- [ ] **Step 3: Implement pages and adapters**

Use 44×44 px minimum controls, one primary question per screen, fixed bottom actions that move above the keyboard, button-based sorting, full-screen suggestion/source views, and remote preview/export. Keep complete files and long-lived tokens out of local storage.

- [ ] **Step 4: Run tests, static scan and build**

Run: `pnpm --filter @resume/miniprogram test`

Run: `rg -n 'wx\\.' app/miniprogram/src --glob '!platform/**'`

Run: `pnpm --filter @resume/miniprogram build`

Expected: tests/build pass and the scan returns no matches outside `platform`.

- [ ] **Step 5: Commit**

```bash
git add app/miniprogram pnpm-lock.yaml
git commit -m "feat(miniprogram): add full mobile resume flows"
```

### Task 10: Local Infrastructure, Deployment and Operations

**Files:**
- Create: `infra/docker/docker-compose.yml`
- Create: `infra/docker/api.Dockerfile`
- Create: `infra/docker/ai.Dockerfile`
- Create: `infra/docker/web.Dockerfile`
- Create: `infra/deployment/cloudbase/api.yaml`
- Create: `infra/deployment/cloudbase/ai.yaml`
- Create: `infra/deployment/cloudbase/web.yaml`
- Create: `infra/deployment/cloudbase/workers.yaml`
- Create: `infra/observability/otel-collector.yaml`
- Create: `infra/observability/alerts.yaml`
- Create: `infra/observability/dashboards.json`
- Create: `docs/runbooks/local-development.md`
- Create: `docs/runbooks/production-release.md`
- Create: `docs/runbooks/rollback.md`
- Create: `docs/runbooks/provider-outage.md`
- Create: `docs/runbooks/data-deletion.md`
- Create: `docs/runbooks/backup-restore.md`
- Create: `docs/security/data-flow.md`
- Create: `tests/contract/deployment.test.ts`

**Interfaces:**
- Produces: local PostgreSQL, Redis, API, Celery, Pi, Web and OTel topology; production manifests with minimum two API replicas and independent AI/file workers.
- Produces: health, version and configuration traceability.
- Consumes: built service artifacts and environment-only secrets.

- [ ] **Step 1: Write failing executable deployment contract tests**

Parse compose/manifests and assert private database/Redis, no embedded secrets, API replicas ≥2, worker concurrency 10/5, Pi concurrency 10, queues exactly match Task 5, immutable image tag placeholder is `${COMMIT_SHA}`, and health checks exist.

- [ ] **Step 2: Run tests and confirm RED**

Run: `pnpm exec vitest run tests/contract/deployment.test.ts`

Expected: FAIL because deployment files are absent.

- [ ] **Step 3: Implement local and Tencent Cloud topology plus runbooks**

Keep cloud IDs, domain names, credentials and model keys as required environment inputs. Encode cost threshold feature flags, graceful worker shutdown, OpenTelemetry routes, redacted JSON logs, 10%/30-minute canary and non-destructive migration rollback boundaries.

- [ ] **Step 4: Validate contracts and configurations**

Run: `pnpm exec vitest run tests/contract/deployment.test.ts`

Run: `docker compose -f infra/docker/docker-compose.yml config` when Docker is available; otherwise record `BLOCKED` for the container-runtime evidence while keeping the static contract result.

Expected: contract tests pass; Docker config either passes or is recorded as a named external blocker.

- [ ] **Step 5: Commit**

```bash
git add infra docs/runbooks docs/security tests/contract
git commit -m "ops: add deployment observability and runbooks"
```

### Task 11: Acceptance Harness, Evidence Manifest and Full Verification

**Files:**
- Create: `scripts/acceptance/run.mjs`
- Create: `scripts/acceptance/record-command.mjs`
- Create: `scripts/acceptance/build-manifest.mjs`
- Create: `scripts/acceptance/check-sensitive-data.mjs`
- Create: `scripts/acceptance/check-coverage.mjs`
- Create: `tests/acceptance/manifest.test.ts`
- Create: `tests/performance/non-ai.js`
- Create: `tests/performance/ai-queue.js`
- Create: `tests/security/pii-patterns.json`
- Create: `docs/acceptance/external-evidence-guide.md`
- Create: `docs/acceptance/final-report-template.md`
- Create: `artifacts/acceptance/.gitkeep`
- Modify: `package.json`

**Interfaces:**
- Produces: `pnpm acceptance` and a release directory with manifest, command logs, hashes and one status for every acceptance ID.
- Produces: deterministic `PASS` only after its command and evidence hash exist; unavailable real-device/cloud/user/provider checks become explicit `BLOCKED`.
- Consumes: every test/build command and the acceptance specification.

- [ ] **Step 1: Write failing manifest integrity tests**

Test that all 146 unique acceptance IDs are present exactly once; status is only PASS/FAIL/BLOCKED; every PASS has at least one existing evidence file and matching SHA-256; commit SHA matches HEAD; command evidence includes command, start/end, exit code and output; sensitive fixtures trigger the privacy scanner.

- [ ] **Step 2: Run tests and confirm RED**

Run: `pnpm exec vitest run tests/acceptance/manifest.test.ts`

Expected: FAIL because the harness does not exist.

- [ ] **Step 3: Implement evidence recording without synthetic passes**

Generate a release ID from UTC time and short commit SHA. Map local unit, contract, build and deterministic E2E checks to their acceptance IDs. Predeclare device, browser-Safari/Edge, Tencent Cloud, real provider billing, backup restore, filing/review, alert delivery and 30-student validation as `BLOCKED` until the required evidence paths are supplied and verified.

- [ ] **Step 4: Execute complete local verification**

Run: `pnpm install`

Run: `pnpm lint`

Run: `pnpm test`

Run: `pnpm build`

Run: `pnpm test:contract`

Run: `pnpm acceptance`

Expected: every command exits 0; the manifest honestly distinguishes locally proven PASS items from external BLOCKED items and contains no sensitive data.

- [ ] **Step 5: Commit**

```bash
git add package.json scripts tests docs/acceptance artifacts/acceptance pnpm-lock.yaml
git commit -m "test: add acceptance evidence pipeline"
```

### Task 12: Independent Review, Fix Wave and Release Decision

**Files:**
- Modify: files identified by the independent reviewer.
- Create: `docs/acceptance/review-findings.md`
- Create: `docs/acceptance/release-decision.md`

**Interfaces:**
- Produces: spec-compliance verdict, code-quality verdict, severity-ranked findings and final release decision.
- Consumes: complete branch diff, plan ledger, acceptance manifest and all command evidence.

- [ ] **Step 1: Generate the whole-branch review package**

Run the Superpowers `review-package` script with merge base and HEAD, and provide the plan, implementation reports, ledger and deferred findings to the independent checker.

- [ ] **Step 2: Record RED for every Critical/Important finding**

For each real finding, first add or identify a test that fails for the reported behavior; run the focused command and retain the failing output in the task report.

- [ ] **Step 3: Apply one consolidated fix wave**

The implementation agent fixes all confirmed Critical/Important findings without broad refactors, re-runs the focused tests and appends exact command evidence.

- [ ] **Step 4: Re-review and run fresh verification**

Run: `pnpm lint && pnpm test && pnpm build && pnpm acceptance`

Expected: commands exit 0, the checker reports no open Critical/Important code findings, and external evidence gaps remain `BLOCKED` rather than being waived.

- [ ] **Step 5: Write the release decision**

`release-decision.md` must state the exact commit, locally proven scope, open Sev1–Sev4 defects, every external blocker and one of:

- `NOT READY FOR PUBLIC RELEASE` when any P0 is FAIL/BLOCKED;
- `READY FOR PUBLIC RELEASE` only when every P0 is PASS and the evidence belongs to the same commit/build.

- [ ] **Step 6: Commit**

```bash
git add docs/acceptance
git commit -m "docs: record independent release assessment"
```

## Plan Self-Review

- Spec coverage: Tasks 1–11 cover ENG, FLOW, WEB, MP, AI, FILE, DATA, PERF, SEC, OBS, UX, USER and OPS; Task 12 enforces final review and release gating.
- Placeholder scan: the plan contains no deferred implementation placeholder; cloud IDs and credentials are explicit environment inputs, not invented values.
- Type consistency: clients consume `ResumeSnapshot`, `TaskRecord`, enums and `createApiClient` from Task 1; FastAPI owns public state; Pi consumes only explicit workflow contracts; both clients use the same public API.
- External evidence boundary: iPhone/two Android devices, WeChat audit package, current/previous Safari and Edge, Tencent Cloud failure drills, provider invoice reconciliation, filing/privacy publication, alert delivery, backup restore and 30-student validation cannot be made `PASS` on this local machine without real evidence.
