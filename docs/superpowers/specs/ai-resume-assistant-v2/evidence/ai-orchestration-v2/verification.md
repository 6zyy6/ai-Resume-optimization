# AI 业务编排 V2.1 本地验证报告

## 1. 结论与边界

| 项目 | 状态 | 可核对结论 |
| --- | --- | --- |
| 实现冻结提交 S | `PASS` | `7877aa4f5db6a4a9742b92a3046a6a6dc9a09e55`，在 detached 干净 worktree 采集 |
| 本地 deterministic HTTP 编排 | `PASS` | Chrome 150 → Next.js production → FastAPI → 实际业务 operation → TCP Pi fixture；3/3 Playwright 通过 |
| 27 张候选截图与 manifest | `PASS` | 3 个视口 × 9 个状态；27 个 PNG SHA-256 全部匹配且互不重复 |
| Task/AiRun/Trace/Outbox | `PASS` | 每个视口均为 4 个成功 Task → 5 个成功 AiRun；失败 Task 为 `failed`；owner/outbox/trace 断言通过 |
| 当前源码 Hallmark 58 项 | `BLOCKED` | 历史报告不与候选源码同 commit；本候选只覆盖 390/1024/1440，不能替代七视口逐项复核 |
| 完整 Redis/Dispatcher/Celery 拓扑 | `BLOCKED` | 当前证据明确记录 `BLOCKED_NO_REDIS`，未伪造 broker 派发 |
| 真实模型、云、外部浏览器、用户研究 | `BLOCKED` | 当前没有凭据、部署或外部原始证据 |

本报告的 `PASS` 只覆盖本地确定性 HTTP 编排增量，不等同 Web V2 全量验收或公开发布
就绪。证据存储提交 T 在本目录提交后产生，因此 manifest 只冻结并校验测试源码提交 S；T
由 Git 历史记录。

## 2. 不可变候选证据

证据目录：[`candidate-7877aa4`](./candidate-7877aa4/)

关键元数据：

- 源码、构建、Web、API、Pi commit：`7877aa4f5db6a4a9742b92a3046a6a6dc9a09e55`；
- 浏览器：Google Chrome 150.0.7871.187；
- 开始：2026-08-02T11:28:05.484Z；结束：2026-08-02T11:28:38.749Z；
- 测试结果：3/3 通过，31.9 秒，退出码 0；
- 环境：Next.js production build/start + FastAPI + SQLite + in-process worker operation + TCP Pi deterministic fixture；
- 敏感模式扫描：邮箱、密码、Authorization、Bearer、access/refresh token 命中 0 个文件。

采集与复核命令：

```bash
node scripts/acceptance/capture-ai-orchestration-v2.mjs \
  docs/superpowers/specs/ai-resume-assistant-v2/evidence/ai-orchestration-v2/candidate-7877aa4

node scripts/acceptance/hash-ai-orchestration-v2.mjs \
  docs/superpowers/specs/ai-resume-assistant-v2/evidence/ai-orchestration-v2/candidate-7877aa4
```

`hash` 输出：

```text
Hashed 27 AI orchestration screenshots for 7877aa4f5db6a4a9742b92a3046a6a6dc9a09e55
```

独立复算结果：manifest 文件 hash 不匹配 0；截图 27；唯一截图 hash 27；三服务 SHA
不一致 0；三个视口 nonce hash 唯一数 3；console/page/server error 和可见内部标记均为 0。

## 3. 覆盖的真实链路

```text
Chrome → Next.js production build/start → FastAPI → TaskExecutor/业务 operation
                                             ↓
                                     SQLite Task/AiRun/Trace
                                             ↓ TCP
                                  Pi deterministic fixture
```

浏览器没有拦截 `/api/v1/**`，没有直接修改数据库推进流程。公开测试工具只以当前登录
owner 触发实际 `TaskExecutor` 并读取脱敏 inspection。因为本环境没有 broker，Outbox 保持
未派发，不能据此宣称 Redis/Dispatcher/Celery 已通过。

五条进入业务调用链的工作流：

1. `analyze_intake_answer`；
2. `compose_resume_draft`；
3. `parse_jd`；
4. `match_resume_to_jd`；
5. `generate_suggestions_batch`。

每条成功 AiRun 均核对 workflow version `2`、prompt template `@2`、input/receipt hash、
result reference 和连续 trace。owner 标识和专用 nonce 元数据使用 SHA-256；为了证明页面
显示的是当次真实业务数据，确定性测试的简历/JD 文本会包含可见 nonce。

## 4. 视觉状态证据

390×844、1024×768、1440×900 每个视口均保存以下 9 个全页状态：

1. 创建回答分析中；
2. 事实候选确认；
3. 模型草稿与事实来源；
4. JD 模型解析 provenance；
5. 匹配四分类；
6. 待处理建议；
7. 缺证据阻断建议；
8. 任务成功结果；
9. 可恢复失败。

自动测量结果：横向溢出 0、Runtime Error 0、console error 0、page error 0、API 5xx 0、
可见内部枚举/工作流名/JSON pointer 0。状态 01–07、09 显示本次业务 nonce；状态 08
通过 `task_id` 绑定对应数据库 `succeeded` Task。人工抽查了桌面与移动端的模型草稿、
任务成功和可恢复失败截图，未发现内容裁切或未本地化错误码。

## 5. 工程验证结果

下表是本轮在源码提交 S 上执行的本地开发检查；除候选目录中的 Playwright `command.log`
外，其他命令的完整原始日志没有纳入候选证据，因此不能单独据此把相应 V2 验收 ID 标成
正式 `PASS`。

| 命令 | 结果 | 说明 |
| --- | --- | --- |
| `node --test scripts/tests/dev-supervisor.test.mjs` | `PASS`，6/6 | 含服务启动失败后的 ready 竞态回归 |
| `pnpm vitest run tests/contract/ai-orchestration-v2.test.ts --reporter=verbose` | `PASS`，1/1 | 真实 HTTP 覆盖五工作流 |
| `AI_ORCHESTRATION_REAL_SERVICES=1 pnpm exec playwright test --project=ai-orchestration-real --reporter=line` | `PASS`，3/3 | 三视口、27 状态截图 |
| `pnpm --filter @resume/web test` | `PASS`，70/70 | 含质量问题中文映射与未知值安全回退 |
| `pnpm lint` | `PASS`，退出码 0 | 根工作区 lint |
| `pnpm test` | `PASS`，0 失败 | AI 94（另 1 跳过）、Shared 8、Token 2、小程序 12、Web 70、Supervisor 6、API 1387 |
| `pnpm build` | `PASS`，退出码 0 | evidence E2E 也从干净 worktree 重建 production Web |
| `pnpm acceptance` | `BLOCKED` | 命令退出码 0，但发布 manifest 的 146/146 项均因缺正式发布证据保持 `BLOCKED` |

原始 capture log 含现有工具链警告：Node `module.register`/`util._extend` 弃用、
`baseline-browser-mapping` 数据过期、`NO_COLOR`/`FORCE_COLOR` 冲突以及 standalone start
建议。根测试还产生 8 条既有 aiosqlite 连接线程在 event loop 关闭后的 pytest warning。
根构建还记录 Taro webpack cache 无法解析 `@tarojs/taro-loader/lib/page` 的 warning。
它们没有造成测试或构建失败，但根据 V2-ENG-01 的“warning 0”严格阈值，该验收 ID
仍不能标记为全量 `PASS`。

## 6. Hallmark 58 项当前状态

历史报告 `docs/superpowers/evidence/task-8-hallmark-review.md` 在较早提交上记录过 58/58
`PASS`，但从该提交到候选源码 `7877aa4`，Web 已发生实质修改，不能把历史结论直接沿用。
本次候选只对三个正式视口进行了真实截图和 0 溢出检查，未覆盖 320/375/414/768 的当前
源码证据；补跑历史 fixture 七视口套件也未在合理时间内完成，因此当前严格状态如下：

| Gates | 结果 | 增量检查 |
| --- | --- | --- |
| 1–9 | `BLOCKED` | 需在当前源码逐项复核 |
| 10–19 | `BLOCKED` | 需在当前源码逐项复核 |
| 20–27 | `BLOCKED` | 需在当前源码逐项复核八状态与 reduced motion |
| 28–36 | `BLOCKED` | 本次三视口横向溢出为 0；其余 gate 未形成当前源码证据 |
| 37–45 | `BLOCKED` | 需在当前源码逐项复核 |
| 46–49 | `BLOCKED` | 本次截图证明核心文案未泄漏内部枚举；其他 gate 未完整复核 |
| 50–58 | `BLOCKED` | 390/1024/1440 为 0 溢出；320/375/414/768 当前源码证据缺失 |

结论：当前源码 Hallmark 58 项为 `BLOCKED`，不宣称 58/58 `PASS`。Hallmark 的“58 项
gate”和 V2-UI-01/02 的“58 张截图”是两种证据，二者目前都未达到全量验收。

## 7. 未完成验收

| 范围 | 状态 | 具体缺口 |
| --- | --- | --- |
| V2-UI-01 / V2-UI-02 完整矩阵 | `BLOCKED` | 本增量为 27 张 AI 编排截图，不是 42 张 ready + 16 张异常截图 |
| V2-UI-08 Hallmark | `BLOCKED` | 历史 58/58 报告不与候选同 commit；当前仅有三视口增量复核 |
| V2-CREATE-09 / V2-OPT-01 | `BLOCKED` | 未执行各 10/10 完整 PDF 闭环 |
| V2-ENG-01 warning 0 | `BLOCKED` | capture 原始日志仍有上述工具链 warning |
| 真实 Redis/Dispatcher/Celery | `BLOCKED` | 无 Redis/容器运行环境及真实 broker 证据 |
| 真实 DeepSeek/云/COS | `BLOCKED` | 无生产凭据、账单、部署和真实模型评测证据 |
| Edge/Safari/真机 | `BLOCKED` | Chrome 开发环境不能替代外部浏览器与真机 |
| 30 名学生用户研究 | `BLOCKED` | 无每路径至少 15 人的原始记录 |

因此当前准确状态是：**AI 业务编排 V2.1 本地确定性 HTTP 增量已通过；Web V2 全量与
公开发布仍未通过。**
