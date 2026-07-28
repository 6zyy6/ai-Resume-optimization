# AGENTS.md

## 项目结构

- `app/web/`: Next.js Web 应用
- `app/miniprogram/`: Taro 微信小程序
- `packages/api/`: Python 3.12 FastAPI 业务后端、Alembic 与 Celery Worker
- `packages/ai/`: Node.js Pi 内部 AI 工作流服务
- `packages/shared/`: OpenAPI 生成类型、跨端 API 客户端和共享工具
- `packages/design-tokens/`: Web 与小程序共用的语义设计 Token
- `packages/test-fixtures/`: 无云凭据环境使用的确定性测试夹具
- `infra/`: 本地容器、云部署、可观测性和告警配置
- `tests/`: Web E2E、部署契约、验收、性能和安全测试
- `docs/superpowers/specs/`: 系统规格与量化验收标准
- `docs/superpowers/plans/`: 已确认的实施计划

## 系统架构与不可破坏边界

### 总体拓扑

```text
Next.js Web ─────┐
                 ├── HTTPS ──> FastAPI ──> PostgreSQL
Taro 微信小程序 ─┘               │  │
                                │  ├──> 对象存储（本地/内存/COS 适配器）
                                │  └──> Redis + Outbox + Celery Worker
                                │                    │
                                └── 内网 HTTP ─────> Pi AI Service
                                                     │
                                                     └──> 模型供应商
```

- Web 与微信小程序同时上线、业务结果对等，但页面 UI 不跨端共享；只共享 API 契约、业务类型、语义 Token 和确定性夹具。
- FastAPI 是唯一公开业务入口和业务事实拥有者。认证、授权、幂等、配额、事实、简历版本、岗位、任务、文件、导出和审计都归 FastAPI。
- Pi 只负责 AI 编排、结构化输出校验、预算和链路事件；不得直连业务数据库，不得直接操作用户文件，不得拥有业务最终状态，不开放公网业务接口。
- PostgreSQL 是生产事实源；Redis 只用于队列、并发控制和短期协调，不能成为不可恢复的业务事实源。
- Celery Worker 分为 AI 与文件任务；公开请求不得同步等待模型或大文件解析/导出完成。
- 对象存储只保存上传源文件和导出文件；数据库保存 owner、object key、hash、MIME、大小、状态和过期时间。下载使用短期签名 URL。

### 公开请求与异步链路

```text
写请求
  -> 会话认证与 owner 解析
  -> Idempotency-Key 认领
  -> 单事务写入业务资源 + UsageLedger + Task + TaskEvent + Outbox + task_id
  -> 202 返回 task_id
  -> Dispatcher 投递固定队列
  -> Worker claim/lease
  -> 调用业务操作；AI 操作通过内网 HTTP 调 Pi
  -> 进度事件与 trace_id 串联
  -> 成功/失败/取消终态持久化
  -> 客户端轮询或 SSE 获取结果
```

- 业务资源、用量、Task 和 Outbox 必须在同一数据库事务提交；不得先创建 `queued` 资源再另开事务创建 Task。
- 写接口必须支持 24 小时幂等：同 key 同 body 重放原响应，同 key 不同 body 返回 `409 IDEMPOTENCY_KEY_REUSED`。
- 每个 owner 查询都必须在 SQL 中带 owner 条件；不能先按主键读取再在 Python 中判断 owner。幂等重放查询同样适用。
- Worker 使用 claim token、租约、最多三次瞬态重试。HTTP 429/5xx、连接失败和超时属于瞬态；校验失败、权限失败和不支持内容属于永久失败。
- 默认队列固定为 `ai.interactive`、`ai.batch`、`file.parse`、`file.export`、`privacy`，新增队列必须同步修改部署、告警、测试和验收证据。

### 业务事实、简历版本与建议

- 事实状态只允许 `unconfirmed`、`confirmed`、`rejected`。`confirmed` 事实必须至少有一个同 owner 来源。
- 简历版本不可变；修改、建议接受/编辑/忽略/撤销都创建新 `ResumeVersion` 和 `VersionOperation`，不得原地修改历史快照。
- `bullet_fact_links` 保存版本创建时的 claim range、事实值、事实状态和来源 hash 快照。导出只信任这些不可变证据，不信任快照自报的 `fact_refs`。
- 每个可导出原子声明必须 100% 被已确认、有来源、语义相关的事实覆盖；未确认、无来源、新增数字或无证据声明的导出成功次数必须为 0。
- 基础简历 `base` 不因岗位建议改变。岗位优化必须创建或复用唯一的 `job_targeted` 简历，并绑定 `base_resume_id + job_description_id`。
- Match 在 `succeeded` 前不得公开可决策建议。Pi 最终结果必须原子同步 `MatchItem`、`Suggestion` 和 `SuggestionFactLink`；列表展示的来源必须直接读取建议审计链接。
- 建议快捷键 A/E/I/Z 只在焦点不位于输入控件时生效；冲突使用基础版本/hash 检测，禁止静默覆盖。

### 文件与安全边界

- 导入只允许 PDF、DOCX、TXT，单文件不超过 10 MiB，PDF 不超过 10 页；不做 OCR。
- 必须同时校验扩展名、声明 MIME、magic bytes 和解析结果。拒绝加密/损坏文件、DOCX 宏/ActiveX/压缩炸弹，以及 PDF Catalog、Page、Annotation、AcroForm、Names 中的 JavaScript 或可执行动作。
- 上传源文件在解析确认后最长保留 24 小时；导出 PDF 保留 7 天；过期清理必须幂等并有存储清单证据。
- 本地/内存/COS 适配器行为保持一致：签名绑定 action、object key、expiry、owner scope；下载名使用 RFC 5987；生产密钥只来自环境变量。
- 不持久化完整 AI Prompt、thinking/reasoning、真实供应商响应正文、访问令牌或密钥。日志和 trace 事件必须脱敏。

### 容量、配额与发布基线

- MVP 基线：1,000 DAU、100 峰值在线、50 RPS、10–20 AI 并发、5–10 文件任务并发。
- 每用户每日最多 20 个 AI 任务、同时运行最多 2 个；全局 AI 日成本上限 100 元，70% 告警、90% 降级、100% 停止新 AI 任务。
- 生产至少 2 个 API 副本；AI Worker 与文件 Worker 独立扩缩容。数据库和 Redis 不暴露公网。
- 所有服务提供 live/ready/version；发布使用不可变 `${COMMIT_SHA}` 镜像，支持 10%/30 分钟金丝雀和非破坏性迁移回滚边界。
- 本地或测试夹具通过不代表真实云、真机、真实模型、备案/主体或用户研究通过；缺少外部证据时验收状态只能是 `BLOCKED`，不得写 `PASS`。

### 契约与证据

- Pydantic/FastAPI 是公开 API 契约源；`packages/shared/generated/` 只能由 `pnpm generate` 生成。
- 前端只调用 FastAPI；不得从 Web/小程序直连 Pi、数据库、Redis、COS SDK 或模型供应商。
- Pi 输入输出使用严格 schema，`additionalProperties: false`；FastAPI 与 Pi 必须有跨服务真实 schema 测试，不能只验证 HTTP mock 的字段集合。
- 验收以 `docs/superpowers/specs/ai-resume-assistant/11-acceptance-and-evidence.md` 为准。每个验收 ID 只能是 `PASS`、`FAIL`、`BLOCKED`，`PASS` 必须附命令、时间、commit、环境、原始日志或截图及 SHA-256。

## 常用命令

- `pnpm install`
- `pnpm dev`
- `pnpm lint`
- `pnpm test`
- `pnpm build`

## 代码约定

- Web UI 优先复用 `app/web/components/ui`；小程序 UI 优先复用 `app/miniprogram/src/components/ui`
- Web 与小程序 UI 设计、实现和审查使用 Hallmark；开始编码前先扫描现有字体、色板、间距、动效和框架，不覆盖既有设计系统
- 颜色、字体、间距、字号、圆角和动效统一引用 `packages/design-tokens` 中的命名 Token，不在组件内临时写 hex、rgb、OKLCH 或独立字体栈
- 不虚构用户数、提升比例、评价、合作品牌或案例；没有真实数据时使用明确占位或改用非数据型布局
- 交互组件必须覆盖 default、hover、focus-visible、active、disabled、loading、error、success 八种状态
- 接口错误统一走 `createApiError`
- 共享类型放在 `packages/shared`，不要在页面内重复声明

## 验证要求

- 修改业务逻辑后运行 `pnpm test`
- 修改类型或接口后运行 `pnpm build`
- 修改 UI 后至少检查 320、375、414、768 px，并执行规格要求的 390、1024、1440 px 布局检查
- 页面交付前运行 Hallmark 58 项 slop test；任一项失败先修复，不以截图主观判断替代

## 计划执行

- 用户已经提供或确认实施计划后，直接按该计划持续执行，不反复重写、扩写或重新确认计划。
- 只有出现实际阻断、规格冲突、验收失败，或需要用户新增授权时才暂停并说明。
- 进度更新聚焦已完成内容、当前验证证据和真实阻断，不重复复述计划正文。

## 禁止事项

- 不提交 `.env` 或任何密钥
- 不手改 `generated/` 目录
- 不重置用户已有改动
- 新增生产依赖前先说明原因

## 最终回复

- 总结改动
- 列出验证命令
- 说明风险、限制或未覆盖测试
