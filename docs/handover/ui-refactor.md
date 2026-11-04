# 界面重构交接（第一版整治已完成）

> 写给下一个会话的 agent。这份文档自包含，不依赖任何聊天记录。
> 产生它的那次工作：把 WebUI 的几何/字号/层级收敛成一套设计令牌，并修掉模块间距与卡片压盖。
>
> **配套文档**：[`ui-inventory.md`](./ui-inventory.md) —— 界面上所有可交互内容、静态内容，
> 以及它们与接口/状态之间的逻辑关系（按运行时 DOM 逐块核对）。重构前先读那份建立完整地图。

---

## 0. 一句话现状

主页面样式已经从「散落的裸像素」迁移到**四组设计令牌**（圆角 / 间距 / 字号 / 层级），
源码层零裸值、模块间距只剩 8 / 16 / 24 三档，功能零回归。
**底座已经就位，可以直接开始重做界面**——但下面第 7 节的坑要先读。

---

## 1. 状态快照

| 项 | 值 |
|---|---|
| 仓库 | `ExpertKT/notice-hub`，分支 `main` |
| 基线 commit | `cf6bb98`（v1.1.0-beta.16）。本次改动已提交在本地，提交信息以 `style(web): 界面几何收敛成设计令牌` 开头，**未推送**（`git log -1` 看当前 hash） |
| 全局 Python | 3.15.2（python.org 框架版）；项目 venv 在 `.venv/` |
| 依赖 | `requirements.txt` + `requirements-dev` 里已装的都在；**额外装了 `websocket-client`**（截图脚本用，未写进 requirements） |
| 演示服务 | `.ui-verify/dev.sh start` → <http://127.0.0.1:8766/> |
| 门禁 | 9 个模块 **447 个用例，约 150 秒**，当前连跑两轮 EXIT=0 |
| UI 相关文件改动 | `noticehub/web_static/page.html`、`tests/test_webapp.py`、`docs/wiki/changelog.md` |

---

## 2. 界面代码地图

| 文件 | 体量 | 是什么 |
|---|---|---|
| `noticehub/web_static/page.html` | 3164 行 / 281 KB | **主页面**：两段 `<style>` + 全部 DOM + 一大段内联脚本 |
| `noticehub/web_static/js/settings.js` | 128 KB | 设置页的全部逻辑与**由 JS 生成的设置页 DOM** |
| `noticehub/web_static/js/guide.js` | 48 KB | 引导、二维码、NapCat 安装、托管状态胶囊、新手 journey |
| `noticehub/web_static/js/pair.js` | 24 KB | 手机配对、公网入口 |
| `noticehub/web_static/js/announce.js` | 20 KB | 公告铃铛与面板 |
| `noticehub/web_static/js/lab-rules.js` | 18 KB | 实验室里的自定义规则编辑器 |
| `noticehub/floatball_page.py` | 20 KB | **第二个 HTML 面**：桌面浮球页（Windows 专用，macOS 不显示） |
| `noticehub/floatball_window.py` | 36 KB | 浮球窗口（tray/pystray 那套） |

`page.html` 里的两段样式块，性质完全不同：

- **14–241 行：遗留基础样式。** 选择器是 `.hero` / `.workflow` / `header` 这类早期命名，
  页面里已经没有对应 DOM，绝大多数被第二段覆盖（`#calendar{gap:5px}` 这种死规则仍在）。
  **重构时这一整段可以直接删掉或整段替换**，但要先 `grep` 确认没有活规则混在里面。
- **242–699 行：活的样式表。** 里面有：
  - `247` 起：**设计令牌层**（`--r-*` / `--space-*` / `--fs-*` / `--z-*`，带中文注释说明取值依据）
  - `654` 起：**设计决策层**（这次刻意做的决定：边界 ≥12px、三种关系同刻度、卡片同构、装饰层内收）
  - `698` 附近：`<!--CHANNEL_LOGO-->` 标记

> ⚠️ `<!--CHANNEL_BADGE-->` 与 `<!--CHANNEL_LOGO-->` 由服务端替换成版本徽标与品牌色变量
> （`noticehub/webapp.py:2820`）。**这两个标记不能删、不能改名**，否则 beta/正式版的徽标与配色一起失效。

---

## 3. 设计令牌契约

改界面请只用这四组刻度，不要再写裸像素。全部在 `page.html` 的 `:root`（约 247 行）。

| 组 | 取值 | 语义 |
|---|---|---|
| 圆角 | `--r-1:4px` `--r-2:6px` `--r-3:8px` `--r-4:12px` `--r-5:16px` `--r-full:999px` `--r-circle:50%` | 内嵌小块 → 控件 → 卡片 → 大面板 → 弹层 → 胶囊 → 正圆 |
| 间距 | `--space-1:4` `-2:8` `-3:12` `-4:16` `-5:20` `-6:24` `-8:32` `-10:40`（px） | 4 的倍数，全站唯一节奏来源 |
| 字号 | `--fs-1:9` `-2:11` `-3:13` `-4:15` `-5:18` `-6:21` `-7:25` `-8:29` `-9:34`（px） | 相邻比值 1.154–1.222，任意两级一眼可分 |
| 层级 | `--z-behind:-1` `--z-base:0` `--z-raise:1` `--z-raise-2:2` `--z-local:5` `--z-sticky:900` `--z-modal:1100` `--z-panel:1180` `--z-overlay:1300` | 装饰 → 内容 → 抬升 → 局部 → 吸顶 → 弹窗 → 侧栏 → 遮罩 |

当前**生效值**（浏览器实测，不是源码里的写法）：圆角 8 项 = 7 个 token + `0`；
字号桌面 11/13/15/18/21/25/34、手机把主标题降到 29；层级只用 1/2/900。

---

## 4. 间距节奏（核心规则）

**断落之间的距离必须大于断落内部的距离。** 全站只有三档：

| 档 | 用途 |
|---|---|
| **8**（`--space-2`） | 卡片之间、卡片内相邻元素（标签行→标题、标题→详情） |
| **16**（`--space-4`） | 卡片内边距（`.task` / `.surface`）、概览→顶部横幅、横幅→页签、详情→操作按钮 |
| **24**（`--space-6`） | 顶栏→概览、页签→内容首块、分组之间、左右两栏的模块之间、右栏面板之间 |

桌面（≥1024）实测的模块间距：

```
吸顶栏 ─24─ 概览卡 ─16─ 顶部横幅 ─16─ 页签 ─24─ 主内容首块 ─24─ 下一分组
```

**曾经踩过的坑（已修，别改回去）**：右栏面板的 `.side-rail{gap}` 与 `.rail-panel{margin-top}`
曾叠加成 32px；`.journey{margin-top}` 曾叠在 `main` 的 `padding-top` 上。
**容器已经用 grid/flex gap 给间距时，子元素不要再加同方向 margin。**

---

## 5. 测试契约（改 UI 前必须知道）

`tests/test_webapp.py` 有约 **2200 条断言**，其中二十多条是**逐字比对 CSS 文本**的：

```python
self.assertRegex(css, r"\.summary h1\{[^}]*font-size:var\(--fs-9\)")
self.assertIn(".calendar-event{display:block;margin-top:var(--space-1);...}", css)
self.assertIn("html .brand{gap:var(--space-1)}", narrow)
```

**规矩（`AGENTS.md`）：断言是契约，不许放宽、不许删、不许 skip 来让测试变绿。**
改了样式值就**同一次改动里同步这些期望值**——断言形式（`assertIn` / `assertRegex` 的严格程度）
保持不变，只换期望值，这不算放水；删断言、放宽成 `assertIn("border-radius")` 才算。

改 CSS 的推荐循环：

```bash
./.ui-verify/dev.sh literals   # 1 秒：先看哪条字面断言被改没了（不用等 150 秒）
#   → 按输出的选择器逐条同步 tests/test_webapp.py
./.ui-verify/dev.sh test       # 约 150 秒：跑一遍
./.ui-verify/dev.sh gate       # 正式验收：连跑两轮，各自取退出码
```

跑之前按 `AGENTS.md` 确认机器上没有别的 python 在跑；**只用指定的 9 个模块，不要用 `discover`**。

被测试钉住、改动要格外小心的行为（与 UI 强相关）：

| 断言对象 | 位置 | 含义 |
|---|---|---|
| `.summary` 呼吸只动装饰层 | `test_summary_breathe_keeps_text_still_and_moves_only_the_aura` | 卡片本体不许有 transform/scale，否则文字被重采样发虚 |
| 轮播固定高度 | `test_...upcoming...`（`height:200px` / `.is-empty{height:auto}`） | 切换时不顶动下方元素 |
| 页签 `role=tab` + `aria-selected` | 多处 | 键盘可达性 |
| 动效关断 | `@media(prefers-reduced-motion)` + `html[data-motion=paused]` | 两套规则都要能压回原位 |
| 深色主题对比度 | `test_color_tokens_separate_surfaces_and_typography_scales` 等 | 文字对底色 ≥4.5:1、分隔线 ≥3:1 |

---

## 6. 验证与开发循环

```bash
./.ui-verify/dev.sh seed       # 清空并灌演示数据（30 消息 / 3 摘要 / 12 待办 / 收件箱判定 / 投递记录）
./.ui-verify/dev.sh start      # http://127.0.0.1:8766/
./.ui-verify/dev.sh audit      # 源码层：圆角取值数、非 4 倍数间距、裸 z-index、字号阶梯比值
./.ui-verify/dev.sh shots      # 明/暗 × 1440/900/390 六张截图 + 横向溢出/模块压盖/生效刻度
./.ui-verify/dev.sh stop
```

**两个必须先知道的坑：**

1. **改完 `page.html` 必须重启服务。** 服务启动时把页面读进模块常量
   （`noticehub/webapp.py:1125` 的 `PAGE_HTML`），不重启永远看到旧页面。
2. **打开页面带 `?nowiz=1&v=任意值`**：`nowiz=1` 跳过首次使用向导弹层，
   `v=` 绕过 Service Worker 缓存。

`shots.py` 的输出（每档一条 JSON）是最好用的自查：
`overflowX`（横向溢出）、`overlapCount`（**非嵌套**模块互压）、`spacingBad`（非 4 倍数间距）、
`radiusSet` / `fontSet` / `zSet`（生效值集合）。判断标准：全 false / 0 / 集合落在令牌内。

---

## 7. 已知问题与坑（重构前请读完）

1. **首装向导弹层**：`#platform-setup-wizard` 首次访问自动弹出（`localStorage.nh_setup_wizard_seen`），
   用 `?nowiz=1` 或点「稍后配置」。
2. **「已过期」是收起的 `<details>`**：里面的卡片有布局盒但**不绘制**。
   写任何几何/压盖检测都要排除「closed `<details>` 的后代」，否则会得到假的重叠报警。
3. **内边距白名单**：`.tag`/`.chip`/`.badge`/`.verdict`/`.count-pill`/`.progress-label`、
   图标按钮（`.announce-bell`/`.month-controls .btn`/`.upcoming-controls button`/`.pinned-resize`）、
   日历与健康检查的密集网格单元、`fieldset legend`、`.candidate-mark`。
   这些元素的内边距**故意小于 12px**，留白由父级行或网格给。`ui_audit.py` 与 `shots.py` 已按此判定。
4. **`.candidate-mark` 有一条紫色玻璃质感规则**（`rgba(139,92,246,…)`），与本项目「单一强调色」的取向冲突，
   是历史变体残留，**未处理**——重构时可以直接删掉那条重复规则。
5. **轮播固定 200px / 窄屏 220px 高**，内容少时会留一大块空白。高度被测试钉住，改之前先看第 5 节。
6. **深色主题靠约 40 条 `html[data-theme=dark] .xxx{...}` 覆盖**，而不是变量切换。
   重构时可以顺手把这层收敛到变量里，覆盖条数能砍掉一大半。
7. **设置页的 DOM 由 `settings.js` 生成**，改设置页的外观不能只动 CSS。
8. **浮球是第二个独立 UI 面**（`floatball_page.py`），Windows 才有；本次没碰。
9. 浏览器默认边距已归一化：`p,ol,ul{margin-block:var(--space-4)}`、`h1..h4` 同理。
   新加的元素如果指望浏览器默认值，看到的是 16px，不是原来的 1em。
10. **`.section-title` 在遗留样式块里还有一条 `display:flex`**，活的样式表只覆盖了部分属性。
    删除遗留块时注意这类「属性由旧块提供」的情况。

---

## 8. 建议的重构路线

1. **先定范围**：只重做主页面？还是连设置页（JS 生成 DOM）与浮球页一起？
   范围决定第 5 节要盘点多少断言。
2. **盘点被钉住的 DOM 与文案**：
   `grep -n "PAGE_SOURCE\|PAGE_HTML" tests/test_webapp.py` —— 凡是被断言的 `id` / `class` / 文案，
   改结构时要么保留，要么同一次改动里同步断言。
3. **建立改前基线**：`./.ui-verify/dev.sh seed && start && shots`，把六张图存下来当对照。
4. **用令牌层当接缝**：新样式表可以直接复制 `:root` 那四组令牌，旧样式与旧断言先活着，
   按「一块一块替换」的节奏推进；每块替换完跑 `literals` + `gate`。
5. **优先替换遗留样式块（14–241 行）**：它是死代码，删掉能立刻减少排查噪声。
6. 想把「深色主题 40 条覆盖」一并收敛，建议单独一次改动做，别和布局重构混在一起。

---

## 9. 新会话开场检查清单

```bash
cd notice-hub
git log --oneline -3            # 当前起点
git status --porcelain          # 应为干净（本次改动已提交在本地，未推送）
ls .ui-verify/                  # 环境是否还在（未提交，随工作区保留）
./.ui-verify/dev.sh audit       # 令牌层是否完好：圆角 9 项、间距锚点 0、层级锚点 0
./.ui-verify/dev.sh seed && ./.ui-verify/dev.sh start
./.ui-verify/dev.sh gate        # 基线是否绿：447 用例 ×2
```

任何一项不符预期，先修环境再动样式。

- `noticehub/web_static/page.html`
  - 新增 `:root` 设计令牌层（四组刻度 + 取值依据注释）
  - 全量令牌化：圆角 20 种裸值 → 7 档；padding/margin 归到 4 倍数；字号 17 级 → 9 级阶梯；z-index 21 处 → 9 级刻度
  - 新增「设计决策层」：文字/边框边界 ≥12px、三种关系同刻度、卡片同构、摘要卡装饰层内收
  - 模块节奏统一为 8 / 16 / 24，修掉两处间距双算
  - 浏览器默认段落/列表/标题边距归到 16px
- `tests/test_webapp.py`：25 条字面 CSS 断言的期望值从裸像素换成令牌（断言条数与形式不变）
- `docs/wiki/changelog.md`：`## 未发布` 小节记录了用户视角的变化（`AGENTS.md` 要求）

---

## 10. 本次改动摘要（便于 review 或回退）

- `noticehub/web_static/page.html`
  - 新增 `:root` 设计令牌层（四组刻度 + 取值依据注释）
  - 全量令牌化：圆角 20 种裸值 → 7 档；padding/margin 归到 4 倍数；字号 17 级 → 9 级阶梯；z-index 21 处 → 9 级刻度
  - 新增「设计决策层」：文字/边框边界 ≥12px、三种关系同刻度、卡片同构、摘要卡装饰层内收
  - 模块节奏统一为 8 / 16 / 24，修掉两处间距双算
  - 浏览器默认段落/列表/标题边距归到 16px
- `tests/test_webapp.py`：25 条字面 CSS 断言的期望值从裸像素换成令牌（断言条数与形式不变）
- `docs/wiki/changelog.md`：`## 未发布` 小节记录了用户视角的变化（`AGENTS.md` 要求）

---

## 11. v2：整层重写（纸感编辑风，按 `b-integrated` 原型）

第 1–10 节讲的是「第一版整治」——把散落的裸值收进令牌、修间距与压盖。**视觉层在那之后整体重写过一次**，
所以下面这些和第 1–10 节的描述不一致，以本节为准：

| 项 | 第一版（第 1–10 节） | v2 现状 |
|---|---|---|
| 样式结构 | 两段 `<style>`：14–241 遗留死样式 + 242–699 活样式表 | **单段** `<style>`，19 个小节：令牌 → 基础 → 浮岛 → 概览 → bento → 待办 → 按钮 → 表单 → 月历 → 收件箱 → 推送 → 设置 → 浮层 → 接入 → 其它 → 动效 → 暗色 → 响应式 |
| 背景 | `body:before/after` 两层漂移柔光（`bg-silk-a/b`） | `body::after` 固定层纸纹（内联 SVG `feTurbulence`，opacity .045），`body::before{content:none}`；`--z-grain:60` |
| 深度 | 三层 Z 轴（`--depth-mid/top` 0/8/16 + `perspective:1100px`） | 全平：四个 depth 令牌恒为 `0px`，无 `perspective`、无 `translateZ` |
 | 顶栏 | `sticky` 吸顶栏，页签在栏外单独一行 | `.masthead-row` 是**固定浮岛**（`position:fixed;top:var(--space-4)` + `translateX(-50%)` + 84% 半透明 + `backdrop-filter`），岛内只有 Logo 和页签；公告铃铛挪进隐藏的 `#announce-bell-holder`，托管工具挪进设置页 |
| 右栏 | `.side-rail` 常驻右列（含 QQ 接入、订阅群、关于等 7 块） | `.side-rail` **已删**。置顶/小结/快捷/自检搬进主列 `.bento`（2×2，`body[data-tab]:not([data-tab=tasks])` 时隐藏）；QQ 接入与订阅群搬进设置页 |
| 设置页 | 一列长列表（settings.js 依次 append） | `#tab-settings` 两列 grid：`.settings-subnav`（14 个 `data-goto` 按钮，点按滚动到对应面板）+ `.settings-body`（settings.js 的 append 目标，注意它会 `textContent=''` 清空） |
 | 触控尺寸 | 全部 ≥44px | 页签 30px / 按钮 28px（小按钮 24px）/ 输入框 28px / 分组行 34px / 日历格 44px；44px 只在 `pointer:coarse` 生效 |
 | 圆角 | `--r-5:16px` | `--r-3:10px`（卡片/按钮）、`--r-5:14px`（浮岛与概览卡）；胶囊只留给状态标签、徽标、进度条、开关 |
| 主题 | 两态（light/dark），快照 `--page:#e8ebee` | 三态 `auto/light/dark`（`auto` 跟 `prefers-color-scheme`），纸墨色板 `--page:#EDE7D6` / 暗色 `#23231B`；设置页可切五档强调色（写内联 `--teal`/`--teal-soft`） |

改样式时的老坑仍然成立：`<!--CHANNEL_BADGE-->`（设置页 `#channel-badge-slot`）
与 `<!--CHANNEL_LOGO-->`（`<style>` 末尾，服务端在这里注入 `html{--brand-ink/--brand-accent}`）**都不能删**；
改完 `page.html` 必须重启服务；浏览器开 `?nowiz=1&v=x`。

新增的两个自查脚本（都在 `.ui-verify/`，不入库）：

- `qa_probe.py`：几何自查——浮岛与概览的间距、页签是否在岛内、内容是否钻到固定栏底下、标题是否宋体、圆角/字号是否都在令牌内、文字是否被截断、有没有穿模。
- `func_probe.py`：功能连通性 15 项——首屏渲染、勾选落库、四个页签、设置面板生成、主题三态与强调色持久化、新建待办表单、收件箱筛选与转待办、自检 8 行、控制台零报错。

## 12. v3：按原型逐项对齐（Logo / 圆点月历 / 浮岛精简）

在 v2 之上又做了一轮「对表」：拿 `b-integrated` 原型逐项量过，改形状与位置，不动功能与接口。

- **Logo 换原型配色**：墨橄榄 `#3A3A28` 大圆 + 右上纸色 `#F4EFE3` 小圆 + 鼠尾草 `#7B8B5E` 内圈 + 纸色对勾，全部写成 `var(--brand-ink,#3A3A28)` / `var(--brand-accent,#7B8B5E)`，服务端注入的品牌色照常生效。
- **浮岛精简**：`#announce-bell-holder[hidden]` 承接公告铃铛（`announce.js` 全程 null-guard，公告面板与接口未动）；`#hosting-quick-card`（托管状态 + 开始/停止托管）与「关于与支持」(`#rail-about`) 挂到 `#tab-settings` 下（在 `.settings-body` 兄弟位置，靠 `grid-column:2` 归位——settings.js 会 `textContent=''` 清空 `.settings-body`，静态内容不能放里面）。
- **月历圆点化**：`.calendar-evs` / `.calendar-evs i`（4px 圆点，`bottom:5px;gap:3px`），JS 按 `items.slice(0,MAX_CALENDAR_EVENTS)` 生成；`.calendar-event` / `.calendar-more` 的样式与 JS 全删；说明行 `.calendar-note` 同步改写；当天详情 `#calendar-detail` 改虚线顶边无卡形态，关闭按钮纯文字「收起」。
- **尺寸收紧**：页签 `min-height:30px`、`.btn` 28px、`.btn-small` 24px、输入 28px、`.task` 悬停 `border-color:var(--sand);transform:translateX(2px)`（无阴影缩放）；月历格 44px、圆角 6px 不变。
- **demo 修复**：`.ui-verify/build_demo.py` 给单文件演示注入 `window.__NH_DEMO__=true`，并把向导跳过条件改成认这个标记——`file://` 直开时向导浮层不再盖住页面，四个页签正常切换。
- **测试同步**：`tests/test_webapp.py` 的月历/表单/浮岛/待办卡五处断言改成锁新形态（圆点生成逻辑、行式 `.field`、`top:var(--space-4)`、`translateX(2px)`），断言强度不降。

## 13. v4：全量对齐 `b-integrated` 原型（浮岛居中 / 卡片 chips+acts / 概览归待办）

v3 只换了令牌和局部 DOM，形状其实没跟上。这一轮把原型的规格表逐条搬下来。**原型是唯一基准**，
再改样式之前先读 `.ui-verify/prototypes/b-integrated.html`（样式 7–565、body 567–1105）。

### 13.1 权威规格表（与代码同步维护）

| 项 | 值 |
|---|---|
| 圆角 | 控件 `--r-2:6`、卡片/按钮 `--r-3:10`、浮岛/概览 `--r-5:14`、胶囊 `--r-full:999`、正圆 50%；复选框 4px（`--r-1`）、月历点/柱状图 2px（原型特例）。**没有 8px 和 12px** |
| 字号 | `--fs-9:34 large`、`--fs-8:28 title1`、`--fs-7:22 title2`、`--fs-6:20 title3/面板标题`、`--fs-5:16`、`--fs-4:15 body`、`--fs-3:13 subhead`、`--fs-2:11`、`--fs-1:9`；10px 是「.src/.subnav .grp/底栏页签」的固定特例 |
| 版心 | `--measure:1440px` + `--gutter`（16/24/32/40 四档，`min-width:760/1100/1440` 上台阶，都落在 4px 网格） |
| 浮岛 | `position:fixed;top:var(--space-4);left:50%;transform:translateX(-50%)`；**不给 width**，`max-width:calc(100vw - var(--space-8))`；`gap:4px;padding:4px 8px;r-14`；82% 半透明 + `blur(20px)`；`.island-sep` 1px 竖线 |
| 页签 | `min-height:30px;padding:0 16;r-2;font-size:13;font-weight:400`；组内 `gap:2px`；选中 `background:var(--teal);color:var(--on-accent)`；active `scale(.97)` |
| 待办卡 | `check + body(.t → .chips → .context → details.raw → correction) + acts` 一行；`.check` 20px 圆 1.5px 边；`.t` 600 15px；`.chip` 胶囊 pad1/8 11px（`.hot` bad / `.soon` teal / `.ok` ok）；`.acts` `flex:none` 右置 |
| 按钮 | `.btn` 28px r-3（=tinted 描边）；`.filled/.primary` 实心；`.plain/.ghost` 无框；`.destructive` 红字；`.sm` 24px r-2 11px |
| 概览 | 只在 `body[data-tab=tasks]` 显示；`h1` 衬线 28；右侧 `.pct` 衬线 28 + `.cap` 11px；`.ov-rule` 1px；`.mode` 胶囊 24px；`.mini-btn` 28px r-2 强调线 |
| 分组头 | `h2` 衬线 20px `.08em` + 件数衬线 11px + `.section-rule` 拉线 |
| 月历 | 格 44px r-2 `padding-top:4px`、**grid gap 2px**、无边框；today/hover 用 wash；`.is-open` 用 `inset 0 0 0 1.5px` 内描边；月份按钮 `.mini-btn` + 「今天」`.btn.tinted.sm` |
| 收件箱 | `.inbox-item` = `flex` + `.inbox-main(.inbox-title 600 15px + .inbox-meta 11px) + .inbox-acts` 右置 |
| 推送 | `.notice-feed{max-width:70ch;margin:0 auto}`，`.notice` 顶边 1px 实线，time 衬线 13px |
| 设置 | `#tab-settings` `212px + 1fr gap 32`；subnav sticky `--island-pad - 16`；选中 `paper-sink` 底；`.section-title` 衬线 20px；`.field` 左名称(500)右控件，`.desc` 11px |
| 抬头 | `.masthead{text-align:center;padding:40 0 32}`，eyebrow 11px + 5px 点，h1 衬线 34px |
| 窄屏 | ≤760 浮岛落底成标签栏：`bottom:0`、无圆角、`--island-pad:24`、`body padding-bottom:calc(72px+safe)`；页签纵排 48px；选中只变字色 |

### 13.2 与原型的三处有意偏差（都是修正原型的缺陷，不是偷懒）

1. **版心 gutter 用四档台阶**，原型是 `clamp(16px,3.2vw,56px)`：流体 clamp 会算出 46.08px 这种小数，
   和「间距必须落在 4px 网格」的自查规则打架。台阶等价且永远在网格上。
2. **窄屏顶栏留白从 88px 收到 24px**，原型没改 `body padding-top`，手机上会留一条 88px 的死空间；
   同时补了 `padding-bottom:72px+safe`，原型没算底栏高度，内容会被标签栏压住。
3. **收件箱把判定理由/正文收进折叠块**，原型没有这个信息密度问题；折叠块展开才看，卡片保持一条高度。

### 13.3 工具口径同步（`.ui-verify/`）

- `shots.py`：六档截图照旧；新增 `islandAtBottom` 探测（按几何判断：栏心在下半屏）——底栏模式下
  固定栏与正文的「重叠」是设计本意，改查 `body padding-bottom ≥ 栏高`（`out.bar.clears`）。
- `qa_probe.py`：同样认识底栏；`allowed` 圆角集合从 10 项收成 7 项（0/4/6/10/14/999/50%）。
- `func_probe.py`：从 15 项加到 18 项——浮岛必须是不撑满版心的居中胶囊、今日概览只在待办页签出现、
  待办卡必须是 chips+acts 且旧的 `.card-top`/`.urgent-controls` 不存在。

### 13.4 测试同步（`tests/test_webapp.py`，383 例全绿）

只改期望值，不减用例、不 skip：版心断言（`padding-inline:var(--gutter)` + 四档）、圆角四档、
字阶逐档锁值、`.section-title` → `--fs-6`、概览 DOM 顺序（浮岛 → 横幅 → 概览 → bento）、
chips 全展开（`assertNotIn("cardTags.slice(0,2)")` + `assertNotIn("tag-more")`）、月历 gap 2px 与
「今天」按钮、窄屏底栏逐条断言（`top:auto;bottom:0`、48px 触摸目标、选中只变字色）。

### 13.5 已踩过的坑：脚本块嵌套会让 JS 变成页面正文

`page.html` 里曾经出现过 `<script>` 套 `<script>`（一次版面重排时多写了一行 `<script>`）。
后果不是「报个错就算了」，而是三件事同时发生：

1. 浏览器把内层 `<script>` 当成脚本数据，一直读到**第一个** `</script>` 才收尾 → 外层块整体语法错误，**一次都没执行**；
2. 那个 `</script>` 之后到下一个 `</script>` 之前的代码（当时是 `scrollTargetIntoView()` 整个函数）**变成普通正文画在页面上**；
3. 剩下一个无主的 `</script>` 被解析器丢掉。

连带失效：`guide.js` / `settings.js` 三处调用 `scrollTargetIntoView` 全部 `ReferenceError`；
同一个坏块里的「设置页左子导航」IIFE 从未执行 → 14 个跳转按钮没绑事件、也没有 `is-active` 高亮。

**守门测试**：`tests/test_webapp.py::PageStructureTest` 三条——
`test_script_blocks_are_never_nested`（`<script>` 数量与 `</script>` 相等，且内联脚本体内不许再出现 `<script`）、
`test_no_javascript_is_rendered_as_body_text`（`html.parser` 走一遍，`script`/`style` 之外的可见文本里
不许有 `function ` / `=>` / `document.getElementById` 等）、
`test_scroll_helper_is_defined_once_inside_a_script_block`。
`func_probe.py` 里还有一条实机版本：`页面正文里没有漏出来的 JS 源码`。

另外：`settings.js` 里有一句注释原本写着 `<script>` 字面量——放在页内合法（`<script>` 不结束脚本数据），
但单文件 demo 把脚本内联后会骗到检测器，已改成「脚本块」措辞。写注释时**别在脚本里写 `</script>`**，
那个字符串真的会提前结束脚本。

### 13.6 设置页两列布局的定位契约

`#tab-settings` 是 `212px + minmax(0,1fr)` 两列。**必须显式定位**，靠 auto-placement 会错位
（实测过：抬头挤进 212px 窄栏、12 个面板被压成 212px）：

```css
#tab-settings>.masthead{grid-column:1/-1;grid-row:1}                  /* 抬头通栏 */
#tab-settings>.settings-subnav{grid-column:1;grid-row:2/span 10;align-self:start}
#tab-settings>:not(.masthead):not(.settings-subnav){grid-column:2;margin:0}
.settings-subnav{position:sticky;top:calc(var(--island-pad) + var(--space-4))}
```

**踩过的坑：`grid-row:2/-1` 不能用。** `grid-template-rows` 没定义时，`-1` 在隐式网格里
解析成**第 1 条线**，子导航于是落到第 1 行、和通栏抬头**完全重叠**（实测两者 `y` 都是 525），
而且它只占一行高度（645px），`sticky` 滚一点就跟着走。

**也不能用 `span 999`**：会给 `grid-template-rows` 生成几百条 `0px` 空行，列表长到几千项。
`span 10` 够覆盖现有内容行（抬头 1 + 内容 7 行）且留余量。

窄屏（≤1100px）单列时三者都要显式拉回第 1 列 + `grid-row:auto`，
否则残留的 `grid-column:2` 会撑出一个隐性的第二列。

### 13.7 右列面板顺序 = 左侧导航顺序（order 编排）

`settings.js` 按自己的逻辑往 `.settings-body` 里 append，静态面板（QQ 接入/订阅群/关于/自检）又固定在
page.html 末尾——**右侧的实际顺序曾与左侧导航完全对不上**（QQ 接入和订阅群被挤到页面最底下）。

解法是把 `.settings-body` 设为 `display:contents`：它只是挂载点，里面的面板升格为 `#tab-settings`
网格的直接条目，然后给每个锚点一组 `order` 值（10/20/30…，跟导航顺序一一对应）。JS 怎么 append、
异步生成的面板（运行信息/纠错提示）何时出现都不影响最终顺序。

配套三件事：

- `#tab-settings` 的行间隙必须保持 `gap:0 var(--space-8)`（只有列间距），行距由面板自身的
  `margin-bottom:var(--space-3)` 提供——否则子导航 `span` 出的空行会有 phantom 高度。
- `#tab-settings>:not(...)`（`#id` 前缀特异性高于裸属性选择器）别再写 `margin:0`，
  会把 `[data-setting-anchor]` 的 margin 覆盖回 0，实测 groups→dingtalk 间距 -1px。
- 子导航跨度改 `grid-row:2/span 40`：面板各自成行后行数超过 10，跨度不够 `sticky` 会提前失效。
- 窄屏（≤1100px）单列时，升格的面板与 `.settings-body>*` 都要显式回第 1 列。
- 滚动高亮的 `sync()` 必须按**实际位置**排序再取「最后一个越过参考线的面板」：
  视觉顺序由 CSS order 决定、DOM 顺序不是——直接按 DOM 遍历会把高亮停在错误项上
  （实测滚到 3000 停在「订阅群」，应为「学习通」）。

`tests/test_webapp.py::test_settings_panel_order_matches_the_nav_order` 锁「导航相邻两级的 order
值必须递增」；`func_probe.py` 有一条实机版本（按 `getBoundingClientRect().top` 验证单调）。

### 13.8 概览卡为什么不吃 gutter

`.top-banner-row` / `.workspace` / `.bento` 是**容器**（里面装卡片），吃 `padding-inline:var(--gutter)`；
`.summary` **自己就是卡片**，要 `width:calc(100% - 2 * var(--gutter))` + `margin:0 auto`。
两者混在一条共享规则里会让卡框贴到视口边缘（实测 x=0，卡片 1440 宽）。
`tests/test_webapp.py` 里有 `assertNotIn(".summary,.top-banner-row,.workspace,.bento{")` 锁住这件事。

### 13.9 设置页导航：锚点契约与滚动高亮

**导航靠锚点，不靠标题文字。** 每个设置面板带 `data-setting-anchor="<slug>"`，导航按钮带
`data-goto="<slug>"`；两者一一对应（15 项）。老实现按 `.section-title` 的中文前缀匹配，
一改文案就断，而且永远够不到 `<div>` 容器的面板（`closest('section')` 会一路逃到 `#tab-settings` 自己，
于是「托管设置 / 运行信息 / 手机 App」点了等于滚整页）。

静态面板（page.html）：`qq` / `groups` / `about` / `health` / `hosting-status`。
脚本生成的面板（settings.js / pair.js）：`setup` `update` `calendar` `backup` `chaoxing` `dingtalk`
`wechat` `lab` `mobile` `appearance` `push` `hosting` `support` `runtime` `insights`
——都用 `node.dataset.settingAnchor = '<slug>'` 打上。

**推送通道是独立面板**：它原来塞在「托管设置」的 `fieldset#push-settings` 里，导航里那一项是死链。
搬出来时 `#push-save` / `#push-test` / `#push-clear` / `#push-result` 等 id 全部原样保留。

**滚动高亮**：`scroll` 事件用 `requestAnimationFrame` 节流，取「顶边已经越过
`页头底边 + 24` 的最后一个面板」。点击后要 `lockSpy()` 锁住高亮——平滑滚动到远处的面板要一两秒，
期间 `sync()` 会把高亮抢到中途路过的面板上（实测过：点「推送通道」，高亮跳到「外观」）。
锁的解除同时挂在 `scrollend` 事件和一个定时兜底上。

### 13.10 设置项搜索

`#settings-search`（`role=searchbox`）在子导航顶部，纯前端过滤，不碰接口：

- 匹配范围 = 面板标题 + `.field>span` / `.name` / `.desc` / `legend` / `label>span>*` / `summary` /
  `button[id]` / `h3` / `h4` / `.kv span:first-child` 的文本。**不抓 `.setting-note` 长段落**，
  否则每块面板都命中。
- 命中 → 面板显示；未命中 → `hidden`，同时把左侧对应导航项也 `hidden`。
  **搜索期间忽略面板原本的隐藏状态**（订阅群没登录时本来是 `hidden`，搜到就该给它看）。
  这里踩过一个反向坑：写成 `panel.hidden = !hit && wasHidden !== '1'` 会让「原本隐藏」的面板
  反而被强制显示出来。
- 清空后按 `data-was-hidden` 恢复原状；Esc 清空、Enter 跳第一个命中。
- 面板会被 settings.js 异步重建（`root.textContent=''` 再重挂），缓存的节点会变成孤儿，
  且它还会主动清掉 `#groupbox` 的 `hidden`——所以 `applyFilter()` 每次先重新
  `collectPanels()`，并挂一个 `MutationObserver` 在面板变化时重跑一次（`observer.takeRecords()`
  丢掉自己造成的记录，收敛即停）。

### 13.11 悬浮通知中心（NHNotify）：给后续提醒留的唯一通道

重要通知、软件断连、版本更新推送以后都走这里，**不要再各自往页头加横条**。

**DOM**（page.html，`#announce-panel` 前面）：
`#notice-stack`（`role=region aria-label=通知 aria-live=polite`）+ `#notice-modal`（`role=dialog aria-modal=true`）。
脚本 `js/notice.js` 必须先加载（在 `announce.js` 之前），它挂出 `window.NHNotify`。

**API**：
```js
NHNotify.push({id, level, title, body, actionLabel, onOpen, sticky, ttl});
NHNotify.dismiss(id); NHNotify.clear();
NHNotify.openModal({title, body | bodyNode, actions: [{label, primary, onClick, keepOpen}]});
NHNotify.closeModal();
```
- `level`：`info` / `ok` / `warn` / `bad` → 左侧 3px 描边 + 圆点都用 `--teal/--ok/--warn/--bad`，深浅主题共用一套令牌。
- 同 `id` 再推是**原地更新**（不重复堆卡）；最多同时 3 张，超出先丢最旧的非 sticky 卡。
- `sticky:true` 不自动收起；否则 `ttl`（默认 6000ms）后收起，**鼠标悬停时暂停**。
- 有 `onOpen` 时整卡变成可点/可聚焦按钮（Enter、Space 都能触发）。
- 弹窗内 Esc、点遮罩、点 × 都关闭，关闭后焦点回到打开它的元素。

**已接入**：配置体检（`id='setup-checkup'`，`level='warn'`，`sticky`）。
点开弹窗里复用了 `renderCheckupRow`（行内「去装引擎 / 去开微信 / 帮我连上」等动作不变），
底部动作：继续配置（回到首装向导）、去设置里配、先不管。

**接入新提醒的模板**：
```js
NHNotify.push({
  id: 'qq-offline',            // 稳定 id，便于去重/更新/撤销
  level: 'bad',
  title: 'QQ 掉线了',
  body: '最近一次心跳 4 分钟前',
  actionLabel: '查看',
  onOpen: function () { /* 打开你自己的弹窗或跳转 */ }
});
```

**样式契约**：`.notice-stack` 固定右上（`top: calc(var(--island-pad) + var(--space-4))`，右边缘对齐版心）；
窄屏（≤760px）改到底部、通栏、贴在底栏标签栏上方 `calc(72px + env(safe-area-inset-bottom))`。
卡片是**磨砂玻璃**，与浮岛（`.masthead-row`）共用同一套材质，别再各写一份：
```css
background:color-mix(in srgb,var(--paper-raise) 82%,transparent);
-webkit-backdrop-filter:saturate(180%) blur(20px);backdrop-filter:saturate(180%) blur(20px);
box-shadow:inset 0 1px 0 var(--sheen),0 4px 18px rgba(40,40,28,.10);
@media (prefers-reduced-transparency:reduce){ background:var(--paper-raise);backdrop-filter:none; }
```
82% 是「能透出底下纹理、正文对比度仍达标」的折中值，深色主题共用同一组令牌；
背景模糊只用在固定浮层（浮岛 + 通知卡），**页面里的卡片保持不透明**，避免大面积实时模糊拖性能。
入场动画是 `notice-in`（220ms），关动效由全局 `[data-motion=paused]` / `prefers-reduced-motion` 兜底。
注意：浮岛自身的材质不要跟着改——本轮只调整通知卡片。

### 13.12 设置左栏的 sticky 陷阱

`.settings-subnav` 是 `position:sticky`，随视口高度可能比可视区还高（15 项 + 搜索框 ≈ 776px，
而笔记本可视高度常常只有 700–800）。sticky 元素超高时**只能钉住顶部，底部永远滚不出来**——
实测 813px 视口里最后几项完全不可达。

必须同时给：`max-height: calc(100vh - var(--island-pad) - 2 * var(--space-4))` + `overflow-y:auto`
+ `overscroll-behavior:contain`（避免滚到边界继续带动整页）。
窄屏（≤1100px）导航改横排时，这些要显式撤销：`max-height:none;overflow-x:auto;overflow-y:hidden`。

### 13.13 托管设置为什么改成扁平分组

原先三个 `fieldset.preference-list`（各自带 `border` + `background:var(--paper)`）塞进
`.hosting-settings`（自带边框与底色）里，等于**卡片套卡片套三份小卡**；`legend` 是 11px 灰字压在边框上，
和全站其它面板（`.field` 行 + 虚线分隔）不是一套语汇；主行动「开始/结束托管」还压在最底部。

现在：`h2 托管设置` → 状态行 → `hosting-actions-main`（开始/结束托管 + 托盘提示）→ 结果行 →
三组「`.settings-group-title` + `.settings-rows` 里的 `.field` 行」→ 风险提示。
行式结构的 CSS 在 `#tab-settings .hosting-panel` 作用域内，`.field>span small` 是长说明的位置；
所有表单控件 id 原样保留（settings.js 全靠它们读写）。别再往这里放 `fieldset` 卡片。

### 13.15 主题/悬停「文字消失」：teal-soft 底与 --ink 的对比度陷阱

深色下 `--teal-soft`（rgba(151,168,120,.16)）叠在 `--paper` 上合成 ≈`#36382A`，而 `--ink` 是
`#EDE6D2`——**9.6:1 没问题**；但如果底色是更亮的 surface（比如把选中主题卡刷成 teal-soft
而文字继承 --ink），叠加 `#FBF8F0` 之类亮底时会掉到 2.2:1，看起来就是「文字消失」。

规则：**实色状态底（选中/悬停）一律用 `--paper-sink`，不用 `--teal-soft`**；文字色显式锁
`color:var(--ink)`，不依赖继承。已修四处：主题选项卡、`.tabs button:hover`、
`.settings-subnav button:hover`、`.notice-open:hover`（均补了
`html[data-theme=dark] … {background:var(--paper-sink)}` 覆盖）。

### 13.16 本周小结柱状图：容器高度必须 ≥ 柱高算式

柱高按 `count / max * 56px` 算，容器 `.rail-bars` 之前只有 34px 高、`overflow:hidden`——
七根柱全部被裁掉（实测 fillH 全为 0）。容器现在 72px；0 值的日子留 3px 基线桩；
柱顶数字 `>0` 才渲染（`.rail-bar-value` 从 display:none 改为块级 11px 灰字）。

### 13.17 常用设置磁贴（quick-settings）

`#quick-settings` 通栏插在设置页抬头与两列主体之间（`grid-row:2`，两列主体顺延到第 3 行起）。
六个磁贴（主题外观 / 托管 / 推送通道 / iPhone 日历 / 订阅群 / 服务自检），
`data-quick-goto="<slug>"` 复用左导航的 `panelEl()/buttonFor()/lockSpy()/scrollTargetIntoView()`。
跳转与高亮共用一套契约，新增磁贴只要 slug 存在即可。

### 13.18 页签首屏空档：「开始使用」引导块只归待办

`#journey` 之前没有页签约束，推/收/设置三页的抬头被它顶到 462px（浮岛底 60 → 首块 gap 402）。
`body[data-tab]:not([data-tab=tasks]) .journey{display:none}` 之后三页签首块 gap 28px，
与待办页（概览卡）同一条起线。

### 13.19 深色下的「浅绿底 + 亮字」是禁区

`--teal-soft` 在深色纸面上合成 ≈`#38382A`。与 `--teal`（深色 `#A9BA8A`）对比只有 **1.23:1**、
与 `--ink` 约 9.6:1（够）、但与 `--teal-strong` 类亮绿就是灾难。凡是**底色 wash + 强调绿字**
的组合在深色下都会「字消失」（实测：`.pin-toggle[aria-pressed=true]` 1.23:1）。

统一规则：深色下所有 hover / 选中底换成 `--paper-sink`（中性提亮），字色用 `--ink`
或必要时 `--teal`（此时对 `--paper-sink` 有 5.6:1）。已在 `html[data-theme=dark] {…}` 段里
集中覆盖 18 个选择器（brand / tabs / announce-bell / host-state / upcoming-mode / mini-btn /
preference / calendar-day / group-row / inbox-counts / settings-subnav / notice-* / btn 全族 /
urgent-toggle / pin-toggle / journey-step）。新增交互态时**先想深色**，别无脑套 `teal-soft`。

排查脚本（一次性验证用，不改产品）：
遍历所有含文本节点的元素，取 `color` 与「向上找到的第一个非透明背景色」算 WCAG 对比度，
列出 < 3:1 的项。唯一允许的例外是 `.pinned-resize`（文字故意透明，靠 ::before 画拖拽把手）。

### 13.20 胶囊开关：纯 CSS 换掉原生 checkbox

设置页 23 处 `<input type=checkbox>` 全部改成胶囊开关，**DOM 与 JS 一行没动**（仍按 `checked` 读写）：

```css
input[type=checkbox]{
  appearance:none;width:42px;height:24px;border:1px solid var(--line-strong);border-radius:999px;
  background-color:var(--paper-sink);
  background-image:radial-gradient(circle at center,var(--paper-raise) 0 8px,transparent 8.5px);
  background-position:3px center;   /* 关：圆钮在左 */
}
input[type=checkbox]:checked{background-color:var(--teal);background-position:20px center}  /* 开：滑到右 */
```
深色下把圆钮换成「亮底 / 深钮」两套（`#F6F2E7` 与 `#20201A`），否则深色轨道上的钮看不清。
`radio` 保持原生（`accent-color`）；`.preference input{margin-left:auto}` 让开关靠右。

### 13.21 Windows 浮球（floatball）与它的预览页

真实实现：`floatball_page.py` 是一个自包含 HTML（同源 WebView2），`floatball_host.py` 开
**380×600 无边框置顶窗口**，用 `SetWindowRgn` 圆角区域裁剪，只露出页面右下角：
收起时 72×72 的球，展开时整张 380×600 面板。页面通过 `document.title = "nh-floatball:open:WxH"`
把视口尺寸报给宿主，宿主据此 `_fit_window()` 定尺。

**为什么本机看不到 UI**：这段只在 Windows 上跑（WebView2 + ctypes 调 user32/gdi32），
在 macOS 上没有任何窗口会创建；浏览器打开 `/floatball` 能看到页面本体但没有「浮球窗口」效果
（`body{position:fixed}` + 面板 `inset:0`，整页透明，球在右下角）。

**预览入口**：`.ui-verify/floatball_preview.html`——桌面浏览器直接打开，把 380×600 窗口
放在一个示意的桌面上，可切换收起/展开、深浅主题，带 6 条演示待办与全部行内动作。
它和真实页面共用同一套纸墨令牌与尺寸（72 / 380×600 / r-3），改真实页时**同步改预览页**。

### 13.22 演示数据：公告 / 更新 / 通知全形态

`.ui-verify/seed_demo.py` 现在除了消息·摘要·待办·投递，还写
`<data_dir>/updates/announcements.json` 与 `updates/state.json`（公告 + 更新检查共用这一份）：

- `announce_response.items`：一条 `critical`+`sticky`（红色、必须确认）、一条 `info` 使用技巧、一条版本说明；
- `update_result`：`has_update: true` + `latest{version,notes,download_url…}`（触发更新横幅/卡片）；
- `update_checked_epoch`：按当前时间写，避免前端因缓存过期去联网重取。

`.ui-verify/build_demo.py` 另给单文件 demo 注入一段**只存在于演示包**的 `NHNotify` 演示脚本：
依次推 `ok / info(带动作) / warn(带动作) / bad(带动作) / warn(sticky 更新) / info(纯文本) / ok(纯文本)`，
覆盖四种级别、有/无动作、常驻/自动消失、多条堆叠（上限 3）。产品页面不含这段。

### 13.23 胶囊开关被输入框定宽拉长的坑

`.field>input,.field>select{width:auto;min-width:180px;flex:0 1 300px}` 是为了让表单里的文本框
有最小宽度，但它同样命中 `input[type=checkbox]`——托管设置里 5 个开关因此变成 **180×28**，
圆钮也被 `background-position:20px` 推到轨道外（实测）。

修法两处一起：
1. `input[type=checkbox]` 自身把尺寸钉死（`width/min-width/max-width` 与 `height/min-height/max-height`
   三件套都给），光写 `width` 压不住 `min-width:180px`；
2. `.field>input[type=checkbox]` 显式排除：`width:42px;min-width:42px;max-width:42px;flex:none;margin-left:auto`。

圆钮位置用 `background-position:calc(100% - 3px) center`，左右各留 3px 对称边距，
别再写死像素（42px 轨道减 16px 钮 = 26px，除以 2 得 13px，但边框占 1px，写死会偏）。

### 13.24 showFeedback 走悬浮通知（成功即走，失败留底部）

`showFeedback(message, state, retry)` 现在按「要不要重试」分流：

- `success` / `warning` / `loading` **且没有 retry** → `NHNotify.push({id:'feedback', level, title, ttl:3000})`，
  右上角卡片、3 秒自动收起、可多张叠放；
- 其余（含所有 `error`、以及带 `retry` 的）→ `renderActionFeedback()`，仍是页面底部那条可点「重试」的条。

同 `id:'feedback'` 的推送是原地更新：连续保存两次不会叠两张重复卡。
探针口径同步了：`func_probe` 的「转成待办有结果反馈」改查 `#notice-stack .notice-card`。

### 13.25 通知级别：加 critical（紧急红）

`info / ok / warn / bad / critical`。`critical` 用 `--bad`（浅 `#B0503A` / 深 `#C97A62`）：
圆点带 `box-shadow:0 0 0 3px` 的 24% 光晕、卡片描边转 42% 红。
「重要通知 / 关键公告 / 引擎断连」用 critical，「一般提醒（待配置、有新版本）」仍用 warn。

### 13.26 设置页左栏与右列的对齐靠 grid 行，不靠 padding

`.quick-settings` 只占右列（`grid-column:2;grid-row:2`），`.settings-subnav` 也从 `grid-row:2` 起算，
两栏首元素天然同顶（实测搜索框与「常用设置」标题 top 都是 294）。

**别再给左栏补 `padding-top` 去"对齐"**——试过一版按右列 h2 行高补 39px，直接把搜索框推到标题下面。
左栏内部 gap 是 16px，右列面板之间是 12px，两者不需要相等：对齐的是**起点**，不是每一行。

### 13.27 浮球页的圆角与胶囊

浮球（`floatball_page.py`）与主软件共用同一套圆角语义：**控件 6px / 卡片与面板 10px / 胶囊 999px / 正圆 50%**
（早前混着 7/8/12px，已全部收敛）。条目右上角的标签组
`.entry-head>span{display:flex;flex-direction:column;align-items:flex-end;gap:4px}`，
`.entry-head` 用 `align-items:center` 让胶囊列与左侧标题垂直居中。
`.ui-verify/floatball_preview.html` 必须同步这两条。

### 13.28 深色开关的两个坑（都已修，写法别再退回去）

**坑一：`background` 简写会把圆钮位置重置掉。**
深色段里有一条 `html[data-theme=dark] input,select,textarea{background:var(--paper-alt)}`，
`background` 是简写属性，会把 `background-position / repeat / size` 一起写回初始值
（`0% 0%`）。它特异性（0,2,1）高于 `input[type=checkbox]`（0,1,1），于是**未选中态的圆钮被甩到左上角**。
实测：`background-position` 从 `3px center` 变成 `0% 0%`。

现在这条规则排除 checkbox：
`html[data-theme=dark] input:not([type=checkbox]),html[data-theme=dark] select,…`。
**新增任何 `input{background:…}` 简写规则时，都要 `:not([type=checkbox])`。**

**坑二：配色别分散写，用变量。**
开关的四个颜色集中在 `:root` 与 `html[data-theme=dark]`：
```css
--switch-track-off / --switch-knob-off / --switch-track-on / --switch-knob-on
```
浅色：关 `#E3D9C2` + 白钮；开 `--teal` + 白钮。
深色：关 `#3E3E31` + 纸白钮；开 `--teal` + `#1F1F18` 深钮。
（原来的深色轨道用 `--paper-sink:#1D1D16`，和页面底 `#23231B` 几乎同色，只剩一个白点，看不出是开关。）

`background-image` 里圆钮用 `radial-gradient(circle at center,var(--switch-knob-*) 0 8px,transparent 8.5px)`
+ `background-size:16px 16px`，关态 `background-position:3px center`、
开态 `calc(100% - 3px) center`（左右各留 3px 对称）。

### 13.29 服务公告并进悬浮通知（页头横幅整块退场）

页头那格 `#announce-banner` 已经删掉，公告改为推 `NHNotify`：

- id `announce-<公告id>`、`priority:true`（插栈顶，压在「还有 N 项没配好」之上）；
- 等级映射 `info → info`、`warning → warn`、`critical → critical`；
- `dismissible:!item.seen_required` + `sticky:!!item.seen_required`：必须确认的公告没有 ×、不自动消失；
- `onOpen` 打开原公告详情弹窗（`renderAnnounceModal()` + `modal.hidden=false`）；
- `onDismiss` 才写 `seen_announce_ids`——见 13.30，这是「谁算用户关掉」的边界。

`renderAnnouncementFailureHint()`（拉取失败那句「公告暂时无法更新，稍后会自动重试。」）也改走通知：
`id:'announce-failure'`、`warn`、`sticky:true`；`refreshAnnouncements` 成功时先
`NHNotify.dismiss('announce-failure')` 再渲染真实公告。

**坑：** `renderAnnounceModal()` 原来靠「横幅可见就先不弹」做优先序，横幅删掉后那半句判断
（`announcementBanner&&!announcementBanner.hidden`）会取到 null 而永远放行，所以整条条件一起删掉，
只留首装向导 / 欢迎框优先。

### 13.30 「用户关掉」与「程序撤掉」必须分开

`notice.js` 的 `dismiss(id)` 被超时、`dropOldest()`、`clear()` 共用。若在这个函数里无条件回调
`onDismiss`，公告会被记进 `seen_announce_ids`——用户没点 × 也算关掉，下次启动不再提醒。

所以：`dismiss(id, byUser)`，只有 × 的点击处理传 `true`；`clear()` 里
`Array.from(items.keys()).forEach(function(id){dismiss(id);})`（直接 `forEach(dismiss)` 会把
index 当第二参，`index>0` 时误判成 byUser=true）。

同 id 再推时的更新分支也必须刷新 `onOpen/onDismiss`，并把 `sticky` 带进计时器
（`startTimer(id, sticky ? 0 : (options.ttl || 6000))`），否则 sticky 卡被重推一次就会 6 秒后自己消失。

另外卡上落了 `data-notice-id`，探针/测试按 id 找卡，不依赖「栈顶是谁」。

### 13.31 「开始使用」改成首启弹窗 + 通知颜色 + 真磨砂

**弹窗化：** `#journey` 从 `<main>` 里搬出来，变成 `div.journey-overlay`（fixed 遮罩 + 居中）
包一层 `section.journey-card`（`min(760px,100%)`、`max-height:86vh`、自带滚动）。
`body[data-tab]:not([data-tab=tasks]) .journey{display:none}` 这条随页面卡一起删——
弹窗不需要按页签藏。窄屏规则**必须**并进 19 节那条 760 媒体查询里：另开一条
`@media(max-width:760px){…}` 会让测试的 `_css_block(css, "@media(max-width:760px){")` 只读到第一条，
连带 `.upcoming-carousel` / `.calendar-day` 的断言全部误红。

**通知颜色：** 令牌 `--notice-tint` 默认 `var(--paper-raise)`（深浅各自解析），
设置 → 外观里「强调色」下面多一组圆点（`data-tint=""` 表示跟随纸面，其余五个与强调色同色相）。
交互复用 `initAccentPreference` 那套：`localStorage['nh_notice_tint']`、写内联
`--teal` 换成 `--notice-tint`。启动脚本要在首帧前恢复，否则会先闪一下默认色。

**真磨砂（这次的教训）：** 卡片底色不能直接用 `--notice-tint`。浅色主题下用户挑的色是中等饱和，
文字压上去对比度不够；深色主题下问题反过来。做法是先调和再压透明：

```css
background:color-mix(in srgb,color-mix(in srgb,var(--notice-tint) 30%,var(--paper-raise)) 56%,transparent);
backdrop-filter:saturate(200%) blur(28px);
```

内层调和保证任何选项下正文都 ≥4.5:1，外层 56% + 28px 才真的像玻璃（62% 用户嫌闷；更早的 82%/20px 在纸底上
等于「淡一点的纸」）。`prefers-reduced-transparency:reduce` 回退到内层的实色，颜色仍然跟随设置。

**浮岛（`.masthead-row`）不许跟着改**：它仍是 82% / 18px 那套，测试里有断言守着。

### 13.32 演示快照与宣传中间产物不入库

仓库只留源码、行为文档与 README 要用的成品图。这几类留在本地、由脚本生成，不进版本库：

- **单文件演示页**（内联样式与 6 个脚本 + 33 个接口快照，约 575 KB）：`.ui-verify/build_demo.py` 生成。
  要给别人看界面时本地跑一次，别提交——它会随 UI 频繁变化，进库就是一份永远过时的副本。
- **宣传图的九宫格中间产物**（`beta-grid*.png` / `beta-grid.html`）：海报 `poster-16x9.png` 才是成品，
  中间件每次改文案都要重排，同样只留本地。
- **演示数据**（`.ui-verify/seed_demo.py` 灌的群、消息、待办）：含大量合成中文样例，纯粹为看界面效果，
  与产品行为无关；仓库里没有也不影响任何人跑起来。
