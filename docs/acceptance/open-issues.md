# 公开上线遗留问题清单

更新时间：2026-07-29
关联候选版本：待本轮实现提交后生成（开发基线 `ab89b8b`）

本清单只记录尚未关闭的问题和外部验收阻断。已修复的登录、事实证据、异步等待、Outbox 分发和 CloudBase 运行时配置问题见 `review-findings.md`。

## OI-00：Pi 多副本运行状态尚缺真实 Redis 证据

- 严重度：P0 验收阻断
- 状态：BLOCKED（工程实现完成，当前环境没有 Redis 服务，真实集成用例被跳过）
- 已完成：Pi RunStore 已拆分为 Memory/Redis 适配器；Redis 状态 TTL 为 24 小时，取消使用 pub/sub 加 250 ms 补偿轮询，owner 使用 5 秒心跳续租 15 秒 lease，失联后查询会原子转为 `failed/owner_instance_lost`。
- 尚缺证据：设置 `TEST_REDIS_URL` 后执行真实双客户端 100 次 create/get/cancel、owner lease 失效和 Redis 重启测试，并在两个 Pi 进程间完成相同验证。
- 完成条件：100 次跨副本循环 `run_not_found=0`；owner 终止后运行在 lease 到期后进入明确失败终态；Redis 重启后服务恢复且不存在永久 running。

## OI-01：取消链路已实现，尚缺真实模型证据

- 严重度：Sev2
- 状态：BLOCKED（工程实现与确定性测试完成，需要 Redis 和真实模型供应商执行 AI-12）
- 已完成：Task 持久化 `active_ai_run_id`、取消请求/确认时间；InternalAiClient 最多 500 ms 检查取消并调用 Pi；Job/Match 在同一事务内先锁定 Task claim，再写业务结果与 succeeded；Pi 的 20 任务跨副本内存测试在 5 秒门限内完成。
- 完成条件：
  1. 任务、Worker 与 Pi 之间持久化关联 `ai_run_id`；
  2. `POST /v1/tasks/{task_id}/cancel` 向 Pi 发送已认证取消请求；
  3. Worker 在取消后禁止写入业务结果；
  4. 注入 20 个运行中 AI 任务，证明每个任务取消后 5 秒内新增 token/tool 事件为 0，且业务结果写入为 0。
- 所需证据：Pi trace 时间线、任务事件、数据库断言和同一提交的自动化测试原始日志。

## OI-02：生产容器与云拓扑未实测

- 严重度：P0 验收阻断
- 状态：BLOCKED（需要云环境权限和 Docker/CloudBase 实际运行环境）
- 影响：无法证明镜像中无密钥、Outbox dispatcher 在生产实际运行、数据库/Redis 不暴露公网，以及 10%/30 分钟金丝雀可回滚。
- 完成条件：构建不可变镜像并部署候选版本；运行镜像 secrets scan、网络隔离、Outbox 到 Celery、回滚和备份恢复演练。
- 所需证据：镜像 digest、扫描原始报告、CloudBase 部署记录、监控截图/导出、恢复日志。

## OI-03：对象存储与真实 Pi 内网链路未实测

- 严重度：P0 验收阻断
- 状态：BLOCKED（需要 COS 和部署后服务）
- 影响：虽已配置生产 COS 与 `resume-ai.internal`，但尚未证明 API 上传、文件 Worker 读取同一对象、Pi 内网调用和导出下载的端到端行为。
- 完成条件：在生产候选版本执行上传→解析→确认→匹配→导出，验证文件 hash、任务终态、Pi trace 和签名下载 URL。
- 所需证据：匿名测试文件、COS 对象清单、Task/Outbox/trace 查询、导出 hash。

## OI-04：浏览器与微信真机验收未完成

- 严重度：P0 验收阻断
- 状态：BLOCKED（需要设备和平台工具）
- 影响：尚未满足 Safari/Edge 核心流程、小程序 iPhone 与两个 Android 品牌、弱网、后台恢复、包体和键盘遮挡等验收项。
- 完成条件：按验收规格完成指定设备、尺寸和网络条件下的自动化/人工测试。
- 所需证据：开发者工具报告、真机录像、性能 trace、页面截图和操作日志。

## OI-05：真实模型评测、成本与用户验证未完成

- 严重度：P0 验收阻断
- 状态：BLOCKED（需要模型供应商账单、标注者和授权测试用户）
- 影响：AI 幻觉、来源覆盖、模型回退、成本阀门、账单对账和 30 名学生验证均不能以本地夹具替代。
- 完成条件：完成至少 100 条 AI 评测样本、双人标注与一致性统计、100 次账单抽样和 30 名学生任务研究。
- 所需证据：脱敏评测集、标注表、Cohen's kappa、provider 账单对账、用户研究记录。

## 发布门禁

在 OI-00 至 OI-05 任一项仍为 BLOCKED 时，版本状态必须保持 **NOT READY FOR PUBLIC RELEASE**。验收清单的每个 P0 项必须在相同 `commit_sha`、候选镜像和构建版本下具备哈希证据后，才能变为 `PASS`。
