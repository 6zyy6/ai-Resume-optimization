# AI 业务编排 V2.1 本地验证报告

## 1. 当前证据状态

| 项目 | 状态 | 判断 |
| --- | --- | --- |
| 实现冻结提交 | `BLOCKED` | 等待源码提交 S 生成后填入 |
| 27 张候选截图与 SHA-256 manifest | `BLOCKED` | 必须从干净、detached 的源码提交 S 复采 |
| 完整 Redis/Dispatcher/Celery 拓扑 | `BLOCKED` | 当前环境未提供 Redis 或容器运行时 |
| 真实模型、云、外部浏览器、用户研究 | `BLOCKED` | 当前无对应凭据和外部原始证据 |

本文件在源码冻结前只定义可执行证据模板，不把 dirty worktree 下的开发调试结果写成
候选 `PASS`。候选证据提交 T 必须明确记录“测试源码提交 S、证据存储提交 T”。

## 2. 候选采集命令

```bash
node scripts/acceptance/capture-ai-orchestration-v2.mjs <new-evidence-directory>
node scripts/acceptance/hash-ai-orchestration-v2.mjs <new-evidence-directory>
```

`capture` 必须在干净工作树运行，并自动使用 `git rev-parse HEAD` 作为 Web、API、Pi
共同的 `APP_COMMIT_SHA`。`hash` 必须拒绝以下任一情况：

- PNG 不是 `3 × 9 = 27` 张；
- 任一视口不是 390×844、1024×768、1440×900，或 full-page 高度小于视口；
- 任一状态存在横向溢出、Runtime Error、console/page error、API 5xx 或可见内部枚举；
- 9 个固定状态名、route 或 PNG 文件名不完全一致，或 27 张截图内容 hash 不唯一；
- 每个视口没有证明 4 个成功 Task → 5 个 AiRun → 连续 Trace，或失败 Task 终态不完整；
- 任一服务 `/version` 与源码提交 S 不一致；
- broker 状态没有如实记录为 `BLOCKED_NO_REDIS`。

## 3. 本地 partial 链路

```text
Chrome → Next.js production build/start → FastAPI → TaskExecutor/业务 operation
                                             ↓
                                     SQLite Task/AiRun/Trace
                                             ↓ TCP
                                  Pi deterministic fixture
```

浏览器不得拦截 `/api/v1/**`，也不得直接改数据库推进流程。公开测试工具只负责以当前
登录 owner 触发真实 `TaskExecutor` 和读取脱敏 inspection；Outbox 因无 broker 保持未派发，
不能伪造为 Celery 已处理。

## 4. 必须覆盖的视觉状态

每个视口都必须保存下列 9 个状态：

1. 创建回答分析中；
2. 事实候选确认；
3. 模型草稿与事实来源；
4. JD 模型解析 provenance；
5. 匹配四分类；
6. 待处理建议；
7. 缺证据阻断建议；
8. 任务成功结果（任务中心原生成功状态，并绑定对应 Task 数据库 `succeeded` 断言）；
9. 可恢复失败。

状态 01–07 和 09 必须通过简历名称、确认事实、岗位名称或 JD 要求展示当次 run 的
真实业务 nonce；状态 08 通过 `task_id` 同 4 个成功 Task 断言之一绑定。报告只保存 nonce
和可见 proof 的 SHA-256，不把邮箱或 owner 明文写入证据。

截图中文案不得直接出现 `experience`、`fact_candidate_edit`、
`proved/underexpressed/needs_confirmation/real_gap`、工作流 snake_case 或简历 JSON
pointer；对应业务值只允许在 API/DB 报告中保留。

## 5. 自动验证清单

| 命令 | 候选要求 |
| --- | --- |
| `node --test scripts/tests/dev-supervisor.test.mjs` | `PASS`，0 失败 |
| `pnpm vitest run tests/contract/ai-orchestration-v2.test.ts --reporter=verbose` | `PASS`，真实 HTTP 覆盖 5 工作流 |
| `AI_ORCHESTRATION_REAL_SERVICES=1 pnpm exec playwright test --project=ai-orchestration-real --reporter=line` | `PASS`，3/3 viewport、27/27 状态 |
| `pnpm generate` 后前后 SHA 比较 | `PASS`，generated 文件二次生成不变 |
| `pnpm lint` | `PASS`，退出码 0 |
| `pnpm test` | `PASS`，0 失败 |
| `pnpm build` | `PASS`，退出码 0 |
| `pnpm acceptance` | 本地项按证据判定；外部项保持 `BLOCKED` |

## 6. Hallmark 58 项报告模板

预检目标：`P5 H5 E5 S5 R5 V4`。本轮不改变结构、Token 或样式，只修复运行时链路和
用户可见业务标签。候选截图生成后逐项填入 `NO` 或 `N/A`；任一适用项为 `YES`，则
V2-UI-08 为 `FAIL`，不能交付。

| Gates | 候选检查重点 | 待填答案 |
| --- | --- | --- |
| 1–9 | 字体、渐变、卡片、Hero、结构节奏 | `BLOCKED` |
| 10–19 | 动效、焦点、提示、占位文案 | `BLOCKED` |
| 20–27 | CSS stamp、Token、间距、八状态、reduced motion | `BLOCKED` |
| 28–36 | enrichment、图标、ARIA、横向溢出、交互行对齐 | `BLOCKED` |
| 37–45 | 字体数量、输入状态、对比、导航/页脚/Hero | `BLOCKED` |
| 46–49 | 真实文案、chrome、Token、可点击文案换行 | `BLOCKED` |
| 50–58 | 320/375/414/768 响应式与移动安全 | `BLOCKED` |

已有 CSS 顶部 Hallmark stamp 和历史 58 项报告只能作为审查起点，不能替代当前 source
commit 的真实浏览器复核。

## 7. 候选结论模板

| 范围 | 状态 | 边界 |
| --- | --- | --- |
| V2.1 deterministic 本地 HTTP 编排 | `BLOCKED` | 待不可变源码提交 S 的候选 manifest |
| V2-UI-01 / V2-UI-02 完整矩阵 | `BLOCKED` | 本增量只有 27 张，不是规格 42+16 张 |
| V2-CREATE-09 / V2-OPT-01 | `BLOCKED` | 未执行各 10/10 完整 PDF 闭环 |
| 真实 Redis/Celery/DeepSeek/云/外部浏览器/用户研究 | `BLOCKED` | 不由 deterministic fixture 推升状态 |
