# AGENTS.md

## 项目结构

- `app/`: 前端应用
- `packages/api/`: 后端接口
- `packages/shared/`: 共享类型和工具

## 常用命令

- `pnpm install`
- `pnpm dev`
- `pnpm lint`
- `pnpm test`
- `pnpm build`

## 代码约定

- UI 优先复用 `app/components/ui`
- 接口错误统一走 `createApiError`
- 共享类型放在 `packages/shared`，不要在页面内重复声明

## 验证要求

- 修改业务逻辑后运行 `pnpm test`
- 修改类型或接口后运行 `pnpm build`
- 修改 UI 后检查桌面端和移动端布局

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
