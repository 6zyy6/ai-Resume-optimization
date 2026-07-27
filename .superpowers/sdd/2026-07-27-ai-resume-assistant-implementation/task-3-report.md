STATUS: DONE

## Files

- `packages/api/app/modules/auth/{router,schemas,service}.py`
- `packages/api/app/modules/users/service.py`
- `packages/api/app/modules/usage/{router,service}.py`
- `packages/api/app/modules/privacy/{router,service}.py`
- `packages/api/app/main.py`
- `packages/api/tests/{test_auth,test_usage,test_privacy,test_contracts}.py`
- `packages/api/scripts/export_openapi.py`
- `packages/api/{pyproject.toml,requirements.lock}`
- `packages/shared/generated/{openapi.json,schema.ts}`
- `docs/dependencies.md`

## RED evidence

- Usage tests failed at collection with `ModuleNotFoundError: No module named 'app.modules.usage'`.
- Privacy tests failed at collection with `ModuleNotFoundError: No module named 'app.modules.privacy'`.
- Exact identity-type tests failed because the inherited partial implementation stored `email` and `wechat` rather than `email_otp` and `wechat_miniprogram`.
- The 11th data export in an hour returned 202, and the first request at the exact one-hour boundary still returned 429.
- A refreshed stale session bypassed recent-auth deletion proof and created a deletion task.
- The OpenAPI completeness test failed because none of the nine Task 3 routes were exported.
- Bodyless write routes accepted unknown JSON fields; unconfigured email auth claimed success; production accepted missing injected ports; generated error responses used FastAPI's validation schema instead of `ApiErrorEnvelope`.

## Implemented behavior

- Six-digit HMAC-hashed email OTPs expire after ten minutes, wait sixty seconds before resend, and enforce IP 5/minute plus email 5/hour limits.
- Strict request DTOs reject unknown fields, including nominally bodyless refresh, logout and privacy writes.
- First email login requires an accepted consent record. Email values are normalized, AES-GCM encrypted, and indexed by a separate HMAC lookup hash.
- WeChat codes are exchanged only through the injected server-side port; client external identifiers are rejected and unknown identities never create accounts.
- Session cookies are HttpOnly/SameSite and Secure outside tests. Tokens are hashed, refresh preserves the original identity-authentication time, and logout revokes the session.
- `UsageDecision` enforces 20 AI tasks/user/day, 2 running/user, and global 70/90/100 CNY alert/degrade/stop states. Usage recording is append-only through the service port, and `/v1/me/usage` remains available at the global stop threshold.
- Data-export tasks require idempotency keys and enforce 10/user/hour. Deletion requires authentication no older than ten minutes, creates only one active deletion task, marks the account pending deletion, and revokes every session immediately.
- Production auth assembly fails fast unless repository, sender, WeChat, crypto and key ports are injected. Unconfigured development providers return 503 rather than claiming delivery.
- Generated OpenAPI includes all Task 3 paths and declares the runtime `ApiErrorEnvelope`.

## Verification

- `.venv/bin/python -m pytest packages/api/tests/test_auth.py packages/api/tests/test_usage.py packages/api/tests/test_privacy.py -q`: 27 passed.
- `pnpm lint`: passed.
- `pnpm test`: Vitest 4/4, Node supervisor 1/1, pytest 58/58.
- `pnpm build`: passed for all TypeScript workspaces and Python compile checks.
- Two consecutive `pnpm generate` runs produced identical SHA-256 values:
  - `openapi.json`: `a17e2cb195da72f7c396419cfeb4a8748cd60ab7af93654c02f3dd0484460368`
  - `schema.ts`: `1e95589c293175f4291b6a242b4e4b8a98c1d0b2f5acb5a02d2c273e53b74b24`
- `pip check`: no broken requirements.
- Read-only completion review identified four Important findings; three were fixed and reverified.

## Commit

`feat(api): add authentication usage and privacy`

## Concerns

- The repository does not yet contain an AI task-creation route. Task 3 exposes the tested `decide_ai_task` and `record_ai_task` boundaries, but a later AI workflow task must call them in the same transaction as task creation.
- Development/test defaults are intentionally in-memory. Production refuses to start without injected persistent/provider/key ports; concrete cloud email, WeChat and KMS adapters remain deployment work.
- The brief's system `python3.12 -m pytest` command cannot load project packages in this environment; all Python verification used the repository `.venv` selected by `scripts/run-python.mjs`.
