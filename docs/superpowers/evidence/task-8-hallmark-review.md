# Task 8 Hallmark 58 项检查

- 检查对象：`app/web`
- 设计：Workbench / Cobalt / N13 / Ft5
- 预检评分：Philosophy 5、Hierarchy 4、Execution 4、Specificity 5、Restraint 5、Variety 4
- 自动证据：`pnpm --filter @resume/web test`、`pnpm exec playwright test tests/e2e-web --project=chromium`
- 视口：320、375、390、414、768、1024、1440 px

| Gate | 结果 | 判断证据 |
| --- | --- | --- |
| 1 | PASS | 展示字体 Token 为 Space Grotesk，不以 Inter 等默认字体作为展示字体 |
| 2 | PASS | CSS 无渐变文字或紫蓝渐变 |
| 3 | PASS | 无三等分“图标+标题”功能卡阵列 |
| 4 | PASS | 无卡片嵌套卡片结构 |
| 5 | PASS | 无彩色粗侧边条 |
| 6 | PASS | 首屏左对齐 Workbench，不是全居中满屏 Hero |
| 7 | PASS | Cobalt 属 modern-minimal，白色纸面为该类型允许项 |
| 8 | PASS | 使用 Workbench，不是 Hero→三功能→CTA 模板 |
| 9 | PASS | 页面以边框、纸面和深色 Statement 分隔节奏 |
| 10 | PASS | 未使用 `transition: all` |
| 11 | PASS | 未使用统一 hover scale |
| 12 | PASS | 动效无弹跳曲线 |
| 13 | PASS | hover 每个控件只做单一位移或色彩变化 |
| 14 | PASS | 未动画 width/height/top/left/margin/padding |
| 15 | PASS | focus ring 即时出现，不参与 transition |
| 16 | PASS | 保存成功以内联状态表达，无庆祝 toast |
| 17 | PASS | 未实现 tooltip，条件不适用 |
| 18 | PASS | 无自动轮播内容 |
| 19 | PASS | 无 Jane Doe、Acme 等占位名称 |
| 20 | PASS | `globals.css` 顶部包含完整 Hallmark macrostructure stamp |
| 21 | PASS | 未使用 Specimen |
| 22 | PASS | modern-minimal 允许低色度中性色 |
| 23 | PASS | Cobalt 只用于按钮、焦点、短标签和边框，未形成大面积色块 |
| 24 | PASS | 间距使用 4px 倍数及命名空间 Token |
| 25 | PASS | 营销正文保持可读行宽，工作台文本由网格列约束 |
| 26 | PASS | Button/Field 覆盖 focus-visible、active、disabled 及八状态测试 |
| 27 | PASS | 所有 transform transition 有 reduced-motion 降级 |
| 28 | PASS | 无视频 |
| 29 | PASS | 无抽象渐变背景 |
| 30 | PASS | 无混用图标库和 emoji 功能图标 |
| 31 | PASS | 无 Lottie |
| 32 | PASS | 当前项目首次记录该 Workbench 指纹 |
| 33 | PASS | 装饰字符均标记 `aria-hidden`，无未标记 SVG/canvas |
| 34 | PASS | 两流程关键页在七宽度逐页断言 `scrollWidth <= clientWidth` |
| 35 | PASS | 无高亮带、装饰笔触 |
| 36 | PASS | 顶栏、按钮行和状态行显式垂直对齐 |
| 37 | PASS | 字体角色限制为 display/body/mono 三类 |
| 38 | PASS | mono 仅用于 eyebrow、状态和数据标签 |
| 38a | PASS | 标题显式 `font-style: normal`，无标题内斜体 |
| 39 | PASS | Field 固定 1px 边框、outline focus、44px 高度、稳定 helper、disabled 多信号 |
| 40 | PASS | 复用既有高对比语义 Token；正文/纸面、深色页脚/纸面对符合阈值 |
| 41 | PASS | accent 填充同步使用 `--color-accent-ink`；深色页脚同步切换文字色 |
| 42 | PASS | 导航使用 N13 命令入口，不是默认按钮右置 SaaS 导航 |
| 43 | PASS | 页脚使用 Ft5 Statement，不是四列链接页脚 |
| 44 | PASS | Workbench 首页核心入口在 1280×800 内可见，标题行高 1.08 |
| 45 | PASS | 无无语义 Hero 装饰 |
| 46 | PASS | 无虚构用户数、Offer 数、提升比例、评价或品牌 |
| 47 | PASS | 无手绘浏览器/手机/IDE 外框 |
| 48 | PASS | 颜色和字体只在 `:root` 定义，组件均消费命名 Token |
| 49 | PASS | 七宽度断言主导航和 CTA 无文本裁切；交互标签 `nowrap` |
| 50 | PASS | 所有网格使用 `minmax(0, 1fr)`，页面无图片固有宽度溢出 |
| 51 | PASS | 全部标题设置 `overflow-wrap:anywhere` |
| 52 | PASS | 页面头在移动端单列，宽屏才扩展 |
| 53 | PASS | 无 CSS radio tab |
| 54 | PASS | eyebrow 与标题在同一纵向内容列 |
| 55 | PASS | 标题不全大写，行高不低于 1 |
| 56 | PASS | 页面顶栏 top:0；编辑器侧栏使用 top:5rem |
| 57 | PASS | 本次没有 Hallmark study DNA，条件不适用 |

结论：编号 1–57 加 38a，共 58 项，58/58 PASS。交付契约另行完成，不虚构为第 58 个 gate。该结果只覆盖本地确定性 Web UI 和 Chromium 自动化，不替代 Edge、Safari、真实后端、真实模型、Lighthouse 或云环境证据。
