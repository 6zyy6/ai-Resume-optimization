# FastAPI 业务后端规格

## 1. 职责

FastAPI 是公开客户端唯一业务入口，承担：

- 身份认证和资源授权；
- 事务和业务状态机；
- 事实、简历、JD、建议和版本；
- 文件生命周期；
- 任务创建、取消和进度；
- 用量和限额；
- 数据导出与删除；
- 审计与链路上下文。

FastAPI 不负责直接执行模型 Agent loop。

## 2. 模块

```text
app/modules/
├── auth/
├── users/
├── facts/
├── resumes/
├── imports/
├── jobs/
├── matching/
├── suggestions/
├── tasks/
├── exports/
├── usage/
├── privacy/
└── audit/
```

每个模块包含：

- `router.py`：HTTP；
- `schemas.py`：Pydantic 输入输出；
- `service.py`：业务用例；
- `repository.py`：数据访问；
- `models.py`：ORM；
- `policies.py`：权限；
- `errors.py`：模块错误；
- `tests/`：单元与集成测试。

Router 不直接访问 ORM；Repository 不调用外部服务；业务跨模块通过 Service 接口。

## 3. 身份和授权

身份类型：

- `email_otp`
- `wechat_miniprogram`

一个用户可以绑定多个身份。外部平台 ID 只保存在 `user_identities`。

授权规则：

- 所有简历、事实、JD、文件、任务和导出都带 `owner_user_id`；
- 每次查询在数据库条件中包含 `owner_user_id`，不只在 Python 中过滤；
- 内部服务使用短期服务令牌和网络访问控制；
- 管理操作默认不存在公开管理接口；
- 删除账户需要重新验证最近登录身份。

## 4. 事务

以下操作必须单事务：

- 确认答案并创建/更新事实；
- 接受建议并创建新简历版本；
- 恢复历史版本；
- 确认解析结果并写入事实库；
- 创建导出记录并绑定不可变版本；
- 扣减用量并创建 AI 任务。

外部网络调用不得位于数据库事务中。采用：

1. 数据库写入任务与 Outbox；
2. 提交事务；
3. Dispatcher 发布 Redis；
4. Worker 执行；
5. 幂等写回。

## 5. 版本

- `resume` 是逻辑文档；
- `resume_version` 是不可变快照；
- 用户每次已确认编辑创建新版本；
- 自动保存可以合并 5 秒内同一用户、同一模块的连续编辑，但审计操作保留；
- 岗位版本有 `base_resume_version_id` 和 `job_description_id`；
- 恢复历史版本是“复制为新版本”，不修改历史行。

## 6. 幂等

以下接口必须要求 `Idempotency-Key`：

- 文件上传确认；
- 创建解析、AI、匹配和导出任务；
- 接受/忽略建议；
- 创建或恢复版本；
- 数据删除。

规则：

- 键按 `user_id + route + key` 唯一；
- 24 小时内重放返回相同状态码和业务响应；
- 相同键但请求体哈希不同返回 `409 IDEMPOTENCY_KEY_REUSED`；
- 幂等记录保留 7 天；
- Worker 以 `task_id + operation` 保证结果唯一。

## 7. 错误

所有错误通过 `createApiError` 生成。错误码稳定，消息可本地化。

必备错误码：

- `AUTH_REQUIRED`
- `AUTH_CODE_INVALID`
- `RESOURCE_NOT_FOUND`
- `RESOURCE_FORBIDDEN`
- `VALIDATION_FAILED`
- `IDEMPOTENCY_KEY_REUSED`
- `RESUME_VERSION_CONFLICT`
- `FACT_NOT_CONFIRMED`
- `EXPORT_BLOCKED_BY_FACTS`
- `FILE_TYPE_UNSUPPORTED`
- `FILE_TOO_LARGE`
- `FILE_PARSE_FAILED`
- `TASK_QUEUE_BUSY`
- `AI_LIMIT_REACHED`
- `AI_PROVIDER_UNAVAILABLE`
- `DATA_DELETION_IN_PROGRESS`

公开响应不得包含堆栈、SQL、模型密钥、内部主机名或原始供应商错误体。

## 8. 任务

`tasks` 表保存：

- 类型、状态、优先级；
- owner、resource、trace；
- attempts、max_attempts；
- queued、started、finished 时间；
- progress stage 和整数百分比；
- error code；
- result reference；
- cancellation_requested。

Celery 任务要求：

- 可重复执行但业务结果幂等；
- 只有网络超时、供应商 429/5xx 和临时存储错误自动重试；
- Schema 错误、权限错误、无来源事实不自动无限重试；
- 默认最多 3 次；
- 使用指数退避和随机抖动；
- 任务开始前和写回前检查取消状态；
- 重试事件写入链路。

## 9. 用量

`usage_ledger` 使用追加记录，不直接修改一个不可审计计数。

默认限制：

- 每用户每日 20 个 AI 任务；
- 每用户同时 2 个 AI 任务；
- 每 IP 每分钟登录验证码 5 次；
- 每邮箱每小时验证码 5 次；
- 每用户每小时导出 10 次；
- 全局 AI 成本 100 元/日。

编辑、查看、历史版本和本地质量检查不消耗 AI 额度。

## 10. 文件安全

- 后端只签发指定对象键和大小的上传凭证；
- 客户端不能指定任意桶或路径；
- 上传完成后检查 magic bytes，不只信任扩展名；
- 文件对象键不包含用户原文件名；
- 解析在无外网、非特权 Worker 中运行；
- DOCX 禁止宏，拒绝异常压缩比；
- PDF 禁止执行嵌入脚本；
- 解析产物和原文件分别保存。

## 11. 数据删除

删除流程：

1. 重新验证用户；
2. 创建不可重复的删除任务；
3. 立即吊销会话；
4. 标记账户不可登录；
5. 删除对象存储文件；
6. 删除或匿名化业务数据；
7. 写入不含个人内容的删除完成审计；
8. 72 小时内完成在线数据删除。

备份中的数据按备份生命周期最长 30 天自然淘汰，期间不得恢复到生产供正常访问。

## 12. 测试

- Service 规则使用单元测试；
- Repository 使用真实 PostgreSQL 集成测试；
- Redis/Celery 使用容器集成测试；
- 权限测试必须覆盖用户 A 请求用户 B 的每类资源；
- 事务失败测试在每个关键写入点注入错误；
- `createApiError` 契约使用快照测试；
- OpenAPI 差异由 CI 阻断。
