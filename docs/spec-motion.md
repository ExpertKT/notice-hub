# 界面动效口径（第一批）

用户原话：「对话框的拉出没有曲线动画过渡，要检查类似情况。」

盘点结果在 `docs` 外的临时报告 `nh-tmp/diag-motion-inventory.md`（巡检产出，按行号可查）。这一页只写**要改成什么样、改哪几行、怎么算做完**，不写审美评论。

## 一、现状里真正扎眼的几处

1. **宽屏对话面板是「蹦」出来的。** `page.html:2238` 的 `main>#agent-dock[data-state=awake]` 从网格流里脱出、改成 `position:absolute`，高度按内容长；同一时刻 `main` 的 `grid-template-rows` 从一份换成另一份（`:2237`）。地图和右栏有 `rail-in 240ms`（`:2213`、`:2220`），**面板本身没有过渡** —— 旁边的东西都在动，主角在跳。
2. **几个盖在页面上的浮层全是硬切。** `[hidden]{display:none!important}`（`:171`）一刀切：`.announce-modal`（`:1452`，公告弹窗，用户读得最久）、`.journey-overlay`（`:1588`，首启向导）、`.star-prompt`、`.platform-setup-wizard`、通知详情弹窗（`js/notice.js:238/245`）、路线条 `#trip-route`（`js/trip.js:464/476/488/506`）、断网条 `#net-bar`（`:2293`）都是「出现即到位、消失即不见」。
3. **唤醒时周围整片 `display` 翻转。** `.agent-head`（`:385`）、`.summary` 与 `.task-board-block`（`:622-623`）、`.trip-side`/`#trip-legend`/`#trip-map-meta`/`.trip-map-note`（`:610-613`）在同一帧消失或出现。
4. **个别时长超标。** `summary-enter 420ms`（`:323`）、`task-focus 1600ms`（`:376`）—— 后者一个描边亮 1.6 秒，看第二遍就烦。
5. **`prefers-reduced-motion` 只覆盖了两条**（`:2248` 只管宽屏地图/右栏）。新增动画必须一并进去。

## 二、口径（照这个写，不要另发明）

- **进场**：`opacity 0→1` + `translateY(6px)→0`，`160ms`，`var(--ease-out)`。浮层（带遮罩的那种）加 `scale(.99)→1` 就够，不再加大位移 —— 位移大了像玩具。
- **出场**：只有 `opacity 1→0`，`110ms`，`var(--ease-in)`。短、且一定要真的等它跑完再 `hidden`。
- **面板长高（宽屏对话）**：`translateY(100%)→0`，`220ms`，`var(--ease-drawer)`。它贴着视口底边，等于从下沿升上来；不要动 `height`（内容驱动的 `height` 只能一帧一帧量，必然抖）。
- **不要动 `height` / `max-height` / `display` 做动画**。`display` 翻转本身用来触发进场动画（`display:none → block` 会让该元素上的 animation 重新开始），这是唯一允许的「配合」。
- 时长上限 **240ms**；超过 240 的一律压下来，`task-focus` 单独压到 `600ms`。
- 一个缓动只从现有 token 里挑：`--ease-out`（进场）、`--ease-in`（出场）、`--ease-drawer`（贴边滑入）。**不许新造 cubic-bezier**，除 `trip.js:4650` 那处历史遗留可顺手换成 token。

## 三、怎么落（三块，按顺序）

### 1. 一套公用动画 + 一个 JS 帮手

`page.html` 的 CSS 段（放 20 节字号档之前）加：

```css
@keyframes surface-in{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
@keyframes surface-out{from{opacity:1}to{opacity:0}}
@keyframes sheet-in{from{transform:translateY(100%)}to{transform:none}}
.surface-in{animation:surface-in 160ms var(--ease-out) both}
.surface-out{animation:surface-out 110ms var(--ease-in) both}
@media(prefers-reduced-motion:reduce){
  .surface-in,.surface-out{animation:none}
}
```

JS 帮手（`page.html` 的内联脚本里已有 `var` 风格的公用函数，跟着同一套写法）：

```js
function surfaceShow(el){
  if(!el) return;
  el.classList.remove('surface-out');
  var wasHidden = el.hidden;
  el.hidden = false;
  if(!wasHidden) return;
  el.classList.remove('surface-in');
  void el.offsetWidth;          // 重启动画，只读一次布局
  el.classList.add('surface-in');
}
function surfaceHide(el, done){
  if(!el || el.hidden){ if(done)done(); return; }
  if(window.matchMedia('(prefers-reduced-motion:reduce)').matches){ el.hidden = true; if(done)done(); return; }
  el.classList.remove('surface-in');
  el.classList.add('surface-out');
  var finished = false;
  var finish = function(){ if(finished) return; finished = true; el.hidden = true; el.classList.remove('surface-out'); if(done)done(); };
  el.addEventListener('animationend', finish, {once:true});
  setTimeout(finish, 180);      // 动画没跑起来（元素不可见/被打断）也要收尾
}
```

`setTimeout` 那行不是冗余：`animationend` 在元素被祖先 `display:none` 吃掉、或者页面在后台标签页时会不来，没有它面板就永远关不掉。这条踩过。

### 2. 浮层改用帮手

逐个把「`el.hidden = false/true`」换成 `surfaceShow(el)` / `surfaceHide(el, cb)`：

| 元素 | 现在的写法 | 位置 |
| --- | --- | --- |
| 公告弹窗 `.announce-modal` | `modal.hidden=false/true` | `js/announce.js:50`、`:432` |
| 通知详情弹窗 `#notice-modal` | 同上 | `js/notice.js:238`、`:245`（Esc 那条在 `announce.js:442`） |
| 首启向导 `.journey-overlay` | `box.hidden=…` | `js/guide.js:449`、`:483`、`:499` |
| 路线条 `#trip-route` | `panel.hidden=false/true` | `js/trip.js:464`、`:476`、`:488`、`:506` |
| 断网条 `#net-bar` | `bar.hidden` + `body.classList` | `page.html:3581-3608` |
| 星标提示 `.star-prompt`、设置向导 `.platform-setup-wizard` | `hidden` 切换 | 在 `page.html` 内联脚本里，grep `.star-prompt`、`.platform-setup-wizard` 找调用点 |

**出场必须等动画跑完**：凡是在 `surfaceHide` 之后马上读几何、或者要靠 `hidden` 让位给下一步的地方，把后续动作放进 `done` 回调里，不要写在下一行。

### 3. 宽屏对话面板与周围地块

```css
@media(min-width:1100px){
  main>#agent-dock[data-state=awake]{animation:sheet-in 220ms var(--ease-drawer) both}
}
```

周围那些在唤醒/收起时 `display` 翻转的块（`.agent-head`、`.summary`、`.task-board-block`、`.trip-side`、`#trip-legend`、`#trip-map-meta`、`.trip-map-note`）：出现方向加 `.surface-in` 的时长（160ms），消失方向不做动画 —— 它们是被别的东西挤走的，给退场只会在中间露出空洞。做法：在这些选择器的「出现态」上写 `animation:surface-in 160ms var(--ease-out) both`。

`summary-enter` 从 `420ms` 降到 `240ms`；`task-focus` 从 `1600ms` 降到 `600ms`，两段关键帧并成一段。

## 四、验收

1. 单测：`python -m unittest tests.test_webapp tests.test_webapp_assets -v` 两轮全绿（改的是静态资源，测试只守文件完整性）。
2. 截图（照 `notice-hub-ui-verify`）：
   - 1280 暗色：待机 → 点输入框 → 在 120ms 处抓一张（面板正在上升，能看到它没到底），再等 300ms 抓终态；
   - 390 亮色：打开公告弹窗的 80ms 中间帧 + 终态；
   - 390 亮色：断网条出现与消失各一张（用 CDP 的 `Network.emulateNetworkConditions` 断网）。
3. 交互回归：`prefers-reduced-motion` 打开后，上面四处都要「立刻到位、不留空白」；连点输入框两次不出现双份动画；面板出场期间点外部不会卡死（`setTimeout` 兜底那条）。

## 五、不做

- 不做滚动视差、不做视口进入动画（IntersectionObserver 那套）。工具型界面，列表滚过来就淡进来一次是噪音。
- 不给「点别处收起」加回弹。`--ease-spring` 只留给复选框圆钮。
- 不重写现有 `rail-in`、`brand-spin`、`agent-wait` 那几条 —— 它们已经对。
