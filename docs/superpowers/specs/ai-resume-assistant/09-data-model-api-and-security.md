# 数据模型、API 与安全

## 1. 数据原则

- 用户输入、AI 表达和最终简历必须分开保存；
- 事实来源不可被覆盖，只能追加修订；
- 简历版本不可变；
- AI 建议不是事实；
- PostgreSQL 是最终数据源；
- 个人信息最小化；
- 删除范围包含业务数据、文件和可识别链路关联。

## 2. 核心对象

### 2.1 用户与身份

| 表 | 关键字段 |
| --- | --- |
| `users` | `id`, `status`, `locale`, `created_at`, `deleted_at` |
| `user_identities` | `user_id`, `type`, `external_subject_hash`, `verified_at` |
| `user_consents` | `user_id`, `document_type`, `document_version`, `decision`, `decided_at` |
| `sessions` | `user_id`, `token_hash`, `expires_at`, `revoked_at`, `device_type` |

邮箱使用加密列保存，检索使用规范化值的不可逆索引。微信外部标识不进入日志。

### 2.2 来源与事实

| 表 | 关键字段 |
| --- | --- |
| `source_records` | `id`, `user_id`, `source_type`, `source_ref`, `content_encrypted`, `created_at` |
| `experiences` | `id`, `user_id`, `type`, `title`, `organization`, `start_date`, `end_date` |
| `facts` | `id`, `experience_id`, `kind`, `value_encrypted`, `status`, `confirmed_at` |
| `fact_sources` | `fact_id`, `source_record_id`, `source_range`, `source_hash` |
| `fact_revisions` | `fact_id`, `previous_value_hash`, `new_value_encrypted`, `actor`, `created_at` |

`source_type`：

- `question_answer`
- `imported_resume`
- `user_edit`
- `user_confirmation`

一个事实至少有一个来源。`confirmed` 事实必须有 `confirmed_at`。

### 2.3 简历与版本

| 表 | 关键字段 |
| --- | --- |
| `resumes` | `id`, `user_id`, `kind`, `title`, `base_resume_id`, `job_description_id` |
| `resume_versions` | `id`, `resume_id`, `parent_version_id`, `snapshot_json`, `snapshot_hash`, `created_by` |
| `resume_sections` | 只作为查询索引；权威内容在版本快照 |
| `bullet_fact_links` | `resume_version_id`, `bullet_id`, `fact_id`, `claim_range` |
| `version_operations` | `version_id`, `operation_type`, `actor`, `metadata` |

`kind`：

- `base`
- `job_targeted`

岗位版本不能作为基础版本的原地修改。`snapshot_hash` 相同的重复保存不创建新版本。

### 2.4 JD、匹配与建议

| 表 | 关键字段 |
| --- | --- |
| `job_descriptions` | `id`, `user_id`, `title`, `company`, `raw_encrypted`, `status` |
| `jd_requirements` | `id`, `job_id`, `type`, `priority`, `text_encrypted`, `confirmed` |
| `match_analyses` | `id`, `resume_version_id`, `job_id`, `status`, `workflow_version` |
| `match_items` | `analysis_id`, `requirement_id`, `category`, `evidence_refs` |
| `suggestions` | `id`, `analysis_id`, `target_path`, `original_hash`, `suggested_encrypted`, `status` |
| `suggestion_fact_links` | `suggestion_id`, `fact_id`, `claim_range` |
| `suggestion_decisions` | `suggestion_id`, `decision`, `edited_text_encrypted`, `decided_at` |

`suggestion.status`：

- `pending`
- `accepted`
- `edited`
- `ignored`
- `reverted`
- `blocked`

### 2.5 文件、任务、AI 与用量

| 表 | 关键字段 |
| --- | --- |
| `files` | `id`, `owner_user_id`, `purpose`, `object_key`, `sha256`, `size`, `mime`, `expires_at` |
| `tasks` | 见 FastAPI 规格 |
| `task_events` | `task_id`, `seq`, `stage`, `progress`, `created_at` |
| `ai_runs` | 见 Pi 规格 |
| `ai_trace_events` | 见 Pi 规格 |
| `exports` | `resume_version_id`, `template_version`, `file_id`, `content_hash`, `status` |
| `usage_ledger` | `user_id`, `usage_type`, `quantity`, `cost_cny`, `trace_id`, `created_at` |
| `audit_logs` | `actor_id`, `action`, `resource_type`, `resource_id`, `result`, `created_at` |

## 3. 数据约束

数据库必须强制：

- 所有用户资源有 `owner_user_id`；
- `fact_sources.fact_id` 引用存在事实；
- `confirmed` 事实至少一个来源；
- 岗位版本同时有 `base_resume_id` 和 `job_description_id`；
- `bullet_fact_links` 只引用当前用户的事实；
- 建议接受前状态为 `pending`；
- 终态任务不得修改结果引用；
- 同一导出记录绑定一个不可变版本；
- `ai_trace_events(ai_run_id, event_seq)` 唯一；
- 用量账本只追加，不更新历史记录。

跨用户引用在 Service 和数据库约束/触发器中双重阻断。

## 4. 公开 API

前缀：`/v1`

### 4.1 Auth

```text
POST /auth/email/start
POST /auth/email/verify
POST /auth/wechat/login
POST /auth/identities/bind-email
POST /auth/refresh
POST /auth/logout
```

### 4.2 Facts

```text
GET    /facts
POST   /facts
GET    /facts/{fact_id}
PATCH  /facts/{fact_id}
POST   /facts/{fact_id}/confirm
POST   /facts/{fact_id}/reject
GET    /facts/{fact_id}/sources
```

### 4.3 Resumes

```text
GET    /resumes
POST   /resumes
GET    /resumes/{resume_id}
PATCH  /resumes/{resume_id}
GET    /resumes/{resume_id}/versions
POST   /resumes/{resume_id}/versions
POST   /resumes/{resume_id}/versions/{version_id}/restore
POST   /resumes/{resume_id}/quality-checks
```

### 4.4 Import、JD 与匹配

```text
POST   /files/upload-tokens
POST   /files/{file_id}/confirm-upload
DELETE /files/{file_id}
POST   /imports
GET    /imports/{import_id}
POST   /imports/{import_id}/confirm
POST   /jobs
POST   /jobs/{job_id}/parse
PATCH  /jobs/{job_id}/requirements/{requirement_id}
POST   /match-analyses
GET    /match-analyses/{analysis_id}
GET    /match-analyses/{analysis_id}/suggestions
POST   /suggestions/{suggestion_id}/accept
POST   /suggestions/{suggestion_id}/edit
POST   /suggestions/{suggestion_id}/ignore
POST   /suggestions/{suggestion_id}/revert
```

### 4.5 Task、导出与隐私

```text
GET    /tasks
GET    /tasks/{task_id}
GET    /tasks/{task_id}/events
POST   /tasks/{task_id}/cancel
POST   /exports
GET    /exports/{export_id}
POST   /me/data-exports
POST   /me/deletion-requests
GET    /me/usage
```

## 5. API 契约

- JSON 使用 `snake_case`；
- ID 使用不可预测的 UUIDv7 或等价随机 ID；
- 时间使用 UTC ISO 8601；
- 列表使用游标分页；
- 写请求使用 Pydantic 严格校验并拒绝未知字段；
- 每个响应包含 `request_id`；
- 资源响应包含 `version` 或 `etag`；
- 长任务返回 `202`；
- 创建成功返回 `201`；
- 冲突返回 `409`；
- 限流返回 `429` 和 `Retry-After`；
- 删除任务接受返回 `202`。

分页：

```json
{
  "items": [],
  "next_cursor": "opaque-or-null",
  "request_id": "req_019fa2876c0e"
}
```

## 6. Pi 内部 API

仅内部网络：

```text
POST /internal/v1/runs
GET  /internal/v1/runs/{ai_run_id}
POST /internal/v1/runs/{ai_run_id}/cancel
GET  /internal/v1/health/live
GET  /internal/v1/health/ready
```

调用要求：

- mTLS 或短期服务令牌；
- `trace_id`、`task_id`、`workflow_type`、`workflow_version` 必填；
- 请求体最大 512 KB；
- FastAPI/Worker 只传必要事实；
- Pi 返回结构化结果和 usage；
- 取消必须传播到 `AbortSignal`；
- 公网无法路由内部端点。

## 7. 安全

### 7.1 传输和存储

- 外部和内部 HTTP 使用 TLS；
- 数据库、Redis 和对象存储不暴露公网；
- 数据库磁盘、备份和对象存储服务端加密；
- 邮箱、联系方式、简历、JD 和事实列应用级加密；
- 密钥存入云密钥管理，不进入 `.env` 提交、日志或镜像；
- 密钥至少每 90 天轮换一次。

### 7.2 Web

- HttpOnly、Secure、SameSite 会话 Cookie；
- CSRF 防护；
- CSP；
- 禁止把敏感内容写入 URL、localStorage 和分析事件；
- 上传下载使用短期签名；
- 富文本按纯文本和结构化字段渲染，禁止任意 HTML。

### 7.3 小程序

- 不信任客户端 `openid`；
- 每次登录凭证由后端换取；
- 本地缓存不保存模型密钥、长期令牌或完整文件；
- 请求域名、上传域名和下载域名使用正式白名单；
- 调试日志在生产构建关闭。

### 7.4 防滥用

- 账号、用户、IP 和设备维度限流；
- 上传文件类型、大小、频率限制；
- Prompt injection 输入不能改变系统工具白名单；
- Pi Tool 参数严格 Schema 验证；
- 模型输出当作不可信输入再次验证；
- 高风险异常触发人工禁用开关，不自动封禁真实用户数据。

## 8. 数据保留

| 数据 | 保留 |
| --- | --- |
| 原上传文件 | 解析确认后最长 24 小时 |
| 导出文件 | 7 天 |
| 业务简历与事实 | 账户存在期间，用户可主动删除 |
| AI 完整 Prompt | 不持久化 |
| AI 链路元数据 | 90 天 |
| 不含正文的安全审计 | 180 天 |
| 邮箱验证码 | 10 分钟 |
| 会话 | 最长 30 天，可主动吊销 |
| 在线删除任务 | 72 小时内完成 |
| 备份 | 最长 30 天 |

保留期限到期由每日任务执行，失败触发告警。

## 9. 迁移

- Alembic 迁移进入代码评审；
- 禁止生产环境启动时自动执行不可逆迁移；
- 新列先可空/带默认，应用兼容后再收紧；
- 大表迁移分批；
- 每次发布记录 schema version；
- 回滚不能依赖删除用户数据；
- 迁移前创建可验证备份，恢复演练通过后发布。
