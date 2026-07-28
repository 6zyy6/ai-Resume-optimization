# 九轮检查 · 第 7 轮 · 架构师（Intended vs. Implemented）

STATUS: ARCHITECT_3_DONE

审计对象：`8ab2abf`。本轮只读代码、测试和已有报告；未运行测试，未把“测试文件存在”当作“测试已经执行的发布证据”。

## 输入 → 处理 → 输出 → 验证点

- 输入：PRD、竞品调研、主规格、12 份子规格、实施计划、当前代码/测试、九轮 1–6 报告。
- 处理：程序化抽取 `11-acceptance-and-evidence.md` 中形如 `| PREFIX-NN |` 的验收行；逐条检查服务端执行点、测试断言和发布证据；只在三者均成立时允许 `IMPLEMENTED_AND_EVIDENCED`。
- 输出：146 条矩阵和 10 个边界型 mismatch。
- 验证点：抽取总数 146；分类计数 `ENG 10 / FLOW 14 / WEB 10 / MP 10 / AI 14 / FILE 12 / DATA 10 / PERF 12 / SEC 14 / OBS 12 / UX 8 / USER 10 / OPS 10`，合计 146，没有补号或凑数。

## 判定摘要

| 状态 | 数量 | 判断 |
| --- | ---: | --- |
| `IMPLEMENTED_AND_EVIDENCED` | 0 | 没有任何条目同时具备 `11` 规定的原始命令/环境/哈希/manifest 证据 |
| `IMPLEMENTED_NOT_EVIDENCED` | 6 | 实现和阈值型自动化断言已存在，但只有摘要报告，没有发布证据原件 |
| `PARTIAL` | 28 | 只覆盖部分资源、部分链路、部分环境或低于验收次数 |
| `NOT_IMPLEMENTED` | 112 | 当前 HEAD 没有对应执行链路 |
| `NOT_APPLICABLE_THIS_INCREMENT` | 0 | 146 条均是已确认公开 MVP 的发布要求，不能因当前只做到 Task 4 而从发布矩阵排除 |

当前 Task 4 增量可被描述为“Facts + immutable resume versioning 的本地自动化门禁通过”；不可描述为“系统可上线”。规格要求 Web 和小程序同时上线、完整 FastAPI/Pi/Worker/文件/导出/运维链路（主规格 48–70、110–148；交付清单 22–122），而 ledger 只记录到 Task 4（`progress.md:21-28`）。

## P0–P3 findings

### P0-1 Web 与小程序公开产品不存在

- 意图：两端同时上线且能力对等（主规格 `19-36`、`48-70`；验收 `FLOW-01..14`、`WEB-01..10`、`MP-01..10`）。
- 实际：实施计划仍把 Web/小程序放在 Task 8/9（`plan:539-673`）；当前仓库没有 `app/web` 或 `app/miniprogram`，ledger 仅到 Task 4（`progress.md:21-28`）。
- 受影响角色：所有目标学生；不存在可攻击对象，但这是公开发布的硬阻断。
- 复现：`rg --files app` 返回目录不存在；OpenAPI 之外没有客户端制品。
- 修复：完成 Task 8/9，并提交两端 E2E、真机、包体、响应式和同 commit 构建哈希证据。

### P0-2 Pi、Worker、Durable Task 与成本执行链未注册

- 意图：Pi 不直连业务库，Worker 调 Pi，任务持久化，成本 70/90/100% 门槛必须作用于每个 AI run（主规格 `110-136`；计划 `13-18`；验收 `AI-10..14`、`PERF-03..10`、`OBS-02..12`）。
- 实际：`create_app` 只注册 auth/usage/privacy/facts/resumes（`packages/api/app/main.py:81-119`）；Pi 和 Task 5/6 仍在计划 `374-482`。现有 usage 判定（`packages/api/app/modules/usage/service.py:219-301`）没有可调用的 AI 任务创建链路，因此不能证明成本阀门不可绕过。
- 攻击者/受害者：恶意或高频用户 / 个人运营者的模型额度与费用。
- 复现：枚举 OpenAPI，无 tasks/runs/internal Pi 路由；直接搜索不存在 Worker→usage admission→Pi 的调用链。
- 修复：以一个事务写 task/outbox/usage admission，Worker 仅消费已准入 task，Pi 内部鉴权且无 DB 凭据；为所有入口做并发额度和故障注入测试。

### P0-3 文件导入、对象存储和 PDF 导出门禁不存在

- 意图：只有全部声明有 confirmed + source 的版本可导出，原文件 24h、导出 7d（主规格 `48-70`、`138-148`；验收 `FILE-01..12`）。
- 实际：Task 7 尚未实现（`plan:483-538`）；`main.py:111-115` 无 imports/exports 路由。Task 4 的 persisted evidence projection（`evidence_projection.py:35-73`）只是可复用注册点，不是导出执行点。
- 攻击者/受害者：错误或恶意 AI 输出 / 求职学生与接收简历的招聘方。
- 复现：请求 `/v1/exports` 或 `/v1/imports` 得 404；不存在 PDF 生成前的 graph validation。
- 修复：导出必须按 version ID 调用 `load_version_evidence` 并在同一执行链阻断 unsupported/unconfirmed/source-less；补 50 次负例、文本一致和对象过期证据。

### P0-4 发布证据链和 manifest 不存在

- 意图：每项证据必须绑定同一 commit、制品哈希、执行时间、退出码和 SHA-256（验收 `13-61`；交付清单 `108-122`）。
- 实际：没有 `artifacts/acceptance/<release-id>/manifest.json`；第 6 轮仅保存汇总数字（`nine-round-06-delivery.md:63-74`），没有原始输出、时间、环境、制品摘要或证据哈希。
- 受影响角色：发布负责人、用户、事故复核者。
- 复现：`find artifacts/acceptance -type f` 无结果。
- 修复：Task 11 生成不可手改 manifest；每条引用原始证据并校验 SHA-256、commit_sha、构建哈希和复核者。

### P1-1 删除请求不是删除闭环

- 意图：20 个账户 72 小时内全部在线数据不可访问（验收 `SEC-10`；主规格 `138-148`）。
- 实际：请求仅创建 queued task、停用用户和撤销 session（`privacy/service.py:182-224`），仓库没有删除 Worker；Task 5 尚未实现（`plan:374-419`）。
- 攻击者/受害者：拥有数据库/备份访问权的内部角色或事故泄漏 / 已申请删除的用户。
- 复现：提交删除后直接查 facts/resumes 等表，业务行仍存在且 task 无消费者推进。
- 修复：实现可重试级联删除 Worker、对象删除、别名账户处理、删除 tombstone 和 72h/备份策略证据。

### P1-2 公开容量与单点故障边界未实现

- 意图：50 RPS、100 在线、API 多副本、Redis/Postgres/Provider 故障可控（计划 `11-23`；验收 `PERF-01..12`、`OPS-01..10`）。
- 实际：Task 10 尚未实现（`plan:674-730`），没有容器、LB、PgBouncer、Redis/Celery、k6、备份或回退制品。
- 攻击者/受害者：突发流量或单个故障 / 全体在线用户。
- 复现：无生产 compose/manifests/k6/故障演练结果。
- 修复：完成 Task 10，按验收负载持续 15 分钟并注入副本、Redis、Provider 故障。

### P1-3 Cookie 写接口缺少明确 CSRF 执行点

- 意图：20 个跨站写请求成功 0 次（验收 `SEC-03`）。
- 实际：会话 Cookie 设置 HttpOnly/Secure/SameSite=Lax（`auth/router.py:53-62`），但所有写路由仅依赖 Cookie session，未见 Origin/Referer、CSRF token 或 Fetch Metadata 校验（`auth/router.py:65-76`；`resumes/router.py:42-117`）。
- 攻击者/受害者：恶意站点 / 已登录用户。SameSite=Lax 降低风险，但不是验收要求的服务端执行与 20 次证据。
- 复现：构造跨站 form/fetch 和浏览器兼容矩阵；当前服务端没有可观察的 CSRF 拒绝分支。
- 修复：统一写请求中间件校验 Origin/Fetch Metadata 或双提交 token，并提交 20 个浏览器级攻击用例。

### P2-1 PostgreSQL 迁移和 legacy audit 只有 mock/SQLite 证据

- 意图：空库和上一版本升级成功（`ENG-06`），且生产事实源为 PostgreSQL（计划 `5-9`）。
- 实际：第 6 轮明确承认无真实 PostgreSQL 验证（`nine-round-06-delivery.md:71-74`）；PostgreSQL audit 测试是 mock connection（`test_task4_round5_adversarial.py:195-220`）。
- 受影响角色：部署人员与所有租户；风险是升级中断或 schema 漂移。
- 复现：在 PostgreSQL 运行 0001→0002、downgrade guard 和 legacy audit；当前没有结果。
- 修复：临时 PostgreSQL CI 服务执行迁移并保存 revision/schema/data hash。

### P2-2 存在第二套未注册的导出判定算法

- 意图：事实门禁应由不可变 persisted graph 统一复现（主规格 `42-46`；第 6 轮 `21-26`）。
- 实际：`quality.py:62-98` 仍保留基于 snapshot `fact_refs`、claim 分割和词项/数字启发式的 `check_exportable`；当前 service 质量路径改用 projection（`service.py:323-335`），但旧函数仍被测试并可能在未来 Export 注册时被误用。
- 受影响角色：实现 Task 7 的开发者 / 用户与招聘方。
- 复现：搜索 `check_exportable`，生产函数存在但没有与 persisted projection 绑定。
- 修复：Task 7 只暴露一个 projection-based export guard；删除或明确封存旧 helper，并用契约测试禁止 Export 导入它。

### P3-1 版本列表伪造 operation

- 意图：恢复历史创建新版本且操作可审计（验收 `FLOW-12`）。
- 实际：列表把每行包装成 `SavedVersion(..., "save")`（`resumes/router.py:75-84`），因此 restore 版本在列表中也显示 save；ledger 已记录此 deferred minor（`progress.md:21`）。
- 受影响角色：用户与审计人员。
- 复现：创建版本、restore、再 GET versions；restore 行 `operation == "save"`。
- 修复：查询 persisted `VersionOperation.operation_type`，并为 save/restore 列表加入断言。

## 146 条状态矩阵

格式：`ID@意图行`。每组末尾的“实现/证据引用”适用于该组每条；不存在对应代码时，以计划中的未来 Task 和 ledger 仅完成到 Task 4 作为可验证缺口，不用测试文件代替运行证据。

### IMPLEMENTED_NOT_EVIDENCED（6）

- `ENG-05@71`
- `DATA-04@199`
- `DATA-05@200`
- `DATA-06@201`
- `DATA-07@202`
- `DATA-09@204`

实现/断言：`packages/api/tests/test_task4_acceptance.py:250-616`、`packages/api/app/modules/resumes/service.py:166-418`。现有执行摘要：`nine-round-06-delivery.md:63-69`。缺口：不满足验收文档 `42-61` 的 manifest、原始日志、时间、环境和 SHA-256，因此不能进入 `IMPLEMENTED_AND_EVIDENCED`。

### PARTIAL（28）

- `ENG-01@67`, `ENG-02@68`, `ENG-03@69`, `ENG-04@70`, `ENG-06@72`, `ENG-07@73`, `ENG-10@76`
- `FLOW-06@87`, `FLOW-08@89`, `FLOW-09@90`, `FLOW-11@92`, `FLOW-12@93`, `FLOW-14@95`
- `AI-03@152`, `AI-05@154`
- `DATA-01@196`, `DATA-02@197`, `DATA-03@198`, `DATA-08@203`
- `PERF-10@224`
- `SEC-02@233`, `SEC-06@237`, `SEC-10@241`, `SEC-12@243`
- `OBS-01@251`
- `OPS-05@302`, `OPS-07@304`, `OPS-10@307`

实现/断言引用：

- 工程只覆盖当前包且无四类发布制品：`plan:30-166`、`nine-round-06-delivery.md:63-74`。
- 版本、冲突、隔离、restore：`service.py:68-335`、`test_resume_versions.py:92-338`、`test_task4_acceptance.py:190-616`；没有双端自动保存/E2E。
- 数字和 fact 引用只覆盖保存门禁：`service.py:338-522`、`test_task4_claim_evidence_contract.py:120-270`；没有 AI 评测或导出。
- DATA 只覆盖已实现资源，不是 12 类全资源：`test_task4_acceptance.py:190-616`。
- 成本只有 admission 规则：`usage/service.py:219-335`；没有真实 AI task 入口。
- Cookie/OTP/删除/无管理路由只具备局部实现：`auth/router.py:53-173`、`auth/service.py:276-316`、`privacy/service.py:182-224`、`main.py:111-119`。
- trace 仅在当前 FastAPI 请求传播：`core/middleware.py:16-27`；没有客户端、Worker、Pi、provider。
- ready/版本/迁移兼容仅为应用局部：`main.py:108-122`、`nine-round-06-delivery.md:71-74`。

### NOT_IMPLEMENTED（112）

- ENG：`ENG-08@74`, `ENG-09@75`。引用：`plan:674-789`、`progress.md:21-28`。
- FLOW：`FLOW-01@82`, `FLOW-02@83`, `FLOW-03@84`, `FLOW-04@85`, `FLOW-05@86`, `FLOW-07@88`, `FLOW-10@91`, `FLOW-13@94`。引用：`plan:483-673`、`progress.md:21-28`。
- WEB：`WEB-01@101`, `WEB-02@102`, `WEB-03@103`, `WEB-04@104`, `WEB-05@105`, `WEB-06@106`, `WEB-07@107`, `WEB-08@108`, `WEB-09@109`, `WEB-10@110`。引用：`plan:539-606`、`progress.md:21-28`。
- MP：`MP-01@116`, `MP-02@117`, `MP-03@118`, `MP-04@119`, `MP-05@120`, `MP-06@121`, `MP-07@122`, `MP-08@123`, `MP-09@124`, `MP-10@125`。引用：`plan:607-673`、`progress.md:21-28`。
- AI：`AI-01@150`, `AI-02@151`, `AI-04@153`, `AI-06@155`, `AI-07@156`, `AI-08@157`, `AI-09@158`, `AI-10@159`, `AI-11@160`, `AI-12@161`, `AI-13@162`, `AI-14@163`。引用：`plan:420-538`、`progress.md:21-28`。
- FILE：`FILE-01@179`, `FILE-02@180`, `FILE-03@181`, `FILE-04@182`, `FILE-05@183`, `FILE-06@184`, `FILE-07@185`, `FILE-08@186`, `FILE-09@187`, `FILE-10@188`, `FILE-11@189`, `FILE-12@190`。引用：`plan:483-538`、`main.py:111-119`。
- DATA：`DATA-10@205`。引用：`plan:420-482`、`main.py:111-119`。
- PERF：`PERF-01@215`, `PERF-02@216`, `PERF-03@217`, `PERF-04@218`, `PERF-05@219`, `PERF-06@220`, `PERF-07@221`, `PERF-08@222`, `PERF-09@223`, `PERF-11@225`, `PERF-12@226`。引用：`plan:674-730`、`progress.md:21-28`。
- SEC：`SEC-01@232`, `SEC-03@234`, `SEC-04@235`, `SEC-05@236`, `SEC-07@238`, `SEC-08@239`, `SEC-09@240`, `SEC-11@242`, `SEC-13@244`, `SEC-14@245`。引用：`plan:483-538,674-730`、`progress.md:21-28`。
- OBS：`OBS-02@252`, `OBS-03@253`, `OBS-04@254`, `OBS-05@255`, `OBS-06@256`, `OBS-07@257`, `OBS-08@258`, `OBS-09@259`, `OBS-10@260`, `OBS-11@261`, `OBS-12@262`。引用：`plan:420-482,674-730`、`progress.md:21-28`。
- UX：`UX-01@268`, `UX-02@269`, `UX-03@270`, `UX-04@271`, `UX-05@272`, `UX-06@273`, `UX-07@274`, `UX-08@275`。引用：`plan:539-673`、`progress.md:21-28`。
- USER：`USER-01@283`, `USER-02@284`, `USER-03@285`, `USER-04@286`, `USER-05@287`, `USER-06@288`, `USER-07@289`, `USER-08@290`, `USER-09@291`, `USER-10@292`。引用：`plan:731-789`、`progress.md:21-28`。
- OPS：`OPS-01@298`, `OPS-02@299`, `OPS-03@300`, `OPS-04@301`, `OPS-06@303`, `OPS-08@305`, `OPS-09@306`。引用：`plan:674-789`、`progress.md:21-28`。

## 发布判定

- Task 4 增量：实现层面达到本轮针对 fact-backed immutable versions 的局部门禁，仍缺严格发布证据原件和真实 PostgreSQL 证据。
- 整个系统：`FAIL / 不可上线`。理由不是“未来可能优化”，而是 112/146 未实现、28/146 部分实现、0/146 具备完整发布证据，且存在 4 个 P0。

