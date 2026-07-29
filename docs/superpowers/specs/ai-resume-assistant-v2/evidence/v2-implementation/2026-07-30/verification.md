# Web V2 本地实现验证报告

## 1. 证据身份

| 字段 | 值 |
| --- | --- |
| 验证日期 | 2026-07-30 03:35 +08:00 |
| 浏览器 | Chrome 150.0.7871.187，Playwright system channel |
| Web | Next.js production build，`http://127.0.0.1:3100` |
| API | 本地真实 FastAPI，SQLite 测试库 |
| 截图对应源码 | `0ba819539856867407adeab76d71d3be8270e57a` |
| 实现主提交 | `be7eae9c6c82c482a033cefb449896c4555ac71b` |
| 截图清单 | [`manifest.json`](./manifest.json) |

所有截图均由隔离的系统 Chrome 直接访问本地 Next.js production build 生成。
登录使用单独创建的合成验收账号；报告和仓库均不记录密码。

## 2. 自动验证

| 命令或检查 | 结果 | 可核对结果 |
| --- | --- | --- |
| `pnpm lint` | `PASS` | 6 个 workspace 与 Python lint 全部退出码 0 |
| `pnpm test` | `PASS` | 632 项通过、1 项 Redis 环境集成测试跳过 |
| `pnpm build` | `PASS` | Shared、AI、Web、小程序和 Python 构建全部退出码 0 |
| `git diff --check` | `PASS` | 应用源码无 whitespace error |
| Web 单元/组件测试 | `PASS` | 10 个文件、53 项通过 |
| API 测试 | `PASS` | 484 项通过，33.70 秒 |
| AI 服务测试 | `PASS` | 71 项通过，1 项真实 Redis 测试跳过 |
| Shared / Tokens / 小程序 | `PASS` | 8 / 2 / 12 项通过 |
| 本地进程监督器 | `PASS` | 2 项通过 |

已知非失败警告：

- `baseline-browser-mapping` 数据提示超过两个月；
- 小程序 Webpack 缓存写入时无法解析 Taro loader 的 cache key，但构建本身成功；
- 真实 Redis 集成用例在未提供 Redis 服务时跳过。

## 3. 真实浏览器证据

### 3.1 响应式页面矩阵

对 `320 / 375 / 390 / 414 / 768 / 1024 / 1440 px` 七档宽度逐一访问：

`/login`、`/home`、`/create`、导入确认、岗位创建、导出恢复、`/settings`、隐私政策。

共 `7 × 8 = 56` 个页面状态，逐页检查结果：

| 判断项 | 通过数 | 失败数 | 判断方法 |
| --- | ---: | ---: | --- |
| 无横向溢出 | 56 | 0 | `scrollWidth <= clientWidth` |
| H1 未被裁切 | 56 | 0 | 标题边界全部处于视口横向范围 |
| 表单控件未被裁切 | 56 | 0 | input、textarea、select、button 边界检查 |
| 可点击文本未折成两行 | 56 | 0 | 链接与按钮渲染行高检查 |
| 无 Next Runtime overlay | 56 | 0 | 页面文本与 overlay DOM 检查 |
| 无 page error / console error | 56 | 0 | 浏览器事件采集 |
| 无非主动取消的请求失败 | 56 | 0 | requestfailed 事件，排除导航预取主动取消 |
| 无 HTTP 5xx | 56 | 0 | 主文档和 API 响应状态检查 |

全部 56 张原图位于 [`responsive/`](./responsive/)；清单记录每张图的路由、视口、
采集模式、像素尺寸和 SHA-256。

### 3.2 1280×800 折叠线

[`landing-viewport-1280x800.png`](./landing-viewport-1280x800.png) 是严格的
`1280×800` viewport 截图，不是 full-page 截图。测量值：

| 项目 | 实测 |
| --- | ---: |
| Hero 顶部留白 | 64 px |
| Hero 底部留白 | 83.2 px |
| 底部 / 顶部 | 1.30 |
| Hero 底边 | 674.0625 px |
| 下一段入口顶边 | 674.0625 px |
| 下一段是否进入首屏 | 是，`674.0625 < 800` |

### 3.3 真实交互

- 邮箱密码登录成功后进入 `/home`，受保护页面使用真实 session cookie；
- `/create` 恢复服务端 intake session；
- 点击“跳过此题”后，问题从“课程、社团、兼职或志愿服务中，有没有一件你投入较久的事？”
  变为“你是否参加过实习、兼职，或为他人解决过一个实际问题？”；
- 页面无硬编码重复提问，跳过动作不会创建肯定事实。

核心截图：

- [`login-1440.png`](./login-1440.png)
- [`home-1440.png`](./home-1440.png)
- [`create-390.png`](./create-390.png)
- [`create-after-skip-1024.png`](./create-after-skip-1024.png)
- [`privacy-1440.png`](./privacy-1440.png)
- [`landing-viewport-1280x800.png`](./landing-viewport-1280x800.png)

## 4. Hallmark 58 项审查

预检评分：`P5 H5 E5 S5 R5 V4`。表内 `NO` 表示问题不存在，即通过；
`N/A` 表示当前页面没有该类元素。检查代理最终确认 Gate 8 和 56/56 响应式矩阵通过。

| Gate | 结果 | 代码或浏览器依据 |
| ---: | --- | --- |
| 1 | NO | 展示字体为 Space Grotesk |
| 2 | NO | 无渐变和渐变文字 |
| 3 | NO | 无三等分图标卡片模板 |
| 4 | NO | 无卡片套卡片 |
| 5 | NO | 无粗彩色侧边条 |
| 6 | NO | Hero 左对齐且非 100vh 居中堆叠 |
| 7 | NO | 纸色与墨色均来自语义 Token |
| 8 | NO | 当前为 Guided Ledger；与上一 Workbench 结构记录不同 |
| 9 | NO | 使用规则、纸色切换和深色 statement footer |
| 10 | NO | 无 `transition-all` |
| 11 | NO | 无统一 hover scale |
| 12 | NO | 无弹跳/overshoot UI easing |
| 13 | NO | 单元素没有叠加多个 hover 动效 |
| 14 | NO | 不动画布局尺寸和位置 |
| 15 | NO | focus ring 即时出现 |
| 16 | NO | 无可见结果后的庆祝 toast |
| 17 | N/A | 无 tooltip |
| 18 | N/A | 无自动轮播内容 |
| 19 | NO | 无 Jane Doe、Acme 等占位陈词 |
| 20 | NO | CSS 顶部有 Hallmark macrostructure stamp |
| 21 | NO | 未使用 Specimen 默认结构 |
| 22 | NO | 中性色来自带锚色色相的设计 Token |
| 23 | NO | accent 面积低于单视口约 5% |
| 24 | NO | 间距来自 4 px 语义比例 |
| 25 | NO | 正文 measure 保持 45–75ch |
| 26 | NO | 交互组件覆盖 focus-visible、active、disabled 等状态 |
| 27 | NO | 动效有 reduced-motion 兜底 |
| 28 | N/A | 无 hero 视频 |
| 29 | N/A | 无抽象背景 |
| 30 | NO | 不混用图标库，不使用 emoji 功能图标 |
| 31 | N/A | 无插画/Lottie |
| 32 | NO | Guided Ledger 使用 action-first 与 evidence-ledger variation |
| 33 | NO | 视觉符号明确 `aria-hidden` |
| 34 | NO | 56/56 无横向滚动；html/body 使用 `overflow-x: clip` |
| 35 | N/A | 无高亮带或装饰性文字描边 |
| 36 | NO | 导航和交互行显式垂直居中 |
| 37 | NO | 仅 display、body、mono 三个字体族 |
| 38 | NO | mono 只用于 status 与 resource-id 两个语义槽 |
| 38a | NO | 标题和展示文字无 italic |
| 39 | NO | 输入状态不改变边框宽度，焦点使用 outline |
| 40 | NO | 正文、muted、accent、状态色和 focus 对比均达阈值 |
| 41 | NO | accent-ink 和深色区文字均显式切换 |
| 42 | NO | 应用内为紧凑 N5 导航，不是默认营销导航 |
| 43 | NO | 使用 Ft5 statement footer |
| 44 | NO | 1280×800 实测 1.30 倍底部留白，入口进入首屏 |
| 45 | NO | 无无语义 Hero 装饰 |
| 46 | NO | 无虚构用户数、提升率或性能指标 |
| 47 | NO | 无重绘浏览器/手机/终端外壳 |
| 48 | NO | 颜色和字体均引用命名 Token |
| 49 | NO | 56/56 可点击文本不折行 |
| 50 | N/A | 无图片承载 grid track |
| 51 | NO | display 标题有 `overflow-wrap` 和 `min-width: 0` |
| 52 | N/A | 无主题专属双列 section head |
| 53 | N/A | 无 CSS-only radio tabs |
| 54 | NO | eyebrow 与标题为同列垂直关系 |
| 55 | N/A | 无全大写且行高小于 1 的 display heading |
| 56 | NO | 无两个 `top: 0` 的 sticky 层叠 |
| 57 | N/A | 本轮没有 study DNA 需要继承 |

Hallmark 结论：`58/58 PASS`，其中 11 项因对应元素不存在而 `N/A`。

## 5. 验收结论与边界

| 范围 | 状态 | 原因 |
| --- | --- | --- |
| 本地源码、单元/组件/API 测试、构建 | `PASS` | 命令与计数见第 2 节 |
| 本地 7 视口 × 8 页面响应式冒烟 | `PASS` | 56/56，原图与哈希见 manifest |
| 本地创建页题目切换 | `PASS` | 真实截图和前后问题文本均已记录 |
| 完整 V2-P0 / `ENGINEERING READY` | `BLOCKED` | 尚缺规格中的 42 个主状态、16 个异常状态、axe、API/DB/trace 同链证据 |
| 真实 Redis、云数据库、COS、邮件、DeepSeek/Pi | `BLOCKED` | 本轮没有真实外部凭据和账单证据 |
| Safari、Edge、真实手机和 staging | `BLOCKED` | 本轮环境未提供 |
| 30 名学生可用性验证 | `BLOCKED` | 未开展每条路径至少 15 人的研究 |

因此，本报告证明 V2 的本地代码闭环、自动测试和响应式页面冒烟通过；不把缺失的云环境、
真模型、真机或用户研究证据写成 `PASS`。
