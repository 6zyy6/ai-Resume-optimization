# V2 当前实现差距审计

> 审计范围：2026-07-30 工作区  
> 判定对象：当前源码和本机运行版本，不引用计划中的“预计完成”  
> 总结：`0 个完整真实 Web 闭环；2 个认证页面基本打通；多数业务页面为静态壳或半真半假`

## 1. 判定标准

| 状态 | 判断方法 |
| --- | --- |
| `DONE` | 页面读取并写入真实 API，关键状态有自动化测试，刷新后结果仍由服务端恢复 |
| `PARTIAL` | 至少一个真实 API 动作存在，但页面仍展示固定业务数据、遗漏必要读取或恢复 |
| `STATIC` | 只有链接、文案或本地交互，没有读取/写入对应业务 API |
| `MISSING` | 规格要求的路由、状态或契约不存在 |
| `BLOCKED` | 工程完成但缺少真实外部环境证据 |

“页面能打开”“按钮能变色”“fixture 返回 200”均不能单独判为 `DONE`。

## 2. Web 路由审计

| 路由 | 当前状态 | 已实现 | 关键差距 | V2 目标数据源 |
| --- | --- | --- | --- | --- |
| `/` | `PARTIAL` | 双入口和事实保护文案 | 未登录入口直接进入受保护业务页；无会话分流 | 会话状态 + 安全 returnTo |
| `/login` | `DONE*` | 邮箱密码/验证码登录、错误映射、安全 returnTo | 当前为未提交工作区修改；无真实浏览器认证截图和 staging 证据 | Auth API |
| `/register` | `DONE*` | 邮箱验证码注册并设置密码 | 当前为未提交工作区修改；协议链接仍指向静态设置页 | Auth API |
| `/home` | `STATIC` | 入口卡、空状态文案 | 0 个 API 请求；最近简历、任务、用量都不是真实数据 | Resumes + Tasks + Usage |
| `/create` | `PARTIAL` | 六题有不同文案；每题写 Fact；最后创建 Resume | 固定题目不是经历雷达；刷新不恢复；跳过被写成事实；没有动态追问、事实确认、初始版本或草稿生成任务；标题固定为“产品运营实习” | Intake Session + Facts + Tasks + Resumes |
| `/imports/new/confirm` | `PARTIAL/P0` | 真实申请上传、PUT、确认、创建解析任务 | 页面固定展示七个模块；忽略真实 `draft_facts`；把“待用户确认”伪文本写入事实；删除和粘贴兜底按钮未接通 | Files + Imports + Tasks |
| `/imports/:id/confirm` | `PARTIAL/P0` | 动态路由存在 | 路由参数不用于读取待确认导入；上传与确认职责混在一页 | Import detail |
| `/resumes` | `STATIC/P0` | 页面布局 | 固定简历名、固定资源 ID、固定“今天” | Resume list |
| `/resumes/:id/edit` | `PARTIAL/P0` | 本地 reducer、20 步撤销、800 ms/15 s 保存机制、创建 Version | 不读取目标 Resume/Version；示例内容固定；角色/时间不进入快照；把最终文案重新创建为已确认 Fact；base version 固定；冲突按钮无行为 | Resume + Versions + Facts + Quality |
| `/resumes/:id/versions` | `STATIC` | 时间线布局 | 固定 v2/v3；查看与恢复按钮无 API | Resume versions + restore |
| `/jobs/new` | `PARTIAL/P0` | 创建 Job、发起 parse task、发起 Match | Job 标题固定；解析后展示固定两条要求；不读取、编辑或确认真实 requirements | Jobs + Tasks |
| `/jobs/:id/match` | `PARTIAL` | 读取 Match，按真实 items 汇总四类数量 | 明细只显示占位文案；没有真实 JD、事实链接、失败重试和逐项状态 | Match + Job + Facts |
| `/suggestions/:analysisId` | `PARTIAL/P0` | 决策 API 和 A/E/I/Z 键盘入口 | 页面建议、理由、风险、来源和编辑文本全部固定；未加载 suggestions；决策后只改本地标签 | Suggestions + Resume Version |
| `/facts` | `STATIC` | 分类和卡片布局 | 固定“课程产品设计”；新增、筛选、来源均无 API | Facts + sources |
| `/tasks` | `STATIC` | 队列/失败状态视觉 | 固定任务；取消和重试无 API；没有轮询/SSE | Tasks + events |
| `/exports/:id` | `PARTIAL` | 创建 Export、等待 Task、读取下载 URL | route id 未使用；预览固定；模板 select 不影响请求；没有恢复已有导出状态或失败重试 | Exports + Tasks |
| `/settings` | `STATIC/P0` | 设置布局 | 固定“邮箱已验证”；导出与删除按钮无 API；协议/隐私正文缺失 | Me + Privacy + Tasks |

`DONE*` 只表示代码与局部测试已完成，不代表已经满足同 commit 的全量证据和公开发布要求。

## 3. 硬编码边界

### 3.1 允许保留在前端

- 导航名称、页面标题和按钮文案；
- 枚举到中文标签的映射；
- 明确标注为示例的 placeholder；
- 空、加载、错误和权限拒绝说明；
- 不含用户状态的确定性经历雷达题库，且题目选择由后端会话决定；
- 模板名称、缩放档位和快捷键说明。

### 3.2 禁止出现在生产业务状态

- 用户简历名称、岗位名称、学校、项目、经历或建议文本；
- `resume_sample_01`、`version_targeted_01` 等示例资源 ID；
- “今天”“排队中”“导出失败”“邮箱已验证”等未经过 API 的状态；
- 固定任务数、事实数、用量或进度；
- 固定解析模块、JD requirements、match items、suggestions 和 fact sources；
- 用页面当前文案反向创建“已确认事实”来绕过证据要求。

## 4. 链路审计

| 链路层 | 当前结论 | 证据 |
| --- | --- | --- |
| Web → FastAPI | `PARTIAL` | Auth、Fact、Resume、Import、Job、Match、Suggestion、Export 的部分写动作存在 |
| Web 读取业务状态 | `FAIL` | 5 个一级业务页为静态；编辑、导入、JD、建议读取不完整 |
| FastAPI 契约 | `PARTIAL` | 核心模块端点存在；缺少可恢复的经历梳理会话和 `GET /v1/me` |
| PostgreSQL | `PARTIAL` | SQLAlchemy/Alembic 与 owner/幂等/不可变版本测试存在；Web 未正确消费 |
| Redis/Worker | `PARTIAL/BLOCKED` | 本地服务可启动；真实双副本 Redis 和真实模型取消证据缺失 |
| Pi | `PARTIAL/BLOCKED` | `next_question` 等工作流存在，但没有公开业务入口将其接到创建流程 |
| 对象存储 | `PARTIAL/BLOCKED` | 本地上传链路存在；真实 COS 全链路未验证 |
| E2E | `FAIL` | Playwright 用 `page.route("**/api/**")` 拦截全部 API，且当前测试文案已与页面不一致 |
| 截图证据 | `PARTIAL` | 有人工问题截图；过去验收 manifest 没有 V2 关键状态截图矩阵 |

## 5. 主要根因

1. **按路由交付，而不是按用户任务交付。** 文件列表把“创建页面”视为完成单位，没有要求该页真实恢复某个后端状态。
2. **测试替身覆盖面过大。** Fixture 拦截全部 API，使 E2E 可以在 FastAPI、Redis、Worker、Pi 全部不可用时继续通过。
3. **前端缺少明确的数据所有权规则。** 示例数据从设计占位进入生产组件，没有编译或验收门禁阻止。
4. **创建流程缺少一等业务资源。** Pi 有 `next_question`，数据库有 Fact，但中间没有可恢复、可去重、可审计的 intake session。
5. **证据定义偏后置。** 计划要求最终截图，但页面开发任务没有为每个加载、空、错误、成功状态规定截图和数据断言。

## 6. 必须先修复的 P0

1. 增加会话读取和受保护路由守卫；
2. 删除所有生产业务固定数据，并为静态壳先显示真实空/加载/错误状态；
3. 建立经历梳理会话和初稿生成任务；
4. 拆分上传页与导入确认页，真实渲染 `draft_facts`；
5. 编辑器先读取当前版本，禁止以最终文案自证事实；
6. JD、匹配和建议全部读取服务端资源；
7. 让工作台、简历、事实、任务、版本、设置使用真实 GET 接口；
8. 新增不拦截 API 的 real-service Playwright 项目；
9. 把真实浏览器截图、API/数据库断言和哈希写入同一 evidence manifest。

## 7. Hallmark 视觉审计摘要

当前设计的“克制、可信、Cobalt 强调色、事实工作台”方向可以保留，不需要为 V2 更换品牌。

需要修正的不是颜色，而是结构与诚实性：

- `critical`：多页使用相同“大标题 + 等宽卡片”模板，信息层级没有随任务变化；
- `critical`：静态示例业务内容看起来像真实数据，违反 honest copy；
- `major`：顶部五项导航在窄屏缺少真实折叠模型；
- `major`：“⌘ K 快速前往”没有命令面板行为，是伪交互；
- `major`：任务长等待只靠通用状态字样，没有真实阶段、耗时或恢复动作；
- `minor`：页面标题普遍过大，压缩了编辑和确认任务的首屏工作空间。

V2 视觉修复方案见[视觉与交互规格](./03-visual-and-interaction-spec.md)。
