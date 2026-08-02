# Web V2 验收与证据标准

## 1. 状态与发布规则

每项只能是：

- `PASS`：阈值达到，且必需证据完整；
- `FAIL`：已执行但未达到阈值；
- `BLOCKED`：环境、账号、设备或外部依赖使测试无法执行。

禁止使用“基本通过”“代码看起来正确”“测试应该能过”“截图正常”等表述。

Web V2 的 `ENGINEERING READY` 必须满足：

- 本文全部 P0 为 `PASS`；
- Sev1、Sev2 未关闭数为 0；
- `pnpm lint`、`pnpm test`、`pnpm build` 退出码均为 0；
- real-service E2E 不拦截 `/api/v1/**`；
- 测试、截图、API/数据库断言和构建来自同一 commit；
- 证据 manifest 中所有文件 SHA-256 匹配。

公开发布还必须满足 V1 全局 P0，包括小程序、云、COS、真实模型、真浏览器和真实用户证据。Web V2 通过不能自动把公开发布状态改为 READY。

## 2. 证据的四个层次

关键 UI 验收必须同时有：

1. **自动化结果**：Vitest/Pytest/Playwright/axe/Lighthouse 原始结果；
2. **业务断言**：API 响应和数据库前后计数/hash/owner 断言；
3. **真实视觉证据**：浏览器截图，交互流程用 trace 或录像；
4. **可追溯元数据**：commit、UTC 时间、环境、命令、退出码、制品和 SHA-256。

单一证据不能替代其他层：

- 截图不能证明数据成功写库；
- 日志不能证明页面没有遮挡或错误文案；
- fixture E2E 不能证明 FastAPI/Worker/Pi 可用；
- 单元测试不能证明完整用户路径；
- 开发者工具模拟不能证明真实 Safari 或真机。

## 3. Manifest

目录：

```text
artifacts/acceptance/<release-id>/web-v2/
├── manifest.json
├── commands/
├── e2e-real-service/
├── e2e-contract-fixture/
├── api-db-assertions/
├── screenshots/
│   ├── 390x844/
│   ├── 1024x768/
│   └── 1440x900/
├── traces/
├── accessibility/
├── lighthouse/
├── bundle/
└── hallmark/
```

每个 manifest item 必须包含：

```json
{
  "id": "V2-FLOW-01",
  "priority": "P0",
  "status": "PASS",
  "commit_sha": "40-char sha",
  "build_id": "immutable id",
  "environment": "local-http-partial | local-real-services | staging | production",
  "started_at": "UTC ISO 8601",
  "finished_at": "UTC ISO 8601",
  "command": "exact command",
  "exit_code": 0,
  "evidence": [
    { "path": "relative/path", "sha256": "64 hex chars", "kind": "screenshot" }
  ]
}
```

若 working tree 非干净，当前证据只能标记 `BASELINE`，不得标记候选版本 `PASS`。

## 4. 工程与契约

| ID | P | 验收项 | 判断方法与通过数字 | 必需证据 |
| --- | --- | --- | --- | --- |
| V2-ENG-01 | P0 | 根命令 | lint/test/build 各执行 1 次，退出码 0，warning 0 | 原始日志、命令元数据 |
| V2-ENG-02 | P0 | OpenAPI 一致 | `pnpm generate` 后 tracked diff 为 0 | 生成日志、diff |
| V2-ENG-03 | P0 | 数据迁移 | 空库和上一 schema 各升级 3 次，成功 3/3；schema hash 一致 | Alembic 日志、hash |
| V2-ENG-04 | P0 | 路由守卫 | 无 Cookie 访问 20 个受保护 URL，跳登录 20/20；站外 returnTo 成功 0 | Playwright trace、URL 表 |
| V2-ENG-05 | P0 | API 边界 | Web 生产源码直连 Pi/DB/Redis/COS/provider 命中 0 | 静态扫描 |
| V2-ENG-06 | P0 | 写幂等 | Web 所有业务写请求缺 Idempotency-Key 数量 0 | 网络 trace、API 测试 |
| V2-ENG-07 | P0 | 未捕获错误 | 两条主流程中 Next Runtime Error overlay 出现 0 次；console error 0 | Playwright console 记录、截图 |
| V2-ENG-08 | P1 | Bundle | 编辑器首屏 JS gzip ≤350 KiB，不含懒加载 PDF 预览 | analyzer JSON |

## 5. 真实性与数据所有权

| ID | P | 验收项 | 判断方法与通过数字 | 必需证据 |
| --- | --- | --- | --- | --- |
| V2-DATA-01 | P0 | 无生产示例数据 | production bundle/source 扫描已知示例正文、示例 ID、伪任务状态命中 0；placeholder 白名单单独列出 | 扫描报告 |
| V2-DATA-02 | P0 | API 断开行为 | 关闭 API 后打开 12 个业务页，显示错误/空状态 12/12，示例业务卡出现 0 | 12 张截图、网络日志 |
| V2-DATA-03 | P0 | 唯一值传播 | 向测试库写入 20 个唯一 nonce，页面显示正确 20/20，未写入 nonce 显示 0 | DB seed、截图/OCR、API 响应 |
| V2-DATA-04 | P0 | owner 隔离 | A/B 两用户对 Web 涉及的 12 类资源各读写 5 次，跨 owner 成功 0 | 权限矩阵、SQL 断言 |
| V2-DATA-05 | P0 | 刷新恢复 | 随机 30 个已保存步骤刷新/重开，恢复正确 30/30，丢失 0 | E2E、服务端快照 |
| V2-DATA-06 | P0 | 正式状态来源 | 页面上的 resume/task/usage/fact/job/suggestion/export 状态各抽样 20 条，与 API 一致 100% | DOM/API diff |
| V2-DATA-07 | P0 | 静态配置白名单 | 前端硬编码只属于导航、枚举、空/错文案、placeholder、后端选定题库和模板元数据；越界 0 | AST 扫描、人工审查表 |

建议维护 `scripts/acceptance/web-business-literals-allowlist.json`。扫描失败必须人工分类，不能通过扩大通配白名单消除。

## 6. 认证和账户

| ID | P | 验收项 | 判断方法与通过数字 | 必需证据 |
| --- | --- | --- | --- | --- |
| V2-AUTH-01 | P0 | 注册 | 新邮箱验证码注册并设置密码 20/20；重复注册返回明确冲突 20/20 | real-service trace、API、DB |
| V2-AUTH-02 | P0 | 密码登录 | 正确密码 20/20；错误密码成功 0/20；限流阈值行为 100% | trace、API 日志 |
| V2-AUTH-03 | P0 | 验证码登录 | 发送、60 秒等待、10 分钟有效、一次消费均符合 20/20 | API/Redis 断言、截图 |
| V2-AUTH-04 | P0 | Cookie | HttpOnly/SameSite；生产 Secure；登出后旧会话成功 0/20 | 浏览器 Cookie 记录、API |
| V2-AUTH-05 | P0 | 返回地址 | 20 个恶意 returnTo 均不能站外跳转或执行脚本 | 安全测试 |
| V2-AUTH-06 | P0 | 会话过期恢复 | 10 次编辑中会话过期均保留本地输入，重新登录后返回原资源 10/10 | 录像、草稿/API diff |

## 7. 从零创建

| ID | P | 验收项 | 判断方法与通过数字 | 必需证据 |
| --- | --- | --- | --- | --- |
| V2-CREATE-01 | P0 | 会话持久化 | 创建/恢复 intake session 30/30；重复 POST 同 key 只建 1 个 | API、DB |
| V2-CREATE-02 | P0 | 问题差异 | 10 个不同画像各完成 ≥8 问；所有画像问题序列完全相同数量为 0 | 问题序列 JSON、统计 |
| V2-CREATE-03 | P0 | 已知不重问 | 100 个已确认字段中普通重复提问 ≤4，即 <5% | question/fact 对照 |
| V2-CREATE-04 | P0 | 澄清可解释 | 冲突、缺单位、角色模糊各 10 个样本，澄清 reason 正确 30/30 | schema/API、截图 |
| V2-CREATE-05 | P0 | 否定保护 | “没有/不知道/跳过”各 20 次，肯定 Fact 新增 0 | DB 前后计数 |
| V2-CREATE-06 | P0 | 事实确认 | confirmed Fact 无来源数量 0；确认/编辑/拒绝各 20 次状态一致 | DB/API |
| V2-CREATE-07 | P0 | 草稿原子性 | 10 次草稿任务均同时得到 Resume、首版 Version、evidence；部分提交 0 | 故障注入、DB |
| V2-CREATE-08 | P0 | 失败恢复 | next_question、bullet、draft 各注入失败 10 次，已保存回答丢失 0 | 故障 trace、截图 |
| V2-CREATE-09 | P0 | Web 闭环 | 新用户到成功获得 PDF 连续 10/10，无人工改库 | real-service trace、PDF hash、录像 |

## 8. 导入与优化

| ID | P | 验收项 | 判断方法与通过数字 | 必需证据 |
| --- | --- | --- | --- | --- |
| V2-IMPORT-01 | P0 | 文件规则 | 非允许格式、>10 MiB、>10 页、伪 MIME 各 10 次，进入解析成功 0 | API/浏览器 |
| V2-IMPORT-02 | P0 | 真实解析展示 | 30 个文件的页面字段与 `draft_facts` 逐字段一致率 100% | DOM/API diff、截图 |
| V2-IMPORT-03 | P0 | 确认门槛 | 用户确认前正式 Fact 新增 0；missing 生成“待确认”Fact 0 | DB |
| V2-IMPORT-04 | P0 | 失败兜底 | 扫描、加密、损坏各 3 个样本，原因正确且可粘贴/删除 9/9 | 录像、API |
| V2-IMPORT-05 | P0 | 删除源文件 | 删除 20 次后签名 URL 访问成功 0 | 存储清单、HTTP |
| V2-JOB-01 | P0 | JD 确认 | 页面 requirements 与 API 一致 20/20；编辑确认持久化 20/20 | DOM/API/DB |
| V2-MATCH-01 | P0 | 四类明细 | 200 个标注 requirement 分类准确率 ≥85%；页面 item 与 API 一致 100% | confusion matrix、DOM diff |
| V2-SUG-01 | P0 | 建议真实性 | requirement/original/suggested/reason/facts/risks 六字段与 API 一致 100/100 | DOM/API diff、截图 |
| V2-SUG-02 | P0 | 决策 | 接受、编辑、忽略、撤销各 20 次，页面、API、版本一致 100% | E2E、DB |
| V2-SUG-03 | P0 | 快捷键 | 非输入态 A/E/I/Z 各 20 次成功；输入态误触发 0/80 | Playwright |
| V2-SUG-04 | P0 | 基础隔离 | 20 个岗位版本创建后基础版本 hash 改变 0 | hash 报告 |
| V2-OPT-01 | P0 | Web 闭环 | 上传到岗位 PDF 连续 10/10，无人工改库 | real-service trace、PDF、录像 |

## 9. 编辑、任务和导出

| ID | P | 验收项 | 判断方法与通过数字 | 必需证据 |
| --- | --- | --- | --- | --- |
| V2-EDIT-01 | P0 | 服务端初始化 | 20 个不同版本页面内容与 API snapshot 一致 20/20；固定示例出现 0 | DOM/API diff |
| V2-EDIT-02 | P0 | 编辑能力 | 模块/条目增删、排序、拆分、合并各 20 次正确 100% | E2E、snapshot |
| V2-EDIT-03 | P0 | 自动保存 | 100 次随机编辑后 800 ms/15 s 保存符合规则；刷新丢失 0 | 网络时间线、DB |
| V2-EDIT-04 | P0 | 撤销 | 连续 20 步可逆，21 步越界不破坏数据 | 单元/E2E |
| V2-EDIT-05 | P0 | 冲突 | 20 次双会话编辑均显示差异；静默覆盖 0；三种处理均有测试 | 409 trace、截图、DB |
| V2-EDIT-06 | P0 | 禁止自证 | 保存 bullet 导致新 confirmed Fact 且来源仅为同一句最终文案的数量 0 | SQL 检查 |
| V2-TASK-01 | P0 | 任务真实性 | 100 个任务的 DOM status/stage/progress 与 API 一致 100% | timeline diff |
| V2-TASK-02 | P0 | 取消 | 20 个运行任务取消后 5 秒内无新 token/tool，业务写入 0 | Pi/Task/DB timeline |
| V2-EXPORT-01 | P0 | 阻断 | 含待确认或无来源声明的 50 次导出成功 0 | API/E2E |
| V2-EXPORT-02 | P0 | 一致性 | 20 个版本预览文本、PDF 提取文本和结构化版本一致 20/20 | 文本/hash diff |
| V2-EXPORT-03 | P0 | 模板 | 两模板切换 20 次，content hash 改变 0，template version 正确 40/40 | API/PDF |
| V2-EXPORT-04 | P0 | 失败恢复 | 导出失败 10 次后版本和模板选择保留 10/10，重试可成功 | trace、截图 |

## 10. 视觉、响应式和可访问性

| ID | P | 验收项 | 判断方法与通过数字 | 必需证据 |
| --- | --- | --- | --- | --- |
| V2-UI-01 | P0 | 三视口 | 390×844、1024×768、1440×900 的 14 个关键 ready 状态，水平溢出/裁切/控件遮挡均为 0 | 42 张全页截图、自动测量 |
| V2-UI-02 | P0 | 异常状态 | 8 个关键异常/异步状态在 390 和 1440 下问题为 0 | 16 张截图 |
| V2-UI-03 | P0 | 开发宽度 | 320/375/414/768 下关键流程 overflow 0，交互文字换行 0 | Playwright 测量 |
| V2-UI-04 | P0 | 八状态 | P0 交互组件缺失 default/hover/focus-visible/active/disabled/loading/error/success 的数量 0 | Story/页面截图、测试 |
| V2-UI-05 | P0 | 键盘 | 不使用鼠标完成两条主流程各 3 次，成功 6/6 | 录像、axe |
| V2-UI-06 | P0 | 对比与颜色 | axe serious/critical 为 0；正文 ≥4.5:1；灰度下状态仍可辨 | axe JSON、对比报告、截图 |
| V2-UI-07 | P0 | 200% 缩放 | Chrome 200% 下两条核心流程完成 2/2，阻断 0 | 录像 |
| V2-UI-08 | P0 | Hallmark | 58 项适用 gate 全部为 no；失败项 0 | gate 报告 |
| V2-UI-09 | P1 | Lighthouse | 营销首页 Performance ≥85；核心页 Accessibility ≥90 | Lighthouse JSON |
| V2-UI-10 | P1 | Web Vitals | staging LCP P75 ≤2.5 s，CLS P75 ≤0.1 | 原始数据 |

### 10.1 强制截图矩阵

14 个 ready 状态，每个在 390×844、1024×768、1440×900 截图，共 42 张：

1. 登录默认；
2. 注册默认；
3. 工作台空；
4. 工作台有数据；
5. 创建进行中；
6. 导入上传；
7. 导入确认；
8. 简历列表；
9. 编辑器；
10. 版本历史；
11. JD 确认；
12. 匹配报告；
13. 单条建议；
14. 导出成功。

8 个异常/异步状态，每个在 390×844、1440×900 截图，共 16 张：

1. 工作台局部 API 失败；
2. 创建任务运行；
3. 导入解析失败；
4. 编辑器保存中；
5. 编辑器离线；
6. 编辑器冲突；
7. 任务失败；
8. 导出被事实检查阻断。

最低截图数为 **58 张**。每张必须：

- 来自真实浏览器；
- 显示完整页面或明确裁剪区域；
- 记录 route、viewport、browser/version、build ID、UTC 时间；
- 使用脱敏测试数据；
- 有对应 API 响应或状态 seed；
- 生成 SHA-256；
- 不能由设计稿、DOM 拼图或生成图片代替。

## 11. Playwright 分层

### 11.1 Contract fixture 项目

允许 `page.route`，只验证：

- 请求路径；
- schema；
- unknown path/body 被拒绝；
- 客户端错误映射；
- 无后端环境下的确定性布局。

命名必须包含 `fixture`，报告不得用于 V2-FLOW/OPT 的 PASS。

### 11.2 Local real-services 项目

必须连接：

- Next Web；
- FastAPI；
- 测试 PostgreSQL；
- Redis；
- Dispatcher；
- Celery Worker；
- Pi deterministic runtime。

禁止：

- 拦截 `/api/v1/**`；
- 直接改数据库推进流程；
- 在前端注入 API response；
- 跳过任务等待。

允许通过公开测试工具创建隔离用户、清理数据和设置固定 AI 输出，但请求仍必须经过真实服务链。

### 11.2.1 Local HTTP partial 项目

当本机没有 Redis/Dispatcher/Celery 时，可以运行隔离的增量链路：Next production
build/start → FastAPI → 实际 Worker operation → TCP Pi deterministic fixture。该项目必须：

- 不拦截 `/api/v1/**`，不由浏览器直接写数据库；
- 使用迁移后的隔离 SQLite，并检查 owner、Task、AiRun、Trace 和业务结果；
- 在报告和 manifest 中把 broker 明确写为 `BLOCKED_NO_REDIS`；
- 对 Web、API、Pi `/version` 校验同一 source commit；
- 不把结果用于宣称真实 Redis/Celery、真实模型准确率、云、外部浏览器或用户研究 `PASS`。

此层可以作为 V2.1 编排接线和本地 UI 状态的增量证据，但不能替代 11.2 的完整
local real-services，也不能单独把 V2-CREATE-09、V2-OPT-01、V2-UI-01 或
V2-UI-02 改为 `PASS`。

### 11.3 Staging 项目

连接 staging 的真实存储、队列和 Pi，使用匿名测试账号。真实 Safari、Edge、COS、provider 和负载证据在此产生。

## 12. 截图复核方法

自动检查：

- `scrollWidth <= clientWidth`；
- 所有主控件 bounding box 在 viewport 内；
- 固定底栏不覆盖最后一个字段；
- h1、nav、CTA 的 `scrollWidth <= clientWidth`；
- 字体加载状态和 CLS；
- axe；
- screenshot SHA-256。

人工复核两人制：

- A 检查内容是否与 API seed 一致；
- B 检查层级、换行、遮挡、状态和文案；
- 任一人发现问题则该截图对应验收项为 FAIL；
- 复核记录包含姓名/标识、时间和结论。

## 13. 当前证据状态

本规格创建时：

- 局部 Web 单元测试和 Auth/API 后端测试已有通过记录；
- 当前 Playwright 主流程使用全量 API fixture，不能证明真实链路；
- 当前页面真实基线截图只能证明静态/半静态现状；
- V2 功能尚未实施，因此本文 V2-P0 不得标为 PASS。

当前实测索引见[当前基线证据](./06-current-baseline-evidence.md)。
