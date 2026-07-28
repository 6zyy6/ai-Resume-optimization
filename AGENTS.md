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
