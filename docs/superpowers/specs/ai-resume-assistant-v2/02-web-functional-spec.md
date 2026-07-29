# Web V2 功能规格

## 1. 功能完成定义

一个页面只有同时满足以下条件才算完成：

- 首次加载从真实 API 恢复业务状态；
- 所有写操作使用共享客户端和幂等键；
- 明确实现 loading、empty、ready、saving/running、error、success，以及适用时的 offline、conflict、cancelled；
- 刷新后不会退回固定示例或丢失已保存输入；
- 401 跳转登录，403、404、409、422、429、5xx 有不同的可恢复文案；
- 对应组件测试、真实服务 E2E、API/数据库断言和截图齐全；
- 页面中不存在未标注的示例用户数据。

## 2. 全局会话与路由

### 2.1 路由分组

公开路由：

```text
/
/login
/register
/legal/user-agreement
/legal/privacy-policy
```

受保护路由：

```text
/home
/create
/imports/*
/resumes/*
/jobs/*
/suggestions/*
/facts
/tasks
/exports/*
/settings
```

### 2.2 会话守卫

- 受保护 layout 在渲染业务内容前请求 `GET /v1/me`；
- 401 跳转 `/login?returnTo=<站内路径>`；
- `returnTo` 只接受以单个 `/` 开头且不以 `//` 开头的站内路径；
- 已登录访问 `/login` 或 `/register` 时跳转安全 returnTo 或 `/home`；
- 会话过期时保留当前未同步草稿，登录后返回原路由；
- 禁止仅依赖“页面请求业务 API 后自然报 401”充当路由守卫。

### 2.3 全局数据规则

- 页面不得直接调用 Pi、数据库、Redis、COS SDK 或模型供应商；
- 所有 HTTP 通过 `createWebApiClient`；
- 列表响应使用服务端 cursor，不在浏览器伪分页；
- 服务端资源状态与本地 UI 状态分开命名；
- 正式资源不得只保存在 React state 或 localStorage；
- localStorage/IndexedDB 只可保存未同步编辑草稿，必须带 owner、resource、base_version 和过期时间，登录退出时清除。

## 3. 工作台 `/home`

### 3.1 数据请求

页面并行请求：

```text
GET /v1/resumes?limit=3
GET /v1/tasks?limit=5
GET /v1/me/usage
```

三个区块独立失败；用量失败不能阻止最近简历显示。

### 3.2 内容

主区域只显示一个“下一步”：

- 没有简历：`开始梳理经历`；
- 有未完成 intake：`继续经历梳理`；
- 有导入待确认：`确认导入结果`；
- 有最近编辑版本：`继续编辑 <简历名>`；
- 有失败任务：先显示可恢复失败；
- 全部完成：`按岗位优化`。

次级区域：

- 最近 3 份真实简历；
- 进行中和失败任务，最多 5 条；
- 当日 AI 使用量、20 次日限额和恢复时间；
- “导入已有简历”作为次级入口。

### 3.3 状态

- 首次加载使用结构骨架，不显示 0 或伪内容；
- 空状态必须说明“尚未创建简历”并给一个动作；
- 部分失败在对应卡片内重试，不整页错误；
- 用量达到 90% 和 100% 时解释降级，但编辑和下载仍可用。

## 4. 从零创建 `/create`

### 4.1 首次进入

1. `POST /v1/intake-sessions` 创建或复用未完成会话；
2. 服务端返回当前问题、已完成数量、剩余估计和已有事实摘要；
3. 如果存在未完成会话，页面提供“继续”与“重新开始”；重新开始不得删除已确认事实；
4. 问题不是固定六步线性表单，而是后端控制的状态机。

### 4.2 问题类型

- 单选：有 / 好像有 / 没有 / 跳过；
- 短答：≤300 个中文字符；
- 深挖开放题：≤1,000 个中文字符；
- 数字确认：值、单位、范围和含义分开；
- 冲突澄清：明确展示两条冲突来源；
- 事实确认：确认、编辑、不采用、查看来源。

### 4.3 回答提交

```text
POST /v1/intake-sessions/{session_id}/answers
Idempotency-Key: <uuid>
```

请求至少包含：

- `question_id`
- `answer` 或 `skipped=true`
- `base_version`

行为：

- 按下保存后立即禁用重复提交；
- API 成功才将问题标为已回答；
- 如果返回任务，显示 `queued/running/waiting_for_user/failed` 的真实阶段；
- “没有”“不知道”“跳过”保存为回答状态，不创建肯定 Fact；
- 生成的 Fact 初始为 `unconfirmed`，用户确认后才变为 `confirmed`；
- 当前回答失败时保留输入，提供重试，不前进到下一题。

### 4.4 恢复与后退

- URL 包含会话 ID 或服务端能按用户恢复最近会话；
- 刷新后恢复最后成功保存的问题；
- 浏览器后退只改变浏览位置，不删除服务端事实；
- 未同步输入使用本地草稿，界面明确显示“未同步”；
- 另一端已改变会话版本时返回 409，用户选择本地、云端或逐字段合并。

### 4.5 草稿生成

用户至少确认 2 段经历后可发起：

```text
POST /v1/intake-sessions/{session_id}/drafts
```

返回 `202 + task_id`。成功必须原子创建：

- 一个 `base` Resume；
- 首个不可变 ResumeVersion；
- 每个 bullet 的 claim evidence；
- 生成任务和事件。

不能只创建空 Resume 后把用户送进固定编辑器。

## 5. 文件导入

### 5.1 上传 `/imports/new`

- 上传前展示 PDF/DOCX/TXT、10 MiB、PDF 10 页、不做 OCR；
- 客户端预检扩展名、声明 MIME 和大小，但最终以服务端检查为准；
- 申请 upload token、PUT、confirm-upload、create import；
- 上传进度和解析阶段分开；
- 文件选择后可移除或更换；
- 解析失败提供粘贴文本和删除源文件；
- 页面离开后可在任务中心继续查看。

### 5.2 确认 `/imports/:id/confirm`

页面读取：

```text
GET /v1/imports/{id}
GET /v1/tasks/{task_id}
```

要求：

- 只渲染服务端 `draft_facts` 和解析状态；
- 按基本信息、教育、实习、项目、校园、技能、荣誉分组；
- 每个字段显示来源摘录和 `confirmed/uncertain/missing`；
- 用户可编辑、删除、改变模块类型；
- `missing` 是空状态，不生成“待用户确认”文本 Fact；
- 提交 `POST /v1/imports/{id}/confirm` 时只发送用户保留的真实字段；
- 确认成功后创建基础 Resume 和首个 ResumeVersion，再进入 JD 或编辑器；
- 删除源文件真实调用 `DELETE /v1/files/{file_id}`，成功后签名 URL 不可访问。

## 6. 我的简历 `/resumes`

### 6.1 读取

```text
GET /v1/resumes?limit=20&cursor=...
```

显示：

- 标题、kind、更新时间、当前版本、目标岗位；
- 基础版本和岗位版本的从属关系；
- 是否有未完成任务或导出阻断；
- 编辑、历史、岗位优化、预览。

禁止：

- 固定示例 ID；
- 在没有 API 字段时显示“今天”；
- 把空列表替换成示例简历。

### 6.2 空与错误

- 空：解释基础简历和岗位版本的区别，主按钮“开始梳理经历”；
- 错误：保留新建入口，列表区域提供重试；
- cursor 加载失败不清空已经显示的数据。

## 7. 结构化编辑器 `/resumes/:id/edit`

### 7.1 初始读取

```text
GET /v1/resumes/{id}
GET /v1/resumes/{id}/versions?limit=1
POST /v1/resumes/{id}/quality-checks
```

页面必须以服务端最新版本初始化。若尚无版本，显示明确空状态，不注入示例教育、项目或技能。

### 7.2 编辑能力

- 模块和条目增删、排序；
- 标题、组织、角色、时间、地点和 bullet 编辑；
- bullet 拆分与合并；
- 本地撤销 20 步；
- 桌面拖拽与键盘按钮排序结果一致；
- 所有字段进入 snapshot，不能只显示不保存；
- 编辑中的事实引用同步显示，缺失来源的 claim 标为“无法导出”。

### 7.3 保存

- 停止输入 800 ms 自动保存；
- 15 秒兜底保存；
- 保存 `POST /v1/resumes/{id}/versions`，携带真实 `base_version`；
- snapshot hash 未变化不创建版本；
- 只有服务端返回成功才显示“已保存”；
- 保存失败不清空本地编辑；
- 409 停止自动保存，显示真实本地/云端差异；
- 冲突三个按钮必须都有真实行为和回归测试。

禁止通过“把当前 bullet 新建成 confirmed Fact”来让文案自证。新增事实必须来自用户明确确认的源回答或编辑，并能展示源文本与确认动作。

### 7.4 质量

质量面板读取真实检查结果：

- issue code、路径、说明；
- 一键定位字段；
- 来源覆盖；
- 未确认数字、角色、工具和结果；
- 导出阻断原因。

## 8. 版本历史 `/resumes/:id/versions`

- 读取真实版本 cursor 列表；
- 显示创建时间、operation、父版本和 snapshot hash 短值；
- “查看”打开只读快照；
- “恢复”调用 restore API，并创建新版本；
- 历史行不得原地修改；
- 恢复前后显示版本 ID，不能只变本地标签。

## 9. JD 与匹配

### 9.1 JD `/jobs/new`

- 用户填写岗位名称、公司可选、JD 原文；
- 创建 Job 后发起 parse task；
- 任务完成后重新 `GET /v1/jobs/{id}`；
- 展示真实 requirements；
- 用户可编辑 type、priority、text 和 confirmed；
- 至少一条 confirmed requirement 后才能匹配；
- 不以固定“用户研究”“内容策划”替代解析结果。

### 9.2 匹配 `/jobs/:id/match`

- 读取真实 Match 和 Job；
- 顶部显示四类真实数量，不显示 ATS 总分；
- 每个 item 展示 requirement、category、事实证据、当前简历位置和下一步；
- `needs_confirmation` 可直接进入补充事实；
- `real_gap` 明确说明不能靠改写补齐；
- 任务失败保留简历与 JD，提供重试；
- 匹配成功前不得请求或展示可决策建议。

## 10. 建议 `/suggestions/:analysisId`

- 首次读取 `GET /v1/match-analyses/{analysisId}/suggestions`；
- 当前 suggestion 由 URL ID 在响应中查找，找不到显示 404；
- 展示服务端 requirement_text、original_text、suggested_text、reason、fact_refs、risk_flags、status；
- fact_refs 可打开来源抽屉；
- Edit 打开真实文本输入，并把用户文本发送到 edit API；
- Accept/Edit/Ignore/Revert 成功后读取或使用响应返回的新 ResumeVersion；
- 操作失败回滚本地状态；
- A/E/I/Z 在输入、选择、contenteditable 和对话框中不触发；
- 不提供“全部接受”。

## 11. 事实库 `/facts`

- `GET /v1/facts` 支持状态、kind、搜索和 cursor；
- 卡片显示 value、status、experience、更新时间和来源数；
- 展开来源调用 `GET /v1/facts/{id}/sources`；
- 新增、编辑、确认、拒绝调用对应 API；
- 确认前必须至少一条同 owner 来源；
- 已拒绝事实默认折叠但可筛选；
- 无来源风险同时用图标、文字和颜色表达。

## 12. 任务中心 `/tasks`

- `GET /v1/tasks` 读取真实列表；
- 运行任务通过 SSE events 或退避轮询更新；
- 显示 type、stage、progress、开始时间、最近事件、error_code；
- 只有允许取消的 queued/running 任务显示取消；
- 取消调用 API 并等待真实终态；
- 失败任务的“重试”回到对应资源页面重新发起，不伪造 succeeded；
- 任务完成后可直接打开 result_ref。

## 13. 预览与导出 `/exports/:id`

- 若 route id 为 `new`，从 `version` 查询参数创建导出；否则读取已有 export；
- 模板选择进入请求体；
- 预览和导出使用同一渲染资源，显示 template_version；
- 100%、适宽、整页只改变视图，不改变文件内容；
- 导出前显示真实质量检查和阻断项；
- running 显示任务阶段，失败保留版本和模板选择；
- succeeded 后显示真实预览与下载 URL；
- 下载文件名按服务端 Content-Disposition，不在前端猜测；
- URL 过期时重新申请或刷新 export，不展示死链接。

## 14. 设置 `/settings`

读取 `GET /v1/me` 和 `GET /v1/me/usage`，显示：

- 脱敏邮箱和身份类型；
- 当前协议/隐私版本；
- 当日 AI 用量；
- 数据副本任务；
- 删除账户与数据。

动作：

- 数据导出调用 `POST /v1/me/data-exports`；
- 删除调用 `POST /v1/me/deletion-requests`，使用重新认证和明确影响确认；
- 用户协议、隐私政策必须是独立可访问文档，不用设置页占位；
- 退出调用 `POST /v1/auth/logout`，成功后清除本地草稿和缓存。

## 15. 网络、任务和错误

统一错误呈现：

| 错误 | 界面行为 |
| --- | --- |
| 401 | 保存本地未同步草稿，跳转登录并安全返回 |
| 403 | 显示无权限，不提供重试写操作 |
| 404 | 显示资源不存在或已删除，返回对应列表 |
| 409 | 显示冲突内容，停止自动覆盖 |
| 422 | 定位字段，保留输入 |
| 429 | 显示恢复时间；禁止无节制自动重试 |
| 5xx/网络 | 保留输入，指数退避或用户重试 |
| task failed | 显示 error_code 对应原因和资源级恢复动作 |

页面不得直接抛出未捕获 `ApiError` 触发 Next.js Runtime Error 覆盖层。

## 16. 埋点

只记录 ID、枚举、阶段、耗时和结果，不记录简历/JD/回答正文。V2 额外需要：

- `protected_route_redirected`
- `workspace_next_action_shown`
- `intake_session_started/resumed`
- `intake_answer_saved/skipped`
- `intake_question_clarified`
- `draft_generation_started/completed`
- `ui_recovery_action_used`
- `hardcoded_fixture_guard_failed`

所有事件在同一页面刷新后不得重复计为完成。
