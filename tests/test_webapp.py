from __future__ import annotations

import datetime as dt
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

from PIL import Image
from html.parser import HTMLParser

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from noticehub import host_actions, webapp  # noqa: E402
from noticehub import updater  # noqa: E402
from noticehub import ics  # noqa: E402
from noticehub import pairing  # noqa: E402
from noticehub.config import Settings  # noqa: E402
from noticehub.qr import matrix  # noqa: E402
from noticehub.store import Store  # noqa: E402
from noticehub.timeutil import iso  # noqa: E402
from noticehub.webapp import PAGE_HTML, TaskWebServer, group_tasks, overview  # noqa: E402
from noticehub.version import APP_CHANNEL, APP_VERSION  # noqa: E402


def _read_normalized(path: Path) -> str:
    """读文本并统一换行，避免 CRLF（Windows 编辑器/脚本写出）让多行断言误红。"""
    return path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")


def _css_rule(css: str, selector: str) -> str:
    """按选择器取出一条规则的声明体；规则不见了就当场失败，不许静默变成「查无此项」。"""
    match = re.search(r"(?m)^" + re.escape(selector) + r"([^}]*)\}", css)
    assert match is not None, selector
    return match.group(1)


def _css_block(css: str, header: str) -> str:
    """按括号配平取出 header{...} 的内部文本（@media 里嵌的规则也算在内）。"""
    start = css.index(header) + len(header)
    depth, cursor = 1, start
    while depth and cursor < len(css):
        if css[cursor] == "{":
            depth += 1
        elif css[cursor] == "}":
            depth -= 1
        cursor += 1
    return css[start:cursor - 1]


def _read_json_when_ready(path: Path, deadline: float) -> tuple[dict | None, int, str]:
    """在总预算内反复读 JSON，把「暂时读不到」当「还没好」而不是失败。

    后台刷新是异步写盘，`updates_api._write_updates_state` 用 `tmp.replace(path)` 覆盖写：
    Windows 上覆盖与并发 `read_text` 会撞 `PermissionError: [Errno 13]`，满载时约 6%。
    文件还没落（FileNotFoundError）、正写到一半（JSONDecodeError）同理。

    返回 `(state, attempts, last_error)`：`state` 为 None 表示预算内始终没读到可解析的一份，
    `attempts` 是实际尝试次数，`last_error` 是最后一次的问题——三者一起进失败信息，
    才分得清「是竞态还是真回归」。

    轮询间隔取 0.1 秒而不是 20ms：读得太密会一直占着写线程要覆盖的那个文件，
    把它耗到重试用尽（写侧因此抛异常退出，状态永远不更新）。6 秒预算内仍有 ~60 次机会。
    """
    attempts = 0
    last_error = ""
    while True:
        attempts += 1
        try:
            return json.loads(path.read_text(encoding="utf-8")), attempts, ""
        except (OSError, json.JSONDecodeError) as exc:
            # OSError 含 FileNotFoundError / PermissionError；JSONDecodeError 是 ValueError 子类。
            last_error = "%s: %s" % (exc.__class__.__name__, exc)
        if time.monotonic() >= deadline:
            return None, attempts, last_error
        time.sleep(0.1)


# 前端已把部分逻辑拆到 web_static/js/*.js：断言「页面里有什么」时要用合并后的全文。
PAGE_DIR = Path(__file__).resolve().parents[1] / "noticehub" / "web_static"
PAGE_HTML = _read_normalized(PAGE_DIR / "page.html")
# 只有脚本部分的合并文本：要按起止标记切一段函数体时用这个，别用 PAGE_SOURCE，
# 否则结束标记落在 page.html 里会比脚本里的起点还靠前，切出空段或抛 ValueError。
PAGE_SCRIPT = "".join(_read_normalized(p) for p in sorted((PAGE_DIR / "js").glob("*.js")))
PAGE_SOURCE = PAGE_HTML + PAGE_SCRIPT

NOW = dt.datetime(2026, 9, 30, 9, 0, 0)
class _VisibleTextProbe(HTMLParser):
    """收集 <script>/<style> 之外的可见文本。用来抓「JS 漏成正文」这类结构性事故。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_script = False
        self.in_style = False
        self.leaks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.in_script = True
        elif tag == "style":
            self.in_style = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False
        elif tag == "style":
            self.in_style = False

    def handle_data(self, data):
        if self.in_script or self.in_style:
            return
        text = data.strip()
        if not text:
            return
        for marker in ("function ", "=>", "document.getElementById", "var ", "(){", "window."):
            if marker in text:
                self.leaks.append(text[:120])
                return


class PageStructureTest(unittest.TestCase):
    """页面结构的合法性。样式与文案断言救不了「脚本块嵌套」这种事故，单独守一道。"""

    def test_script_blocks_are_never_nested(self) -> None:
        # 内层 <script> 会被浏览器当脚本内容读掉，一直吞到第一个 </script>：
        # 外层块整体语法错误、块后半段变成页面正文、剩下一个无主的 </script>。
        self.assertEqual(PAGE_HTML.count("<script"), PAGE_HTML.count("</script>"))
        for match in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", PAGE_HTML, flags=re.S):
            body = match.group(1)
            line = PAGE_HTML[: match.start()].count("\n") + 1
            self.assertNotIn("<script", body, f"第 {line} 行的脚本块里又开了一个 <script>")

    def test_no_javascript_is_rendered_as_body_text(self) -> None:
        probe = _VisibleTextProbe()
        probe.feed(PAGE_HTML)
        self.assertEqual(probe.leaks, [], "页面上出现了本应是脚本的源码文本")

    def test_scroll_helper_is_defined_once_inside_a_script_block(self) -> None:
        # guide.js / settings.js / 页面三处都调它；只定义一次，且必须在脚本块内——
        # 落在块外就会变成正文（上面那条测试兜的就是这个）。
        self.assertEqual(PAGE_HTML.count("function scrollTargetIntoView("), 1)
        blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", PAGE_HTML, flags=re.S)
        self.assertTrue(any("function scrollTargetIntoView(" in block for block in blocks))
        self.assertIn("scrollTargetIntoView(target, 'smooth')", PAGE_HTML)

    def test_settings_page_places_masthead_and_panels_in_the_right_columns(self) -> None:
        # 设置页是两列 grid：抬头通栏、子导航占左列、其余面板全在右列。
        # 靠 auto-placement 猜会错位——实测过抬头挤进 212px 窄栏、12 个面板被压成 212px 宽。
        css = PAGE_HTML.rsplit("<style>", 1)[1].split("</style>", 1)[0]
        self.assertIn("grid-template-columns:212px minmax(0,1fr)", css)
        self.assertIn("#tab-settings>.masthead{grid-column:1/-1;grid-row:1;", css)
        # 子导航必须用显式 span 而不是 `2/-1`：隐式网格里 -1 解析成第一条线，
        # 结果它落到第 1 行，和通栏抬头压在一起（实测 y 完全相同）。
        # 面板各自成行后行数超过 10，span 40 兜住（span 999 会生成几百条空行）。
        # 常用设置插在抬头与两列主体之间：抬头 1 → 常用 2 → 子导航从第 3 行跨到末尾。
        # 常用设置只占右列第一行，左栏与它同排起算（通栏会把左栏压到下面，视觉上「偏低」）。
        self.assertIn("#tab-settings>.quick-settings{grid-column:2;grid-row:2;margin:0 0 var(--space-4)}", css)
        self.assertIn("#tab-settings>.settings-subnav{grid-column:1;align-self:start}", css)
        self.assertIn("#tab-settings>.settings-subnav{grid-row:2/span 40}", css)
        # 两栏从同一 grid 行起算（row 2），首元素天然同顶；不许再额外加 padding-top
        # （加过一版，把搜索框推到「常用设置」标题下面 39px）。
        self.assertNotIn("padding-top:calc(var(--fs-6)", _css_rule(css, ".settings-subnav{"))
        # .settings-body 是 settings.js 的挂载点：display:contents 透明化后，
        # 面板升格为外层网格条目，顺序才能用一组 order 值统一编排。
        self.assertIn(".settings-body{display:contents}", css)
        self.assertIn("#tab-settings .settings-body>*{grid-column:2;margin:0 0 var(--space-4)}", css)
        self.assertIn("#tab-settings [data-setting-anchor]{grid-column:2;margin:0 0 var(--space-4)}", css)
        # 行距由面板 margin 提供，行间隙保持 0——span 出的空行才不会有 phantom 高度。
        self.assertIn("gap:0 var(--space-8)", css)
        self.assertNotIn("grid-row:2/-1", css)
        self.assertIn("#tab-settings>:not(.masthead):not(.settings-subnav){grid-column:2;margin:0 0 var(--space-4)}", css)
        # sticky 的落点要和固定浮岛让出的顶部空间对齐。
        self.assertIn("position:sticky;top:calc(var(--island-pad) + var(--space-4))", css)
        # 窄屏单列时三者都要回到第 1 列，否则会撑出隐性的第 2 列。
        narrow = _css_block(css, "@media(max-width:1100px){")
        self.assertIn("#tab-settings{grid-template-columns:minmax(0,1fr)}", narrow)
        self.assertIn("#tab-settings>:not(.masthead):not(.settings-subnav){grid-column:1;grid-row:auto}", narrow)
        # display:contents 升格的面板与 settings-body 的挂载子节点也要回第 1 列（否则撑出隐性第 2 列）。
        self.assertIn("#tab-settings [data-setting-anchor],\n  #tab-settings .settings-body>*\n  {grid-column:1;grid-row:auto}", narrow)

    def test_quick_actions_card_spans_full_width(self) -> None:
        css = PAGE_HTML.rsplit("<style>", 1)[1].split("</style>", 1)[0]
        self.assertIn(".bento>#rail-actions{grid-column:1/-1}", css)

    def test_notice_stack_aligns_with_the_content_edge(self) -> None:
        # 历史用例名保留（beta.17 之前叫这个）。通知已收进右上角铃铛面板，
        # 「贴版心右边缘悬浮」那条约束作废，换成更硬的：不许脱离文档流、必须待在面板的通知页签里。
        css = PAGE_HTML.rsplit("<style>", 1)[1].split("</style>", 1)[0]
        stack_rule = _css_rule(css, ".notice-stack{")
        self.assertNotIn("position:fixed", stack_rule, "通知栈不许再悬浮盖住正文")
        self.assertNotIn("z-index", stack_rule, "通知栈不再自己叠一层")
        self.assertIn("position:fixed", _css_rule(css, ".announce-panel{"), "改由面板承载浮层")
        notice_pane = PAGE_HTML.index('id="announce-pane-notice"')
        announce_pane = PAGE_HTML.index('id="announce-pane-announce"')
        stack = PAGE_HTML.index('id="notice-stack"')
        self.assertGreater(stack, notice_pane, "#notice-stack 必须挂在通知页签里")
        self.assertLess(stack, announce_pane, "#notice-stack 不许跑到公告页签里")

    def test_settings_subnav_aligns_with_the_first_panel(self) -> None:
        css = PAGE_HTML.rsplit("<style>", 1)[1].split("</style>", 1)[0]
        # 左栏（sticky top）与右列首块的落点必须是同一条线：常用设置区通栏，
        # 子导航从它下面开始，与右列第一个面板同一行。
        # 常用设置只占右列第一行，左栏与它同排起算（通栏会把左栏压到下面，视觉上「偏低」）。
        self.assertIn("#tab-settings>.quick-settings{grid-column:2;grid-row:2;margin:0 0 var(--space-4)}", css)
        self.assertIn("#tab-settings>.settings-subnav{grid-column:1;align-self:start}", css)
        self.assertIn("#tab-settings>.settings-subnav{grid-row:2/span 40}", css)
        # 两栏从同一 grid 行起算（row 2），首元素天然同顶；不许再额外加 padding-top
        # （加过一版，把搜索框推到「常用设置」标题下面 39px）。
        self.assertNotIn("padding-top:calc(var(--fs-6)", _css_rule(css, ".settings-subnav{"))

    def test_settings_panel_order_matches_the_nav_order(self) -> None:
        """右列面板自上而下的顺序必须与左侧导航一致——这是这页的基本可用性。"""
        css = PAGE_HTML.rsplit("<style>", 1)[1].split("</style>", 1)[0]
        order_of = {}
        for slug, value in re.findall(r'#tab-settings \[data-setting-anchor="([a-z-]+)"\]\{order:(\d+)\}', css):
            order_of[slug] = int(value)
        nav = re.findall(r'data-goto="([a-z-]+)"', PAGE_HTML)
        self.assertGreaterEqual(len(nav), 15)
        for prev, nxt in zip(nav, nav[1:]):
            self.assertLess(order_of[prev], order_of[nxt],
                            f"导航顺序 {prev} → {nxt} 与面板 order 值不一致")
        # 未列入导航的杂项面板（公告/首次配置/托管状态摘要）落在最前，纠正提示/实验室/支持开发者落在尾部附近。
        self.assertLess(order_of.get("insights", 135), order_of["health"])
        self.assertLess(order_of.get("lab", 140), order_of["health"])
        self.assertGreater(order_of.get("support", 170), order_of["about"])

    def test_settings_subnav_has_no_inner_vertical_scroll(self) -> None:
        """整页只允许一条垂直滚动轴：左导航不得再自带 max-height + overflow-y 内滚。"""
        css = PAGE_HTML.rsplit("<style>", 1)[1].split("</style>", 1)[0]
        subnav = _css_rule(css, ".settings-subnav{")
        self.assertIn("position:sticky;top:calc(var(--island-pad) + var(--space-4))", subnav)
        self.assertNotIn("max-height:calc(100vh", subnav)
        self.assertNotIn("overflow-y:auto", subnav)
        self.assertNotIn("overscroll-behavior:contain", subnav)
        self.assertNotIn("scrollbar-width:thin", subnav)
        # 窄屏单列时导航改横排，横向滚动不属垂直轴，按口径保留。
        narrow = _css_block(css, "@media(max-width:1100px){")
        self.assertIn("max-height:none;overflow-x:auto;overflow-y:hidden;scrollbar-width:none", narrow)

    def test_group_picker_has_no_nested_scroll_axis(self) -> None:
        """设置页群列表按内容自然增高；回溯页 #history-groups 的 220px 内滚保持不变。"""
        css = PAGE_HTML.rsplit("<style>", 1)[1].split("</style>", 1)[0]
        shared = _css_rule(css, ".history-groups,.group-list{")
        self.assertNotIn("max-height", shared)
        self.assertNotIn("overflow:auto", shared)
        self.assertIn("max-height:220px;overflow:auto", _css_rule(css, ".history-groups{"))
        # 微信/钉钉 gsel 群选择区本来就不该自带滚动容器。
        gsel = css[css.index(".gsel{"):css.index("@media (max-width: 760px) {")]
        self.assertNotIn("max-height", gsel)
        self.assertNotIn("overflow:auto", gsel)
        self.assertNotIn("overflow-y:auto", gsel)


    def test_patch_auto_copy_says_update_package(self) -> None:
        """开关文案要说清它管「新版本 + 同版本修复包」，不能只剩修复包一种口径。"""
        self.assertIn("<strong>自动下载更新包</strong>", PAGE_SOURCE)
        self.assertIn("新版本和同版本修复包都会在后台下好", PAGE_SOURCE)
        self.assertIn("'已打开，更新包会自动下载。'", PAGE_SOURCE)
        self.assertIn("'已关闭，更新包不再自动下载，仍可手动检查。'", PAGE_SOURCE)
        self.assertNotIn("<strong>自动接收修复包</strong>", PAGE_SOURCE)

    def test_hosting_panel_uses_flat_groups_not_nested_cards(self) -> None:
        """托管设置原来把三个 fieldset（各自带边框+底色）塞进一张卡片里，是「卡片套卡片」。"""
        self.assertIn('class="settings-group-title"', PAGE_SOURCE)
        self.assertIn('class="settings-rows"', PAGE_SOURCE)
        hosting = PAGE_SOURCE.split("hostingBox.innerHTML='", 1)[1].split("';", 1)[0]
        self.assertNotIn("<fieldset", hosting, "托管设置里不该再有 fieldset 卡片")
        self.assertNotIn('class="preference-list"', hosting)
        # 主行动（开始/结束托管）跟在状态行后面，不再压在整块设置最底部。
        self.assertIn('class="hosting-actions hosting-actions-main"', hosting)
        self.assertLess(hosting.index("hosting-actions-main"), hosting.index("settings-group-title"))
        # 表单控件 id 一个都不能丢（settings.js 全靠它们读写）。
        for element_id in ("hosting-status", "hosting-start", "hosting-stop", "hosting-result",
                           "pref-quit_qq", "pref-restore_qq", "pref-quit_restore_qq",
                           "pref-auto_on_start", "pref-autostart", "pref-floatball-enabled",
                           "pref-catchup-enabled", "pref-catchup-days", "pref-catchup-save",
                           "llm-provider", "llm-key", "llm-model", "llm-save", "llm-off"):
            self.assertIn('id="%s"' % element_id, hosting, element_id)
        # 桌面浮球开关：跟其它自动化选项一样用 label.field（不是 preference），勾选即存，
        # 不能有独立的 result 行（会多占一行让下面内容跳位置）。
        floatball_label = hosting.split("<span>桌面浮球", 1)[1].split("</label>", 1)[0]
        self.assertIn("<small>", floatball_label)
        self.assertIn("默认关闭。打开后屏幕右下角出现浮球", floatball_label)
        self.assertIn("开关状态存在本机，重启后不变。", floatball_label)
        self.assertIn('id="pref-floatball-enabled"', hosting)
        self.assertNotIn('pref-floatball-result', PAGE_SOURCE)
        self.assertIn("floatball_enabled", PAGE_SCRIPT)
        self.assertIn('body:JSON.stringify({floatball_enabled: enabled})', PAGE_SCRIPT)
        self.assertIn("pref-floatball-enabled", PAGE_SCRIPT)

    def test_announce_bell_badge_is_a_dot_without_a_count(self) -> None:
        """未读公告只点一个小红点：徽标里不再写数字（99+ 之后没意义，还挤铃铛）。"""
        self.assertIn('id="announce-bell-badge"', PAGE_HTML)
        # 徽标元素内不许再有文本节点（以前是 <span …>0</span>，换成点状后必须留空）。
        self.assertNotRegex(PAGE_HTML, r'announce-bell-badge[^>]*>\s*\d')
        # 条数只留给读屏和面板列表，公告脚本不许再往徽标里写数字。
        announce_js = _read_normalized(PAGE_DIR / "js" / "announce.js")
        self.assertNotIn("badge.textContent", announce_js)
        self.assertIn("badge.hidden = count <= 0", announce_js)

    def test_floating_notice_center_is_available_for_future_alerts(self) -> None:
        """悬浮通知中心：右侧堆叠 + 点卡开弹窗，供重要通知/断连/更新复用。"""
        self.assertIn('id="notice-stack"', PAGE_HTML)
        self.assertIn('role="region" aria-label="通知" aria-live="polite"', PAGE_HTML)
        self.assertIn('id="notice-modal"', PAGE_HTML)
        self.assertIn('role="dialog" aria-modal="true"', PAGE_HTML)
        self.assertIn('<script src="js/notice.js"></script>', PAGE_HTML)
        notice = (PAGE_DIR / "js" / "notice.js").read_text(encoding="utf-8")
        # 对外 API：推 / 收 / 清 / 开弹窗 / 关弹窗
        for api in ("push: push", "dismiss: dismiss", "clear: clear",
                    "openModal: openModal", "closeModal: closeModal"):
            self.assertIn(api, notice)
        self.assertIn("window.NHNotify = {", notice)
        # 去重、上限、自动收起、悬停暂停、Esc 关闭、焦点归还——都是别人接入时会依赖的行为。
        # MAX_VISIBLE 12：公告卡现在全部 sticky 常挂，3 张会把后面的挤掉（用户报的「通知突然消失」）。
        self.assertIn("var MAX_VISIBLE = 12;", notice)
        self.assertIn("if (existing) {", notice)
        # 同 id 重推：内容没变不重置计时器（公告列表每次刷新重画，不能让通知「又出现」）。
        self.assertIn("var changed = existing.el.dataset.level !== level", notice)
        self.assertIn("if (changed) {", notice)
        # 退场动画 + 关动效兜底直接移除。
        self.assertIn("el.classList.add('is-leaving')", notice)
        self.assertIn("prefers-reduced-motion: reduce", notice)
        self.assertIn("card.addEventListener('mouseenter'", notice)
        self.assertIn("if (event.key === 'Escape') {", notice)
        self.assertIn("if (lastFocused && lastFocused.focus) lastFocused.focus();", notice)
        # 主题：只用令牌，不许出现硬编码颜色/阴影以外的即席取值。
        css = PAGE_HTML.rsplit("<style>", 1)[1].split("</style>", 1)[0]
        stack_rule = _css_rule(css, ".notice-stack{")
        # 通知卡搬进铃铛面板后不再是悬浮层：不许再 fixed/z-index，否则又会压住页面正文。
        self.assertNotIn("position:fixed", stack_rule)
        self.assertNotIn("z-index", stack_rule)
        card_rule = _css_rule(css, ".notice-card{")
        # 磨砂玻璃：「通知颜色」先与纸面按 3:7 调和，再整体压到 56% 不透明 + 28px 背景模糊。
        # 比浮岛那套 82% / 20px 透得多（62% 用户嫌闷，56% 更透一档）；底色走 --notice-tint（设置里可换）。
        self.assertIn("background:color-mix(in srgb,color-mix(in srgb,var(--notice-tint) 30%,var(--paper-raise)) 56%,transparent)", card_rule)
        self.assertIn("-webkit-backdrop-filter:saturate(200%) blur(28px)", card_rule)
        self.assertIn("backdrop-filter:saturate(200%) blur(28px)", card_rule)
        self.assertIn("border:1px solid var(--line)", card_rule)
        self.assertIn("border-radius:var(--r-3)", card_rule)
        # 浮岛材质不许被带着一起变（只有通知卡改成了玻璃）。
        island_rule = _css_rule(css, ".masthead-row{")
        self.assertIn("background:color-mix(in srgb,var(--paper-raise) 82%,transparent)", island_rule)
        self.assertIn("backdrop-filter:saturate(180%) blur(20px)", island_rule)
        # 系统关透明度时回退实色（仍保留所选通知色），不做模糊。
        self.assertIn("background:color-mix(in srgb,var(--notice-tint) 30%,var(--paper-raise));-webkit-backdrop-filter:none;backdrop-filter:none", css)
        # 通知色令牌默认跟随卡片纸面（浅/深主题各自解析出对应的那一档）。
        self.assertIn("--notice-tint:var(--paper-raise)", css)
        # 启动脚本在首帧之前恢复已保存的通知色，避免先闪一下默认色。
        self.assertIn("localStorage.getItem('nh_notice_tint')", PAGE_HTML.split("<body", 1)[0])
        # 深浅两套都靠状态令牌，不各写一套颜色。
        self.assertNotIn(".notice-card[data-level]", PAGE_HTML.replace(".notice-card[data-level=info]", ""))

    def test_notice_tint_is_customizable_below_the_accent_choice(self) -> None:
        """「通知颜色」必须在设置 → 外观里、紧跟「强调色」之后，同一套圆点交互，存 localStorage。"""
        appearance_html = PAGE_SOURCE.split("themeBox.innerHTML=", 1)[1][:6000]
        self.assertIn("pref-notice-tint", appearance_html)
        self.assertLess(
            appearance_html.index("pref-accent"),
            appearance_html.index("pref-notice-tint"),
            "通知颜色排在强调色下面（用户指定）",
        )
        self.assertIn('<legend>通知颜色</legend>', appearance_html)
        # 首个圆点是「跟随纸面」（data-tint 为空），后面五个与强调色同一套色相。
        self.assertIn('data-tint="" aria-label="跟随纸面"', appearance_html)
        for label in ("鼠尾草", "梅紫", "赭橙", "黛蓝", "石墨"):
            self.assertIn('aria-label="%s" title="%s" style="background:color-mix(in srgb,' % (label, label), appearance_html)
        # 交互与强调色同一套：initNoticeTintPreference 定义在页面脚本里、外观面板初始化时被调用。
        self.assertIn("function initNoticeTintPreference() {", PAGE_HTML)
        self.assertIn("initAccentPreference(); initNoticeTintPreference();", PAGE_SOURCE)
        self.assertIn("localStorage.setItem('nh_notice_tint'", PAGE_HTML)
        self.assertIn("localStorage.removeItem('nh_notice_tint')", PAGE_HTML)
        # 应用方式：写 --notice-tint（卡片背景引用这个变量），「跟随纸面」清掉内联覆盖。
        self.assertIn("setProperty('--notice-tint', tint)", PAGE_HTML)
        self.assertIn("removeProperty('--notice-tint')", PAGE_HTML)

    def test_settings_nav_items_all_point_at_a_real_anchor(self) -> None:
        """左导航每一项都必须能落到一局面板；靠中文标题前缀匹配的老办法做不到这件事。"""
        gotos = re.findall(r'data-goto="([a-z-]+)"', PAGE_HTML)
        self.assertGreaterEqual(len(gotos), 15, "导航项少了：QQ/订阅群/钉钉/微信/学习通/推送/日历/手机/托管/外观/更新/备份/运行/自检/关于")
        self.assertEqual(len(gotos), len(set(gotos)), "导航锚点不许重复")
        # 静态面板写在 HTML 里；settings.js / pair.js 生成的面板在脚本里打锚点。
        static_anchors = set(re.findall(r'data-setting-anchor="([a-z-]+)"', PAGE_HTML))
        script_anchors = set(re.findall(r"dataset\.settingAnchor\s*=\s*'([a-z-]+)'", PAGE_SOURCE))
        available = static_anchors | script_anchors
        missing = [anchor for anchor in gotos if anchor not in available]
        self.assertEqual(missing, [], f"这些导航项没有对应面板：{missing}")
        # 导航不再靠标题前缀匹配，也不再 closest('section')（生成的面板有 <div> 容器）。
        self.assertIn("function panelEl(anchor)", PAGE_HTML)
        self.assertIn("return settings.querySelector(", PAGE_HTML)
        self.assertNotIn("title.textContent.trim().indexOf(label) === 0", PAGE_HTML)
        self.assertNotIn("found.closest('section')", PAGE_HTML)
        # page.html 里带真实内容、会被分组搬走的卡片必须标 data-setting-static：
        # settings.js 重渲染会清空 .settings-body 并删旧组，没标就被当旧卡片一起 remove
        # —— QQ 接入 / 订阅群整块消失，托管状态 / 关于 / 服务自检也一起没。
        # 其余锚点只是空挂载位（settings.js 往里建面板），不需要标记。
        self.assertEqual(
            set(re.findall(r'data-setting-anchor="([a-z-]+)"\s+data-setting-static', PAGE_HTML)),
            {'qq', 'groups', 'hosting-status', 'about', 'health'},
            '静态设置卡片没标 data-setting-static，切两次设置页就会消失')
        # QQ 的群和微信 / 钉钉 / 学习通的群不是一回事，界面上必须写明是 QQ 订阅群。
        self.assertIn('<h2 id="groups-title">QQ 订阅群</h2>', PAGE_HTML)
        self.assertIn('<button type="button" data-goto="groups">QQ 订阅群</button>', PAGE_HTML)

    def test_settings_search_filters_panels_and_nav(self) -> None:
        self.assertIn('id="settings-search"', PAGE_HTML)
        search_tag = re.search(r'<input[^>]*id="settings-search"[^>]*>', PAGE_HTML).group(0)
        self.assertIn('type="search"', search_tag)
        self.assertIn('role="searchbox"', search_tag)
        self.assertIn('aria-controls="tab-settings"', search_tag)
        self.assertIn('id="settings-search-count"', PAGE_HTML)
        self.assertIn("settings-search-empty", PAGE_SOURCE)
        # 过滤时「面板 + 对应导航项」一起收，清空后按原状恢复。
        self.assertIn("panel.hidden = !hit;", PAGE_HTML)
        self.assertIn("if (button) button.hidden = !hit;", PAGE_HTML)
        self.assertIn("panel.hidden = panel.dataset.wasHidden === '1'", PAGE_HTML)
        # Esc 清空、Enter 跳到第一个命中。
        self.assertIn("if (event.key === 'Escape')", PAGE_HTML)
        self.assertIn("if (event.key === 'Enter')", PAGE_HTML)

    def test_settings_cards_are_grouped_into_four_collapsible_sections(self) -> None:
        """19 张卡平铺一列要滚很久：按用途收成四组，默认只展开第一组。"""
        self.assertIn("var SETTINGS_GROUPS = [", PAGE_HTML)
        groups_block = PAGE_HTML.split("var SETTINGS_GROUPS = [", 1)[1].split("];", 1)[0]
        keys = re.findall(r"key: '([a-z]+)'", groups_block)
        self.assertEqual(keys, ["access", "notify", "system", "about"], "四组：接入与通道/提醒与订阅/系统与维护/关于")
        anchors = []
        for chunk in re.findall(r"anchors: \[([^\]]*)\]", groups_block):
            anchors.extend(re.findall(r"'([a-z-]+)'", chunk))
        self.assertEqual(len(anchors), len(set(anchors)), "同一张卡不许落进两个组")
        # 每个导航项都得有归属，否则点了导航会被滚到一张没分组的卡上。
        gotos = set(re.findall(r'data-goto="([a-z-]+)"', PAGE_HTML))
        ungrouped = sorted(a for a in gotos if a not in anchors)
        self.assertEqual(ungrouped, [], f"这些导航项没进任何组：{ungrouped}")
        # 「托管状态」是实时摘要、没有导航项，不进组。
        self.assertNotIn("'hosting-status'", groups_block)
        # 折叠是 details/summary，键盘可开可关；展开状态存内存，重渲染后不回弹。
        self.assertIn("group.className = 'settings-group';", PAGE_HTML)
        self.assertIn("group.open = groupOpen[spec.key] === undefined ? spec.open === true : groupOpen[spec.key];", PAGE_HTML)
        self.assertIn("#tab-settings .settings-group>summary{", PAGE_HTML)
        # 跳转前先摊开目标所在的组，否则滚到一个收起来的组上什么也看不到。
        self.assertIn("function openGroupFor(el) {", PAGE_HTML)
        self.assertIn("openGroupFor(el);", PAGE_HTML)
        # settings.js 每次重建面板都换一个 render 号，分组脚本据此丢掉上一轮的旧卡片；
        # 少了这句，重建后组里留着孤儿卡、页面里同时存在两份同锚点面板。
        self.assertIn("root.setAttribute('data-render', String(Date.now()) + '-' + String(++settingsRenderSeq));", PAGE_SOURCE)
        self.assertIn("var render = body.getAttribute('data-render') || '';", PAGE_HTML)

    def test_motion_switch_lives_only_in_appearance(self) -> None:
        # 快捷操作里不再有暂停动效按钮；设置里只有「外观」一处开关。
        self.assertNotIn('id="rail-motion"', PAGE_HTML)
        self.assertNotIn("railToggleMotion", PAGE_SOURCE)
        self.assertIn('id="pref-motion-enabled"', PAGE_SOURCE)
        # 动效 fieldset 必须挂在外观面板里：settings.js 里它拼在主题与强调色之间。
        # 只在外观面板的 HTML 片段里比先后：整页里「强调色」在别处也出现。
        appearance_html = PAGE_SOURCE.split("themeBox.innerHTML=", 1)[1][:6000]
        self.assertIn("pref-motion-enabled", appearance_html)
        self.assertIn("pref-accent", appearance_html)
        self.assertLess(
            appearance_html.index("pref-motion-enabled"),
            appearance_html.index("pref-accent"),
            "外观面板里动效开关排在强调色之前（与原型「主题 / 动态效果 / 强调色」一致）",
        )

    def test_push_channel_has_its_own_panel(self) -> None:
        # 推送通道原来塞在「托管设置」里，导航里那一项是死链；现在独立成面板。
        self.assertIn("dataset.settingAnchor = 'push'", PAGE_SOURCE)
        self.assertIn("'<h2 class=\"section-title\">推送通道</h2>' +", PAGE_SOURCE)
        # 推送相关的交互 id 一个都不能丢。
        for element_id in ("push-save", "push-test", "push-clear", "push-result", "push-settings"):
            self.assertIn(element_id, PAGE_SOURCE)

    def test_agent_hint_only_on_napcat_and_update_screens(self) -> None:
        # 用户口径：提示只放 NapCat 相关与更新功能相关的地方，同一屏只放一次；
        # 通用配置（平台总览）不放。用户拍板 C 方案后层级反转：
        # 第一行主句（13px/500），第二行说明（11px，含「项目地址」「什么是 agent？」两个入口）。
        block = '<div class="agent-hint" role="note">'
        lead_tag = '<p class="agent-hint-lead">'
        note_tag = '<p class="agent-hint-note">'
        entries = ('<a href="https://github.com/ExpertKT/notice-hub" target="_blank" '
                   'rel="noopener noreferrer">项目地址</a>')
        note = ('把这一段发给它，或者让它直接接手这台电脑 · ' + entries +
                ' · <button class="agent-hint-open" type="button" '
                'aria-haspopup="dialog">什么是 agent？</button>')

        def check(slice_, lead_text, what):
            self.assertEqual(slice_.count(block), 1, what + "要恰好一块提示")
            # 主句与说明必须是两个元素，且主句在前。
            self.assertEqual(slice_.count(lead_tag), 1, what + "要有独立的主句元素")
            self.assertEqual(slice_.count(note_tag), 1, what + "要有独立的说明元素")
            self.assertLess(slice_.index(lead_tag), slice_.index(note_tag))
            self.assertIn('</p>', slice_[slice_.index(lead_tag):slice_.index(note_tag)],
                          "主句和说明不能挤在同一个 <p> 里")
            self.assertIn(lead_tag + lead_text + '</p>', slice_)
            self.assertIn(note_tag + note + '</p>', slice_)

        # 1) 连接页 NapCat 安装块：引擎状态 #napcat-engine 与安装说明同容器，所以只放这一处。
        install_box = PAGE_HTML.split('id="install-box"', 1)[1].split('class="connect-actions"', 1)[0]
        check(install_box, "建议让 agent 来装", "连接页 NapCat 块")
        self.assertLess(install_box.index('id="napcat-engine"'), install_box.index(block))
        # 2) 首次配置向导第 1 步讲的是「一键接入装引擎」，属于 NapCat 接入。
        onboarding = PAGE_HTML.split('id="onboarding-title"', 1)[1].split('id="onboarding-error"', 1)[0]
        check(onboarding, "建议让 agent 来装", "向导第 1 步")
        self.assertLess(onboarding.index("点「一键接入」装好引擎"), onboarding.index(block))
        # 3) 设置 → 版本更新卡片。
        update_card = PAGE_SCRIPT.split("updateCard.innerHTML = ", 1)[1][:4000]
        check(update_card, "建议让 agent 来更新", "版本更新卡片")
        self.assertLess(update_card.index('id="update-checked"'), update_card.index(block))
        # 平台配置总览面板不放。
        wizard = PAGE_HTML.split('id="platform-setup-wizard"', 1)[1].split('id="announce-modal"', 1)[0]
        self.assertNotIn('class="agent-hint"', wizard, "通用平台配置面板不放 agent 提示")
        # 全页合计三块，主句文案统一为「建议让 agent 来装/更新」。
        self.assertEqual(PAGE_SOURCE.count(block), 3)
        self.assertEqual(PAGE_SOURCE.count('class="agent-hint-open"'), 3)
        self.assertEqual(PAGE_SOURCE.count("建议让 agent 来"), 3)

        # 层级是这次的重点：主句字号必须大于说明行，且主句更实、说明更淡。
        lead_css = PAGE_HTML.split('agent-hint-lead{', 1)[1].split('}', 1)[0]
        note_css = PAGE_HTML.split('agent-hint-note{', 1)[1].split('}', 1)[0]
        # 不写死具体档位：主句用哪一档就读哪一档，只要它比说明行大，且说明行不再是小字。
        lead_token = re.search(r"font-size:var\(--(fs-\d)\)", lead_css).group(1)
        note_token = re.search(r"font-size:var\(--(fs-\d)\)", note_css).group(1)
        lead_px = int(re.search(r"--%s:[^;]*?(\d+)px" % re.escape(lead_token), PAGE_HTML).group(1))
        note_px = int(re.search(r"--%s:[^;]*?(\d+)px" % re.escape(note_token), PAGE_HTML).group(1))
        self.assertGreater(lead_px, note_px, "主句字号必须大于说明行：%s vs %s" % (lead_token, note_token))
        self.assertGreaterEqual(note_px, 13, "说明行不许退回小字：%s = %spx" % (note_token, note_px))
        self.assertIn('color:var(--text)', lead_css)
        self.assertIn('color:var(--dim)', note_css)

        # 三处共用一个原生 dialog，文案要点出几个现成的 agent 工具。
        self.assertEqual(PAGE_HTML.count('id="agent-help-dialog"'), 1)
        dialog = PAGE_HTML.split('id="agent-help-dialog"', 1)[1].split('</dialog>', 1)[0]
        self.assertIn('agent 是什么', dialog)
        self.assertIn('Trae', dialog)
        self.assertIn('WorkBuddy', dialog)
        self.assertIn('Cursor', dialog)
        self.assertIn('Claude Code', dialog)
        self.assertIn('Codex', dialog)
        self.assertIn('知道了', dialog)
        self.assertIn('function setupAgentHelp()', PAGE_SCRIPT)
        self.assertIn("showModal()", PAGE_SCRIPT)

    def test_update_channel_switches_on_radio_change_without_save_button(self) -> None:
        # 用户口径：保存通道太麻烦，切单选即生效；失败必须把选中项退回去，不许显示成功却没保存。
        self.assertNotIn('id="update-channel-save"', PAGE_SCRIPT, "「保存通道」按钮已删除")
        self.assertIn("switchUpdateChannel(currentUpdateChannel)", PAGE_SCRIPT)
        self.assertIn("update_channel: next", PAGE_SCRIPT)
        self.assertIn("已切到正式版", PAGE_SCRIPT)
        self.assertIn("已切到测试版", PAGE_SCRIPT)
        self.assertIn("'切换失败：' + ((error && error.message) || error)", PAGE_SCRIPT)
        # 回退用的是切之前那个通道，而不是「不选中」。
        self.assertIn("selectUpdateChannel(previous)", PAGE_SCRIPT)

    def test_update_download_source_follows_real_host(self) -> None:
        # 审计第 9 条：下载地址可能来自 Gitee 镜像，别写死 GitHub。
        self.assertNotIn("下载地址来自 GitHub。", PAGE_SOURCE)
        self.assertIn("function updateSourceLabel(latest)", PAGE_SCRIPT)
        self.assertIn("来自 Gitee 镜像", PAGE_SCRIPT)
        self.assertIn("来自 GitHub", PAGE_SCRIPT)
        # 没有下载地址就不提来源。
        self.assertIn("(sourceLabel ? '，下载地址' + sourceLabel : '')", PAGE_SCRIPT)

class DashboardMotionTest(unittest.TestCase):
    @staticmethod
    def _current_stylesheet() -> str:
        return PAGE_HTML.rsplit("<style>", 1)[1].split("</style>", 1)[0]

    def test_dashboard_has_bounded_perspective_and_three_depth_levels(self) -> None:
        css = self._current_stylesheet()
        # 纸感是平的：没有透视容器，也没有 Z 轴位移。深度令牌还留着（旧规则仍引用它们），
        # 但取值恒为 0——「三层景深」被一层安静的纸面取代，卡片边界靠线和底色分层。
        self.assertNotIn("perspective:", css)
        self.assertNotIn("translateZ", css)
        defaults = css.split("@media (prefers-reduced-motion:reduce)", 1)[0]
        depths = {name: int(value) for name, value in re.findall(r"--depth-([a-z-]+):\s*(\d+)px", defaults)}
        self.assertEqual(depths, {"mid": 0, "top": 0, "content": 0, "focus": 0})
        self.assertIn("--depth-mid:0px;--depth-top:0px;--depth-content:0px;--depth-focus:0px", css)
        # 版心只用一套宽度：--measure 收口，不再 960 / 1760 两套数。
        # 版心只用一套宽度：--measure 收口。横向留白走 --gutter，分四档落在 4px 网格上
        # （原型是 clamp(16px,3.2vw,56px)，视觉等价，但不会算出 46.08px 这种小数）。
        self.assertIn("width:100%;max-width:var(--measure);margin-inline:auto;padding-inline:var(--gutter)", css)
        self.assertIn("--measure:1440px", css)
        self.assertIn("--gutter:var(--space-4)", css)
        self.assertIn("@media(min-width:1100px){:root{--gutter:var(--space-8)}}", css)
        self.assertIn("@media(min-width:1440px){:root{--gutter:var(--space-10)}}", css)
        self.assertNotIn("width:min(calc(100% - 32px),var(--measure))", css)
        self.assertNotIn("width:min(calc(100% - 32px),var(--measure))", css)
        self.assertIn("--measure:1440px", css)
        # 手机触控审计要求按钮至少 40px；分组行与日历格保留各自尺寸契约。
        self.assertRegex(css, r"\.btn,[^{]*\{[^}]*min-height:40px")
        self.assertRegex(css, r"\.group-row\{[^}]*min-height:34px")
        self.assertRegex(css, r"\.calendar-day\{[^}]*min-height:44px")
        self.assertRegex(css, r"details\.section>summary\{[^}]*min-height:0")
        self.assertNotRegex(css, r"summary\{[^}]*min-height:44px")
        self.assertNotRegex(css, r"\.login-choice label\{[^}]*min-height:44px")
        self.assertNotRegex(css, r"#groups label\{[^}]*min-height:44px")
        self.assertIn("button:focus-visible", css)
        self.assertIn("outline:2px solid var(--teal)!important;outline-offset:2px!important", css)
        self.assertNotIn("outline:3px solid", PAGE_SOURCE)
        self.assertNotIn("outline-offset:3px", PAGE_SOURCE)
        self.assertIn("min-width:0", css)
        self.assertIn("width:calc(100% - 2 * var(--gutter))", css)

    def test_background_flow_is_composited_and_pauses_with_motion_settings(self) -> None:
        css = self._current_stylesheet()
        # 背景从「两层绸缎柔光」换成「一层纸纹」：固定在视口上的内联 SVG 噪声（feTurbulence），
        # 不拦事件、透明度极低，而且不跑任何动画——所以关动效 / 暂停动效都不影响它。
        grain = _css_rule(css, "body::after{")
        self.assertIn('content:""', grain)
        self.assertIn("position:fixed;inset:0", grain)
        self.assertIn("pointer-events:none", grain)
        self.assertIn("feTurbulence", grain)
        self.assertIn('background-image:url("data:image/svg+xml', grain)
        self.assertEqual(
            [float(value) for value in re.findall(r"opacity:([0-9.]+)", grain)],
            [0.045],
            "纸纹只允许一层极低不透明度，叠第二层就会把纸底糊脏",
        )
        self.assertNotIn("animation", grain)
        self.assertNotIn("repeating-linear-gradient", grain)
        # 绸缎层的遗产必须清干净：body::before 不再产出，旧 keyframe / 位移 / will-change 都消失。
        self.assertIn("body::before{content:none}", css)
        self.assertNotIn("bg-silk-a", css)
        self.assertNotIn("bg-silk-b", css)
        self.assertNotIn("inset:-30%", css)
        self.assertNotIn("will-change", css)
        self.assertNotIn("translate3d", css)
        # 暂停动效的开关仍在：动画/过渡被整页关掉，纸纹因为本来就不动，不受影响。
        self.assertIn("html[data-motion=paused] *::before", css)
        self.assertIn("prefers-reduced-motion:reduce", css)
        reduced = _css_block(css, "@media (prefers-reduced-motion:reduce){")
        self.assertNotIn("body::after", reduced)
        self.assertNotIn("content:none", reduced)
        # 可见性契约：body 必须透明。body 一旦自己刷底色，固定在 ::after 的纸纹会被整块盖住。
        self.assertRegex(css, r"html\{[^}]*background:var\(--page\)")
        self.assertRegex(css, r"body\{[^}]*background:transparent[^}]*\}")

    def test_color_tokens_separate_surfaces_and_typography_scales(self) -> None:
        css = self._current_stylesheet()
        # 纸墨色板（纸感编辑风）：暖纸底 + 半透明墨线 + 鼠尾草强调。
        # 分层靠纸面明度差（--page < --paper < --paper-raise）和线的一深一浅两档，取值就此锁住。
        for token in (
            "--paper:#F4EFE3",
            "--paper-raise:#FBF8F0",
            "--paper-sink:#EDE5D2",
            "--sand:#D6C7A4",
            "--page:#EDE7D6",
            "--paper-alt:#F6F2E7",
            "--ink:#3A3A28",
            "--muted:#4E4B3A",
            "--dim:#5A5744",
            "--line:rgba(58,58,40,.16)",
            "--line-strong:rgba(58,58,40,.30)",
            "--teal:#5C6B44",
            "--teal-soft:rgba(123,139,94,.14)",
            "--red-soft:#F7E4DC",
            "--amber-soft:#F6ECD2",
        ):
            self.assertIn(token, css)
        self.assertNotIn("--page:#e8ebee", css)
        self.assertNotIn("--line:#b0b8bd", css)
        self.assertNotIn("--paper-alt:#f2f4f6", css)
        # 排版阶梯（对齐原型 --t-* 表）：概览主标题与页签抬头都是 28/34 档，
        # 分组标题与面板标题同级（衬线 20px = --fs-6）。手机端各降一档。
        self.assertRegex(css, r"\.summary h1\{[^}]*font-size:var\(--fs-8\)")
        self.assertRegex(css, r"\.section-title\{[^}]*font-size:var\(--fs-6\)")
        self.assertRegex(css, r"\.masthead h1\{[^}]*font-size:var\(--fs-9\)")
        self.assertIn(".summary h1{font-size:var(--fs-7)}", css)
        # 字阶取值就是原型那张表，逐档锁住（相邻比值不再当契约：HIG 阶梯靠语义分档）。
        # 档位倍数由 --font-scale 提供：默认 1 时就是原表原值，放大档整条阶梯等比跟动。
        self.assertIn("--font-scale:1;", css)
        self.assertIn("--fs-1:calc(9px * var(--font-scale));--fs-2:calc(11px * var(--font-scale));", css)
        # 12px 下限只对放大档生效：默认档必须是原型原值 9/11px，否则观感就变了。
        self.assertIn(
            "html[data-font-scale=large],html[data-font-scale=xlarge]{"
            "--fs-1:max(12px,calc(9px * var(--font-scale)));"
            "--fs-2:max(12px,calc(11px * var(--font-scale)))}",
            css,
        )
        self.assertIn("--fs-3:calc(13px * var(--font-scale));--fs-4:calc(15px * var(--font-scale));", css)
        self.assertIn("--fs-5:calc(16px * var(--font-scale));--fs-6:calc(20px * var(--font-scale));", css)
        self.assertIn("--fs-7:calc(22px * var(--font-scale));--fs-8:calc(28px * var(--font-scale));", css)
        self.assertIn("--fs-9:calc(34px * var(--font-scale));", css)
        # 三档倍数：默认 1 / 偏大 1.25 / 很大 1.5；间距与控件跟随同一个 --ui-scale。
        self.assertIn("html[data-font-scale=large]{--font-scale:1.25}", css)
        self.assertIn("html[data-font-scale=xlarge]{--font-scale:1.5}", css)
        self.assertIn("--ui-scale:var(--font-scale);", css)
        # 放大档同时放宽版心：字大了每行字数跟默认档持平，内容多占一点屏，
        # 不锁死在 1440 的固定占比；宽不过窗口时自然被 100% 收住，窄屏照旧撑满。
        self.assertIn("html[data-font-scale=large]{--measure:1800px}", css)
        self.assertIn("html[data-font-scale=xlarge]{--measure:2160px}", css)
        # 改字号后不许把视口带跑到别的设置块：先量字号卡相对视口的位置，重排后按差值补回滚动。
        self.assertIn("var anchorTop = fieldset.getBoundingClientRect().top;", PAGE_SOURCE)
        self.assertIn("var delta = fieldset.getBoundingClientRect().top - anchorTop;", PAGE_SOURCE)
        self.assertIn("window.scrollBy({top: delta, behavior: 'instant'})", PAGE_SOURCE)
        # 窄屏 + 放大档：长按钮折行，不靠硬撑一行把页面顶出横向滚动。
        self.assertIn(".btn{white-space:normal}", _css_block(css, "@media(max-width:760px){"))
        # 窄屏：服务自检一律收成一列，别把「标签 | 值」压成竖排。
        # 早先只在放大档收（`html:not([data-font-scale=default])` 前缀），390 实测默认档两列也挤：
        # 「公网日历 / 消息推送 / 推送渠道」这类四字标签 + 值 + 按钮放不下，中文被压成一列一个字。
        # 现在窄屏不分字号全收一列；值放不下时整行折行，别把四字标签拆开。
        self.assertIn(
            "#tab-settings .health-list{grid-template-columns:minmax(0,1fr)}",
            _css_block(css, "@media(max-width:760px){"),
        )
        self.assertIn(
            "#tab-settings .health-row{flex-wrap:wrap}",
            _css_block(css, "@media(max-width:760px){"),
        )
        # 字族：标题走宋体栈、正文走系统栈，都由令牌给出。
        self.assertIn("font-family:var(--font-serif)", css)
        self.assertIn("font-family:var(--font-sans)", css)
        self.assertIn(".summary{\n  /* 卡片自己缩进一层", css)
        self.assertIn(".section-title:before{content:none}", css)
        self.assertIn(".section-title:before{content:none}", css)
        # 悬停微交互：纸面是平的，按钮不再「抬起 + 阴影」，只换底色和描边（两套关动效规则照旧兜底）。
        self.assertIn(
            ".btn:not(:disabled):hover,.correct-btn:not(:disabled):hover,.btn-small:not(:disabled):hover{\n"
            "  background:var(--teal-soft);border-color:var(--teal);transform:none;box-shadow:none;",
            css,
        )
        self.assertIn("*,*::before,*::after{animation:none!important;transition:none!important}", css)
        self.assertIn(
            "html[data-motion=paused] *,html[data-motion=paused] *::before,html[data-motion=paused] *::after{",
            css,
        )


class DashboardThemeTest(unittest.TestCase):
    """暗色主题：选择器要对、取值要够对比度、首帧不能闪、设置页仅提供两态。"""

    @staticmethod
    def _current_stylesheet() -> str:
        return PAGE_HTML.rsplit("<style>", 1)[1].split("</style>", 1)[0]

    @staticmethod
    def _rule(css: str, selector: str) -> str:
        match = re.search(r"(?m)^" + re.escape(selector) + r"([^}]*)\}", css)
        assert match is not None, selector
        return match.group(1)

    def _tag(self, node_id: str) -> str:
        """按 id 取出开始标签，标签内部的属性就能单独断言，不怕加类名或换属性顺序。"""
        match = re.search(r'<[a-zA-Z][^>]*\bid="%s"[^>]*>' % re.escape(node_id), PAGE_HTML)
        self.assertIsNotNone(match, 'PAGE_HTML 里找不到 id="%s" 的标签' % node_id)
        return match.group(0)

    @staticmethod
    def _contrast(foreground: str, background: str) -> float:
        """WCAG 相对亮度公式；注释里写的比值就是这么算出来的，测试替它们背书。"""

        def channel(value: int) -> float:
            ratio = value / 255.0
            return ratio / 12.92 if ratio <= 0.03928 else ((ratio + 0.055) / 1.055) ** 2.4

        def luminance(color: str) -> float:
            red, green, blue = (int(color[i:i + 2], 16) for i in (1, 3, 5))
            return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)

        light, dark = sorted((luminance(foreground), luminance(background)), reverse=True)
        return (light + 0.05) / (dark + 0.05)

    def test_theme_overrides_are_explicit_and_ignore_system_preference(self) -> None:
        css = self._current_stylesheet()
        # 暗色有显式覆盖块；亮色就是 :root 的默认值（纸感本身就是亮色底），不再写第二份。
        self.assertRegex(css, r"(?m)^html\[data-theme=dark\]\{")
        self.assertRegex(css, r"(?m)^:root\{[^}]*color-scheme:light")
        self.assertNotIn("html[data-theme=light]{", css)
        self.assertNotIn("@media(prefers-color-scheme:dark)", css)
        self.assertNotIn("html[data-theme=dark] :root", css)
        self.assertNotIn("html[data-theme=light] :root", css)
        options = re.findall(r'name="pref-theme" type="radio" value="([^"]+)"', PAGE_SOURCE)
        self.assertEqual(options, ["auto", "light", "dark"])
        # 默认跟随系统：首屏脚本必须读存储 + 系统偏好，二者都参与决定首帧。
        self.assertIn("localStorage.getItem('nh_theme')", PAGE_SOURCE)
        self.assertIn("window.matchMedia('(prefers-color-scheme:dark)')", PAGE_SOURCE)
        self.assertIn("t==='dark'||(auto&&window.matchMedia", PAGE_SOURCE)

    def test_history_cancel_control_tracks_cancelled_terminal_state(self) -> None:
        self.assertIn('<button id="history-cancel" class="btn" type="button" hidden>取消回溯</button>', PAGE_SOURCE)
        self.assertIn("/api/history/fetch/cancel", PAGE_SOURCE)
        self.assertIn("status.stage === 'cancelled'", PAGE_SOURCE)
        self.assertIn("已取消", PAGE_SOURCE)
        self.assertIn("if (status.running)", PAGE_SOURCE)

    def test_dark_mode_preserves_dark_native_controls_and_readable_surfaces(self) -> None:
        css = self._current_stylesheet()
        self.assertNotRegex(css, r"html\{[^}]*color-scheme:light")
        self.assertIn("html[data-theme=dark]{", css)
        self.assertIn("color-scheme:dark", self._rule(css, "html[data-theme=dark]{"))
        self.assertNotIn("html:not([data-theme=light])", css)
        self.assertNotIn("html:not([data-theme])", css)
        self.assertEqual(css.count("{"), css.count("}"))
        dark = css[css.index("/* ── 18. 暗色微调"):]
        dark = re.sub(r"/\*.*?\*/", "", dark, flags=re.S)
        # 暗色只补这几处：面统一 --paper-raise、已完成的行 / 有事项的格用 --paper-alt、
        # 输入控件 --paper-alt + --line、警告与错误文字换成暗色板上的 warn/bad。
        # 其余文字颜色不再逐条覆盖：对比度由暗色令牌（--muted/--dim/--faint）一次给足。
        for selector, rule in (
            ("html[data-theme=dark] .task", r"background:var\(--paper-raise\)"),
            ("html[data-theme=dark] .summary", r"background:var\(--paper-raise\)"),
            ("html[data-theme=dark] .journey-card", r"background:var\(--paper-raise\)"),
            ("html[data-theme=dark] .task.done", r"background:var\(--paper-alt\)"),
            ("html[data-theme=dark] .calendar-day[data-has-events=true]", r"background:var\(--paper-alt\)"),
            ("html[data-theme=dark] .calendar-day.today", r"background:var\(--teal-soft\)"),
            ("html[data-theme=dark] input", r"background:var\(--paper-alt\);border-color:var\(--line\)"),
            ("html[data-theme=dark] .chaoxing-expired-banner", r"color:var\(--warn\)"),
            ("html[data-theme=dark] .action-feedback[data-state=error]", r"color:var\(--ink\)"),
        ):
            match = re.search(re.escape(selector) + r"[^{}]*\{([^}]*)\}", dark)
            self.assertIsNotNone(match, selector)
            self.assertRegex(match.group(1), rule)
        warning_rule = re.search(r"html\[data-theme=dark\] #status,html\[data-theme=dark\] \.hosting-warning,html\[data-theme=dark\] \.chaoxing-expired-banner\{([^}]*)\}", dark)
        self.assertIsNotNone(warning_rule, "html[data-theme=dark] #status,….chaoxing-expired-banner")
        self.assertRegex(warning_rule.group(1), r"color:var\(--warn\)")
        # 主按钮在暗色下的悬停仍压回墨色底（纸面上的反差手段），文字回落纸色。
        hover_rule = re.search(r"html\[data-theme=dark\] \.btn\.primary:hover:not\(:disabled\)\{([^}]*)\}", dark)
        self.assertIsNotNone(hover_rule, "html[data-theme=dark] .btn.primary:hover")
        self.assertRegex(hover_rule.group(1), r"background:var\(--ink\);color:var\(--paper\)")
        self.assertNotRegex(css, r"\.correct-select[^}]*color-scheme:light")
        self.assertNotRegex(css, r"\.correct-select[^}]*color-scheme:light")

    def test_journey_is_a_first_run_dialog_not_a_persistent_card(self) -> None:
        """「开始使用」改成首次打开才出现的弹窗：遮罩 + 居中卡片，不再常驻页面挤掉首屏。"""
        css = self._current_stylesheet()
        overlay = _css_rule(css, ".journey-overlay{")
        self.assertIn("position:fixed;inset:0;z-index:var(--z-overlay)", overlay)
        self.assertIn("display:flex;align-items:center;justify-content:center", overlay)
        self.assertIn(".journey-overlay[hidden]{display:none}", css)
        # 弹窗里仍然没有「跳过全部」这种假出口，只有「别再提醒」；也不许再有页面内的常驻容器。
        self.assertIn('<button id="journey-dismiss" class="btn" type="button">别再提醒</button>', PAGE_SOURCE)
        self.assertNotIn("暂时收起", PAGE_SOURCE)
        self.assertNotIn('<section id="journey" class="journey"', PAGE_SOURCE)

    def test_mobile_journey_stacks_and_orders_its_content(self) -> None:
        css = self._current_stylesheet()
        # 桌面两栏（步骤 / 下一步），窄屏收成一栏；顺序靠 DOM，不再用 order 改排。
        self.assertIn(".journey-card{\n  display:grid;grid-template-columns:minmax(0,1fr) minmax(0,300px);gap:var(--space-4);", css)
        # 卡片宽度自守（min(760px,100%)），高度让出视口，内容超出自己滚。
        self.assertIn("width:min(760px,100%);max-height:86vh;overflow:auto;padding:var(--space-5);", css)
        narrow = _css_block(css, "@media(max-width:760px){")
        self.assertIn(".journey-card{grid-template-columns:minmax(0,1fr)}", narrow)
        # 全站只有一条 760 窄屏规则：再开一条会让 _css_block 只看到其中一条，
        # 也会让「谁在缩」这件事散成两处，日后没人能一次看全。
        self.assertEqual(css.count("@media(max-width:760px){"), 1)
        self.assertIn(".journey-side{display:flex;flex-direction:column;justify-content:center;min-width:0}", css)
        self.assertIn(".journey-steps{display:grid;gap:var(--space-2);margin:0;padding:0;list-style:none}", css)
        self.assertIn(".journey-actions{display:flex;flex-wrap:wrap;gap:var(--space-2);margin-top:var(--space-3)}", css)
        self.assertIn(".journey-next{margin:0;color:var(--dim);font-size:var(--fs-3)}", css)

    def test_dark_native_checkboxes_and_radios_follow_theme(self) -> None:
        css = self._current_stylesheet()
        # 原生控件跟着 color-scheme 走：亮色写在 :root，暗色写在 html[data-theme=dark]，不再逐控件打补丁。
        self.assertIn("color-scheme:light", self._rule(css, ":root{"))
        self.assertIn("color-scheme:dark", self._rule(css, "html[data-theme=dark]{"))
        # radio 保持原生（accent-color）；checkbox 全部换成胶囊开关（纯 CSS，DOM/JS 不变）。
        self.assertIn("input[type=radio]{width:16px;height:16px;accent-color:var(--teal)}", css)
        switch_rule = _css_rule(css, "input[type=checkbox]{")
        self.assertIn("appearance:none", switch_rule)
        # 尺寸钉死（.field>input 的 min-width:180px 曾把 5 个开关拉成 180×28）
        for dim in ("width:42px", "min-width:42px", "max-width:42px",
                    "height:24px", "min-height:24px", "max-height:24px"):
            self.assertIn(dim, switch_rule)
        self.assertIn("border-radius:var(--r-full)", switch_rule)
        self.assertIn("background-size:16px 16px", switch_rule)
        checked_rule = _css_rule(css, "input[type=checkbox]:checked{")
        self.assertIn("background-color:var(--switch-track-on)", checked_rule)
        self.assertIn("background-position:calc(100% - 3px) center", checked_rule)
        self.assertIn("background-image:radial-gradient(circle at center,var(--switch-knob-on) 0 8px,transparent 8.5px)", checked_rule)
        # 表单行里的开关不参与输入框定宽
        self.assertIn(".field>input[type=checkbox]{width:42px;min-width:42px;max-width:42px;flex:none;margin-left:auto}", css)
        # 配色走变量：深浅两套只换四个值，不重写 background-*（重写会被简写规则重置）。
        self.assertIn("--switch-track-off:#E3D9C2", css)
        self.assertIn("--switch-knob-off:#FFFFFF", css)
        self.assertIn("--switch-track-off:#3E3E31", css)
        self.assertIn("--switch-knob-on:#1F1F18", css)
        # 根因守门：深色通用 input 简写必须排除 checkbox，否则 background 简写会把
        # background-position 重置成 0% 0%，未选中态的圆钮被甩到左上角。
        self.assertIn("html[data-theme=dark] input:not([type=checkbox]),html[data-theme=dark] select,html[data-theme=dark] textarea{background:var(--paper-alt);border-color:var(--line)}", css)
        self.assertNotIn("html[data-theme=dark] input,html[data-theme=dark] select", css)
        # 深色不再单独重写 checkbox 的 background-*：只换上面那四个变量值即可。
        self.assertIn("input[type=checkbox]:disabled{opacity:.42;cursor:not-allowed}", css)
        self.assertNotIn("html[data-theme=light]", css)

    def test_dark_tokens_reach_contrast_thresholds(self) -> None:
        css = self._current_stylesheet()
        dark = self._rule(css, "html[data-theme=dark]{")
        for token in (
            "--page:#23231B",
            "--paper:#23231B",
            "--paper-raise:#2B2B21",
            "--paper-alt:#242419",
            "--ink:#EDE6D2",
            "--muted:#C3BCA4",
            "--line:rgba(237,230,210,.14)",
            "--line-strong:rgba(237,230,210,.28)",
            # 强调色两级 + 状态三色：取值与原型 --accent / --accent-strong / --state-* 一致
            "--accent:#97A878",
            "--accent-strong:#A9BA8A",
            "--teal:#A9BA8A",
            "--teal-soft:rgba(151,168,120,.16)",
            "--red:#C97A62",
            "--red-soft:#3A2620",
            "--amber:#C9A55C",
            "--amber-soft:#3A3320",
        ):
            self.assertIn(token, dark, token)
        # 旧的冷蓝深色板必须彻底消失，否则暗色下会和新纸感打架。
        self.assertNotIn("--page:#0a0c12", css)
        self.assertNotIn("--line:#303747", css)
        # 分隔线改成半透明墨的两档阶梯（细线 14% / 强调线 28%）：强调线必须比细线重，
        # 纸感里不再用「实色线卡 3:1」那种硬描边。
        def line_alpha(token: str) -> float:
            match = re.search(re.escape(token) + r":rgba\([^)]*?,([0-9.]+)\)", dark)
            self.assertIsNotNone(match, token)
            return float(match.group(1))

        self.assertLess(line_alpha("--line"), line_alpha("--line-strong"))
        # 实算背书：正文 / 次要文字 ≥4.5:1（含柔光抬亮后的最坏底色；
        # --teal-soft 叠在 #23231B 上合成为 #38382A，按这个合成值算）。
        for surface in ("#23231B", "#2B2B21", "#242419"):
            self.assertGreaterEqual(self._contrast("#C3BCA4", surface), 4.5, surface)
            self.assertGreaterEqual(self._contrast("#EDE6D2", surface), 4.5, surface)
        for chip in ("#38382A", "#3A2620", "#3A3320"):
            self.assertGreaterEqual(self._contrast("#C3BCA4", chip), 4.5, chip)

    def test_dark_glow_alpha_stays_below_the_ceiling(self) -> None:
        css = self._current_stylesheet()
        # 暗色不再叠柔光层：纸纹只有一条，也不随主题变浓——所以这里锁的是「暗色没有自己的背景层」。
        self.assertNotIn("html[data-theme=dark] body::after", css)
        self.assertNotIn("html[data-theme=dark] body:before", css)
        self.assertNotIn("body:before{", css)
        grain = _css_rule(css, "body::after{")
        self.assertNotIn("repeating-linear-gradient", grain)
        self.assertEqual(
            [float(value) for value in re.findall(r"opacity:([0-9.]+)", grain)],
            [0.045],
            "纸纹不透明度就是 .045，主题不许把它调浓",
        )
        dark = self._rule(css, "html[data-theme=dark]{")
        self.assertNotIn("body::after", dark)
        self.assertNotIn("rgba(151,168,120,.11)", css)
        self.assertNotIn("rgba(200,178,110,.08)", css)

    def test_theme_boots_before_first_paint_and_defaults_to_light(self) -> None:
        head = PAGE_HTML.split("<head>", 1)[1].split("</head>", 1)[0]
        first_script = head.split("<script>", 1)[1].split("</script>", 1)[0]
        self.assertIn("nh_theme", first_script)
        self.assertIn("document.documentElement.dataset.theme", first_script)
        # 默认跟随系统：没存过（或选了 auto）就跟系统深浅，存过 light/dark 才固定。
        self.assertIn("var dark=t==='dark'||(auto&&window.matchMedia", first_script)
        self.assertIn("document.documentElement.dataset.theme=dark?'dark':'light'", first_script)
        self.assertIn("prefers-color-scheme:dark", first_script)
        self.assertIn('meta name="theme-color" content="#EDE7D6"', PAGE_HTML.split("</head>", 1)[0])
        self.assertLess(head.index("nh_theme"), head.index("<style>"))
        self.assertIn('id="pref-theme"', PAGE_SOURCE)
        for value in ("light", "dark"):
            self.assertIn('value="' + value + '"', PAGE_SOURCE)
        self.assertNotIn('value="system"', PAGE_SOURCE)
        self.assertNotIn("delete document.documentElement.dataset.theme", PAGE_SOURCE)
        self.assertNotIn("localStorage.removeItem('nh_theme')", PAGE_SOURCE)
        self.assertIn("function initThemePreference()", PAGE_SOURCE)

    @staticmethod
    def _all_css() -> str:
        """页面有两个 <style> 块；只看最后一块会漏掉第一块里的规则。"""
        return "\n".join(
            block.split("</style>", 1)[0]
            for block in PAGE_HTML.split("<style>")[1:]
        )

    def test_theme_selector_is_a_previewed_radio_group_not_a_select(self) -> None:
        """主题设置提供可辨识的浅色/深色预览单选卡片。"""
        css = self._all_css()
        self.assertIn('<fieldset id="pref-theme" class="theme-choice-list">', PAGE_SOURCE)
        self.assertNotIn('id="pref-theme" aria-label="选择配色主题"', PAGE_SOURCE)
        self.assertNotIn('<select id="pref-theme"', PAGE_SOURCE)
        for value in ("auto", "light", "dark"):
            self.assertIn(
                '<input name="pref-theme" type="radio" value="' + value + '"', PAGE_SOURCE
            )
        self.assertNotIn('value="system"', PAGE_SOURCE)
        # 颜色预览方块已删（用户要求）：选项卡只有文字。
        self.assertNotIn("theme-preview", PAGE_SOURCE)
        self.assertNotIn(".theme-preview", css)
        # 深色下 teal-soft 底会把 --ink 衬到看不清：选中底改 paper-sink，文字色锁 --ink。
        self.assertIn(".theme-option.selected,.theme-option:has(input:checked){border-color:var(--teal);background:var(--paper-sink)}", css)
        self.assertIn(".theme-option .theme-option-body strong,.theme-option .theme-option-body small{color:var(--ink)}", css)
        # 选中态必须由 JS 打在选择卡片上，不能只靠原生 radio。
        self.assertIn("card.classList.toggle('selected', input.checked)", PAGE_SOURCE)
        self.assertIn(".theme-option.selected,", css)
        # 卡片用 auto-fit 自适应：窄屏自然收成一列，不用再补一条窄屏规则。
        self.assertIn(".theme-choice-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr))", css)

    def test_field_containers_are_block_level(self) -> None:
        """class="field" 之前在样式表里根本不存在，导致链接/输入框/按钮挤在一行互相压边。"""
        css = self._all_css()
        field_rule = re.search(r"(?m)^\.field\{([^}]*)\}", css)
        self.assertIsNotNone(field_rule, "missing .field rule")
        self.assertIn("display:flex", field_rule.group(1))
        self.assertIn("justify-content:space-between", field_rule.group(1))
        # 行式排版：标签在左、控件在右，虚线底分隔。输入框定宽不铺满整行，但也不许被压成一小截。
        input_rule = re.search(r"(?m)^\.field>input,\.field>select,\.field>textarea,\n[^{]*\{([^}]*)\}", css)
        self.assertIsNotNone(input_rule, "missing .field input rule")
        self.assertIn("min-height:28px", input_rule.group(1))
        self.assertIn(".field>input,.field>select{width:auto;min-width:180px;flex:0 1 300px}", css)
        self.assertIn("border-bottom:1px dashed var(--line)", field_rule.group(1))

    def test_backup_settings_button_uses_authenticated_download(self) -> None:
        self.assertIn("backupCard.id='backup-card'", PAGE_SOURCE)
        self.assertIn('id="backup-download"', PAGE_SOURCE)
        self.assertIn("/api/backup", PAGE_SOURCE)
        self.assertIn("'X-Token':token", PAGE_SOURCE)
        self.assertIn("notice-hub-backup-", PAGE_SOURCE)
        self.assertIn("不包含令牌、配置文件或日志", PAGE_SOURCE)

    def test_chaoxing_manual_cookie_is_advanced_and_saves_then_validates(self) -> None:
        self.assertIn('<details id="cx-manual-option">', PAGE_SOURCE)
        self.assertNotIn('<details id="cx-manual-option" open', PAGE_SOURCE)
        self.assertIn('id="cx-manual-cred"', PAGE_SOURCE)
        self.assertIn('id="cx-manual-cred-save"', PAGE_SOURCE)
        self.assertIn('保存并校验', PAGE_SOURCE)
        self.assertNotIn('id="cx-cookie"', PAGE_SOURCE)
        self.assertNotIn('cx-clear', PAGE_SOURCE)
        self.assertIn('首选浏览器自动读取；失败时用托盘右键「读取学习通登录凭证」；仍失败可展开「高级：手动粘贴登录凭证」', PAGE_SOURCE)
        handler = PAGE_SOURCE.split("var manualCookieButton=document.getElementById('cx-manual-cred-save');", 1)[1].split("var dtBox=el(", 1)[0]
        self.assertIn("api('/api/settings'", handler)
        self.assertIn("JSON.stringify({chaoxing_cookie:cookie})", handler)
        self.assertIn("api('/api/chaoxing/cookie/validate'", handler)
        self.assertIn("validation.error||'登录凭证校验失败'", handler)
        self.assertIn("replace(/\\r/g,'').replace(/\\n/g,'')", handler)
        self.assertIn("replace(/^\\s*Cookie:\\s*/i,'').trim()", handler)
    def test_llm_state_copy_distinguishes_off_from_missing_key(self) -> None:
        """用户报 AI 摘要默认落本地模型：配置好了却还说「没有用 AI」会让人以为坏了。

        provider='off' 才是真的不用 AI；选了服务商只差钥匙时要指路，别只报「没在用」。
        """
        self.assertIn("if(state.provider==='off'||!state.enabled)", PAGE_SOURCE)
        self.assertNotIn("var item=llmProvider(state.provider)||{};if(!state.enabled){", PAGE_SOURCE)
        self.assertIn("想让它替你读消息、写待办，就在上面选一家", PAGE_SOURCE)
        self.assertIn("还差最后一步——钥匙", PAGE_SOURCE)
        # 不用钥匙的服务商（本机模型）不该显示「已配置：***」的钥匙框。
        self.assertIn("if(!item.needs_key){keyInput.placeholder='这家不用钥匙';", PAGE_SOURCE)
        self.assertIn("keyRow.hidden=true", PAGE_SOURCE)

    def test_napcat_status_text_does_not_claim_the_api_is_up(self) -> None:
        """引擎=程序文件已就位，接口可用=能连上 3001；两者混为一谈就是「没运行」旁边躺着「已运行」。"""
        self.assertIn("连不上 NapCat 的接口（127.0.0.1:3001）", PAGE_SOURCE)
        self.assertIn("上面「NapCat 引擎」只表示程序文件已就位，不代表接口已经可用", PAGE_SOURCE)
        self.assertIn("NapCat 进程已经在运行，正在确认接口和登录状态…", PAGE_SOURCE)
        # 引擎状态文案集中在 applyQQConnectState 的状态表：装好未启动 / 在跑等登录，都不说接口已通。
        self.assertIn("引擎已安装，还没启动。", PAGE_SOURCE)
        self.assertIn("引擎已在运行，等你登录。", PAGE_SOURCE)
        # 旧文案把「进程在跑」当成「登录状态可用」，必须消失。
        self.assertNotIn("QQ 未登录：NapCat 在运行，但还没有可用的登录状态", PAGE_SOURCE)

    def test_mobile_calendar_starts_collapsed_with_a_toggle_button(self) -> None:
        css = self._current_stylesheet()
        self.assertIn('id="calendar-toggle"', PAGE_SOURCE)
        self.assertIn("function setCollapsed(collapsed)", PAGE_SOURCE)
        self.assertIn("window.matchMedia('(max-width:480px)').matches", PAGE_SOURCE)
        self.assertIn("toggle.textContent=collapsed?'查看月历':'收起月历'", PAGE_SOURCE)
        # 桌面默认不显示这个按钮；手机（≤480px）才显示，并且折叠时只藏面板内部。
        self.assertIn(".month-controls .calendar-toggle{display:none}", css)
        # 折叠按钮在新版面里常驻隐藏：日历格已收窄到 44px（手机 38px），不再需要折叠；
        # JS 的 setCollapsed / calendar-collapsed 逻辑保留，样式层只锁「按钮不出现」。
        self.assertIn(".month-controls .calendar-toggle{display:none}", css)
        narrow = _css_block(css, "@media(max-width:760px){")
        self.assertIn(".calendar-day{min-height:38px}", narrow)
        self.assertRegex(css, r"\.calendar-day\{[^}]*min-height:44px;padding-top:4px")
        # 网格 2px 间隙（原型 .cal-grid gap:2px）；月份翻页走 28px 的 .mini-btn，另有「今天」按钮
        self.assertIn(".weekday-row,#calendar{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:2px}", css)
        self.assertIn(".month-controls{display:flex;align-items:center;gap:var(--space-2)}", css)
        self.assertIn('id="cal-today" class="btn tinted sm"', PAGE_SOURCE)
        self.assertIn(".mini-btn{", css)
        self.assertNotIn("#calendar-panel.calendar-collapsed", css)
        # 按钮必须在 #calendar-panel 里面：面板会被 taskSurface.appendChild(calendarPanel) 整体搬进「待办」
        # 标签页，按钮放在外面就会留在原地、和面板分家。
        self.assertIn("taskSurface.appendChild(calendarPanel)", PAGE_SOURCE)
        panel = PAGE_HTML.split('id="calendar-panel"', 1)[1].split("</section>", 1)[0]
        self.assertIn('id="calendar-toggle"', panel)

    def test_sync_address_alternates_are_rendered_and_switchable(self) -> None:
        self.assertIn('id="sync-alts" class="sync-alts" hidden', PAGE_SOURCE)
        # 备选地址按钮如今直接用 .btn.ghost 的样式（sync-alt 不再有专属规则）。
        self.assertIn(".btn.ghost{border-color:transparent;color:var(--dim);background:transparent}", self._current_stylesheet())
        self.assertIn("function renderAddressChoices(calendars, current)", PAGE_SOURCE)
        self.assertIn("renderAddressChoices(data.calendars, calendar.url)", PAGE_SOURCE)
        self.assertIn("button.className = 'btn ghost sync-alt'", PAGE_SOURCE)
        self.assertIn("input.dataset.testScheme = new URL(item.url).protocol", PAGE_SOURCE)
        self.assertIn("setupApi('/api/sync/test?url=' + encodeURIComponent(requestUrl))", PAGE_SOURCE)
        self.assertNotIn("fetch(requestUrl, {method: 'GET', cache: 'no-store'})", PAGE_SOURCE)

    def test_reduced_motion_removes_depth_and_active_transform(self) -> None:
        css = self._current_stylesheet()
        # 只取这一段媒体查询的花括号内部：v2 设计层是追加在文件末尾的，
        # 直接 split 会把后面所有规则一起吞进来，先出现的同名选择器会被后者覆盖。
        reduced = _css_block(css, "@media (prefers-reduced-motion:reduce){")
        rules: dict[str, str] = {}
        for selector, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", reduced):
            for name in selector.split(","):
                rules[name.strip()] = declarations
        depth_values = dict(re.findall(r"(--depth-[a-z-]+):\s*(\d+)px", rules.get(":root", "")))
        self.assertEqual(depth_values, {"--depth-mid": "0", "--depth-top": "0"})
        # 一条兜底规则关掉所有动画与过渡（含伪元素）；不再逐个选择器写 transform:none。
        for selector in ("*", "*::before", "*::after"):
            self.assertIn(selector, rules, "关动效要覆盖到伪元素")
            declarations = dict(re.findall(r"([a-z-]+):([^;]+)", rules.get(selector, "")))
            self.assertEqual(declarations.get("animation"), "none!important", selector)
            self.assertEqual(declarations.get("transition"), "none!important", selector)
        self.assertIn("scroll-behavior:auto", reduced)
        self.assertNotIn("transform:translate", reduced)
        self.assertNotIn("transition-duration", reduced)

    def test_interactive_feedback_and_backend_urgency_are_wired(self) -> None:
        css = self._current_stylesheet()
        for selector in (
            ".btn:not(:disabled):hover", ".brand:hover", ".tabs button:hover",
            ".preference:hover", ".history-groups label:hover,.group-row:hover",
            ".settings-subnav button:hover", "input[type=checkbox]:hover",
            ".accent-swatch:hover", ".theme-option:hover", ".calendar-day:hover",
        ):
            self.assertIn(selector, css)
        self.assertIn("transition:background 200ms var(--ease-in)", css)
        self.assertIn("button:disabled,input:disabled,select:disabled{cursor:not-allowed;opacity:.5}", css)
        self.assertIn("outline:2px solid var(--teal)!important;outline-offset:2px!important", css)
        self.assertIn("html[data-motion=paused]", css)
        node_start = PAGE_SOURCE.index("function taskNode(task)")
        node_end = PAGE_SOURCE.index("function section(title", node_start)
        task_node = PAGE_SOURCE[node_start:node_end]
        self.assertIn("task.effective_urgent", task_node)
        self.assertIn("aria-pressed", task_node)
        self.assertIn("task.urgent_override", task_node)
        self.assertIn("sendUrgentOverride", PAGE_SOURCE)
        self.assertIn("/api/tasks/urgent", PAGE_SOURCE)
        self.assertIn("/api/sync/info", PAGE_SOURCE)
        self.assertIn("/api/sync/qr.png?text=", PAGE_SOURCE)
        self.assertIn("webcal:", PAGE_SOURCE)

    def test_task_hitboxes_stay_fixed_across_hover_focus_and_active_states(self) -> None:
        css = self._current_stylesheet()
        task_states = re.search(
            r"\.task:not\(\.done\):not\(\[aria-busy=true\]\):not\(\.completing\):hover,\s*"
            r"\.task:not\(\.done\):not\(\[aria-busy=true\]\):not\(\.completing\)\.is-focused,\s*"
            r"\.task:not\(\.done\):not\(\[aria-busy=true\]\):not\(\.completing\):focus-within\{([^}]*)\}",
            css,
        )
        self.assertIsNotNone(task_states)
        # 悬停/聚焦只抬 2px + 换边框色 + 加阴影，不改尺寸：命中区不随状态伸缩。
        self.assertIn("transform:translateY(-2px)", task_states.group(1))
        self.assertIn("border-color:var(--sand)", task_states.group(1))
        self.assertNotIn("scale(", task_states.group(1))
        # 横移会蹭到右边那张卡（原来的 translateX(2px) 就是这么坏的），只许上下抬。
        self.assertNotIn("translateX", task_states.group(1))
        for layout in ("width:", "height:", "padding:", "margin:"):
            self.assertNotIn(layout, task_states.group(1))
        self.assertIn("box-shadow:0 6px 16px rgba(10,20,18,.10)", task_states.group(1))
        self.assertIn("z-index:1", task_states.group(1))
        self.assertNotIn(".task.is-focused,.task:focus-within{transform:none}", css)
        # 复选框按下时只做视觉缩放，命中区由固定的 20px 方框兜住，不随缩放变小。
        self.assertIn(".task .check:active{transform:scale(.9)}", css)
        self.assertRegex(css, r"\.task \.check\{[^}]*width:20px;height:20px;min-width:20px;min-height:20px")
        self.assertNotIn("background:#d2e9e2", css)
        self.assertNotIn("box-shadow:inset 0 0 0 2px", css)

    def test_calendar_follows_today_on_mobile_and_uses_a_wide_desktop_column(self) -> None:
        css = self._current_stylesheet()
        render_start = PAGE_SOURCE.index("function render(data)")
        render_end = PAGE_SOURCE.index("function syncTasks()", render_start)
        renderer = PAGE_SOURCE[render_start:render_end]
        # 分组列表挂在概览卡的时间轴上（renderTimeline），详情页只留「已过期 / 已完成 / 月历」三块。
        # 旧口径是 render() 里拼一个 var blocks 再统一 append；e444d0a 把分组搬进详情页之后
        # 那几块没人 append 了（死变量），所以这里锁住「详情页三块仍在、死块不再回来」。
        self.assertIn("if (calendarPanel && anchor && anchor.after) anchor.after(calendarPanel);", renderer)
        self.assertIn("if (overdueBlock) detail.appendChild(overdueBlock);", renderer)
        self.assertIn("if (doneBlock) detail.appendChild(doneBlock);", renderer)
        self.assertNotIn("var blocks =", renderer)
        self.assertNotIn("todayBlock", renderer)
        self.assertIn("section('已过期', overdue, {className: 'overdue', collapsed: true, open: foldOpen('已过期')})", renderer)
        self.assertIn("section('已完成', data.done || [], {className: 'done', collapsed: true, open: foldOpen('已完成')})", renderer)
        self.assertIn("foldState[titleNode.textContent.trim()] = node.open", renderer)
        self.assertIn("return Object.prototype.hasOwnProperty.call(foldState, title) ? foldState[title] : true", renderer)
        section_fn = PAGE_SOURCE.split("function section(title, tasks, options)", 1)[1].split("var calendarPanel", 1)[0]
        self.assertIn("if (fold && options.open) wrap.open = true", section_fn)
        self.assertIn("details.section:not([open])>ul{display:none}", css)
        # 版心：一条 --measure 管桌面；待办页签不再分「主列 + 侧栏」，月历跟在主视图下面。
        base_css = css.split("@media(max-width:1100px)", 1)[0]
        calendar_rules = re.findall(r"\.calendar-day\{([^}]*)\}", base_css)
        self.assertTrue(calendar_rules)
        baseline_height = int(re.findall(r"min-height:(\d+)px", calendar_rules[-1])[-1])
        self.assertEqual(baseline_height, 44, "桌面日历格定在 44px：再矮放不下日期行高，再高会挤掉一屏")
        # 版心容器吃 gutter 内边距；.summary 自己就是卡片，单独缩进一层——
        # 它若也吃 gutter，卡框会顶到屏幕左右边缘（实测 x=0）。
        self.assertIn(".top-banner-row,.workspace,.bento{width:100%;max-width:var(--measure);margin-inline:auto;padding-inline:var(--gutter)}", css)
        self.assertNotIn(".summary,.top-banner-row,.workspace,.bento{", css)
        summary_rule = _css_rule(css, ".summary{")
        self.assertIn("width:calc(100% - 2 * var(--gutter))", summary_rule)
        self.assertIn("max-width:calc(var(--measure) - 2 * var(--gutter))", summary_rule)
        self.assertIn("margin:0 auto var(--space-8)", summary_rule)
        self.assertIn(".workspace{display:block;margin-top:0}", css)
        self.assertIn(".bento{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:var(--space-3);margin-top:var(--space-4)}", css)
        self.assertIn("body[data-tab]:not([data-tab=tasks]) .bento{display:none}", css)
        self.assertIn(".calendar-panel{margin:var(--space-6) 0 var(--space-8)}", css)
        self.assertNotIn(".side-rail", css)

    def test_summary_static_state_has_no_depth_transform(self) -> None:
        css = self._current_stylesheet()
        self.assertIn("--depth-mid:0px", css)
        self.assertRegex(css, r"\.summary\{[^}]*transform:none;animation:summary-enter")
        self.assertIn("to{opacity:1;transform:none}", css)
        self.assertNotIn("to{opacity:1;transform:translate3d(0,0,var(--depth-mid))", css)

    def test_completion_feedback_runs_after_confirmation_and_before_refresh(self) -> None:
        helper_start = PAGE_SOURCE.index("function animateConfirmedTaskCompletion")
        mutation_start = PAGE_SOURCE.index("function runTaskMutation", helper_start)
        action_start = PAGE_SOURCE.index("function sendAction", mutation_start)
        helper = PAGE_SOURCE[helper_start:mutation_start]
        mutation = PAGE_SOURCE[mutation_start:action_start]
        confirmed = mutation.index("if (!result.ok)")
        animation = mutation.index("var completionAnimation", confirmed)
        refreshed = mutation.index("return syncTasks().then", animation)
        self.assertLess(confirmed, animation)
        self.assertLess(animation, refreshed)
        self.assertIn("payload.action === 'done'", mutation)
        self.assertRegex(helper, re.compile(r"classList\.add\('completion-confirmed'\).*setTimeout\(resolve, 140\).*classList\.add\('completing'\).*setTimeout\(resolve, 160\)", re.S))

    def test_recent_push_feed_uses_type_time_and_body_not_count_summary(self) -> None:
        start = PAGE_SOURCE.index("function loadNotices()")
        end = PAGE_SOURCE.index("\nfunction", start + 1)
        feed = PAGE_SOURCE[start:end]
        self.assertIn("item.kind", feed)
        self.assertIn("item.created_at", feed)
        self.assertIn("item.body", feed)
        self.assertNotIn("item.summary", feed)
        self.assertNotIn("!data.ok", feed)
        self.assertIn("暂无推送记录。", feed)

    def test_inbox_attachment_renderer_keeps_text_and_hides_missing_image(self) -> None:
        start = PAGE_SOURCE.index("function renderInboxItem(")
        end = PAGE_SOURCE.index("\nfunction", start + 1)
        renderer = PAGE_SOURCE[start:end]
        self.assertIn("details.appendChild(el('p', '', content))", renderer)
        # 图片按后端给的 has_image 渲染：普通消息/回溯消息的图片也要出来，
        # 不再只认「群附件事件」那个 source（旧条件留在这里就是漏图的那个 bug）。
        self.assertIn("if (item.has_image)", renderer)
        self.assertNotIn("item.source === 'onebot-attachment'", renderer)
        self.assertIn("/api/inbox/image?", renderer)
        self.assertNotIn("/^onebot:.+:attach:[0-9a-f]{16}$/", renderer)
        # 缩略图挂在卡片上、不进默认收起的正文折叠块，否则不点开就看不见图；
        # 取不到图时外层链接一起撤掉，不留空占位。
        self.assertIn("main.appendChild(details);", renderer)
        self.assertIn("main.appendChild(imageLink);", renderer)
        self.assertNotIn("details.appendChild(imageLink);", renderer)
        self.assertIn("image.onerror = function () { imageLink.remove(); }", renderer)

    def test_inbox_hides_zero_count_filters_and_uses_one_line_empty_states(self) -> None:
        start = PAGE_SOURCE.index("function loadInbox(")
        end = PAGE_SOURCE.index("function loadInboxTab", start)
        inbox = PAGE_SOURCE[start:end]
        self.assertIn("countsBar.hidden = filterCounts.all === 0", inbox)
        self.assertIn("button.hidden = count === 0", inbox)
        self.assertIn("当前筛选条件下暂无通知。", inbox)
        self.assertIn("暂无判定结果。", inbox)
        self.assertNotIn("allCount", inbox)
        self.assertNotIn("先回溯聊天记录", inbox)

    def test_motion_preference_is_local_and_only_entrance_feedback_remains(self) -> None:
        """动效偏好只写本机、不联网；站内只剩一次性入场/反馈动画。"""
        start = PAGE_SOURCE.index("function setMotionPreference")
        end = PAGE_SOURCE.index("reducedMotionQuery.addEventListener", start)
        setting = PAGE_SOURCE[start:end]
        self.assertIn("localStorage.setItem('nh_motion'", setting)
        self.assertNotIn("fetch(", setting)
        self.assertIn('id="pref-motion-enabled"', PAGE_SOURCE)
        css = self._current_stylesheet()
        # 动效只剩一次性入场/反馈（feedback-in、task-enter、calendar-turn）；
        # 4s 呼吸光晕那类常驻动画连同 keyframe 一起删了。
        self.assertIn("@keyframes feedback-in{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:none}}", css)
        self.assertIn(".action-feedback.is-shown{animation:feedback-in 200ms var(--ease-out) both}", css)
        self.assertIn(".pct.is-bumped{animation:feedback-in 200ms var(--ease-out) both}", css)
        self.assertIn(".task.is-new{animation:task-enter 240ms var(--ease-out) both}", css)
        self.assertIn("#calendar.calendar-turn{animation:calendar-turn 240ms var(--ease-out) both}", css)
        self.assertNotIn("breathe", css)
        self.assertNotIn("transform:scale(1.12)", css)
        self.assertNotIn("box-shadow:0 0 0 5px", css)
        self.assertIn("html[data-motion=paused]", css)

    def test_agent_trace_contract(self) -> None:
        # 对话每条回复下面要能展开「它做了什么」：读了什么、选哪个工具、参数、结果
        css = self._current_stylesheet()
        self.assertIn(".agent-trace-head{", css)
        self.assertIn(".agent-trace-list{", css)
        start = PAGE_SOURCE.index("function agentTrace(steps)")
        end = PAGE_SOURCE.index("function agentGather()", start)
        block = PAGE_SOURCE[start:end]
        self.assertIn("aria-expanded", block)
        self.assertIn("list.hidden = open", block, "展开/收起必须真的切 hidden，不能只改样式")
        self.assertIn("head.setAttribute('aria-expanded', 'true')", block, "默认摊开：过程是这个版本的卖点")
        self.assertIn("list.hidden = false", block)
        self.assertIn("agentArgText(key, value)", block, "参数键要翻成人话，不能把 summary= 直接甩给用户")
        self.assertIn("function agentHumanWhen(value)", PAGE_SOURCE, "截止时间要说成「11 月 18 日」，不能把 ISO 原文甩出来")
        arg_fn = PAGE_SOURCE.split("function agentArgText(key, value)", 1)[1].split("function agentHumanWhen", 1)[0]
        self.assertIn("agentHumanWhen(value)", arg_fn, "日期得走到人话那一层")
        self.assertIn("AGENT_ARG_NAMES = {", PAGE_SOURCE)
        self.assertIn("AGENT_ACTIONS = {", PAGE_SOURCE)
        self.assertIn("agentTrace(result.trace)", PAGE_SOURCE, "回复到了要把它接上去")
        self.assertIn("replace(/^读了\\s*/, '')", block, "行首已有「读」标签，正文不能再以「读了」开头")
        # 对话态最开始那一屏以前是一整块空白，不知从哪下手
        self.assertIn('class="agent-empty"', PAGE_SOURCE, "空对话要给一句能照着说的示例")
        self.assertIn("问它「今天还有哪些事」", PAGE_SOURCE)
        self.assertIn(".agent-log:has(.agent-line) .agent-empty{display:none}", css,
                      "有第一句以后示例必须让位，不能一直挂在对话里")

    def test_tasks_page_can_scroll_when_one_screen_is_not_enough(self) -> None:
        """锁一屏是愿望，不是事实：装不下时必须能滚。

        1280x800 实测输入栏落在视口外 79px，main 又是 overflow:hidden，页面上点不到输入框。
        """
        css = self._current_stylesheet()
        rule = css.split("body:not([data-tab]) main,body[data-tab=tasks] main{", 1)[1].split("}", 1)[0]
        self.assertIn("overflow-y:auto", rule, "内容比一屏高时 main 要能滚，否则输入栏被裁掉")
        self.assertIn("overflow-x:hidden", rule, "纵向放开，横向别跟着出滚动条")
        self.assertNotIn("overflow:hidden", rule)
        # 待机态那行「待办 N 件」是横跨整栏的绝对定位说明，不设 pointer-events:none 就盖住输入框。
        count_rule = css.split(".agent-dock[data-state=idle] .agent-count{", 1)[1].split("}", 1)[0]
        self.assertIn("pointer-events:none", count_rule, "它挡住输入框，点输入框打不了字")


    def test_trip_stage_contract(self) -> None:
        # 今天几趟：常态是地图+行程，点对话时行程让位、顶部只剩一条行程条。
        css = self._current_stylesheet()
        self.assertIn(".trip-stage{display:grid", css)
        # 手机署名曾与绝对定位图例叠字；两者改走正常文档流，各占一行。
        self.assertIn(".trip-legend{flex:none;align-self:flex-end", css)
        self.assertIn(".trip-map-note{flex:none;", css)
        self.assertIn(".trip-map-wrap{display:flex;flex-direction:column;", css)
        # 拖地图转视角时不能顺手把楼名选中（选区蓝底会跟着指针跑）。
        self.assertIn("cursor:grab;user-select:none;-webkit-user-select:none", css)
        # 冷装发现旧隐藏规则让手机完全看不到待办；现在有行程时概览照常显示，
        # 高度上限撤了——待办列表已经搬出概览（`.task-board-block`），不用再靠
        # max-height 给行程让位，卡片也就不会再被 `overflow:hidden` 裁成半截。
        self.assertIn("body.has-trip .summary{display:block}", css,
                      "手机上有行程也让待办能看")
        self.assertNotIn("body.has-trip .summary{display:none", css,
                         "别再整块藏掉概览")
        self.assertIn(".task-board-block{", css, "待办列表自成一块，不再塞进限高的概览卡片")
        self.assertNotIn("body.has-trip .summary{display:block;max-height", css,
                         "概览再限高就会连待办卡片一起裁")
        self.assertIn(".task-detail-body{max-height:180px;overflow:auto", css,
                      "展开长详情在自身滚动，不能把输入栏挤走")
        self.assertIn(".agent-form{flex:none;", css, "输入栏不随长内容压缩")
        self.assertIn(".trip-strip-row{", css)
        awake = css.split("main:has(#agent-dock[data-state=awake]) .trip-stage{", 1)[1].split("}", 1)[0]
        self.assertIn("height:auto", awake, "抬头留在窄带上方，高度交给内容，不能再钉死成 104px")
        self.assertIn("main:has(#agent-dock[data-state=awake]) .trip-map-wrap{height:104px}", css,
                      "窄带高度由地图那条给，抬头不吃它的高度")
        self.assertIn("main:has(#agent-dock[data-state=awake]) .trip-side", css, "对话时行程卡整块收起")
        # 抬头必须挂在 #trip-stage 底下、和地图并列：塞在 .trip-side 里的话，对话态整个面板一收，
        # 这条窄带就没东西说明它是「今天几趟」。
        self.assertNotIn("main:has(#agent-dock[data-state=awake]) .trip-head{display:none", css)
        stage_html = PAGE_SOURCE.split('<section id="trip-stage"', 1)[1].split("</section>", 1)[0]
        self.assertLess(stage_html.index('class="trip-head"'), stage_html.index('class="trip-side"'),
                        "抬头在行程卡之前，才能横跨两栏留在最上面")
        self.assertNotIn('class="trip-head"', stage_html.split('class="trip-side"', 1)[1])
        page = PAGE_DIR.joinpath("js", "trip.js").read_text(encoding="utf-8")
        self.assertIn("if ((host.parentNode ? host.parentNode.getBoundingClientRect().height : box.height) < 200) {", page,
                      "扁带里画等距图只会得到一堆小方块，必须切行程条")
        self.assertNotIn("if (box.height < 200) {", page,
                         "判据不许量画布自己的高度：行程条一进来自家工具栏被 :has(.trip-strip) 收掉、画布反而变高，"
                         "3D 与行程条会自己喂自己无限来回翻")
        self.assertIn("if (window.nhMap3d) window.nhMap3d.dispose(host);\n      drawStrip(plan, host, byId);", page,
                      "扁带不画 3D：切走前要把画布和 GL 资源收掉，别留一个不显示的渲染器")
        self.assertIn("document.body.classList.add('has-trip')", page)
        self.assertIn("document.body.classList.remove('has-trip')", page, "读失败要把标记撤掉，否则待办概览被永久藏起来")
        self.assertIn("'这台机器没开行程接口（演示版启动时会打开）'", page)
        self.assertIn("error.status === 404", page,
                      "后端 404 的 body 是英文 'not found'，必须按状态码判，否则界面上直接出现英文")
        self.assertIn("failed.status = response.status", PAGE_SOURCE + page,
                      "api() 不把状态码带出去，调用方就分不出 404 和别的失败")
        # 楼名挪回画框里再判重叠，编号先占位：窄屏上最靠边的楼名原本被裁成半个字，
        # 编号圆点也会压在楼名上。
        self.assertIn("x = Math.min(Math.max(x, half + 2), W - half - 2);", page, "楼名必须夹回画框内")
        self.assertIn("if (W < 2 * half + 4) return null;", page, "画框窄到放不下就整条不画，不能折成半截")
        self.assertLess(page.index("function overlaps(box)"), page.index("function labelFits(x, y, name)"),
                        "labelFits 依赖 overlaps 判重")
        self.assertLess(page.index("stops.forEach(function (item) {", page.index("var taken = [];")),
                        page.index("function labelFits(x, y, name)"),
                        "编号要在画楼名之前先占住位置")
        self.assertIn('rx="3" fill="\' + token(\'--map-ground\') + \'" opacity=".82"', page,
                      "路线从楼名底下穿过去时，行程里的楼名要垫一块底色，否则看着像被划掉")

    def test_task_hover_lifts_up_instead_of_shoving_sideways(self) -> None:
        """待办卡悬停是往上抬 2px，不是往右挪：往右会蹭到右边那张卡，读起来像被推走。"""
        css = PAGE_SOURCE.split("<style", 1)[1].split("</style>", 1)[0]
        self.assertNotIn("transform:translateX(2px)", css, "待办卡不许再往右推")
        self.assertIn(
            ".task:not(.done):not([aria-busy=true]):not(.completing):hover,\n"
            ".task:not(.done):not([aria-busy=true]):not(.completing).is-focused,\n"
            ".task:not(.done):not([aria-busy=true]):not(.completing):focus-within{\n"
            "  border-color:var(--sand);transform:translateY(-2px);box-shadow:0 6px 16px rgba(10,20,18,.10);z-index:1;",
            css, "悬停往上抬 2px 并抬层，阴影才不会被邻居裁掉")
        self.assertIn(
            "transition:border-color 140ms var(--ease-in),transform 140ms var(--ease-out),box-shadow 140ms var(--ease-out)",
            css, "位移与阴影同一条过渡，抬起来不能只有一半在动")
        # 抬层要生效的前提：卡片自己就是定位元素，否则 z-index 不参与层叠。
        self.assertIn(".task{\n  position:relative;", css)
        reduce_block = css.split("@media (prefers-reduced-motion:reduce){", 1)[1].split("\n}", 1)[0]
        self.assertIn(".task:hover,.task.is-focused,.task:focus-within{transform:none!important", reduce_block,
                      "减少动态偏好下不位移，只留描边和阴影")
        # .completing 的淡出上浮不能被 hover 规则顶回原位：两条规则都改 transform。
        self.assertIn(":not(.completing):hover", css)
        self.assertIn(".task.completing{opacity:0;transform:translateY(-4px)", css)

    def test_trip_side_column_keeps_one_readable_line_per_stop(self) -> None:
        """右侧「第几趟」那列只留一趟标签 + 地点名：小字全收进 title，字号不低于 12px。"""
        page = PAGE_DIR.joinpath("js", "trip.js").read_text(encoding="utf-8")
        rows = page.split("function rowsOf(trip) {", 1)[1].split("function renderTrips", 1)[0]
        self.assertNotIn('class="trip-time"', page, "时间不再单独占一列：11px 数字看不清，1280 下被裁成「08:」")
        self.assertNotIn('class="trip-card-meta"', page, "抬头那串「– 件 · 走路 N 分钟」不进栏")
        self.assertNotIn("<i>", rows, "「为什么排这一站」这类细注只进 title，不铺在栏里")
        self.assertIn('title="\' + esc(detail) + \'">', rows, "砍掉的小字要收进 title，别直接丢")
        self.assertIn("stop.minutes ? '留 ' + stop.minutes + ' 分钟' : ''", rows)
        css = PAGE_SOURCE.split("<style", 1)[1].split("</style>", 1)[0]
        self.assertNotIn(".trip-time{", css)
        self.assertNotIn(".trip-card-meta{", css)
        self.assertIn(".trip-card-head h3{margin:0;font-size:var(--fs-4)", css, "一趟的标签不小于 12px")
        self.assertIn(".trip-row{display:grid;grid-template-columns:minmax(0,1fr)", css, "单列，别再挤时间列")
        # 右侧这一列自己的规则不许再出现 9px；地图上的角标（.trip-map-*、.trip-legend、.trip-strip-*）
        # 是画布上的覆盖层，不在这条之内。
        column_rules = (".trip-card", ".trip-row", ".trip-what", ".trip-rows", ".trip-list", ".trip-side",
                        ".trip-meta", ".trip-rest")
        for rule in css.splitlines():
            if rule.startswith(column_rules) and "font-size:var(--fs-1)" in rule:
                self.fail("行程列不许再用 9px 小字：" + rule)

    def test_trip_map_3d_asset_and_dispose_contract(self) -> None:
        """3D 地图：three 打进静态目录、等 load 之后再拉模块，重画前必须能释放上一块画布。"""
        page = PAGE_DIR.joinpath("js", "trip.js").read_text(encoding="utf-8")
        # 690 KB 的 three 跟这一页抢带宽，弱网首屏要多等半分钟（手机端实测 51 秒才可用），
        # 所以 map3d.js 改成 load 之后 import。代价是 3D 比 SVG 晚一步，靠 trip.js 这条监听补画，
        # 没有它的话懒加载等于把 3D 丢了：先 SVG，模块到位后没人重画。
        self.assertNotIn('<script type="module" src="js/map3d.js"></script>', PAGE_SOURCE,
                         "3D 模块不许在首屏就拉：三个文件 690 KB，弱网会拖住整页")
        self.assertIn("import('./js/map3d.js')", PAGE_SOURCE,
                      "说明符要带 ./：裸写 js/map3d.js 浏览器当包名，直接 Failed to resolve")
        self.assertLess(PAGE_SOURCE.index("js/guide.js"), PAGE_SOURCE.index("import('./js/map3d.js')"),
                       "3D 模块在普通脚本之后加载，trip.js boot 时 window.nhMap3d 才在")
        self.assertIn("window.addEventListener('nh-map3d-ready', function () { if (state.plan) drawMap(state.plan); });",
                      page, "模块迟到时要按已有的排法重画成 3D，否则页面停在 SVG 上")
        self.assertIn("console.warn('三维地图没拉起来，改用平面地图', error)", PAGE_SOURCE,
                      "拉不起来可以退平面图，但不许一声不吭：控制台要留一行，否则「没画出来」查不了")
        three = PAGE_DIR.joinpath("js", "three.module.min.js").read_text(encoding="utf-8")
        self.assertGreater(len(three), 100000, "three 必须打进静态目录，不能去 CDN 取")
        map3d = PAGE_DIR.joinpath("js", "map3d.js").read_text(encoding="utf-8")
        self.assertIn("from './three.module.min.js'", map3d)
        self.assertNotIn("cdn", map3d.lower())
        # 失败路径：材质可能是数组。旧写法 node.material.dispose() 会在换主题重画时抛异常，
        # 整张图停在上一版（画布还是亮色那张），所以释放必须容忍数组与没有 dispose 的材质。
        self.assertIn("function killMat(m) {", map3d)
        self.assertIn("if (Array.isArray(m)) { m.forEach(killMat); return; }", map3d)
        self.assertIn("if (typeof m.dispose === 'function') m.dispose();", map3d)
        self.assertIn("killMat(node.material);", map3d)
        self.assertIn("if (window.nhMap3d && window.nhMap3d.draw(host, plan)) {", page)
        # 站号那个圆点就压在楼顶上，楼名原本也锚在楼顶，投影下来正好被它盖住：
        # 实测 1280 下 6 个站有 4 栋名字不见了，只剩数字。楼名改成跟站号锚同一处、再用 dy
        # 抬一个站号的高度，名在上号在下。
        self.assertIn("var PIN_H = 19;", map3d, "站号圆点的像素高要有个名字，楼名让开的距离从它算")
        self.assertIn("at: new THREE.Vector3(p.x - cx, h + (live ? 22 : 2), p.y + size[1] / 2 - cz),", map3d)
        self.assertIn("dy: live ? -(PIN_H + 4) : -3", map3d, "行程里的楼名必须让开站号，否则等于没写")
        self.assertNotIn("-(p.y", map3d, "楼名/路线/取景的 y 一律正向（南正），不许再出现镜像写法")
        self.assertIn("y: (-v.y * 0.5 + 0.5) * state.Hh + (item.dy || 0), z: v.z,", map3d,
                      "dy 是在画面上抬，不是世界坐标里抬")
        # 认路的楼名被画框切掉半个字，看着像没画完：楼名跟它的站号当一个组，
        # 组里有框出界就整组藏起来，不留半截字。
        self.assertIn("return group[index].z <= 1 && b[0] >= 0 && b[2] <= state.W && b[1] >= 0 && b[3] <= state.Hh &&",
                      map3d, "出画框的楼名要整组藏，不许画半个字")
        self.assertIn("p.item.el.style.display = placed === null ? 'none' : '';", map3d,
                      "排不下就隐藏，不是留半截")

    def test_road_line_switch_and_fallback(self) -> None:
        """路线走向有两档：「沿路」用后端算的绕楼折线，「直线」楼与楼直连。

        两档都留在页面上：折线偶尔看着绕，用户得能一键切回来看清楚。
        老数据没有 `line` 时不许把图画空，退回直连。
        """
        page = PAGE_DIR.joinpath("js", "trip.js").read_text(encoding="utf-8")
        map3d = PAGE_DIR.joinpath("js", "map3d.js").read_text(encoding="utf-8")
        self.assertIn("button.setAttribute('data-map-line', item[0]);", page)
        self.assertIn("localStorage.setItem('nh_map_line', want);", page)
        # 3D 和 SVG 读的是同一个键，否则切了档只换半边天。
        self.assertIn("localStorage.getItem('nh_map_line') === 'straight' ? 'straight' : 'road'", page)
        self.assertIn("localStorage.getItem('nh_map_line') === 'straight' ? 'straight' : 'road'", map3d)
        self.assertIn("if (lineMode() === 'road' && trip.line && trip.line.length >= 2) return trip.line;", page,
                      "没有折线时要退回直连，不能因为缺数据就不画路线")
        self.assertIn("var line = trip && trip.line;", map3d)
        self.assertIn(".map-modes-gap{width:1px;align-self:stretch;margin:2px 3px;background:var(--line)}",
                      PAGE_SOURCE, "两组开关之间要有一道竖线，否则六个按钮看着像一排并列的底图")

    def test_map_here_picker_and_visible_start(self) -> None:
        """「我在哪」能在地图上标，标完起点从这儿算；起点在图上要看得见。

        起点只写在地图下方那行灰字里等于没标：线从楼里出来，看不出人从哪儿出发。
        """
        page = PAGE_DIR.joinpath("js", "trip.js").read_text(encoding="utf-8")
        self.assertIn("pick.id = 'map-here-select';", page)
        self.assertIn("'<option value=\"\">我在…</option>'", page)
        self.assertIn("body: JSON.stringify({ action: 'here', place: place }),", page)
        # 没有起点时得明说怎么标，而不是默认让人以为起点是第一个地点。
        self.assertIn("还没标你在哪：点「我在…」选一栋楼，路线按那儿算", page)
        # 用户是在对话框里说话的，提示也得落在对话框里：起点按课表算的地方，人未必还在那儿。
        self.assertIn("先说你在哪栋楼（「我在图书馆」），路线就按你那儿算", page)
        self.assertIn("var chatHint = null;", page)
        self.assertIn("chat.placeholder = plan.here ? chatHint :", page, "标了位置要把原来的提示语还回去")
        self.assertIn("labelFits(ox, oy + 21, '起点')", page, "起点要有可见标记，不只靠下面那行小字")
        # 1280 的地图是 3D 画的，SVG 那份根本没上屏：3D 得自己画起点。
        map3d = PAGE_DIR.joinpath("js", "map3d.js").read_text(encoding="utf-8")
        self.assertIn("putLabel(labelBox, '起点', 'map3d-pin is-start')", map3d)
        self.assertIn("var originPoi = plan.origin && byId[plan.origin.id];", map3d,
                      "起点是某一站时也要标，那枚编号在同一栋楼上，摆位会把它们错开")
        self.assertIn(".map3d-pin.is-start{", PAGE_SOURCE)
        self.assertIn(".map-here-select{", PAGE_SOURCE)
        # 手机上别把它浮回画布上：工具栏已经排在画布上方，选择器留在那一行里，
        # 手指够得着也不压路线（旧写法 absolute + top:calc(100% + 6px) 会盖住整条折线）。
        self.assertIn("@media(max-width:720px){.map-here-select{min-height:44px;max-width:96px;font-size:var(--fs-2)}}",
                      PAGE_SOURCE)
        self.assertNotIn("top:calc(100% + 6px)", PAGE_SOURCE, "选择器别再浮到画布上")

    def test_routine_panel_hugs_the_chat_box(self) -> None:
        """「常做的事」要贴着对话条，随手能拖进趟里。

        趟列表一长，它就滚到栏外去了：想插一条得先往下翻，翻完又看不见路线。
        宽屏钉在列表底边，窄屏挪到列表前头（手机上这一栏有一屏多长）。
        """
        self.assertIn('<div class="trip-pin">', PAGE_SOURCE)
        # 栏改成竖排 flex：列表自己滚，钉子不进滚动区，所以它一直在栏底、贴着对话条，
        # 不靠 sticky 去和滚动位置较劲（sticky 那次实测钉在半路，底下漏出半截被淡掉的字）。
        self.assertIn("main>.trip-stage>.trip-side{flex:1 1 auto;display:flex;flex-direction:column;", PAGE_SOURCE)
        self.assertIn("main>.trip-stage>.trip-side .trip-scroll{flex:1 1 auto;min-height:0;overflow:auto;", PAGE_SOURCE)
        self.assertIn("main>.trip-stage>.trip-side>.trip-pin{position:static;flex:none;z-index:auto;", PAGE_SOURCE)
        self.assertIn(".trip-pin{order:-1;margin-bottom:var(--space-2)}", PAGE_SOURCE)

    def test_plan_order_carries_the_trip_boundaries(self) -> None:
        """把站拖到别的趟里，得把「哪几站算同一趟」一起发上去。

        只发扁平站序时服务端只能按区域重新分趟，跨区拖过去会当场弹回原来那趟。
        """
        page = PAGE_DIR.joinpath("js", "trip.js").read_text(encoding="utf-8")
        self.assertIn("var groups = [];", page)
        self.assertIn("if (names.length) groups.push(names);", page)
        self.assertIn("groups: groups", page, "POST body 里要带上趟边界")
        self.assertIn("// groups 是趟边界：只发扁平站序的话，拖到别的趟里的站会被区域重新拆回去。", page)

    def test_tapping_a_routine_already_at_the_end_says_so(self) -> None:
        """轻点已经在末尾的常做事项，页面不该报「已经挪好」——那一次什么都没变。"""
        page = PAGE_DIR.joinpath("js", "trip.js").read_text(encoding="utf-8")
        self.assertIn("function planTitles(plan)", page)
        self.assertIn("if (planTitles(state.plan) === before) {", page)
        self.assertIn("已经在那一趟的最后了，没动。", page)

    def test_map_note_names_the_real_source(self) -> None:
        """底图说明条原先写「楼体位置据校园平面图量取」，坐标早改成 OSM 投影了：说明得跟着数据来源走。"""
        self.assertNotIn("楼体位置据校园平面图量取", PAGE_SOURCE,
                         "说明条还写着平面图量取，跟现在的 OSM 坐标对不上")
        self.assertIn("楼位与道路据 OpenStreetMap 数据换算", PAGE_SOURCE)
        self.assertIn("虚线为估算位置", PAGE_SOURCE, "估算的点要有交代，署名后面就是图例")

    def test_reduced_motion_and_paused_switch_stop_animation(self) -> None:
        """系统偏好与站内开关两条路都要把动画、过渡全停；不许靠裁字换高度。"""
        css = self._current_stylesheet()
        self.assertNotIn("-webkit-line-clamp", css)
        reduced = _css_block(css, "@media (prefers-reduced-motion:reduce){")
        self.assertIn("animation:none!important", reduced)
        self.assertIn("transition:none!important", reduced)
        self.assertIn("html[data-motion=paused] *,html[data-motion=paused] *::before", css)

    def test_summary_breathe_keeps_text_still_and_moves_only_the_aura(self) -> None:
        css = self._current_stylesheet()
        # 概览卡有三条 .summary 规则（版心覆盖 / 主体 / 窄屏）；要锁的是主体那条。
        cards = [rule for rule in re.findall(r"\.summary\{[^}]*\}", css) if "position:relative" in rule]
        self.assertEqual(len(cards), 1, "概览卡主体规则应当唯一")
        card = re.match(r"\.summary\{[^}]*\}", cards[0])
        self.assertIsNotNone(card)
        self.assertNotIn("margin-top:var(--space-4)}", card.group(0))
        self.assertNotIn("@keyframes summary-breathe", css)
        self.assertNotIn("summary-glow-breathe", css)
        self.assertNotIn("summary-shadow-breathe", css)
        self.assertNotIn("badge-breathe", css)
        # 两个伪元素层直接不产出（地面阴影与光晕都没了），也就不存在「盖住文字」的风险。
        self.assertIn(".summary::before,.summary::after{display:none}", css)
        self.assertRegex(card.group(0), r"animation:summary-enter 240ms var\(--ease-out\) both")
        self.assertNotIn("infinite", card.group(0))
        enter = re.search(r"@keyframes summary-enter\{(.*?)\}\n", css, re.S)
        self.assertIsNotNone(enter)
        self.assertIn("to{opacity:1;transform:none}", enter.group(1))
        # 入场只动 opacity/transform：绘制属性会让浏览器每帧重绘整卡文字。
        self.assertNotIn("box-shadow", enter.group(1))
        self.assertNotIn("filter", enter.group(1))

    def test_action_feedback_uses_the_same_column_as_the_cards(self) -> None:
        """红条跟卡片列同宽：宽屏下不许比下面的卡片岛更宽。
        它的 max-width 曾经写成 var(--measure)，而卡片全是
        calc(var(--measure) - 2 * var(--gutter))，1920 下宽出 80px。
        """
        css = self._current_stylesheet()
        rule = re.search(r"\.action-feedback\{[^}]*\}", css)
        self.assertIsNotNone(rule, "红条必须有一条独立规则")
        self.assertIn("width:calc(100% - 2 * var(--gutter))", rule.group(0))
        self.assertIn("max-width:calc(var(--measure) - 2 * var(--gutter))", rule.group(0))
        cards = [r for r in re.findall(r"\.summary\{[^}]*\}", css) if "position:relative" in r]
        self.assertEqual(len(cards), 1)
        self.assertIn("max-width:calc(var(--measure) - 2 * var(--gutter))", cards[0])

    def test_boot_loads_tasks_through_the_quiet_retry(self) -> None:
        """首次打开走静默重试：页面可能来自 Service Worker 缓存，后端还在起。
        一次取数失败就出红条，等于让用户替启动时序买单。
        """
        self.assertNotIn("\nloadTasks();", PAGE_SOURCE, "启动不该直接调 loadTasks()")
        self.assertIn("\nprimeTasks();", PAGE_SOURCE)
        start = PAGE_SOURCE.index("function primeTasks(attempt)")
        end = PAGE_SOURCE.index("\nfunction ", start + 1)
        quiet = PAGE_SOURCE[start:end]
        self.assertIn("tries >= 5", quiet, "重试要有上限，不能无限等")
        self.assertIn("reportTaskLoadFailure(error)", quiet, "重试用完要报错，别吞掉失败")
        self.assertNotIn("showFeedback(", quiet, "重试期间不许弹提示")

    def test_header_brand_mark_is_the_app_icon_not_a_letter(self) -> None:
        mark = re.search(r'<svg class="brand-mark".*?</svg>', PAGE_HTML, re.S)
        self.assertIsNotNone(mark, "页头的小 logo 必须是品牌 mark，而不是字母方块")
        check = "M112.64 307.2 L184.32 378.88 L276.48 235.52"
        self.assertIn(check, mark.group(0))
        # 与托盘/任务栏用的图标同源（assets/icon.svg），避免页头又变成另一套画法
        icon = (PROJECT_ROOT / "assets" / "icon.svg").read_text(encoding="utf-8")
        self.assertIn(check, icon)
        self.assertNotIn('<span class="brand-mark"', PAGE_SOURCE)

    def test_transport_errors_are_translated_before_display(self) -> None:
        # QQ 未登录时 NapCat 的 OneBot 端口拒绝连接，服务端 JSON 里保留原始 socket 文本；
        # 界面必须只显示一句人话，且翻译只写一份（friendlyError），不许各面板各写一套。
        self.assertIn("function friendlyError(raw)", PAGE_SOURCE)
        self.assertIn("10061|积极拒绝|Connection refused|ECONNREFUSED", PAGE_SOURCE)
        self.assertIn("x.error = friendlyError(x.error)", PAGE_SOURCE)  # setupApi
        self.assertIn("body.error = friendlyError(body.error)", PAGE_SOURCE)  # api
        self.assertIn("friendlyError((error && error.message) || error)", PAGE_SOURCE)
        self.assertIn("连接被拒绝|QQ 未登录", PAGE_SOURCE)  # checkSetup 的兜底判定也要认这句人话

    def test_group_subscription_ui_uses_suggestions_and_truthful_sources(self) -> None:
        self.assertIn("/api/groups/suggest", PAGE_SOURCE)
        self.assertIn("/api/napcat/groups", PAGE_SOURCE)
        self.assertIn('id="group-search" class="group-search" type="search"', PAGE_SOURCE)
        self.assertIn("其它 ", PAGE_SOURCE)
        self.assertIn("全选建议", PAGE_SOURCE)
        self.assertIn("清空", PAGE_SOURCE)
        self.assertIn("suggestion.source==='llm'?'AI 识别'", PAGE_SOURCE)
        self.assertIn("suggestion.source==='heuristic'?'按关键词识别'", PAGE_SOURCE)
        self.assertIn("未选择任何群，将清空所有订阅", PAGE_SOURCE)
        self.assertIn("前往 QQ 接入", PAGE_SOURCE)

    def test_chaoxing_cookie_setup_orders_recommended_auto_then_collapsed_manual(self) -> None:
        start = PAGE_SOURCE.index('id="cx-auto-option"')
        end = PAGE_SOURCE.index("root.appendChild(pairCard())", start)
        chaoxing = PAGE_SOURCE[start:end]
        self.assertIn('<details id="cx-manual-option">', chaoxing)
        self.assertNotIn('<details id="cx-manual-option" open', chaoxing)
        self.assertNotIn('id="cx-cookie"', chaoxing)
        self.assertNotIn('cx-clear', chaoxing)
        self.assertIn("方式一：浏览器自动获取", chaoxing)
        self.assertIn(">推荐</span>", chaoxing)
        self.assertIn("托盘右键「读取学习通登录凭证」", chaoxing)
        self.assertIn("保持本页打开", chaoxing)
        self.assertIn("复制一份登录凭证库和日志再读取", chaoxing)
        self.assertIn("会提示下一步", chaoxing)
        self.assertNotIn("② 完全退出该浏览器（含托盘图标和后台进程）", chaoxing)
        self.assertNotIn("③ 回到这里点“重试自动获取”", chaoxing)
        self.assertNotIn("document.cookie", chaoxing)
        self.assertNotIn("HTTP <status>", PAGE_SOURCE)
        self.assertIn('href="https://i.chaoxing.com/"', chaoxing)
        for step in ("login", "exit", "retry"):
            self.assertIn('id="cx-step-' + step + '-state"', chaoxing)
        # 保存时读 cxLoaded 之前必须先声明：否则用户从未拉取过课程时读未声明变量会抛
        # ReferenceError，按钮停在「保存中…」、请求根本发不出去（用户报的「一直卡住」）
        self.assertIn("var cxLoaded=false,cxSelected=[];", PAGE_SOURCE)
        # 保存群白名单：点「保存」那一刻现读 DOM 勾选（value 即 course_id），不用页面加载时的快照。
        save_start = PAGE_SOURCE.index("document.getElementById('cx-save').onclick")
        cx_save = PAGE_SOURCE[save_start:PAGE_SOURCE.index("root.appendChild(pairCard())", save_start)]
        self.assertIn("var boxes=document.querySelectorAll('#cx-courses input[type=checkbox]')", cx_save)
        self.assertIn("if(x.checked&&x.value)groups.push(x.value)", cx_save)  # 只发勾选中的，空 course_id 连勾了也不发
        self.assertIn("input.value=row.course_id||''", PAGE_SCRIPT)  # 渲染时就把缺失的 course_id 落成空串
        # 课程没渲染（0 个 checkbox）才回退内存已选集合；有 DOM 就以 DOM 为准
        self.assertIn("payload.chaoxing_group_whitelist=boxes.length?groups:cxSelected.slice();", cx_save)
        # 一个都没勾时发空列表（清空白名单），绝不能退化成「全选」或跳过这个字段
        self.assertNotIn("rows", cx_save)
        # 保存成功后内存集合跟着 DOM 走，否则下次回退会拿回旧的全量
        self.assertIn("if(boxes.length)cxSelected=groups.slice();", cx_save)
        # 同一段行为在「合并页面文本」和「只看 JS」两种切分下都要取得到（与文件顺序无关）
        script_save = PAGE_SCRIPT[PAGE_SCRIPT.index("document.getElementById('cx-save').onclick"):]
        self.assertIn("payload.chaoxing_group_whitelist=boxes.length?groups:cxSelected.slice();", script_save)
        auto_cookie_handler = PAGE_SOURCE.split("document.getElementById('cx-auto-cred').onclick", 1)[1].split("document.getElementById('cx-load-courses')", 1)[0]
        self.assertNotIn("chaoxing_cookie:cookie", auto_cookie_handler)
        for category in ("browser_unavailable", "browser_locked", "no_chaoxing_cookie", "decrypt_failed"):
            self.assertIn(category, chaoxing)
        self.assertIn("renderCxAttempts(v.attempts||[])", chaoxing)
        self.assertIn("method:'POST'", chaoxing)
        self.assertIn("cx-manual-option", chaoxing)

    def test_chaoxing_wechat_enable_toggles_post_only_the_enabled_field(self) -> None:
        start = PAGE_SOURCE.index("function saveEnabledToggle")
        end = PAGE_SOURCE.index("saveEnabledToggle('wx-enabled'", start)
        toggle = PAGE_SOURCE[start:end]
        self.assertIn("input.addEventListener('change'", toggle)
        self.assertIn("var payload={};payload[key]=enabled", toggle)
        self.assertIn("body:JSON.stringify(payload)", toggle)
        self.assertIn("input.checked=!enabled", toggle)
        self.assertIn("保存失败，已恢复原状态：", toggle)
        self.assertIn("saveEnabledToggle('wx-enabled','wx-result','wechat_enabled')", PAGE_SOURCE)
        self.assertIn("saveEnabledToggle('cx-enabled','cx-result','chaoxing_enabled')", PAGE_SOURCE)

    def test_chaoxing_save_reports_failure_and_restores_button(self) -> None:
        start = PAGE_SOURCE.index("document.getElementById('cx-save').onclick")
        end = PAGE_SOURCE.index("root.appendChild(pairCard())", start)
        handler = PAGE_SOURCE[start:end]
        self.assertIn("button.disabled=true", handler)
        self.assertIn("button.textContent='保存中…'", handler)
        self.assertIn("保存超时：请求没有响应，请刷新页面或改用托盘导入", handler)
        self.assertIn("api('/api/chaoxing/status').then(renderChaoxingStatus)", handler)
        self.assertIn("button.disabled=false;button.textContent='保存学习通设置'", handler)
        self.assertIn("Promise.race([api('/api/settings'", handler)
        self.assertIn("保存超时：请求没有响应，请刷新页面或改用托盘导入", handler)
        self.assertIn("setTimeout(function(){reject(new Error", handler)

    def test_chaoxing_auto_cookie_success_preserves_enabled_checkbox(self) -> None:
        start = PAGE_SOURCE.index("document.getElementById('cx-auto-cred').onclick")
        end = PAGE_SOURCE.index("document.getElementById('cx-load-courses').onclick", start)
        handler = PAGE_SOURCE[start:end]
        self.assertIn("document.getElementById('cx-enabled').checked=true", handler)
        self.assertNotIn("checked=!!s.chaoxing_enabled", handler)

    def test_wechat_dingtalk_subscription_ui_shows_recommendations(self) -> None:
        # 群选择区改成「推荐区折叠 + 全部群默认收起」后，全选建议从工具栏移进推荐区，
        # 所以这里锁的是选择区本身：两条通道走同一套渲染，推荐行仍带建议徽标。
        self.assertIn("renderGroupPicker('wechat',payload)", PAGE_SOURCE)
        self.assertIn("renderGroupPicker('dingtalk',payload)", PAGE_SOURCE)
        for source in ("wx", "dt"):
            self.assertIn('id="' + source + '-suggest-source"', PAGE_SOURCE)
        self.assertIn("按关键词识别", PAGE_SOURCE)
        self.assertIn("appendRecommendation(meta,row)", PAGE_SOURCE)
        self.assertIn("data-suggested=\"true\"", PAGE_SOURCE)
        self.assertIn("建议订阅", PAGE_SOURCE)
        self.assertIn("dataset.gselPickAll='1'", PAGE_SOURCE)
        self.assertIn("'全部群聊（'+rows.length+' 个）'", PAGE_SOURCE)

    def test_task_mutation_waits_for_server_and_syncs_calendar_before_success(self) -> None:
        start = PAGE_SOURCE.index("function runTaskMutation")
        end = PAGE_SOURCE.index("function sendAction", start)
        mutation = PAGE_SOURCE[start:end]
        confirmed = mutation.index("if (!result.ok)")
        refreshed = mutation.index("return syncTasks().then", confirmed)
        announced = mutation.index("showFeedback(success, 'success')", confirmed)
        self.assertLess(confirmed, refreshed)
        self.assertLess(refreshed, announced)
        self.assertIn("showFeedback('操作未完成：' + error.message, 'error', retry)", mutation)
        self.assertIn("window.syncCalendarTasks(data)", PAGE_SOURCE)

    def test_settings_and_hosting_require_successful_response_and_status(self) -> None:
        self.assertIn("if(!result.ok)throw new Error(result.error||'服务端未保存设置')", PAGE_SOURCE, "settings success requires a confirmed response")
        save_start = PAGE_SOURCE.index("function savePreferences")
        save_end = PAGE_SOURCE.index("\n  setPreferencesBusy(true);", save_start)
        saving = PAGE_SOURCE[save_start:save_end]
        self.assertLess(saving.index("if(!result.ok)"), saving.index("showFeedback('设置已保存','success')"))
        self.assertIn("showFeedback('设置未保存：'+error.message,'error'", saving, "settings failures must be visible")
        start = PAGE_SOURCE.index("function changeHosting")
        end = PAGE_SOURCE.index("document.getElementById('hosting-start'", start)
        hosting = PAGE_SOURCE[start:end]
        self.assertIn("if(!result.ok)throw new Error(result.error||'服务端未确认操作')", hosting, "hosting success requires a confirmed response")
        self.assertGreaterEqual(hosting.count("refreshHosting().then(function(status)"), 2, "start/stop must read hosting status after action")
        self.assertIn("!!status.hosting_active!==desired", hosting, "start/stop must verify the requested state")


    def test_setup_wizard_and_group_status_are_wired(self) -> None:
        self.assertIn('id="platform-setup-wizard"', PAGE_SOURCE)
        # 「还有 N 项没配好」不再占页头横幅：走右上角悬浮通知 + 详情弹窗。
        self.assertNotIn('id="platform-setup-banner"', PAGE_SOURCE)
        self.assertNotIn('id="platform-setup-panel"', PAGE_SOURCE)
        self.assertIn("NHNotify.push({", PAGE_SOURCE)
        self.assertIn("id:SETUP_NOTICE_ID", PAGE_SOURCE)
        self.assertIn("onOpen:openCheckupModal", PAGE_SOURCE)
        self.assertIn("NHNotify.openModal({", PAGE_SOURCE)
        self.assertIn("/api/setup/status", PAGE_SOURCE)
        self.assertIn("refreshGroupPull", PAGE_SOURCE)
        self.assertIn("/api/qq/groups/status", PAGE_SOURCE)
        self.assertIn('id="group-pull-status"', PAGE_SOURCE)
        self.assertIn('id="platform-setup-rerun"', PAGE_SOURCE)
        self.assertIn("function openPlatformSetupTarget(key)", PAGE_SOURCE)
        # 「去配置」的接线：列表里每个按钮点下去必须带着**那一行自己的 key** 调到 openPlatformSetupTarget。
        # 体检列表和向导第 2 步共用 renderCheckupRow，所以这里锁三件事：按钮挂的是 runCheckupAction、
        # panel 动作把 action.target 翻回平台 key 再跳、翻不动时回落本行的 key（不给一个点了没用的按钮）。
        self.assertIn("button.addEventListener('click',function(){runCheckupAction(row,button);})", PAGE_SOURCE)
        self.assertIn("openPlatformSetupTarget(checkupTargetKey(action.target,row.key))", PAGE_SOURCE)
        self.assertIn("function checkupTargetKey(target,fallback)", PAGE_SOURCE)
        self.assertIn("var hit=Object.keys(setupTargets).filter(function(k){return setupTargets[k]===target;});", PAGE_SOURCE)
        self.assertIn("return hit.length?hit[0]:fallback;", PAGE_SOURCE)
        # 「去配置」不能赌固定 150ms：卡片异步填内容，滚完还会变（8767 实测 #connect 1000→2500ms
        # 从 3027 涨到 3047）；等两帧稳定再滚、版面再变校正一次；超高目标顶边要落在页头下面。
        self.assertIn("settleScrollWizardTarget(id)", PAGE_SOURCE)
        self.assertNotIn("window.setTimeout(function(){var target=document.getElementById(id)", PAGE_SOURCE)
        # 横幅文案由「未配好那几行」的名字拼出来：名字来自同一个 platformRows，不另起一份推导。
        self.assertIn("setupRows.map(function(row){return row.name;}).join('、')", PAGE_SOURCE)
        self.assertIn("title:'还有 '+setupRows.length+' 项没配好'", PAGE_SOURCE)
        self.assertIn("暂不可用（开发中）", PAGE_SOURCE)

    def test_dashboard_notices_and_layout_regressions(self) -> None:
        # 公告横幅已并进右上角悬浮通知：公告弹窗只让位给首装向导、升级引导页与欢迎框，不再等页头横幅。
        self.assertNotIn("announcementBanner", PAGE_SOURCE)
        self.assertIn(
            "if ((setupWizard&&!setupWizard.hidden)||(updateGuide&&!updateGuide.hidden)"
            "||(onboarding&&!onboarding.hidden))",
            PAGE_SOURCE,
        )
        self.assertIn("var updateGuide=document.getElementById('update-guide');", PAGE_SOURCE)
        # 引导页没加进互斥判据时，两层遮罩会叠着弹出来。
        self.assertNotIn("if ((setupWizard&&!setupWizard.hidden)||(onboarding&&!onboarding.hidden))", PAGE_SOURCE)
        self.assertIn("setupWizard&&!setupWizard.hidden", PAGE_SOURCE)
        # 向导不再主动藏欢迎框（那会把 NapCat 封号风险提示推迟到第二次访问）
        self.assertNotIn("if(ob){ob.hidden=true;}", PAGE_SOURCE)
        self.assertIn('id="top-banner-row"', PAGE_SOURCE)
        # 标签不再截断成「最多 2 个 + N」：按原型全展开（chips 换行）。
        self.assertIn("var chips = el('div', 'chips');", PAGE_SOURCE)
        self.assertIn("body.appendChild(chips);", PAGE_SOURCE)
        self.assertNotIn("cardTags.slice(0,2)", PAGE_SOURCE)
        self.assertNotIn("tag-more", PAGE_SOURCE)
        self.assertIn('class="connect-actions-primary"', PAGE_SOURCE)
        self.assertIn('class="connect-actions-secondary"', PAGE_SOURCE)
        # 订阅群是 #connect 的兄弟卡片（自己一整块 surface），不再嵌在 QQ 卡片里：
        # 以前 #connect 少了 </section>，后面 5 张卡全被吞进一张 2700px 的 QQ 卡片。
        self.assertRegex(PAGE_SOURCE, r'<div id="steps" role="status" aria-live="polite"></div>\s*</section>\s*<section id="groupbox" class="subscription-panel"')
        # 二维码详情块仍在登录方式之前（DOM 顺序，不靠 order 补丁），二维码框自带虚线底。
        self.assertLess(PAGE_HTML.index('id="qq-qr-details"'), PAGE_HTML.index('class="login-choice"'))
        self.assertIn("#qrbox{margin-top:var(--space-3);padding:var(--space-3);border:1px dashed var(--line-strong);border-radius:var(--r-2);background:var(--paper);", PAGE_SOURCE)

    def test_announce_bell_and_panel_are_wired(self) -> None:
        self.assertEqual(PAGE_SOURCE.count('id="announce-bell"'), 1)
        self.assertIn('aria-haspopup="dialog"', PAGE_SOURCE)
        self.assertIn('aria-controls="announce-panel"', PAGE_SOURCE)
        self.assertEqual(PAGE_SOURCE.count('id="announce-panel"'), 1)
        panel_tag = self._tag("announce-panel")
        # 对话框语义 + 默认收起；类名/属性顺序不属于契约。
        self.assertIn('role="dialog"', panel_tag)
        self.assertIn('aria-labelledby="announce-panel-title"', panel_tag)
        self.assertIn(" hidden", panel_tag, "公告面板默认必须是收起的")
        for name in (
            "getAnnounceRead",
            "markAnnounceRead",
            "announceItems",
            "announcementsUnreadCount",
            "renderAnnounceBell",
            "announcePanelItemNode",
            "renderAnnouncePanel",
            "openAnnouncePanel",
            "closeAnnouncePanel",
            "announcePanelKeydown",
            "setupAnnounceBell",
        ):
            self.assertIn("function " + name + "(", PAGE_SOURCE, "缺少 " + name)
        self.assertIn("var ANNOUNCE_READ_KEY = 'nh_ann_read';", PAGE_SOURCE)
        self.assertIn("setupAnnounceModal();\nsetupAnnounceBell();", PAGE_SOURCE)
        # 标为已读要真生效：横幅、启动弹窗、徽标读同一份已读集合，不是纯视觉效果。
        self.assertIn("read.indexOf(id) !== -1 || dismissed.indexOf(id) !== -1 || seen.indexOf(id) !== -1", PAGE_SOURCE)
        self.assertIn("getAnnounceRead().includes(id)", PAGE_SOURCE)
        self.assertIn("renderAnnounceBell(); renderAnnouncePanel();", PAGE_SOURCE)
        # 正文来自网络：标题/正文只用 textContent，链接只走 safeUrl 白名单。
        self.assertIn("title.textContent = String(item.title || '公告')", PAGE_SOURCE)
        self.assertIn("var url = safeUrl(item.url);", PAGE_SOURCE)
        self.assertNotIn("announce-panel-list').innerHTML", PAGE_SOURCE)
        # 键盘与焦点：Esc 关闭、Tab 圈在面板内、点面板外关闭（点铃铛本身跳过）。
        self.assertIn("event.key === 'Escape'", PAGE_SOURCE)
        self.assertIn("announcePanelFocusable(document.getElementById('announce-panel'))", PAGE_SOURCE)
        self.assertIn("if (bell.contains && bell.contains(target)) return;", PAGE_SOURCE)
        # 小屏铺满，不在 390px 挤出侧边。
        # 小屏不铺满也不锁死左右：top/right 由 positionAnnouncePanel() 按铃铛位置写内联样式，
        # 390 下铃铛在底栏右侧，面板翻上来盖在铃铛上方；这里只放宽 max-height。
        self.assertIn("@media(max-width:760px){", PAGE_SOURCE)
        self.assertIn(".announce-panel{left:auto;width:auto;max-height:72vh}", PAGE_SOURCE)
        self.assertIn("width:min(420px,calc(100vw - 32px))", PAGE_SOURCE)
        # 面板定位跟铃铛走（不再写死从视口顶掉下来）。
        self.assertIn("function positionAnnouncePanel(", PAGE_SOURCE)
        self.assertIn("positionAnnouncePanel(panel, bell.getBoundingClientRect()", PAGE_SOURCE)
        # beta.17：铃铛必须真的出现在浮岛右端。以前外层 div 带着 hidden，全仓没人去掉，
        # 结果是「公告面板永远打不开」——这条锁住那个 bug 不许回来。
        self.assertNotIn('announce-bell-holder" hidden', PAGE_HTML)
        masthead = PAGE_HTML.index('class="masthead-row"')
        self.assertGreater(PAGE_HTML.index('id="announce-bell"'), masthead, "铃铛要在浮岛里")
        self.assertLess(PAGE_HTML.index('id="announce-bell"'), PAGE_HTML.index('id="top-banner-row"'))
        # 面板两个页签：通知（#notice-stack）与公告（原设置页那批公告），公告不再只藏在设置页深处。
        self.assertIn('id="announce-tab-notice"', PAGE_HTML)
        self.assertIn('id="announce-tab-announce"', PAGE_HTML)
        self.assertIn("function setAnnounceTab(", PAGE_SOURCE)
        self.assertIn('id="announce-pane-notice"', PAGE_HTML)
        self.assertIn('id="announce-pane-announce"', PAGE_HTML)
        # 公告要尽快到：页面 15 分钟拉一次（不是 6 小时），打开铃铛时再强制刷新（带 30 秒防抖）。
        self.assertIn("window.setInterval(refreshAnnouncements, 15 * 60 * 1000);", PAGE_SOURCE)
        self.assertNotIn("window.setInterval(refreshAnnouncements, 6 * 60 * 60 * 1000);", PAGE_SOURCE)
        self.assertIn("api('/api/announcements' + (force ? '?refresh=1' : ''))", PAGE_SOURCE)
        self.assertIn("refreshAnnouncements(true);", PAGE_SOURCE)

    def test_tunnel_panel_and_firewall_one_click_are_wired(self) -> None:
        """公网入口是后台长流程：页面只能点一次 start 再轮询，不能同步等，也不能假装成功。"""
        self.assertIn("api('/api/tunnel/start'", PAGE_SOURCE)
        self.assertIn("api('/api/tunnel/status'", PAGE_SOURCE)
        # 旧实现（同步等 /api/pair/public 后立刻报成功）必须消失，否则用户会看到假成功。
        self.assertNotIn("/api/pair/public", PAGE_SOURCE)
        self.assertNotIn("公网入口开通成功", PAGE_SOURCE)
        # 轮询要有定时器句柄、切走设置页能停、错误态能重试，不能把用户锁在弹层里。
        self.assertIn("var tunnelTimer = null;", PAGE_SOURCE)
        self.assertIn("function pollTunnel(immediate)", PAGE_SOURCE)
        self.assertIn("stopTunnelPolling()", PAGE_SOURCE)
        self.assertIn("document.body.dataset.tab !== 'settings'", PAGE_SOURCE)
        self.assertIn('id="tunnel-retry"', PAGE_SOURCE)
        self.assertIn("getElementById('tunnel-retry')", PAGE_SOURCE)
        # 旧 pair.js 那套独立状态机/轮询必须删干净，别留两套定时器。
        self.assertNotIn("TUNNEL_MAX_POLL_MS", PAGE_SOURCE)
        self.assertNotIn("tunnelCancel.addEventListener", PAGE_SOURCE)
        self.assertNotIn("tunnelRetry.addEventListener", PAGE_SOURCE)
        # 一键放行：服务只写请求，托盘提权；页面轮询 /api/firewall/status 看结果，不靠返回瞬间判定。
        self.assertIn("api('/api/firewall/allow'", PAGE_SOURCE)
        self.assertIn("api('/api/firewall/status'", PAGE_SOURCE)
        self.assertIn("window.setTimeout(tick, 2000);", PAGE_SOURCE)
        self.assertIn('id="pair-firewall-allow"', PAGE_SOURCE)

    def test_firewall_allow_call_carries_confirm(self) -> None:
        """写接口护栏：一键放行必须显式带 confirm: true，别被顺手改回裸 POST。"""
        self.assertIn('id="pair-firewall-allow"', PAGE_SOURCE)
        call = PAGE_SOURCE.find("api('/api/firewall/allow'")
        self.assertGreaterEqual(call, 0)
        confirm = PAGE_SOURCE.find("body: JSON.stringify({confirm: true})", call)
        self.assertGreaterEqual(confirm, 0)
        # 必须落在同一个调用点上，而不是页面别处的另一处 confirm。
        self.assertLessEqual(confirm - call, 300)

    def test_dingtalk_enabled_toggle_saves_immediately(self) -> None:
        """钉钉勾「启用」必须立刻落库（和微信一致）；只勾不点保存会让用户以为「未应用」。"""
        call = PAGE_SOURCE.find("saveEnabledToggle('dt-enabled'")
        self.assertGreaterEqual(call, 0, "钉钉启用开关没接到 saveEnabledToggle")
        line_end = PAGE_SOURCE.find("\n", call)
        self.assertIn("refreshDingtalkStatus", PAGE_SOURCE[call:line_end])
        # 保存成功后要重读状态行，否则开关已存、状态还写着「未启用」。
        define = PAGE_SOURCE.find("function saveEnabledToggle(")
        self.assertGreaterEqual(define, 0)
        signature = PAGE_SOURCE[define:PAGE_SOURCE.find(")", define) + 1]
        self.assertIn("after", signature)

    def test_run_info_shows_wechat_and_chaoxing_rows(self) -> None:
        """运行信息要同时看 QQ / 微信 / 学习通；数据源统一走 /api/overview。"""
        # 「运行信息」的标题现在由 settings.js 渲染（设置页里是个子面板），从脚本文本里找这一段。
        start = PAGE_SCRIPT.find("'运行信息'")
        self.assertGreaterEqual(start, 0)
        block = PAGE_SCRIPT[start:start + 2500]
        for label in ("QQ 监控群", "微信", "学习通"):
            self.assertIn(label, block)
        self.assertIn("api('/api/overview')", PAGE_SOURCE)
        self.assertIn("var describeSource = function", PAGE_SOURCE)
        # 换数据源后，「运行信息」这一段不许再各自打 wechat/chaoxing 状态接口。
        # 只框运行信息这块，不框整个 loadSettings：设置页的微信/学习通卡片仍要刷新自己的状态，
        # 那是另一件事，不能因此判红。
        self.assertNotIn("api('/api/wechat/status'", block)
        self.assertNotIn("api('/api/chaoxing/status'", block)

    def test_sticky_brand_top_banner_and_brand_home_button(self) -> None:
        masthead = PAGE_HTML.index('class="masthead-row"')
        tabs = PAGE_HTML.index('<nav class="tabs"')
        banners = PAGE_HTML.index('id="top-banner-row"')
        overview = PAGE_HTML.index('id="overview"')
        overview_end = PAGE_HTML.index('id="timeline-axis"', overview)
        bento = PAGE_HTML.index('<div class="bento"')
        # 原型顺序：浮岛（品牌 + 页签）→ 横幅区 → 今日概览 → bento。
        # 概览不再是全站抬头，它搬进了 main、只在「待办」页签显示。
        self.assertLess(masthead, tabs)
        self.assertLess(tabs, banners)
        self.assertLess(banners, overview)
        self.assertIn(".top-banner-row,.workspace,.bento{width:100%;max-width:var(--measure);margin-inline:auto;padding-inline:var(--gutter)}", PAGE_SOURCE)
        self.assertGreater(PAGE_HTML.index('<main>'), tabs)
        self.assertIn("body[data-tab]:not([data-tab=tasks]) .summary{display:none}", PAGE_SOURCE)
        self.assertIn(".top-banner-row,.workspace,.bento{width:100%;max-width:var(--measure);margin-inline:auto;padding-inline:var(--gutter)}", PAGE_SOURCE)
        self.assertIn(".top-banner-row{margin-top:var(--space-4);display:flex;flex-direction:column;gap:var(--space-2)}", PAGE_SOURCE)
        self.assertIn(".top-banner-row:not(:has(> :not([hidden]))){display:none}", PAGE_SOURCE)
        self.assertIn(".announcement-copy{min-width:0;white-space:normal}", PAGE_SOURCE)
        # 体检列表现在渲染在通知弹窗里：去掉外框与左侧竖线，交给弹窗卡片承载。
        self.assertIn(".setup-checkup{margin:0;padding:0;list-style:none}", PAGE_SOURCE)
        self.assertNotIn(".platform-setup-banner{", PAGE_SOURCE)
        # 悬浮通知的堆叠容器与卡片（右侧、纸面、语义色圆点）
        self.assertIn(".notice-stack{", PAGE_SOURCE)
        # 通知堆叠收进铃铛面板的内容区：原来那条「贴版心右边缘、从浮岛下沿起算」的
        # 悬浮定位必须消失，否则又会压住页面正文（历史用例名保留）。
        self.assertNotIn("right:calc(max(var(--gutter), (100vw - var(--measure)) / 2 + var(--gutter)));", PAGE_SOURCE)
        self.assertNotIn("top:calc(var(--island-pad) + var(--space-2));", PAGE_SOURCE)
        self.assertIn(".notice-card{", PAGE_SOURCE)
        # 语义级别只落在圆点上：卡片本体四边同一条 1px 线，不再画左侧色条。
        # info 是圆点默认色（teal），ok/warn/bad 各有一条覆盖。
        self.assertIn(".notice-dot{width:7px;height:7px;margin-top:6px;border-radius:var(--r-circle);background:var(--teal);flex:none}", PAGE_SOURCE)
        for level, token in (("ok", "ok"), ("warn", "warn"), ("bad", "bad")):
            self.assertIn(".notice-card[data-level=%s] .notice-dot{background:var(--%s)}" % (level, token), PAGE_SOURCE)
        self.assertNotIn("{border-left:3px solid", PAGE_SOURCE)
        self.assertIn("white-space:normal", PAGE_SOURCE, "横幅文案要换行，不许被省略号截断")
        css = PAGE_HTML.rsplit("<style>", 1)[1].split("</style>", 1)[0]
        # 通知栈只在面板里排流：不许再有任何固定定位或层级，盒子由面板内容区决定。
        stack_rule = css[css.index(".notice-stack{"):].split("}", 1)[0]
        self.assertIn("display:flex", stack_rule)
        for gone in ("position:fixed", "right:", "top:", "z-index"):
            self.assertNotIn(gone, stack_rule, "通知栈不该再有悬浮定位：%s" % stack_rule)
        # 浮岛必须是「内容自适应宽度的居中胶囊」：没有 width，只有 max-width。
        self.assertRegex(
            css,
            r"\.masthead-row\{[^}]*position:fixed;top:var\(--space-4\);left:50%;transform:translateX\(-50%\)"
            r"[^}]*max-width:calc\(100vw - var\(--space-8\)\)"
            r"[^}]*border:1px solid var\(--line\);border-radius:var\(--r-5\)"
            r"[^}]*background:color-mix\(in srgb,var\(--paper-raise\) 82%,transparent\)"
            r"[^}]*backdrop-filter:saturate\(180%\) blur\(20px\)",
        )
        island_rule = _css_rule(css, ".masthead-row{")
        self.assertNotRegex(island_rule, r"(?<![a-z-])width:", "浮岛不许给宽度，否则会撑成横贯版心的宽带")
        self.assertIn(".masthead-row:hover,.masthead-row:focus-within{box-shadow:inset 0 1px 0 var(--sheen),0 4px 18px rgba(40,40,28,.10)}", css)
        self.assertIn(".island-sep{width:1px;align-self:stretch;margin:var(--space-1) 0;background:var(--line);flex:none}", css)
        self.assertIn('class="island-sep" aria-hidden="true"', PAGE_SOURCE)
        # 竖分隔线两条：品牌↔页签、页签↔消息铃铛。窄屏（≤760）靠 .island-sep{display:none} 一起隐藏。
        self.assertEqual(PAGE_SOURCE.count('class="island-sep" aria-hidden="true"'), 2, "浮岛里应有两条竖分隔线")
        self.assertIn(
            'class="island-sep"',
            PAGE_SOURCE[PAGE_SOURCE.index("</nav>"):PAGE_SOURCE.index('<div class="masthead-tools">')],
            "页签与消息铃铛之间也要一条竖分隔线与品牌侧同款",
        )
        # 连续曲率圆角：原生 corner-shape，旧浏览器由 @supports 自动退回标准圆弧。
        self.assertIn("@supports (corner-shape:squircle){*{corner-shape:squircle}}", css)
        # 页签组间距 2px、字重 400（原型没有加粗）
        self.assertIn(".tabs{display:flex;gap:2px}", css)
        self.assertIn(".tabs button,.tabs button[role=tab]{", css)
        self.assertIn("min-height:30px;padding:0 var(--space-4);", css)
        self.assertIn("font-size:var(--fs-3);font-weight:400;white-space:nowrap;", css)
        # 760 窄屏：逐条盯「谁在缩」，整块媒体查询的书写顺序不算契约。
        # 页面上只有一个 760 块，但按「所有 760 块拼起来」找更稳，将来拆块也不会误红。
        narrow_parts, cursor = [], 0
        while True:
            cursor = css.find("@media(max-width:760px)", cursor)
            if cursor < 0:
                break
            narrow_parts.append(_css_block(css[cursor:], "@media(max-width:760px){"))
            cursor += 1
        self.assertTrue(narrow_parts, "缺 760 窄屏的媒体查询")
        narrow = "".join(narrow_parts)
        # 窄屏把浮岛改成底部标签栏（原型 760 段的做法）：贴底、去圆角、图标在文字上方。
        for needle, why in (
            ("top:auto;bottom:0;left:0;right:0;transform:none;", "窄屏浮岛要贴到底部并取消居中位移"),
            ("max-width:none;border-radius:0;border-left:0;border-right:0;border-bottom:0;", "底栏通宽、不要圆角与左右边"),
            (".island-sep{display:none}", "底栏里不画竖分隔线"),
            (".tabs{flex:1;justify-content:space-around;min-width:0}", "页签均分底栏宽度"),
            ("font-size:10px;min-height:48px;", "底栏页签是 48px 触摸目标"),
            (".tabs button svg{width:19px;height:19px}", "底栏图标放大到 19px"),
            (".tabs button.active,.tabs button[aria-selected=true]{background:transparent;color:var(--teal)}", "底栏选中态只改字色，不铺底色"),
            (".brand strong{font-size:var(--fs-3)}", "窄屏品牌字降一档，否则一行放不下"),
            ("--island-pad:var(--space-6)", "底栏在下方，顶部不再留 88px 空带"),
            ("padding-bottom:calc(72px + env(safe-area-inset-bottom))", "底部要让出标签栏高度"),
        ):
            self.assertIn(needle, narrow, why)
        self.assertIn('<header class="top-shell" role="banner">', PAGE_SOURCE)
        self.assertNotIn('<header class="masthead" role="banner">', PAGE_SOURCE)
        self.assertIn('id="brand-home"', PAGE_SOURCE)
        self.assertIn('aria-label="回到页面顶部"', PAGE_SOURCE)
        self.assertIn("window.scrollTo({top:0,behavior:paused?'auto':'smooth'})", PAGE_SOURCE)
        self.assertIn('<span class="brand-logo" id="brand-mark" aria-hidden="true">', PAGE_SOURCE)
        self.assertNotIn('<button class="brand-mark-button"', PAGE_SOURCE)

    def test_dashboard_first_view_is_single_layer_and_readable(self) -> None:
        self.assertIn('id="top-banner-row"', PAGE_SOURCE)
        # 公告横幅已并进右上角悬浮通知：公告弹窗只让位给首装向导、升级引导页与欢迎框，不再等页头横幅。
        self.assertNotIn("announcementBanner", PAGE_SOURCE)
        self.assertIn(
            "if ((setupWizard&&!setupWizard.hidden)||(updateGuide&&!updateGuide.hidden)"
            "||(onboarding&&!onboarding.hidden))",
            PAGE_SOURCE,
        )
        self.assertIn("var updateGuide=document.getElementById('update-guide');", PAGE_SOURCE)
        self.assertNotIn("if ((setupWizard&&!setupWizard.hidden)||(onboarding&&!onboarding.hidden))", PAGE_SOURCE)
        self.assertIn("setupWizard&&!setupWizard.hidden", PAGE_SOURCE)
        self.assertIn("var noticeModal=document.getElementById('announce-modal')", PAGE_SOURCE)
        # 欢迎框带着封号风险提示，打开向导时不能顺手把它藏掉
        self.assertNotIn("if(ob){ob.hidden=true;}", PAGE_SOURCE)
        # 状态标签全展开（原型 .chips），不再「最多 2 个 + N」。
        self.assertIn("function addChip(css, label)", PAGE_SOURCE)
        self.assertNotIn("cardTags.slice(0,2)", PAGE_SOURCE)
        self.assertIn('class="connect-actions-primary"', PAGE_SOURCE)
        self.assertIn('class="connect-actions-secondary"', PAGE_SOURCE)
        # 订阅群是 #connect 的兄弟卡片（自己一整块 surface），不再嵌在 QQ 卡片里：
        # 以前 #connect 少了 </section>，后面 5 张卡全被吞进一张 2700px 的 QQ 卡片。
        self.assertRegex(PAGE_SOURCE, r'<div id="steps" role="status" aria-live="polite"></div>\s*</section>\s*<section id="groupbox" class="subscription-panel"')
        self.assertIn(".connect-actions{display:flex;flex-wrap:wrap;gap:var(--space-2);margin-top:var(--space-3)}", PAGE_SOURCE)
        self.assertIn("font-family:var(--font-sans);font-size:var(--fs-4);line-height:1.65;", PAGE_SOURCE)

    def test_star_support_entry_points_are_wired(self) -> None:
        self.assertIn('id="star-prompt"', PAGE_SOURCE)
        self.assertIn('id="star-prompt-never"', PAGE_SOURCE)
        self.assertEqual(PAGE_SOURCE.count('id="rail-about"'), 1)
        self.assertIn('https://github.com/ExpertKT/notice-hub', PAGE_SOURCE)
        self.assertIn('setupStarPrompt();', PAGE_SOURCE)
        self.assertIn('star-note', PAGE_SOURCE)

    def test_pinned_panel_and_pin_control_are_wired(self) -> None:
        self.assertIn('id="pinned-panel"', PAGE_SOURCE)
        self.assertIn('id="pinned-list"', PAGE_SOURCE)
        self.assertIn('id="pinned-empty"', PAGE_SOURCE)
        self.assertIn("还没有置顶日程。在待办卡片里点「置顶」，就会固定到这里。", PAGE_SOURCE)
        # 置顶面板不许再挂浮球入口（beta.12 撤下时的契约）。2026-11-06 起浮球开关
        # 按负责人指令回到设置页托管卡（settings.js），所以 page.html 半边照旧锁全量；
        # PAGE_SCRIPT 半边只锁 renderPinned 函数体——设置页开关文案在 settings.js，不算。
        self.assertNotIn("浮球", PAGE_HTML)
        pinned_source = PAGE_HTML if "function renderPinned(data)" in PAGE_HTML else PAGE_SCRIPT
        render_pinned = pinned_source.split("function renderPinned(data)", 1)[1]
        render_pinned = render_pinned.split("\nfunction ", 1)[0]
        self.assertNotIn("浮球", render_pinned)
        self.assertIn("function renderPinned(data)", PAGE_SOURCE)
        self.assertIn("renderPinned(data);", PAGE_SOURCE)
        self.assertIn("'/api/tasks/pin'", PAGE_SOURCE)
        self.assertIn("pin-toggle", PAGE_SOURCE)
        # 置顶面板必须在 QQ 接入上方（用户要求：月历右边、QQ 接入上面）
        self.assertLess(PAGE_HTML.index('id="pinned-panel"'), PAGE_HTML.index('id="connect"'))
        # 紧急按钮不再写死宽度：min-width:88px 会让两个字比按钮窄一大截
        self.assertNotIn("min-width:88px", PAGE_SOURCE)


    def test_new_task_cards_and_calendar_month_animate(self) -> None:
        self.assertIn(".task.is-new{animation:task-enter", PAGE_SOURCE)
        self.assertIn("@keyframes task-enter{from{opacity:0", PAGE_SOURCE)
        self.assertIn("var lastRenderedTaskIds = null;", PAGE_SOURCE)
        self.assertIn("if (previousIds && !previousIds[cardId]) card.classList.add('is-new');", PAGE_SOURCE)
        # 翻月改成 #calendar 自己的 calendar-turn 动画（--turn-x 控方向），不再靠 transition:transform
        self.assertIn("#calendar.calendar-turn{animation:calendar-turn 240ms var(--ease-out) both}", PAGE_SOURCE)
        self.assertIn("root.classList.add('calendar-turn')", PAGE_SOURCE)


    def test_overview_keeps_today_headline_and_completion(self) -> None:
        """/api/tasks 本来就带 headline/subline/progress，概览缺 DOM 就等于这些字没人显示。

        曾经的坑：render() 用 `if (headline)` 兜着，元素被删掉时页面不报错、标题和完成率
        悄悄消失，只有肉眼能发现。所以这里把元素和赋值两半都钉住。
        """
        overview = PAGE_SOURCE.split('id="overview"', 1)[1].split("</section>", 1)[0]
        for marker in ('id="headline"', 'id="subline"', 'id="progressLabel"',
                       '<i id="progress">', 'role="progressbar"', '今日已完成'):
            self.assertIn(marker, overview)
        self.assertIn("headline.textContent = data.headline || '今天没有待办'", PAGE_SOURCE)
        self.assertIn("progressLabel.textContent = progress + '%'", PAGE_SOURCE)
        self.assertIn("progressEl.style.transform = 'scaleX(' + boundedProgress / 100 + ')'", PAGE_SOURCE)
        # 概览抬头在前，整块待办板排在概览之后（列表已经独立成块，不再塞进概览卡片里——
        # 概览在待机时限高 32vh 且 overflow:hidden，装进去的卡片会被裁成半截）。
        self.assertLess(overview.index('id="headline"'), overview.index('id="progress"'))
        self.assertLess(PAGE_SOURCE.index('id="overview"'), PAGE_SOURCE.index('class="task-board-block"'))
        self.assertLess(PAGE_SOURCE.index('id="headline"'), PAGE_SOURCE.index('id="timeline-board"'))
        self.assertLess(PAGE_SOURCE.index('id="progress"'), PAGE_SOURCE.index('id="timeline-board"'))
        self.assertNotIn('id="timeline-board"', overview,
                         "待办列表塞回概览卡片就会被限高裁掉，390 下整块看不见")


    def test_overview_timeline_keeps_span_parser_and_one_focus_ring(self) -> None:
        """概览换成时间轴后，仍要靠时间窗口判「进行中」；焦点环全站只写一条。"""
        css = self._current_stylesheet()
        self.assertIn('id="timeline-axis"', PAGE_SOURCE)
        self.assertIn('id="timeline-board"', PAGE_SOURCE)
        self.assertIn('>待办概览</span>', PAGE_SOURCE)
        self.assertIn('id="tab-detail-button"', PAGE_SOURCE)
        self.assertIn('id="agent-dock"', PAGE_SOURCE)
        # 时间段解析只有一处实现：起止两个时刻 + 中间的连接符；同一时刻重复出现不算窗口。
        self.assertIn("function parseTimeWindow(task) {", PAGE_SOURCE)
        self.assertIn(r"var pattern = /(\d{1,2})\s*[:：]\s*(\d{2})/g;", PAGE_SOURCE)
        self.assertIn("if (!/[-–—~～]|至/.test(between)) return null;", PAGE_SOURCE)
        self.assertIn("if (end === start) return null;", PAGE_SOURCE)
        self.assertIn("function windowBounds(window_, now) {", PAGE_SOURCE)
        self.assertIn("var timeWindow = parseTimeWindow(task);", PAGE_SOURCE)
        self.assertIn("addChip('soon', '进行中');", PAGE_SOURCE)
        # 键盘可达：焦点环由全站统一的 :focus-visible 规则给，不再为个别按钮单写。
        self.assertIn("button:focus-visible,input:focus-visible", css)
        self.assertIn("outline:2px solid var(--teal)!important;outline-offset:2px!important", css)

    def test_agent_dock_uses_task_actions(self) -> None:
        """对话框改的是待办接口本身。删除和清空必须先复述，不能一句就动数据。"""
        self.assertIn("'/api/tasks/urgent'", PAGE_SOURCE)
        self.assertIn("'/api/tasks/clear-finished'", PAGE_SOURCE)
        self.assertIn("'/api/tasks/pin'", PAGE_SOURCE)
        self.assertIn("agentAct(task, 'snooze'", PAGE_SOURCE)
        self.assertIn("agentAct(task, 'reopen'", PAGE_SOURCE)
        self.assertIn("agentAct(task, 'delete'", PAGE_SOURCE)
        self.assertIn("agentAct(task, 'confirm'", PAGE_SOURCE)
        self.assertIn("correction: 'deadline'", PAGE_SOURCE)
        self.assertIn("再发一次「清空已完成」才清", PAGE_SOURCE)
        self.assertIn("再发一次「删除 '", PAGE_SOURCE)
        self.assertIn('class="agent-voice"', PAGE_SOURCE)
        self.assertIn("voice.setAttribute('aria-label', '不能语音输入')", PAGE_SOURCE)
        self.assertNotIn("data-agent-say=", PAGE_SOURCE)
        self.assertNotIn("el('button', 'check'", PAGE_SOURCE)
        self.assertIn(".timeline-lane.is-collapsed{flex:0 0 8px", PAGE_SOURCE)
        self.assertIn(".timeline-lane.is-collapsed .timeline-seg{width:8px;height:2px;margin-top:18px;border-radius:0}", PAGE_SOURCE)
        # 日期分隔线是线不是进度条：8px 药丸撑满整行时会跟概览那条真进度条撞脸。
        self.assertIn(".timeline-seg{height:2px;margin-top:18px;border-radius:0;background:var(--teal)", PAGE_SOURCE)
        self.assertIn('data-state="idle"', PAGE_SOURCE)
        self.assertIn("countNode.textContent = '待办' + shown + '件'", PAGE_SOURCE)
        self.assertNotIn("count.textContent = '待办' + brief.count + '件'", PAGE_SOURCE)
        self.assertIn('id="agent-count"', PAGE_SOURCE)
        self.assertNotIn("agentLine('bot', '待办' + brief.count + '件')", PAGE_SOURCE)
        self.assertIn(".agent-dock[data-state=idle] .agent-count{position:absolute;top:0;left:0;right:0;pointer-events:none}", PAGE_SOURCE)
        self.assertIn("#agent-send{height:40px;min-height:40px", PAGE_SOURCE,
                      "发送按钮与触控审计一致，保留至少40px高度")
        wake = PAGE_SOURCE.split("function wakeAgentDock()", 1)[1].split("function initAgentDock()", 1)[0]
        self.assertNotIn("setTimeout", wake)
        self.assertNotIn(".style.", wake)
        self.assertIn("--ease-drawer:cubic-bezier(0.32,0.72,0,1)", PAGE_SOURCE)
        self.assertIn("transition:width 260ms var(--ease-drawer),padding 260ms var(--ease-drawer)", PAGE_SOURCE)
        self.assertIn("transition:height 260ms var(--ease-drawer)", PAGE_SOURCE)
        self.assertIn(".agent-log{transform:none!important;transition:opacity 150ms linear!important}", PAGE_SOURCE)
        self.assertIn('id="agent-collapse"', PAGE_SOURCE)
        self.assertIn("dock.dataset.state = 'idle'", wake)
        self.assertIn(".agent-dock[data-state=idle] .agent-head{display:none}", PAGE_SOURCE)
        self.assertIn("e.key === 'Escape'", PAGE_SOURCE)
        # 只有真的显示着的模态框才该拦 Esc：#onboarding 内层的 [aria-modal] 节点自身没有 hidden，
        # 父级 hidden 时 `:not([hidden])` 照样命中，对话面板就永远收不起来（旧守卫的坑）。
        modal_guard = PAGE_SOURCE.split("function agentModalOpen()", 1)[1].split("function initAgentDock()", 1)[0]
        self.assertIn("!list[i].hidden && list[i].getClientRects().length", modal_guard)
        self.assertNotIn('[aria-modal="true"]:not([hidden])', PAGE_SOURCE)
        self.assertIn("dock.dataset.state === 'awake' && !agentModalOpen()", PAGE_SOURCE)
        self.assertIn("!(task.effective_urgent && category === 'urgent')", PAGE_SOURCE)
        self.assertIn("if (task.overdue || key === todayKey) buckets.today.push(task);", PAGE_SOURCE)
        self.assertIn(".timeline-board{display:flex;gap:var(--space-1);overflow-x:auto;", PAGE_SOURCE)
        self.assertIn("scrollbar-width:none", PAGE_SOURCE)
        self.assertIn(".timeline-board::-webkit-scrollbar{display:none", PAGE_SOURCE)
        self.assertIn("board.scrollLeft += delta", PAGE_SOURCE)
        self.assertIn(".timeline-lane{flex:0 0 auto;min-width:max-content;", PAGE_SOURCE)
        self.assertIn("seg.style.width = cards.getBoundingClientRect().width + 'px'", PAGE_SOURCE)
        self.assertIn("body:not([data-tab]) .summary,body[data-tab=tasks] .summary{flex:none;min-width:0;", PAGE_SOURCE)
        self.assertIn(".timeline-cards{display:flex;gap:var(--space-2);align-items:stretch;flex:1 1 auto;margin:0;padding:0 0 var(--space-1);list-style:none}\n", PAGE_SOURCE)
        self.assertIn(".timeline-cards .task{flex:0 0 240px;width:240px;min-width:240px;max-width:240px;", PAGE_SOURCE)
        self.assertIn(".timeline-cards .task .acts{flex-wrap:wrap;margin-top:auto;", PAGE_SOURCE)
        self.assertIn("if (undated.length) columns.push({label: '以后', tasks: undated})", PAGE_SOURCE)
        self.assertNotIn("applyAgentCount(pair[1])", PAGE_SOURCE)
        self.assertIn("node.style.opacity = String(1 - wakeEase(t))", PAGE_SOURCE)
        # 点对话框任意空白处也要进对话态；按钮/输入框交给它们自己的处理，别抢
        self.assertIn("dock.addEventListener('click', function (event) {", PAGE_SOURCE)
        self.assertIn("if (event.target.closest('button, a, input, textarea, select, label')) return;", PAGE_SOURCE)
        # 点别处要收回：只有「收起」和 Esc 两条路时，文档许诺的「点别处回来还是那张图」是假话。
        self.assertIn("if (dock.dataset.state !== 'awake') return;", PAGE_SOURCE)
        self.assertIn("if (event.target.closest && event.target.closest('#agent-dock')) return;", PAGE_SOURCE)
        self.assertIn("document.addEventListener('click', function (event) {", PAGE_SOURCE)
        self.assertIn("runTimelineFrames(token, 280,", PAGE_SOURCE)
        self.assertIn("runTimelineFrames(token, 360,", PAGE_SOURCE)
        self.assertIn("lane.dataset.lane = column.label", PAGE_SOURCE)
        self.assertIn("api('/api/agent/chat'", PAGE_SOURCE)
        self.assertIn("nh-agent-session", PAGE_SOURCE)
        self.assertIn("result.fallback", PAGE_SOURCE)
        # 后端回 fallback（模型没连上/没配钥匙）时前端会退到固定说法，必须当场说明，
        # 不让关键词匹配冒充「智能体在决定」。措辞不写「本机」：同一个界面也连云端模型。
        self.assertIn("模型这会儿没连上，先用固定说法回你", PAGE_SOURCE)
        self.assertNotIn("本机模型这会儿没连上", PAGE_SOURCE)
        # 固定说法只在「fallback 且没有答案」时才顶：按本地规则算出来的 say 与 trace 要照原样显示。
        self.assertIn("if (!result || (result.fallback && !result.say)) {", PAGE_SOURCE)
        self.assertIn("（没有模型，这句按本地规则办）", PAGE_SOURCE)
        self.assertIn("nh-agent-log", PAGE_SOURCE)
        self.assertIn("restoreAgentLog()", PAGE_SOURCE)
        paint = PAGE_SOURCE.split("function paintAgentHistory", 1)[1].split("function restoreAgentLog", 1)[0]
        self.assertNotIn("dataset.state = 'awake'", paint)
        self.assertIn("dock.dataset.state = 'idle'", PAGE_SOURCE)
        self.assertIn("正在回复", PAGE_SOURCE)
        self.assertIn("className = 'agent-line agent-line-bot agent-wait'", PAGE_SOURCE)
        self.assertIn("handleAgent(raw, true)", PAGE_SOURCE)
        submit = PAGE_SOURCE.split("function submitAgent", 1)[1].split("function handleAgent", 1)[0]
        self.assertLess(submit.find("agentLine('user', raw)"), submit.find("api('/api/agent/chat'"))
        self.assertLess(submit.find("agentWait()"), submit.find("api('/api/agent/chat'"))

    def test_calendar_shows_time_caps_three_events_and_opens_a_day_panel(self) -> None:
        css = self._current_stylesheet()
        self.assertIn("var MAX_CALENDAR_EVENTS=3;", PAGE_SOURCE)
        self.assertIn("cell.setAttribute('data-has-events','true')", PAGE_SOURCE)
        self.assertIn("cell.setAttribute('tabindex','0')", PAGE_SOURCE)
        self.assertIn("cell.setAttribute('aria-controls','calendar-detail')", PAGE_SOURCE)
        # 格子里只画圆点：一件待办一个点，最多 3 个；文字与「+N」都不再进格子。
        self.assertIn("evs.className='calendar-evs'", PAGE_SOURCE)
        self.assertIn(
            "items.slice(0,MAX_CALENDAR_EVENTS).forEach(function(){evs.appendChild(document.createElement('i'));})",
            PAGE_SOURCE,
        )
        self.assertIn("cell.appendChild(evs)", PAGE_SOURCE)
        self.assertIn("cell.classList.add('today')", PAGE_SOURCE)
        self.assertNotIn("chip.textContent=text;", PAGE_SOURCE)
        self.assertNotIn("more.className='calendar-more'", PAGE_SOURCE)
        self.assertIn("function openCalendarDay(cell,key,day)", PAGE_SOURCE)
        self.assertIn("function closeCalendarDetail()", PAGE_SOURCE)
        # 每一天的点击/键盘处理器必须绑定到本迭代的日期，否则所有格子都会打开最后一天（var 捕获事故）
        self.assertIn("for(let d=1;d<=days;d++){let key=dayKey(y,m,d);", PAGE_SOURCE)
        self.assertIn('id="calendar-detail"', PAGE_SOURCE)
        # 格子里只画圆点；文字事项与「+N」连同它们的样式一起消失，全部内容进当天详情面板。
        self.assertIn(".calendar-evs{position:absolute;bottom:5px;left:50%;transform:translateX(-50%);display:flex;gap:3px}", css)
        self.assertIn(".calendar-evs i{width:4px;height:4px;border-radius:var(--r-circle);background:var(--teal)}", css)
        self.assertNotIn(".calendar-event{", css)
        self.assertNotIn(".calendar-more{", css)
        self.assertIn(".calendar-detail ul{margin:var(--space-1) 0 0;padding:0;list-style:none}", css)
        self.assertIn("overflow:auto", _css_rule(css, ".calendar-detail{"))

    def test_calendar_day_panel_toggles_shut_and_has_a_close_button(self) -> None:
        css = self._current_stylesheet()
        # 再点同一个日期格必须收起（此前一进来就 closeCalendarDetail() 再打开，等于永远关不掉）
        self.assertIn("if(cell.classList.contains('is-open')){closeCalendarDetail(); return;}", PAGE_SOURCE)
        self.assertIn("head.className='calendar-detail-head'", PAGE_SOURCE)
        self.assertIn("close.className='calendar-detail-close'", PAGE_SOURCE)
        self.assertIn("close.textContent='收起'", PAGE_SOURCE)
        self.assertIn("close.addEventListener('click',function(){closeCalendarDetail(); cell.focus();})", PAGE_SOURCE)
        self.assertIn("calendarDetail.appendChild(head);", PAGE_SOURCE)
        self.assertNotIn("calendarDetail.appendChild(heading);", PAGE_SOURCE)
        self.assertIn(".calendar-detail-head{", css)
        self.assertIn(".calendar-detail-close{", css)
        self.assertIn(".calendar-detail-close{min-height:0;padding:0;border:0;background:none;color:var(--faint);font-size:var(--fs-2)}", css)

    def test_task_reflow_and_calendar_detail_animate_instead_of_jumping(self) -> None:
        """勾选待办后剩余卡片上移、月历当天详情展开/收起都必须有过渡（用户报的硬切）。"""
        css = self._current_stylesheet()
        # 卡片重排用 FLIP：重建前记文档坐标，重建后把卡片从旧位置平移回新位置再过渡
        self.assertIn("var beforeRects = Object.create(null);", PAGE_SOURCE)
        # 记的是文档坐标（加上滚动量）和所在分组；算式写法不算契约，这两个字段才是。
        self.assertIn("beforeRects[card.id] = {", PAGE_SOURCE)
        self.assertIn("left: rect.left + window.scrollX", PAGE_SOURCE)
        self.assertIn("top: rect.top + window.scrollY", PAGE_SOURCE)
        self.assertIn("section: sectionKey(card)", PAGE_SOURCE)
        self.assertIn("if (!dx && !dy) return;", PAGE_SOURCE)
        # 换分组的卡片（例如刚完成、去「已完成」）不参与 FLIP，否则会横跨整页飞几千像素
        self.assertIn("if (!before || before.section !== sectionKey(card)) return;", PAGE_SOURCE)
        self.assertIn("return title ? title.textContent.trim() : '';", PAGE_SOURCE)
        self.assertIn("card.style.transition = 'transform 240ms var(--ease-out)';", PAGE_SOURCE)
        self.assertIn("card.addEventListener('transitionend', clearFlip, {once: true});", PAGE_SOURCE)
        # 过渡没被真正启动（帧被节流/降级）时也必须把内联样式收干净，否则残留 transform 会错位
        self.assertIn("window.setTimeout(clearFlip, 280);", PAGE_SOURCE)
        # 关动效/暂停时不退化成「瞬移」
        self.assertIn("if (!motionIsPaused()) {", PAGE_SOURCE)
        # 详情面板的空间也要过渡，否则收起时下面的内容瞬间跳上来
        self.assertIn("max-height:var(--detail-h,640px)", css)
        self.assertIn("calendarDetail.style.setProperty('--detail-h',calendarDetail.offsetHeight+'px');", PAGE_SOURCE)
        self.assertIn("calendarDetail.style.removeProperty('--detail-h');", PAGE_SOURCE)
        # 原型形态：详情是「虚线分隔 + 衬线日号」的开放块，不再是卡片；格子里只画圆点。
        for needle in (
            ".calendar-detail{\n  margin-top:var(--space-3);padding-top:var(--space-3);\n"
            "  border:0;border-top:1px dashed var(--line);border-radius:0;background:transparent;\n"
            "  color:var(--dim);font-size:var(--fs-3);max-height:var(--detail-h,640px);overflow:auto;\n}",
            ".calendar-detail h3{margin:0;font-family:var(--font-serif);font-size:var(--fs-4);color:var(--ink);font-weight:600}",
            ".calendar-detail h3 span{font-family:var(--font-sans);font-size:var(--fs-2);color:var(--faint);font-weight:400;margin-left:var(--space-2)}",
            ".calendar-evs{position:absolute;bottom:5px;left:50%;transform:translateX(-50%);display:flex;gap:3px}",
            ".calendar-evs i{width:4px;height:4px;border-radius:var(--r-circle);background:var(--teal)}",
            ".calendar-detail ul{margin:var(--space-1) 0 0;padding:0;list-style:none}",
            ".calendar-detail li{padding-left:var(--space-3);text-indent:calc(-1 * var(--space-3))}",
            ".calendar-detail li::before{content:\"· \"}",
            ".calendar-detail time{color:var(--faint);font-variant-numeric:tabular-nums;margin-right:var(--space-1)}",
        ):
            self.assertIn(needle, css)
        self.assertNotIn(".calendar-event{", css)
        self.assertNotIn(".calendar-more{", css)

    def test_pinned_panel_can_be_resized_and_remembers_the_height(self) -> None:
        css = self._current_stylesheet()
        self.assertIn('id="pinned-resize"', PAGE_SOURCE)
        self.assertIn('role="separator"', PAGE_SOURCE)
        self.assertIn('aria-valuenow="280"', PAGE_SOURCE)
        self.assertIn("function initPinnedResize()", PAGE_SOURCE)
        self.assertIn("list.style.maxHeight=height+'px'", PAGE_SOURCE)
        self.assertIn("localStorage.setItem(storeKey,String(height))", PAGE_SOURCE)
        self.assertIn("localStorage.getItem(storeKey)", PAGE_SOURCE)
        self.assertIn("handle.addEventListener('keydown'", PAGE_SOURCE)
        self.assertIn("handle.addEventListener('pointermove'", PAGE_SOURCE)
        self.assertIn(".pinned-resize{display:flex", css)
        self.assertIn(".pinned-resize::before{content:\"\";position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:32px;height:4px;border-radius:var(--r-full);background:var(--sand)}", css)
        # 焦点态换把手颜色（沙色 → 鼠尾草）；拖动中的类由 JS 打，样式层不再靠描边变色提示。
        self.assertIn(".pinned-resize:focus-visible::before{background:var(--teal)}", css)
        self.assertIn("panel.classList.add('is-resizing')", PAGE_SOURCE)
        self.assertNotIn(".pinned-panel.is-resizing .pinned-resize{", css)

    def test_hosting_shortcut_and_onboarding_journey_are_wired(self) -> None:
        self.assertIn('id="host-state"', PAGE_SOURCE)
        self.assertIn('id="host-quick"', PAGE_SOURCE)
        self.assertIn('id="journey"', PAGE_SOURCE)
        self.assertIn('id="journey-steps"', PAGE_SOURCE)
        self.assertIn('id="journey-action"', PAGE_SOURCE)
        self.assertIn('id="journey-dismiss"', PAGE_SOURCE)
        self.assertIn("function fetchHostingStatus()", PAGE_SOURCE)
        self.assertIn("api('/api/hosting/'+desired", PAGE_SOURCE)
        self.assertIn("status.hosting_active", PAGE_SOURCE)
        self.assertIn("status.napcat_running", PAGE_SOURCE)
        self.assertIn("status.napcat_online", PAGE_SOURCE)
        self.assertIn("Number(status.groups_selected||0)>0", PAGE_SOURCE)
        self.assertIn("Array.isArray(info.calendars)&&info.calendars.length", PAGE_SOURCE)
        self.assertIn("localStorage.getItem('qq_digest_journey_hidden')", PAGE_SOURCE)
        self.assertIn("window.setInterval(fetchHostingStatus, 10000)", PAGE_SOURCE)
        self.assertIn("initPinnedResize();", PAGE_SOURCE)
        self.assertIn("initJourney();", PAGE_SOURCE)
        self.assertLess(PAGE_HTML.index('id="journey"'), PAGE_HTML.index('id="action-feedback"'))
        # 两条提示条一套尺寸；操作提示放在正文滚动区之外——在 main 里会被滚动条挤掉 15px 宽度、
        # 被 flex 压缩 2px 高度，右缘和高度跟页头那条更新提示对不齐。
        self.assertLess(PAGE_HTML.index('id="action-feedback"'), PAGE_HTML.index('<main>'))
        self.assertIn(".action-feedback,.announcement-banner{\n  min-height:64px;line-height:1.6;", PAGE_SOURCE)
        self.assertIn("display:flex;align-items:center;gap:var(--space-3);padding:var(--space-3) var(--space-4);", PAGE_SOURCE)
        self.assertIn("background:var(--paper-raise);font-size:var(--fs-4);", PAGE_SOURCE)
        self.assertIn(".announcement-actions .btn,.feedback-actions button{min-height:40px;min-width:40px;padding:0 var(--space-3);font-size:var(--fs-4);line-height:1.6}", PAGE_SOURCE)
        self.assertIn(".action-feedback,.announcement-banner{flex-wrap:wrap}", PAGE_SOURCE)
        self.assertIn(".announcement-actions,.feedback-actions{width:100%;justify-content:flex-end}", PAGE_SOURCE)

    def test_mobile_overview_is_one_row_and_opens_a_drawer(self) -> None:
        # 手机上概览只占一行：三格数字 + 明细按钮，原来的大卡片收进浮层，页面高度不动。
        strip = PAGE_HTML.split('<div class="ov-strip">', 1)[1].split('<div id="ov-detail"', 1)[0]
        for cell in ('id="ov-trips"', 'id="ov-due"', 'id="ov-done"'):
            self.assertIn(cell, strip)
        self.assertIn('id="ov-more" class="ov-more" type="button" aria-expanded="false" aria-controls="ov-detail"', PAGE_HTML)
        # 大卡片留在 #ov-detail 里，顺带保住概览切片里 headline < timeline-board < progress 的顺序。
        self.assertIn('<button id="ov-close"', PAGE_HTML)
        self.assertLess(PAGE_HTML.index('id="ov-detail"'), PAGE_HTML.index('id="timeline-board"'))
        self.assertLess(PAGE_HTML.index('id="ov-detail"'), PAGE_HTML.index('<i id="progress">'))
        # 宽屏不出现这行：基础规则 display:none，只有 760px 块里放出来。
        self.assertIn(".ov-strip,.ov-close{display:none}", PAGE_SOURCE)
        # 手机上是一句话而不是三个格子：标签在前、数字在后，中间用 · 连，等高对齐基线。
        self.assertIn(".ov-strip{display:flex;flex-wrap:wrap;align-items:baseline;gap:0 var(--space-1);min-height:0}", PAGE_SOURCE)
        self.assertIn(".ov-cell{display:inline-flex;align-items:baseline;gap:var(--space-1);flex:0 1 auto;min-width:0}", PAGE_SOURCE)
        # 分隔点自己有 order:-3，比标签的 -2 还靠前；留 0 会落成「最近 · 20:00」。
        self.assertIn(".ov-cell+.ov-cell::before{content:'·';color:var(--faint);order:-3}", PAGE_SOURCE)
        # order 必须比标签（-2）靠后，否则「·」会落到标签和数字中间，读成「最近 · 20:00」。
        self.assertIn(".ov-cell span{order:-2;", PAGE_SOURCE)
        self.assertIn(".ov-cell b{font-family:var(--font-serif);font-size:var(--fs-4)", PAGE_SOURCE)
        # 抽屉浮层：避开底部 dock。
        self.assertIn("body[data-ov-open] .ov-detail{display:block;position:fixed;left:var(--space-2);right:var(--space-2);bottom:calc(72px + env(safe-area-inset-bottom,0px))", PAGE_SOURCE)
        # 明细入口收成一行文字链，但触控高度仍要够 40px。
        self.assertIn(".ov-more{flex:none;margin-left:auto;min-width:40px;min-height:40px;padding:0 var(--space-2);border:0;background:none;color:var(--teal-strong,var(--teal));font-size:var(--fs-2);text-decoration:underline;text-underline-offset:2px}", PAGE_SOURCE)
        # 三个标签写成人话，退格到「今天 / 最近 / 完成」。
        self.assertIn('<span>今天</span>', PAGE_SOURCE)
        self.assertIn('<span>最近</span>', PAGE_SOURCE)
        self.assertIn('<span>完成</span>', PAGE_SOURCE)
        self.assertIn(">展开</button>", PAGE_SOURCE)
        # .summary 的进入动画 fill 会留下 transform，成为 fixed 的包含块把抽屉钉在卡片里；开抽屉时必须清掉。
        self.assertIn("body[data-ov-open] #overview{animation:none;transform:none", PAGE_SOURCE)
        # 三个收起入口 + Esc 让位给模态框。
        self.assertIn("if (open) document.body.dataset.ovOpen = '1';", PAGE_SOURCE)
        self.assertIn("more.addEventListener('click', function () { setOpen(document.body.dataset.ovOpen !== '1'); });", PAGE_SOURCE)
        self.assertIn("if (close) close.addEventListener('click', function () { setOpen(false); });", PAGE_SOURCE)
        self.assertIn("document.body.dataset.ovOpen === '1' && !agentModalOpen()", PAGE_SOURCE)
        self.assertIn("more.setAttribute('aria-expanded', open ? 'true' : 'false');", PAGE_SOURCE)
        # 数据回来才填数：任务变了重画，行程靠接口异步回来所以盯 #trip-meta。
        self.assertIn("document.addEventListener('nh:tasks-changed', function () { window.setTimeout(paintOverviewStrip, 0); });", PAGE_SOURCE)
        self.assertIn("new MutationObserver(paintOverviewStrip).observe(meta, {childList: true, characterData: true, subtree: true})", PAGE_SOURCE)
        self.assertIn("initOverviewDetail();", PAGE_SOURCE)

    def test_tasks_page_grows_a_right_rail_above_1100px(self) -> None:
        # 宽屏两栏：左边概览与待办，右边 400px 地图在上、今天几趟在下、对话条贴底。
        self.assertIn("@media(min-width:1100px){", PAGE_SOURCE)
        self.assertIn("grid-template-columns:minmax(0,1fr) 400px", PAGE_SOURCE)
        # 右栏：跨前两行（概览行 + 待步行），自己撑满，内容不往下溢。
        self.assertIn("grid-column:2;grid-row:1 / 3;height:auto;min-height:0;align-self:stretch;overflow:hidden", PAGE_SOURCE)
        # 地图 42vh 封顶、趟列表自己滚：右栏高度就是左栏两行之和，列表照旧往下长会画到屏幕外。
        self.assertIn("main>.trip-stage>.trip-map-wrap{height:min(42vh,340px)", PAGE_SOURCE)
        # 栏本身不再整体滚动：竖排 flex 里列表吃剩余高度自己滚，钉子（常做的事）排在滚动区外，
        # 永远贴着对话条那一条边。整栏滚的时候钉子会滚到半路，底下漏出半截被淡掉的字。
        self.assertIn("main>.trip-stage>.trip-side{flex:1 1 auto;display:flex;flex-direction:column;", PAGE_SOURCE)
        self.assertIn("margin:0;max-height:none;height:auto;min-height:0;overflow:hidden;", PAGE_SOURCE)
        # 标题留在右栏里跟列表一起（右栏是「地图在上、今天几趟在下」）。
        self.assertIn("main>.trip-stage>.trip-head{margin:0}", PAGE_SOURCE)
        # 内滚下移到 .trip-scroll 这一层，滚到底前 36px 渐隐，最后一行不会硬切成半截。
        self.assertIn("main>.trip-stage>.trip-side .trip-scroll{flex:1 1 auto;min-height:0;overflow:auto;", PAGE_SOURCE)
        # 进栏动效 240ms 走 --ease-out，全段不许出现 ease-in；减弱动效档不播。
        self.assertIn("@keyframes rail-in{from{opacity:0;transform:translateX(16px)}to{opacity:1;transform:none}}", PAGE_SOURCE)
        self.assertIn("animation:rail-in 240ms var(--ease-out) both", PAGE_SOURCE)
        rail = PAGE_SOURCE.split("@keyframes rail-in", 1)[1].split("/* ── 20.", 1)[0]
        self.assertNotIn("ease-in", rail)
        self.assertIn("@media(min-width:1100px) and (prefers-reduced-motion:reduce){", PAGE_SOURCE)
        # 基线给 .trip-stage 写了 width:calc(100% - 2 * var(--gutter)) + margin-inline:auto，
        # 右栏里再减一次边距，400 的栏里地图只剩 336、右边空 64 像素：右栏里必须把这层解掉。
        self.assertIn("display:flex;flex-direction:column;gap:var(--space-3);width:100%;max-width:none;margin-inline:0;", PAGE_SOURCE)
        # 对话拉开 = 盖上来：dock 绝对定位贴着待机那条的底边，行高留成待机条的 70。
        # 以前是把它搬到左列 + 压扁地图 + 收起待办，一次点击重排三处，输入框从指针底下挪走，
        # 真实点击永远醒不过来——所以这里锁「不重排」。
        self.assertIn("main:has(#agent-dock[data-state=awake]){grid-template-rows:auto minmax(0,1fr) 70px}", PAGE_SOURCE)
        self.assertIn("main>#agent-dock[data-state=awake]{\n    position:absolute;left:0;right:0;bottom:0;height:auto;max-height:min(62vh,540px);align-self:stretch;", PAGE_SOURCE)
        # 高度跟内容走：写死 540 的时候，只有一句回复的对话里 400 多像素是白的（1280 实测）。
        self.assertIn("main>#agent-dock[data-state=awake] .agent-log{min-height:96px}", PAGE_SOURCE)
        # 板子自己滚，那条滚动条会吃掉 15px，卡片右沿比概览卡窄一截；收起来两块同宽。
        self.assertIn("main>.task-board-block{grid-area:2 / 1;margin:0;min-height:0;overflow:auto;scrollbar-width:none}", PAGE_SOURCE)
        self.assertIn("main>.task-board-block::-webkit-scrollbar{display:none}", PAGE_SOURCE)
        # 待机时对话条横跨整页底边（以前缩在右栏那一格，左边整片空）。
        self.assertIn("main>#agent-dock{grid-area:3 / 1 / 4 / 3;align-self:end;position:static;width:100%;max-width:none;margin:0;z-index:5}", PAGE_SOURCE)
        # 「待办 N」是绝对定位压在对话条上的，两栏里正好盖住输入提示，待机时藏掉。
        self.assertIn("main>#agent-dock[data-state=idle] .agent-count{display:none}", PAGE_SOURCE)
        # 基线已经藏了 .summary 和 .task-board-block，别再写一遍（写了也是空操作，还会掩盖基线改动）。
        self.assertNotIn("main:has(#agent-dock[data-state=awake])>.task-board-block{display:none}", PAGE_SOURCE)
        # 待办列表仍是 main 的直接子块，没被塞进收起的概览或限高容器里。
        self.assertIn('<div class="task-board-block">', PAGE_SOURCE)
        self.assertLess(PAGE_SOURCE.index('</section>', PAGE_SOURCE.index('id="overview"')), PAGE_SOURCE.index('class="task-board-block"'))
        # 待办是单列整行列表，不是并排车道：真数据里「今日」一桶 43 张卡，
        # 竖着排成车道就是一列 14000 像素（实测），横着切 5 个 130 宽又把标题挤成竖线。
        # 现在时间桶降级成 sticky 组头，卡片整行铺满 631，一屏四张。
        self.assertIn("main>.task-board-block .timeline-board{", PAGE_SOURCE)
        self.assertIn("display:flex;flex-direction:column;gap:0;overflow:visible;width:100%", PAGE_SOURCE)
        self.assertIn("main>.task-board-block .timeline-lane{display:block;min-width:0;width:auto}", PAGE_SOURCE)
        self.assertIn("main>.task-board-block .timeline-lane.is-collapsed{display:none}", PAGE_SOURCE)
        self.assertIn("position:sticky;top:0;z-index:2;", PAGE_SOURCE)
        # 上留白 8：组头 sticky，卡片悬停上移 2px 会钻进组头里点不中（实测只差 1px）。
        self.assertIn("main>.task-board-block .timeline-cards{flex-direction:column;gap:var(--space-2);",
                      PAGE_SOURCE)
        self.assertIn("padding-top:var(--space-2);padding-bottom:var(--space-2)}", PAGE_SOURCE)

        # 卡片：整行一条，文字在左、三个操作在右；高矮按内容走（基线 flex:0 0 240px 必须被盖掉）。
        self.assertIn("main>.task-board-block .timeline-cards .task{", PAGE_SOURCE)
        self.assertIn("display:flex;flex-direction:row;align-items:flex-start;justify-content:space-between;", PAGE_SOURCE)
        self.assertIn("flex:0 0 auto;", PAGE_SOURCE)
        self.assertIn("width:auto;min-width:0;max-width:none;padding:var(--space-3) var(--space-4);", PAGE_SOURCE)

    def test_map_mode_bar_hides_when_the_map_is_a_strip(self) -> None:
        # 地图退成扁带（唤醒对话后 132 像素那种）就没有画布了，那排底图按钮和「＋地点」
        # 悬在带子里没有可作用的对象；收起它，有画布时自动回来。
        for selector in (".trip-map-wrap:has(.trip-strip) .map-modes",
                         ".trip-map-wrap:has(.trip-strip) .map-add",
                         ".trip-map-wrap:has(.trip-strip) .map3d-zoom"):
            self.assertIn(selector, PAGE_SOURCE)
        rule = PAGE_SOURCE.split(".trip-map-wrap:has(.trip-strip) .map3d-zoom", 1)[1].split("}", 1)[0]
        self.assertIn("display:none", rule)

    def test_map_controls_and_overview_entry_are_finger_sized(self) -> None:
        # 这些控件原先浮在画布右上角：1280 的画布只有 399×275，三个 40px 的键加「＋地点」
        # 浮上去就吃掉半张图（用户原话「地图没法看」）。现在它们排进画布上方那条工具栏，
        # 画布自己留出高度；手机上按钮仍是 44px 触屏下限，桌面上收小到一行放得下，
        # 免得工具栏顶成两排又把画布切走 40 像素。
        css = self._current_stylesheet()
        bar = self._rule(css, ".map-modes")
        self.assertIn("order:-1", bar, "工具栏要排在画布上方（order:-1）")
        for selector in (".map3d-zoom", ".map-add"):
            self.assertNotIn("position:absolute", self._rule(css, selector),
                             "%s 不许再浮在画布上" % selector)
        self.assertNotIn("left:10px", self._rule(css, ".map-add"), "「＋地点」贴左边缘就还在浮着")

        def floors(body: str, sides, name: str, least: float = 40) -> None:
            for side in sides:
                found = re.search(side + r":(\d+(?:\.\d+)?)px", body)
                self.assertIsNotNone(found, "%s 没写死 %s：%s" % (name, side, body))
                self.assertGreaterEqual(float(found.group(1)), least,
                                        "%s 的 %s 小于 %gpx：%s" % (name, side, least, body))

        # 手机：三键和「＋地点」都要 44px（触屏下限），「我在…」40px。
        narrow_zoom = re.search(r"@media\(max-width:720px\)\{\.map3d-zoom button\{([^}]*)\}", css)
        self.assertIsNotNone(narrow_zoom, "≤720 那条缩放键规则不见了")
        floors(narrow_zoom.group(1), ("width", "height"), "≤720 .map3d-zoom button", 44)
        narrow_add = re.search(r"\.map-add\{min-height:(\d+(?:\.\d+)?)px;padding:0 (\d+)px\}", css)
        self.assertIsNotNone(narrow_add, "≤720 那条 .map-add 规则不见了")
        floors("min-height:%spx" % narrow_add.group(1), ("min-height",), "≤720 .map-add", 44)

        # 桌面那一行：缩放键比手机小（一行才放得下），但鼠标目标不许小到点不着。
        desktop_zoom = self._rule(css, ".map3d-zoom button")
        floors(desktop_zoom, ("width", "height"), ".map3d-zoom button", 28)

        # 「＋地点」：字要够 12px（用 --fs-3=13px，不许把全页在用的 --fs-1 抬上来顶账）。
        add = self._rule(css, ".map-add")
        self.assertIn("font-size:var(--fs-3)", add)
        self.assertNotIn("font-size:var(--fs-1)", add)
        self.assertIn("--fs-3:calc(13px * var(--font-scale))", css)
        self.assertIn("--fs-1:calc(9px * var(--font-scale))", css)
        floors(add, ("min-height",), ".map-add", 30)

        # 明细「展开」：38×40 差 2px 宽，两头都要 40。这条在手机媒体块里，带缩进，不走 _rule。
        more = re.search(r"\.ov-more\{([^}]*)\}", css)
        self.assertIsNotNone(more, "找不到 .ov-more 那条规则")
        floors(more.group(1), ("min-width", "min-height"), ".ov-more")

        # 控件确实被收进工具栏（不是各自浮着）：缩放键和「＋地点」由 JS 挂到 .map-modes 里。
        page = PAGE_DIR.joinpath("js", "trip.js").read_text(encoding="utf-8")
        map3d = PAGE_DIR.joinpath("js", "map3d.js").read_text(encoding="utf-8")
        self.assertIn("['.map3d-zoom', '.map-add'].forEach(function (sel) {", page)
        self.assertIn("(bar || box).appendChild(button);", page)
        self.assertIn("(bar || wrap).appendChild(box);", map3d)

    def test_calendar_month_turn_and_event_titles_are_explained(self) -> None:
        css = self._current_stylesheet()
        self.assertIn("@keyframes calendar-turn{from{opacity:0;transform:translateX(var(--turn-x,18px))}", css)
        self.assertIn("#calendar.calendar-turn{animation:calendar-turn 240ms var(--ease-out) both}", css)
        self.assertIn("root.style.setProperty('--turn-x',direction>0?'18px':'-18px')", PAGE_SOURCE)
        self.assertNotIn("animateSurface(root,direction*12)", PAGE_SOURCE)
        # 事件的解释从悬停 title 挪进了说明行与 aria-label：格子里只画圆点，靠文案与读屏讲清「点格子看全部」。
        self.assertIn("cell.setAttribute('aria-label',(m+1)+'月'+d+'日 '+items.length+' 件待办，回车查看全部')", PAGE_SOURCE)
        self.assertIn("格子里只留圆点：有事项才画，每格最多 3 个", PAGE_SOURCE)
        self.assertIn("点一下日期格（或键盘回车）看当天全部事项", PAGE_SOURCE)

    def test_brand_click_and_connect_buttons_explain_themselves(self) -> None:
        # Logo 按钮回到页顶；Enter/Space 原生触发 click，不再连带重开引导。
        self.assertIn('id="brand-home" class="brand" aria-label="回到页面顶部"', PAGE_SOURCE)
        self.assertNotIn("回到顶部；重新显示「开始使用」引导", PAGE_SOURCE)
        self.assertNotIn("已回到顶部，并重新显示", PAGE_SOURCE)
        self.assertIn('class="brand-logo" id="brand-mark"', PAGE_SOURCE)
        self.assertIn("window.scrollTo({top:0,behavior:paused?'auto':'smooth'})", PAGE_SOURCE)
        self.assertNotIn('class="brand-mark-button"', PAGE_SOURCE)
        self.assertIn("@keyframes brand-spin{from{transform:rotate(0)}to{transform:rotate(360deg)}}", self._current_stylesheet())
        self.assertIn("mark.classList.add('is-spinning')", PAGE_SOURCE)
        self.assertIn("localStorage.removeItem('qq_digest_journey_hidden')", PAGE_SOURCE)
        self.assertIn('class="connect-hint"', PAGE_SOURCE)
        self.assertIn('title="第一次用点这个：下载安装 NapCat、启动引擎并显示登录二维码"', PAGE_SOURCE)
        self.assertIn('title="只启动引擎并确认登录状态；不会退出电脑版 QQ"', PAGE_SOURCE)
        self.assertIn('title="二维码过期或看不清时重新获取一张"', PAGE_SOURCE)
        self.assertIn(".connect-hint,.star-note{margin:var(--space-2) 0 0;color:var(--faint);font-size:var(--fs-2)}", self._current_stylesheet())

    def test_right_rail_panels_report_week_actions_and_health(self) -> None:
        css = self._current_stylesheet()
        # bento 现在只剩三块（服务自检搬进设置页）：置顶 / 本周小结 / 快捷操作，顺序即 DOM 顺序。
        bento = PAGE_HTML.split('<div class="bento"', 1)[1].split('<div class="workspace">', 1)[0]
        panels = ('id="pinned-panel"', 'id="rail-summary"', 'id="rail-actions"')
        positions = [bento.index(panel) for panel in panels]
        self.assertEqual(positions, sorted(positions), "面板顺序必须是 置顶 → 小结 → 快捷")
        self.assertNotIn('id="rail-health"', bento, "服务自检不该留在 bento 里")
        self.assertLess(PAGE_HTML.index('<main>'), PAGE_HTML.index('<div class="bento"'))
        self.assertLess(PAGE_HTML.index('<div class="bento"'), PAGE_HTML.index('<div class="workspace">'))
        self.assertIn("body[data-tab]:not([data-tab=tasks]) .bento{display:none}", css)
        self.assertNotIn('class="side-rail"', PAGE_HTML)
        # 本周小结：三个数字 + 七根柱，柱子要有无障碍名称
        bars_tag = self._tag("rail-bars")
        self.assertIn('role="img"', bars_tag, "七根柱要当一张无障碍图片读")
        self.assertIn('aria-label="本周每天到期的待办条数"', bars_tag)
        self.assertIn("var stats = data.stats || {};", PAGE_SOURCE)
        self.assertIn("['rail-stat-open', stats.open]", PAGE_SOURCE)
        self.assertIn("['rail-stat-done', stats.done]", PAGE_SOURCE)
        self.assertIn("['rail-stat-overdue', stats.overdue]", PAGE_SOURCE)
        # 快捷操作：每个按钮都绑定到页面里已有的能力，不留摆设。
        # 动效开关不在这里——它只在「设置 → 外观」里，同一开关出现两处会让人以为状态分两套。
        for button_id, handler in (
            ("rail-copy-today", "railCopyToday"),
            ("rail-export-ics", "railExportIcs"),
            ("rail-show-qr", "railShowSubscription"),
        ):
            self.assertRegex(
                PAGE_SOURCE,
                r"\['%s',\s*%s\]" % (button_id, handler),
                "%s 没绑定到 %s" % (button_id, handler),
            )
        self.assertNotIn('id="rail-motion"', PAGE_HTML, "快捷操作里的暂停动效按钮应该已经删掉")
        self.assertNotIn("railToggleMotion", PAGE_SOURCE)
        self.assertIn("navigator.clipboard.writeText(text)", PAGE_SOURCE)
        self.assertIn("document.execCommand('copy')", PAGE_SOURCE)
        # 订阅地址带 token，否则日历订阅会 401
        self.assertIn("link.href = '/calendar.ics'", PAGE_SOURCE)
        self.assertIn("'?token=' + encodeURIComponent(token)", PAGE_SOURCE)
        # 动效偏好只有一条写入口：设置 → 外观里的开关调 setMotionPreference。
        self.assertIn("setMotionPreference(motionToggle.checked)", PAGE_SOURCE)
        self.assertIn("function setMotionPreference(", PAGE_SOURCE)
        # 服务自检行完全来自聚合接口，面板只发这一个请求。
        render_start = PAGE_SOURCE.index('function renderRailHealth()')
        render_end = PAGE_SOURCE.index('function initRailPanels()', render_start)
        render_health = PAGE_SOURCE[render_start:render_end]
        self.assertIn('id="rail-recheck"', PAGE_SOURCE)
        self.assertIn("api('/api/selfcheck').then(function (data)", render_health)
        self.assertIn("var rows = (data && Array.isArray(data.items)) ? data.items : [];", render_health)
        self.assertNotIn("/api/health", render_health)
        self.assertNotIn("/api/napcat/status", render_health)
        self.assertNotIn("/api/hosting/status", render_health)
        self.assertNotIn("/api/sync/info", render_health)
        self.assertNotIn("/api/wechat/status", render_health)
        self.assertIn("item.dataset.state = row.state;", render_health)
        self.assertIn("if (row.hint)", render_health)
        self.assertIn(".catch(function () {", render_health)
        self.assertIn("settingsButton.onclick = showSettingsTab", PAGE_SOURCE)
        # 接线：渲染时刷新小结，初始化时绑定按钮
        self.assertRegex(PAGE_SOURCE, r"renderPinned\(data\);\s+renderRailSummary\(data\);")
        self.assertIn("initRailPanels();", PAGE_SOURCE)
        # 样式沿用既有 token，不引入新色
        for selector in (".rail-panel,.pinned-panel,.bento .surface{", ".rail-bar.is-today .rail-bar-fill{", ".health-row[data-state=bad] .health-dot{"):
            self.assertIn(selector, css)
        # 「消息推送」那行会多出一个「去设置」按钮；按钮若落回第 1 列会把该列撑宽，标题被推右
        self.assertIn(".health-row .btn-small{flex:none;margin:0 0 0 var(--space-1);color:var(--teal)}", css)
        # 服务自检面板搬到设置页：容器在 #tab-settings 里、带检索锚点；
        # 设置页内部不需要「打开设置」这个大按钮（行内那个「去设置」小按钮保留）。
        health_block = PAGE_HTML.split('id="rail-health"', 1)[1].split('</section>', 1)[0]
        self.assertLess(PAGE_HTML.index('id="tab-settings"'), PAGE_HTML.index('id="rail-health"'))
        self.assertIn('data-setting-anchor="health"', health_block)
        self.assertNotIn('id="rail-settings"', health_block, "设置页里不需要「打开设置」")
        self.assertIn('id="rail-recheck"', health_block)

    def test_subscription_qr_refresh_refetches_the_address(self) -> None:
        """订阅二维码的内容就是订阅地址；刷新必须重新取地址，并把「地址没变」说清楚。"""
        self.assertIn('id="sync-refresh" class="btn" type="button">刷新二维码</button>', PAGE_SOURCE)
        self.assertIn("function refreshSyncInfo(manual)", PAGE_SOURCE)
        self.assertIn("window.__nhRefreshSyncInfo = function () { refreshSyncInfo(false); };", PAGE_SOURCE)
        self.assertIn("if (window.__nhRefreshSyncInfo) window.__nhRefreshSyncInfo();", PAGE_SOURCE)
        self.assertIn("订阅地址没变，二维码内容与上次相同。", PAGE_SOURCE)
        self.assertIn("地址已更新，二维码已换新。", PAGE_SOURCE)
        # 同一 URL 的图片不会真正重新请求：带时间戳强制重取
        self.assertIn("encodeURIComponent(setupToken) + '&t=' + Date.now();", PAGE_SOURCE)

    def test_create_task_form_and_calendar_detail_transition_are_wired(self) -> None:
        css = self._current_stylesheet()
        # 新建待办：按钮 → 内联表单 → POST /api/tasks/create → 刷新列表
        new_task_tag = self._tag("rail-new-task")
        self.assertIn('aria-controls="rail-new-form"', new_task_tag)
        self.assertIn('aria-expanded="false"', new_task_tag, "表单默认收起，按钮要报出收起状态")
        self.assertIn('type="button"', new_task_tag, "不是 submit，别让它顺手提交外层表单")
        self.assertIn(" hidden", self._tag("rail-new-form"), "内联表单默认必须收起")
        self.assertIn('type="text"', self._tag("rail-new-summary"))
        self.assertIn('maxlength="200"', self._tag("rail-new-summary"))
        self.assertIn('type="datetime-local"', self._tag("rail-new-deadline"))
        save_tag = self._tag("rail-new-save")
        self.assertIn('type="submit"', save_tag)
        # _tag 只取开始标签，按钮文字在标签之后：断言整段可见文案仍然存在。
        self.assertIn("保存</button>", PAGE_SOURCE)
        cancel_tag = self._tag("rail-new-cancel")
        self.assertIn('type="button"', cancel_tag)
        self.assertIn("取消</button>", PAGE_SOURCE)
        self.assertIn("api('/api/tasks/create', {", PAGE_SOURCE)
        self.assertIn("body: JSON.stringify({summary: summary, deadline: deadline})", PAGE_SOURCE)
        self.assertIn("return syncTasks();", PAGE_SOURCE)
        self.assertIn("railShowNewForm(document.getElementById('rail-new-form').hidden)", PAGE_SOURCE)
        self.assertIn("newForm.onsubmit = function (event) { event.preventDefault(); railCreateTask(); };", PAGE_SOURCE)
        self.assertIn("event.key === 'Escape'", PAGE_SOURCE)
        self.assertIn(".rail-new-form[hidden]{display:none}", css)
        # 月历当天详情：展开/收起都有过渡，且关键帧只动 opacity/transform（不重采样文字）
        # 收起/展开共用一条 feedback-in（只动 opacity/transform，不重采样文字）：
        # 详情面板与「新建待办」表单的进入都挂它，退场只降到透明。
        body = re.search(r"@keyframes feedback-in\{(.*?)\}\}", css, re.S)
        self.assertIsNotNone(body)
        self.assertNotIn("box-shadow", body.group(1))
        self.assertNotIn("filter", body.group(1))
        self.assertIn("opacity", body.group(1))
        self.assertIn("transform", body.group(1))
        self.assertIn(".calendar-detail.is-opening,.rail-new-form[data-state=entering]{animation:feedback-in 200ms var(--ease-out) both}", css)
        self.assertIn(".calendar-detail.is-closing,.rail-new-form[data-state=exiting]{opacity:0}", css)
        self.assertIn("calendarDetail.classList.add('is-opening')", PAGE_SOURCE)
        self.assertIn("calendarDetail.classList.add('is-closing')", PAGE_SOURCE)
        self.assertIn("calendarDetail.dataset.closing='1'", PAGE_SOURCE)
        self.assertIn("getComputedStyle(calendarDetail).animationName==='none'", PAGE_SOURCE)
        # 表单不再做高度动画（那会连着 reflow）：进入靠同一条 feedback-in，地面布局参数就此锁住。
        self.assertIn(
            ".rail-new-form{display:grid;gap:var(--space-2);margin-top:var(--space-2);padding:var(--space-3);"
            "border:1px dashed var(--line-strong);border-radius:var(--r-2);background:var(--paper);overflow:visible}",
            css,
        )
        self.assertIn("form.dataset.state = 'entering';", PAGE_SOURCE)
        self.assertIn("getComputedStyle(form).animationName === 'none'", PAGE_SOURCE)
        self.assertIn("clearTimeout(Number(form.dataset.pending))", PAGE_SOURCE)


class TaskStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp
        self.store = Store(Path(tmp.name) / "tasks.sqlite3")

    def test_upsert_keeps_done_status(self) -> None:
        task_id = self.store.upsert_task(
            task_key="m1",
            summary="提交实验报告",
            deadline=iso(NOW + dt.timedelta(hours=9)),
            category="action",
        )
        self.assertGreater(task_id, 0)
        toggled = self.store.set_task_status(task_id, True)
        self.assertTrue(toggled)
        self.store.upsert_task(task_key="m1", summary="提交实验报告（更新）", category="action")
        tasks = self.store.list_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "done")
        self.assertEqual(tasks[0]["summary"], "提交实验报告（更新）")
        self.assertEqual(self.store.task_stats()["done"], 1)

    def test_calendar_maps_task_statuses_for_apple_calendar(self) -> None:
        tasks = [
            {"id": "open", "summary": "open", "deadline": "2026-10-01", "status": "open"},
            {"id": "progress", "summary": "progress", "deadline": "2026-10-02", "status": "in_progress"},
            {"id": "dismissed", "summary": "dismissed", "deadline": "2026-10-03", "status": "dismissed"},
            {"id": "expired", "summary": "expired", "deadline": "2026-10-04", "status": "expired"},
            {"id": "done", "summary": "done", "deadline": "2026-10-05", "status": "done"},
        ]
        feed = ics.render_calendar(tasks, now=NOW)
        self.assertEqual(feed.count("STATUS:CONFIRMED"), 3)
        self.assertEqual(feed.count("STATUS:CANCELLED"), 2)
        self.assertNotIn("STATUS:NEEDS-ACTION", feed)
        # RFC 5545: COMPLETED is a VTODO value, not a legal VEVENT STATUS. A strict client
        # could drop the whole event, so it must never appear. (was emitted for "done")
        self.assertNotIn("STATUS:COMPLETED", feed)
        self.assertNotIn("STATUS:NEEDS-ACTION", feed)
        # Only the three values RFC 5545 allows for VEVENT.
        for value in re.findall(r"^STATUS:(.+)$", feed, re.MULTILINE):
            self.assertIn(value.strip(), {"TENTATIVE", "CONFIRMED", "CANCELLED"})

    def test_group_tasks_splits_today_week_later(self) -> None:
        self.store.upsert_task(task_key="a", summary="today", deadline=iso(NOW + dt.timedelta(hours=2)))
        self.store.upsert_task(task_key="b", summary="week", deadline=iso(NOW + dt.timedelta(days=3)))
        self.store.upsert_task(task_key="c", summary="later")
        grouped = group_tasks(self.store.list_tasks(), NOW)
        buckets = (("today", "today"), ("week", "week"), ("later", "later"))
        for bucket, want in buckets:
            with self.subTest(bucket=bucket):
                self.assertEqual([t["summary"] for t in grouped[bucket]], [want])
        result = overview(self.store.list_tasks(), grouped, NOW)
        self.assertIn("今天", result["headline"])
        self.assertEqual(result["progress"], 0)


    def test_overdue_count_is_neutral_and_never_the_primary_headline(self) -> None:
        self.store.upsert_task(task_key="expired", summary="expired", deadline=iso(NOW - dt.timedelta(hours=1)))
        tasks = self.store.list_tasks()
        grouped = group_tasks(tasks, NOW)
        summary = overview(tasks, grouped, NOW)
        self.assertEqual(summary["headline"], "今天 1 件 · 逾期 1 件")
        self.assertNotIn("都已过期", summary["headline"])
        self.assertNotIn("先处理已逾期", summary["subline"])
        # 逾期件数已经写在标题里，副标题不再重复一遍「已收起、可展开」这类纯状态话。
        self.assertEqual(summary["subline"], "按截止时间从上到下处理")

    def test_undated_open_tasks_headline_names_them(self) -> None:
        """只有没写截止日的待办时，抬头不能只说「没有到期的事」。

        这几件会被 trips 按就近原则排进今天的趟，右侧「N 件·M 趟」和左侧「0% 今日已完成」
        会同屏打架。抬头要点明「没有要截止的」+ 副标题说清「没写日期的也按就近排趟」，
        读者才知道趟里那几件是哪来的。
        """
        self.store.upsert_task(task_key="nd1", summary="有空再看")
        self.store.upsert_task(task_key="nd2", summary="整理桌面")
        tasks = self.store.list_tasks()
        grouped = group_tasks(tasks, NOW)
        self.assertEqual(grouped["today"], [])
        summary = overview(tasks, grouped, NOW)
        self.assertEqual(summary["headline"], "今天没有要截止的事")
        self.assertIn("就近", summary["subline"])

    def test_candidate_is_separate_from_today(self) -> None:
        self.store.upsert_task(
            task_key="candidate",
            summary="可能要交报名表",
            category="action",
            status="candidate",
            confidence=0.6,
            deadline=iso(NOW + dt.timedelta(hours=1)),
        )
        grouped = group_tasks(self.store.list_tasks(), NOW)
        self.assertEqual(len(grouped["candidates"]), 1)
        self.assertEqual(grouped["today"], [])


class TaskApiTest(unittest.TestCase):
    def setUp(self) -> None:
        # 这一类的用例会 POST /api/settings，热写会把 NOTICEHUB_* / QQ_DIGEST_* 灌进
        # 真实 os.environ 且永不退出；后面的微信取钥用例读到就会当成用户手填的密钥。
        from tests import snapshot_env_prefixes

        snapshot_env_prefixes(self)
        # 更新用例模拟发布包；源码判定另由 test_updates_api 覆盖。
        build = mock.patch("noticehub.updates_api.build_source", return_value="package")
        build.start()
        self.addCleanup(build.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # NapCat 端口探测缓存 3 秒：用例之间必须隔离，否则上一条的探测结果会盖住这一条。
        webapp._clear_napcat_live_cache()
        self.addCleanup(webapp._clear_napcat_live_cache)
        # 快速通道实例是模块级注册表：不隔离的话上一条开的假隧道会漏进下一条的状态断言。
        webapp._clear_quick_tunnels()
        self.addCleanup(webapp._clear_quick_tunnels)
        self.store = Store(Path(self.tmp.name) / "api.sqlite3")
        self.task_id = self.store.upsert_task(
            task_key="m1",
            summary="提交实验报告",
            category="action",
            groups=["测仪2602班群"],
            deadline=iso(dt.datetime(2026, 9, 30, 18, 0)),
            evidence="请各班班长今天18:00前提交实验报告",
        )
        self.settings = Settings(
            group_whitelist=("g1",),
            web_host="127.0.0.1",
            group_aliases={"g1": "学院通知群"},
            web_token="secret",
            web_port=0,
            data_dir=Path(self.tmp.name) / "data",
        )
        self.server = TaskWebServer(self.settings, self.store)
        self.assertTrue(self.server.start())
        self.addCleanup(self.server.stop)
        assert self.server.server is not None
        self.base = f"http://127.0.0.1:{self.server.server.server_address[1]}"

        # /api/hosting/* 会真的动这台机器：start 先 taskkill 电脑版 QQ 再启 NapCat，
        # status 每次起一个 powershell 查进程（本机实测单次 2.7-3.8s、整条 start 6.9-7.4s）。
        # 这个类只验 HTTP 语义（状态码、body 上限、字段形状），所以系统动作与进程查询都打桩；
        # 真逻辑由 tests/test_hosting.py 覆盖（那边已经 patch 了 _processes/user_qq_pids/_taskkill）。
        for target, value in (
            ("noticehub.webapp.hosting.quit_user_qq",
             {"ok": True, "pids": [], "already_closed": True, "forced": False, "remaining": [], "error": None}),
            ("noticehub.webapp.hosting.start_user_qq",
             {"ok": True, "pid": None, "already_running": True, "error": None}),
            ("noticehub.webapp.hosting._processes", []),
            ("noticehub.webapp.hosting._bridge", {"ok": True, "error": None}),
        ):
            patcher = mock.patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_campus_calibration_reaches_plan_poi_response(self) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.settings.data_dir / 'campus_poi.json'
        with mock.patch.dict(os.environ, {'NOTICEHUB_AGENT_SHELL': '1'}):
            # library 在表里是 (549.7, 876.4)，y 是南正。校准时 dx 直接加，dy 是「屏幕
            # 往上抬多少」，落到南正坐标里要减：876.4 - 20 = 856.4。
            for fields, expected in [
                ({'floors': 7, 'dx': 10, 'dy': 20}, (7, 559.7, 856.4)),
                ({'floors': 'bad', 'dx': 'bad', 'dy': {}}, (5, 549.7, 876.4)),
            ]:
                path.write_text(json.dumps([dict(id='library', **fields)]), encoding='utf-8')
                status, _, raw = self._raw_request('/api/agent/plan')
                self.assertEqual(status, 200)
                pois = json.loads(raw)['plan']['pois']
                self.assertEqual(len(pois), 27)
                poi = next(p for p in pois if p['id'] == 'library')
                self.assertEqual((poi['floors'], poi['x'], poi['y']), expected)
                self.assertEqual(poi['name'], '图书馆')

    def test_manual_order_post_round_trips_through_plan_api(self) -> None:
        """拖拽排出来的站序要真存进服务端：POST 带 action=order，回的是重算过的 plan。

        锁的是整条链路，不是「有 3 站」：排完 POST 一次 → 接口回的 plan 就是用户排的顺序
        → 文件里也真写了 → 再 GET（= 刷新页面）顺序还在；坏 payload 必须 400 说清楚。
        """
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        (self.settings.data_dir / 'campus_routines.json').write_text(json.dumps({"routines": [
            {"title": "还书", "place": "图书馆", "minutes": 10},
            {"title": "交报告", "place": "武进楼", "minutes": 10},
            {"title": "找老师", "place": "立德楼", "minutes": 10},
        ]}, ensure_ascii=False), encoding="utf-8")
        with mock.patch.dict(os.environ, {'NOTICEHUB_AGENT_SHELL': '1'}):
            status, _, raw = self._raw_request('/api/agent/plan')
            self.assertEqual(status, 200)
            plan = json.loads(raw)['plan']
            self.assertEqual([s['title'] for s in plan['trips'][0]['stops']],
                             ['还书', '交报告', '找老师'])

            status, _, raw = self._raw_request(
                '/api/agent/plan', method='POST',
                payload={'action': 'order', 'date': plan['date'],
                         'order': ['找老师', '还书', '交报告']})
            self.assertEqual(status, 200, raw)
            body = json.loads(raw)
            self.assertTrue(body['ok'])
            self.assertEqual([s['title'] for s in body['plan']['trips'][0]['stops']],
                             ['找老师', '还书', '交报告'])

            saved = json.loads((self.settings.data_dir
                                / 'campus_plan_order.json').read_text(encoding='utf-8'))
            self.assertEqual(saved, {plan['date']: ['找老师', '还书', '交报告']})

            # 刷新页面 = 再取一次 plan：顺序得还在，别只在响应里演一遍。
            status, _, raw = self._raw_request('/api/agent/plan')
            self.assertEqual(status, 200)
            self.assertEqual([s['title'] for s in json.loads(raw)['plan']['trips'][0]['stops']],
                             ['找老师', '还书', '交报告'])

            status, _, raw = self._raw_request('/api/agent/plan', method='POST',
                                               payload={'action': 'nope', 'date': plan['date']})
            self.assertEqual(status, 400)
            self.assertFalse(json.loads(raw)['ok'])

    def test_here_post_moves_the_start_and_sticks(self) -> None:
        """「我在图书馆」也得真落到服务端：POST action=here → 回的 plan 起点就变了 →
        文件里也写了 → 再 GET（刷新）还是这个起点；认不出的楼名 400 说清楚，且不动已存的起点。
        """
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        here_file = self.settings.data_dir / 'campus_here.json'
        with mock.patch.dict(os.environ, {'NOTICEHUB_AGENT_SHELL': '1'}):
            status, _, raw = self._raw_request('/api/agent/plan', method='POST',
                                               payload={'action': 'here', 'place': '图书馆'})
            self.assertEqual(status, 200, raw)
            body = json.loads(raw)
            self.assertTrue(body['ok'])
            self.assertEqual(body['plan']['here']['name'], '图书馆')
            self.assertEqual(body['plan']['origin']['id'], 'library')
            saved = json.loads(here_file.read_text(encoding='utf-8'))
            self.assertEqual((saved['place_id'], saved['name']), ('library', '图书馆'))

            status, _, raw = self._raw_request('/api/agent/plan')
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw)['plan']['here']['name'], '图书馆')

            status, _, raw = self._raw_request('/api/agent/plan', method='POST',
                                               payload={'action': 'here', 'place': '火星基地'})
            self.assertEqual(status, 400)
            self.assertIn('没认出', json.loads(raw)['error'])
            self.assertEqual(json.loads(here_file.read_text(encoding='utf-8'))['place_id'], 'library')

    def test_split_frontend_script_is_served_without_token(self) -> None:
        # 页面用 <script src="js/announce.js"> 取脚本，浏览器不会带 X-Token，所以这条路必须免鉴权；
        # 但只放行 web_static/js 下的 .js，别把整棵树暴露出去。
        status, headers, body = self._raw_request("/js/announce.js", token="")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "text/javascript; charset=utf-8")
        self.assertIn("function refreshAnnouncements(", body.decode("utf-8"))
        status, _headers, _body = self._raw_request("/js/%2e%2e/web_static/page.html", token="")
        self.assertNotEqual(status, 200, "只放行 web_static/js 下的 .js，别把整棵树暴露出去")

    def test_floatball_page_requires_token_and_is_not_cached(self) -> None:
        request = urllib.request.Request(self.base + "/floatball", headers={"X-Token": "secret"})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get("Content-Type"), "text/html; charset=utf-8")
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertIn("nh-floatball", response.read().decode("utf-8"))
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base + "/floatball", timeout=5)
        self.assertEqual(caught.exception.code, 401)

    def test_floatball_page_theme_defaults_to_light_like_main_page(self) -> None:
        """浮球跟主页面同口径：不看系统深浅色，只有显式 data-theme=dark 才深色。"""
        from noticehub.floatball_page import FLOATBALL_PAGE_HTML

        page = FLOATBALL_PAGE_HTML
        self.assertNotIn("prefers-color-scheme:dark", page)
        self.assertIn("html[data-theme=dark]{", page)
        self.assertIn(":root{color-scheme:light;--page:#EDE7D6", page)
        self.assertIn('<html lang="zh-CN" data-theme="light">', page)
        # 浮球页与主页面同一套连续曲率圆角（原生 corner-shape，旧内核自动退回圆弧）。
        self.assertIn("@supports (corner-shape:squircle){*{corner-shape:squircle}}", page)

    def test_backup_requires_token_and_contains_only_a_valid_database(self) -> None:
        status, headers, body = self._raw_request("/api/backup", token="")
        self.assertEqual(status, 401)

        status, headers, body = self._raw_request("/api/backup")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/zip")
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertRegex(headers.get("Content-Disposition", ""), r'attachment; filename="notice-hub-backup-\d{4}-\d{2}-\d{2}\.zip"')
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            self.assertEqual(archive.namelist(), ["data/digest.sqlite3"])
            database_bytes = archive.read("data/digest.sqlite3")
        snapshot = sqlite3.connect(":memory:")
        try:
            snapshot.deserialize(database_bytes)
            self.assertEqual(snapshot.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertIsNotNone(snapshot.execute("SELECT summary FROM tasks WHERE summary = ?", ("提交实验报告",)).fetchone())
        finally:
            snapshot.close()

    def test_backup_reports_missing_database_as_structured_error(self) -> None:
        missing_path = Path(self.tmp.name) / "missing.sqlite3"
        with mock.patch.object(self.store, "path", missing_path):
            status, _, body = self._raw_request("/api/backup")
        self.assertEqual(status, 404)
        payload = json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "database_missing")
        self.assertIn("数据库文件不存在", payload["error"])

    def test_unauthorized_document_request_gets_html_hint_instead_of_json(self) -> None:
        """用户 2026-10-11 报的「到处都在加载中」：旧标签页里存着另一份安装的令牌，
        页面画得出来但每个请求都 401。浏览器直接打开/刷新时要给个说人话的页面。"""
        request = urllib.request.Request(
            self.base + "/", headers={"Accept": "text/html,application/xhtml+xml"}
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 401)
        self.assertIn("text/html", caught.exception.headers.get("Content-Type", ""))
        body = caught.exception.read().decode("utf-8")
        self.assertIn("QQ-Notice-Hub.exe", body)

        # API 请求（fetch 的默认 Accept 是 */*）仍旧回 JSON，别把接口变成网页。
        api_request = urllib.request.Request(
            self.base + "/api/tasks", headers={"Accept": "*/*"}
        )
        with self.assertRaises(urllib.error.HTTPError) as api_caught:
            urllib.request.urlopen(api_request, timeout=5)
        self.assertEqual(api_caught.exception.code, 401)
        self.assertIn("application/json", api_caught.exception.headers.get("Content-Type", ""))
        self.assertFalse(json.loads(api_caught.exception.read().decode("utf-8"))["ok"])

    def test_task_groups_are_shown_as_group_names_not_group_ids(self) -> None:
        """待办里存的是群号；界面上要显示群名（用户 2026-10-09 报的「显示的是群号」）。"""
        self.store.insert_message(
            msg_id="m-later",
            group_id="1051550372",
            group_name="1051550372",
            content="通知",
            ts=dt.datetime(2026, 9, 30, 9, 0),
        )
        self.store.insert_message(
            msg_id="m-named",
            group_id="1051550372",
            group_name="测控2602班群",
            content="通知",
            ts=dt.datetime(2026, 9, 30, 8, 0),
        )
        named_id = self.store.upsert_task(
            task_key="group-name",
            summary="交实训报告",
            status="open",
            groups=["1051550372"],
            deadline=iso(dt.datetime(2026, 10, 2, 18, 0)),
        )
        aliased_id = self.store.upsert_task(
            task_key="group-alias",
            summary="学院的事",
            status="open",
            groups=["g1"],
            deadline=iso(dt.datetime(2026, 10, 3, 18, 0)),
        )
        unknown_id = self.store.upsert_task(
            task_key="group-unknown",
            summary="陌生群的事",
            status="open",
            groups=["999000111"],
            deadline=iso(dt.datetime(2026, 10, 4, 18, 0)),
        )
        with mock.patch("noticehub.webapp.now_local", return_value=NOW):
            data = self._get("/api/tasks", token="secret")
        by_id = {task["id"]: task for task in data["later"] + data["today"] + data["week"]}
        # 群号 → 消息记录里那个真正的名字（最新记录只有群号，也不能覆盖真名）
        self.assertEqual(by_id[named_id]["groups"], ["测控2602班群"])
        # 用户保存过的别名优先
        self.assertEqual(by_id[aliased_id]["groups"], ["学院通知群"])
        # 什么都不知道就老实显示群号，不能显示成空
        self.assertEqual(by_id[unknown_id]["groups"], ["999000111"])
        self.assertEqual(self.store.group_names(), {"1051550372": "测控2602班群"})
        # NapCat 群列表里问到的名字要记住：下次 NapCat 掉线，界面也得显示群名
        self.assertEqual(self.store.remember_group_names({"999000111": "外语学院通知群", "999000222": ""}), 1)
        self.assertEqual(self.store.remember_group_names({"999000111": "外语学院通知群"}), 1)
        with mock.patch("noticehub.webapp.now_local", return_value=NOW):
            refreshed = self._get("/api/tasks", token="secret")
        after = {task["id"]: task for task in refreshed["later"] + refreshed["today"] + refreshed["week"]}
        self.assertEqual(after[unknown_id]["groups"], ["外语学院通知群"])
        self.assertEqual(self.store.group_names()["999000111"], "外语学院通知群")

    def test_hosting_status_exposes_the_fields_the_dashboard_reads(self) -> None:
        status = self._get("/api/hosting/status", "secret")
        for field in ("ok", "hosting_active", "napcat_running", "napcat_online", "groups_selected"):
            self.assertIn(field, status)
        self.assertIsInstance(status["groups_selected"], int)
        self.assertTrue(status["ok"])

    def test_concurrent_selfcheck_probes_only_once(self) -> None:
        """B7：页面首次加载会并发打两次自检，必须只探一遍，第二次复用结果而不是干等。"""
        calls: list[int] = []
        entered = threading.Event()
        items = [{"key": k, "label": k, "state": "ok", "value": "ok", "hint": ""} for k in "abcdefgh"]
        canned = {"ok": True, "items": items, "last_push_at": ""}

        def slow(handler):
            calls.append(1)
            entered.set()
            time.sleep(1.0)
            return dict(canned)

        first: list[dict] = []
        with mock.patch.object(webapp._Handler, "_compute_selfcheck_report", slow):
            thread = threading.Thread(target=lambda: first.append(self._get("/api/selfcheck", "secret")))
            thread.start()
            self.assertTrue(entered.wait(10), "第一次自检没有开始")
            second = self._get("/api/selfcheck", "secret")
            thread.join(20)
        self.assertEqual(len(calls), 1, "两次并发请求只能算一遍")
        self.assertEqual(len(first), 1)
        self.assertEqual(len(first[0]["items"]), 8)
        self.assertEqual(len(second["items"]), 8)
        self.assertTrue(second["cached"], "第二个请求应复用第一次的结果")
        self.assertFalse(second["checking"])

    def test_selfcheck_api_returns_fixed_eight_rows(self) -> None:
        expected_keys = ["qq", "hosting", "dingtalk", "wechat", "chaoxing", "attachments", "calendar", "push"]
        expected_labels = ["QQ 登录", "引擎托管", "钉钉", "微信", "学习通登录信息", "附件解析", "公网日历", "消息推送"]
        patches = (
            mock.patch.object(
                webapp._Handler,
                "_overview_payload",
                return_value={"ok": True, "sources": {
                    "qq": {"state": "ready", "detail": "监控 1 个群", "action": ""},
                    "napcat": {"state": "ready", "detail": "在线", "action": ""},
                    "wechat": {"state": "ready", "detail": "已连接 1 个群", "action": ""},
                    "dingtalk": {"state": "ready", "detail": "已连接实时监听 1 个群", "action": ""},
                    "chaoxing": {"state": "ready", "detail": "上次同步 now", "action": ""},
                    "funnel": {"state": "ready", "detail": "https://demo.ts.net", "action": ""},
                    "push": {"state": "ready", "detail": "已配置 飞书", "action": ""},
                }},
            ),
            mock.patch.object(webapp._Handler, "_napcat_doctor_payload", return_value={"ok": True}),
            mock.patch.object(webapp._Handler, "_napcat_status_payload", return_value={"ok": True, "nickname": "Bot"}),
            mock.patch.object(webapp._Handler, "_hosting_status_payload", return_value={"hosting_active": True, "groups_selected": 2}),
            mock.patch.object(webapp._Handler, "_dingtalk_status_payload", return_value={"running": True, "groups": 1, "scan_state": "running"}),
            mock.patch.object(webapp._Handler, "_wechat_status_payload", return_value={"available": True, "running": True, "groups": 1}),
            mock.patch.object(webapp._Handler, "_chaoxing_status_payload", return_value={"expired": False, "last_sync_at": "now"}),
            mock.patch.object(webapp._Handler, "_sync_info_response", return_value=(200, {"calendars": [{"kind": "public", "events": 3}], "events": 3})),
            mock.patch.object(self.settings, "push_channels", return_value=["飞书"]),
        )
        with mock.patch.multiple(self.settings, dingtalk_enabled=True, dingtalk_group_whitelist=("g1",), wechat_enabled=True, wechat_group_whitelist=("g1",), chaoxing_enabled=True, chaoxing_cookie="cookie"):
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
                data = self._get("/api/selfcheck", "secret")
        self.assertTrue(data["ok"])
        self.assertEqual([row["key"] for row in data["items"]], expected_keys)
        self.assertEqual([row["label"] for row in data["items"]], expected_labels)
        self.assertTrue(all(set(row) == {"key", "label", "state", "value", "hint"} for row in data["items"]))
        self.assertTrue(all(row["state"] == "ok" for row in data["items"]))

    def test_selfcheck_probes_run_concurrently_and_keep_row_order(self) -> None:
        """7 个外部探测并发跑：串行要 1.4 秒，并发应明显更短，且 8 行顺序/字段都不变。"""
        def slow_payload(payload):
            def probe(_handler):
                time.sleep(0.2)
                return dict(payload) if isinstance(payload, dict) else payload
            return probe

        expected_keys = ["qq", "hosting", "dingtalk", "wechat", "chaoxing", "attachments", "calendar", "push"]
        patches = (
            mock.patch.object(webapp._Handler, "_overview_payload", return_value={"ok": True, "sources": {}}),
            mock.patch.object(webapp._Handler, "_napcat_doctor_payload", slow_payload({"ok": True})),
            mock.patch.object(webapp._Handler, "_napcat_status_payload", slow_payload({"ok": True, "nickname": "Bot"})),
            mock.patch.object(webapp._Handler, "_hosting_status_payload", slow_payload({"hosting_active": True, "groups_selected": 2})),
            mock.patch.object(webapp._Handler, "_dingtalk_status_payload", slow_payload({"running": True, "groups": 1})),
            mock.patch.object(webapp._Handler, "_wechat_status_payload", slow_payload({"available": True, "running": True, "groups": 1})),
            mock.patch.object(webapp._Handler, "_chaoxing_status_payload", slow_payload({"expired": False, "last_sync_at": "now"})),
            mock.patch.object(
                webapp._Handler,
                "_sync_info_response",
                slow_payload((200, {"calendars": [{"kind": "public", "events": 3}], "events": 3})),
            ),
            mock.patch.object(self.settings, "push_channels", return_value=[]),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
            started = time.monotonic()
            data = self._get("/api/selfcheck", "secret")
            elapsed = time.monotonic() - started
        self.assertEqual([row["key"] for row in data["items"]], expected_keys)
        self.assertEqual(len(data["items"]), 8)
        self.assertTrue(all(set(row) == {"key", "label", "state", "value", "hint"} for row in data["items"]))
        self.assertLess(elapsed, 1.0, "7 项各睡 0.2 秒：串行要 1.4 秒，并发应 <1 秒（实测 %.2f）" % elapsed)

    def test_selfcheck_timeout_demotes_only_that_row(self) -> None:
        """单项超时只让它自己变 warn 并写明「检测超时」，其余项照常出结果。"""
        def hang(_handler):
            time.sleep(1.5)
            return {"available": True, "running": True, "groups": 1}

        with mock.patch.object(webapp._Handler, "_overview_payload", return_value={"ok": True, "sources": {}}), \
             mock.patch.object(webapp._Handler, "_napcat_doctor_payload", return_value={"ok": True}), \
             mock.patch.object(webapp._Handler, "_napcat_status_payload", return_value={"ok": True, "nickname": "Bot"}), \
             mock.patch.object(webapp._Handler, "_hosting_status_payload", return_value={"hosting_active": True, "groups_selected": 2}), \
             mock.patch.object(webapp._Handler, "_dingtalk_status_payload", return_value={"running": True, "groups": 1}), \
             mock.patch.object(webapp._Handler, "_wechat_status_payload", hang), \
             mock.patch.object(webapp._Handler, "_chaoxing_status_payload", return_value={"expired": False, "last_sync_at": "now"}), \
             mock.patch.object(webapp._Handler, "_sync_info_response", return_value=(200, {"calendars": [{"kind": "public", "events": 3}], "events": 3})), \
             mock.patch.object(self.settings, "push_channels", return_value=[]), \
             mock.patch.object(webapp, "SELFCHECK_ITEM_TIMEOUT_SECONDS", 0.2):
            started = time.monotonic()
            data = self._get("/api/selfcheck", "secret")
            elapsed = time.monotonic() - started
        rows = {row["key"]: row for row in data["items"]}
        self.assertEqual(len(data["items"]), 8, "超时不能让自检少一行")
        self.assertEqual(rows["wechat"]["state"], "warn")
        self.assertEqual(rows["wechat"]["value"], "正在检测", "冷启动没查完说「正在检测」，不说「检测超时」")
        self.assertIn("还没查完", rows["wechat"]["hint"])
        self.assertNotEqual(rows["qq"]["value"], "正在检测", "别的项照常给结果")
        self.assertNotEqual(rows["calendar"]["value"], "正在检测")
        self.assertLess(elapsed, 1.4, "超时项不能拖住整块自检（实测 %.2f）" % elapsed)

    def test_selfcheck_timeout_does_not_probe_twice(self) -> None:
        """超时之后总览不许重探：每个探测在整个请求里只能跑一次（冷检 37.89s 就是这么来的）。"""
        calls = {key: 0 for key in ("doctor", "napcat", "hosting", "sync", "dingtalk", "wechat", "chaoxing")}

        def payload_of(key):
            return {
                "doctor": {"ok": True},
                "napcat": {"ok": True, "nickname": "Bot"},
                "hosting": {"hosting_active": True, "groups_selected": 2},
                "sync": (200, {"calendars": [{"kind": "public", "events": 3}], "events": 3}),
                "dingtalk": {"running": True, "groups": 1},
                "wechat": {"available": True, "running": True, "groups": 1},
                "chaoxing": {"expired": False, "last_sync_at": "now"},
            }[key]

        def make(key, delay=0.0):
            def probe(_handler):
                calls[key] += 1
                if delay:
                    time.sleep(delay)
                return payload_of(key)
            return probe

        with mock.patch.object(webapp._Handler, "_napcat_doctor_payload", make("doctor")), \
             mock.patch.object(webapp._Handler, "_napcat_status_payload", make("napcat")), \
             mock.patch.object(webapp._Handler, "_hosting_status_payload", make("hosting")), \
             mock.patch.object(webapp._Handler, "_sync_info_response", make("sync")), \
             mock.patch.object(webapp._Handler, "_dingtalk_status_payload", make("dingtalk")), \
             mock.patch.object(webapp._Handler, "_wechat_status_payload", make("wechat", 5.0)), \
             mock.patch.object(webapp._Handler, "_chaoxing_status_payload", make("chaoxing")), \
             mock.patch.object(self.settings, "push_channels", return_value=[]), \
             mock.patch.object(webapp, "SELFCHECK_ITEM_TIMEOUT_SECONDS", 0.3):
            started = time.monotonic()
            data = self._get("/api/selfcheck", "secret")
            elapsed = time.monotonic() - started
        self.assertEqual(calls, {key: 1 for key in calls}, "每一项只能探一次：总览不许重探")
        self.assertEqual(len(data["items"]), 8)
        # 卡住的探测睡 5 秒；15 秒预算不影响，这里只给 0.3 秒 + 2 秒收尾，所以必须远小于 5 秒。
        self.assertLess(elapsed, 3.5, "超时后应当直接返回，不能再等一轮（实测 %.2f）" % elapsed)

    def test_selfcheck_over_http_uses_overview_sources(self) -> None:
        """走真实 HTTP：overview 的 error 必须透到自检面板（外层 dict 会被拆成 sources）。"""
        def fake_overview(_handler):
            return {
                "ok": True,
                "sources": {
                    "wechat": {"state": "error", "detail": "解密失败：找不到密钥", "action": "点「重新扫描微信群」"},
                },
            }

        with mock.patch.object(webapp._Handler, "_overview_payload", fake_overview), \
                mock.patch.object(webapp._Handler, "_napcat_doctor_payload", return_value={"ok": True}), \
                mock.patch.object(webapp._Handler, "_napcat_status_payload", return_value={"ok": True}), \
                mock.patch.object(webapp._Handler, "_sync_info_response", return_value=(200, {})), \
                mock.patch.object(self.settings, "push_channels", return_value=[]):
            data = self._get("/api/selfcheck", "secret")
        row = next(item for item in data["items"] if item["key"] == "wechat")
        self.assertEqual((row["state"], row["value"], row["hint"]), ("bad", "解密失败：找不到密钥", "点「重新扫描微信群」"))

    def test_selfcheck_api_explains_unselected_groups_and_missing_wechat(self) -> None:
        patches = (
            mock.patch.object(
                webapp._Handler,
                "_overview_payload",
                return_value={"ok": True, "sources": {
                    "dingtalk": {"state": "needs_setup", "detail": "还没选监听群", "action": "去设置里选群"},
                    "wechat": {"state": "needs_setup", "detail": "没找到微信数据目录", "action": "确认本机微信已登录过一次"},
                }},
            ),
            mock.patch.object(webapp._Handler, "_napcat_doctor_payload", return_value={"ok": True}),
            mock.patch.object(webapp._Handler, "_napcat_status_payload", return_value={"ok": True}),
            mock.patch.object(webapp._Handler, "_hosting_status_payload", return_value={"hosting_active": True, "groups_selected": 1}),
            mock.patch.object(webapp._Handler, "_dingtalk_status_payload", return_value={"running": True, "groups": 0, "scan_state": "idle"}),
            mock.patch.object(webapp._Handler, "_wechat_status_payload", return_value={"available": False, "running": False, "groups": 0, "scan_state": "idle"}),
            mock.patch.object(webapp._Handler, "_chaoxing_status_payload", return_value={"expired": False, "last_sync_at": "now"}),
            mock.patch.object(webapp._Handler, "_sync_info_response", return_value=(200, {"calendars": [{"kind": "public", "events": 1}], "events": 1})),
            mock.patch.object(self.settings, "push_channels", return_value=["飞书"]),
        )
        with mock.patch.multiple(self.settings, dingtalk_enabled=True, dingtalk_group_whitelist=(), wechat_enabled=True, wechat_group_whitelist=()):
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
                items = self._get("/api/selfcheck", "secret")["items"]
        by_key = {row["key"]: row for row in items}
        self.assertEqual(by_key["dingtalk"]["state"], "warn")
        # 判定来源已统一到 overview：文案就是它给的 detail/action，不再各写一套。
        self.assertEqual(by_key["dingtalk"]["value"], "还没选监听群")
        self.assertEqual(by_key["dingtalk"]["hint"], "去设置里选群")
        self.assertEqual(by_key["wechat"]["state"], "warn")
        self.assertEqual(by_key["wechat"]["value"], "没找到微信数据目录")
        self.assertEqual(by_key["wechat"]["hint"], "确认本机微信已登录过一次")
        self.assertNotIn("已运行并登录", by_key["dingtalk"]["value"])

    def test_phone_pairing_flow_over_http(self) -> None:
        """手机一开始没有令牌；只有电脑上点了「允许」，手机才拿到令牌。"""
        pairing.reset()
        self.addCleanup(pairing.reset)
        started = self._post("/api/pair/start", {}, token="")
        self.assertEqual(len(started["code"]), 6)
        # 免令牌的只有「发起配对」和「用自己的码查结果」，别的接口仍然要令牌
        status, _, _ = self._raw_request("/api/pair/pending", token="")
        self.assertEqual(status, 401)
        waiting = self._get("/api/pair/pending", token="secret")["pending"]
        self.assertEqual([row["code"] for row in waiting], [started["code"]])
        query = urllib.parse.urlencode({"code": started["code"], "secret": started["secret"]})
        pending_state = self._get(f"/api/pair/status?{query}")
        self.assertEqual(pending_state["status"], "pending")
        self.assertNotIn("token", pending_state)
        # 光知道码、没有 secret 的手机问不出任何东西
        wrong = urllib.parse.urlencode({"code": started["code"], "secret": "nope"})
        status, _, _ = self._raw_request(f"/api/pair/status?{wrong}", token="")
        self.assertEqual(status, 404)
        self.assertTrue(self._post("/api/pair/approve", {"code": started["code"]})["ok"])
        approved = self._get(f"/api/pair/status?{query}")
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["token"], "secret")
        self.assertEqual(self._get("/api/pair/pending", token="secret")["pending"], [])

    def test_pair_status_rejects_oversized_code_and_secret(self) -> None:
        """WEB-4: /api/pair/status is unauthenticated, so bound what it will compare."""
        pairing.reset()
        self.addCleanup(pairing.reset)
        # Code far longer than the 6 digits pairing ever issues.
        long_code = urllib.parse.urlencode({"code": "1" * 5000, "secret": "x" * 32})
        status, _, _ = self._raw_request(f"/api/pair/status?{long_code}", token="")
        self.assertEqual(status, 404)
        # Oversized secret but still inside the 64 KiB request-line limit, so we are
        # testing OUR length cap and not the transport-level reset.
        long_secret = urllib.parse.urlencode({"code": "123456", "secret": "x" * 5000})
        status, _, _ = self._raw_request(f"/api/pair/status?{long_secret}", token="")
        self.assertEqual(status, 404)

    def test_pair_status_still_works_for_a_real_pairing(self) -> None:
        """Guard against the length cap rejecting legitimate codes."""
        pairing.reset()
        self.addCleanup(pairing.reset)
        started = self._post("/api/pair/start", {}, token="")
        query = urllib.parse.urlencode({"code": started["code"], "secret": started["secret"]})
        self.assertEqual(self._get(f"/api/pair/status?{query}")["status"], "pending")

    def test_request_line_logging_redacts_the_token(self) -> None:
        """WEB-2: the access token must never reach the log file."""
        from noticehub.webapp import _redact_secrets

        self.assertEqual(
            _redact_secrets('"GET /api/tasks?token=supersecretvalue HTTP/1.1"'),
            '"GET /api/tasks?token=*** HTTP/1.1"',
        )
        # Case-insensitive, and other secret-ish keys too.
        self.assertNotIn("abc123", _redact_secrets("/x?TOKEN=abc123"))
        self.assertNotIn("abc123", _redact_secrets("/x?secret=abc123&y=1"))
        # Non-secret params survive so the line stays useful.
        self.assertIn("group_id=42", _redact_secrets("/x?group_id=42&token=abc123"))

    def test_auth_accepts_token_via_query_and_header(self) -> None:
        """WEB-1 changed the comparison to hmac.compare_digest; both paths still work."""
        self.assertEqual(self._get("/api/pair/info", token="secret")["ok"], True)
        status, _, _ = self._raw_request("/api/pair/info", token="wrong-token")
        self.assertEqual(status, 401)

    def test_pair_candidate_kinds_are_deterministic(self) -> None:
        cases = {
            "192.168.1.5": "lan",
            "10.23.4.5": "lan",
            "172.16.0.5": "lan",
            "172.17.0.2": "lan",
            "172.18.3.9": "lan",
            "172.20.10.3": "cellular",
            "172.21.5.5": "lan",
            "172.31.255.9": "lan",
            "100.109.123.59": "tailscale",
            "192.168.56.1": "virtual",
            "192.168.99.1": "virtual",
            "192.168.122.1": "virtual",
            "8.8.8.8": "other",
        }
        for address, expected in cases.items():
            with self.subTest(address=address):
                self.assertEqual(webapp._pair_host_kind(address), expected)

    def test_pair_info_lists_all_candidates_and_recommends_lan_first(self) -> None:
        with mock.patch("noticehub.webapp._tailscale_public_app_url", return_value=""), mock.patch(
            "noticehub.webapp._lan_hosts",
            return_value=["172.20.10.3", "100.109.123.59", "192.168.56.1", "192.168.1.5"],
        ):
            info = self._get("/api/pair/info", token="secret")
        port = self.settings.web_port
        self.assertEqual(info["base"], "http://192.168.1.5:%d" % port)
        self.assertEqual([item["url"] for item in info["candidates"]], [
            "http://192.168.1.5:%d" % port,
            "http://100.109.123.59:%d" % port,
            "http://172.20.10.3:%d" % port,
            "http://192.168.56.1:%d" % port,
        ])
        self.assertEqual(info["candidates"][0]["kind"], "lan")
        self.assertTrue(info["candidates"][0]["recommended"])
        self.assertEqual(sum(item["recommended"] for item in info["candidates"]), 1)
        self.assertEqual(info["lan"], [
            "http://172.20.10.3:%d" % port,
            "http://100.109.123.59:%d" % port,
            "http://192.168.56.1:%d" % port,
            "http://192.168.1.5:%d" % port,
        ])

    def test_pair_info_keeps_lan_first_and_offers_two_public_channels(self) -> None:
        public = "https://box.ts.net"
        with mock.patch("noticehub.webapp._tailscale_public_app_url", return_value=public), mock.patch(
            "noticehub.webapp._lan_hosts", return_value=["100.109.123.59", "192.168.1.5"]
        ):
            info = self._get("/api/pair/info", token="secret")
        port = self.settings.web_port
        self.assertEqual(info["base"], "http://192.168.1.5:%d" % port)
        self.assertEqual([item["url"] for item in info["candidates"]], [
            "http://192.168.1.5:%d" % port,
            "http://100.109.123.59:%d" % port,
            public,
        ])
        self.assertTrue(info["candidates"][0]["recommended"])
        self.assertEqual(sum(item["recommended"] for item in info["candidates"]), 1)
        steps = " ".join(info["steps"])
        self.assertIn("在同一个 Wi-Fi 下，用局域网地址即可，无需公网入口。", steps)
        self.assertIn("不在同一个 Wi-Fi 下时，可开通 Cloudflare 快速通道（不需要注册账号）", steps)
        self.assertIn("或安装 Tailscale 后使用固定地址。", steps)
        self.assertIn("6 位数字", steps)
        # 旧口径（把「加入同一个 Tailscale VPN」「Funnel」当唯一异地方案）不得回潮。
        self.assertNotIn("同一个 Tailscale VPN", steps)
        self.assertNotIn("Funnel", steps)

    def test_pair_info_recommends_first_non_lan_candidate_when_no_lan_exists(self) -> None:
        with mock.patch("noticehub.webapp._tailscale_public_app_url", return_value=""), mock.patch(
            "noticehub.webapp._lan_hosts", return_value=["8.8.8.8"]
        ):
            info = self._get("/api/pair/info", token="secret")
        self.assertTrue(info["candidates"])
        self.assertEqual(info["candidates"][0]["kind"], "other")
        self.assertTrue(info["candidates"][0]["recommended"])
        self.assertEqual(info["base"], info["candidates"][0]["url"])

    def test_pair_host_kind_accepts_non_ip_text(self) -> None:
        with mock.patch("noticehub.webapp._tailscale_public_app_url", return_value=""):
            self.assertEqual(webapp._pair_host_kind("不是IP的字符串"), "other")

    def test_pair_page_uses_recommended_candidate_with_base_fallback(self) -> None:
        self.assertIn("Array.isArray(data.candidates)", PAGE_SOURCE)
        self.assertIn("candidates.find(function (item) { return item.recommended; })", PAGE_SOURCE)
        self.assertIn("id=\"pair-candidate\"", PAGE_SOURCE)
        self.assertIn("candidateSelect.onchange", PAGE_SOURCE)
        self.assertIn("var pairUrl = (recommended && recommended.url) || data.base || '';", PAGE_SOURCE)
        self.assertIn("input.value = pairUrl;", PAGE_SOURCE)
        self.assertIn("encodeURIComponent(pairUrl)", PAGE_SOURCE)
        self.assertIn("hint.textContent = '手机与电脑在同一个 Wi-Fi 下时，用局域网地址即可；不在同一个 Wi-Fi 下时，请在下方「公网入口」中选择一条通道开通。';", PAGE_SOURCE)
        self.assertNotIn("同一 Wi-Fi 下用局域网地址；异地请在下方", PAGE_SOURCE)
        # 这句文案在 webapp.py 里出现两次（服务端错误、前端 JS 的 else 分支），
        # 只断言文案本身钉不牢：改掉前端那处、服务端那处还在，断言照样绿。
        # 连上它所在的那一整行 JS，才能确定钉的是前端那一处。
        self.assertIn("qrMessage.textContent = '没有检测到可供手机访问的地址。';", PAGE_SOURCE)

    def test_pair_decision_validates_the_code_and_pair_info_is_copyable(self) -> None:
        pairing.reset()
        self.addCleanup(pairing.reset)
        status, _, _ = self._raw_request("/api/pair/approve", method="POST", payload={"code": "123"})
        self.assertEqual(status, 400)
        with mock.patch("noticehub.webapp._tailscale_public_app_url", return_value=""), mock.patch(
            "noticehub.webapp._tailscale_executable", return_value=""
        ):
            info = self._get("/api/pair/info", token="secret")
        self.assertTrue(info["base"].startswith("http"))
        self.assertFalse(info["public_enabled"])
        self.assertFalse(info["tailscale_ready"])
        self.assertGreaterEqual(len(info["steps"]), 2)

    def test_pair_qr_explains_missing_token_and_listener_mismatch(self) -> None:
        server = self.server.server
        self.assertIsNotNone(server)
        assert server is not None
        old_token = server.token
        try:
            server.token = ""
            with (
                mock.patch("noticehub.webapp._tailscale_public_app_url", return_value=""),
                mock.patch("noticehub.webapp._tailscale_executable", return_value=""),
                mock.patch("noticehub.webapp._lan_hosts", return_value=["100.78.42.0"]),
            ):
                info = self._get("/api/pair/info", token="")
                self.assertIn("QQ_DIGEST_WEB_TOKEN", info["qr_error"])
                self.assertIn("只监听本机回环地址", info["qr_error"])
                self.assertNotIn("token", info)
                status, headers, body = self._raw_request(
                    "/api/app/qr.png?" + urllib.parse.urlencode({"base": "http://100.78.42.0:8790", "token": "test-only"}),
                    token="",
                )
            self.assertEqual(status, 400)
            self.assertEqual(headers.get_content_type(), "application/json")
            self.assertIn("QQ_DIGEST_WEB_TOKEN", json.loads(body)["error"])

            server.token = old_token
            with (
                mock.patch("noticehub.webapp._tailscale_public_app_url", return_value=""),
                mock.patch("noticehub.webapp._tailscale_executable", return_value=""),
                mock.patch("noticehub.webapp._lan_hosts", return_value=["100.78.42.0"]),
            ):
                info = self._get("/api/pair/info", token="secret")
                status, headers, body = self._raw_request(
                    "/api/app/qr.png?" + urllib.parse.urlencode({"base": "http://100.78.42.0:8790", "token": "secret"}),
                    token="",
                )
            self.assertEqual(status, 400)
            self.assertEqual(headers.get_content_type(), "application/json")
            self.assertIn("只监听本机回环地址", json.loads(body)["error"])
            self.assertIn("只监听本机回环地址", info["qr_error"])
            self.assertIn("data.qr_error", PAGE_SOURCE)
            self.assertIn("qr.onerror", PAGE_SOURCE)
            self.assertIn("二维码加载失败", PAGE_SOURCE)
        finally:
            server.token = old_token

    def test_pair_public_refuses_empty_token_and_keeps_existing_behavior(self) -> None:
        server = self.server.server
        assert server is not None
        # 老页面走的是 Tailscale 那条路（不带 provider），这条用例就锁那条路的行为。
        self.settings.tunnel_provider = "tailscale"
        old_token = server.token
        setup = webapp._funnel_setup(server.settings.web_port)
        try:
            server.token = ""
            with mock.patch.object(webapp.tunnel.FunnelSetup, "start") as start:
                status, headers, body = self._raw_request("/api/pair/public", method="POST", payload={}, token="")
            self.assertEqual(status, 400)
            self.assertEqual(headers.get_content_type(), "application/json")
            self.assertIn("请先设置访问令牌再开到公网", json.loads(body)["error"])
            # 空 token 时绝不能真的去开 Funnel：开了就等于整站无鉴权暴露到公网。
            start.assert_not_called()

            server.token = old_token
            with mock.patch.object(setup, "start", return_value={"state": "installing", "message": "正在检查…"}) as start:
                status, headers, body = self._raw_request("/api/pair/public", method="POST", payload={})
            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertTrue(payload["ok"])
            # 开通是后台任务：请求必须立刻返回状态机的阶段，不能同步等到 Funnel 开完。
            self.assertEqual(payload["state"], "installing")
            start.assert_called_once_with()
        finally:
            server.token = old_token

    def test_tunnel_status_reports_ready_url_when_funnel_rule_exists(self) -> None:
        # 这条锁的是 Tailscale 通道：Cloudflare 成了默认，必须显式声明在测哪条路。
        self.settings.tunnel_provider = "tailscale"
        with (
            mock.patch("noticehub.webapp._tailscale_public_app_url", return_value="https://demo.ts.net:8443"),
            mock.patch("noticehub.webapp._tailscale_executable", return_value="tailscale"),
        ):
            status = self._get("/api/tunnel/status", token="secret")
        self.assertTrue(status["ok"])
        self.assertEqual(status["state"], "ready")
        self.assertEqual(status["url"], "https://demo.ts.net:8443")
        self.assertFalse(status["retryable"])

    def test_tunnel_status_reports_state_machine_stage_when_not_enabled(self) -> None:
        self.settings.tunnel_provider = "tailscale"
        port = self.server.server.settings.web_port
        setup = webapp._funnel_setup(port)
        with (
            mock.patch("noticehub.webapp._tailscale_public_app_url", return_value=""),
            mock.patch("noticehub.webapp._tailscale_executable", return_value=""),
            mock.patch.object(setup, "snapshot", return_value={"state": "waiting_login", "message": "请在浏览器里登录", "url": "", "auth_url": "https://login.tailscale.com/a/x", "retryable": True}),
        ):
            status = self._get("/api/tunnel/status", token="secret")
        self.assertEqual(status["state"], "waiting_login")
        self.assertEqual(status["auth_url"], "https://login.tailscale.com/a/x")
        self.assertFalse(status["installed"])

    def test_tunnel_start_requires_token(self) -> None:
        server = self.server.server
        old_token = server.token
        try:
            server.token = ""
            with mock.patch.object(webapp.tunnel.FunnelSetup, "start") as start:
                status, _, body = self._raw_request("/api/tunnel/start", method="POST", payload={}, token="")
            self.assertEqual(status, 400)
            self.assertIn("请先设置访问令牌再开到公网", json.loads(body)["error"])
            start.assert_not_called()
        finally:
            server.token = old_token

    def test_tunnel_stop_requires_a_token(self) -> None:
        status, _, body = self._raw_request("/api/tunnel/stop", method="POST", payload={}, token="")
        self.assertEqual(status, 401)
        self.assertFalse(json.loads(body)["ok"])

    def test_tunnel_stop_off_returns_ok_and_reports_failure_with_reason(self) -> None:
        # stop 现在按当前通道分流：这条锁 Tailscale 那条路的 off 行为。
        self.settings.tunnel_provider = "tailscale"
        self.settings.log_dir = self.settings.data_dir / "logs"  # 审计别写进仓库 logs/
        with mock.patch.dict(os.environ, {"NH_TEST_MODE": "", "QQ_DIGEST_HOST_ACTIONS": ""}):
            with mock.patch.object(
                webapp.tunnel, "disable_funnel", return_value={"ok": True, "message": "公网入口已关闭"}
            ) as disable:
                status, body = self._lab_request("POST", "/api/tunnel/stop", payload={})
            self.assertEqual(status, 200, body)
            self.assertTrue(body["ok"])
            self.assertEqual(body["url"], "")
            disable.assert_called_once_with(self.settings.web_port)

            with mock.patch.object(
                webapp.tunnel,
                "disable_funnel",
                return_value={"ok": False, "error": "Tailscale 说关了，但公网地址还挂着，规则没真撤掉。"},
            ):
                status, body = self._lab_request("POST", "/api/tunnel/stop", payload={})
            self.assertEqual(status, 400, "没真关掉就不能回 200")
            self.assertFalse(body["ok"])
            self.assertIn("规则没真撤掉", body["error"])

    def test_tunnel_stop_is_blocked_in_test_mode(self) -> None:
        with mock.patch.dict(os.environ, {"NH_TEST_MODE": "1"}):
            with mock.patch.object(webapp.tunnel, "disable_funnel") as disable:
                status, _body = self._lab_request("POST", "/api/tunnel/stop", payload={})
        self.assertEqual(status, 403)
        disable.assert_not_called()

    def test_tunnel_status_reports_both_providers_and_follows_the_active_one(self) -> None:
        # Cloudflare 是默认通道：顶层字段必须代表 cloudflare，而且不能为了它去跑 Tailscale CLI。
        with (
            mock.patch.object(webapp, "_tailscale_public_app_url", return_value="https://demo.ts.net:8443") as live,
            mock.patch.object(webapp.tunnel, "tailscale_executable", return_value=""),
        ):
            status = self._get("/api/tunnel/status", token="secret")
        live.assert_not_called()
        self.assertEqual(status["provider"], "cloudflare")
        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["url"], "")
        self.assertTrue(status["url_changes_on_restart"], "快速通道的地址每次重开都会变，前端要提示")
        self.assertEqual(sorted(status["providers"]), ["cloudflare", "tailscale"])
        self.assertEqual(
            sorted(status["providers"]["cloudflare"]), ["installed", "message", "state", "url"]
        )
        self.assertEqual(sorted(status["providers"]["tailscale"]), ["installed", "message", "state", "url"])

        # 切到 Tailscale 之后，顶层改成跟 tailscale，而且 url_changes_on_restart 要变回 False。
        self.settings.tunnel_provider = "tailscale"
        with (
            mock.patch.object(webapp, "_tailscale_public_app_url", return_value="https://demo.ts.net:8443"),
            mock.patch.object(webapp.tunnel, "tailscale_executable", return_value="tailscale"),
        ):
            switched = self._get("/api/tunnel/status", token="secret")
        self.assertEqual(switched["provider"], "tailscale")
        self.assertEqual(switched["state"], "ready")
        self.assertEqual(switched["url"], "https://demo.ts.net:8443")
        self.assertFalse(switched["url_changes_on_restart"])
        self.assertTrue(switched["installed"])
        self.assertEqual(switched["providers"]["tailscale"]["url"], "https://demo.ts.net:8443")
        self.assertEqual(switched["providers"]["cloudflare"]["state"], "idle")

    def test_tunnel_start_needs_confirm_but_the_old_pair_path_still_works(self) -> None:
        # 两个路径共用同一个 handler：新路径必须显式 confirm，旧路径保留旧行为，
        # 否则老页面点「开通」会突然被 400 挡住。
        with mock.patch.dict(os.environ, {"NH_TEST_MODE": "", "QQ_DIGEST_HOST_ACTIONS": ""}):
            with mock.patch.object(
                webapp.cloudflare.QuickTunnel,
                "start",
                return_value={"state": "idle", "message": "", "url": "", "installed": True},
            ) as quick_start:
                status, body = self._lab_request("POST", "/api/tunnel/start", payload={})
                self.assertEqual(status, 400, body)
                self.assertTrue(body.get("needs_confirm"))
                quick_start.assert_not_called()

                status, body = self._lab_request("POST", "/api/pair/public", payload={})
                self.assertEqual(status, 200, body)
                self.assertTrue(body["ok"])
                quick_start.assert_called_once_with()

    def test_tunnel_start_routes_to_the_requested_provider_only(self) -> None:
        with mock.patch.dict(os.environ, {"NH_TEST_MODE": "", "QQ_DIGEST_HOST_ACTIONS": ""}):
            # 默认通道是 cloudflare：只能碰 QuickTunnel，绝不能顺带去动 Tailscale 的 Funnel。
            with (
                mock.patch.object(
                    webapp.cloudflare.QuickTunnel,
                    "start",
                    return_value={"state": "downloading", "message": "正在下载通道程序…", "url": "", "installed": False},
                ) as quick_start,
                mock.patch.object(webapp.tunnel.FunnelSetup, "start") as funnel_start,
            ):
                status, body = self._lab_request(
                    "POST", "/api/tunnel/start", payload={"confirm": True}
                )
            self.assertEqual(status, 200, body)
            self.assertEqual(body["provider"], "cloudflare")
            self.assertEqual(body["state"], "downloading")
            quick_start.assert_called_once_with()
            funnel_start.assert_not_called()

            # 显式点名 tailscale 时反过来：只跑 Funnel，不下载也不起 cloudflared。
            with (
                mock.patch.object(webapp.cloudflare.QuickTunnel, "start") as quick_start,
                mock.patch.object(
                    webapp.tunnel.FunnelSetup,
                    "start",
                    return_value={"state": "installing", "message": "正在装 Tailscale…"},
                ) as funnel_start,
            ):
                status, body = self._lab_request(
                    "POST", "/api/tunnel/start", payload={"confirm": True, "provider": "tailscale"}
                )
            self.assertEqual(status, 200, body)
            self.assertEqual(body["provider"], "tailscale")
            funnel_start.assert_called_once_with()
            quick_start.assert_not_called()

            # 乱填的通道名要当场拒掉，不能默默回落到某一条通道上开出去。
            with mock.patch.object(webapp.cloudflare.QuickTunnel, "start") as quick_start:
                status, body = self._lab_request(
                    "POST", "/api/tunnel/start", payload={"confirm": True, "provider": "ngrok"}
                )
            self.assertEqual(status, 400, body)
            self.assertIn("只有两种", body["error"])
            quick_start.assert_not_called()

    def test_tunnel_stop_stops_only_the_active_provider(self) -> None:
        self.settings.log_dir = self.settings.data_dir / "logs"  # 审计别写进仓库 logs/
        quick = webapp._quick_tunnel(self.settings)
        with mock.patch.dict(os.environ, {"NH_TEST_MODE": "", "QQ_DIGEST_HOST_ACTIONS": ""}):
            with (
                mock.patch.object(quick, "stop") as quick_stop,
                mock.patch.object(webapp.tunnel, "disable_funnel") as disable,
            ):
                status, body = self._lab_request("POST", "/api/tunnel/stop", payload={})
            self.assertEqual(status, 200, body)
            self.assertEqual(body["provider"], "cloudflare")
            self.assertEqual(body["state"], "idle")
            self.assertEqual(body["url"], "")
            quick_stop.assert_called_once_with()
            disable.assert_not_called()

            # 切到 tailscale 后，同一个按钮必须去关 Funnel，而不是停已经不在用的快速通道。
            self.settings.tunnel_provider = "tailscale"
            with (
                mock.patch.object(quick, "stop") as quick_stop,
                mock.patch.object(
                    webapp.tunnel, "disable_funnel", return_value={"ok": True, "message": "公网入口已关闭"}
                ) as disable,
            ):
                status, body = self._lab_request("POST", "/api/tunnel/stop", payload={})
            self.assertEqual(status, 200, body)
            self.assertEqual(body["provider"], "tailscale")
            disable.assert_called_once_with(self.settings.web_port)
            quick_stop.assert_not_called()

    def test_stop_returns_only_after_in_flight_requests_finish(self) -> None:
        # daemon_threads=True 时 server_close() 不等在处理的请求：stop() 已返回、后台线程
        # 还在改状态。实测就是这样漏的——上一条用例的 /api/tunnel/stop 晚一步跑进了这一条
        # 的打桩窗口，把「只关一次」打成两次。这里锁住「stop() 必须等请求做完」。
        entered = threading.Event()
        release = threading.Event()
        handler_finished_at: list[float] = []
        stop_returned_at: list[float] = []
        original = webapp._Handler._route_get_health

        def slow_health(handler, path):
            entered.set()
            release.wait(10)
            original(handler, path)
            handler_finished_at.append(time.monotonic())

        patcher = mock.patch.object(webapp._Handler, "_route_get_health", slow_health)
        patcher.start()
        self.addCleanup(patcher.stop)
        client_errors: list[BaseException] = []

        def slow_client() -> None:
            try:
                with urllib.request.urlopen(self.base + "/api/health", timeout=15) as response:
                    response.read()
            except BaseException as error:  # noqa: BLE001 - 只记录，不影响断言
                client_errors.append(error)

        client = threading.Thread(target=slow_client, daemon=True)
        client.start()
        try:
            self.assertTrue(entered.wait(10), "慢请求没进到处理函数里")

            def stopper() -> None:
                self.server.stop()
                stop_returned_at.append(time.monotonic())

            stop_thread = threading.Thread(target=stopper, daemon=True)
            stop_thread.start()
            # 给关闭流程（shutdown 的 0.5 秒轮询、join 线程）足够时间跑完：
            # 请求仍被 release 卡着，所以有 drain 的实现在这一刻一定还没返回。
            time.sleep(1.0)
        finally:
            release.set()
        stop_thread.join(10)
        client.join(10)
        self.assertTrue(stop_returned_at, "stop() 没在请求结束后返回")
        self.assertTrue(handler_finished_at, "慢请求没走完处理函数")
        self.assertGreaterEqual(
            stop_returned_at[0],
            handler_finished_at[0],
            "stop() 先返回了，请求还在后面跑：这种情况下它还会改已关闭实例的状态",
        )
        self.assertEqual(client_errors, [])

    def test_tunnel_provider_is_saved_and_takes_effect_without_restart(self) -> None:
        env_path = Path(self.tmp.name) / "tunnel-provider.env"
        env_path.write_text("QQ_DIGEST_TOKEN=abc\n", encoding="utf-8")
        self.settings.env_file = str(env_path)
        try:
            status, body = self._lab_request(
                "POST", "/api/settings", payload={"tunnel_provider": "tailscale"}
            )
            self.assertEqual(status, 200, body)
            self.assertEqual(body["tunnel_provider"], "tailscale")
            self.assertEqual(self.settings.tunnel_provider, "tailscale", "存完当场生效，不必重启")
            self.assertEqual(os.environ.get("NOTICEHUB_TUNNEL_PROVIDER"), "tailscale")
            self.assertIn("NOTICEHUB_TUNNEL_PROVIDER=tailscale", env_path.read_text(encoding="utf-8"))

            with mock.patch.object(webapp, "_tailscale_public_app_url", return_value=""), mock.patch.object(
                webapp.tunnel, "tailscale_executable", return_value=""
            ):
                status_payload = self._get("/api/tunnel/status", token="secret")
            self.assertEqual(status_payload["provider"], "tailscale")

            before = env_path.read_text(encoding="utf-8")
            status, body = self._lab_request(
                "POST", "/api/settings", payload={"tunnel_provider": "ngrok"}
            )
            self.assertEqual(status, 400, body)
            self.assertIn("只有 cloudflare 或 tailscale", body["error"])
            status, body = self._lab_request(
                "POST", "/api/settings", payload={"tunnel_provider": "cloudflare", "patch_auto": True}
            )
            self.assertEqual(status, 400, body)
            self.assertIn("单独保存", body["error"])
            self.assertEqual(env_path.read_text(encoding="utf-8"), before, "被拒的请求不许动 .env")
            self.assertEqual(self.settings.tunnel_provider, "tailscale", "被拒后内存里的值也不能被改掉")
        finally:
            os.environ.pop("NOTICEHUB_TUNNEL_PROVIDER", None)

    def test_switching_tunnel_provider_closes_the_old_channel(self) -> None:
        # 切通道必须把旧通道真关掉：页面上只显示当前通道，旧通道继续开着就是用户关不掉的公网入口。
        env_path = Path(self.tmp.name) / "tunnel-switch.env"
        env_path.write_text("QQ_DIGEST_TOKEN=abc\n", encoding="utf-8")
        self.settings.env_file = str(env_path)
        self.settings.tunnel_provider = "cloudflare"
        quick = webapp._quick_tunnel(self.settings)
        try:
            with mock.patch.object(quick, "stop") as quick_stop, mock.patch.object(
                webapp.tunnel, "disable_funnel", return_value={"ok": True}
            ) as funnel_off:
                status, body = self._lab_request("POST", "/api/settings", payload={"tunnel_provider": "tailscale"})
                self.assertEqual(status, 200, body)
                quick_stop.assert_called_once_with()
                funnel_off.assert_not_called()

                # 同一个值再存一次：没有「换」，不许再去关一次。
                status, body = self._lab_request("POST", "/api/settings", payload={"tunnel_provider": "tailscale"})
                self.assertEqual(status, 200, body)
                quick_stop.assert_called_once_with()

                status, body = self._lab_request("POST", "/api/settings", payload={"tunnel_provider": "cloudflare"})
                self.assertEqual(status, 200, body)
                funnel_off.assert_called_once()
        finally:
            os.environ.pop("NOTICEHUB_TUNNEL_PROVIDER", None)

    def test_public_calendar_url_follows_the_quick_tunnel(self) -> None:
        port = int(self.settings.web_port)
        quick = webapp._quick_tunnel(self.settings)
        url = "https://neutral-colleges-photographic-softball.trycloudflare.com"
        with (
            mock.patch.object(
                quick,
                "snapshot",
                return_value={"state": "ready", "message": "", "url": url, "installed": True},
            ),
            mock.patch.object(webapp.tunnel, "public_calendar_url") as tailscale_url,
        ):
            self.assertEqual(webapp._tailscale_calendar_url(port), url + "/calendar.ics")
        tailscale_url.assert_not_called()

        # 快速通道没开的时候仍按原来的 Tailscale 地址走，别把日历订阅搞断。
        with mock.patch.object(webapp, "_existing_quick_tunnel", return_value=None), mock.patch.object(
            webapp.tunnel, "public_calendar_url", return_value="https://demo.ts.net:8443/calendar.ics"
        ) as fallback:
            self.assertEqual(
                webapp._tailscale_calendar_url(port), "https://demo.ts.net:8443/calendar.ics"
            )
        fallback.assert_called_once_with(port)

    def _overview(self):
        """固定住四个通道的 status payload，避免真实机器状态影响总览断言。"""
        with mock.patch.object(webapp._Handler, "_napcat_status_payload", return_value={"ok": True, "online": True, "stage": "online", "error": ""}), \
                mock.patch.object(webapp._Handler, "_wechat_status_payload", return_value={"ok": True, "enabled": False}), \
                mock.patch.object(webapp._Handler, "_dingtalk_status_payload", return_value={"ok": True, "enabled": False}), \
                mock.patch.object(webapp._Handler, "_chaoxing_status_payload", return_value={"ok": False, "has_cookie": False, "expired": False, "last_error": "", "last_sync_at": ""}), \
                mock.patch.object(webapp, "_tailscale_public_app_url", return_value=""), \
                mock.patch.object(webapp.tunnel, "tailscale_executable", return_value=""):
            return self._get("/api/overview", token="secret")

    def test_overview_lists_seven_sources_with_four_states(self) -> None:
        body = self._overview()
        self.assertTrue(body["ok"])
        self.assertEqual(
            set(body["sources"]),
            {"qq", "napcat", "wechat", "dingtalk", "chaoxing", "funnel", "push"},
        )
        allowed = {"off", "needs_setup", "ready", "error"}
        for key, source in body["sources"].items():
            self.assertIn(source["state"], allowed, f"{key} 的 state 越界：{source['state']}")
            self.assertEqual(set(source), {"state", "label", "detail", "action"}, key)

    def test_overview_unconfigured_channels_are_off_not_error(self) -> None:
        sources = self._overview()["sources"]
        self.assertEqual(sources["wechat"]["state"], "off")
        self.assertEqual(sources["dingtalk"]["state"], "off")
        self.assertEqual(sources["chaoxing"]["state"], "off")
        self.assertEqual(sources["funnel"]["state"], "off")
        self.assertIn(sources["qq"]["state"], {"ready", "needs_setup"})
        for key in ("wechat", "dingtalk", "chaoxing"):
            self.assertNotEqual(sources[key]["state"], "error", f"{key} 没配不该报 error")

    def test_overview_maps_napcat_error_stage_to_error(self) -> None:
        with mock.patch.object(webapp._Handler, "_napcat_status_payload", return_value={"ok": False, "online": False, "stage": "error", "error": "token 无效"}), \
                mock.patch.object(webapp._Handler, "_wechat_status_payload", return_value={"ok": True, "enabled": False}), \
                mock.patch.object(webapp._Handler, "_dingtalk_status_payload", return_value={"ok": True, "enabled": False}), \
                mock.patch.object(webapp._Handler, "_chaoxing_status_payload", return_value={"ok": False, "has_cookie": False, "expired": False}), \
                mock.patch.object(webapp, "_tailscale_public_app_url", return_value=""), \
                mock.patch.object(webapp.tunnel, "tailscale_executable", return_value=""):
            body = self._get("/api/overview", token="secret")
        self.assertEqual(body["sources"]["napcat"]["state"], "error")
        self.assertIn("token", body["sources"]["napcat"]["detail"])
        self.assertEqual(body["sources"]["qq"]["state"], "off")

    def _onboarding_next(self, sources: dict, *, firewall_allowed: bool = False) -> dict:
        """固定 overview 的五项与防火墙探测，只看步骤机本身怎么顺延。"""
        def fake_overview(_handler):
            return {"ok": True, "sources": sources}

        with mock.patch.object(webapp._Handler, "_overview_payload", fake_overview), \
                mock.patch.object(
                    webapp,
                    "_firewall_allows",
                    return_value={"checked": True, "allowed": firewall_allowed, "port": 1, "command": "netsh"},
                ):
            return self._get("/api/onboarding/next", token="secret")

    def test_onboarding_next_starts_with_qq_when_nothing_configured(self) -> None:
        body = self._onboarding_next({key: {"state": "off"} for key in ("qq", "napcat", "funnel", "push")})
        self.assertTrue(body["ok"])
        self.assertEqual(body["step"], "qq")
        self.assertEqual(body["action"], "connect_qq")
        self.assertTrue(body["blocked"])
        self.assertEqual(body["done"], [])

    def test_onboarding_next_moves_to_napcat_after_qq_ready(self) -> None:
        body = self._onboarding_next(
            {
                "qq": {"state": "ready", "detail": "监控 2 个群"},
                "napcat": {"state": "off", "detail": "还没安装 NapCat 引擎"},
                "funnel": {"state": "off"},
                "push": {"state": "off"},
            }
        )
        self.assertEqual(body["step"], "napcat")
        self.assertEqual(body["action"], "install_napcat")
        self.assertFalse(body["blocked"])
        self.assertEqual(body["done"], ["qq"])
        self.assertIn("NapCat", body["detail"])

    def test_onboarding_next_reports_done_when_all_gates_ready(self) -> None:
        body = self._onboarding_next(
            {key: {"state": "ready"} for key in ("qq", "napcat", "funnel", "push")},
            firewall_allowed=True,
        )
        self.assertEqual(body["step"], "done")
        self.assertEqual(body["done"], ["qq", "napcat", "tailscale", "firewall", "push"])

    def test_onboarding_run_requires_confirm(self) -> None:
        status, body = self._lab_request("POST", "/api/onboarding/run", payload={"action": "connect_qq"})
        self.assertEqual(status, 400)
        self.assertTrue(body.get("needs_confirm"))

    def test_onboarding_run_is_blocked_in_test_mode(self) -> None:
        with mock.patch.dict(os.environ, {"NH_TEST_MODE": "1"}):
            status, _body = self._lab_request(
                "POST", "/api/onboarding/run", payload={"action": "connect_qq", "confirm": True}
            )
        self.assertEqual(status, 403)

    def test_onboarding_run_connect_qq_is_blocked_for_manual_scan(self) -> None:
        self.settings.log_dir = self.settings.data_dir / "logs"  # 审计别写进仓库 logs/
        with mock.patch.dict(os.environ, {"NH_TEST_MODE": "", "QQ_DIGEST_HOST_ACTIONS": ""}):
            status, body = self._lab_request(
                "POST", "/api/onboarding/run", payload={"action": "connect_qq", "confirm": True}
            )
        self.assertEqual(status, 200)
        self.assertTrue(body["blocked"])
        self.assertEqual(body["state"], "blocked")
        self.assertIn("扫二维码", body["message"])

    def test_firewall_allow_defers_to_tray_instead_of_prompting_uac(self) -> None:
        """服务进程不再自己弹 UAC：校验通过后只写请求文件，立即返回 pending。"""
        status, body = self._lab_request("POST", "/api/firewall/allow", payload={})
        self.assertEqual(status, 400)
        self.assertTrue(body.get("needs_confirm"))

        with mock.patch.dict(os.environ, {"NH_TEST_MODE": "1"}):
            status, body = self._lab_request("POST", "/api/firewall/allow", payload={"confirm": True})
        self.assertEqual(status, 403)

        # 审计日志写进临时目录，别污染仓库 logs/。
        self.settings.log_dir = self.settings.data_dir / "logs"
        with mock.patch.dict(os.environ, {"NH_TEST_MODE": "", "QQ_DIGEST_HOST_ACTIONS": ""}):
            status, body = self._lab_request("POST", "/api/firewall/allow", payload={"confirm": True})
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "pending")
        self.assertTrue(body["request_id"])
        self.assertEqual(body["port"], int(self.settings.web_port))

        request_path = self.settings.data_dir / webapp.FIREWALL_REQUEST_NAME
        request = json.loads(request_path.read_text(encoding="utf-8"))
        self.assertEqual(request["port"], int(self.settings.web_port))
        self.assertIn("requested_at", request)
        audit_lines = (self.settings.data_dir / "logs" / "host-actions.log").read_text(encoding="utf-8").splitlines()
        self.assertIn("delegated", [json.loads(line)["result"] for line in audit_lines])

    def test_firewall_status_reports_granted_failed_and_unavailable(self) -> None:
        data_dir = self.settings.data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        request_path = data_dir / webapp.FIREWALL_REQUEST_NAME
        result_path = data_dir / webapp.FIREWALL_RESULT_NAME
        port = int(self.settings.web_port)

        with mock.patch.object(webapp, "_firewall_allows", return_value={"checked": True, "allowed": True}):
            granted = self._get("/api/firewall/status", token="secret")
        self.assertEqual(granted["state"], "granted")

        with mock.patch.object(webapp, "_firewall_allows", return_value={"checked": True, "allowed": False}):
            result_path.write_text(
                json.dumps({"port": port, "ok": False, "message": "管理员授权被取消。", "at": "2026-10-25T00:00:00+0800"}),
                encoding="utf-8",
            )
            failed = self._get("/api/firewall/status", token="secret")
            self.assertEqual(failed["state"], "failed")
            self.assertIn("被取消", failed["message"])

            result_path.write_text(
                json.dumps({"port": port, "ok": True, "message": "已请求管理员权限", "at": "2026-10-25T00:00:00+0800"}),
                encoding="utf-8",
            )
            self.assertEqual(self._get("/api/firewall/status", token="secret")["state"], "granted")

            result_path.unlink()
            request_path.write_text(json.dumps({"port": port, "requested_at": "old"}), encoding="utf-8")
            stale = time.time() - 200
            os.utime(request_path, (stale, stale))
            unavailable = self._get("/api/firewall/status", token="secret")
        self.assertEqual(unavailable["state"], "unavailable")
        self.assertIn("托盘程序", unavailable["message"])

    def test_pair_info_reports_firewall_diagnosis_for_phone_access(self) -> None:
        diagnosis = {
            "checked": True,
            "allowed": False,
            "port": 8766,
            "command": (
                'netsh advfirewall firewall add rule name="群务台 8766" '
                "dir=in action=allow protocol=TCP localport=8766"
            ),
        }
        with (
            mock.patch("noticehub.webapp._firewall_allows", return_value=diagnosis),
            mock.patch("noticehub.webapp._lan_hosts", return_value=["192.168.8.20"]),
        ):
            info = self._get("/api/pair/info", token="secret")
        self.assertEqual(info["firewall"], diagnosis)

    def test_dingtalk_status_asks_to_pick_groups_when_whitelist_empty(self) -> None:
        # 空群白名单以前落到「钉钉已启用，监听未启动」，用户不知道要去勾群。
        with mock.patch.multiple(self.settings, dingtalk_enabled=True, dingtalk_group_whitelist=()):
            payload = self._get("/api/dingtalk/status", "secret")
        self.assertEqual(payload["state"], "已启用，但还没勾群")
        self.assertEqual(payload["groups"], 0)

    def test_dingtalk_status_names_connected_groups_when_reader_alive(self) -> None:
        self.server.dingtalk_reader_provider = lambda: mock.Mock(is_alive=True)
        self.addCleanup(delattr, self.server, "dingtalk_reader_provider")
        with mock.patch.multiple(
            self.settings, dingtalk_enabled=True, dingtalk_group_whitelist=("g1", "g2")
        ):
            payload = self._get("/api/dingtalk/status", "secret")
        self.assertEqual(payload["state"], "已连接实时监听 2 个群")

    def test_setup_status_reports_dingtalk_ready_instead_of_pending(self) -> None:
        # 平台状态以前硬编码 {"configured": None, "status": "pending"}，向导第二步恒显示
        # 「钉钉：暂不可用（开发中）」，顶部横幅也不提示没配。
        self.server.dingtalk_reader_provider = lambda: mock.Mock(is_alive=True)
        self.addCleanup(delattr, self.server, "dingtalk_reader_provider")
        with mock.patch.multiple(
            self.settings, dingtalk_enabled=True, dingtalk_group_whitelist=("g1",)
        ):
            platforms = self._get("/api/setup/status", "secret")["platforms"]
        self.assertIs(platforms["dingtalk"]["configured"], True)
        self.assertEqual(platforms["dingtalk"]["status"], "ready")

    def test_setup_status_marks_dingtalk_off_when_not_configured(self) -> None:
        with mock.patch.multiple(
            self.settings, dingtalk_enabled=False, dingtalk_group_whitelist=()
        ):
            platforms = self._get("/api/setup/status", "secret")["platforms"]
        self.assertIs(platforms["dingtalk"]["configured"], False)
        self.assertEqual(platforms["dingtalk"]["status"], "off")

    def test_setup_status_separates_engine_running_from_logged_in(self) -> None:
        """引擎在跑但还没扫码时：未配置，但要能看出缺的是「登录」这一半。"""
        napcat = {
            "ok": True, "online": False, "good": False,
            "napcat_installed": True, "napcat_running": True, "stage": "waiting_login",
        }
        with mock.patch.object(webapp._Handler, "_napcat_status_payload", return_value=napcat):
            qq = self._get("/api/setup/status", "secret")["platforms"]["qq"]
        self.assertFalse(qq["configured"], "没登录就不算可用")
        self.assertEqual(qq["engine_state"], "running")
        self.assertEqual(qq["login_state"], "waiting_scan")

    def test_setup_status_reports_installed_engine_not_running(self) -> None:
        napcat = {
            "ok": False, "online": False, "good": False,
            "napcat_installed": True, "napcat_running": False, "stage": "stopped",
        }
        with mock.patch.object(webapp._Handler, "_napcat_status_payload", return_value=napcat):
            qq = self._get("/api/setup/status", "secret")["platforms"]["qq"]
        self.assertFalse(qq["configured"])
        self.assertEqual(qq["engine_state"], "installed_not_running")
        self.assertEqual(qq["login_state"], "not_logged_in")

    def test_setup_status_gives_every_platform_a_human_headline_and_one_action(self) -> None:
        """体检要能「一个按钮修好」：每个平台都有一句人话 + 一个可执行动作。"""
        from noticehub import routes

        platforms = self._get("/api/setup/status", "secret")["platforms"]
        self.assertEqual(
            set(platforms), {"qq", "wechat", "dingtalk", "chaoxing", "push", "llm"}, "六个平台必须全覆盖"
        )
        machine_words = {"未配置", "incomplete", "offline", "needs_setup", "off", "error"}
        for name, entry in platforms.items():
            with self.subTest(platform=name):
                headline = entry.get("headline") or ""
                self.assertTrue(headline, "每个平台都要有 headline")
                self.assertLessEqual(len(headline), 40, "headline 要一句话说清，不超 40 字")
                self.assertNotIn(headline, machine_words, "headline 不许是机器词")
                self.assertIn("action", entry, "action 键必须存在（已配好时为 null）")
                action = entry["action"]
                if entry.get("configured"):
                    self.assertIsNone(action, "已配好的平台不该再给动作")
                    continue
                self.assertIsInstance(action, dict)
                self.assertIn(action.get("kind"), {"panel", "api", "manual"})
                self.assertTrue(action.get("label"))
                if action["kind"] == "api":
                    self.assertIn(action["target"], routes.POST_ROUTES, "api 动作必须是真实存在的 POST 路由")
                elif action["kind"] == "manual":
                    self.assertTrue(action.get("hint"), "manual 动作必须写清点哪里")
                else:
                    self.assertTrue(action.get("target"))

    def test_setup_status_qq_running_no_api_offers_one_click_connect(self) -> None:
        """引擎在跑但接口没通：唯一该做的事就是「帮我连上它」，且指向后端3 已跑绿的接口。"""
        napcat = {
            "ok": False, "online": False, "good": False, "stage": "running_no_api",
            "napcat_installed": True, "napcat_running": True,
        }
        with mock.patch.object(webapp._Handler, "_napcat_status_payload", return_value=napcat):
            qq = self._get("/api/setup/status", "secret")["platforms"]["qq"]
        self.assertFalse(qq["configured"], "接口没通就不算配好")
        self.assertEqual(qq["engine_state"], "running")
        self.assertEqual(
            qq["action"],
            {"kind": "api", "label": "帮我连上它", "target": "/api/napcat/connect", "hint": ""},
        )
        self.assertIn("连不上", qq["headline"], "要告诉用户卡在『连不上』，不是只报个状态词")

    def test_setup_status_headline_avoids_platform_names_and_jargon(self) -> None:
        """行左边已经有平台名：headline 不许再重复；也不许把 NapCat/OneBot/token 这类内部词漏给用户。"""
        platform_names = {
            "qq": ("QQ", "qq"),
            "wechat": ("微信",),
            "dingtalk": ("钉钉",),
            "chaoxing": ("学习通",),
            "push": ("推送",),
            "llm": ("模型", "AI"),
        }
        jargon = ("NapCat", "OneBot", "AppKey", "API", "token", "Token", "Cookie", "cookie", "webhook", "HTTP")
        platforms = self._get("/api/setup/status", "secret")["platforms"]
        for name, entry in platforms.items():
            with self.subTest(platform=name):
                headline = entry["headline"]
                for word in platform_names[name]:
                    self.assertNotIn(word, headline, "headline 里不许再出现平台名")
                for word in jargon:
                    self.assertNotIn(word, headline, "headline 不许出现内部术语")
                action = entry["action"]
                if action:
                    for word in jargon:
                        self.assertNotIn(word, action["label"], "动作按钮上也不许出现内部术语")

    def test_setup_status_never_runs_the_connect_discovery(self) -> None:
        """首屏接口只许读已有状态：它一旦跑 discover_api_url，就是 3 秒的阻塞探测。"""
        def boom(*_args, **_kwargs):
            raise AssertionError("/api/setup/status 不该跑 discover_api_url")

        with mock.patch.object(webapp.napcat_admin, "discover_api_url", side_effect=boom):
            platforms = self._get("/api/setup/status", "secret")["platforms"]
        self.assertEqual(len(platforms), 6)

    def test_health_receivers_are_unknown_until_first_scan(self) -> None:
        """idle = 一次都没跑过（未验证），不能当 ok 报绿。"""
        for name in ("dingtalk", "wechat", "chaoxing"):
            setattr(self.settings, name + "_status", {"scan_state": "idle"})
            self.addCleanup(setattr, self.settings, name + "_status", None)
        body = self._get("/health")
        self.assertEqual(body["receivers"], {"dingtalk": "unknown", "wechat": "unknown", "chaoxing": "unknown"})

    def test_delete_manual_quiet(self) -> None:
        """手工待办不是 AI 判出来的：放弃它要能真删，且不能污染纠错样本。"""
        created = self._post("/api/tasks/create", {"summary": "手动待办：交作业"})
        task_id = created["task_id"]
        self.assertEqual(self.store.get_task(task_id)["status"], "open")
        before = self._get("/api/insights", "secret")
        body = self._post("/api/tasks/%d" % task_id, {"action": "delete"})
        self.assertTrue(body["ok"], body)
        self.assertIsNone(self.store.get_task(task_id), "删的是行本身，不是标成 dismissed")
        after = self._get("/api/insights", "secret")
        self.assertEqual(after["stats"]["by_type"], before["stats"]["by_type"])
        self.assertEqual(int(after["stats"]["by_type"].get("not_notice") or 0), 0, "不能写学习记录")

    def test_delete_needs_manual(self) -> None:
        """普通 open（AI 判出来的）删不掉，仍走忽略：状态码与返回体形状不变。"""
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._post("/api/tasks/%d" % self.task_id, {"action": "delete"})
        self.assertEqual(caught.exception.code, 404)
        payload = json.loads(caught.exception.read().decode("utf-8"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["action"], "delete")
        self.assertEqual(self.store.get_task(self.task_id)["status"], "open")

    def _open(self, request: urllib.request.Request, attempts: int = 3):
        """发起一次本机请求，对「连接被本机重置」做有限重试。

        Windows 回环连接偶发被本机重置（CI 上出现过 ConnectionAbortedError /
        WinError 10053），与新开一条连接无关，所以这里重试；请求内容与断言都不变。

        超时给到 20 秒：/api/setup/status 与 /api/selfcheck 要探测本机平台，单次约
        4.9 秒，原来 5 秒只剩 100 毫秒余量，全量跑必红——这是余量不足，不是负载抖动。
        """
        error: Exception | None = None
        for _ in range(attempts):
            try:
                return urllib.request.urlopen(request, timeout=20)
            except (ConnectionAbortedError, ConnectionResetError) as caught:
                error = caught
                time.sleep(0.2)
        raise error  # type: ignore[misc]

    def _get(self, path: str, token: str = "") -> dict:
        headers = {"X-Token": token} if token else {}
        request = urllib.request.Request(self.base + path, headers=headers)
        with self._open(request) as response:
            return json.loads(response.read().decode("utf-8"))

    def _require_task(self, task_id: int) -> dict:
        """取一条待办；取不到直接断言失败，省掉每个用例各写一遍 assert is not None。"""
        task = self.store.get_task(task_id)
        assert task is not None
        return task

    def _post(self, path: str, payload: dict, token: str = "secret") -> dict:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"X-Token": token, "Content-Type": "application/json"},
            method="POST",
        )
        # 非幂等 POST 不能复用 _open 的「连接被重置就重试」：重试会把同一请求
        # 再执行一遍，副作用翻倍（cookie 被读两次就是这么来的）。这里直发，
        # 连接被重置就让它抛给用例，暴露真实环境问题。
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def _lab_request(self, method: str, path: str, *, token: str = "secret", payload: dict | None = None, raw: bytes | None = None) -> tuple[int, dict]:
        body = raw if raw is not None else (json.dumps(payload).encode("utf-8") if payload is not None else None)
        if "?" in path:
            path_base, _, path_query = path.partition("?")
            path = path_base + "?" + urllib.parse.quote(path_query, safe="=&%")
        request = urllib.request.Request(
            self.base + path,
            data=body,
            headers={"X-Token": token, "Content-Type": "application/json"},
            method=method,
        )
        try:
            with self._open(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def _inject_lab_module(self, module_name: str, *funcs: object) -> None:
        import importlib.machinery
        import types
        module = types.ModuleType(module_name)
        module.__spec__ = importlib.machinery.ModuleSpec(module_name, None)
        for func in funcs:
            setattr(module, func.__name__, func)
        patcher = mock.patch.dict(sys.modules, {module_name: module})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_lab_status_is_always_200_and_reports_the_three_features(self) -> None:
        status, body = self._lab_request("GET", "/api/lab/status")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        # 检索/抽日程常开，规则那项回显自己的开关（默认开）。
        self.assertEqual(body["result"], {"search": True, "events": True, "rules": True})
        self.assertEqual(body["enabled"], {"search": True, "events": True, "rules": True})

    def test_lab_routes_reject_with_403_when_rules_switch_off(self) -> None:
        self.settings.lab_rules = False
        status, body = self._lab_request("GET", "/api/lab/rules")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "自动整理规则已关闭")
        self.assertEqual(self._lab_request("POST", "/api/lab/rules", payload={"rules": []})[0], 403)
        self.assertEqual(self._lab_request("POST", "/api/lab/rules/test", payload={"text": "通知"})[0], 403)

    def test_deleted_lab_routes_are_gone(self) -> None:
        self.assertEqual(self._lab_request("GET", "/api/lab/replies")[0], 404)
        self.assertEqual(
            self._lab_request("POST", "/api/lab/replies/ack", payload={"msg_id": "m1"})[0], 404
        )

    def test_lab_rules_routes_save_read_and_test(self) -> None:
        # 真规则模块（rules.py 已定稿，不 mock）：走完整 HTTP 链路。
        self.settings.lab_rules = True
        self.assertEqual(
            self._lab_request("GET", "/api/lab/rules")[0], 200
        )
        saved = self._lab_request(
            "POST", "/api/lab/rules",
            payload={"rules": [{"id": "notice", "match": {"keyword": ["通知"]}, "action": "mark"}]},
        )
        self.assertEqual(saved[0], 200)
        self.assertTrue(saved[1]["ok"])
        self.assertEqual(saved[1]["count"], 1)
        # 读回：摊平后直接是 {"ok", "rules", "count", "path"}，没有多余的 result 包装。
        status, listed = self._lab_request("GET", "/api/lab/rules")
        self.assertEqual(status, 200)
        self.assertNotIn("result", listed)
        self.assertEqual([one["id"] for one in listed["rules"]], ["notice"])
        # 试跑：命中 / 不命中
        hit = self._lab_request("POST", "/api/lab/rules/test", payload={"message": {"content": "这是一条通知", "group_id": "g1"}})
        self.assertEqual(hit[0], 200)
        self.assertEqual([one["rule_id"] for one in hit[1]["hits"]], ["notice"])
        miss = self._lab_request("POST", "/api/lab/rules/test", payload={"message": {"content": "闲聊"}})
        self.assertEqual(miss[0], 200)
        self.assertEqual(miss[1]["hits"], [])

    def test_lab_rules_invalid_rule_returns_400_not_fake_success(self) -> None:
        # 门面把底层 {"ok": False} 包在 {"ok": True, "result": ...} 里，
        # 以前不摊平就会把「第 2 条规则非法」当成保存成功回 200 —— 页面照单写库、规则却没落盘。
        self.settings.lab_rules = True
        status, body = self._lab_request(
            "POST", "/api/lab/rules",
            payload={"rules": [
                {"id": "ok", "match": {"keyword": ["通知"]}, "action": "mark"},
                {"id": "bad", "match": {}},
            ]},
        )
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])
        self.assertIn("第 2 条规则非法", body["error"])
        # 整体拒绝：第一条也没写进去。
        self.assertEqual(self._lab_request("GET", "/api/lab/rules")[1]["rules"], [])

    def test_lab_rules_rejects_non_array_and_bad_json(self) -> None:
        self.settings.lab_rules = True
        status, body = self._lab_request("POST", "/api/lab/rules", payload={"rules": {"id": "x"}})
        self.assertEqual(status, 400)
        self.assertIn("必须是一个数组", body["error"])
        self.assertEqual(self._lab_request("POST", "/api/lab/rules", raw=b"not json")[0], 400)
        self.assertEqual(self._lab_request("POST", "/api/lab/rules", raw=b"[1]")[0], 400)

    def test_lab_rules_test_rejects_empty_message(self) -> None:
        self.settings.lab_rules = True
        status, body = self._lab_request("POST", "/api/lab/rules/test", payload={"message": {"content": "  "}})
        self.assertEqual(status, 400)
        self.assertIn("缺少 content", body["error"])

    def test_lab_search_forwards_query_and_filters(self) -> None:
        calls: list[tuple[object, str, dict]] = []

        def search(db_path: object, query: str, **filters: object) -> dict:
            calls.append((db_path, query, filters))
            return {"hits": [query]}

        self._inject_lab_module("noticehub.search", search)
        status, body = self._lab_request("GET", "/api/lab/search?q=评优&group_id=g1&limit=10")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["result"], {"hits": ["评优"]})
        # 消息库路径必须由调用方交进来：门面自己拼路径就会把「未就绪」掩盖成永远 503。
        self.assertEqual(
            calls,
            [(self.settings.data_dir / "digest.sqlite3", "评优", {"group_id": "g1", "limit": 10})],
        )
        self.assertEqual(self._lab_request("GET", "/api/lab/search")[0], 400)
        self.assertEqual(self._lab_request("GET", "/api/lab/search?q=")[0], 400)
        self.assertEqual(self._lab_request("GET", "/api/lab/search?q=" + "长" * 2001)[0], 400)
        self.assertEqual(self._lab_request("GET", "/api/lab/search?q=x&limit=0")[0], 400)
        self.assertEqual(self._lab_request("GET", "/api/lab/search?q=x&limit=abc")[0], 400)

    def test_lab_events_route_forwarding_and_503_when_module_missing(self) -> None:
        seen: list[str] = []

        def extract_events(text: str) -> dict:
            seen.append(text)
            return {"candidates": []}

        self._inject_lab_module("noticehub.event_extract", extract_events)
        status, body = self._lab_request("POST", "/api/lab/events", payload={"text": "明天下午三点半在A楼302开会"})
        self.assertEqual(status, 200)
        self.assertEqual(seen, ["明天下午三点半在A楼302开会"])
        for payload in ({}, {"text": ""}, {"text": "  \n\t"}):
            with self.subTest(payload=payload):
                status, body = self._lab_request("POST", "/api/lab/events", payload=payload)
                self.assertEqual(status, 400)
                self.assertEqual(body, {"ok": False, "error": "这条消息没有文字，请选择有文字的消息再点“抽日程”"})
        self.assertEqual(seen, ["明天下午三点半在A楼302开会"])
        self.assertEqual(self._lab_request("POST", "/api/lab/events", payload={"text": "长" * 2001})[0], 400)
        # 模块没落地 → 503（模块确实在时，用 patch 把「缺失」造出来）
        from noticehub import lab as lab_module

        with mock.patch.object(lab_module, "_module_ready", return_value=False):
            status, body = self._lab_request("GET", "/api/lab/search?q=x")
        self.assertEqual(status, 503)
        self.assertIn("该实验功能尚未就绪", body["error"])

        self.assertEqual(self._get("/api/meta", "secret")["channel"], "")
        self.settings.update_channel = "beta"
        self.assertEqual(self._get("/api/meta", "secret")["channel"], "beta")
        request = urllib.request.Request(self.base + "/", headers={"X-Token": "secret"})
        with self._open(request) as response:
            body = response.read().decode("utf-8")
        self.assertIn("BETA 测试版", body)
        self.assertIn('class="channel-badge channel-badge-beta"', body)
        current_version = webapp._current_version()
        self.assertIn(current_version, body)
        self.assertIn('class="channel-badge-version"> · ' + current_version + '</span>', body)
        # 页头 Logo 固定用原型配色：SVG 吃 --brand-ink 变量（服务端保留 <!--CHANNEL_LOGO--> 出口），
        # 但不再往 beta 通道里注入琥珀——否则底圆和内圈同色，Logo 糊成一团。
        self.assertIn('fill="var(--brand-ink,', body)
        self.assertIn("<!--CHANNEL_LOGO-->", PAGE_SOURCE)
        self.assertNotIn("#B5481E", body)
        self.assertNotIn("--brand-ink:#B5481E", PAGE_SOURCE)
        self.assertIn('fill="var(--brand-ink,', body)
        self.assertIn("<!--CHANNEL_LOGO-->", PAGE_SOURCE)
        self.assertRegex(PAGE_SOURCE, r"\.channel-badge\s*\{[^}]*\}")
        # 通道标不再占页头：副标题整块删掉，版本徽标挪进设置页「运行信息」。
        self.assertNotIn("群消息 · 待办与日历", PAGE_SOURCE)
        self.assertIn('<span id="channel-badge-slot" hidden><!--CHANNEL_BADGE--></span>', PAGE_SOURCE)
        self.assertIn("getElementById('channel-badge-slot')", PAGE_SOURCE)
        self.settings.update_channel = ""
        with self._open(request) as response:
            body = response.read().decode("utf-8")
        # 空配置回落到 APP_CHANNEL：比赛分支是 beta，正式版仓库是 stable，别把通道写死。
        expect_label = "BETA 测试版" if APP_CHANNEL == "beta" else "正式版"
        self.assertIn('class="channel-badge channel-badge-%s">%s' % (APP_CHANNEL, expect_label), body)
        self.assertIn('fill="var(--brand-ink,', body)  # 通道色出口还在，只是服务端不再注入琥珀值

    def test_channel_badge_omits_unknown_version(self) -> None:
        self.settings.update_channel = "beta"
        request = urllib.request.Request(self.base + "/", headers={"X-Token": "secret"})
        with mock.patch("noticehub.webapp._current_version", return_value="未知"):
            with self._open(request) as response:
                body = response.read().decode("utf-8")
        self.assertIn("BETA 测试版</span>", body)
        self.assertNotIn('class="channel-badge-version"', body)

    def test_chaoxing_manual_cookie_is_normalized_before_persisting(self) -> None:
        for raw in ("'Cookie: sid=test; uid=1'", '"document.cookie=sid=test; uid=1"', "Cookie: sid=test;\r\n uid=1 "):
            self.settings.chaoxing_cookie = ""
            result = self._post("/api/settings", {"chaoxing_cookie": raw})
            self.assertTrue(result["ok"])
            self.assertEqual(self.settings.chaoxing_cookie, "sid=test; uid=1")
            self.assertEqual((self.settings.data_dir / "chaoxing.cookie").read_text(encoding="utf-8"), "sid=test; uid=1")
        self.assertEqual(webapp._clean_chaoxing_cookie("\"\nCookie:  \""), "")

    def test_chaoxing_settings_are_written_to_env_file(self) -> None:
        """学习通的开关和群名单必须落进 .env：只改内存的话，重启后悄悄回到旧值。"""
        env_path = Path(self.tmp.name) / ".env"
        env_path.write_text("NOTICEHUB_CHAOXING_ENABLED=0\n", encoding="utf-8")
        self.settings.env_file = str(env_path)
        result = self._post("/api/settings", {"chaoxing_enabled": True, "chaoxing_group_whitelist": ["g1", "g2"]})
        self.assertTrue(result["ok"])
        self.assertTrue(self.settings.chaoxing_enabled)
        self.assertEqual(self.settings.chaoxing_group_whitelist, ("g1", "g2"))
        text = env_path.read_text(encoding="utf-8")
        self.assertIn("NOTICEHUB_CHAOXING_ENABLED=1", text)
        self.assertIn("NOTICEHUB_CHAOXING_GROUP_WHITELIST=g1,g2", text)

    def test_chaoxing_auto_cookie_success_enables_and_saves_cookie(self) -> None:
        read_result = {"ok": True, "cookie": "sid=test", "source": "edge", "attempts": []}
        with mock.patch(
            "noticehub.webapp.browser_cookie.read_chaoxing_cookie",
            return_value=read_result,
        ), mock.patch("noticehub.webapp.ChaoxingClient.my_courses", return_value=[]):
            result = self._post("/api/chaoxing/auto_cookie", {})
        self.assertTrue(result["ok"])
        self.assertTrue(self.settings.chaoxing_enabled)
        self.assertEqual((self.settings.data_dir / "chaoxing.cookie").read_text(encoding="utf-8"), "sid=test")

    def test_chaoxing_auto_cookie_failure_preserves_actionable_diagnostics(self) -> None:
        attempts = [
            {
                "ok": False,
                "cookie": "",
                "source": "edge",
                "category": "browser_locked",
                "error": "Edge Cookie 库被占用",
                "detail": "配置: Default",
                "next_step": "请完全退出 Edge（含托盘图标和后台进程），再回来点重试。",
            },
            {
                "ok": False,
                "cookie": "",
                "source": "chrome",
                "category": "no_chaoxing_cookie",
                "error": "Chrome 中没有学习通登录态",
                "detail": "配置: Default",
                "next_step": "请先登录 chaoxing.com。",
            },
        ]
        read_result = {**attempts[0], "attempts": attempts}
        with mock.patch(
            "noticehub.webapp.browser_cookie.read_chaoxing_cookie",
            return_value=read_result,
        ) as read_cookie:
            result = self._post("/api/chaoxing/auto_cookie", {})
        read_cookie.assert_called_once_with()
        self.assertFalse(result["ok"])
        self.assertEqual(result["category"], "browser_locked")
        self.assertEqual(result["next_step"], attempts[0]["next_step"])
        self.assertEqual(result["attempts"], attempts)

    def test_chaoxing_auto_cookie_requires_token(self) -> None:
        status, _, _ = self._raw_request(
            "/api/chaoxing/auto_cookie", method="POST", payload={}, token=""
        )
        self.assertEqual(status, 401)

    def test_napcat_restart_route_restarts_this_install_only(self) -> None:
        """用户报「二维码不刷新、点了也没反应」：卡住时唯一的出路是显式重启。

        重启必须走 napcat_admin.restart（先停本安装的实例再拉一个干净的），
        并和别的写接口一样要求令牌。
        """
        status, _, _ = self._raw_request("/api/napcat/restart", method="POST", payload={}, token="")
        self.assertEqual(status, 401)
        with mock.patch(
            "noticehub.napcat_admin.restart",
            return_value={
                "ok": True, "stopped": [7], "already_stopped": False, "pid": 9,
                "api_up": False, "waiting_for_login": True, "error": None,
            },
        ) as restarted:
            result = self._post("/api/napcat/restart", {})
        restarted.assert_called_once()
        self.assertTrue(result["ok"])
        self.assertTrue(result["waiting_for_login"])
        self.assertEqual(result["stopped"], [7])
        # 页面要给出这个入口，并且重启后作废缓存的二维码标记 —— 否则 refreshQrIfStale
        # 认为「图没变」会继续跳过重取，用户看到的还是旧二维码。
        self.assertIn('id="restart-napcat"', PAGE_SOURCE)
        self.assertIn("function runRestart()", PAGE_SOURCE)
        self.assertIn("/api/napcat/restart", PAGE_SOURCE)
        self.assertIn("lastQrStamp=null;", PAGE_SOURCE)

    def test_napcat_doctor_route_reports_the_first_broken_link(self) -> None:
        """自检必须能说出「卡在哪一格」。

        这正是过去查不出「扫码登录后无事发生」的原因：旧自检只看 /api/napcat/status，
        而那个接口本身依赖 OneBot API，API 起不来的根因（配置里没有 httpServers）
        它永远看不见。
        """
        status, _, _ = self._raw_request("/api/napcat/doctor", method="GET", token="")
        self.assertEqual(status, 401)
        report = {
            "ok": False,
            "steps": [{"name": "OneBot 配置里有 3001 端口的 httpServer", "ok": False, "detail": "network.httpServers 是空的", "hint": "点「重启引擎」"}],
            "broken": "OneBot 配置里有 3001 端口的 httpServer",
            "hint": "点「重启引擎」会自动补齐配置再启动；这之前无论怎么扫码登录都不会有反应",
        }
        with mock.patch("noticehub.napcat_admin.diagnose", return_value=report) as doctor:
            data = self._get("/api/napcat/doctor", token="secret")
        doctor.assert_called_once()
        self.assertFalse(data["ok"])
        self.assertIn("httpServer", data["broken"])
        self.assertIn("重启引擎", data["hint"])
        # 页面要在卡住时把断点显示出来，否则用户还是只看到「什么都没发生」
        self.assertIn("/api/napcat/doctor", PAGE_SOURCE)
        self.assertIn("当前卡在", PAGE_SOURCE)

    def test_manual_scan_routes_require_token_and_return_status_contract(self) -> None:
        for source in ("dingtalk", "wechat"):
            if source == "dingtalk":
                self.settings.dingtalk_enabled = True
                self.settings.dingtalk_group_whitelist = ("g1",)
            request = urllib.request.Request(self.base + f"/api/{source}/scan", data=b"{}", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as context:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(context.exception.code, 401)
            context.exception.close()
            setattr(self.settings, f"{source}_status", {
                "scan_state": "running",
                "scan_stage": "正在扫描测试数据",
                "scan_started_at": "2026-10-11 14:00:00",
                "scan_started_monotonic": time.monotonic() - 2,
            })
            with mock.patch.object(self.server.server, "start_scan", return_value=True):
                result = self._post(f"/api/{source}/scan", {})
            self.assertEqual(result["source"], source)
            self.assertTrue(result["started"])
            self.assertEqual(result["scan_state"], "running")
            self.assertEqual(result["scan_stage"], "正在扫描测试数据")
            self.assertGreaterEqual(result["scan_elapsed_seconds"], 1.9)

    def test_manual_scan_routes_report_unavailable_when_service_hook_unbound(self) -> None:
        for source in ("dingtalk", "wechat"):
            result = self._post(f"/api/{source}/scan", {})
            self.assertEqual(result, {"ok": False, "error": "扫描接口暂不可用"})

    def test_dingtalk_scan_without_selected_groups_returns_structured_error(self) -> None:
        self.settings.dingtalk_enabled = True
        self.settings.dingtalk_group_whitelist = ()
        self.settings.dingtalk_status = {"scan_state": "disabled"}
        with mock.patch.object(self.server.server, "start_scan", return_value=False) as start_scan:
            result = self._post("/api/dingtalk/scan", {})
        # 还没配好不算故障：ok=True 只当提示，前端才不会报「扫描启动失败」。
        self.assertTrue(result["ok"])
        self.assertFalse(result["started"])
        self.assertEqual(result["error"], "请先选择并保存至少一个钉钉群")
        self.assertEqual(result["category"], "no_groups_selected")
        self.assertEqual(result["scan_state"], "disabled")
        start_scan.assert_called_once_with("dingtalk")

    def test_scan_already_running_is_not_reported_as_failure(self) -> None:
        for source in ("dingtalk", "wechat"):
            setattr(self.settings, f"{source}_enabled", True)
            setattr(self.settings, f"{source}_group_whitelist", ("g1",))
            setattr(self.settings, f"{source}_status", {
                "scan_state": "running",
                "scan_stage": "正在读取测试库",
                "scan_started_monotonic": time.monotonic() - 3,
            })
            # 已有扫描持锁时 start_scan() 返回 False，这不是故障。
            with mock.patch.object(self.server.server, "start_scan", return_value=False):
                result = self._post(f"/api/{source}/scan", {})
            self.assertTrue(result["ok"])
            self.assertFalse(result["started"])
            self.assertTrue(result["already_running"])
            self.assertEqual(result["scan_state"], "running")
            self.assertEqual(result["scan_stage"], "正在读取测试库")
            self.assertGreaterEqual(result["scan_elapsed_seconds"], 2.9)
            self.assertIn("正在跑，这一轮跑完会自动刷新", result["message"])

    def test_wechat_scan_scanner_unavailable_stays_a_failure(self) -> None:
        self.settings.wechat_enabled = True
        self.settings.wechat_group_whitelist = ("g1",)
        self.settings.wechat_status = {"scan_state": "idle"}
        with mock.patch.object(self.server.server, "start_scan", return_value=False):
            result = self._post("/api/wechat/scan", {})
        self.assertFalse(result["ok"])
        self.assertFalse(result["started"])
        self.assertEqual(result["category"], "scanner_unavailable")
        self.assertIn("扫描器未运行", result["error"])

    def test_scan_status_endpoints_expose_progress_shape(self) -> None:
        for source in ("dingtalk", "wechat"):
            setattr(self.settings, f"{source}_status", {
                "scan_state": "running",
                "scan_stage": "正在读取测试库",
                "scan_started_at": "2026-10-11 14:00:00",
                "scan_started_monotonic": time.monotonic() - 1,
            })
            patcher = mock.patch("noticehub.webapp.wechat_locate_storage", return_value=None) if source == "wechat" else mock.patch("noticehub.webapp.DingTalkLocalReader")
            with patcher:
                result = self._get(f"/api/{source}/status", "secret")
            self.assertEqual(result["scan_state"], "running")
            self.assertEqual(result["scan_stage"], "正在读取测试库")
            self.assertEqual(result["scan_started_at"], "2026-10-11 14:00:00")
            self.assertGreaterEqual(result["scan_elapsed_seconds"], 0.9)

    def test_scan_status_pollers_are_cleared_before_settings_rebuild(self) -> None:
        # loadSettings() 每重建一次设置页就会重新绑一个 1.5s 轮询；句柄必须先记下来、下次重建前清掉，
        # 否则每进一次设置页就多一个永不停止的定时器。只赋值不留句柄（或留了句柄却清 null）都算泄漏。
        for name in ("dingtalk", "wechat"):
            self.assertIn("var %sScanTimer = null;" % name, PAGE_SOURCE, "%s 轮询句柄要先声明" % name)
            # 只认可「判空 → clearInterval(同一个句柄) → 置回 null」这三件事出现在同一段里；
            # 具体空格、大括号写法不算契约。
            self.assertRegex(
                PAGE_SOURCE,
                r"if \(%sScanTimer !== null\) \{\s*clearInterval\(%sScanTimer\);\s*%sScanTimer = null;\s*\}"
                % (name, name, name),
                "%s 轮询必须在设置页重建前清掉" % name,
            )
            self.assertRegex(
                PAGE_SOURCE,
                r"%sScanTimer = setInterval\(function ?\(\) ?\{\s*updateScanStatus\('%s'\);\s*\}, ?1500\);"
                % (name, name),
                "%s 轮询的句柄要真的存进变量，否则 clearInterval 清的是 null" % name,
            )
        # 同一处轮询绑定时历史上被整行复制过两次（一个句柄追不住两个定时器）
        for name in ("dingtalk", "wechat"):
            self.assertEqual(
                PAGE_SOURCE.count("setInterval(function(){updateScanStatus('%s');},1500);" % name),
                1,
                "%s 轮询只能绑定一处" % name,
            )

    def test_wechat_groups_include_suggestions_and_preserve_existing_fields(self) -> None:
        self.settings.llm_enabled = False
        self.settings.wechat_group_whitelist = ("w1",)
        self.settings.wechat_group_aliases = {"w1": "学院通知"}
        self.store.insert_message(msg_id="w-sample", source="wechat", group_id="w1", group_name="课程通知群", content="本周课程安排", received_at=NOW)
        with mock.patch("noticehub.webapp.wechat_locate_storage", return_value=Path(self.tmp.name)), mock.patch(
            "noticehub.webapp.wechat_group_names", return_value={"w1": "课程通知群", "w2": "社团活动群"}
        ):
            result = self._get("/api/wechat/groups", "secret")
        self.assertTrue(result["ok"])
        self.assertEqual(result["suggest_source"], "heuristic")
        first = next(row for row in result["groups"] if row["id"] == "w1")
        self.assertEqual({key: first[key] for key in ("id", "name", "alias", "selected")}, {
            "id": "w1", "name": "课程通知群", "alias": "学院通知", "selected": True,
        })
        self.assertEqual(first["category"], "course")
        self.assertTrue(first["suggested"])
        self.assertEqual(first["source"], "heuristic")

    def test_dingtalk_groups_include_suggestions_and_preserve_existing_fields(self) -> None:
        self.settings.llm_enabled = False
        self.settings.dingtalk_group_whitelist = ("d1",)
        self.store.insert_message(msg_id="d-sample", source="dingtalk", group_id="d1", group_name="教务课程群", content="考试安排", received_at=NOW)
        with mock.patch("noticehub.webapp.DingTalkLocalReader") as reader_type:
            reader_type.return_value.list_groups.return_value = [
                {"cid": "d1", "title": "教务课程群", "members": 28},
                {"cid": "d2", "title": "二手交易群", "members": 15},
            ]
            result = self._get("/api/dingtalk/groups", "secret")
        self.assertTrue(result["ok"])
        self.assertEqual(result["suggest_source"], "heuristic")
        first = result["groups"][0]
        self.assertEqual({key: first[key] for key in ("cid", "title", "members")}, {
            "cid": "d1", "title": "教务课程群", "members": 28,
        })
        self.assertEqual(first["category"], "course")
        self.assertTrue(first["suggested"])
        self.assertEqual(first["source"], "heuristic")

    def test_dingtalk_group_classification_uses_bounded_timeout(self) -> None:
        self.settings.llm_enabled = True
        self.settings.llm_backend = "auto"
        self.settings.llm_timeout = 600
        captured = {}

        def classify(settings, source, groups, *, sample=None):
            captured["timeout"] = settings.llm_timeout
            captured["original_timeout"] = self.settings.llm_timeout
            return webapp.group_suggest.heuristic_for_source(source, groups)

        with mock.patch("noticehub.webapp.DingTalkLocalReader") as reader_type, mock.patch(
            "noticehub.webapp._message_samples", return_value={}
        ), mock.patch("noticehub.webapp.group_suggest.suggest_for_source", side_effect=classify):
            reader_type.return_value.list_groups.return_value = [
                {"cid": "d1", "title": "课程通知群", "members": 8},
            ]
            result = self._get("/api/dingtalk/groups", "secret")
        self.assertTrue(result["ok"])
        self.assertEqual(captured["timeout"], 5)
        self.assertEqual(captured["original_timeout"], 600)

    def test_dingtalk_group_read_error_returns_structured_cause(self) -> None:
        failure = RuntimeError("钉钉本地数据库解密或读取失败")
        failure.__cause__ = ValueError("未能从本机钉钉配置中验证数据库密钥")
        with mock.patch("noticehub.webapp.DingTalkLocalReader") as reader_type:
            reader_type.return_value.list_groups.side_effect = failure
            result = self._get("/api/dingtalk/groups", "secret")
        self.assertFalse(result["ok"])
        self.assertEqual(result["category"], "read_failed")
        self.assertEqual(result["error"], "钉钉本地数据库解密或读取失败")
        self.assertEqual(result["detail"], "未能从本机钉钉配置中验证数据库密钥")

    def test_wechat_suggestions_receive_recent_stored_message_samples(self) -> None:
        self.settings.llm_enabled = True
        self.settings.llm_backend = "codebuddy"
        self.store.insert_message(msg_id="w-old", source="wechat", group_id="w1", group_name="课程群", content="旧消息", received_at=NOW - dt.timedelta(days=1))
        self.store.insert_message(msg_id="w-new", source="wechat", group_id="w1", group_name="课程群", content="最近课程安排", received_at=NOW)
        captured: dict[str, object] = {}
        def classify(_settings, groups, samples):
            captured["samples"] = samples
            return [{"group_id": groups[0]["group_id"], "category": "course", "reason": "课程样本", "suggested": True}]
        webapp.group_suggest._clear_caches()
        with mock.patch("noticehub.webapp.wechat_locate_storage", return_value=Path(self.tmp.name)), mock.patch(
            "noticehub.webapp.wechat_group_names", return_value={"w1": "课程群"}
        ), mock.patch("noticehub.group_suggest._llm", side_effect=classify):
            result = self._get("/api/wechat/groups", "secret")
        self.assertTrue(result["ok"])
        self.assertEqual(captured["samples"], {"w1": ["最近课程安排", "旧消息"]})
        self.assertEqual(result["groups"][0]["source"], "llm")

    def test_recommendation_failure_keeps_group_list_and_returns_human_fallback(self) -> None:
        self.settings.llm_enabled = False
        with mock.patch("noticehub.webapp.wechat_locate_storage", return_value=Path(self.tmp.name)), mock.patch(
            "noticehub.webapp.wechat_group_names", return_value={"w1": "课程通知群"}
        ), mock.patch("noticehub.webapp.group_suggest.suggest_for_source", side_effect=RuntimeError("synthetic")):
            result = self._get("/api/wechat/groups", "secret")
        self.assertTrue(result["ok"])
        self.assertIn("关键词", result["suggest_error"])
        self.assertEqual(result["groups"][0]["id"], "w1")
        self.assertEqual(result["groups"][0]["category"], "course")

    def test_wechat_groups_require_token(self) -> None:
        status, _, _ = self._raw_request("/api/wechat/groups", token="")
        self.assertEqual(status, 401)

    def _raw_request(self, path: str, method: str = "GET", payload: dict | None = None, token: str = "secret") -> tuple[int, object, bytes]:
        headers = {"X-Token": token} if token else {}
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            response = self._open(request)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    def _raw_request_with_cookie(self, path: str, cookie: str, method: str = "GET") -> tuple[int, object, bytes]:
        """只带 Cookie、不带 X-Token 的请求：验证会话 Cookie 真的能当凭据。"""
        request = urllib.request.Request(self.base + path, headers={"Cookie": cookie}, method=method)
        try:
            response = self._open(request)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    def test_session_exchange_sets_httponly_cookie_and_cookie_authorizes(self) -> None:
        """红队 S1：令牌换成 HttpOnly 会话 Cookie，URL/历史记录里不再留明文令牌。"""
        status, headers, raw = self._raw_request(
            "/api/session/exchange", method="POST", payload={"token": "secret"}, token=""
        )
        self.assertEqual(status, 200)
        cookie = str(headers.get("Set-Cookie", ""))
        self.assertIn("nh_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        # 响应正文里不能出现明文令牌，Cookie 值也不能等于令牌本身
        self.assertNotIn(b"secret", raw)
        value = cookie.split("nh_session=", 1)[1].split(";", 1)[0]
        self.assertNotEqual(value, "secret")
        # 只带 Cookie（没有 X-Token、没有 ?token=）也能通过鉴权
        status, _headers, body = self._raw_request_with_cookie("/api/tasks", f"nh_session={value}")
        self.assertEqual(status, 200)
        self.assertIn(b'"stats"', body)
        # 伪造的 Cookie 不算数
        status, _headers, _body = self._raw_request_with_cookie("/api/tasks", "nh_session=deadbeef")
        self.assertEqual(status, 401)

    def test_session_exchange_rejects_wrong_token(self) -> None:
        status, headers, _raw = self._raw_request(
            "/api/session/exchange", method="POST", payload={"token": "nope"}, token=""
        )
        self.assertEqual(status, 401)
        self.assertNotIn("nh_session=", str(headers.get("Set-Cookie", "")))

    def test_url_token_still_works_and_upgrades_to_session_cookie(self) -> None:
        """老链接（?token=）必须继续可用，且顺手换成 Cookie，下次就不用在 URL 里带令牌。"""
        status, headers, _body = self._raw_request("/api/tasks?token=secret", token="")
        self.assertEqual(status, 200)
        self.assertIn("nh_session=", str(headers.get("Set-Cookie", "")))

    def test_many_bad_tokens_get_throttled_then_recover(self) -> None:
        """红队 S12：一分钟内 401 超过 30 次就锁 60 秒，别让人在线爆破。"""
        webapp._reset_auth_limits()
        self.addCleanup(webapp._reset_auth_limits)
        for _ in range(webapp.AUTH_FAILURE_LIMIT + 1):
            status, _headers, _body = self._raw_request("/api/tasks", token="bad-token")
            self.assertEqual(status, 401)
        status, _headers, _body = self._raw_request("/api/tasks", token="secret")
        self.assertEqual(status, 429, "锁定期间连正确令牌也先拒，等 60 秒")
        # 锁定期过期后自动恢复：用注入时间点验证，不真的睡 60 秒
        source = "127.0.0.1"
        self.assertTrue(webapp._auth_blocked(source, now=time.monotonic()))
        self.assertFalse(webapp._auth_blocked(source, now=time.monotonic() + webapp.AUTH_FAILURE_BLOCK_SECONDS + 1))

    def test_ping_is_public_and_carries_no_user_data(self) -> None:
        """/api/ping 给公网探活用：不许要令牌（探活方拿不到），也不许漏用户数据。"""
        status, _headers, raw = self._raw_request("/api/ping", token="")
        self.assertEqual(status, 200)
        body = json.loads(raw.decode("utf-8"))
        self.assertEqual(body, {"ok": True, "app": "notice-hub"})

    def test_health_reports_503_when_database_probe_fails(self) -> None:
        """红队 S20：库读不了就不能再无条件 200，也不能把路径/令牌写进响应。"""
        from unittest import mock as _mock
        with _mock.patch.object(webapp.Store, "_connect", side_effect=sqlite3.OperationalError("boom")):
            status, _headers, raw = self._raw_request("/api/health")
        self.assertEqual(status, 503)
        body = json.loads(raw.decode("utf-8"))
        self.assertFalse(body["ok"])
        self.assertEqual(body["database"], "down")
        text = raw.decode("utf-8")
        self.assertNotIn("secret", text)
        self.assertNotIn(str(self.tmp.name), text)

    def _wait_for_announcement_state(self, data_dir: Path) -> dict:
        state_path = data_dir / "updates" / "state.json"
        for _ in range(200):
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                state = {}
            if isinstance(state, dict) and isinstance(state.get("announce_response"), dict):
                return state
            time.sleep(0.01)
        self.fail("announcement refresh did not persist a response")

    def test_announcements_require_token_without_refreshing(self) -> None:
        with mock.patch("noticehub.webapp.announce.active") as active:
            status, _, _ = self._raw_request("/api/announcements", token="")
        self.assertEqual(status, 401)
        active.assert_not_called()

    def test_announcements_load_fake_cache_in_background(self) -> None:
        data_dir = Path(self.tmp.name) / "announcement-cache"
        cache_dir = data_dir / "updates"
        cache_dir.mkdir(parents=True)
        # 渠道由 APP_CHANNEL 兜底，正式版之后默认不再是 beta；这条用例只验后台缓存加载，把渠道钉死。
        self.settings.update_channel = "beta"
        fake_items = [
            {"id": "a1", "title": "测试公告一", "body": "安全文本", "level": "info", "seen_required": False},
            {"id": "a2", "title": "测试公告二", "body": "<script>no</script>", "level": "critical", "seen_required": True},
        ]
        (cache_dir / "announcements.json").write_text(json.dumps({"items": fake_items}), encoding="utf-8")
        self.settings.data_dir = data_dir

        def read_local_cache(*, cache_dir: Path, channel: str | None = None) -> dict:
            payload = json.loads((cache_dir / "announcements.json").read_text(encoding="utf-8"))
            return {"ok": True, "items": payload["items"], "from_cache": True, "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(), "error": ""}

        with mock.patch("noticehub.webapp.announce.active", side_effect=read_local_cache) as active:
            first_status, _, first_body = self._raw_request("/api/announcements")
            state = self._wait_for_announcement_state(data_dir)
            status, _, body = self._raw_request("/api/announcements")
        active.assert_called_once_with(cache_dir=cache_dir, channel="beta")
        self.assertEqual(first_status, 200)
        self.assertEqual(json.loads(first_body)["items"], [])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["items"], fake_items)
        self.assertEqual(state["announce_response"]["items"], fake_items)

    def test_forced_announcement_refresh_uses_service_channel(self) -> None:
        data_dir = Path(self.tmp.name) / "announcement-channel"
        self.settings.data_dir = data_dir
        self.settings.update_channel = "stable"
        stable_item = {"id": "stable-only", "title": "正式版公告", "body": "stable", "level": "info"}

        def active_for_channel(*, cache_dir: Path, channel: str | None = None) -> dict:
            items = [stable_item] if channel == "stable" else []
            return {"ok": True, "items": items, "from_cache": False, "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(), "error": ""}

        with mock.patch("noticehub.webapp.announce.active", side_effect=active_for_channel) as active:
            status, _, body = self._raw_request("/api/announcements?refresh=1")

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["items"], [stable_item])
        active.assert_called_once_with(cache_dir=data_dir / "updates", channel="stable")

    def test_cached_announcement_from_cache_expires_after_error_retry_window(self) -> None:
        stale_dir = Path(self.tmp.name) / "announcement-stale-cache"
        self.settings.data_dir = stale_dir
        stale_checked_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=11)).isoformat()
        stale = {
            "ok": True,
            "items": [{"id": "stale", "title": "旧公告", "seen_required": False}],
            "from_cache": True,
            "checked_at": stale_checked_at,
            "error": "",
        }
        stale_state = stale_dir / "updates" / "state.json"
        stale_state.parent.mkdir(parents=True)
        stale_state.write_text(json.dumps({"announce_checked_at": stale_checked_at, "announce_response": stale}), encoding="utf-8")

        with mock.patch("noticehub.webapp.announce.active", return_value=stale) as active:
            response = self._get("/api/announcements", "secret")
            self.assertTrue(response["checking"])
            for _ in range(200):
                if active.call_count:
                    break
                time.sleep(0.01)
            self.assertEqual(active.call_count, 1)

        fresh_dir = Path(self.tmp.name) / "announcement-fresh-cache"
        self.settings.data_dir = fresh_dir
        fresh_checked_at = dt.datetime.now(dt.timezone.utc).isoformat()
        fresh = {
            "ok": True,
            "items": [{"id": "fresh", "title": "新公告", "seen_required": False}],
            "from_cache": False,
            "checked_at": fresh_checked_at,
            "error": "",
        }
        fresh_state = fresh_dir / "updates" / "state.json"
        fresh_state.parent.mkdir(parents=True)
        fresh_state.write_text(json.dumps({"announce_checked_at": fresh_checked_at, "announce_response": fresh}), encoding="utf-8")

        with mock.patch("noticehub.webapp.announce.active") as active:
            response = self._get("/api/announcements", "secret")
        active.assert_not_called()
        self.assertEqual(response["items"], fresh["items"])
        self.assertTrue(response["from_cache"])
        self.assertFalse(response["checking"])

    def test_announcement_cache_from_previous_version_is_invalidated(self) -> None:
        data_dir = Path(self.tmp.name) / "announcement-old-version-cache"
        self.settings.data_dir = data_dir
        checked_at = dt.datetime.now(dt.timezone.utc).isoformat()
        old_cache = {
            "ok": True,
            "items": [{"id": "old", "title": "旧公告"}],
            "from_cache": False,
            "checked_at": checked_at,
            "error": "",
            "current_version": "1.1.0-beta.2",
        }
        state_path = data_dir / "updates" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(json.dumps({"announce_checked_at": checked_at, "announce_response": old_cache}), encoding="utf-8")

        with mock.patch("noticehub.webapp._current_version", return_value=APP_VERSION), mock.patch(
            "noticehub.webapp.announce.active",
            return_value={"ok": True, "items": [{"id": "new", "title": "新公告"}], "from_cache": False, "checked_at": checked_at, "error": ""},
        ) as active:
            response = self._get("/api/announcements", "secret")
            self.assertNotIn({"id": "old", "title": "旧公告"}, response["items"])
            self.assertTrue(response["checking"])
            for _ in range(200):
                if active.call_count:
                    break
                time.sleep(0.01)

        active.assert_called_once()

    def test_announcement_cache_for_current_version_stays_fresh(self) -> None:
        data_dir = Path(self.tmp.name) / "announcement-current-version-cache"
        self.settings.data_dir = data_dir
        checked_at = dt.datetime.now(dt.timezone.utc).isoformat()
        cached_items = [{"id": "current", "title": "当前版本公告"}]
        current_cache = {
            "ok": True,
            "items": cached_items,
            "from_cache": False,
            "checked_at": checked_at,
            "error": "",
            "current_version": APP_VERSION,
        }
        state_path = data_dir / "updates" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(json.dumps({"announce_checked_at": checked_at, "announce_response": current_cache}), encoding="utf-8")

        with mock.patch("noticehub.webapp._current_version", return_value=APP_VERSION), mock.patch(
            "noticehub.webapp.announce.active"
        ) as active:
            response = self._get("/api/announcements", "secret")

        active.assert_not_called()
        self.assertEqual(response["items"], cached_items)
        self.assertTrue(response["from_cache"])
        self.assertFalse(response["checking"])

    def test_announcements_return_cached_result_within_refresh_window(self) -> None:
        """成功的公告缓存 15 分钟内直接复用（公告改动要尽快到用户），过期才重取。"""
        data_dir = Path(self.tmp.name) / "announcement-throttle"
        self.settings.data_dir = data_dir
        checked_at = dt.datetime.now(dt.timezone.utc).isoformat()
        cached = {"ok": True, "items": [{"id": "cached", "title": "缓存公告", "seen_required": False}], "from_cache": False, "checked_at": checked_at, "error": ""}
        state_path = data_dir / "updates" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(json.dumps({"announce_checked_at": checked_at, "announce_response": cached, "other_state": 7}), encoding="utf-8")

        with mock.patch("noticehub.webapp.announce.active") as active:
            result = self._get("/api/announcements", "secret")
            again = self._get("/api/announcements", "secret")
        active.assert_not_called()
        self.assertEqual(result["items"], cached["items"])
        self.assertTrue(result["from_cache"])
        self.assertEqual(again["items"], cached["items"])
        self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["other_state"], 7)
        self.assertEqual(webapp.ANNOUNCEMENT_REFRESH_SECONDS, 15 * 60)

        # 过期必须真的重取：缓存超过刷新窗口后仍拿旧结果，公告改动要等旧窗口才到用户。
        stale_dir = Path(self.tmp.name) / "announcement-throttle-stale"
        self.settings.data_dir = stale_dir
        stale_checked_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=16)).isoformat()
        stale = {"ok": True, "items": cached["items"], "from_cache": False, "checked_at": stale_checked_at, "error": ""}
        stale_state = stale_dir / "updates" / "state.json"
        stale_state.parent.mkdir(parents=True)
        stale_state.write_text(json.dumps({"announce_checked_at": stale_checked_at, "announce_response": stale}), encoding="utf-8")

        with mock.patch("noticehub.webapp.announce.active", return_value=stale) as active:
            stale_result = self._get("/api/announcements", "secret")
            self.assertTrue(stale_result["checking"])
            for _ in range(200):
                if active.call_count:
                    break
                time.sleep(0.01)
            self.assertEqual(active.call_count, 1)

    def test_announcement_refresh_failures_stay_http_200_and_are_not_shown_as_red_errors(self) -> None:
        for suffix, outcome in (("returned", {"ok": False, "items": [], "from_cache": False, "checked_at": "", "error": "网络不通"}), ("raised", RuntimeError("synthetic exception"))):
            with self.subTest(outcome=suffix):
                data_dir = Path(self.tmp.name) / ("announcement-error-" + suffix)
                self.settings.data_dir = data_dir
                active_effect = outcome if isinstance(outcome, BaseException) else lambda: outcome
                with mock.patch("noticehub.webapp.announce.active", side_effect=active_effect) as active:
                    first_status, _, _ = self._raw_request("/api/announcements")
                    state = self._wait_for_announcement_state(data_dir)
                    status, _, body = self._raw_request("/api/announcements")
                    active.assert_called_once()
                    # 失败缓存只压 10 分钟：到期必须重试，否则一次失败会顶满成功缓存的 15 分钟窗口，
                    # 公告源修好后用户仍要一直看「无法获取公告」。
                    state_file = data_dir / "updates" / "state.json"
                    stale = json.loads(state_file.read_text(encoding="utf-8"))
                    stale["announce_checked_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=11)).isoformat()
                    state_file.write_text(json.dumps(stale), encoding="utf-8")
                    _, _, retry_body = self._raw_request("/api/announcements")
                    self.assertTrue(json.loads(retry_body)["checking"])
                    for _ in range(200):
                        if active.call_count >= 2:
                            break
                        time.sleep(0.01)
                    self.assertEqual(active.call_count, 2)
                result = json.loads(body)
                self.assertEqual(first_status, 200)
                self.assertEqual(status, 200)
                self.assertFalse(result["ok"])
                self.assertTrue(result["error"])
                self.assertEqual(result["error"], state["announce_response"]["error"])
                page_status, _, page = self._raw_request("/")
                self.assertEqual(page_status, 200)
                # 公告脚本已拆到 web_static/js/announce.js，按合并后的源码断言。
                self.assertIn("公告暂时不可用。", PAGE_SOURCE)
                self.assertNotIn("网络不通", page.decode("utf-8"))

    # ---- 设置 →「版本更新」（task-151 / task-152）----

    def _update_state_file(self, data_dir: Path) -> Path:
        return data_dir / "updates" / "state.json"

    def _seed_update_state(self, data_dir: Path, *, age_seconds: float, tag: str = "v9.9.9", current_version: str = APP_VERSION) -> Path:
        state_path = self._update_state_file(data_dir)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({
            "announce_checked_at": "2026-10-11T00:00:00Z",
            "announce_response": {"ok": True, "items": [], "from_cache": False, "checked_at": "2026-10-11T00:00:00Z", "error": ""},
            "update_checked_epoch": time.time() - age_seconds,
            "update_result": {
                "ok": True, "checked_at": "2026-10-11 12:00:00", "channel": "beta",
                "current": {"version": current_version, "tag": "v" + current_version},
                "latest": {"tag": tag, "version": tag[1:], "name": "群务台 " + tag, "notes": "", "notes_html": "",
                           "published_at": "2026-10-11T01:02:03Z", "asset_name": "QQ-Notice-Hub.zip", "size": 1048576,
                           "sha256": "ab" * 32, "download_url": "https://example.invalid/x.zip", "html_url": "https://example.invalid/rel"},
                "has_update": True, "error": "",
            },
        }, ensure_ascii=False), encoding="utf-8")
        return state_path

    def _seed_update_state_from_old_version(self, data_dir: Path) -> Path:
        state_path = self._seed_update_state(data_dir, age_seconds=60, tag="v1.1.0-beta.4")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["update_result"]["current"]["version"] = "1.1.0-beta.2"
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        return state_path

    def test_update_requires_token(self) -> None:
        request = urllib.request.Request(self.base + "/api/update")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 401)

    def test_update_check_reports_release_and_renders_notes(self) -> None:
        data_dir = Path(self.tmp.name) / "update-shape"
        self.settings.data_dir = data_dir
        notes = "## 新增功能\n- 支持 **新东西**\n## 修复问题\n- 修掉 <script>alert(1)</script>"
        info = {
            "ok": True, "checked_at": "2026-10-11 12:00:00", "channel": "beta",
            "current": {"version": "1.0.0", "tag": "v1.0.0"},
            "latest": {"tag": "v9.9.9", "version": "9.9.9", "name": "群务台 v9.9.9", "notes": notes,
                       "published_at": "2026-10-11T01:02:03Z", "asset_name": "QQ-Notice-Hub.zip", "size": 1048576,
                       "sha256": "ab" * 32, "download_url": "https://example.invalid/x.zip", "html_url": "https://example.invalid/rel"},
            "has_update": True, "error": "",
        }
        with mock.patch("noticehub.webapp.updater") as fake:
            fake.check.return_value = info
            fake.current_version.return_value = "1.0.0"
            fake.resolve_channel.return_value = "beta"
            payload = self._get("/api/update?check=1", "secret")
        fake.check.assert_called_once()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["has_update"])
        self.assertFalse(payload["checking"])
        self.assertEqual(payload["current"]["version"], "1.0.0")
        self.assertEqual(payload["latest"]["tag"], "v9.9.9")
        self.assertEqual(payload["download"]["state"], "idle")
        self.assertFalse(payload["applying"])
        self.assertIsNone(payload["pending"])
        html = payload["latest"]["notes_html"]
        self.assertIn("新增功能", html)
        self.assertIn("修复问题", html)
        self.assertIn("<li>支持 <strong>新东西</strong></li>", html)
        self.assertNotIn("<script>", html)          # 正文里的 <script> 必须被转义
        self.assertIn("&lt;script&gt;", html)

    def test_normalize_update_preserves_source_and_download_fallbacks(self) -> None:
        fallback = "https://gitee.com/x/y/releases/download/v1/a.zip"
        info = {
            "ok": True,
            "source": "github",
            "current": {"version": APP_VERSION, "tag": "v" + APP_VERSION},
            "latest": {
                "tag": "v1.1.0-beta.4", "version": APP_VERSION, "name": "release",
                "notes": "release notes", "asset_name": "QQ-Notice-Hub.zip", "size": 321,
                "sha256": "ab" * 32, "download_url": "https://example.invalid/main.zip",
                "fallbacks": [fallback, 123, None],
            },
            "has_update": True,
        }
        normalized = webapp._normalize_update(info)
        self.assertEqual(normalized["source"], "github")
        self.assertEqual(normalized["latest"]["fallbacks"], [fallback])
        for field in ("tag", "sha256", "size", "notes"):
            self.assertEqual(normalized["latest"][field], info["latest"][field])

        info["latest"]["fallbacks"] = "not-a-list"
        self.assertEqual(webapp._normalize_update(info)["latest"]["fallbacks"], [])

    def test_update_returns_pending_update_record_from_real_updater(self) -> None:
        self.settings.data_dir = Path(self.tmp.name) / "update-pending"
        self._seed_update_state(self.settings.data_dir, age_seconds=60)
        archive = Path(self.tmp.name) / "pending.zip"
        archive.write_bytes(b"zip")
        self.assertTrue(updater.mark_pending(self.settings.data_dir, zip_path=archive, version="v9.9.9")["ok"])
        payload = self._get("/api/update", "secret")
        self.assertEqual(payload["pending"]["version"], "v9.9.9")
        self.assertEqual(payload["pending"]["at"], updater.pending_update(self.settings.data_dir)["at"])

    def test_update_uses_six_hour_cache_without_network(self) -> None:
        data_dir = Path(self.tmp.name) / "update-cache"
        self.settings.data_dir = data_dir
        state_path = self._seed_update_state(data_dir, age_seconds=60, current_version="1.0.0")
        with mock.patch("noticehub.webapp.updater") as fake:
            fake.current_version.return_value = "1.0.0"
            payload = self._get("/api/update", "secret")
        fake.check.assert_not_called()
        self.assertFalse(payload["checking"])
        self.assertEqual(payload["latest"]["tag"], "v9.9.9")
        # 公告与更新共用 state.json：查更新不能把公告的键写丢
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertIn("announce_response", state)
        self.assertIn("update_result", state)

    def test_update_cache_from_previous_version_is_invalidated(self) -> None:
        data_dir = Path(self.tmp.name) / "update-old-version-cache"
        self.settings.data_dir = data_dir
        state_path = self._seed_update_state_from_old_version(data_dir)

        refresh_called = threading.Event()
        with mock.patch("noticehub.webapp._current_version", return_value=APP_VERSION), mock.patch(
            "noticehub.webapp._refresh_update", side_effect=lambda _server: refresh_called.set()
        ) as refresh:
            payload = self._get("/api/update", "secret")
            self.assertTrue(refresh_called.wait(2))

        self.assertEqual(payload["current"]["version"], APP_VERSION)
        self.assertTrue(payload["checking"])
        refresh.assert_called_once()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertNotIn("update_result", state)
        self.assertNotIn("update_checked_epoch", state)

    def test_saving_update_channel_invalidates_cache_and_checks_selected_channel(self) -> None:
        old_env_value = os.environ.get("QQ_DIGEST_UPDATE_CHANNEL")
        self.addCleanup(lambda: os.environ.pop("QQ_DIGEST_UPDATE_CHANNEL", None) if old_env_value is None else os.environ.__setitem__("QQ_DIGEST_UPDATE_CHANNEL", old_env_value))
        env_path = Path(self.tmp.name) / "update-channel.env"
        env_path.write_text("QQ_DIGEST_UPDATE_CHANNEL=beta\n", encoding="utf-8")
        self.settings.env_file = env_path

        for channel in ("stable", "beta"):
            with self.subTest(channel=channel):
                data_dir = Path(self.tmp.name) / ("update-channel-" + channel)
                self.settings.data_dir = data_dir
                state_path = self._seed_update_state(data_dir, age_seconds=30, current_version="1.0.0")
                self.settings.update_channel = "beta" if channel == "stable" else "stable"
                info = {
                    "ok": True, "checked_at": "2026-10-15 12:00:00", "channel": channel,
                    "current": {"version": "1.0.0", "tag": "v1.0.0"}, "latest": {},
                    "has_update": False, "error": "",
                }
                with mock.patch("noticehub.webapp.updater") as fake:
                    fake.check.return_value = info
                    fake.current_version.return_value = "1.0.0"
                    fake.resolve_channel.side_effect = lambda value=None: value or "beta"
                    self.assertTrue(self._post("/api/settings", {"update_channel": channel})["ok"])
                    # 这一份是 POST 之后、后台还没写盘时的状态；读它同样会撞覆盖写。
                    # 预算只给 0.5 秒：等太久就可能等到后台把 update_result 写进去，误伤下面两条断言。
                    state, attempts, last_error = _read_json_when_ready(
                        state_path, time.monotonic() + 0.5)
                    if state is None:
                        self.fail(
                            "0.5 秒内没读到更新状态文件（%d 次尝试，最后一次：%s）"
                            % (attempts, last_error)
                        )
                    self.assertNotIn("update_result", state,
                                     "刚 POST 完读到的这份里已有 update_result（%d 次尝试，"
                                     "后台可能抢先写盘）；实际 state=%r" % (attempts, state))
                    self.assertNotIn("update_checked_epoch", state,
                                     "%d 次尝试后读到的 state=%r" % (attempts, state))
                    self.assertIn("announce_response", state,
                                  "%d 次尝试后读到的 state=%r" % (attempts, state))
                    payload = self._get("/api/update", "secret")
                    self.assertTrue(payload["checking"])
                    # 后台刷新是异步写盘：文件可能还没落、也可能正写到一半（读到空/半截 JSON）。
                    # 满载时 2 秒不够，给 6 秒总预算，把「没写完」当「还没好」继续等，别当失败。
                    deadline = time.monotonic() + 6
                    attempts = 0
                    last_error = ""
                    state = None
                    while True:
                        candidate, attempts, last_error = _read_json_when_ready(
                            state_path, time.monotonic() + 0.02)
                        if candidate is not None:
                            if (candidate.get("update_result") or {}).get("channel") == channel:
                                state = candidate
                                break
                            state = candidate
                        if time.monotonic() >= deadline:
                            self.fail(
                                "6 秒内没等到 update_result.channel=%r 写进状态文件；"
                                "%d 次尝试，最后一次读到的问题：%s；最后一次 state=%r"
                                % (channel, attempts, last_error, state)
                            )
                    self.assertEqual(state["update_result"]["channel"], channel)
                fake.check.assert_called_once_with(channel=channel, timeout=8.0)
                self.assertEqual(env_path.read_text(encoding="utf-8").strip(), "NOTICEHUB_UPDATE_CHANNEL=" + channel)
                self.assertEqual(self.settings.update_channel, channel)

    def test_invalid_update_channel_normalizes_to_default(self) -> None:
        old_env_value = os.environ.get("QQ_DIGEST_UPDATE_CHANNEL")
        self.addCleanup(lambda: os.environ.pop("QQ_DIGEST_UPDATE_CHANNEL", None) if old_env_value is None else os.environ.__setitem__("QQ_DIGEST_UPDATE_CHANNEL", old_env_value))
        env_path = Path(self.tmp.name) / "invalid-update-channel.env"
        env_path.write_text("", encoding="utf-8")
        self.settings.env_file = env_path
        result = self._post("/api/settings", {"update_channel": "not-a-channel"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["update_channel"], "")
        self.assertEqual(self.settings.update_channel, "")
        self.assertEqual(os.environ.get("NOTICEHUB_UPDATE_CHANNEL"), "")
        self.assertEqual(env_path.read_text(encoding="utf-8"), "NOTICEHUB_UPDATE_CHANNEL=\n")

    def test_stale_update_cache_triggers_background_check(self) -> None:
        data_dir = Path(self.tmp.name) / "update-stale"
        self.settings.data_dir = data_dir
        state_path = self._seed_update_state(data_dir, age_seconds=7 * 3600, current_version="1.0.0")
        info = {
            "ok": True, "checked_at": "2026-10-11 13:00:00", "channel": "beta",
            "current": {"version": "1.0.0", "tag": "v1.0.0"},
            "latest": {"tag": "v9.9.10", "version": "9.9.10", "name": "", "notes": "", "published_at": "",
                       "asset_name": "QQ-Notice-Hub.zip", "size": 1, "sha256": "", "download_url": "https://example.invalid/y.zip", "html_url": ""},
            "has_update": True, "error": "",
        }
        with mock.patch("noticehub.webapp.updater") as fake:
            fake.check.return_value = info
            fake.current_version.return_value = "1.0.0"
            payload = self._get("/api/update", "secret")
            self.assertTrue(payload["checking"])
            for _ in range(200):
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    if (state.get("update_result") or {}).get("latest", {}).get("tag") == "v9.9.10":
                        break
                except (json.JSONDecodeError, FileNotFoundError, PermissionError, OSError):
                    pass
                time.sleep(0.01)
        # Wait a bit more for any pending writes to complete
        time.sleep(0.05)
        # Final read with retries to handle transient file locks
        for attempt in range(50):
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                break
            except (json.JSONDecodeError, FileNotFoundError, PermissionError, OSError):
                if attempt == 49:
                    raise
                time.sleep(0.01)
        self.assertEqual(state["update_result"]["latest"]["tag"], "v9.9.10")

    def test_concurrent_state_writes_preserve_all_fields(self) -> None:
        """Concurrent announcement refresh and update persist preserve both fields without PermissionError."""
        data_dir = Path(self.tmp.name) / "concurrent-state"
        self.settings.data_dir = data_dir
        state_path = data_dir / "updates" / "state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({}), encoding="utf-8")

        announce_response = {"ok": True, "items": [], "from_cache": False, "checked_at": "2026-10-22T20:00:00Z", "error": "", "current_version": "1.0.0"}
        update_info = {
            "ok": True, "checked_at": "2026-10-22 20:00:00", "channel": "beta",
            "current": {"version": "1.0.0", "tag": "v1.0.0"},
            "latest": {"tag": "v9.9.11", "version": "9.9.11", "name": "", "notes": "", "published_at": "",
                       "asset_name": "QQ-Notice-Hub.zip", "size": 1, "sha256": "a" * 64,
                       "download_url": "https://example.invalid/z.zip", "html_url": "", "fallbacks": []},
            "has_update": True, "error": "", "source": "github", "sources_tried": [],
        }

        errors = []
        # mock.patch 改的是模块全局属性，不是线程安全的：多个线程各自进出 patch 时，
        # 恢复顺序交错会把 webapp.updater 永久留成 MagicMock，污染后续用例。
        # _persist_update 只走 _normalize_update，不碰 updater，这里根本不需要 patch。
        def write_announces(n):
            for _ in range(n):
                try:
                    with mock.patch("noticehub.webapp.announce.active", return_value=announce_response):
                        webapp._refresh_announcements(self.server.server)
                except Exception as e:
                    errors.append(("announce", str(e)))

        def write_updates(n):
            for _ in range(n):
                try:
                    webapp._persist_update(self.server.server, update_info)
                except Exception as e:
                    errors.append(("update", str(e)))

        threads = [
            threading.Thread(target=write_announces, args=(10,)),
            threading.Thread(target=write_updates, args=(10,)),
            threading.Thread(target=write_announces, args=(10,)),
            threading.Thread(target=write_updates, args=(10,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"No PermissionError or other exceptions expected: {errors}")
        final_state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(final_state["update_result"]["latest"]["tag"], "v9.9.11")
        self.assertEqual(final_state["announce_response"]["checked_at"], "2026-10-22T20:00:00Z")

    def test_onedir_frozen_update_root_is_executable_directory(self) -> None:
        install_root = Path(self.tmp.name) / "install"
        with mock.patch("noticehub.webapp.RUNTIME_ROOT", install_root), mock.patch.object(
            webapp.sys, "frozen", True, create=True
        ):
            self.assertEqual(webapp._install_root(), install_root)

    def test_update_current_version_falls_back_to_unknown(self) -> None:
        self.settings.data_dir = Path(self.tmp.name) / "update-old"
        with mock.patch("noticehub.webapp.updater") as fake:
            fake.current_version.side_effect = RuntimeError("旧版安装没有版本常量")
            fake.check.return_value = {"ok": False, "checked_at": "", "channel": "", "current": {"version": "", "tag": ""}, "latest": {}, "has_update": False, "error": "GitHub 上没有可用的版本"}
            payload = self._get("/api/update?check=1", "secret")
        self.assertEqual(payload["current"]["version"], "未知")
        self.assertFalse(payload["ok"])
        self.assertIn("GitHub", payload["error"])

    def _check_update_versions(self, key: str, current: str, latest: str) -> dict:
        self.settings.data_dir = Path(self.tmp.name) / ("update-versions-" + key)
        info = {
            "ok": True, "checked_at": "2026-10-11 12:00:00", "channel": "beta",
            "current": {"version": current, "tag": "v" + current},
            "latest": {"tag": "v" + latest, "version": latest, "name": "", "notes": "", "published_at": "",
                       "asset_name": "QQ-Notice-Hub.zip", "size": 1, "sha256": "",
                       "download_url": "https://example.invalid/update.zip", "html_url": ""},
            "has_update": True, "error": "",
        }
        with mock.patch("noticehub.webapp.updater") as fake:
            fake.check.return_value = info
            payload = self._get("/api/update?check=1", "secret")
        fake.check.assert_called_once()
        return payload

    def test_update_semver_release_is_newer(self) -> None:
        payload = self._check_update_versions("semver-newer", "1.0.0", "1.1.0")
        self.assertTrue(payload["has_update"])
        self.assertEqual(payload["error"], "")

    def test_update_mixed_version_rules_require_manual_check(self) -> None:
        payload = self._check_update_versions("mixed", "1.1.0-beta.1", "2026.10.27")
        self.assertFalse(payload["has_update"])
        self.assertIn("1.1.0-beta.1", payload["notice"])
        self.assertIn("v2026.10.27", payload["notice"])
        self.assertIn("不同", payload["notice"])
        self.assertEqual(payload["error"], "")

    def test_update_date_release_is_newer(self) -> None:
        payload = self._check_update_versions("date-newer", "2026.10.20", "2026.10.27")
        self.assertTrue(payload["has_update"])
        self.assertEqual(payload["error"], "")

    def test_update_older_date_release_is_suppressed(self) -> None:
        payload = self._check_update_versions("date-older", "2026.10.27", "2026.10.20")
        self.assertFalse(payload["has_update"])
        self.assertIn("还没发布", payload["notice"])

    def test_update_suppresses_a_downgrade_prompt(self) -> None:
        """本机比线上 release 新（本地版还没发布）时不能提示「更新」，那是往回退。"""
        self.settings.data_dir = Path(self.tmp.name) / "update-downgrade"
        info = {
            "ok": True, "checked_at": "2026-10-11 12:00:00", "channel": "beta",
            "current": {"version": "1.1.0-beta.1", "tag": "v1.1.0-beta.1"},
            "latest": {"tag": "v1.0.0", "version": "1.0.0", "name": "", "notes": "", "published_at": "",
                       "asset_name": "QQ-Notice-Hub.zip", "size": 1, "sha256": "", "download_url": "https://example.invalid/z.zip", "html_url": ""},
            "has_update": True, "error": "",
        }
        with mock.patch("noticehub.webapp.updater") as fake:
            fake.check.return_value = info
            payload = self._get("/api/update?check=1", "secret")
        self.assertFalse(payload["has_update"])
        self.assertIn("还没发布", payload["notice"])
        self.assertEqual(payload["latest"]["tag"], "v1.0.0")

    def test_update_download_refuses_without_a_known_release(self) -> None:
        self.settings.data_dir = Path(self.tmp.name) / "update-nodl"
        with mock.patch("noticehub.webapp.updater") as fake:
            result = self._post("/api/update/download", {})
        self.assertFalse(result["ok"])
        self.assertFalse(result["started"])
        self.assertIn("检查更新", result["error"])
        fake.download.assert_not_called()

    def test_update_download_refuses_cache_from_previous_version(self) -> None:
        self.settings.data_dir = Path(self.tmp.name) / "update-old-version-download"
        self._seed_update_state_from_old_version(self.settings.data_dir)

        with mock.patch("noticehub.webapp._current_version", return_value=APP_VERSION), mock.patch(
            "noticehub.webapp.updater.download"
        ) as download:
            result = self._post("/api/update/download", {})

        self.assertFalse(result["ok"])
        self.assertFalse(result["started"])
        self.assertIn("先点「检查更新」", result["error"])
        download.assert_not_called()

    def test_update_download_receives_normalized_gitee_fallback(self) -> None:
        data_dir = Path(self.tmp.name) / "update-download-fallback"
        self.settings.data_dir = data_dir
        fallback = "https://gitee.com/ExpertKT/notice-hub-mirror/releases/download/v1.1.0-beta.4/QQ-Notice-Hub.zip"
        info = {
            "ok": True,
            "source": "github",
            "current": {"version": APP_VERSION, "tag": "v" + APP_VERSION},
            "latest": {
                "tag": "v1.1.0-beta.4", "version": APP_VERSION, "name": "release",
                "notes": "", "published_at": "", "asset_name": "QQ-Notice-Hub.zip",
                "size": 12, "sha256": "ab" * 32,
                "download_url": "https://example.invalid/unreachable.zip",
                "html_url": "", "fallbacks": [fallback],
            },
            "has_update": True, "error": "",
        }
        webapp._persist_update(self.server.server, info)
        called = threading.Event()
        captured: dict[str, object] = {}

        def inspect_download(latest, _dest, *, timeout=0, on_progress=None):
            captured["latest"] = latest
            called.set()
            return {"ok": False, "error": "test stop"}

        with mock.patch.object(webapp.updater, "download", side_effect=inspect_download):
            started = self._post("/api/update/download", {})
            self.assertTrue(started["ok"])
            self.assertTrue(started["started"])
            self.assertTrue(called.wait(2))
        self.assertEqual(captured["latest"]["fallbacks"], [fallback])

    def test_update_download_progress_then_done(self) -> None:
        data_dir = Path(self.tmp.name) / "update-dl"
        self.settings.data_dir = data_dir
        self._seed_update_state(data_dir, age_seconds=10, current_version="1.0.0")
        started = threading.Event()
        release = threading.Event()

        def slow_download(info, dest, *, timeout=0, on_progress=None):
            on_progress(5, 10)
            started.set()
            release.wait(5)
            archive = Path(dest) / "QQ-Notice-Hub-v9.9.9.zip"
            archive.write_bytes(b"zip")
            return {"ok": True, "path": str(archive), "bytes": 10, "sha256": "", "error": ""}

        with mock.patch("noticehub.webapp.updater") as fake:
            fake.current_version.return_value = "1.0.0"
            fake.download.side_effect = slow_download
            first = self._post("/api/update/download", {})
            self.assertTrue(first["ok"])
            self.assertTrue(first["started"])
            self.assertTrue(started.wait(5))
            again = self._post("/api/update/download", {})
            self.assertTrue(again["ok"])
            self.assertFalse(again["started"])  # 已经在下了，别再起一条
            during = self._get("/api/update", "secret")
            self.assertEqual(during["download"]["state"], "downloading")
            self.assertEqual(during["download"]["done"], 5)
            self.assertEqual(during["download"]["total"], 10)
            release.set()
            for _ in range(300):
                after = self._get("/api/update", "secret")
                if after["download"]["state"] != "downloading":
                    break
                time.sleep(0.02)
        self.assertEqual(after["download"]["state"], "done")
        self.assertTrue(after["download"]["path"].endswith(".zip"))

    def test_update_download_records_pending_update_for_the_tray(self) -> None:
        """下载成功后必须真的写出待安装记录，托盘才有「重启并更新」这个入口。"""
        data_dir = Path(self.tmp.name) / "update-pending-dl"
        self.settings.data_dir = data_dir
        self._seed_update_state(data_dir, age_seconds=10)
        archive = Path(self.tmp.name) / "QQ-Notice-Hub-v9.9.9.zip"

        def fast_download(info, dest, *, timeout=0, on_progress=None):
            on_progress(10, 10)
            archive.write_bytes(b"zip")
            return {"ok": True, "path": str(archive), "bytes": 10, "sha256": "", "error": ""}

        # 只替换下载动作，mark_pending / pending_update 都走真实现（模块级 mock 会让这里假绿）。
        with mock.patch("noticehub.webapp.updater.download", side_effect=fast_download):
            started = self._post("/api/update/download", {})
            self.assertTrue(started["ok"])
            self.assertTrue(started["started"])
            deadline = time.time() + 5
            while time.time() < deadline:
                payload = self._get("/api/update", "secret")
                if payload["download"]["state"] != "downloading":
                    break
                time.sleep(0.02)
        self.assertEqual(payload["download"]["state"], "done")
        self.assertIsNotNone(payload["pending"])
        self.assertEqual(payload["pending"]["version"], "9.9.9")
        self.assertEqual(payload["pending"]["at"], updater.pending_update(data_dir)["at"])

    def test_update_apply_refuses_before_download(self) -> None:
        self.settings.data_dir = Path(self.tmp.name) / "update-apply-early"
        with mock.patch("noticehub.webapp.updater") as fake:
            result = self._post("/api/update/apply", {})
        self.assertFalse(result["ok"])
        self.assertIn("下载", result["error"])
        fake.apply_async.assert_not_called()

    def test_update_apply_starts_script_and_schedules_self_exit(self) -> None:
        data_dir = Path(self.tmp.name) / "update-apply"
        self.settings.data_dir = data_dir
        archive = Path(self.tmp.name) / "QQ-Notice-Hub-v9.9.9.zip"
        archive.write_bytes(b"zip")
        assert self.server.server is not None
        self.server.server.update_download = {"state": "done", "done": 3, "total": 3, "path": str(archive), "error": "", "tag": "v9.9.9"}
        timers: list[tuple[float, object]] = []

        class CapturedTimer:
            def __init__(self, interval, function):
                timers.append((interval, function))

            def start(self):
                pass

        with mock.patch("noticehub.webapp.updater") as fake, mock.patch("noticehub.webapp.threading.Timer", CapturedTimer):
            fake.apply_async.return_value = {"ok": True, "script": "C:\\Temp\\update.ps1", "error": ""}
            result = self._post("/api/update/apply", {})
        self.assertTrue(result["ok"])
        self.assertTrue(result["restarting"])
        args, kwargs = fake.apply_async.call_args
        self.assertEqual(Path(args[1]), archive)
        self.assertEqual(kwargs.get("data_dir"), data_dir)
        self.assertEqual(len(timers), 1)
        self.assertEqual(timers[0][0], 2.0)

    def test_update_rollback_returns_the_patch_it_restored(self) -> None:
        data_dir = Path(self.tmp.name) / "update-rollback"
        self.settings.data_dir = data_dir
        self.settings.log_dir = data_dir / "logs"  # 审计别写进仓库 logs/
        timers: list[tuple[float, object]] = []

        class CapturedTimer:
            def __init__(self, interval, function):
                timers.append((interval, function))

            def start(self):
                pass

        with mock.patch("noticehub.webapp.updater") as fake, mock.patch(
            "noticehub.webapp.threading.Timer", CapturedTimer
        ), mock.patch.dict(os.environ, {"NH_TEST_MODE": "", "QQ_DIGEST_HOST_ACTIONS": ""}):
            fake.rollback_async.return_value = {
                "ok": True,
                "script": "C:\\Temp\\rollback.ps1",
                "from_patch": 2,
                "to_patch": 1,
            }
            status, body = self._lab_request("POST", "/api/update/rollback", payload={})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertTrue(body["restarting"])
        self.assertEqual(body["patch"], 1, "回给页面的应该是退到哪个补丁号")
        self.assertEqual(fake.rollback_async.call_args[0][0], data_dir)
        # 退回去的那一版不许再被自动装回来
        fake.skip_patch.assert_called_once_with(data_dir, 2)
        self.assertEqual([item[0] for item in timers], [2.0])

    def test_update_rollback_without_backup_is_400(self) -> None:
        with mock.patch("noticehub.webapp.updater") as fake, mock.patch.dict(
            os.environ, {"NH_TEST_MODE": "", "QQ_DIGEST_HOST_ACTIONS": ""}
        ):
            fake.rollback_async.return_value = {"ok": False, "error": "没有可回退的备份"}
            status, body = self._lab_request("POST", "/api/update/rollback", payload={})
        self.assertEqual(status, 400, body)
        self.assertIn("没有可回退的备份", body["error"])

    def test_update_rollback_is_409_while_an_install_is_running(self) -> None:
        assert self.server.server is not None
        self.server.server.update_applying = True
        try:
            with mock.patch("noticehub.webapp.updater") as fake, mock.patch.dict(
                os.environ, {"NH_TEST_MODE": "", "QQ_DIGEST_HOST_ACTIONS": ""}
            ):
                status, body = self._lab_request("POST", "/api/update/rollback", payload={})
        finally:
            self.server.server.update_applying = False
        self.assertEqual(status, 409, body)
        fake.rollback_async.assert_not_called()

    def test_update_rollback_is_blocked_in_test_mode(self) -> None:
        with mock.patch.dict(os.environ, {"NH_TEST_MODE": "1"}):
            with mock.patch("noticehub.webapp.updater") as fake:
                status, _body = self._lab_request("POST", "/api/update/rollback", payload={})
        self.assertEqual(status, 403)
        fake.rollback_async.assert_not_called()

    def test_patch_check_downloads_only_when_a_new_patch_is_listed(self) -> None:
        self.settings.data_dir = Path(self.tmp.name) / "patch-check"
        self.settings.patch_auto = True
        quiet = {
            "ok": True,
            "has_update": False,
            "patch_current": 0,
            "patch_latest": 0,
            "patch_update": False,
            "latest": {},
            "checked_at": "2026-11-07T14:00:00+08:00",
            "error": "",
        }
        with mock.patch.dict(
            os.environ, {"NH_TEST_MODE": "", "QQ_DIGEST_HOST_ACTIONS": ""}
        ), mock.patch("noticehub.webapp.updater") as fake, mock.patch(
            "noticehub.webapp._update_channel_name", return_value="beta"
        ):
            fake.pending_update.return_value = None
            fake.patch_skip.return_value = 0
            fake.check.return_value = quiet
            with mock.patch("noticehub.webapp._start_update_download") as start:
                self.server._patch_check_once()
            start.assert_not_called()

            fake.check.return_value = dict(quiet, patch_latest=1, patch_update=True)
            with mock.patch("noticehub.webapp._start_update_download") as start:
                self.server._patch_check_once()
            start.assert_called_once()
            # 补丁号必须活过缓存裁剪，否则下一次读缓存又变回「没有补丁」
            assert self.server.server is not None
            cached = webapp._read_updates_state(
                webapp._updates_state_path(self.server.server)
            ).get("update_result") or {}
            self.assertEqual(cached.get("patch_latest"), 1)
            self.assertIs(cached.get("patch_update"), True)

    def test_patch_check_stays_quiet_when_switched_off_or_already_pending(self) -> None:
        self.settings.patch_auto = False
        with mock.patch("noticehub.webapp.updater") as fake, mock.patch(
            "noticehub.webapp._start_update_download"
        ) as start:
            self.server._patch_check_once()
        fake.check.assert_not_called()
        start.assert_not_called()

        self.settings.patch_auto = True
        with mock.patch.dict(
            os.environ, {"NH_TEST_MODE": "", "QQ_DIGEST_HOST_ACTIONS": ""}
        ), mock.patch("noticehub.webapp.updater") as fake, mock.patch(
            "noticehub.webapp._update_channel_name", return_value="beta"
        ), mock.patch("noticehub.webapp._start_update_download") as start:
            fake.pending_update.return_value = {"version": "1.1.0-beta.18", "patch": 1}
            self.server._patch_check_once()
        start.assert_not_called()

    def test_patch_check_respects_the_skip_record(self) -> None:
        self.settings.patch_auto = True
        self.settings.data_dir = Path(self.tmp.name) / "patch-skip"
        info = {
            "ok": True,
            "has_update": False,
            "patch_current": 1,
            "patch_latest": 3,
            "patch_update": True,
            "latest": {},
            "checked_at": "2026-11-07T14:00:00+08:00",
            "error": "",
        }
        with mock.patch.dict(
            os.environ, {"NH_TEST_MODE": "", "QQ_DIGEST_HOST_ACTIONS": ""}
        ), mock.patch("noticehub.webapp.updater") as fake, mock.patch(
            "noticehub.webapp._update_channel_name", return_value="beta"
        ), mock.patch("noticehub.webapp._start_update_download") as start:
            fake.pending_update.return_value = None
            fake.patch_skip.return_value = 3  # 用户退回去的正是这一版
            fake.check.return_value = info
            self.server._patch_check_once()
        start.assert_not_called()

    @staticmethod
    def _new_version_result(**extra) -> dict:
        """一条「tag 变了」的检查结果：新版本、没有同版本补丁。"""
        result = {
            "ok": True,
            "has_update": True,
            "patch_current": 0,
            "patch_latest": 0,
            "patch_update": False,
            "latest": {
                "tag": "v1.1.0-beta.19",
                "version": "1.1.0-beta.19",
                "download_url": "https://example.invalid/notice-hub-1.1.0-beta.19.zip",
                "size": 10,
            },
            "checked_at": "2026-11-09T10:00:00+08:00",
            "error": "",
        }
        result.update(extra)
        return result

    def test_patch_check_downloads_a_new_version_and_only_triggers_once(self) -> None:
        # 新版本和同版本补丁走同一条路：后台自己下好，等用户点「重启并更新」。
        self.settings.data_dir = Path(self.tmp.name) / "version-check"
        self.settings.patch_auto = True
        with mock.patch.dict(
            os.environ, {"NH_TEST_MODE": "", "QQ_DIGEST_HOST_ACTIONS": ""}
        ), mock.patch("noticehub.webapp.updater") as fake, mock.patch(
            "noticehub.webapp._update_channel_name", return_value="beta"
        ):
            fake.pending_update.return_value = None
            fake.patch_skip.return_value = 0
            fake.check.return_value = self._new_version_result()
            with mock.patch("noticehub.webapp._start_update_download") as start:
                # 真下载会把状态置成 downloading（下完写 pending）；这里照实模拟，
                # 于是第二轮检查必须被幂等闸挡住——不能把 51 MB 再下一遍。
                def begin(server):
                    server.update_download = {
                        "state": "downloading",
                        "done": 0,
                        "total": 10,
                        "path": "",
                        "error": "",
                        "tag": "v1.1.0-beta.19",
                    }
                    return {"ok": True, "started": True}

                start.side_effect = begin
                self.server._patch_check_once()
                self.server._patch_check_once()
            start.assert_called_once()
        assert self.server.server is not None
        self.assertEqual(self.server.server.update_download["state"], "downloading")

    def test_patch_check_does_not_download_a_new_version_when_patch_auto_is_off(self) -> None:
        self.settings.patch_auto = False
        with mock.patch("noticehub.webapp.updater") as fake, mock.patch(
            "noticehub.webapp._start_update_download"
        ) as start:
            self.server._patch_check_once()
        fake.check.assert_not_called()
        start.assert_not_called()

    def test_patch_check_does_not_download_a_new_version_on_the_stable_channel(self) -> None:
        self.settings.patch_auto = True
        with mock.patch.dict(
            os.environ, {"NH_TEST_MODE": "", "QQ_DIGEST_HOST_ACTIONS": ""}
        ), mock.patch("noticehub.webapp.updater") as fake, mock.patch(
            "noticehub.webapp._update_channel_name", return_value="stable"
        ), mock.patch("noticehub.webapp._start_update_download") as start:
            fake.pending_update.return_value = None
            fake.check.return_value = self._new_version_result()
            self.server._patch_check_once()
        start.assert_not_called()

    def test_patch_check_does_not_redownload_a_new_version_already_pending(self) -> None:
        self.settings.patch_auto = True
        with mock.patch.dict(
            os.environ, {"NH_TEST_MODE": "", "QQ_DIGEST_HOST_ACTIONS": ""}
        ), mock.patch("noticehub.webapp.updater") as fake, mock.patch(
            "noticehub.webapp._update_channel_name", return_value="beta"
        ), mock.patch("noticehub.webapp._start_update_download") as start:
            fake.pending_update.return_value = {"version": "1.1.0-beta.19", "patch": 0}
            fake.check.return_value = self._new_version_result()
            self.server._patch_check_once()
        start.assert_not_called()

    def test_patch_auto_setting_writes_env_and_takes_effect_live(self) -> None:
        env_path = Path(self.tmp.name) / "patch-auto.env"
        env_path.write_text("QQ_DIGEST_TOKEN=abc\n", encoding="utf-8")
        self.settings.env_file = str(env_path)
        try:
            result = self._post("/api/settings", {"patch_auto": False})
            self.assertTrue(result["ok"])
            self.assertIs(self.settings.patch_auto, False)
            self.assertIn("NOTICEHUB_PATCH_AUTO=0", env_path.read_text(encoding="utf-8"))
            self.assertEqual(os.environ.get("NOTICEHUB_PATCH_AUTO"), "0", "改完当场生效，不必重启")
        finally:
            os.environ.pop("NOTICEHUB_PATCH_AUTO", None)

    def test_patch_auto_must_be_saved_alone_and_as_a_real_boolean(self) -> None:
        env_path = Path(self.tmp.name) / "patch-auto-mixed.env"
        env_path.write_text("QQ_DIGEST_TOKEN=abc\n", encoding="utf-8")
        self.settings.env_file = str(env_path)
        before = env_path.read_text(encoding="utf-8")
        status, body = self._lab_request(
            "POST", "/api/settings", payload={"patch_auto": True, "update_channel": "stable"}
        )
        self.assertEqual(status, 400, body)
        self.assertIn("单独保存", body["error"])
        status, body = self._lab_request("POST", "/api/settings", payload={"patch_auto": "yes"})
        self.assertEqual(status, 400, body)
        self.assertIn("布尔", body["error"])
        self.assertEqual(env_path.read_text(encoding="utf-8"), before, "被拒的请求不许动 .env")

    def test_update_page_copy_and_banner_are_present(self) -> None:
        self.assertIn("版本更新", PAGE_SOURCE)
        self.assertIn("下载更新包", PAGE_SOURCE)
        self.assertIn("检查更新", PAGE_SOURCE)
        self.assertIn("更新包下载好后不会自己装；点托盘『重启并更新』才会换上新版，你的数据和订阅不会丢", PAGE_SOURCE)
        self.assertIn("点右下角托盘图标 →『重启并更新』，约 10–30 秒后会自动换好并重开", PAGE_SOURCE)
        self.assertIn("已就绪，等待重启", PAGE_SOURCE)
        self.assertNotIn("下载完成，正在安装", PAGE_SOURCE)
        self.assertNotIn("api('/api/update/apply'", PAGE_SOURCE)
        self.assertIn("refreshUpdate(false);", PAGE_SOURCE)
        self.assertIn("pending", PAGE_SOURCE)
        self.assertIn('id="update-banner"', PAGE_SOURCE)
        # 「点这里更新」要滚到更新卡片：它走全站通用 helper scrollTargetIntoView（矮目标 center：
        # 卡片高约 452px，落点保持实测过的 208/231 档；超高目标才顶边对齐，判据在 helper 的 harness 用例里）。
        banner_open = PAGE_SOURCE.split("open.addEventListener('click'", 1)[1].split("});", 1)[0]
        self.assertIn("showSettingsTab()", banner_open)
        self.assertIn("scrollTargetIntoView(card, 'smooth')", banner_open)
        self.assertNotIn("scrollIntoView({behavior: 'smooth', block: 'start'})", banner_open)
        self.assertIn("发现新版本", PAGE_SOURCE)
        self.assertIn("查看完整更新说明", PAGE_SOURCE)
        self.assertIn("applyButton.hidden = !showLatest || !!pending", PAGE_SOURCE)
        self.assertIn("pending ? '更新包已就绪", PAGE_SOURCE)
        self.assertIn("点托盘『重启并更新』", PAGE_SOURCE)
        self.assertIn('rel="noopener noreferrer"', PAGE_SOURCE)
        # 页面里出现的 notes_html 是服务端预渲染好的，客户端不再自己解析 markdown
        self.assertIn("notes_html", PAGE_SOURCE)

    def test_release_notes_split_and_escape(self) -> None:
        sections = webapp.split_release_notes("## 新增功能\n- 支持 A\n## 修复问题\n- 修掉 B\n## 已知问题\n- 老毛病 C")
        self.assertEqual(sections["features"], ["支持 A"])
        self.assertEqual(sections["fixes"], ["修掉 B"])
        self.assertEqual(sections["other"], ["老毛病 C"])
        loose = webapp.split_release_notes("## New\n- x\n## Fixes\n- y")
        self.assertEqual(loose["features"], ["x"])
        self.assertEqual(loose["fixes"], ["y"])
        plain = webapp.split_release_notes("没有分节的说明\n第二行")
        self.assertEqual(plain["other"], ["没有分节的说明", "第二行"])
        self.assertEqual(webapp.split_release_notes(""), {"features": [], "fixes": [], "other": []})
        html = webapp._release_notes_html("## 新增功能\n- <img src=x onerror=alert(1)>")
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)

    def test_announcement_endpoint_carries_current_version(self) -> None:
        data_dir = Path(self.tmp.name) / "ann-modal-version"
        self.settings.data_dir = data_dir
        payload_in = {"ok": True, "items": [{"id": "a1", "title": "计划维护", "body": "今晚 23:00 维护", "level": "warning", "seen_required": False}],
                      "from_cache": False, "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(), "error": "", "current_version": "1.1.0-beta.1"}
        with mock.patch("noticehub.webapp.announce.active", return_value=payload_in):
            payload = self._get("/api/announcements?refresh=1", "secret")
        self.assertEqual(payload["current_version"], "1.1.0-beta.1")
        self.assertEqual(len(payload["items"]), 1)
        self.assertFalse(payload["checking"])
        # 旧数据没有 current_version 时也要给个能显示的值（弹窗上写「当前版本 X」）
        fallback_dir = Path(self.tmp.name) / "ann-modal-fallback"
        self.settings.data_dir = fallback_dir
        payload_in.pop("current_version")
        with mock.patch("noticehub.webapp.announce.active", return_value=payload_in):
            fallback = self._get("/api/announcements?refresh=1", "secret")
        self.assertTrue(fallback["current_version"])

    def test_announcement_popup_markup_and_safe_rendering(self) -> None:
        self.assertIn('id="announce-modal"', PAGE_SOURCE)
        self.assertIn("我知道了", PAGE_SOURCE)
        self.assertIn("announce-modal-version", PAGE_SOURCE)
        self.assertIn("announce-modal-link", PAGE_SOURCE)
        self.assertIn("nh_ann_seen", PAGE_SOURCE)
        self.assertIn("if (item.seen_required) return false;", PAGE_SOURCE)
        self.assertIn('rel="noopener noreferrer"', PAGE_SOURCE)
        modal_js = PAGE_SOURCE.split("function appendAnnounceText", 1)[1].split("function refreshAnnouncements", 1)[0]
        self.assertIn("createTextNode", modal_js)
        self.assertNotIn("innerHTML", modal_js)  # 正文来自网络：只许 textContent / createTextNode

    def test_announcement_ui_uses_text_and_required_items_cannot_be_dismissed(self) -> None:
        # 公告走右上角悬浮通知：正文取首行进通知卡，全文进详情弹窗，都只用 textContent。
        self.assertIn(r"String(item.body || item.content || item.description || '').split('\n')[0]", PAGE_SOURCE)
        self.assertIn("body.textContent = String(item.body", PAGE_SOURCE)
        # seen_required（必须确认）的公告不给 ×；公告卡一律 sticky——不挂 6 秒自动消失，
        # 挂到用户查看/标已读/点 × 才撤（用户报的「通知突然消失」根因）。
        self.assertIn("dismissible: !item.seen_required", PAGE_SOURCE)
        self.assertIn("sticky: true,", PAGE_SOURCE)
        self.assertIn("onOpen: function ()", PAGE_SOURCE)
        # 点 × 的持久化仍在 seen_announce_ids 里（旧横幅「关闭」那份），不新造一个键。
        dismiss_branch = PAGE_SOURCE.split("function persistDismissedAnnouncement(id) {", 1)[1].split("\n}", 1)[0]
        self.assertIn("String(id)", dismiss_branch)
        self.assertIn("localStorage.setItem('seen_announce_ids'", dismiss_branch)
        self.assertIn("localStorage.getItem('seen_announce_ids')", PAGE_SOURCE)
        self.assertIn("localStorage.setItem('seen_announce_ids'", PAGE_SOURCE)
        self.assertIn("item.level || 'info'", PAGE_SOURCE)
        self.assertIn("'暂无公告。'", PAGE_SOURCE)
        self.assertNotIn("detail.innerHTML", PAGE_SOURCE)
        # 服务公告全部进悬浮通知，页头不再留公告横幅的坑位；版本更新横幅照旧。
        self.assertNotIn('id="announce-banner"', PAGE_SOURCE)
        self.assertIn('id="update-banner"', PAGE_SOURCE)
        self.assertIn("id = 'announce-list'", PAGE_SOURCE)

    def test_donation_qr_serves_jpeg_and_missing_is_404(self) -> None:
        asset = Path(__file__).resolve().parents[1] / "assets" / "donate-wechat.jpg"
        with mock.patch("noticehub.webapp.RUNTIME_ROOT", asset.parent.parent):
            status, headers, body = self._raw_request("/api/donate/qr.png", token="")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "image/jpeg")
        self.assertEqual(len(body), asset.stat().st_size)
        with mock.patch("noticehub.webapp.RUNTIME_ROOT", Path(self.tmp.name)):
            status, _, _ = self._raw_request("/api/donate/qr.png", token="")
        self.assertEqual(status, 404)

    def test_onboarding_requires_acceptance_and_is_idempotent(self) -> None:
        self.assertEqual(self._get("/api/onboarding", token="")["required"], True)
        first = self._post("/api/onboarding/accept", {}, token="")
        self.assertTrue(first["ok"])
        self.assertEqual(self._get("/api/onboarding", token="")["required"], False)
        self.assertEqual(self.store.meta_get("onboarding_accepted_at", ""), first["accepted_at"])
        self.assertEqual(self._post("/api/onboarding/accept", {}, token="")["accepted_at"], first["accepted_at"])

    def test_page_contains_onboarding_and_donation_copy(self) -> None:
        for text in ("支持开发者", "/api/donate/qr.png", "欢迎使用群务台", "我知道了，开始使用", "/api/onboarding", 'id="onboarding-close"', 'aria-label="关闭"', 'id="onboarding-error"'):
            self.assertIn(text, PAGE_SOURCE)
        # 这是风险提示而不是要用户签的协议：不许再出现「我已阅读并理解 / 稍后再说」这种
        # 假装要同意、不同意也能用的假门。
        self.assertNotIn("我已阅读并理解", PAGE_SOURCE)
        self.assertNotIn("稍后再说", PAGE_SOURCE)
        self.assertNotIn('id="onboarding-dismiss"', PAGE_SOURCE)
        onboarding_script = PAGE_SCRIPT.split("function initOnboarding()", 1)[1].split("function applyHostingState", 1)[0]
        self.assertIn("modal.addEventListener('click',function(event){if(event.target===modal)closeOnboarding();});", onboarding_script)
        # 从哪条路关掉都算看过：closeOnboarding 里就记一次（幂等）。
        self.assertIn("function closeOnboarding(){modal.hidden=true;persistAccept();}", onboarding_script)
        self.assertIn("if(accepted)return;", onboarding_script)
        self.assertIn("fetch('/api/onboarding/accept'", onboarding_script)
        self.assertIn("window.setTimeout(function(){send(attempt+1);},2000);", onboarding_script)
        self.assertNotIn("确认失败，请重试", onboarding_script)
        self.assertIn("本软件按 PolyForm Noncommercial 1.0.0 许可「按原样」提供", PAGE_SOURCE)
        self.assertIn("商用需授权，请联系 jkl2755908073@qq.com", PAGE_SOURCE)
        self.assertNotIn("上游 wc985732-lang/qq-live-digest 的部分仍适用 MIT，详情见 NOTICE", PAGE_SOURCE)
        self.assertNotIn("本软件按 MIT 许可", PAGE_SOURCE)

    def test_requires_token(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as context:
            self._get("/api/tasks")
        self.assertEqual(context.exception.code, 401)

    def test_list_tasks_and_complete(self) -> None:
        with mock.patch("noticehub.webapp.now_local", return_value=NOW):
            data = self._get("/api/tasks", token="secret")
            self.assertEqual(len(data["today"]), 1)
            self.assertEqual(data["today"][0]["summary"], "提交实验报告")
            self.assertIn("今天", data["today"][0]["deadline_text"])

            result = self._post(f"/api/tasks/{self.task_id}", {"done": True})
            self.assertTrue(result["ok"])

            data = self._get("/api/tasks", token="secret")
            self.assertEqual(len(data["done"]), 1)
            self.assertEqual(len(data["today"]), 0)

    def test_clear_finished_tasks_route_removes_done_and_keeps_open(self) -> None:
        done_id = self.store.upsert_task(task_key="api-done", summary="已经做完的待办")
        self.store.apply_task_action(done_id, "done")
        open_id = self.store.upsert_task(task_key="api-open", summary="还没做完的待办")

        result = self._post("/api/tasks/clear-finished", {})

        self.assertEqual(result, {"ok": True, "removed": 1})
        data = self._get("/api/tasks", token="secret")
        self.assertEqual(data["done"], [])
        self.assertEqual(data["stats"]["done"], 0)
        remaining = {task["id"] for bucket in ("today", "week", "later") for task in data[bucket]}
        self.assertIn(open_id, remaining)
        self.assertNotIn(done_id, remaining)
        self.assertIsNone(self.store.get_task(done_id))

    def test_delete_action_removes_finished_task_but_not_open_one(self) -> None:
        done_id = self.store.upsert_task(task_key="api-delete-done", summary="已完成")
        self.store.apply_task_action(done_id, "done")
        status, _headers, body = self._raw_request(f"/api/tasks/{done_id}", "POST", {"action": "delete"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertIsNone(self.store.get_task(done_id))

        # open 的不能真删：没有「删掉但还欠着」这条路，先让用户走忽略。
        status, _headers, body = self._raw_request(f"/api/tasks/{self.task_id}", "POST", {"action": "delete"})
        self.assertEqual(status, 404)
        self.assertFalse(json.loads(body)["ok"])
        self.assertIsNotNone(self.store.get_task(self.task_id))

    def test_urgent_override_persists_and_orders_by_effective_state(self) -> None:
        urgent_id = self.store.upsert_task(
            task_key="urgent-ui",
            summary="User urgent override",
            category="action",
            deadline=iso(dt.datetime(2026, 9, 30, 18, 0)),
        )
        status, _, body = self._raw_request("/api/tasks/urgent", "POST", {"task_id": str(urgent_id), "urgent": True})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

        with mock.patch("noticehub.webapp.now_local", return_value=NOW):
            first = self._get("/api/tasks", token="secret")
            refreshed = self._get("/api/tasks", token="secret")
        self.assertEqual(first["today"][0]["id"], urgent_id)
        self.assertTrue(first["today"][0]["effective_urgent"])
        self.assertEqual(first["today"][0]["urgent_override"], 1)
        self.assertEqual(refreshed["today"][0]["id"], urgent_id)
        self.assertTrue(refreshed["today"][0]["effective_urgent"])

        status, _, body = self._raw_request("/api/tasks/urgent", "POST", {"task_id": str(urgent_id), "urgent": None})
        self.assertEqual(status, 200)
        self.assertIsNone(json.loads(body)["urgent_override"])
        with mock.patch("noticehub.webapp.now_local", return_value=NOW):
            reset = self._get("/api/tasks", token="secret")
            reset_again = self._get("/api/tasks", token="secret")
        task = next(item for item in reset["today"] if item["id"] == urgent_id)
        self.assertFalse(task["effective_urgent"])
        self.assertIsNone(task["urgent_override"])
        self.assertFalse(next(item for item in reset_again["today"] if item["id"] == urgent_id)["effective_urgent"])

    def test_urgent_override_validates_values_and_missing_tasks(self) -> None:
        invalid = (
            {"task_id": str(self.task_id), "urgent": 1},
            {"task_id": str(self.task_id), "urgent": "true"},
            {"task_id": str(self.task_id)},
            {"task_id": "not-an-id", "urgent": True},
            {"task_id": "9" * 19, "urgent": True},
        )
        for payload in invalid:
            status, headers, body = self._raw_request("/api/tasks/urgent", "POST", payload)
            self.assertEqual(status, 400)
            self.assertEqual(headers.get_content_type(), "application/json")
            self.assertFalse(json.loads(body)["ok"])
        status, _, body = self._raw_request("/api/tasks/urgent", "POST", {"task_id": "999999999", "urgent": True})
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"ok": False, "error": "任务不存在"})
        self.assertIsNone(self.store.get_task(self.task_id)["urgent_override"])

    def test_pin_endpoint_toggles_and_lists_pinned(self) -> None:
        pinned_id = self.store.upsert_task(
            task_key="pin-ui",
            summary="要置顶的日程",
            category="action",
            deadline=iso(dt.datetime(2026, 9, 30, 18, 0)),
        )
        other_id = self.store.upsert_task(
            task_key="pin-other",
            summary="普通日程",
            category="action",
            deadline=iso(dt.datetime(2026, 10, 1, 18, 0)),
        )
        status, _, body = self._raw_request("/api/tasks/pin", "POST", {"task_id": str(pinned_id), "pinned": True})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertTrue(json.loads(body)["pinned"])

        with mock.patch("noticehub.webapp.now_local", return_value=NOW):
            data = self._get("/api/tasks", token="secret")
        self.assertEqual([item["id"] for item in data["pinned"]], [pinned_id])
        self.assertTrue(data["pinned"][0]["pinned"])
        self.assertNotIn(other_id, [item["id"] for item in data["pinned"]])
        self.assertFalse(next(item for item in data["today"] + data["week"] + data["later"] if item["id"] == other_id)["pinned"])

        status, _, body = self._raw_request("/api/tasks/pin", "POST", {"task_id": str(pinned_id), "pinned": False})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["pinned"])
        with mock.patch("noticehub.webapp.now_local", return_value=NOW):
            cleared = self._get("/api/tasks", token="secret")
        self.assertEqual(cleared["pinned"], [])

    def test_pin_endpoint_validates_values_and_done_tasks(self) -> None:
        invalid = (
            {"task_id": str(self.task_id), "pinned": 1},
            {"task_id": str(self.task_id), "pinned": "true"},
            {"task_id": str(self.task_id)},
            {"task_id": "not-an-id", "pinned": True},
            {"task_id": "9" * 19, "pinned": True},
        )
        for payload in invalid:
            status, headers, body = self._raw_request("/api/tasks/pin", "POST", payload)
            self.assertEqual(status, 400)
            self.assertEqual(headers.get_content_type(), "application/json")
            self.assertFalse(json.loads(body)["ok"])
        status, _, body = self._raw_request("/api/tasks/pin", "POST", {"task_id": "999999999", "pinned": True})
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"ok": False, "error": "任务不存在"})
        done_id = self.store.upsert_task(task_key="pin-done-api", summary="已经做完", category="action")
        self.store.set_task_status(done_id, True)
        status, _, body = self._raw_request("/api/tasks/pin", "POST", {"task_id": str(done_id), "pinned": True})
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"ok": False, "error": "已完成的任务不能置顶"})
        self.assertFalse(self.store.get_task(done_id)["pinned"])

    def test_task_stats_are_the_same_on_tasks_and_meta(self) -> None:
        tasks = self._get("/api/tasks", token="secret")
        meta = self._get("/api/meta", token="secret")
        self.assertEqual(tasks["stats"], meta["stats"])
        self.assertEqual(
            set(tasks["stats"]),
            {"total", "candidate", "open", "done", "dismissed", "expired", "overdue"},
        )

    def test_create_endpoint_adds_manual_task_with_and_without_deadline(self) -> None:
        status, _, body = self._raw_request(
            "/api/tasks/create", "POST", {"summary": "  给导师  发周报  ", "deadline": "2026-09-30T18:00"}
        )
        self.assertEqual(status, 200)
        created = json.loads(body)
        self.assertTrue(created["ok"])
        self.assertEqual(created["summary"], "给导师 发周报")
        self.assertEqual(created["deadline"], "2026-09-30T18:00:00")
        self.assertGreater(created["task_id"], 0)

        status, _, body = self._raw_request("/api/tasks/create", "POST", {"summary": "整理桌面"})
        self.assertEqual(status, 200)
        loose = json.loads(body)
        self.assertEqual(loose["deadline"], "")

        with mock.patch("noticehub.webapp.now_local", return_value=NOW):
            data = self._get("/api/tasks", token="secret")
        today = {item["id"]: item for item in data["today"]}
        self.assertIn(created["task_id"], today)
        self.assertEqual(today[created["task_id"]]["summary"], "给导师 发周报")
        self.assertFalse(today[created["task_id"]]["done"])
        self.assertIn(loose["task_id"], [item["id"] for item in data["later"]])
        self.assertNotIn(created["task_id"], [item["id"] for item in data["candidates"]])

    def test_create_endpoint_validates_summary_and_deadline(self) -> None:
        before = len(self.store.list_tasks())
        invalid = (
            ({}, "summary 必须是文本"),
            ({"summary": 12}, "summary 必须是文本"),
            ({"summary": "   "}, "请填写待办内容"),
            ({"summary": "x" * 201}, "待办内容请控制在 200 字以内"),
            ({"summary": "正常内容", "deadline": 123}, "deadline 必须是文本"),
            ({"summary": "正常内容", "deadline": "下周三"}, "截止时间格式不对，请重新选择"),
        )
        for payload, message in invalid:
            status, headers, body = self._raw_request("/api/tasks/create", "POST", payload)
            self.assertEqual(status, 400)
            self.assertEqual(headers.get_content_type(), "application/json")
            self.assertEqual(json.loads(body), {"ok": False, "error": message})
        self.assertEqual(len(self.store.list_tasks()), before)

    def test_sync_info_counts_canonical_calendar_events(self) -> None:
        self.store.upsert_task(task_key="invalid-calendar-date", summary="Not an event", deadline="2026-99-99")
        self.store.upsert_task(task_key="event-marker-summary", summary="BEGIN:VEVENT", deadline="2026-10-01")
        with mock.patch("noticehub.webapp._adapter_ipv4s", return_value=[]), mock.patch(
            "noticehub.webapp._lan_ipv4", return_value="192.168.8.10"
        ), mock.patch("noticehub.webapp._hostname_ipv4s", return_value=[]), mock.patch(
            "noticehub.webapp._tailscale_calendar_url", return_value=""
        ):
            info = self._get("/api/sync/info", token="secret")
        self.assertEqual(info["lan_base"], f"http://192.168.8.10:{self.server.server.server_address[1]}")
        self.assertEqual(info["port"], self.server.server.server_address[1])
        self.assertEqual(info["events"], 2)
        self.assertEqual(info["calendars"][0]["kind"], "lan")
        self.assertEqual(info["calendars"][0]["url"], info["lan_base"] + "/calendar.ics?token=secret")
        status, headers, raw = self._raw_request("/calendar.ics?token=secret")
        self.assertEqual(status, 200)
        self.assertEqual(sum(line == b"BEGIN:VEVENT" for line in raw.splitlines()), info["events"])
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")

    def test_calendar_ics_requires_token_and_accepts_query_token(self) -> None:
        """手机日历订阅 URL 无法带自定义 header：token 必须能走 ?token=，没有就 401。"""
        status, headers, raw = self._raw_request("/calendar.ics", token="")
        self.assertEqual(status, 401)
        self.assertNotIn(b"BEGIN:VCALENDAR", raw)
        status, headers, raw = self._raw_request("/calendar.ics?token=secret")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type", "").split(";")[0], "text/calendar")

    def test_tailscale_calendar_url_recognises_whole_site_funnel_route(self) -> None:
        """S2：Funnel 开的是整站 "/"，/calendar.ics 已公网可达，别再要求专门的 .ics 规则。"""
        serve = json.dumps(
            {
                "AllowFunnel": {"demo.ts.net:8443": True},
                "Web": {"demo.ts.net:8443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8766"}}}},
            }
        )
        with mock.patch("noticehub.webapp.tunnel.tailscale_executable", return_value="tailscale"), mock.patch(
            "noticehub.webapp.tunnel.subprocess.run", return_value=mock.Mock(stdout=serve)
        ):
            self.assertEqual(webapp._tailscale_calendar_url(8766), "https://demo.ts.net:8443/calendar.ics")

    def test_sync_info_prefers_public_address_and_keeps_lan_alternate(self) -> None:
        port = self.server.server.server_address[1]
        with mock.patch("noticehub.webapp._adapter_ipv4s", return_value=[]), mock.patch(
            "noticehub.webapp._lan_ipv4", return_value="100.78.7.108"
        ), mock.patch("noticehub.webapp._hostname_ipv4s", return_value=[]), mock.patch(
            "noticehub.webapp._tailscale_calendar_url", return_value="https://demo.tail1234.ts.net/notice.ics"
        ):
            info = self._get("/api/sync/info", token="secret")
        self.assertEqual(info["calendars"][0]["kind"], "public")
        self.assertEqual(info["calendars"][0]["url"], "https://demo.tail1234.ts.net/notice.ics?token=secret")
        self.assertTrue(any(item["kind"] == "lan" and item["url"] == f"http://100.78.7.108:{port}/calendar.ics?token=secret" for item in info["calendars"]))

    def test_tailscale_calendar_url_reads_proxy_url_with_path(self) -> None:
        """serve status 的 Handler.Proxy 是完整 URL（带 /calendar.ics），端口必须按 URL 解析。

        回归：曾经用 proxy.rsplit(":", 1)[-1] 取端口，拿到的是 "8766/calendar.ics"，
        与端口永远不相等，于是公网订阅地址在页面上整条消失（手机用流量没法订阅）。
        """
        serve = json.dumps(
            {
                "AllowFunnel": {"demo-machine.demo-tailnet.ts.net:443": True},
                "Web": {
                    "demo-machine.demo-tailnet.ts.net:443": {
                        "Handlers": {
                            "/": {"Proxy": "http://127.0.0.1:8787"},
                            "/notice.ics": {"Proxy": "http://127.0.0.1:8766/calendar.ics"},
                        }
                    }
                },
            }
        )
        with mock.patch("noticehub.webapp.shutil.which", return_value=__file__), mock.patch(
            "noticehub.webapp.subprocess.run", return_value=mock.Mock(stdout=serve)
        ) as run:
            self.assertEqual(webapp._tailscale_calendar_url(8766), "https://demo-machine.demo-tailnet.ts.net/notice.ics")
            self.assertEqual(webapp._tailscale_calendar_url(9999), "")
            for call in run.call_args_list:
                self.assertTrue(call.kwargs["creationflags"] & 0x08000000)

    def test_sync_info_reports_unavailable_lan_address_as_json(self) -> None:
        with mock.patch("noticehub.webapp._lan_hosts", return_value=[]), mock.patch("noticehub.webapp._tailscale_calendar_url", return_value=""):
            status, headers, body = self._raw_request("/api/sync/info")
        self.assertEqual(status, 503)
        self.assertEqual(headers.get_content_type(), "application/json")
        self.assertEqual(json.loads(body), {"ok": False, "error": "无法确定可供手机访问的局域网地址"})

    def test_sync_test_endpoint_reads_advertised_url_and_rejects_foreign_hosts(self) -> None:
        port = self.server.server.server_address[1]
        info = self._get("/api/sync/info", token="secret")
        local = f"http://127.0.0.1:{port}/calendar.ics?token=secret"
        result = self._get("/api/sync/test?" + urllib.parse.urlencode({"url": local}), token="secret")
        self.assertTrue(result["ok"])
        self.assertEqual(result["events"], info["events"])
        self.assertEqual(result["url"], local)

        status, headers, body = self._raw_request("/api/sync/test?" + urllib.parse.urlencode({"url": "http://example.com/calendar.ics"}))
        self.assertEqual(status, 400)
        self.assertEqual(headers.get_content_type(), "application/json")
        self.assertEqual(json.loads(body), {"ok": False, "error": "只能测试本服务公布的日历地址"})
    def test_sync_qr_is_native_scale_and_rejects_bad_text_as_json(self) -> None:
        text = "webcal://example.com/calendar.ics?token=" + "x" * 45 + "&标签=中文"
        path = "/api/sync/qr.png?" + urllib.parse.urlencode({"text": text, "token": "secret"})
        status, headers, raw = self._raw_request(path, token="")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "image/png")
        self.assertTrue(raw.startswith(bytes.fromhex("89504e470d0a1a0a")))
        image = Image.open(io.BytesIO(raw))
        self.assertEqual(image.size, ((len(matrix(text)) + 8) * 10,) * 2)
        self.assertEqual(image.size, (490, 490))

        prefix = "webcal://example.com/"
        boundary_text = prefix + "x" * (213 - len(prefix))
        path = "/api/sync/qr.png?" + urllib.parse.urlencode({"text": boundary_text})
        status, headers, raw = self._raw_request(path)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "image/png")
        self.assertEqual(Image.open(io.BytesIO(raw)).size, (650, 650))

        invalid_paths = (
            "/api/sync/qr.png",
            "/api/sync/qr.png?" + urllib.parse.urlencode({"text": ""}),
            "/api/sync/qr.png?" + urllib.parse.urlencode({"text": "not a URL"}),
            "/api/sync/qr.png?" + urllib.parse.urlencode({"text": "https://u:p@h/x.ics"}),
            "/api/sync/qr.png?" + urllib.parse.urlencode({"text": "webcal://example.com/line" + chr(10) + "feed"}),
            "/api/sync/qr.png?" + urllib.parse.urlencode({"text": "webcal://example.com/" + "x" * 600}),
            "/api/sync/qr.png?" + urllib.parse.urlencode({"text": prefix + "x" * (214 - len(prefix))}),
            "/api/sync/qr.png?" + urllib.parse.urlencode({"text": "webcal://example.com/" + "x" * 220}),
        )
        for path in invalid_paths:
            status, headers, body = self._raw_request(path)
            self.assertEqual(status, 400)
            self.assertEqual(headers.get_content_type(), "application/json")
            self.assertFalse(json.loads(body)["ok"])

    def test_app_qr_carries_the_connect_link_and_rejects_bad_addresses(self) -> None:
        """手机相机扫一下就带着地址和令牌打开 App；这条码在本页登录后才给，地址必须是 http(s)。"""
        base = "https://demo-machine.demo-tailnet.ts.net:8443"
        expected = "noticehub://connect?" + urllib.parse.urlencode({"base": base, "token": "secret"})
        status, headers, raw = self._raw_request("/api/app/qr.png?" + urllib.parse.urlencode({"base": base, "token": "secret"}), token="")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "image/png")
        self.assertTrue(raw.startswith(bytes.fromhex("89504e470d0a1a0a")))
        self.assertEqual(Image.open(io.BytesIO(raw)).size, ((len(matrix(expected)) + 8) * 8,) * 2)

        invalid_paths = (
            "/api/app/qr.png",
            "/api/app/qr.png?" + urllib.parse.urlencode({"base": "ftp://host/x"}),
            "/api/app/qr.png?" + urllib.parse.urlencode({"base": "https://user:pw@host/x"}),
            "/api/app/qr.png?" + urllib.parse.urlencode({"base": "https://" + "h" * 600 + ".net"}),
        )
        for path in invalid_paths:
            status, headers, body = self._raw_request(path, token="secret")
            self.assertEqual(status, 400)
            self.assertFalse(json.loads(body)["ok"])
        # 这条地址里带着令牌，所以没登录就不给
        status, _, _ = self._raw_request("/api/app/qr.png?" + urllib.parse.urlencode({"base": base}), token="")
        self.assertEqual(status, 401)

    def test_four_unauthenticated_sync_and_task_requests_have_no_side_effects(self) -> None:
        before = self.store.get_task(self.task_id)
        requests = (
            ("/api/sync/info", "GET", None),
            ("/api/sync/qr.png?text=" + urllib.parse.quote("webcal://example.com/calendar.ics"), "GET", None),
            ("/api/tasks/urgent", "POST", {"task_id": str(self.task_id), "urgent": True}),
            (f"/api/tasks/{self.task_id}", "POST", {"action": "done"}),
        )
        with mock.patch("noticehub.webapp._lan_ipv4") as lan_probe:
            for path, method, payload in requests:
                status, headers, body = self._raw_request(path, method, payload, token="")
                self.assertEqual(status, 401, path)
                self.assertEqual(headers.get_content_type(), "application/json")
                self.assertEqual(json.loads(body), {"ok": False, "error": "invalid token"})
            lan_probe.assert_not_called()
        after = self.store.get_task(self.task_id)
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["urgent_override"], before["urgent_override"])

    def test_candidate_group_and_actions(self) -> None:
        candidate_id = self.store.upsert_task(
            task_key="candidate-1",
            summary="可能要交报名表",
            category="action",
            status="candidate",
            confidence=0.66,
            deadline=iso(NOW + dt.timedelta(hours=3)),
            groups=["班级群"],
            classification_reason="可能出现弱行动词",
            evidence="记得看一下报名表",
        )
        with mock.patch("noticehub.webapp.now_local", return_value=NOW):
            data = self._get("/api/tasks", token="secret")
        self.assertEqual(len(data["candidates"]), 1)
        self.assertEqual(data["candidates"][0]["id"], candidate_id)
        self.assertIn("把握", data["candidates"][0]["confidence_text"])
        self.assertIn("弱行动词", data["candidates"][0]["confidence_reason"])

        result = self._post(f"/api/tasks/{candidate_id}", {"action": "confirm"})
        self.assertTrue(result["ok"])
        self.assertEqual(self.store.get_task(candidate_id)["status"], "open")

    def test_calendar_page_and_ics_are_served(self) -> None:
        request = urllib.request.Request(f"{self.base}/calendar?token=secret")
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            page = response.read().decode("utf-8")
        self.assertIn("月历", page)
        # 月历页必须像主页一样把 token 带进 /api/tasks，否则页面永远空白
        self.assertIn("X-Token", page)
        self.store.upsert_task(
            task_key="all-day", summary="逗号,分号;反斜\\线\n长文本" * 12,
            deadline="2026-10-01",
        )
        request = urllib.request.Request(f"{self.base}/calendar.ics?token=secret")
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.headers.get_content_type(), "text/calendar")
            raw = response.read()
        self.assertIn(b"\r\n", raw)
        text = raw.decode("utf-8")
        self.assertEqual(text.count("BEGIN:VEVENT"), 2)
        self.assertIn("DTSTART;VALUE=DATE:20261001", text)
        self.assertIn("DTEND;VALUE=DATE:20261002", text)
        self.assertIn("SUMMARY:", text)
        self.assertIn("STATUS:CONFIRMED", text)
        self.assertNotIn("STATUS:NEEDS-ACTION", text)
        self.assertIn("\\\\", text)
        self.assertIn("\\,", text)
        self.assertIn("\\;", text)
        self.assertTrue(all(len(line.encode("utf-8")) <= 75 for line in text.split("\r\n") if line))
        # RFC 5545 3.2.19：DTSTART 引用的每个 TZID 都必须由 VTIMEZONE 定义，否则 iOS 会丢掉该日程
        self.assertIn("BEGIN:VTIMEZONE", text)
        self.assertLess(text.index("BEGIN:VTIMEZONE"), text.index("BEGIN:VEVENT"))
        defined_tzids = {line[len("TZID:"):] for line in text.split("\r\n") if line.startswith("TZID:")}
        referenced_tzids = {
            match.group(1)
            for match in re.finditer(r"^DTSTART;TZID=([^:\r\n]+):", text, re.M)
        }
        self.assertIn("Asia/Shanghai", referenced_tzids)
        self.assertEqual(referenced_tzids - defined_tzids, set())
        self.assertIn("TZOFFSETTO:+0800", text)
        # 客户端（尤其 iOS）靠 SEQUENCE / LAST-MODIFIED 判断同一个 UID 要不要更新，
        # 靠 X-PUBLISHED-TTL / REFRESH-INTERVAL 决定多久回来拉一次；
        # 少了它们，网页上改过的截止时间同步不到手机上。
        self.assertIn("X-PUBLISHED-TTL:PT15M", text)
        self.assertIn("REFRESH-INTERVAL;VALUE=DURATION:PT15M", text)
        self.assertEqual(text.count("LAST-MODIFIED:"), 2)
        # ICS 用 CRLF 折行，按行切再判断，别拿 ^$ 去匹配整段文本
        sequences = [line[len("SEQUENCE:"):] for line in text.split("\r\n") if line.startswith("SEQUENCE:")]
        self.assertEqual(len(sequences), 2)
        self.assertTrue(all(value.isdigit() and int(value) < 2**31 for value in sequences))
        # DTSTAMP 必须是真正的 UTC：本地时间直接拼 Z 会差 8 小时
        stamp_text = next(line[len("DTSTAMP:"):] for line in text.split("\r\n") if line.startswith("DTSTAMP:"))
        stamp = dt.datetime.strptime(stamp_text, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
        self.assertLess(abs((dt.datetime.now(dt.timezone.utc) - stamp).total_seconds()), 300)

    def test_ics_sequence_and_last_modified_follow_task_updates(self) -> None:
        """回归：改过截止时间的任务必须让 SEQUENCE 变大，否则 iOS 不刷新该事件。"""

        def render(updated_at: str) -> str:
            return ics.render_calendar(
                [{"id": 7, "summary": "交实验报告", "deadline": "2026-10-09T15:15", "updated_at": updated_at}]
            )

        def field(text: str, name: str) -> str:
            return next(line[len(name) + 1 :] for line in text.split("\r\n") if line.startswith(name + ":"))

        before = render("2026-10-09T10:00:00")
        after = render("2026-10-09T10:01:00")

        def parse(value: str) -> dt.datetime:
            return dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ")

        self.assertLess(int(field(before, "SEQUENCE")), int(field(after, "SEQUENCE")))
        self.assertNotEqual(field(before, "LAST-MODIFIED"), field(after, "LAST-MODIFIED"))
        self.assertTrue(field(before, "LAST-MODIFIED").endswith("Z"))
        # 两个时间点相差 1 分钟，换算成 UTC 后间隔必须仍是 60 秒（说明是真换算，不是原样搬运）
        delta = parse(field(after, "LAST-MODIFIED")) - parse(field(before, "LAST-MODIFIED"))
        self.assertEqual(delta.total_seconds(), 60)
        # 没有 updated_at 的旧数据也要给出合法 SEQUENCE
        self.assertEqual(field(render(""), "SEQUENCE"), "0")

    def test_page_is_served(self) -> None:
        request = urllib.request.Request(f"{self.base}/?token=secret")
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read().decode("utf-8")
        # 页签写的是产品名：锁标签本身，别用页面里到处都有的品牌字样冒充。
        self.assertIn("<title>群务台</title>", body)
        self.assertNotIn("群消息待办", body)
        self.assertIn("/api/tasks", body)

    def test_dashboard_has_accessible_tabs_and_controlled_panels(self) -> None:
        request = urllib.request.Request(f"{self.base}/?token=secret")
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read().decode("utf-8")
        self.assertRegex(body, r'<nav\b[^>]*aria-label="主导航"[^>]*role="tablist"')
        for name in ("tasks", "notices", "settings"):
            self.assertRegex(body, rf'<button id="tab-{name}-button"[^>]*role="tab"[^>]*aria-controls="tab-{name}"')
            self.assertRegex(body, rf'<section id="tab-{name}"[^>]*role="tabpanel"')
            self.assertIn(f'aria-labelledby="tab-{name}-button"', body)
        self.assertRegex(body, r'id="tab-tasks-button"[^>]*tabindex="0"')
        self.assertIn('tabindex="-1"', body)
        self.assertIn("other.tabIndex = -1", body)
        self.assertRegex(body, r"button\.onkeydown\s*=\s*function \(event\)")
        self.assertIn("event.key === 'ArrowRight'", body)
        self.assertIn("event.preventDefault(); tabs[next].focus(); tabs[next].click();", body)
        self.assertIn("prefers-reduced-motion:reduce", body)

    def test_pwa_assets_are_public(self) -> None:
        assets = (
            ("/manifest.webmanifest", "application/manifest+json"),
            ("/icon.svg", "image/svg+xml"),
            ("/sw.js", "text/javascript"),
            # 加桌面快捷方式要的位图：安卓取 192/512，iOS 只认 apple-touch-icon。
            ("/icon-192.png", "image/png"),
            ("/icon-512.png", "image/png"),
            ("/icon-maskable-512.png", "image/png"),
            ("/apple-touch-icon.png", "image/png"),
        )
        for path, content_type in assets:
            with self.subTest(path=path):
                with urllib.request.urlopen(self.base + path, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn(content_type, response.headers.get_content_type())
                    self.assertTrue(response.read())

    def test_service_worker_stays_fresh_and_out_of_the_api(self) -> None:
        """离线外壳只装静态资源：sw 每次都要真取，接口数据一律不进 Cache Storage。"""
        from noticehub import webapp

        with urllib.request.urlopen(self.base + "/sw.js", timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("text/javascript", response.headers.get("Content-Type", ""))
            self.assertIn("no-cache", response.headers.get("Cache-Control", ""))
            self.assertEqual(response.headers.get("Service-Worker-Allowed"), "/")
            self.assertEqual(response.read().decode("utf-8"), webapp.SERVICE_WORKER_JS)
        # 页面注册的 /js/sw.js 就是这一份字节：两边各留一版，早晚长成两种离线行为。
        self.assertEqual(
            webapp.SERVICE_WORKER_JS,
            (PROJECT_ROOT / "noticehub" / "web_static" / "js" / "sw.js").read_text(encoding="utf-8"),
        )
        # fetch 处理里先看到 /api/ 就直接 return，别走到 caches.open。
        fetch_handler = webapp.SERVICE_WORKER_JS.split("addEventListener('fetch'", 1)[1]
        self.assertLess(fetch_handler.index("'/api/'"), fetch_handler.index("caches.open"))
        shell = webapp.SERVICE_WORKER_JS.split("SHELL = ", 1)[1].split("];", 1)[0]
        self.assertNotIn("/api/", shell)
        with urllib.request.urlopen(self.base + "/manifest.webmanifest", timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("manifest", response.headers.get("Content-Type", ""))

    def test_manifest_declares_installable_icons(self) -> None:
        """「加到桌面」在安卓/iOS 上都只吃位图，SVG 单条声明装不出独立图标。"""
        from noticehub.webapp import MANIFEST_JSON

        manifest = json.loads(MANIFEST_JSON)
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["start_url"], "/")
        icons = {item["src"]: item for item in manifest["icons"]}
        self.assertIn("/icon-192.png", icons)
        self.assertEqual(icons["/icon-192.png"]["sizes"], "192x192")
        self.assertIn("/icon-512.png", icons)
        self.assertEqual(icons["/icon-512.png"]["sizes"], "512x512")
        self.assertEqual(icons["/icon-maskable-512.png"]["purpose"], "maskable")
        self.assertEqual(icons["/icon.svg"]["type"], "image/svg+xml")
        for name in ("/icon-192.png", "/icon-512.png", "/icon-maskable-512.png"):
            self.assertEqual(icons[name]["type"], "image/png")
        self.assertIn('<link rel="apple-touch-icon" href="/apple-touch-icon.png">', PAGE_SOURCE)

    def test_offline_shell_speaks_up_when_network_is_gone(self) -> None:
        """装了桌面快捷方式后离线也能翻开页面，那就必须在屏幕上说清「看到的是旧的」。"""
        # 注册只在安全上下文：https 或本机回环地址（手在自己电脑上打开也要能装）。
        self.assertIn("var nhSecureShell = location.protocol === 'https:' ||", PAGE_SOURCE)
        self.assertIn(r"/^(localhost|127(\.\d+){3}|\[::1\])$/.test(location.hostname)", PAGE_SOURCE)
        self.assertIn("if ('serviceWorker' in navigator && nhSecureShell) {", PAGE_SOURCE)
        # 两种失败各一句不同的说法：没网 vs 本机服务没起来。
        self.assertIn("kind === 'offline'", PAGE_SOURCE)
        self.assertIn("连不上群务台的服务", PAGE_SOURCE)
        self.assertIn('id="net-bar"', PAGE_SOURCE)
        self.assertIn('id="net-bar-retry" type="button">重试<', PAGE_SOURCE)
        self.assertIn("window.addEventListener('offline', function () { show('offline'); });", PAGE_SOURCE)
        # 只有请求没发出去才挂条子；服务回了错误码不算断网。
        self.assertIn("error.name === 'TypeError'", PAGE_SOURCE)
        self.assertIn("if (window.nhNet) window.nhNet.ok();", PAGE_SOURCE)

    def test_phone_mode_previews_the_real_narrow_layout(self) -> None:
        """手机模式：桌面里套一台 390×844 的手机，iframe 载同一个页面的 device=1。
        带 device=1 打开时入口必须自己藏起来，否则点一层套一层；退出要按钮和 Esc 两条路。"""
        # 入口在顶栏，裸 .btn 就是 40px 触控高。
        self.assertIn('<button id="device-mode" class="btn" type="button">手机模式</button>', PAGE_SOURCE)
        # device=1 在 <head> 里就落到 data-device，别让已隐藏的按钮先闪一下。
        self.assertIn(
            "if(/[?&]device=1(&|$)/.test(location.search))document.documentElement.dataset.device='1';",
            PAGE_SOURCE,
        )
        self.assertIn('html[data-device="1"] #device-mode{display:none}', PAGE_SOURCE)
        # 机身与 iframe 都是 390×844：窄屏布局由页面原有媒体查询给出，不另写一套移动端样式。
        self.assertIn("#device-stage{position:absolute;top:0;left:0;width:390px;height:844px;", PAGE_SOURCE)
        self.assertIn("#device-stage iframe{display:block;width:390px;height:844px;", PAGE_SOURCE)
        # 打开时 iframe 的地址带 device=1，外层已有的参数照旧带过去：这条表达式用的是整条
        # location.search，所以外层带 token=xxx 进来时壳内那层也带着 token=xxx（不能退化成只取 pathname）。
        src_expr = PAGE_SOURCE.split("frame.setAttribute('src', ", 1)[1].split(");", 1)[0]
        self.assertEqual(
            src_expr,
            "location.pathname + location.search + (location.search ? '&' : '?') + 'device=1'",
        )
        self.assertIn('id="device-exit"', PAGE_SOURCE)
        self.assertIn("if (event.key === 'Escape' && !document.getElementById('device-shell').hidden)", PAGE_SOURCE)
        # 动效 200ms 以内，reduced-motion 下不播。
        self.assertIn("@media(prefers-reduced-motion:reduce){#device-frame-wrap,#device-stage{transition:none}}", PAGE_SOURCE)
        # 演示直链：?phone=1 进来就自动开壳，但壳内那层的 device=1 不能再套一台。
        self.assertIn(
            "if (/[?&]phone=1(&|$)/.test(location.search) && !/[?&]device=1(&|$)/.test(location.search)) "
            "nhDeviceMode(true);",
            PAGE_SOURCE,
        )
        # 退出时把 phone=1 从地址栏抹掉：刷新不会再自动弹壳，别的参数（nowiz、demo）留着。
        self.assertIn("params.delete('phone');", PAGE_SOURCE)
        self.assertIn(
            "history.replaceState(null, '', location.pathname + (rest ? '?' + rest : ''));",
            PAGE_SOURCE,
        )

    def test_brand_icons_use_single_green_palette(self) -> None:
        from noticehub.webapp import ICON_SVG

        for legacy in ("1B2A6B", "F59F00", "4f7cff", "8b5cf6", "3B5BDB"):
            self.assertNotIn(legacy, PAGE_SOURCE)
            self.assertNotIn(legacy, ICON_SVG)
        self.assertIn("%23173b34", PAGE_SOURCE)
        self.assertIn("#12695b", ICON_SVG)

    def test_task_correction_records_feedback(self) -> None:
        result = self._post(
            f"/api/tasks/{self.task_id}",
            {"action": "correct", "correction": "category", "value": "academic"},
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "correct")
        task = self._require_task(self.task_id)
        self.assertEqual(task["category"], "academic")
        events = self.store.list_task_events(self.task_id)
        self.assertIn("corrected", [item["event"] for item in events])


    def test_snooze_defaults_to_next_morning(self) -> None:
        result = self._post(f"/api/tasks/{self.task_id}", {"action": "snooze"})
        self.assertTrue(result["ok"])
        task = self._require_task(self.task_id)
        parsed = dt.datetime.fromisoformat(task["snooze_until"])
        self.assertEqual((parsed.hour, parsed.minute), (7, 30))
        self.assertEqual(parsed.date(), dt.date.today() + dt.timedelta(days=1))

    def test_duplicate_correction_merges_source_groups(self) -> None:
        copy_id = self.store.upsert_task(
            task_key="m2",
            groups=["班级闲聊群"],
            summary="提交实验报告",
            category="action",
        )
        self.assertTrue(self._post(f"/api/tasks/{copy_id}", {"action": "correct", "correction": "duplicate"})["ok"])
        original = self._require_task(self.task_id)
        self.assertIn("班级闲聊群", original["groups"])
        copy = self._require_task(copy_id)
        self.assertEqual(copy["duplicate_of"], self.task_id)
        self.assertEqual(copy["duplicate_summary"], "提交实验报告")

    def test_napcat_status_unreachable_returns_error_json(self) -> None:
        # running_pids 未打桩时会真去列进程（本机一次 ≈4.4 s），客户端 5 s 超时就会偶发变红。
        # 端口探测也打桩：不打桩时结果取决于本机有没有真的 NapCat 在跑，测试会跟机器走。
        with mock.patch("noticehub.webapp._Handler._napcat", return_value={"ok": False, "error": "connection refused"}), \
             mock.patch("noticehub.webapp.napcat_admin.running_pids", return_value=[]), \
             mock.patch("noticehub.webapp._napcat_port_accepts", return_value=False):
            data = self._get("/api/napcat/status", token="secret")
        self.assertFalse(data["ok"])
        self.assertIn("connection refused", data["error"])
        self.assertIn("qr_stamp", data)

    def test_napcat_port_probe_is_false_when_nothing_listens(self) -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            self.assertTrue(webapp._napcat_port_accepts("http://127.0.0.1:%d" % port))
        finally:
            listener.close()
        # 探测结果缓存 3 秒（状态栏一次请求会问两遍）。端口刚关掉属于「状态变了」，
        # 真实路径由启动/重启/安装动作调 _clear_napcat_live_cache() 失效，这里显式清一次。
        webapp._clear_napcat_live_cache()
        self.assertFalse(webapp._napcat_port_accepts("http://127.0.0.1:%d" % port))
        self.assertTrue(webapp._napcat_port_accepts("http://127.0.0.1"))  # 没端口可探就别拦

    def test_napcat_status_marks_running_no_api_when_other_engine_runs(self) -> None:
        """引擎在跑、接口没通：不能再给「未安装」那条路，要给出「帮我连上它」。"""
        boot = {"boot_exe": r"E:\NapCat\NapCatWinBootMain.exe", "data_dir": "E:\\NapCat"}
        with mock.patch("noticehub.webapp._Handler._napcat", return_value={"ok": False, "error": "连不上"}), \
             mock.patch("noticehub.webapp.napcat_admin.detect_boot", return_value=boot), \
             mock.patch("noticehub.webapp.napcat_admin.running_pids", return_value=[]), \
             mock.patch("noticehub.webapp.napcat_admin.all_napcat_pids", return_value=[4242]), \
             mock.patch("noticehub.webapp.napcat_admin.api_candidates",
                        return_value=["http://127.0.0.1:4001", "http://127.0.0.1:3001"]), \
             mock.patch("noticehub.webapp._napcat_port_accepts", return_value=False):
            data = self._get("/api/napcat/status", token="secret")
        self.assertFalse(data["ok"])
        self.assertEqual(data["stage"], "running_no_api")
        self.assertIs(data["can_connect"], True)
        self.assertEqual(data["connect_url"], "/api/napcat/connect")
        self.assertEqual(data["napcat_api_candidates"], ["http://127.0.0.1:4001", "http://127.0.0.1:3001"])
        self.assertIn("帮我连上它", data["error"])

    def test_napcat_connect_persists_discovered_url(self) -> None:
        env_path = Path(self.tmp.name) / ".env"
        env_path.write_text("QQ_DIGEST_TOKEN=abc\n", encoding="utf-8")
        self.settings.env_file = str(env_path)
        found = {
            "ok": True,
            "api_url": "http://127.0.0.1:4001",
            "tried": [{"url": "http://127.0.0.1:3001", "ok": False},
                      {"url": "http://127.0.0.1:4001", "ok": True}],
            "error": None,
        }
        with mock.patch("noticehub.webapp.napcat_admin.discover_api_url", return_value=found), \
             mock.patch("noticehub.webapp._Handler._napcat", return_value={"ok": False, "error": "连不上"}), \
             mock.patch("noticehub.webapp.napcat_admin.detect_boot", return_value=None):
            result = self._post("/api/napcat/connect", {})
        self.assertTrue(result["ok"])
        self.assertEqual(result["api_url"], "http://127.0.0.1:4001")
        self.assertTrue(result["persisted"])
        self.assertEqual(self.settings.napcat_api_url, "http://127.0.0.1:4001")
        self.assertIn("NOTICEHUB_NAPCAT_API_URL=http://127.0.0.1:4001", env_path.read_text(encoding="utf-8"))
        self.assertEqual(len(result["tried"]), 2, "试过哪些地址要如实带回来")

    def test_napcat_connect_clears_the_port_probe_cache(self) -> None:
        """连上之后必须让端口探测缓存失效：否则下一次状态还是旧端口。"""
        url = "http://127.0.0.1:4001"
        found = {"ok": True, "api_url": url, "tried": [{"url": url, "ok": True}], "error": None}
        env_path = Path(self.tmp.name) / ".env"
        env_path.write_text("QQ_DIGEST_TOKEN=abc\n", encoding="utf-8")
        self.settings.env_file = str(env_path)
        with mock.patch("noticehub.webapp._napcat_port_accepts_uncached", return_value=True) as probe:
            self.assertTrue(webapp._napcat_port_accepts(url))
            self.assertTrue(webapp._napcat_port_accepts(url))
            self.assertEqual(probe.call_count, 1, "3 秒内第二次应命中缓存")
            with mock.patch("noticehub.webapp.napcat_admin.discover_api_url", return_value=found), \
                 mock.patch("noticehub.webapp._Handler._napcat", return_value={"ok": False}), \
                 mock.patch("noticehub.webapp.napcat_admin.detect_boot", return_value=None):
                self.assertTrue(self._post("/api/napcat/connect", {})["ok"])
            webapp._napcat_port_accepts(url)
        self.assertEqual(probe.call_count, 2, "connect 之后缓存必须已失效，要重新探一次")

    def test_napcat_connect_warns_when_env_cannot_be_written(self) -> None:
        """.env 不存在时地址只在本次运行有效：必须让前端看到 persist_error，并留一条 warning。"""
        self.settings.env_file = str(Path(self.tmp.name) / "missing" / ".env")
        found = {"ok": True, "api_url": "http://127.0.0.1:4001",
                 "tried": [{"url": "http://127.0.0.1:4001", "ok": True}], "error": None}
        with mock.patch("noticehub.webapp.napcat_admin.discover_api_url", return_value=found), \
             mock.patch("noticehub.webapp._Handler._napcat", return_value={"ok": False}), \
             mock.patch("noticehub.webapp.napcat_admin.detect_boot", return_value=None), \
             self.assertLogs("noticehub.webapp", level="WARNING") as logs:
            result = self._post("/api/napcat/connect", {})
        self.assertTrue(result["ok"])
        self.assertIs(result["persisted"], False)
        self.assertTrue(result["persist_error"], "写不进去必须把原因给前端")
        self.assertTrue(any("写不回" in line for line in logs.output))

    def test_napcat_connect_failure_keeps_settings_and_offers_webui(self) -> None:
        env_path = Path(self.tmp.name) / ".env"
        env_path.write_text("NOTICEHUB_NAPCAT_API_URL=http://127.0.0.1:3001\n", encoding="utf-8")
        self.settings.env_file = str(env_path)
        found = {"ok": False, "api_url": "", "tried": [{"url": "http://127.0.0.1:3001", "ok": False}],
                 "error": "NapCat 的 OneBot HTTP 服务没在监听"}
        with mock.patch("noticehub.webapp.napcat_admin.discover_api_url", return_value=found), \
             mock.patch("noticehub.webapp.napcat_admin.webui_url",
                        return_value="http://127.0.0.1:6099"):
            result = self._post("/api/napcat/connect", {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "running_no_api")
        self.assertEqual(result["webui_url"], "http://127.0.0.1:6099")
        self.assertEqual(self.settings.napcat_api_url, "http://127.0.0.1:3001")
        self.assertIn("NOTICEHUB_NAPCAT_API_URL=http://127.0.0.1:3001", env_path.read_text(encoding="utf-8"))

    def test_napcat_status_carries_qr_stamp(self) -> None:
        status = {"ok": True, "data": {"online": True, "good": True, "user_id": 7}}
        login = {"ok": True, "data": {"user_id": 7, "nickname": "me"}}

        def fake(action):
            return status if action == "get_status" else login

        with mock.patch("noticehub.webapp._Handler._napcat", side_effect=fake), mock.patch("noticehub.webapp.napcat_admin.qr_stamp", return_value="111-222"), mock.patch("noticehub.webapp.napcat_admin.detect_boot", return_value=None):
            data = self._get("/api/napcat/status", token="secret")
        self.assertTrue(data["ok"])
        self.assertTrue(data["online"])
        self.assertEqual(data["qr_stamp"], "111-222")

    def _napcat_status_with_qr_file(self, qr_path: Path) -> dict:
        status = {"ok": True, "data": {"online": True, "good": True, "user_id": 7}}
        login = {"ok": True, "data": {"user_id": 7, "nickname": "me"}}

        def fake(action):
            return status if action == "get_status" else login

        with mock.patch("noticehub.webapp._Handler._napcat", side_effect=fake), \
             mock.patch("noticehub.webapp.napcat_admin._qr_file", return_value=qr_path), \
             mock.patch("noticehub.webapp.napcat_admin.detect_boot", return_value=None):
            return self._get("/api/napcat/status", token="secret")

    def test_napcat_status_qr_is_not_stale_right_after_a_new_code(self) -> None:
        """刚换的码（mtime 在 120 秒内）：qr_stale=False，qr_age_seconds 是正常小数。"""
        with tempfile.TemporaryDirectory() as tmp:
            qr = Path(tmp) / "qrcode.png"
            qr.write_bytes(b"png")
            data = self._napcat_status_with_qr_file(qr)
        self.assertFalse(data["qr_stale"])
        self.assertIsNotNone(data["qr_age_seconds"])
        self.assertLess(data["qr_age_seconds"], webapp.napcat_admin.QR_STALE_SECONDS)

    def test_napcat_status_qr_is_stale_when_the_file_stops_changing(self) -> None:
        """同一张图超过换码周期没被重写：页面必须能看出它已经过期。"""
        with tempfile.TemporaryDirectory() as tmp:
            qr = Path(tmp) / "qrcode.png"
            qr.write_bytes(b"png")
            old = time.time() - (webapp.napcat_admin.QR_STALE_SECONDS + 60)
            os.utime(qr, (old, old))
            data = self._napcat_status_with_qr_file(qr)
        self.assertTrue(data["qr_stale"])
        self.assertGreater(data["qr_age_seconds"], webapp.napcat_admin.QR_STALE_SECONDS)

    def test_napcat_status_qr_without_file_is_not_stale(self) -> None:
        """还没有二维码：不算过期（没码不等于旧码），age 为 None。"""
        with tempfile.TemporaryDirectory() as tmp:
            data = self._napcat_status_with_qr_file(Path(tmp) / "missing.png")
        self.assertFalse(data["qr_stale"])
        self.assertIsNone(data["qr_age_seconds"])

    def test_napcat_status_reports_a_running_engine_while_waiting_for_login(self) -> None:
        """端口要等扫码登录成功才监听，所以「连不上」未必是故障。

        引擎进程还在时，状态里要带 napcat_running，页面才能说「正在等你扫码」，
        而不是让用户以为坏了、去反复点「启动并登录」（那正是二维码风暴的起因）。
        """
        with mock.patch("noticehub.webapp._Handler._napcat", return_value={"ok": False, "error": "NapCat 没在运行（连不上 http://127.0.0.1:3001）"}), \
             mock.patch("noticehub.webapp.napcat_admin.qr_stamp", return_value="111-222"), \
             mock.patch("noticehub.webapp.napcat_admin.detect_boot", return_value=None), \
             mock.patch("noticehub.webapp.napcat_admin.running_pids", return_value=[4242]) as pids, \
             mock.patch("noticehub.webapp._napcat_port_accepts", return_value=False):
            data = self._get("/api/napcat/status", token="secret")
        pids.assert_called_once()
        self.assertFalse(data["ok"])
        self.assertTrue(data["napcat_running"])
        self.assertEqual(data["qr_stamp"], "111-222")
        self.assertEqual(data["stage"], "waiting_qr")
        self.assertIn("var waiting = !!(e && e.napcatRunning);", PAGE_SOURCE)
        self.assertIn("NapCat 正在等你用手机扫码登录", PAGE_SOURCE)
        self.assertIn("先扫码登录 QQ，登录成功后会自动读取群列表。", PAGE_SOURCE)

    def test_napcat_status_reports_no_engine_when_none_is_running(self) -> None:
        with mock.patch("noticehub.webapp._Handler._napcat", return_value={"ok": False, "error": "NapCat 没在运行"}), \
             mock.patch("noticehub.webapp.napcat_admin.qr_stamp", return_value=""), \
             mock.patch("noticehub.webapp.napcat_admin.detect_boot", return_value=None), \
             mock.patch("noticehub.webapp.napcat_admin.running_pids", return_value=[]), \
             mock.patch("noticehub.webapp._napcat_port_accepts", return_value=False):
            data = self._get("/api/napcat/status", token="secret")
        self.assertFalse(data["ok"])
        self.assertFalse(data["napcat_running"])
        self.assertEqual(data["stage"], "not_installed")

    def test_napcat_status_reports_stopped_when_engine_installed_but_not_running(self) -> None:
        with mock.patch("noticehub.webapp._Handler._napcat", return_value={"ok": False, "error": "连不上"}), \
             mock.patch("noticehub.webapp.napcat_admin.qr_stamp", return_value=""), \
             mock.patch("noticehub.webapp.napcat_admin.detect_boot", return_value={"data_dir": "C:/napcat", "boot_exe": "C:/napcat/boot.exe"}), \
             mock.patch("noticehub.webapp.napcat_admin.running_pids", return_value=[]), \
             mock.patch("noticehub.webapp.napcat_admin.all_napcat_pids", return_value=[]), \
             mock.patch("noticehub.webapp._napcat_port_accepts", return_value=False):
            data = self._get("/api/napcat/status", token="secret")
        self.assertEqual(data["stage"], "stopped")
        self.assertIn("刚点过启动就等十几秒再刷新", data["error"])

    def test_napcat_status_explains_a_hand_started_instance(self) -> None:
        """issue #4 / beta.16：用户自己在 CMD 起 NapCat 并登录成功，本程序连不上 3001。

        以前这里报 ``stopped``，卡片就给出「自动下载并安装 NapCat」——引擎明明在跑，
        等于指导用户再装一遍。语义上也不对：引擎没停，只是 OneBot 接口没通。
        现在报 ``running_no_api``：主按钮换成「帮我连上它」，程序自己去读配置找端口；
        也不再让用户改 .env（beta.14 就是那句提示，普通用户看不懂）。
        """
        with mock.patch("noticehub.webapp._Handler._napcat", return_value={"ok": False, "error": "连不上"}), \
             mock.patch("noticehub.webapp.napcat_admin.qr_stamp", return_value=""), \
             mock.patch("noticehub.webapp.napcat_admin.detect_boot", return_value={"data_dir": "C:/napcat", "boot_exe": "C:/napcat/boot.exe"}), \
             mock.patch("noticehub.webapp.napcat_admin.running_pids", return_value=[]), \
             mock.patch("noticehub.webapp.napcat_admin.all_napcat_pids", return_value=[9090]), \
             mock.patch("noticehub.webapp._napcat_port_accepts", return_value=False):
            data = self._get("/api/napcat/status", token="secret")
        self.assertEqual(data["stage"], "running_no_api")
        self.assertIs(data["can_connect"], True)
        self.assertEqual(data["connect_url"], "/api/napcat/connect")
        self.assertIn("手动启动", data["error"])
        self.assertIn("帮我连上它", data["error"])
        self.assertNotIn("QQ_DIGEST_NAPCAT_API_URL", data["error"], "不许再让普通用户去改 .env")

    def test_napcat_status_trusts_the_port_over_running_pids(self) -> None:
        """running_pids 有假阴性；端口有监听就说明引擎活着、接口还不通＝配置问题，不能判 stopped/waiting_qr。"""
        with mock.patch("noticehub.webapp._Handler._napcat", return_value={"ok": False, "error": "连不上"}), \
             mock.patch("noticehub.webapp.napcat_admin.qr_stamp", return_value=""), \
             mock.patch("noticehub.webapp.napcat_admin.detect_boot", return_value={"data_dir": "C:/napcat", "boot_exe": "C:/napcat/boot.exe"}), \
             mock.patch("noticehub.webapp.napcat_admin.running_pids", return_value=[]), \
             mock.patch("noticehub.webapp._napcat_port_accepts", return_value=True):
            data = self._get("/api/napcat/status", token="secret")
        self.assertEqual(data["stage"], "error")
        self.assertFalse(data["napcat_running"])

    def test_napcat_status_reports_error_when_api_fails_despite_open_port(self) -> None:
        with mock.patch("noticehub.webapp._Handler._napcat", return_value={"ok": False, "error": "token 无效"}), \
             mock.patch("noticehub.webapp.napcat_admin.qr_stamp", return_value=""), \
             mock.patch("noticehub.webapp.napcat_admin.detect_boot", return_value=None), \
             mock.patch("noticehub.webapp.napcat_admin.running_pids", return_value=[4242]), \
             mock.patch("noticehub.webapp._napcat_port_accepts", return_value=True):
            data = self._get("/api/napcat/status", token="secret")
        self.assertEqual(data["stage"], "error")
        self.assertIn("token 无效", data["error"])

    def test_napcat_status_online_stage_ignores_waiting_seconds(self) -> None:
        status = {"ok": True, "data": {"online": True, "good": True, "user_id": 7}}
        login = {"ok": True, "data": {"user_id": 7, "nickname": "me"}}

        def fake(action):
            return status if action == "get_status" else login

        with mock.patch("noticehub.webapp._Handler._napcat", side_effect=fake), \
             mock.patch("noticehub.webapp.napcat_admin.qr_stamp", return_value=""), \
             mock.patch("noticehub.webapp.napcat_admin.detect_boot", return_value=None):
            data = self._get("/api/napcat/status", token="secret")
        self.assertEqual(data["stage"], "online")
        self.assertEqual(data["waiting_seconds"], 0.0)

    def test_napcat_wait_error_wording_distinguishes_qr_seen_from_not_yet(self) -> None:
        """端口没通时的提示要分清「刚扫过码、等手机确认」和「码还没出来」。

        两句都不能再说「用手机扫下面的二维码」：码可能根本不是这个原因不出来。
        """
        base = {"ok": False, "error": "NapCat 没在运行"}
        cases = (
            ("111-222", "已经收到二维码"),
            ("", "还没生成二维码"),
        )
        for stamp, expected in cases:
            with mock.patch("noticehub.webapp._Handler._napcat", return_value=base), \
                 mock.patch("noticehub.webapp.napcat_admin.qr_stamp", return_value=stamp), \
                 mock.patch("noticehub.webapp.napcat_admin.detect_boot", return_value=None), \
                 mock.patch("noticehub.webapp.napcat_admin.running_pids", return_value=[4242]), \
                 mock.patch("noticehub.webapp._napcat_port_accepts", return_value=False):
                data = self._get("/api/napcat/status", token="secret")
            self.assertEqual(data["stage"], "waiting_qr")
            self.assertIn(expected, data["error"])
            self.assertNotIn("用手机扫下面的二维码", data["error"])

    def test_napcat_waiting_seconds_grows_until_the_stage_changes(self) -> None:
        """「已等待 N 秒」的起点存在 server 上：每请求重算成 0 的话页面就永远是 0 秒。"""
        fake_server = type("S", (), {})()
        with mock.patch("noticehub.webapp.time.monotonic", side_effect=[100.0, 103.5]):
            self.assertEqual(webapp._napcat_wait_seconds(fake_server, "waiting_qr", "111-222"), 0.0)
            self.assertEqual(webapp._napcat_wait_seconds(fake_server, "waiting_qr", "111-222"), 3.5)
        # 二维码换了一张：重新起算
        with mock.patch("noticehub.webapp.time.monotonic", side_effect=[200.0]):
            self.assertEqual(webapp._napcat_wait_seconds(fake_server, "waiting_qr", "333-444"), 0.0)

    def test_reply_swallows_a_client_that_went_away(self) -> None:
        """客户端中途刷新/关页面时 write 抛 10053，不能再让它冒出去。

        以前每个写接口都自己拼一遍响应头，异常会冒到路由的 except 里、对着同一条断开的
        连接再写一次、再抛一次 —— 日志里那串 WinError 10053 就是这么来的。
        """

        class FakeWfile:
            def __init__(self, error=None):
                self.error = error
                self.written = None

            def write(self, raw):
                if self.error is not None:
                    raise self.error
                self.written = raw

        class FakeHandler:
            def __init__(self, error=None):
                self.wfile = FakeWfile(error)
                self.headers = []
                self.close_connection = False

            def send_response(self, status):
                self.status = status

            def send_header(self, name, value):
                self.headers.append((name, value))

            def end_headers(self):
                self.ended = True

        # 正常路径：写出体，并且带上长度和缓存头
        handler = FakeHandler()
        webapp._Handler._reply(handler, 200, b"{}", "application/json; charset=utf-8", "no-store")
        self.assertEqual(handler.wfile.written, b"{}")
        self.assertIn(("Content-Length", "2"), handler.headers)
        self.assertIn(("Cache-Control", "no-store"), handler.headers)
        self.assertFalse(handler.close_connection)

        # 断开路径：不抛，并且标记这条连接该关了
        gone = FakeHandler(ConnectionAbortedError(10053, "你的主机中的软件中止了一个已建立的连接。"))
        webapp._Handler._reply(gone, 200, b"{}", "application/json; charset=utf-8", "no-store")
        self.assertTrue(gone.close_connection)

        # send_response 自己就抛（没写出任何体）时同样不能冒出去
        class ExplodingHandler(FakeHandler):
            def send_response(self, status):
                raise BrokenPipeError(32, "Broken pipe")

        exploded = ExplodingHandler()
        webapp._Handler._reply(exploded, 200, b"{}", "application/json; charset=utf-8", "no-store")
        self.assertTrue(exploded.close_connection)

    def test_napcat_groups_maps_group_name(self) -> None:
        result = {"ok": True, "data": [{"group_id": 123, "group_name": "通知群", "member_count": 8}]}
        with mock.patch("noticehub.webapp._Handler._napcat", return_value=result):
            data = self._get("/api/napcat/groups", token="secret")
        self.assertTrue(data["ok"])
        self.assertEqual(data["groups"][0]["group_id"], "123")
        self.assertEqual(data["groups"][0]["name"], "通知群")

    def test_subscriptions_only_rewrite_target_env_lines(self) -> None:
        env_path = Path(self.tmp.name) / ".env"
        original = b"OTHER=keep\r\nQQ_DIGEST_GROUPS=old\nQQ_DIGEST_GROUP_ALIASES=old-name\r\nTAIL=\xe4\xb8\xad\n"
        env_path.write_bytes(original)
        self.server.settings.env_file = env_path
        payload = json.dumps({"groups": ["123"], "aliases": {"123": "通知群"}}).encode("utf-8")
        request = urllib.request.Request(f"{self.base}/api/subscriptions", data=payload, headers={"X-Token": "secret", "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
        self.assertTrue(data["applied"])
        expected = b"OTHER=keep\r\nNOTICEHUB_GROUPS=123\nNOTICEHUB_GROUP_ALIASES=123=" + "通知群".encode("utf-8") + b"\r\nTAIL=\xe4\xb8\xad\n"
        self.assertEqual(env_path.read_bytes(), expected)
        self.assertEqual(self.server.settings.group_whitelist, ("123",))
        self.assertEqual(self.server.settings.group_aliases, {"123": "通知群"})

    def test_subscriptions_append_missing_env_keys_instead_of_failing(self) -> None:
        """测试者报的「保存失败：required .env keys missing」。

        全新安装的 .env 由 launcher.ENV_TEMPLATE 生成，里面根本没有 QQ_DIGEST_GROUPS /
        QQ_DIGEST_GROUP_ALIASES。老代码是手写的一份 env 改写逻辑，缺键就回 400，于是
        任何人第一次装完保存订阅都必定失败。缺键必须追加到文件末尾。
        """
        env_path = Path(self.tmp.name) / ".env"
        env_path.write_bytes(b"QQ_DIGEST_WEB=1\nQQ_DIGEST_WEB_PORT=8766\n")
        self.server.settings.env_file = env_path
        payload = json.dumps({"groups": ["111", "222"], "aliases": {"111": "通知群"}}).encode("utf-8")
        request = urllib.request.Request(f"{self.base}/api/subscriptions", data=payload, headers={"X-Token": "secret", "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
        self.assertTrue(data["applied"])
        written = env_path.read_bytes()
        # 原来那两行必须逐字节保留，缺的两行追加在末尾
        self.assertTrue(written.startswith(b"QQ_DIGEST_WEB=1\nQQ_DIGEST_WEB_PORT=8766\n"))
        self.assertIn(b"NOTICEHUB_GROUPS=111,222", written)
        self.assertIn("NOTICEHUB_GROUP_ALIASES=111=通知群".encode("utf-8"), written)
        self.assertNotIn(b"required .env keys missing", written)
        self.assertEqual(self.server.settings.group_whitelist, ("111", "222"))
        self.assertEqual(self.server.settings.group_aliases, {"111": "通知群"})

    def test_env_template_predefines_the_subscription_keys(self) -> None:
        """模板里预置这两行，新装实例就不会再走到「缺键」那条路上。"""
        template = (PROJECT_ROOT / "packaging" / "launcher.py").read_text(encoding="utf-8")
        self.assertIn('"QQ_DIGEST_GROUPS=\\n"', template)
        self.assertIn('"QQ_DIGEST_GROUP_ALIASES=\\n"', template)

    def test_env_template_does_not_default_ai_to_a_local_model(self) -> None:
        """首装不能假装本机有 Ollama：AI 默认关，由设置页引导用户自己选一家。

        模板以前写死 QQ_DIGEST_LLM_MODEL=qwen3.6:35b-a3b-q4_k_m-gpu20（开发机上的本地模型名）、
        QQ_DIGEST_LLM_ENDPOINT=http://127.0.0.1:11434/...、DASHSCOPE_API_KEY=ollama-local。
        别人机器上没有 Ollama，于是 AI 摘要静默失效，用户还以为开着 —— 这就是
        「AI 摘要默认落本地模型」。默认必须是「不接」+ 留空，等用户在设置页选。
        """
        template = (PROJECT_ROOT / "packaging" / "launcher.py").read_text(encoding="utf-8")
        self.assertIn('"QQ_DIGEST_LLM=0\\n"', template)
        self.assertIn('"QQ_DIGEST_LLM_API_KEY=\\n"', template)
        self.assertNotIn("11434", template)
        self.assertNotIn("ollama-local", template)
        self.assertNotIn("qwen3.6:35b-a3b", template)

    def test_qrcode_image_request_carries_auth_token_and_handles_failure(self) -> None:
        request = urllib.request.Request(f"{self.base}/setup?token=secret")
        with urllib.request.urlopen(request, timeout=5) as response:
            page = response.read().decode("utf-8")
        self.assertIn('id="qr"', page)
        # 二维码必须带 token：不带 → 401；带了 → 必须过鉴权闸
        # （NapCat 没跑时 404 是合法回答，不锁 200）。
        # 旧断言逐字符比对 js 源码，前端挪个空格就全红。
        status, _, _ = self._raw_request("/api/napcat/qrcode", method="GET", token="")
        self.assertEqual(status, 401)
        status, _, _ = self._raw_request("/api/napcat/qrcode?token=secret", method="GET", token="")
        self.assertNotEqual(status, 401)
        # 页面侧：拼二维码地址时必须带 token 参数（只锁拼接，不锁函数体写法）
        self.assertRegex(PAGE_SOURCE, r"image\.src\s*=\s*[^;]{0,200}?/api/napcat/qrcode\?token=")
        self.assertRegex(PAGE_SOURCE, r"image\.onload\s*=\s*function")
        self.assertRegex(PAGE_SOURCE, r"image\.onerror\s*=\s*function")
        # 失败时藏图 + 展开「登录二维码」折叠区：锁顺序与关键词，不锁整行
        self.assertRegex(PAGE_SOURCE, r"image\.onerror\s*=\s*function[\s\S]{0,300}?image\.hidden\s*=\s*true")
        self.assertRegex(PAGE_SOURCE, r"image\.onerror\s*=\s*function[\s\S]{0,400}?qq-qr-details")
        self.assertIn("二维码暂时取不到", PAGE_SOURCE)
        self.assertIn("点上面的「重启引擎」再来一次", PAGE_SOURCE)
        self.assertNotIn("二维码暂不可用", PAGE_SOURCE)
        # 旧的单点「启动 NapCat 并获取二维码」按钮删掉了：七个按钮收成状态驱动的单主按钮。
        self.assertNotIn('id="qr-start"', page)
        self.assertNotIn('id="qr-start-note"', page)
        qr_start = page.index('<details id="qq-qr-details"')
        qr_end = page.index("</details>", qr_start)
        connect_actions = page.index('class="connect-actions"')
        next_step = page.index('id="qq-next-step"')
        self.assertIn('<details id="qq-qr-details" open><summary>登录二维码</summary>', page[qr_start:qr_end])
        self.assertLess(connect_actions, next_step)
        self.assertLess(next_step, qr_start)
        setup_start = PAGE_SCRIPT.index("function checkSetup()")
        setup_script = PAGE_SCRIPT[setup_start:PAGE_SCRIPT.index("function groupCategoryName", setup_start)]
        self.assertIn("qrDetails.open=!ok;", setup_script)
        self.assertIn("if(qrDetails)qrDetails.open=true;", setup_script)
        self.assertIn("catch(function(e){var qrDetails=document.getElementById('qq-qr-details');if(qrDetails)qrDetails.open=true;", setup_script)
        # 三条触发路径（一键接入 / 启动 / 重启）都必须接同一个「动作后显示二维码」入口
        self.assertRegex(PAGE_SOURCE, r"function\s+showQrAfterAction\s*\(")
        for start_marker, end_marker in (
            ("function runAutoSetup", "function run("),
            ("function run(", "function runRestart"),
            ("function runRestart", "function initOnboarding"),
        ):
            start = PAGE_SOURCE.index(start_marker)
            end = PAGE_SOURCE.index(end_marker, start + len(start_marker))
            self.assertIn("showQrAfterAction", PAGE_SOURCE[start:end], start_marker)
        # 入口内部：展开 → 置状态 → 刷新 → 滚到可见（block 判据锁在共用 helper 的 harness 用例里）
        qr_start = PAGE_SOURCE.index("function showQrAfterAction")
        qr_body = PAGE_SOURCE[qr_start:PAGE_SOURCE.index("function ", qr_start + 10)]
        self.assertIn("applyNapcatQrState(true)", qr_body)
        self.assertIn("refreshQr()", qr_body)
        self.assertRegex(qr_body, r"scrollTargetIntoView\s*\(")

    def test_group_list_loading_is_connection_gated_and_reports_failure(self) -> None:
        request = urllib.request.Request(f"{self.base}/setup?token=secret")
        with urllib.request.urlopen(request, timeout=5) as response:
            page = response.read().decode("utf-8")
        check_start = PAGE_SCRIPT.index("function checkSetup()")
        check_setup = PAGE_SCRIPT[check_start:PAGE_SCRIPT.index("function groupCategoryName", check_start)]
        groups_start = PAGE_SCRIPT.index("function loadGroups()")
        group_loader = PAGE_SCRIPT[groups_start:PAGE_SCRIPT.index("function run()", groups_start)]
        # 拒绝响应时要把 napcat_running 带在错误上抛出去：catch 靠它区分「引擎没跑」
        # 和「引擎在跑、只是还在等扫码」——后者不该报成故障。beta.11 起还要带上
        # stage 与 waiting_seconds，页面才能显示「已等 N 秒」而不是一句恒定文案。
        # 不锁整行拼接：只要四件事都在（筛选不会因前端调格式而红）
        self.assertRegex(check_setup, r"notReady\.napcatRunning\s*=\s*!!x\.napcat_running")
        self.assertRegex(check_setup, r"notReady\.stage\s*=\s*x\.stage\s*\|\|\s*''")
        self.assertRegex(check_setup, r"notReady\.waitingSeconds\s*=\s*Number\(x\.waiting_seconds\s*\|\|\s*0\)")
        self.assertIn("throw notReady;", check_setup)
        self.assertIn("连接检查失败：", check_setup)
        self.assertIn("群列表暂不可用：", check_setup)
        self.assertIn("loadGroups();", check_setup)
        self.assertIn("qrWasVisible = true;", check_setup)
        engine_note = check_setup.index("typeof x.napcat_installed!=='undefined'")
        rejected_response = check_setup.index("if(!x.ok){var notReady=Error(")
        self.assertLess(engine_note, rejected_response)
        # 引擎检查行 + 状态按钮矩阵都归 applyQQConnectState：checkSetup 只负责分发状态。
        self.assertIn("applyQQConnectState(engineState", check_setup)
        self.assertNotIn('document.getElementById(\'napcat-engine\')', check_setup)
        # 第五态必须是显式派发：incoming stage 是 running_no_api 时不许落到 running_need_login / installed_not_running，
        # 否则页面上没有「帮我连上它」这个唯一能点的动作（死界面）。
        self.assertIn("applyQQConnectState(eStage === 'running_no_api' ? 'running_no_api'", check_setup)
        # 原有四态的兜底分支不能被第五态吃掉。
        self.assertIn("(waiting ? 'running_need_login' : ((eStage === 'not_installed'", check_setup)
        # 失败态不许再去拉 doctor 覆盖现场（那条 hint 是讲登录的，这个状态下不对）。
        self.assertIn("if(waiting && eStage !== 'running_no_api'){fetch('/api/napcat/doctor'", check_setup)
        self.assertNotIn("这只是「文件装好了」", check_setup)
        self.assertIn("QQ_DIGEST_NAPCAT_DIR", PAGE_SOURCE)
        self.assertIn("if(!r.ok)throw Error", check_setup)
        self.assertNotIn("install-box').hidden", check_setup)
        self.assertRegex(group_loader, r"if\s*\(\s*!\w+\.ok\s*\)\s*throw\s+Error\(")
        self.assertRegex(group_loader, r"\.catch\(function\(e\)\{[^}]*message\.textContent='群列表读取失败：'\+e\.message")
        self.assertIn("groups-connect-link", group_loader)
        self.assertNotRegex(PAGE_SOURCE, r"setInterval\( *loadGroups")

    def test_setup_redirects_home_preserving_token(self) -> None:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        request = urllib.request.Request(f"{self.base}/setup?token=secret")
        opener = urllib.request.build_opener(NoRedirect)
        with self.assertRaises(urllib.error.HTTPError) as context:
            opener.open(request, timeout=5)
        self.assertEqual(context.exception.code, 302)
        self.assertEqual(context.exception.headers.get("Location"), "/?token=secret")
        context.exception.close()

    def test_subscription_save_reports_server_failure(self) -> None:
        request = urllib.request.Request(f"{self.base}/setup?token=secret")
        with urllib.request.urlopen(request, timeout=5) as response:
            page = response.read().decode("utf-8")
        save_start = page.index("document.getElementById('save').onclick")
        save_path = page[save_start:page.index("var KEY", save_start)]
        self.assertIn("setupApi('/api/subscriptions'", save_path)
        self.assertIn("!result.applied", save_path)
        self.assertIn(".catch(function(e)", save_path)
        self.assertIn("保存失败：", save_path)

    def test_login_action_is_bound_and_calls_launch_with_progress(self) -> None:
        request = urllib.request.Request(f"{self.base}/setup?token=secret")
        with urllib.request.urlopen(request, timeout=5) as response:
            page = response.read().decode("utf-8")
        self.assertRegex(page, r'<button id="go"[^>]*aria-label="启动 NapCat 并登录"')
        run_start = PAGE_SCRIPT.index("function run()")
        run_end = PAGE_SCRIPT.index("function runRestart()", run_start)
        run_path = PAGE_SCRIPT[run_start:run_end]
        self.assertIn("setupApi('/api/napcat/launch'", run_path)
        self.assertIn("正在启动 NapCat", run_path)
        self.assertIn("启动失败：", run_path)
        self.assertIn("document.getElementById('go').onclick=run", page)

    def test_napcat_launch_passes_uin_and_empty_as_none(self) -> None:
        expected = {"ok": True, "already_running": False, "pid": 12, "command": [], "profile_dir": "x", "boot": {}, "error": None}
        fake = mock.Mock()
        fake.launch.return_value = expected
        with mock.patch("noticehub.webapp.napcat_admin", fake):
            for value, expected_uin in (("123456", "123456"), ("", None)):
                payload = json.dumps({"uin": value}).encode("utf-8")
                request = urllib.request.Request(f"{self.base}/api/napcat/launch", data=payload, headers={"X-Token": "secret", "Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(json.loads(response.read().decode("utf-8")), expected)
                self.assertEqual(fake.launch.call_args.kwargs["uin"], expected_uin)

    def test_napcat_launch_requires_token(self) -> None:
        request = urllib.request.Request(f"{self.base}/api/napcat/launch", data=b'{"uin":"1"}', method="POST")
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(context.exception.code, 401)

    def test_napcat_launch_exception_returns_json(self) -> None:
        fake = mock.Mock()
        fake.launch.side_effect = RuntimeError("not found")
        with mock.patch("noticehub.webapp.napcat_admin", fake):
            request = urllib.request.Request(f"{self.base}/api/napcat/launch", data=b'{"uin":"1"}', headers={"X-Token": "secret", "Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "not found")

    def _post_raw(self, path: str, data: bytes) -> int:
        """用真实 HTTP 发一个指定原始 body 的 POST，返回状态码（4xx/5xx 也正常返回）。"""
        request = urllib.request.Request(
            self.base + path, data=data, method="POST",
            headers={"X-Token": "secret", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status
        except urllib.error.HTTPError as error:
            with error:
                return error.code

    def test_oversized_body_is_rejected(self) -> None:
        # 超过 MAX_BODY_BYTES(64KB) 的 body 必须被拒，且服务不崩（后续请求仍可用）。
        # 两种请求各配一个「同一份内容但没超限」的孪生体：差异只有长度，
        # 所以拒绝只可能来自上限，不能来自 payload 本身不合法。
        # /api/settings 的成功路径要写 .env，给它一份临时 .env（否则小 body 也返回 400，
        # 就分不清 400 是「超限」还是「写不了 .env」）。
        env_path = Path(self.tmp.name) / ".env"
        env_path.write_text("QQ_DIGEST_CATCHUP_HOURS=24\n", encoding="utf-8")
        self.server.settings.env_file = env_path
        cases = (
            ("/api/settings", ('{"catchup_enabled": true, "catchup_hours": 72, "padding": "%s"}' % ("a" * (64 * 1024))).encode(),
             b'{"catchup_enabled": true, "catchup_hours": 72}'),
            ("/api/hosting/start", ('{"uin":"%s"}' % ("1" * (64 * 1024))).encode(),
             b'{"uin":"123"}'),
        )
        for path, oversized, small in cases:
            with self.subTest(path=path):
                self.assertGreater(len(oversized), 64 * 1024)
                self.assertEqual(self._post_raw(path, small), 200)
                self.assertEqual(self._post_raw(path, oversized), 400)
        # 被拒之后服务仍活着：再发一个正常请求，必须照常应答。
        self.assertEqual(self._post_raw("/api/hosting/start", b'{"uin":"123"}'), 200)

    def test_schedule_image_body_may_exceed_the_global_cap(self) -> None:
        # 课表截图必须整张图进请求：8 MB 的图 base64 之后约 11 MB，比全局 64 KB 上限大两个数量级。
        # 这条路由单独放宽，其余接口不许跟着松——同一个大 body 发到 /api/tasks 仍然要 400。
        big = ('{"text": "周一 08:00-09:40 高等数学 立功楼302", "padding": "%s"}'
               % ("a" * (100 * 1024))).encode()
        self.assertGreater(len(big), 64 * 1024)
        with mock.patch.dict(os.environ, {"NOTICEHUB_AGENT_SHELL": "1"}):
            status, raw = self._post_raw_json("/api/agent/schedule", big)
        self.assertEqual(status, 200, raw[:200])
        self.assertEqual(json.loads(raw)["lessons"], 1)
        self.assertEqual(self._post_raw("/api/tasks/%s" % self.task_id, big), 400)

    def _post_raw_json(self, path: str, data: bytes, token: str = "secret") -> tuple[int, bytes]:
        """发原始 body，返回 (状态码, 响应体)。dict 序列化表达不了 [1,2,3]/null 这类合法非对象 JSON。"""
        request = urllib.request.Request(
            self.base + path, data=data, method="POST",
            headers={"X-Token": token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read()

    def test_non_object_task_body_returns_400_and_service_survives(self) -> None:
        # 合法 JSON 但不是对象：以前 payload.get() 直接 AttributeError，连接被摔掉。
        for body in (b"[1,2,3]", b"null", b'"hello"'):
            with self.subTest(body=body):
                status, raw = self._post_raw_json(f"/api/tasks/{self.task_id}", body)
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(raw.decode("utf-8"))["ok"], False)
        # 处理线程没炸：后续正常请求照常应答。
        self.assertEqual(self._post("/api/tasks/%d" % self.task_id, {"action": "confirm"})["ok"], True)
        request = urllib.request.Request(self.base + "/api/tasks", headers={"X-Token": "secret"})
        with self._open(request) as response:
            self.assertEqual(response.status, 200)

    def test_onboarding_rejects_non_object_body(self) -> None:
        # 前端固定发 '{}'；数组/字符串/null 之类没有意义，必须 400 而不是断连。
        for body in (b"[1,2,3]", b"null", b'"hello"'):
            with self.subTest(body=body):
                status, raw = self._post_raw_json("/api/onboarding/accept", body, token="")
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(raw.decode("utf-8"))["ok"], False)
        self.assertTrue(self._post("/api/onboarding/accept", {}, token="")["ok"])

    def test_settings_unknown_key_returns_parseable_json(self) -> None:
        status, _headers, raw = self._raw_request("/api/settings", "POST", {"unknown_key_xyz": 1})
        self.assertEqual(status, 400)
        body = json.loads(raw.decode("utf-8"))
        self.assertEqual(body["ok"], False)
        self.assertTrue(body["error"])
        # 空体走 400 时同样必须是可解析的 JSON 错误体，不能是空字符串。
        status, raw = self._post_raw_json("/api/settings", b"")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(raw.decode("utf-8"))["ok"], False)

    def test_oversized_declared_body_is_refused_and_connection_closed(self) -> None:
        # 裸 socket 实测（不能用 urllib：它一定把 body 发全，那样测不出「未读完就回应」）。
        # 声称 200KB 但只发 256 字节：服务端若先回应再关连接，就不会有 RST/截断。
        host, port = self.base.split("//", 1)[1].split(":", 1)
        sock = socket.create_connection((host, int(port)), timeout=5)
        self.addCleanup(sock.close)
        sock.sendall(
            b"POST /api/settings HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"X-Token: secret\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 204800\r\n\r\n" + b'{"x":"' + b"a" * 240 + b'"'
        )
        sock.settimeout(5)
        received = b""
        while True:
            try:
                chunk = sock.recv(65536)
            except ConnectionResetError:
                break  # 对端带未读数据关闭 → RST，属于「没把 body 吞掉」的证据
            if not chunk:
                break
            received += chunk
        self.assertIn(b"400", received.split(b"\r\n", 1)[0])
        # 服务仍能正常响应，且是另一个连接上的正常请求。
        self.assertEqual(self._post_raw("/api/hosting/start", b'{"uin":"123"}'), 200)

    def test_empty_body_keeps_existing_semantics(self) -> None:
        # 补上限不能顺手改语义：空 body 在 /api/settings 仍是 400，在 hosting 仍是 {}。
        self.assertEqual(self._post_raw("/api/settings", b""), 400)
        self.assertEqual(self._post_raw("/api/hosting/start", b""), 200)

    def test_hosting_settings_requires_token(self) -> None:
        request = urllib.request.Request(f"{self.base}/api/settings")
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(context.exception.code, 401)

    def test_hosting_settings_round_trip(self) -> None:
        env_path = Path(self.tmp.name) / ".env"
        env_path.write_text("QQ_DIGEST_HOSTING_QUIT_QQ=1\nQQ_DIGEST_HOSTING_RESTORE_QQ=1\nQQ_DIGEST_HOSTING_AUTO_ON_START=0\nQQ_DIGEST_AUTOSTART=0\nKEEP=yes\n", encoding="utf-8")
        self.server.settings.env_file = env_path
        payload = json.dumps({"quit_qq": False, "restore_qq": True, "auto_on_start": True, "autostart": False}).encode()
        request = urllib.request.Request(f"{self.base}/api/settings", data=payload, headers={"X-Token":"secret", "Content-Type":"application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertTrue(json.loads(response.read().decode())["ok"])
        data = self._get("/api/settings", "secret")
        self.assertFalse(data["quit_qq"]); self.assertTrue(data["auto_on_start"])
        raw = env_path.read_text(encoding="utf-8")
        self.assertIn("NOTICEHUB_HOSTING_QUIT_QQ=0", raw); self.assertIn("KEEP=yes", raw)

    def test_floatball_setting_defaults_off_and_round_trips(self) -> None:
        """浮球默认关闭：GET 回 false；POST true 写进 .env 且立刻能读回，false 亦然。"""
        env_path = Path(self.tmp.name) / ".env"
        env_path.write_text("QQ_DIGEST_AUTOSTART=0\nKEEP=yes\n", encoding="utf-8")
        self.server.settings.env_file = env_path
        self.assertFalse(self._get("/api/settings", "secret")["floatball_enabled"])
        self.assertTrue(self._post("/api/settings", {"floatball_enabled": True})["ok"])
        self.assertTrue(self._get("/api/settings", "secret")["floatball_enabled"])
        self.assertIn("NOTICEHUB_FLOATBALL_ENABLED=1", env_path.read_text(encoding="utf-8"))
        self.assertTrue(self._post("/api/settings", {"floatball_enabled": False})["ok"])
        self.assertFalse(self._get("/api/settings", "secret")["floatball_enabled"])
        self.assertIn("NOTICEHUB_FLOATBALL_ENABLED=0", env_path.read_text(encoding="utf-8"))
        self.assertIn("KEEP=yes", env_path.read_text(encoding="utf-8"))

    def test_task71_unauthorized_routes_have_no_side_effects(self) -> None:
        webapp._reset_auth_limits()
        self.addCleanup(webapp._reset_auth_limits)
        with (
            mock.patch("noticehub.webapp.catchup") as catchup_module,
            mock.patch("noticehub.webapp.inbox") as inbox_module,
            mock.patch.object(self.store, "inbox_items") as inbox_items,
            mock.patch.object(self.store, "inbox_counts") as inbox_counts,
            mock.patch.object(self.store, "delete_inbox_items") as delete_inbox_items,
        ):
            requests = [
                ("GET", "/api/history/floor?group_id=g1", None),
                ("GET", "/api/history/fetch/status", None),
                ("GET", "/api/inbox/classify/status", None),
                ("GET", "/api/inbox", None),
                ("GET", "/api/inbox/image?msg_id=onebot%3Ag1%3Aattach%3A0123456789abcdef", None),
                ("POST", "/api/history/fetch", {"groups": ["g1"]}),
                ("POST", "/api/history/fetch/cancel", {}),
                ("POST", "/api/inbox/classify/cancel", {}),
                ("POST", "/api/inbox/classify", {"groups": ["g1"]}),
                ("POST", "/api/inbox/delete", {"msg_id": "m1"}),
                ("POST", "/api/inbox/promote", {"msg_id": "m1"}),
                ("POST", "/api/settings", {"catchup_enabled": True, "catchup_hours": 48}),
            ]
            for method, path, payload in requests:
                request = urllib.request.Request(
                    self.base + path,
                    data=json.dumps(payload).encode("utf-8") if payload is not None else None,
                    headers={"Content-Type": "application/json"} if payload is not None else {},
                    method=method,
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(context.exception.code, 401, path)
                context.exception.close()
            catchup_module.available_floor.assert_not_called()
            catchup_module.backfill_range.assert_not_called()
            inbox_module.classify_messages.assert_not_called()
            inbox_module.promote.assert_not_called()
            inbox_items.assert_not_called()
            inbox_counts.assert_not_called()
            delete_inbox_items.assert_not_called()

    def test_history_floor_validates_and_returns_local_cache_boundary(self) -> None:
        fake = mock.Mock()
        fake.available_floor.return_value = {"floor_ts": "2026-10-01T08:00:00", "total_seen": 42, "pages": 2, "error": ""}
        with mock.patch("noticehub.webapp.catchup", fake):
            result = self._get("/api/history/floor?group_id=g1", "secret")
            self.assertEqual(
                result,
                {
                    "ok": True, "floor_ts": "2026-10-01T08:00:00", "total_seen": 42,
                    "pages": 2, "partial": True,
                    "note": "只探测了当前 QQ 缓存里最近的约 2000 条，更早的记录未验证。",
                    "error": "",
                },
            )
            fake.available_floor.assert_called_once_with(self.settings, "g1", max_pages=2)
            request = urllib.request.Request(self.base + "/api/history/floor?group_id=", headers={"X-Token": "secret"})
            with self.assertRaises(urllib.error.HTTPError) as context:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(context.exception.code, 400)
            context.exception.close()

    def test_history_floor_reports_complete_when_scan_stops_early(self) -> None:
        """没探满上限就是「已到缓存底线」，不能贴 partial 标签。"""
        fake = mock.Mock()
        fake.available_floor.return_value = {"floor_ts": "2026-09-01T08:00:00", "total_seen": 300, "pages": 1, "error": ""}
        with mock.patch("noticehub.webapp.catchup", fake):
            result = self._get("/api/history/floor?group_id=g1", "secret")
        self.assertFalse(result["partial"])
        self.assertEqual(result["note"], "")
        self.assertEqual(result["pages"], 1)

    def test_classify_cancel_is_ok_even_when_no_job_is_running(self) -> None:
        """取消一个不存在/已结束的任务要回 ok，不能 500/409 让前端报错。"""
        body = self._post("/api/inbox/classify/cancel", {})
        self.assertTrue(body["ok"])
        self.assertFalse(body["cancelled"])

    def test_history_and_classification_jobs_report_progress_and_exclude_each_other(self) -> None:
        history_started = threading.Event()
        history_release = threading.Event()
        history_done = threading.Event()
        classify_done = threading.Event()

        def backfill(settings, store, *, groups, since, until, progress, cancel):
            history_started.set()
            progress("history", 1, 2, groups[0])
            history_release.wait(3)
            history_done.set()
            return {"ok": True, "inserted": 3, "scanned": 4, "groups": [], "error": ""}

        def classify(settings, store, *, since, until, groups, limit, progress, cancel):
            progress(2, 5)
            classify_done.set()
            return {"ok": True, "classified": 2, "counts": {"notice": 1, "suspect": 1, "noise": 0}, "method": "heuristic", "error": ""}

        with mock.patch("noticehub.webapp.catchup.backfill_range", side_effect=backfill) as backfill_mock, mock.patch("noticehub.webapp.inbox.classify_messages", side_effect=classify) as classify_mock:
            started = self._post("/api/history/fetch", {"groups": ["g1"], "since": "2026-10-01", "until": "2026-10-02"})
            self.assertEqual(started, {"ok": True, "started": True})
            self.assertTrue(history_started.wait(2))
            status = self._get("/api/history/fetch/status", "secret")
            self.assertTrue(status["running"])
            self.assertEqual((status["done"], status["total"], status["stage"]), (1, 2, "history"))
            for path, payload in (
                ("/api/history/fetch", {"groups": ["g1"]}),
                ("/api/inbox/classify", {"groups": ["g1"]}),
            ):
                request = urllib.request.Request(self.base + path, data=json.dumps(payload).encode(), headers={"X-Token": "secret", "Content-Type": "application/json"}, method="POST")
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(context.exception.code, 409)
                context.exception.close()
            history_release.set()
            self.assertTrue(history_done.wait(2))
            deadline = time.monotonic() + 2
            while self._get("/api/history/fetch/status", "secret")["running"] and time.monotonic() < deadline:
                time.sleep(0.01)
            status = self._get("/api/history/fetch/status", "secret")
            self.assertFalse(status["running"])
            self.assertEqual(status["result"]["inserted"], 3)
            self.assertEqual(backfill_mock.call_args.kwargs["since"], dt.datetime(2026, 10, 1))
            self.assertEqual(backfill_mock.call_args.kwargs["until"], dt.datetime(2026, 10, 2, 23, 59, 59))

            started = self._post("/api/inbox/classify", {"groups": ["g1"], "limit": 7})
            self.assertTrue(started["started"])
            self.assertTrue(classify_done.wait(2))
            deadline = time.monotonic() + 2
            while self._get("/api/inbox/classify/status", "secret")["running"] and time.monotonic() < deadline:
                time.sleep(0.01)
            status = self._get("/api/inbox/classify/status", "secret")
            self.assertFalse(status["running"])
            self.assertEqual((status["done"], status["total"]), (2, 5))
            self.assertEqual(status["result"]["classified"], 2)
            self.assertEqual(classify_mock.call_args.kwargs["limit"], 7)
        history_release.set()

    def test_history_cancel_only_cancels_history_and_reports_cancelled(self) -> None:
        def cancel_request() -> tuple[int, dict]:
            request = urllib.request.Request(
                self.base + "/api/history/fetch/cancel",
                data=b"{}",
                headers={"X-Token": "secret", "Content-Type": "application/json"},
                method="POST",
            )
            try:
                with self._open(request) as response:
                    return response.status, json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                try:
                    return error.code, json.loads(error.read().decode("utf-8"))
                finally:
                    error.close()

        status, body = cancel_request()
        self.assertEqual(status, 409)
        self.assertFalse(body["ok"])
        self.assertTrue(body["error"])

        classify_started = threading.Event()
        classify_release = threading.Event()
        history_started = threading.Event()
        history_cancelled = threading.Event()
        history_release = threading.Event()

        def classify(*args, **kwargs):
            classify_started.set()
            classify_release.wait(3)
            return {"ok": True, "classified": 0}

        def backfill(*args, cancel, **kwargs):
            history_started.set()
            while not cancel():
                time.sleep(0.01)
            history_cancelled.set()
            history_release.wait(3)
            return {"ok": True, "inserted": 0, "error": ""}

        with (
            mock.patch("noticehub.webapp.catchup.backfill_range", side_effect=backfill),
            mock.patch("noticehub.webapp.inbox.classify_messages", side_effect=classify),
        ):
            started = self._post("/api/inbox/classify", {"groups": ["g1"]})
            self.assertTrue(started["started"])
            self.assertTrue(classify_started.wait(2))
            status, body = cancel_request()
            self.assertEqual(status, 409)
            self.assertFalse(body["ok"])
            self.assertTrue(self._get("/api/inbox/classify/status", "secret")["running"])
            classify_release.set()
            deadline = time.monotonic() + 2
            while self._get("/api/inbox/classify/status", "secret")["running"] and time.monotonic() < deadline:
                time.sleep(0.01)

            started = self._post(
                "/api/history/fetch",
                {"groups": ["g1"], "since": "2026-10-01", "until": "2026-10-02"},
            )
            self.assertTrue(started["started"])
            self.assertTrue(history_started.wait(2))
            self.assertEqual(cancel_request(), (200, {"ok": True, "cancelled": True}))
            self.assertTrue(history_cancelled.wait(2))
            self.assertTrue(self._get("/api/history/fetch/status", "secret")["running"])
            history_release.set()
            deadline = time.monotonic() + 2
            while self._get("/api/history/fetch/status", "secret")["running"] and time.monotonic() < deadline:
                time.sleep(0.01)
            state = self._get("/api/history/fetch/status", "secret")
            self.assertFalse(state["running"])
            self.assertEqual(state["stage"], "cancelled")
            self.assertEqual(state["result"], {"ok": False, "cancelled": True, "error": "已取消"})

    def test_inbox_image_endpoint_serves_only_exact_attachment_images(self) -> None:
        files_dir = self.settings.data_dir / "files"
        files_dir.mkdir(parents=True)

        msg_id = "onebot:g1:attach:0123456789abcdef"
        self.store.insert_message(
            msg_id=msg_id,
            group_id="g1",
            content="【图片】\n图片说明",
            source="onebot-attachment",
            event="GROUP_ATTACHMENT",
        )
        image_path = files_dir / "0123456789abcdef_photo.png"
        image = Image.new("RGB", (2, 2), "red")
        output = io.BytesIO()
        image.save(output, format="PNG")
        expected = output.getvalue()
        image_path.write_bytes(expected)

        path = "/api/inbox/image?" + urllib.parse.urlencode({"msg_id": msg_id, "token": "secret"})
        status, headers, body = self._raw_request(path, token="")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "image/png")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("Cache-Control"), "private, no-store")
        self.assertEqual(body, expected)

        # 路径穿越现在按契约直接 400（以前是 404），其余不合法/不存在仍是 404。
        status, _, _ = self._raw_request("/api/inbox/image?msg_id=../../secret", token="secret")
        self.assertEqual(status, 400)
        status, _, _ = self._raw_request(
            "/api/inbox/image?" + urllib.parse.urlencode({"msg_id": msg_id}), token=""
        )
        self.assertEqual(status, 401)

    def test_inbox_image_endpoint_rejects_non_attachment_and_ambiguous_files(self) -> None:
        files_dir = self.settings.data_dir / "files"
        files_dir.mkdir(parents=True)
        cases = (
            ("onebot:g1:attach:1111111111111111", "qqbot", "GROUP_ATTACHMENT", ["1111111111111111_bad.png"]),
            ("onebot:g1:attach:4444444444444444", "onebot-attachment", "PRIVATE_ATTACHMENT", ["4444444444444444_bad-event.png"]),
            ("onebot:g1:attach:2222222222222222", "onebot-attachment", "GROUP_ATTACHMENT", ["2222222222222222_a.png", "2222222222222222_b.png"]),
            ("onebot:g1:attach:3333333333333333", "onebot-attachment", "GROUP_ATTACHMENT", ["3333333333333333_file.txt"]),
        )
        for msg_id, source, event, filenames in cases:
            with self.subTest(msg_id=msg_id):
                self.store.insert_message(
                    msg_id=msg_id,
                    group_id="g1",
                    content="【图片】",
                    source=source,
                    event=event,
                )
                for filename in filenames:
                    (files_dir / filename).write_bytes(b"not served")
                path = "/api/inbox/image?" + urllib.parse.urlencode({"msg_id": msg_id})
                status, _, _ = self._raw_request(path)
                self.assertEqual(status, 404)

        malformed_keys = (
            "0123456789abcdef0",  # longer than the 16-hex key contract
            "0123456789abcdeg",  # non-hex character
        )
        for key in malformed_keys:
            msg_id = f"onebot:g1:attach:{key}"
            with self.subTest(msg_id=msg_id):
                self.store.insert_message(
                    msg_id=msg_id,
                    group_id="g1",
                    content="【图片】",
                    source="onebot-attachment",
                    event="GROUP_ATTACHMENT",
                )
                (files_dir / f"{key}_bad.png").write_bytes(b"not really a png")
                path = "/api/inbox/image?" + urllib.parse.urlencode({"msg_id": msg_id})
                status, _, _ = self._raw_request(path)
                self.assertEqual(status, 404)

    def _write_plain_image(self, msg_id: str) -> tuple[bytes, Path]:
        files_dir = self.settings.data_dir / "files"
        files_dir.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (2, 2), "blue")
        output = io.BytesIO()
        image.save(output, format="PNG")
        expected = output.getvalue()
        path = files_dir / f"{webapp.image_key(msg_id)}_0.png"
        path.write_bytes(expected)
        return expected, path

    def test_inbox_image_serves_plain_and_catchup_message_images(self) -> None:
        # 普通消息（onebot）与历史回溯（onebot-catchup）都按 msg_id 推导留图，接口不该再卡 source。
        for msg_id, source in (
            ("onebot:g1:2048", "onebot"),
            ("onebot-catchup:g1:2049", "onebot-catchup"),
        ):
            with self.subTest(msg_id=msg_id):
                self.store.insert_message(msg_id=msg_id, group_id="g1", content="[图片]", source=source)
                self.store.save_mark(msg_id, "notice", method="llm")
                expected, file_path = self._write_plain_image(msg_id)

                path = "/api/inbox/image?" + urllib.parse.urlencode({"msg_id": msg_id})
                status, headers, body = self._raw_request(path)
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("Content-Type"), "image/png")
                self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
                self.assertEqual(body, expected)

                items = self._get("/api/inbox", "secret")["items"]
                item = next(entry for entry in items if entry["msg_id"] == msg_id)
                self.assertTrue(item["has_image"], "带原图的消息要在 JSON 里标 has_image")

                cleared = self._post("/api/inbox/delete", {"msg_ids": [msg_id]})
                self.assertEqual(cleared["deleted"], 1)
                self.assertFalse(file_path.exists(), "删条目要连原图一起删")

    def test_inbox_image_plain_message_without_file_is_404(self) -> None:
        msg_id = "onebot:g1:3001"
        self.store.insert_message(msg_id=msg_id, group_id="g1", content="[图片]", source="onebot")
        self.store.save_mark(msg_id, "notice", method="llm")
        path = "/api/inbox/image?" + urllib.parse.urlencode({"msg_id": msg_id})
        status, _, _ = self._raw_request(path)
        self.assertEqual(status, 404)
        items = self._get("/api/inbox", "secret")["items"]
        self.assertFalse(next(entry for entry in items if entry["msg_id"] == msg_id)["has_image"])

    def test_inbox_image_rejects_traversal_and_keeps_auth(self) -> None:
        for msg_id in ("../secret", "a/b", "a\\b", ""):
            with self.subTest(msg_id=msg_id):
                path = "/api/inbox/image?" + urllib.parse.urlencode({"msg_id": msg_id})
                status, _, _ = self._raw_request(path)
                self.assertEqual(status, 400)

        msg_id = "onebot:g1:3002"
        expected, _first = self._write_plain_image(msg_id)
        status, _, body = self._raw_request("/api/inbox/image?" + urllib.parse.urlencode({"msg_id": msg_id}))
        self.assertEqual(status, 200)
        self.assertEqual(body, expected)

        status, _, _ = self._raw_request(
            "/api/inbox/image?" + urllib.parse.urlencode({"msg_id": msg_id}), token=""
        )
        self.assertEqual(status, 401)

    def test_inbox_image_multiple_plain_files_serve_first_and_agree_with_has_image(self) -> None:
        """契约变更（2026-10-31）：同一 msg_id 有多张图不再 404，取序号最小的 `_0`。

        老规则要求「唯一才算」，于是 files/<key>_0 与 <key>_1 同时存在时接口回 404，
        前端 onerror 把 <img> 撤掉，用户一张也看不到；可这两张本来就是同一条消息的图，
        不是歧义。同时锁住不变式：has_image 为真 ⟺ 这个接口回 200。
        """
        msg_id = "onebot:g1:3101"
        self.store.insert_message(msg_id=msg_id, group_id="g1", content="[图片]", source="onebot")
        self.store.save_mark(msg_id, "notice", method="llm")
        first_bytes, first_path = self._write_plain_image(msg_id)
        second_path = first_path.with_name(first_path.name.replace("_0.", "_1."))
        second_bytes = b"\x89PNG\r\n\x1a\nnot-the-first-one"
        second_path.write_bytes(second_bytes)
        self.assertNotEqual(first_bytes, second_bytes)

        path = "/api/inbox/image?" + urllib.parse.urlencode({"msg_id": msg_id})
        status, headers, body = self._raw_request(path)
        self.assertEqual(status, 200, "同一条消息的两张图不是歧义，必须能显示")
        self.assertEqual(headers.get("Content-Type"), "image/png")
        self.assertEqual(body, first_bytes, "多张时取序号最小的 _0")

        def has_image_flag() -> bool:
            items = self._get("/api/inbox", "secret")["items"]
            return bool(next(entry for entry in items if entry["msg_id"] == msg_id)["has_image"])

        self.assertTrue(has_image_flag(), "接口会回 200，has_image 就必须是 true")

        second_path.unlink()
        status, _, body = self._raw_request(path)
        self.assertEqual(status, 200, "只剩一张也要能显示")
        self.assertEqual(body, first_bytes)
        self.assertTrue(has_image_flag(), "剩一张也算有图")

        first_path.unlink()
        status, _, _ = self._raw_request(path)
        self.assertEqual(status, 404, "没有图时接口 404，与 has_image=false 一致")
        self.assertFalse(has_image_flag(), "没有图时 has_image 必须 false")

    def test_attachment_event_image_round_trip(self) -> None:
        """端到端：附件事件真过 ``AttachmentWorker.process()``，再用收件箱记录的 msg_id 取图。

        回归背景：写盘按 ``image_key(消息行 id)``、读盘按 ``attach:<key>`` 找，两边不同源，
        附件记录那张图永远 404——而它本来是唯一能出图的路径。这条用例必须真的走
        「下载落盘 → 留原图 → 发 GET」，不许往 ``files/`` 里手搓文件冒充。
        """
        from noticehub import attachments as att

        records: list[dict] = []
        worker = att.AttachmentWorker(self.settings, records.append)
        stub = b"\x89PNG\r\n\x1a\nround-trip-stub"
        with tempfile.TemporaryDirectory() as td:
            downloaded = Path(td) / "downloaded.png"
            downloaded.write_bytes(stub)
            attachment = att.Attachment(
                kind="image",
                name="photo.png",
                group_id="g1",
                sender_id="u1",
                msg_id="onebot:g1:4096",
                size=len(stub),
            )
            with mock.patch.object(worker, "_fetch", return_value=downloaded), \
                 mock.patch.object(att, "image_text", return_value="明天下午三点开会，带材料"), \
                 mock.patch.object(Settings, "vision_active", new_callable=mock.PropertyMock, return_value=True):
                record = worker.process(attachment)

        self.assertIsNotNone(record)
        self.assertEqual(record["source"], "onebot-attachment")
        record_id = str(record["msg_id"])
        self.assertEqual(record_id, f"onebot:g1:attach:{attachment.key}")

        self.store.insert_message(
            msg_id=record_id,
            group_id="g1",
            content=str(record["content"]),
            source="onebot-attachment",
            event="GROUP_ATTACHMENT",
        )
        self.store.save_mark(record_id, "notice", method="llm")

        path = "/api/inbox/image?" + urllib.parse.urlencode({"msg_id": record_id})
        status, headers, body = self._raw_request(path)
        self.assertEqual(status, 200, "附件记录的图必须能取到：写盘前缀要等于读盘前缀")
        self.assertEqual(headers.get("Content-Type"), "image/png")
        self.assertEqual(body, stub)

        # 同一条消息的普通记录（onebot:g1:4096）也要照旧有图：硬链接别名，磁盘只占一份。
        self.store.insert_message(msg_id="onebot:g1:4096", group_id="g1", content="[图片]", source="onebot")
        self.store.save_mark("onebot:g1:4096", "notice", method="llm")
        items = self._get("/api/inbox", "secret")["items"]
        self.assertTrue(next(e for e in items if e["msg_id"] == "onebot:g1:4096")["has_image"])

    def test_inbox_promoted_filter_uses_store_flag_and_counts(self) -> None:
        row = {"msg_id": "m-promoted", "verdict": "notice", "task_id": 12, "content": "测试通知"}
        with mock.patch.object(self.store, "inbox_items", return_value=[row]) as items, mock.patch.object(self.store, "inbox_counts", return_value={"notice": 4, "suspect": 2, "noise": 1, "promoted": 1}):
            result = self._get("/api/inbox?verdict=promoted", "secret")
        self.assertTrue(result["ok"])
        self.assertEqual(result["items"], [row])
        self.assertEqual(result["total"], 1)
        self.assertIsNone(items.call_args.kwargs["verdict"])
        self.assertIs(items.call_args.kwargs["promoted"], True)

    def test_inbox_hides_placeholder_noise_and_filters_by_kind(self) -> None:
        rows = {
            "noise-image": ("[图片]", "noise"),
            "notice-image": ("[图片]", "notice"),
            "notice-file": ("[文件]作业.pdf", "notice"),
            "notice-link": ("https://example.com/notice", "notice"),
            "notice-text": ("明天开会", "notice"),
        }
        for msg_id, (content, verdict) in rows.items():
            self.store.insert_message(msg_id=msg_id, group_id="g1", content=content)
            self.store.save_mark(msg_id, verdict)

        default = self._get("/api/inbox", "secret")
        default_ids = {row["msg_id"] for row in default["items"]}
        self.assertNotIn("noise-image", default_ids, "纯 [图片] 噪声默认不该出现在收件箱")
        self.assertIn("notice-image", default_ids, "判定为通知的 [图片] 是正文，不能被当成噪声藏掉")
        self.assertEqual(len(default_ids), default["total"], "total 必须是筛选后的条数，不是全表条数")
        self.assertEqual("all", default["kind"])
        self.assertFalse(default["include_noise"])
        self.assertEqual(1, default["counts"]["image"], "chip 的图片数要按默认可见口径算")
        self.assertEqual(1, default["counts"]["file"])
        self.assertEqual(1, default["counts"]["link"])
        self.assertEqual(1, default["counts"]["text"])
        self.assertEqual(4, default["counts"]["visible"])

        with_noise = self._get("/api/inbox?include_noise=1", "secret")
        self.assertIn("noise-image", {row["msg_id"] for row in with_noise["items"]})
        self.assertEqual(5, with_noise["total"])
        self.assertEqual(2, with_noise["counts"]["image"])
        self.assertTrue(with_noise["include_noise"])

        only_images = self._get("/api/inbox?kind=image", "secret")
        self.assertEqual({"notice-image"}, {row["msg_id"] for row in only_images["items"]})
        self.assertEqual("image", only_images["kind"])

        only_text = self._get("/api/inbox?kind=text", "secret")
        text_ids = {row["msg_id"] for row in only_text["items"]}
        self.assertNotIn("notice-image", text_ids, "kind=text 不该返回纯图片条目")
        self.assertIn("notice-text", text_ids)

        both_images = self._get("/api/inbox?kind=image&include_noise=1", "secret")
        self.assertEqual({"notice-image", "noise-image"}, {row["msg_id"] for row in both_images["items"]})

    def test_inbox_total_follows_filters_and_pagination(self) -> None:
        for index in range(3):
            self.store.insert_message(msg_id=f"text-{index}", group_id="g1", content=f"通知 {index}")
            self.store.save_mark(f"text-{index}", "notice")
        self.store.insert_message(msg_id="noise-0", group_id="g1", content="[表情]")
        self.store.save_mark("noise-0", "noise")

        first = self._get("/api/inbox?limit=2&offset=0", "secret")
        last = self._get("/api/inbox?limit=2&offset=2", "secret")
        self.assertEqual(3, first["total"], "被藏起来的噪声不能算进总数")
        self.assertEqual(2, len(first["items"]))
        self.assertEqual(3, last["total"])
        self.assertEqual(1, len(last["items"]), "总数说 3 条就要真能翻到第 3 条")

    def test_inbox_rejects_unknown_kind_and_include_noise(self) -> None:
        for query in ("kind=video", "include_noise=yes"):
            status, _headers, body = self._raw_request("/api/inbox?" + query, token="secret")
            self.assertEqual(400, status, query)
            self.assertFalse(json.loads(body.decode("utf-8"))["ok"], query)

    def test_inbox_promote_still_finds_placeholder_noise(self) -> None:
        self.store.insert_message(msg_id="noise-promote", group_id="g1", content="[图片]")
        self.store.save_mark("noise-promote", "noise")

        result = self._post("/api/inbox/promote", {"msg_id": "noise-promote"})

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["task_id"], "收件箱藏起来不等于这条不存在，转待办必须仍能找到它")

    def test_inbox_delete_removes_message_marks_and_listing(self) -> None:
        for msg_id in ("del-1", "del-2"):
            self.store.insert_message(msg_id=msg_id, group_id="g1", content="通知")
            self.store.save_mark(msg_id, "notice")

        result = self._post("/api/inbox/delete", {"msg_id": "del-1"})

        self.assertEqual(result, {"ok": True, "deleted": 1})
        listed = [row["msg_id"] for row in self._get("/api/inbox", "secret")["items"]]
        self.assertNotIn("del-1", listed)
        self.assertIn("del-2", listed)
        self.assertEqual(self.store.inbox_counts()["notice"], 1)

    def test_inbox_delete_accepts_a_batch_of_ids(self) -> None:
        for msg_id in ("batch-1", "batch-2", "batch-3"):
            self.store.insert_message(msg_id=msg_id, group_id="g1", content="通知")
            self.store.save_mark(msg_id, "notice")

        result = self._post("/api/inbox/delete", {"msg_ids": ["batch-1", "batch-2", "不存在"]})

        self.assertTrue(result["ok"])
        self.assertEqual(result["deleted"], 2)
        listed = [row["msg_id"] for row in self._get("/api/inbox", "secret")["items"]]
        self.assertEqual(listed, ["batch-3"])

    def test_inbox_delete_rejects_invalid_bodies(self) -> None:
        cases = [
            {},
            {"msg_id": 12},
            {"msg_id": ""},
            {"msg_ids": []},
            {"msg_ids": ["ok", 5]},
            {"msg_ids": ["x"] * 201},
            {"msg_id": "x" * 201},
        ]
        for payload in cases:
            with self.subTest(payload=str(payload)[:60]):
                status, _headers, body = self._raw_request("/api/inbox/delete", "POST", payload)
                self.assertEqual(status, 400, body)
                result = json.loads(body.decode("utf-8"))
                self.assertFalse(result["ok"])
                self.assertTrue(result["error"])

    def test_catchup_settings_round_trip_existing_keys_and_preserves_env(self) -> None:
        env_path = Path(self.tmp.name) / ".env"
        env_path.write_text("QQ_DIGEST_CATCHUP_ENABLED=0\nQQ_DIGEST_CATCHUP_HOURS=24\nKEEP=yes\n", encoding="utf-8")
        self.server.settings.env_file = env_path
        self.assertEqual(self._get("/api/settings", "secret")["catchup_hours"], 24)
        result = self._post("/api/settings", {"catchup_enabled": True, "catchup_hours": 72})
        self.assertTrue(result["ok"])
        self.assertTrue(result["catchup_enabled"])
        self.assertEqual(result["catchup_hours"], 72)
        self.assertFalse(self.settings.catchup_enabled)
        self.assertEqual(self.settings.catchup_hours, 24)
        saved = self._get("/api/settings", "secret")
        self.assertTrue(saved["catchup_enabled"])
        self.assertEqual(saved["catchup_hours"], 72)
        self.assertEqual(env_path.read_text(encoding="utf-8"), "NOTICEHUB_CATCHUP_ENABLED=1\nNOTICEHUB_CATCHUP_HOURS=72\nKEEP=yes\n")

    def test_lab_rules_setting_round_trip_and_hot_apply(self) -> None:
        """自动整理规则开关：勾选即存 QQ_DIGEST_LAB_RULES，当次生效，回读一致。"""
        env_path = Path(self.tmp.name) / ".env"
        env_path.write_text("KEEP=yes\n", encoding="utf-8")
        self.server.settings.env_file = env_path
        self.server.settings.lab_rules = True

        before = self._get("/api/settings", "secret")
        self.assertTrue(before["lab_rules"])

        result = self._post("/api/settings", {"lab_rules": False})
        self.assertTrue(result["ok"])
        self.assertEqual(result["lab"], {"rules": False})
        # 热生效：不用重启，当前 Settings 立刻可用
        self.assertFalse(self.settings.lab_rules)

        saved = self._get("/api/settings", "secret")
        self.assertFalse(saved["lab_rules"])

        text = env_path.read_text(encoding="utf-8")
        self.assertIn("NOTICEHUB_LAB_RULES=0", text)
        self.assertIn("KEEP=yes", text, "只写这一项，别的行要原样保留")

        # 打开：写 1 并回读为 True
        self.assertTrue(self._post("/api/settings", {"lab_rules": True})["ok"])
        self.assertTrue(self._get("/api/settings", "secret")["lab_rules"])
        self.assertIn("NOTICEHUB_LAB_RULES=1", env_path.read_text(encoding="utf-8"))

        # 非布尔值拒绝；和别的设置混着存也要拒绝（避免一次请求写两个区块）
        bad_status, bad_body = self._lab_request("POST", "/api/settings", payload={"lab_rules": "yes"})
        self.assertEqual(bad_status, 400)
        self.assertFalse(bad_body["ok"])
        mixed_status, mixed = self._lab_request("POST", "/api/settings", payload={"lab_rules": True, "catchup_hours": 48})
        self.assertEqual(mixed_status, 400)
        self.assertIn("自动整理规则设置请单独保存", mixed["error"])

    def test_community_telemetry_settings_round_trip(self) -> None:
        """社区在线数开关：GET 必须能看到状态（前端靠它判断），POST 走同一套热生效。"""
        self.addCleanup(os.environ.pop, "QQ_DIGEST_COMMUNITY_TELEMETRY", None)
        env_path = Path(self.tmp.name) / ".env"
        env_path.write_text("KEEP=yes\n", encoding="utf-8")
        self.server.settings.env_file = env_path
        self.server.settings.community_telemetry = False

        before = self._get("/api/settings", "secret")
        self.assertIn("community_telemetry", before)
        self.assertFalse(before["community_telemetry"])

        result = self._post("/api/settings", {"community_telemetry": True})
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["community_telemetry"])
        self.assertTrue(self.settings.community_telemetry, "保存后当次运行就要生效")

        after = self._get("/api/settings", "secret")
        self.assertTrue(after["community_telemetry"])
        text = env_path.read_text(encoding="utf-8")
        self.assertIn("NOTICEHUB_COMMUNITY_TELEMETRY=1", text)
        self.assertIn("KEEP=yes", text, "只写这一项，别的行要原样保留")

    def test_community_counts_skips_ping_when_telemetry_disabled(self) -> None:
        """遥测关着时页面请求不许发上报（守卫在调用点，别白建线程）。"""
        self.server.settings.community_telemetry = False
        self.addCleanup(setattr, self.server.settings, "community_telemetry", False)
        calls: list[int] = []
        with mock.patch.object(webapp.community, "ping", side_effect=lambda *a, **k: calls.append(1)), \
             mock.patch.object(webapp.community, "fetch_counts", return_value={"online": None, "today": None}):
            body = self._get("/api/community/counts", "secret")
            time.sleep(0.3)
        self.assertEqual(calls, [], "关闭遥测时不许发上报")
        self.assertTrue(body["ok"])

    def test_community_counts_pings_when_telemetry_enabled(self) -> None:
        """开着遥测时仍要照常上报：守卫不能把功能一起关掉。"""
        self.server.settings.community_telemetry = True
        self.addCleanup(setattr, self.server.settings, "community_telemetry", False)
        done = threading.Event()
        with mock.patch.object(webapp.community, "ping", side_effect=lambda *a, **k: done.set()), \
             mock.patch.object(webapp.community, "fetch_counts", return_value={"online": None, "today": None}):
            body = self._get("/api/community/counts", "secret")
            self.assertTrue(done.wait(3.0), "开着遥测时应发出上报")
        self.assertTrue(body["ok"])

    def test_push_settings_form_is_present_and_prefilled_from_meta(self) -> None:
        for marker in (
            'id="push-settings"',
            'id="push-wxpusher_key"',
            'id="push-wxpusher_uids"',
            'id="push-wxpusher_topic_ids"',
            'id="push-serverchan_keys"',
            'id="push-pushplus_key"',
            'id="push-robot-urls"',
            'id="push-save"',
            'id="push-clear"',
            # 键名（发给后端的契约）与元素 id（页面上的输入框）必须成对写在一张表里：
            # 早先用 'push-'+name 拼 id，元素改名后 getElementById 返回 null、被静默跳过，
            # 输入值根本发不出去（六格里坏了三格，而测试全绿）。这里把表的两端逐个锁死。
            "['wxpusher_app_token','push-wxpusher_key']",
            "['wxpusher_uids','push-wxpusher_uids']",
            "['wxpusher_topic_ids','push-wxpusher_topic_ids']",
            "['serverchan_keys','push-serverchan_keys']",
            "['pushplus_tokens','push-pushplus_key']",
            "['webhook_urls','push-robot-urls']",
            "function pushInput(pair){return document.getElementById(pair[1]);}",
            "function applyPushState(",
            "function savePushSettings(all)",
            "applyPushState(data.push, data.channels);",
            "api('/api/settings',{method:'POST'",
        ):
            self.assertIn(marker, PAGE_SOURCE)
        # 拼 id 的写法就是这个 bug 的成因，别再回来。
        self.assertNotIn("document.getElementById('push-'+", PAGE_SOURCE)
        # 表单只回掩码占位，不把密钥写进页面
        self.assertIn("input.placeholder=(item&&item.set)?('已配置：'+item.masked.join('、')+'（留空不修改）'):'还没设置';", PAGE_SOURCE)

    def _set_wechat_env(self) -> Path:
        env_path = Path(self.tmp.name) / ".env"
        env_path.write_text("KEEP=yes\n", encoding="utf-8")
        self.server.settings.env_file = env_path
        self.server.settings.wechat_key = ""
        return env_path

    def test_wechat_manual_key_saves_to_env_and_hot_applies(self) -> None:
        """issue #5：手填 64 位 hex 密钥存进 .env，当次生效，不必重启。"""
        env_path = self._set_wechat_env()
        key = "0123456789ABCDEF" * 4  # 64 位，故意大写
        result = self._post("/api/settings", {"wechat_key": key})
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["manual_key"])
        text = env_path.read_text(encoding="utf-8")
        self.assertIn("NOTICEHUB_WECHAT_KEY=" + key.lower(), text, "统一存小写")
        self.assertIn("KEEP=yes", text, "只写这一行，别的行原样保留")
        self.assertEqual(self.settings.wechat_key, key.lower())
        self.assertEqual(os.environ.get("NOTICEHUB_WECHAT_KEY"), key.lower())

    def test_wechat_manual_key_get_never_echoes_the_secret(self) -> None:
        """GET /api/settings 只回 bool，不把密钥原文带回页面（同 /api/wechat/status）。"""
        self._set_wechat_env()
        key = "ab" * 32
        self.assertTrue(self._post("/api/settings", {"wechat_key": key})["ok"])
        saved = self._get("/api/settings", "secret")
        self.assertTrue(saved["manual_key"])
        self.assertNotIn(key, json.dumps(saved, ensure_ascii=False))
        self.assertNotIn("wechat_key", saved)

    def test_wechat_manual_key_rejects_invalid_value_without_writing(self) -> None:
        """非法密钥 400 + 人话，且不写盘、不回显用户贴进来的内容。"""
        env_path = self._set_wechat_env()
        status, body = self._lab_request("POST", "/api/settings", payload={"wechat_key": "deadbeef"})
        self.assertEqual(status, 400)
        self.assertIn("64 位十六进制", body["error"])
        self.assertNotIn("deadbeef", body["error"])
        self.assertNotIn("NOTICEHUB_WECHAT_KEY", env_path.read_text(encoding="utf-8"))
        self.assertEqual(self.settings.wechat_key, "")

    def test_wechat_manual_key_non_string_is_rejected(self) -> None:
        self._set_wechat_env()
        status, body = self._lab_request("POST", "/api/settings", payload={"wechat_key": 123})
        self.assertEqual(status, 400)
        self.assertIn("wechat_key", body["error"])

    def test_wechat_manual_key_empty_value_clears(self) -> None:
        """空字符串 = 清除：.env 写空值、Settings/环境变量一起清掉。"""
        env_path = self._set_wechat_env()
        key = "cd" * 32
        self.assertTrue(self._post("/api/settings", {"wechat_key": key})["ok"])
        cleared = self._post("/api/settings", {"wechat_key": ""})
        self.assertTrue(cleared["ok"], cleared)
        self.assertFalse(cleared["manual_key"])
        self.assertEqual(self.settings.wechat_key, "")
        self.assertIsNone(os.environ.get("NOTICEHUB_WECHAT_KEY"))
        self.assertIn("NOTICEHUB_WECHAT_KEY=\n", env_path.read_text(encoding="utf-8"))
        self.assertFalse(self._get("/api/settings", "secret")["manual_key"])

    def test_push_settings_round_trip_hot_applies_and_masks_secrets(self) -> None:
        env_path = Path(self.tmp.name) / ".env"
        env_path.write_text("KEEP=yes\n", encoding="utf-8")
        self.server.settings.env_file = env_path
        result = self._post(
            "/api/settings",
            {
                "wxpusher_app_token": "AT_abcdefgh1234",
                "wxpusher_uids": "UID_1111,UID_2222",
                "wxpusher_topic_ids": "12, 34",
                "serverchan_keys": "SCT_zzzz9999",
                "pushplus_tokens": "pp_qqqq7777",
                "webhook_urls": "https://example.com/hook/abcd",
            },
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["channels"], ["wxpusher", "serverchan", "pushplus", "webhook"])
        # 热生效：同一个进程里的 Settings 立刻反映新通道，不必重启
        self.assertEqual(self.settings.wxpusher_app_token, "AT_abcdefgh1234")
        self.assertEqual(self.settings.wxpusher_uids, ("UID_1111", "UID_2222"))
        self.assertEqual(self.settings.wxpusher_topic_ids, (12, 34))
        self.assertEqual(self.settings.serverchan_keys, ("SCT_zzzz9999",))
        self.assertEqual(self.settings.webhook_urls, ("https://example.com/hook/abcd",))
        text = env_path.read_text(encoding="utf-8")
        self.assertIn("WXPUSHER_APP_TOKEN=AT_abcdefgh1234", text)
        self.assertIn("WXPUSHER_UIDS=UID_1111,UID_2222", text)
        self.assertIn("WXPUSHER_TOPIC_IDS=12,34", text)
        self.assertIn("NOTICEHUB_WEBHOOKS=https://example.com/hook/abcd", text)
        self.assertIn("KEEP=yes", text)
        # 响应与 /api/meta 都只给尾 4 位，不回明文
        self.assertNotIn("AT_abcdefgh1234", json.dumps(result))
        self.assertTrue(result["push"]["wxpusher_app_token"]["set"])
        masked_token = result["push"]["wxpusher_app_token"]["masked"]
        self.assertEqual(len(masked_token), 1)
        self.assertTrue(masked_token[0].endswith("1234"))
        self.assertTrue(masked_token[0].startswith("*"))
        self.assertEqual(masked_token[0].count("*"), len("AT_abcdefgh1234") - 4)
        meta = self._get("/api/meta", "secret")
        self.assertIn("push", meta)
        self.assertNotIn("AT_abcdefgh1234", json.dumps(meta))
        # 留空的项不动，清除必须显式提交空串
        cleared = self._post("/api/settings", {"webhook_urls": ""})
        self.assertTrue(cleared["ok"])
        self.assertEqual(self.settings.webhook_urls, ())
        self.assertNotIn("webhook", cleared["channels"])
        self.assertNotIn("NOTICEHUB_WEBHOOKS=https://example.com/hook/abcd", env_path.read_text(encoding="utf-8"))

    def test_push_settings_reject_invalid_values_without_writing(self) -> None:
        env_path = Path(self.tmp.name) / ".env"
        env_path.write_text("KEEP=yes\n", encoding="utf-8")
        self.server.settings.env_file = env_path
        for payload in (
            {"webhook_urls": "example.com/hook"},
            {"wxpusher_topic_ids": "abc"},
            {"wxpusher_app_token": "A,B"},
            {"serverchan_keys": {"nested": "no"}},
        ):
            status, _headers, body = self._raw_request("/api/settings", "POST", payload)
            self.assertEqual(status, 400, payload)
            self.assertFalse(json.loads(body.decode("utf-8"))["ok"], payload)
        status, _headers, _body = self._raw_request(
            "/api/settings", "POST", {"webhook_urls": "https://ok.example/hook", "catchup_hours": 24}
        )
        self.assertEqual(status, 400)
        self.assertEqual(env_path.read_text(encoding="utf-8"), "KEEP=yes\n")

    def test_llm_settings_form_is_present_and_plain_language(self) -> None:
        for marker in (
            'id="llm-settings"',
            'id="llm-provider"',
            'id="llm-key"',
            'id="llm-model"',
            'id="llm-endpoint"',
            'id="llm-save"',
            'id="llm-off"',
            'id="llm-result"',
            'id="llm-steps-list"',
            'id="llm-help-link"',
            'id="push-test"',
            'class="push-help"',
            "function applyLlmState(state)",
            "function saveLlm(thenTest)",
            "function testLlm()",
            "function testPush()",
            "function applyPushHelp(map)",
            "llmProviders = data.providers || [];",
            "applyLlmState(data.llm);",
            "applyPushHelp(data.push_help);",
        ):
            self.assertIn(marker, PAGE_SOURCE)
        # 新手引导：每个通道都要有「一步一步怎么做」，说明里不出现接口术语
        self.assertIn("提醒怎么送到手机", PAGE_SOURCE)
        self.assertIn("不配也能用", PAGE_SOURCE)
        self.assertIn("<ol class=\"push-steps\">", PAGE_SOURCE)
        self.assertIn("data-help=\"wxpusher\"", PAGE_SOURCE)
        self.assertIn("data-help=\"serverchan\"", PAGE_SOURCE)
        self.assertIn("data-help=\"pushplus\"", PAGE_SOURCE)
        for jargon in ("OpenAI 兼容", "App Token", "SendKey", "Topic ID", "compatible-mode"):
            self.assertNotIn(jargon, PAGE_SOURCE)

    def test_llm_settings_round_trip_hot_applies_and_masks_key(self) -> None:
        env_path = Path(self.tmp.name) / ".env"
        env_path.write_text("KEEP=yes\n", encoding="utf-8")
        self.server.settings.env_file = env_path
        result = self._post(
            "/api/settings",
            {"llm_provider": "deepseek", "llm_api_key": "sk-test-abcd1234"},
        )
        self.assertTrue(result["ok"])
        llm = result["llm"]
        self.assertEqual(llm["provider"], "deepseek")
        self.assertTrue(llm["enabled"])
        self.assertTrue(llm["key_set"])
        self.assertEqual(llm["key_masked"][-4:], "1234")
        self.assertNotIn("sk-test-abcd1234", json.dumps(result))
        # 热生效：同一个进程里的 Settings 立刻切到新服务商，不必重启
        self.assertEqual(self.settings.dashscope_endpoint, "https://api.deepseek.com/chat/completions")
        self.assertEqual(self.settings.dashscope_model, "deepseek-flash")
        self.assertEqual(self.settings.dashscope_api_key, "sk-test-abcd1234")
        self.assertEqual(self.settings.vision_model, "deepseek-flash")
        text = env_path.read_text(encoding="utf-8")
        self.assertIn("NOTICEHUB_LLM=1", text)
        self.assertIn("NOTICEHUB_LLM_ENDPOINT=https://api.deepseek.com/chat/completions", text)
        self.assertIn("NOTICEHUB_LLM_MODEL=deepseek-flash", text)
        self.assertIn("NOTICEHUB_LLM_API_KEY=sk-test-abcd1234", text)
        self.assertIn("KEEP=yes", text)
        meta = self._get("/api/meta", "secret")
        self.assertEqual(meta["llm"]["provider"], "deepseek")
        self.assertNotIn("sk-test-abcd1234", json.dumps(meta))
        self.assertTrue(any(item["id"] == "deepseek" for item in meta["providers"]))
        self.assertEqual(meta["push_help"]["wxpusher"], "https://wxpusher.zjiecode.com/admin/")
        # 换一家：留空 key 表示不改，仍沿用原来那把钥匙
        switched = self._post("/api/settings", {"llm_provider": "zhipu"})
        self.assertTrue(switched["ok"])
        self.assertEqual(switched["llm"]["provider"], "zhipu")
        self.assertEqual(self.settings.dashscope_api_key, "sk-test-abcd1234")
        self.assertEqual(self.settings.dashscope_endpoint, "https://open.bigmodel.cn/api/paas/v4/chat/completions")
        # 关掉 AI：只记原始消息，不再调用模型
        off = self._post("/api/settings", {"llm_provider": "off"})
        self.assertTrue(off["ok"])
        self.assertFalse(off["llm"]["enabled"])
        self.assertFalse(self.settings.llm_enabled)
        self.assertIn("NOTICEHUB_LLM=0", env_path.read_text(encoding="utf-8"))

    def test_llm_settings_reject_invalid_values_without_writing(self) -> None:
        env_path = Path(self.tmp.name) / ".env"
        env_path.write_text("KEEP=yes\n", encoding="utf-8")
        self.server.settings.env_file = env_path
        self.server.settings.dashscope_api_key = ""
        for payload in (
            {"llm_provider": "no-such-provider"},
            {"llm_provider": "deepseek"},
            {"llm_provider": "custom", "llm_api_key": "sk-x", "llm_endpoint": "api.example.com/v1"},
            {"llm_provider": "custom", "llm_api_key": {"nested": "no"}},
            {"llm_provider": "custom", "llm_api_key": "sk-x", "llm_endpoint": "https://api.example.com/v1/chat/completions"},
        ):
            status, _headers, body = self._raw_request("/api/settings", "POST", payload)
            self.assertEqual(status, 400, payload)
            self.assertFalse(json.loads(body.decode("utf-8"))["ok"], payload)
        # AI 设置不能和推送 / 托管设置混在同一请求里
        status, _headers, _body = self._raw_request(
            "/api/settings", "POST", {"llm_provider": "deepseek", "llm_api_key": "sk-x", "webhook_urls": "https://ok.example/hook"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(env_path.read_text(encoding="utf-8"), "KEEP=yes\n")

    def test_settings_test_endpoints_answer_in_plain_language(self) -> None:
        env_path = Path(self.tmp.name) / ".env"
        env_path.write_text("KEEP=yes\n", encoding="utf-8")
        self.server.settings.env_file = env_path
        self.server.settings.dashscope_api_key = ""
        # 没配任何通道时给一句人话，而不是抛异常
        push = self._post("/api/settings/test-push", {})
        self.assertFalse(push["ok"])
        self.assertEqual(push["error"], "还没有配置任何推送通道")
        self.assertEqual(push["results"], [])
        # 没填钥匙时同样给一句人话
        llm = self._post("/api/settings/test-llm", {})
        self.assertFalse(llm["ok"])
        self.assertIn("钥匙", llm["error"])
        self.assertNotIn("DASHSCOPE", llm["error"])
        self.assertNotIn("sk-", json.dumps(llm))
        # 测试接口不写 .env
        self.assertEqual(env_path.read_text(encoding="utf-8"), "KEEP=yes\n")

    def test_page_is_served_without_browser_cache(self) -> None:
        """页面按会话现算（通道徽标/版本号/agent brief），缓存住就等于「改了看不见」。"""
        request = urllib.request.Request(self.base + "/?token=secret",
                                         headers={"X-Token": "secret"})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")

    def test_inbox_ui_has_four_tabs_accessible_workflow_and_no_external_assets(self) -> None:
        html = urllib.request.urlopen(urllib.request.Request(self.base + "/?token=secret", headers={"X-Token": "secret"}), timeout=5).read().decode("utf-8")
        self.assertIn("id=\"tab-inbox-button\"", html)
        self.assertIn("回溯聊天记录", html)
        self.assertIn("确定通知", html)
        self.assertIn("疑似通知", html)
        self.assertIn("启动时自动回溯最近 N 天", PAGE_SOURCE)
        self.assertIn("var order = ['tasks', 'notices', 'inbox', 'settings'];", html)
        # 外链资源（脚本/字体/图片）会拖慢或泄露本机状态，一律不许引入；
        # 学习通登录页那种“用户点了才跳转”的 <a href> 不算资源，单独排除后再检查。
        without_user_links = re.sub(r'<a\s[^>]*href="https://[^"]*"[^>]*>', "", html)
        self.assertNotRegex(without_user_links, r'<(?:script|link|img|iframe|source)\b[^>]*(?:src|href)=["\']https://')
        self.assertNotRegex(without_user_links, r'url\(\s*["\']?https?://')
        self.assertNotRegex(without_user_links, r'@import\s+(?:url\()?\s*["\']?https?://')

    def test_hosting_status_and_start_stop_bridge(self) -> None:
        fake = mock.Mock()
        fake.hosting_status.return_value = {"ok": True, "hosting_active": False, "napcat_running": False, "napcat_online": False, "user_qq_running": False}
        fake.start.return_value = {"ok": True, "steps": [], "error": None}
        fake.stop.return_value = {"ok": True, "steps": [], "error": None}
        with mock.patch("noticehub.webapp.hosting", fake):
            self.assertTrue(self._get("/api/hosting/status", "secret")["ok"])
            for endpoint in ("/api/hosting/start", "/api/hosting/stop"):
                request = urllib.request.Request(self.base + endpoint, data=b"{}", headers={"X-Token":"secret", "Content-Type":"application/json"}, method="POST")
                with urllib.request.urlopen(request, timeout=5) as response: self.assertTrue(json.loads(response.read())["ok"])
        fake.start.assert_called_once_with(self.server.settings, uin=None); fake.stop.assert_called_once_with(self.server.settings)

    def test_hosting_module_failure_returns_json(self) -> None:
        with mock.patch("noticehub.webapp.hosting", None):
            data = self._get("/api/hosting/status", "secret")
        self.assertFalse(data["ok"])

    def test_dashboard_contains_login_choices_and_explanation(self) -> None:
        request = urllib.request.Request(f"{self.base}/?token=secret")
        with urllib.request.urlopen(request, timeout=5) as response:
            page = response.read().decode("utf-8")
        for text in ("扫码登录", "用指定 QQ 号快速登录", "独立资料目录", "同一 QQ 号不能同时登录两台电脑", "QQ 小号接收通知"):
            self.assertIn(text, PAGE_SOURCE)
        self.assertIn('id="auto-setup"', page)
        self.assertNotIn('id="install-napcat"', page)
        self.assertIn("下载安装并启动", PAGE_SOURCE)
        # 旧的三处重复说明（connect-hint 长段 / qr-start-note / napcat-engine 长句）删除，只留 #qq-next-step。
        self.assertIn('id="qq-next-step"', page)
        self.assertIn("已经自己装过 NapCat？", page)
        self.assertNotIn("第一次使用且还没安装 NapCat：先点", PAGE_SOURCE)
        self.assertNotIn("已有 NapCat 引擎时点「一键接入」完成接入", PAGE_SOURCE)
        self.assertNotIn("点这个按钮会自动下载并启动引擎", PAGE_SOURCE)
        self.assertNotIn("这只是「文件装好了」", PAGE_SOURCE)
        self.assertNotIn("第一次用：点「一键接入」——它会装好引擎", PAGE_SOURCE)
        for text in ("不会动你平时使用的电脑版 QQ", "移除该目录即可卸载"):
            self.assertIn(text, PAGE_SOURCE)

    def test_auto_setup_preserves_steps_restart_and_error_feedback(self) -> None:
        request = urllib.request.Request(f"{self.base}/?token=secret")
        with urllib.request.urlopen(request, timeout=5) as response:
            page = response.read().decode("utf-8")
        start = PAGE_SCRIPT.index("function runAutoSetup()")
        auto_setup = PAGE_SCRIPT[start:PAGE_SCRIPT.index("function run()", start)]
        self.assertIn("setupApi('/api/napcat/autosetup'", auto_setup)
        self.assertIn("method:'POST'", auto_setup)
        self.assertIn("result.steps", auto_setup)
        self.assertIn("result.waiting_for_login", auto_setup)
        self.assertIn("用手机 QQ 扫描下面的二维码登录", auto_setup)
        self.assertIn("result.restart_required", auto_setup)
        self.assertIn("NapCat 的接入端口没有起来", auto_setup)
        self.assertIn("result.restarted", auto_setup)
        self.assertIn("NapCat 已重启", auto_setup)
        self.assertIn("一键接入失败：", auto_setup)
        self.assertIn("document.getElementById('auto-setup').onclick=runAutoSetup", page)

    def test_qrcode_refresh_is_gated_by_stamp_and_login_reloads_once(self) -> None:
        request = urllib.request.Request(f"{self.base}/?token=secret")
        with urllib.request.urlopen(request, timeout=5) as response:
            page = response.read().decode("utf-8")
        self.assertIn("var lastQrStamp = null;", PAGE_SOURCE)
        self.assertIn("function refreshQrIfStale(stamp)", PAGE_SOURCE)
        # 状态里带回二维码标记，页面只在标记变化时才重取图片，不再每 5 秒闪一次
        self.assertIn("refreshQrIfStale(x.qr_stamp);", PAGE_SOURCE)
        self.assertIn("if (box && !box.hidden) { checkSetup(); } }, 5000);", PAGE_SOURCE)
        self.assertNotIn("if (box && !box.hidden) { refreshQr(); checkSetup(); }", PAGE_SOURCE)
        # 扫码成功后自动刷新一次整页，并用 sessionStorage 标记防止刷新循环
        self.assertIn("sessionStorage.setItem('qq_login_reloaded','1'); window.location.reload();", PAGE_SOURCE)
        self.assertIn("sessionStorage.removeItem('qq_login_reloaded');", PAGE_SOURCE)

    def test_napcat_install_requires_token(self) -> None:
        request = urllib.request.Request(f"{self.base}/api/napcat/install", data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(context.exception.code, 401)

    def test_napcat_install_passes_through_result(self) -> None:
        expected = {"ok": True, "installed": True, "already_installed": False, "root": "x", "version": "v4.18.33", "bytes": 12, "error": None}
        fake = mock.Mock(); fake.install.return_value = expected
        with mock.patch("noticehub.webapp.napcat_admin", fake):
            request = urllib.request.Request(f"{self.base}/api/napcat/install", data=b"{}", headers={"X-Token":"secret", "Content-Type":"application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(json.loads(response.read()), expected)
        # 安装接口带「确认安装包哈希」（accept_sha256）与 on_progress（进度写 job 快照，UX P1）。
        fake.install.assert_called_once()
        args, kwargs = fake.install.call_args
        self.assertIs(args[0], self.server.settings)
        self.assertEqual(kwargs.get("accept_sha256"), "")
        self.assertTrue(callable(kwargs.get("on_progress")))

    def test_napcat_install_job_records_progress_and_status_route_exposes_it(self) -> None:
        """安装进行中的阶段/字节数要能从 /api/napcat/install/status 读出来。"""
        expected = {"ok": True, "installed": True, "already_installed": False, "root": "x", "version": "v4.18.33", "bytes": 4096, "error": None}

        def fake_install(settings, *, accept_sha256="", on_progress=None):
            on_progress({"stage": "download", "downloaded_bytes": 1024, "total_bytes": 4096, "extracted": None, "entries": None})
            on_progress({"stage": "extract", "downloaded_bytes": 4096, "total_bytes": 4096, "extracted": 3, "entries": 7})
            return expected

        fake = mock.Mock()
        fake.install.side_effect = fake_install
        with mock.patch("noticehub.webapp.napcat_admin", fake):
            request = urllib.request.Request(f"{self.base}/api/napcat/install", data=b"{}", headers={"X-Token": "secret", "Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(json.loads(response.read()), expected)
            status = self._get("/api/napcat/install/status", token="secret")
        self.assertTrue(status["ok"])
        self.assertFalse(status["running"])
        self.assertEqual(status["stage"], "complete")
        self.assertEqual(status["downloaded_bytes"], 4096)
        self.assertEqual(status["total_bytes"], 4096)
        self.assertEqual(status["extracted"], 3)
        self.assertEqual(status["entries"], 7)
        self.assertEqual(status["result"]["version"], "v4.18.33")

    def test_napcat_install_status_reports_nulls_before_any_install(self) -> None:
        """没装过时字段全给 null，别编 0 让前端显示「已下载 0 MB」。"""
        webapp._JOB_STATE["napcat_install"].update(running=False, stage="idle", done=0, total=0, note="", result=None, detail={})
        status = self._get("/api/napcat/install/status", token="secret")
        self.assertTrue(status["ok"])
        self.assertFalse(status["running"])
        self.assertEqual(status["stage"], "idle")
        for key in ("downloaded_bytes", "total_bytes", "extracted", "entries"):
            self.assertIsNone(status[key], f"{key} 没数据时必须是 null")

    def test_napcat_autosetup_passes_through_admin_result(self) -> None:
        expected = {
            "ok": True,
            "steps": [{"name": "找到 NapCat", "ok": True, "detail": "F:\\NapCat"}],
            "qrcode_path": "C:\\tmp\\qrcode.png",
            "uin": "10001",
            "applied_via": "webui",
            "restart_required": False,
            "stage": "waiting_for_login",
            "error": None,
        }
        fake = mock.Mock()
        fake.auto_setup.return_value = expected
        with mock.patch("noticehub.webapp.napcat_admin", fake):
            request = urllib.request.Request(f"{self.base}/api/napcat/autosetup", data=b"{}", headers={"X-Token": "secret", "Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
        self.assertEqual(data, expected)
        fake.auto_setup.assert_called_once()
        self.assertEqual(fake.auto_setup.call_args.args[0], self.server.settings)
        # 安装进度要能透到 job 快照：路由必须把 on_progress 传下去
        self.assertTrue(callable(fake.auto_setup.call_args.kwargs.get("on_progress")))

    def test_napcat_autosetup_exception_is_reported_in_chinese(self) -> None:
        fake = mock.Mock()
        fake.auto_setup.side_effect = RuntimeError("boom")
        with mock.patch("noticehub.webapp.napcat_admin", fake):
            request = urllib.request.Request(f"{self.base}/api/napcat/autosetup", data=b"{}", headers={"X-Token": "secret", "Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
        self.assertFalse(data["ok"])
        self.assertTrue(data["error"].startswith("一键接入失败"), data["error"])
        # 异常也必须有 stage，前端才能分支
        self.assertEqual(data["stage"], "launch_failed")

    def test_napcat_autosetup_without_module_reports_error(self) -> None:
        with mock.patch("noticehub.webapp.napcat_admin", None):
            request = urllib.request.Request(f"{self.base}/api/napcat/autosetup", data=b"{}", headers={"X-Token": "secret", "Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
        self.assertFalse(data["ok"])
        self.assertIn("不可用", data["error"])

    def test_napcat_qrcode_prefers_admin_bytes(self) -> None:
        png = b"\x89PNG\r\n\x1a\nnot-a-real-png"
        fake = mock.Mock()
        fake.qrcode_bytes.return_value = png
        self.server.settings.napcat_qr_path = Path(self.tmp.name) / "missing.png"
        with mock.patch("noticehub.webapp.napcat_admin", fake):
            request = urllib.request.Request(f"{self.base}/api/napcat/qrcode?token=secret")
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), png)

class LanHostLookupTest(unittest.TestCase):
    """回归：hostname 解析卡住时 _lan_hosts() 不能跟着卡（macOS CI 上曾让请求 5 秒超时）。"""

    def setUp(self) -> None:
        webapp._reset_hostname_cache()
        webapp._reset_adapter_cache()
        self.addCleanup(self._settle_hostname_lookup)
        self.addCleanup(webapp._reset_adapter_cache)

    @staticmethod
    def _settle_hostname_lookup() -> None:
        """等后台解析线程退出再清缓存，避免它把结果写进下一个用例。"""
        thread = webapp._hostname_thread
        if thread is not None:
            thread.join(6.0)
        webapp._reset_hostname_cache()

    def test_lan_hosts_returns_without_waiting_for_a_hung_hostname_lookup(self) -> None:
        real_getaddrinfo = socket.getaddrinfo
        release = threading.Event()

        def slow_getaddrinfo(*args, **kwargs):
            release.wait(6.0)
            return real_getaddrinfo(*args, **kwargs)

        try:
            with mock.patch("noticehub.webapp.socket.getaddrinfo", side_effect=slow_getaddrinfo), mock.patch(
                "noticehub.webapp._lan_ipv4", return_value="192.168.8.10"
            ), mock.patch("noticehub.webapp._adapter_ipv4s", return_value=[]):
                started = time.monotonic()
                hosts = webapp._lan_hosts()
                elapsed = time.monotonic() - started
            self.assertLess(elapsed, 4.0)
            self.assertEqual(hosts, ["192.168.8.10"])
        finally:
            release.set()

    def test_hostname_lookup_is_cached_across_calls(self) -> None:
        calls = []

        def fake_getaddrinfo(*args, **kwargs):
            calls.append(args)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.8.11", 0))]

        with mock.patch("noticehub.webapp.socket.getaddrinfo", side_effect=fake_getaddrinfo), mock.patch(
            "noticehub.webapp._lan_ipv4", return_value="192.168.8.10"
        ), mock.patch("noticehub.webapp._adapter_ipv4s", return_value=[]):
            first = webapp._lan_hosts()
            second = webapp._lan_hosts()
        self.assertEqual(len(calls), 1)
        self.assertEqual(first, ["192.168.8.10", "192.168.8.11"])
        self.assertEqual(second, first)

    def test_lan_hosts_returns_without_waiting_for_slow_adapter_lookup(self) -> None:
        """回归：Windows CI 冷启动 Get-NetIPAddress 能跑好几秒，不能卡住 /api/pair/info。"""
        release = threading.Event()

        def slow_run(*args, **kwargs):
            release.wait(6.0)
            raise subprocess.TimeoutExpired("Get-NetIPAddress", 3.0)

        try:
            with mock.patch("noticehub.webapp.subprocess.run", side_effect=slow_run), mock.patch(
                "noticehub.webapp._lan_ipv4", return_value="192.168.8.10"
            ), mock.patch("noticehub.webapp.socket.getaddrinfo", return_value=[]):
                started = time.monotonic()
                hosts = webapp._lan_hosts()
                elapsed = time.monotonic() - started
            self.assertLess(elapsed, 1.0)
            self.assertEqual(hosts, ["192.168.8.10"])
        finally:
            release.set()
            thread = webapp._adapter_thread
            if thread is not None:
                thread.join(6.0)


class PairingNetworkDiagnosticsTest(unittest.TestCase):
    """手机连不上局域网 / 公网点了没反应的诊断：网卡过滤、防火墙提示、Tailscale 状态提示。"""

    def setUp(self) -> None:
        webapp._reset_adapter_cache()
        webapp._reset_firewall_cache()
        webapp._clear_tailscale_cache()  # 公网地址/状态提示缓存 30 秒，用例之间要隔离
        self.addCleanup(webapp._reset_adapter_cache)
        self.addCleanup(webapp._reset_firewall_cache)
        self.addCleanup(webapp._clear_tailscale_cache)

    def test_order_adapter_hosts_drops_virtual_adapters_and_keeps_real_ones(self) -> None:
        rows = [
            {"IPAddress": "192.168.8.20", "InterfaceAlias": "WLAN"},
            {"IPAddress": "192.168.8.10", "InterfaceAlias": "以太网"},
            {"IPAddress": "10.10.10.10", "InterfaceAlias": "USB 网卡"},
            {"IPAddress": "172.20.10.2", "InterfaceAlias": "vEthernet (WSL)"},
            {"IPAddress": "100.101.102.103", "InterfaceAlias": "Tailscale"},
            {"IPAddress": "127.0.0.1", "InterfaceAlias": "Loopback Pseudo-Interface 1"},
            {"IPAddress": "192.168.8.20", "InterfaceAlias": "WLAN"},
            "not-a-row",
        ]
        # 真实网卡（WLAN / 以太网）排前面，虚拟网卡、环回、重复地址全部丢掉。
        self.assertEqual(webapp._order_adapter_hosts(rows), ["192.168.8.20", "192.168.8.10", "10.10.10.10"])
        self.assertEqual(
            webapp._order_adapter_hosts({"IPAddress": "192.168.8.20", "InterfaceAlias": "WLAN"}),
            ["192.168.8.20"],
        )
        self.assertEqual(webapp._order_adapter_hosts(None), [])
        self.assertEqual(webapp._order_adapter_hosts("192.168.8.20"), [])

    def test_order_adapter_hosts_drops_virtual_ranges_even_with_physical_alias(self) -> None:
        # VirtualBox host-only 网卡常被改名成「以太网 4」，别名上看不出来，只能按地址段拦。
        rows = [
            {"IPAddress": "192.168.56.1", "InterfaceAlias": "以太网 4"},
            {"IPAddress": "192.168.8.20", "InterfaceAlias": "WLAN"},
        ]
        self.assertEqual(webapp._order_adapter_hosts(rows), ["192.168.8.20"])

    def test_lan_hosts_prefers_adapter_enumeration_without_fallback(self) -> None:
        with (
            mock.patch(
                "noticehub.webapp._adapter_ipv4s",
                return_value=["192.168.8.20", "192.168.8.20", "127.0.0.1"],
            ),
            mock.patch("noticehub.webapp._lan_ipv4") as lan_ipv4,
            mock.patch("noticehub.webapp._hostname_ipv4s") as hostname,
        ):
            hosts = webapp._lan_hosts()
        self.assertEqual(hosts, ["192.168.8.20"])
        lan_ipv4.assert_not_called()
        hostname.assert_not_called()

    def test_lan_hosts_falls_back_when_adapter_enumeration_is_empty(self) -> None:
        with (
            mock.patch("noticehub.webapp._adapter_ipv4s", return_value=[]),
            mock.patch("noticehub.webapp._lan_ipv4", return_value="192.168.8.10") as lan_ipv4,
            mock.patch("noticehub.webapp._hostname_ipv4s", return_value=["192.168.8.11"]) as hostname,
        ):
            hosts = webapp._lan_hosts()
        self.assertEqual(hosts, ["192.168.8.10", "192.168.8.11"])
        lan_ipv4.assert_called_once_with()
        hostname.assert_called_once_with()

    @staticmethod
    def _inbound_rule(**overrides: str) -> dict[str, str]:
        rule = {
            "rule name": "群务台 8766",
            "enabled": "Yes",
            "direction": "In",
            "action": "Allow",
            "localport": "8766",
        }
        rule.update(overrides)
        return rule

    def test_firewall_detects_enabled_inbound_allow_rule(self) -> None:
        with mock.patch("noticehub.host_actions.firewall_inbound_rules", return_value=(self._inbound_rule(),)):
            result = webapp._firewall_allows(8766)
        self.assertTrue(result["checked"])
        self.assertTrue(result["allowed"])
        self.assertEqual(result["rule"], "群务台 8766")
        self.assertIn("localport=8766", result["command"])

    def test_firewall_does_not_treat_other_ports_as_open(self) -> None:
        with mock.patch("noticehub.host_actions.firewall_inbound_rules", return_value=(self._inbound_rule(),)):
            result = webapp._firewall_allows(8767)
        self.assertTrue(result["checked"])
        self.assertFalse(result["allowed"])
        self.assertNotIn("rule", result)
        self.assertIn("localport=8767", result["command"])

    def test_firewall_ignores_disabled_and_blocking_rules(self) -> None:
        rules = (
            self._inbound_rule(enabled="No"),
            self._inbound_rule(action="Block"),
            self._inbound_rule(localport="8767"),
        )
        with mock.patch("noticehub.host_actions.firewall_inbound_rules", return_value=rules):
            result = webapp._firewall_allows(8766)
        self.assertTrue(result["checked"])
        self.assertFalse(result["allowed"])

    def test_firewall_accepts_any_port_rule(self) -> None:
        with mock.patch(
            "noticehub.host_actions.firewall_inbound_rules",
            return_value=(self._inbound_rule(localport="Any"),),
        ):
            result = webapp._firewall_allows(8766)
        self.assertTrue(result["allowed"])

    def test_firewall_reports_unchecked_when_rules_cannot_be_read(self) -> None:
        with mock.patch("noticehub.host_actions.firewall_inbound_rules", return_value=()):
            result = webapp._firewall_allows(8766)
        self.assertFalse(result["checked"])
        self.assertFalse(result["allowed"])
        self.assertIn("localport=8766", result["command"])

    def test_firewall_rule_list_is_cached_across_ports(self) -> None:
        stdout = (
            b"Rule Name: notice-hub 8766\r\n"
            b"Enabled: Yes\r\n"
            b"Direction: In\r\n"
            b"LocalPort: 8766\r\n"
            b"Action: Allow\r\n"
            b"\r\n"
        )
        completed = mock.Mock(returncode=0, stdout=stdout)
        with (
            mock.patch.object(webapp.os, "name", "nt"),
            # host_actions.is_windows() 看的是 sys.platform，不是 os.name：
            # 只 patch os.name 时这个用例在 linux/macos 上根本不跑 netsh（call_count 0）。
            mock.patch("noticehub.host_actions.is_windows", return_value=True),
            mock.patch("noticehub.webapp.subprocess.run", return_value=completed) as run,
        ):
            first = webapp._firewall_allows(8766)
            second = webapp._firewall_allows(8767)
        # 规则清单与端口无关，一次 netsh 枚举服务所有端口查询。
        self.assertEqual(run.call_count, 1)
        self.assertTrue(first["allowed"])
        self.assertFalse(second["allowed"])

    def test_tailscale_status_hint_explains_login_needed(self) -> None:
        completed = mock.Mock(returncode=0, stdout=b'{"BackendState": "NeedsLogin"}')
        with mock.patch("noticehub.webapp.subprocess.run", return_value=completed):
            hint = webapp._tailscale_status_hint("tailscale")
        self.assertIn("还没登录", hint)

    def test_tailscale_status_hint_is_silent_when_running(self) -> None:
        completed = mock.Mock(returncode=0, stdout=b'{"BackendState": "Running"}')
        with mock.patch("noticehub.webapp.subprocess.run", return_value=completed):
            self.assertEqual(webapp._tailscale_status_hint("tailscale"), "")

    def test_tailscale_status_hint_explains_unreachable_command(self) -> None:
        with mock.patch("noticehub.webapp.subprocess.run", side_effect=OSError("boom")):
            hint = webapp._tailscale_status_hint("tailscale")
        self.assertIn("没连上 Tailscale", hint)

    def test_tailscale_funnel_refuses_before_running_commands_when_not_logged_in(self) -> None:
        hint = "Tailscale 还没登录：请先打开 Tailscale 用同一个账号登录，再点一次「开通公网入口」。"
        with (
            mock.patch("noticehub.tunnel.tailscale_executable", return_value="tailscale"),
            mock.patch("noticehub.tunnel.status_hint", return_value=hint),
            mock.patch("noticehub.tunnel.subprocess.run") as run,
        ):
            result = webapp._enable_tailscale_funnel(8766)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], hint)
        run.assert_not_called()


class ApiTimeoutBehaviorTest(unittest.TestCase):
    """A1/A2/A4/A3 行为级验证。

    用本机 node 直接执行页面里的**真实** JS（api / setupGroupPull / refreshAnnouncements / 更新下载轮询），
    mock 永不返回、可中断、失败或永不推进的 fetch，断言用户实际会看到的文案与按钮状态。
    不断源码字符串：实现等价改写测试依然绿，行为改坏测试立刻红。
    """

    HARNESS_JS = r"""
const fs = require('fs');
const path = require('path');
const pagePath = process.argv[2];
// 前端已按块拆到 web_static/js/*.js：抽函数前把 page.html 与全部 js 块拼成一份全文，
// 这样每拆一块都不用再改这里（page.html 在前，indexOf 取到的是页面里最早的那份）。
const jsDir = path.join(path.dirname(pagePath), 'js');
const page = fs.readFileSync(pagePath, 'utf-8')
  + (fs.existsSync(jsDir)
    ? fs.readdirSync(jsDir).filter((n) => n.endsWith('.js')).sort()
      .map((n) => '\n' + fs.readFileSync(path.join(jsDir, n), 'utf-8')).join('')
    : '');

function extractFunction(src, name) {
  const marker = 'function ' + name + '(';
  const start = src.indexOf(marker);
  if (start < 0) throw new Error('找不到函数：' + name);
  const end = src.indexOf('\n}', start);
  if (end < 0) throw new Error('函数未以行首 } 结束：' + name);
  return src.slice(start, end + 2);
}

const MODULE_PARTS = ['friendlyError', 'api', 'setupGroupPull', 'refreshAnnouncements', 'renderAnnouncementBanner', 'renderAnnouncementFailureHint', 'getDismissedAnnouncements', 'getAnnounceModalSeen']
  .map((name) => extractFunction(page, name)).join('\n')
  // renderAnnouncementBanner 现在也读已读集合，所以把 ANNOUNCE_READ_KEY + 读取/标记函数一起带上。
  + '\n' + extractAnnounceReadBlock(page)
  // 失败提示「关掉后不再重复弹」的那个开关，真实代码里是文件级 var。
  + '\nvar announceFailureHintDismissed = false;'
  // renderAnnouncementBanner / FailureHint 还用到的两条文件级 var + 一个小工具函数。
  + '\nvar announceNoticeIds = [];'
  + "\nvar ANNOUNCE_FAILURE_NOTICE_ID = 'announce-failure';"
  + '\n' + extractFunction(page, 'persistDismissedAnnouncement');

function makeElement(id) {
  const classes = new Set();
  return {
    id: id, textContent: '', innerHTML: '', hidden: false, disabled: false, value: '', open: false, offsetWidth: 0,
    dataset: {}, style: {}, scrollOptions: null,
    classList: {
      add(c) { classes.add(c); },
      remove(c) { classes.delete(c); },
      toggle(c) { if (classes.has(c)) { classes.delete(c); return false; } classes.add(c); return true; },
      contains(c) { return classes.has(c); },
    },
    appendChild() {}, removeChild() {}, addEventListener() {}, removeEventListener() {},
    setAttribute() {}, removeAttribute() {}, focus() {},
    getBoundingClientRect() { return { top: 0, bottom: 0, left: 0, width: 0, height: 0 }; },
    scrollIntoView(options) { this.scrollOptions = options || {}; },
    querySelector() { return null; }, querySelectorAll() { return []; },
  };
}

function makeEnv(ids, options) {
  options = options || {};
  const elements = {};
  ids.forEach((id) => { elements[id] = makeElement(id); });
  const document = {
    getElementById(id) { if (!elements[id]) elements[id] = makeElement(id); return elements[id]; },
    createElement() { return makeElement(''); },
    querySelector() { return null; }, querySelectorAll() { return []; },
    addEventListener() {},
  };
  // fastTimers：setTimeout 立即同步执行，让 90 次轮询在毫秒内跑完；防护上限防止死循环。
  let fastGuard = 0;
  const fakeSetTimeout = options.fastTimers
    ? function (fn) { fastGuard += 1; if (fastGuard > 200000) throw new Error('fast timers 触发防护上限'); fn(); return fastGuard; }
    : setTimeout;
  const window = { setTimeout: fakeSetTimeout, clearTimeout, setInterval, clearInterval, location: { reload() {} } };
  const store = options.storage || {};
  const localStorage = {
    getItem(key) { return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null; },
    setItem(key, value) { store[key] = String(value); },
    removeItem(key) { delete store[key]; },
  };
  return { elements, document, window, localStorage };
}

function buildApiModule(env, seed) {
  const factory = new Function(
    'document', 'window', 'localStorage', 'token',
    'renderAnnouncementList', 'renderAnnounceModal', 'renderAnnounceBell', 'renderAnnouncePanel', 'announcementStateSeed',
    'var announcementState = announcementStateSeed;\n' + MODULE_PARTS +
    '\nreturn {api: api, setupGroupPull: setupGroupPull, refreshAnnouncements: refreshAnnouncements, renderAnnouncementBanner: renderAnnouncementBanner, state: function () { return announcementState; }};'
  );
  const noop = function () {};
  return factory(env.document, env.window, env.localStorage, 'test-token', noop, noop, noop, noop, seed === undefined ? null : seed);
}

function extractGroupPull(env, apiFn, shrinkTimeout) {
  let src = extractFunction(page, 'setupGroupPull');
  if (shrinkTimeout) {
    const shrunk = src.replace('timeoutMs: 600000', 'timeoutMs: 120');
    if (shrunk === src) throw new Error('未能把 timeoutMs: 600000 替换成测试值——webapp.py 写法变了，请同步 harness');
    src = shrunk;
  }
  const factory = new Function('document', 'window', 'api', src + '\nreturn setupGroupPull;');
  return factory(env.document, env.window, apiFn);
}

function extractUpdateModule(env, apiFn) {
  const start = page.indexOf('var updateState = null;');
  const end = page.indexOf('\nfunction setupUpdateCard', start);
  if (start < 0 || end < 0) throw new Error('找不到更新卡函数区段');
  // renderUpdateLatest 用到 page.html 里的 safeUrl，切片里没有，单独带进来（否则 done 分支会抛 safeUrl is not defined）。
  const src = extractFunction(page, 'safeUrl') + '\n' + page.slice(start, end);
  const factory = new Function('document', 'window', 'localStorage', 'api',
    src + '\nreturn {pollUpdateDownload: pollUpdateDownload, abandonUpdateDownload: abandonUpdateDownload, applyUpdateState: applyUpdateState};');
  return factory(env.document, env.window, env.localStorage, apiFn);
}

function hangForever(url, options) {
  return new Promise(function (_, reject) {
    if (options && options.signal) {
      options.signal.addEventListener('abort', function () {
        const err = new Error('The operation was aborted');
        err.name = 'AbortError';
        reject(err);
      });
    }
  });
}

function rejectOnAbort(url, options) {
  return new Promise(function (_, reject) {
    options.signal.addEventListener('abort', function () {
      const err = new Error('The operation was aborted');
      err.name = 'AbortError';
      reject(err);
    });
  });
}

function scenarioApiTimeout() {
  const env = makeEnv([]);
  const mod = buildApiModule(env);
  global.fetch = hangForever;
  return mod.api('/api/hang', {timeoutMs: 80}).then(function () {
    return {pass: false, detail: '请求没有抛错'};
  }, function (e) {
    const ok = e && e.message === '请求超时，请重试' && e.requestTimeout === true;
    return {pass: ok, detail: 'message=' + (e && e.message) + ' requestTimeout=' + (e && e.requestTimeout)};
  });
}

function scenarioApiCancel() {
  const env = makeEnv([]);
  const mod = buildApiModule(env);
  global.fetch = rejectOnAbort;
  const controller = new AbortController();
  const pending = mod.api('/api/hang', {signal: controller.signal, timeoutMs: 5000});
  setTimeout(function () { controller.abort(); }, 30);
  return pending.then(function () {
    return {pass: false, detail: '请求没有抛错'};
  }, function (e) {
    const ok = e && e.message === '已取消' && e.requestCancelled === true;
    return {pass: ok, detail: 'message=' + (e && e.message) + ' requestCancelled=' + (e && e.requestCancelled)};
  });
}

function scenarioApiSuccess() {
  const env = makeEnv([]);
  const mod = buildApiModule(env);
  global.fetch = function () {
    return Promise.resolve({ok: true, status: 200, json: function () { return Promise.resolve({ok: true, groups: []}); }});
  };
  return mod.api('/api/quick', {}).then(function (body) {
    return {pass: !!(body && body.ok === true), detail: JSON.stringify(body)};
  }, function (e) {
    return {pass: false, detail: '成功路径抛错：' + (e && e.message)};
  });
}

function scenarioGroupPullTimeout() {
  const env = makeEnv(['wx-groups', 'wx-load-groups', 'wx-cancel-groups']);
  const mod = buildApiModule(env);
  global.fetch = hangForever;
  const setupGroupPull = extractGroupPull(env, mod.api, true);
  let rendered = false;
  setupGroupPull({
    path: '/api/wechat/groups', boxId: 'wx-groups', buttonId: 'wx-load-groups', cancelButtonId: 'wx-cancel-groups',
    idleButtonText: '拉取我的群', busyButtonText: '正在拉取…', busyCopy: '正在读取微信群…',
    longWaitCopy: function (seconds) { return '已等 ' + seconds + ' 秒'; },
    render: function () { rendered = true; },
  });
  env.elements['wx-load-groups'].onclick();
  return new Promise(function (resolve) {
    setTimeout(function () {
      const box = env.elements['wx-groups'];
      const button = env.elements['wx-load-groups'];
      const cancel = env.elements['wx-cancel-groups'];
      const pass = box.textContent.indexOf('拉取失败：请求超时，请重试') >= 0
        && button.disabled === false && button.textContent === '拉取我的群'
        && cancel.hidden === true && cancel.disabled === false && rendered === false;
      resolve({pass: pass, detail: 'box=' + box.textContent + ' button.disabled=' + button.disabled + ' button.text=' + button.textContent + ' cancel.hidden=' + cancel.hidden + ' rendered=' + rendered});
    }, 500);
  });
}

function scenarioGroupPullCancel() {
  const env = makeEnv(['dt-groups', 'dt-load-groups', 'dt-cancel-groups']);
  const mod = buildApiModule(env);
  global.fetch = rejectOnAbort;
  const setupGroupPull = extractGroupPull(env, mod.api, false);
  setupGroupPull({
    path: '/api/dingtalk/groups', boxId: 'dt-groups', buttonId: 'dt-load-groups', cancelButtonId: 'dt-cancel-groups',
    idleButtonText: '拉取我的群', busyButtonText: '正在拉取…', busyCopy: '正在读取钉钉群…',
    longWaitCopy: null, render: function () {},
  });
  const button = env.elements['dt-load-groups'];
  const cancel = env.elements['dt-cancel-groups'];
  const box = env.elements['dt-groups'];
  button.onclick();
  setTimeout(function () { cancel.onclick(); }, 40);
  return new Promise(function (resolve) {
    setTimeout(function () {
      const pass = box.textContent === '已取消。'
        && button.disabled === false && button.textContent === '拉取我的群'
        && cancel.hidden === true && cancel.disabled === false;
      resolve({pass: pass, detail: 'box=' + box.textContent + ' button.disabled=' + button.disabled + ' cancel.hidden=' + cancel.hidden});
    }, 400);
  });
}

// 失败提示横幅：必须带一个和公告横幅同款样式的「关闭」，点了能关掉；下一次成功刷新恢复正常公告。
function makeTrackingEnv(ids) {
  const nodes = {};
  function makeNode(name) {
    const el = {
      id: name || '', tagName: '', type: '', className: '', hidden: false, disabled: false, value: '',
      dataset: {}, attrs: {}, children: [], listeners: {}, _text: '',
      appendChild(child) { this.children.push(child); return child; },
      removeChild() {}, remove() {}, focus() {},
      addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
      setAttribute(k, v) { this.attrs[k] = String(v); },
      getAttribute(k) { return this.attrs[k]; },
      removeAttribute(k) { delete this.attrs[k]; },
      classList: { add() {}, remove() {}, contains() { return false; }, toggle() { return false; } },
      querySelector() { return null; }, querySelectorAll() { return []; },
      scrollIntoView() {},
    };
    Object.defineProperty(el, 'textContent', {
      get() { return el._text; },
      set(v) { el._text = String(v); el.children.length = 0; },
    });
    return el;
  }
  ids.forEach((id) => { nodes[id] = makeNode(id); });
  const document = {
    getElementById(id) { if (!nodes[id]) nodes[id] = makeNode(id); return nodes[id]; },
    createElement(tag) { const n = makeNode(''); n.tagName = String(tag || '').toUpperCase(); return n; },
    querySelector() { return null; }, querySelectorAll() { return []; },
    addEventListener() {},
  };
  const window = { setTimeout, clearTimeout, setInterval, clearInterval, location: { reload() {} } };
  const store = {};
  const localStorage = {
    getItem(key) { return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null; },
    setItem(key, value) { store[key] = String(value); },
    removeItem(key) { delete store[key]; },
  };
  return { nodes, document, window, localStorage };
}

function scenarioAnnounceFailure() {
  // 公告横幅已经并进右上角悬浮通知：这里用一具假 NHNotify 接住 push/dismiss，
  // 验的是同一条行为契约——失败要出声、能关掉、关掉了不重复吵、下一次成功刷新恢复公告。
  const env = makeTrackingEnv([]);
  const pushed = [];
  const dismissed = [];
  env.window.NHNotify = {
    push(options) { pushed.push(options); return options && options.id; },
    dismiss(id) { dismissed.push(id); },
    clear() { Array.from(dismissed); },
  };
  const mod = buildApiModule(env);
  const notice = function (id) {
    const hits = pushed.filter(function (o) { return o && o.id === id; });
    return hits.length ? hits[hits.length - 1] : null;
  };
  const failurePushes = function () {
    return pushed.filter(function (o) { return o && o.id === 'announce-failure'; }).length;
  };
  global.fetch = function () { return Promise.reject(new TypeError('Failed to fetch')); };
  mod.refreshAnnouncements();
  return new Promise(function (resolve) {
    setTimeout(function () {
      const failure = notice('announce-failure');
      const shown = !!failure && String(failure.title).indexOf('公告暂时无法更新') >= 0 && failure.level === 'warn';
      // 拉取失败没有正文可看：不给「查看」动作（旧横幅也不给「详情」）。
      const noAction = !!failure && !failure.actionLabel;
      const closable = !!failure && failure.dismissible !== false && typeof failure.onDismiss === 'function';
      if (closable) failure.onDismiss(failure.id);   // 等价于用户点卡片上的 ×
      const beforeRepeat = failurePushes();
      mod.refreshAnnouncements();                    // 同一场故障再刷一次
      setTimeout(function () {
        const repeated = failurePushes() > beforeRepeat;
        // 下一次成功刷新：撤掉失败提示，换成真实公告通知（不被「已关闭」永久压住）。
        global.fetch = function () {
          return Promise.resolve({ ok: true, status: 200, json: function () {
            return Promise.resolve({ ok: true, items: [{ id: 'n1', title: '公告标题', body: '正文', level: 'info', date: '2026-10-30' }] });
          } });
        };
        mod.refreshAnnouncements();
        setTimeout(function () {
          const real = notice('announce-n1');
          const recovered = dismissed.indexOf('announce-failure') >= 0
            && !!real && String(real.title).indexOf('公告标题') >= 0 && real.priority === true;
          const checks = [
            ['失败提示以悬浮通知出现（warn 级）', shown],
            ['失败通知不给「查看」动作', noAction],
            ['失败通知可以点 × 关闭', closable],
            ['点关闭后不再重复推同一条', !repeated],
            ['下一次成功刷新撤掉失败提示并恢复真实公告', recovered],
          ];
          const failed = checks.filter(function (row) { return !row[1]; });
          resolve({
            pass: failed.length === 0,
            detail: failed.length ? failed.map(function (row) { return row[0]; }).join('；')
              + ' | pushed=' + pushed.map(function (o) { return o && o.id; }).join(',')
              + ' | dismissed=' + dismissed.join(',')
              : '失败提示 5 项检查全部通过',
          });
        }, 120);
      }, 120);
    }, 120);
  });
}

// 铃铛未读状态：真正影响徽标/横幅的那套逻辑（localStorage 读写、排序、上限、脏数据）。
function extractAnnounceReadBlock(src) {
  const start = src.indexOf('var ANNOUNCE_READ_KEY');
  if (start < 0) throw new Error('找不到 ANNOUNCE_READ_KEY');
  const markEnd = src.indexOf('\n}', src.indexOf('function markAnnounceRead('));
  if (markEnd < 0) throw new Error('markAnnounceRead 未以行首 } 结束');
  return src.slice(start, markEnd + 2);
}

// 面板定位跟铃铛走：positionAnnouncePanel 纯函数（不碰真 DOM），四边 clamp 进视口。
function scenarioAnnouncePanelPosition() {
  const page2 = page;
  const src = extractFunction(page2, 'positionAnnouncePanel');
  const factory = new Function('window', 'document', src + '\nreturn positionAnnouncePanel;');
  const positions = [];
  const panel = {offsetWidth: 420, offsetHeight: 320, style: {}};
  const win = {innerWidth: 1280, innerHeight: 800};
  const doc = {};
  const fn = factory(win, doc);
  // 铃铛在右上角（右缘 886，底 55）：面板应贴铃铛正下方、右对齐。
  fn(panel, {bottom: 55, right: 886}, {viewportWidth: 1280, viewportHeight: 800});
  positions.push(['top=bell.bottom+8', panel.style.top === '63px', panel.style.top]);
  positions.push(['right=viewport-bell.right', panel.style.right === '394px', panel.style.right]);
  // 铃铛太靠下：top clamp 到视口内（视口 800，面板 320 → 上限 472）。
  fn(panel, {bottom: 900, right: 10}, {viewportWidth: 1280, viewportHeight: 800});
  positions.push(['top clamped <= viewport-h-panel-8', panel.style.top === '472px', panel.style.top]);
  // 右缘太靠右（铃铛右 = 视口右）：right clamp 到 ≥8。
  fn(panel, {bottom: 100, right: 1280}, {viewportWidth: 1280, viewportHeight: 800});
  positions.push(['right clamped >= 8', panel.style.right === '8px', panel.style.right]);
  const failed = positions.filter(function (row) { return !row[1]; });
  return {
    pass: failed.length === 0,
    detail: failed.length ? failed.map(function (row) { return row[0] + '=' + row[2]; }).join('；') : '定位 3 项检查全部通过',
  };
}

// 公告拉取失败不清空旧列表：保留上次成功的 items，只把 ok 标成 false。
function scenarioAnnounceFailureKeepsItems() {
  const env = makeEnv([]);
  const pushed = [];
  env.window.NHNotify = {
    push(options) { pushed.push(options); return options && options.id; },
    dismiss() {},
  };
  const seed = {ok: true, items: [
    {id: 'a1', title: '公告一', body: '正文一', level: 'info', date: '2026-11-01'},
    {id: 'a2', title: '公告二', body: '正文二', level: 'info', date: '2026-11-02'},
    {id: 'a3', title: '公告三', body: '正文三', level: 'info', date: '2026-11-03'},
  ]};
  const mod = buildApiModule(env, seed);
  global.fetch = function () { return Promise.reject(new TypeError('Failed to fetch')); };
  mod.refreshAnnouncements();
  return new Promise(function (resolve) {
    setTimeout(function () {
      const state = mod.state();
      const kept = Array.isArray(state.items) && state.items.length === 3
        && state.items[0].id === 'a1' && state.items[2].id === 'a3';
      const notOk = state.ok === false;
      if (!kept || !notOk) {
        resolve({pass: false, detail: '失败后 items=' + JSON.stringify(state.items) + ' ok=' + state.ok});
        return;
      }
      // 首次加载就失败（从没有过数据）：落到空态，不给假列表。
      const env2 = makeEnv([]);
      const mod2 = buildApiModule(env2);
      mod2.refreshAnnouncements();
      setTimeout(function () {
        const state2 = mod2.state();
        const empty = Array.isArray(state2.items) && state2.items.length === 0 && state2.ok === false;
        resolve({pass: empty, detail: empty ? '失败保留旧公告 3 条 + 首次失败落空态' : '首次失败 state=' + JSON.stringify(state2)});
      }, 120);
    }, 120);
  });
}

function buildAnnounceReadModule(env, items) {
  const code = extractAnnounceReadBlock(page) + '\n'
    + extractFunction(page, 'announceItems') + '\n'
    + extractFunction(page, 'announcementsUnreadCount');
  const factory = new Function(
    'document', 'window', 'localStorage', 'debugItems',
    'var announcementState = {ok: true, items: debugItems};\n' + code
    + '\nreturn {read: getAnnounceRead, mark: markAnnounceRead, items: announceItems, unread: announcementsUnreadCount};'
  );
  return factory(env.document, env.window, env.localStorage, items);
}

function scenarioBellReadState() {
  const items = [
    {id: 'old', title: '旧公告', date: '2026-10-01'},
    {id: 'new', title: '新公告', date: '2026-10-22'},
    {id: 'mid', title: '中间公告', date: '2026-10-10'},
  ];
  const env = makeEnv([], {storage: {}});
  const mod = buildAnnounceReadModule(env, items);
  const problems = [];
  if (mod.unread() !== 3) problems.push('初始未读=' + mod.unread());
  const order = mod.items().map(function (item) { return item.id; }).join(',');
  if (order !== 'new,mid,old') problems.push('倒序=' + order);
  mod.mark(['new']);
  mod.mark(['new']);  // 重复标记不能重复计
  if (mod.unread() !== 2) problems.push('标一条后未读=' + mod.unread());
  if (mod.read().join(',') !== 'new') problems.push('已读集合=' + mod.read().join(','));
  const stored = env.localStorage.getItem('nh_ann_read');
  if (stored !== JSON.stringify(['new'])) problems.push('持久化=' + stored);
  const many = [];
  for (let i = 0; i < 250; i += 1) many.push('id-' + i);
  mod.mark(many);
  const capped = mod.read();
  if (capped.length !== 200 || capped[capped.length - 1] !== 'id-249') {
    problems.push('上限=' + capped.length + '/' + capped[capped.length - 1]);
  }
  env.localStorage.setItem('nh_ann_read', 'not-json');
  let dirty = null;
  try { dirty = mod.unread(); } catch (error) { problems.push('脏数据抛异常：' + error); }
  if (dirty !== null && dirty !== 3) problems.push('脏数据未读=' + dirty);
  return {pass: problems.length === 0, detail: problems.join('; ') || 'ok'};
}

// ===== 公网入口：全站单套实现（结构契约）=====
// 旧 pair.js 那套独立状态机/轮询已删除：不再有 TUNNEL_POLL_MS / pair-tunnel / pair-enable。
// 全站公网 UI 只剩 settings.js 的双通道实现，DOM 挂在「手机 App」卡内；
// 行为级覆盖由 tests/test_ui_browser.py 的三个 tunnel 用例承担（两段式关闭 / 通道切换 / 轮询到 ready）。
function scenarioTunnelPollStopsAtReady() {
  const problems = [];
  const pairStart = page.indexOf('var pairTimer = null;');
  const pairEnd = page.indexOf('function pairRow(', pairStart);
  if (pairStart < 0 || pairEnd < 0) return {pass: false, detail: '找不到 pair.js 区段'};
  const pair = page.slice(pairStart, pairEnd);
  ['TUNNEL_POLL_MS', 'TUNNEL_MAX_POLL_MS', 'pair-tunnel', 'pair-enable',
    'pollTunnelOnce', 'scheduleTunnelPoll', 'renderTunnelStatus', '/api/tunnel/start'].forEach(function (token) {
    if (pair.indexOf(token) >= 0) problems.push('pair.js 仍残留公网实现：' + token);
  });
  const cardCount = (page.match(/id="tunnel-card"/g) || []).length;
  if (cardCount !== 1) problems.push('id="tunnel-card" 数量=' + cardCount);
  const syncStart = page.indexOf('id="sync-title"');
  const syncEnd = page.indexOf('root.appendChild(syncBox)', syncStart);
  const sync = page.slice(syncStart, syncEnd);
  if (sync.indexOf('sync-public-block') >= 0 || sync.indexOf('tunnel-compare') >= 0
      || sync.indexOf('tunnel-card') >= 0) problems.push('日历卡仍残留公网区块');
  if (sync.indexOf('id="sync-public-open"') < 0) problems.push('日历卡缺 #sync-public-open');
  const mobileStart = page.indexOf('function pairCard()');
  const mobileEnd = page.indexOf('function pollPairInfo()', mobileStart);
  const mobile = page.slice(mobileStart, mobileEnd);
  if (mobile.indexOf('id="sync-public-block"') < 0) problems.push('App 卡里没有公网区块');
  if (mobile.indexOf('id="tunnel-card"') < 0) problems.push('App 卡里没有 #tunnel-card');
  if (mobile.indexOf('tunnel-compare-row') < 0) problems.push('App 卡里没有六行对比');
  // 跳转只有一处实现：日历卡的 #sync-public-open 复用 scrollToPublicBlock()，
  // 后者是唯一查 [data-setting-anchor="mobile"] 并滚过去的地方（托管卡的 brief 已删）。
  const jumpMarker = 'var card = document.querySelector(' + "'" + '[data-setting-anchor="mobile"]' + "'" + ');';
  const jumpCount = page.split(jumpMarker).length - 1;
  if (jumpCount !== 1) problems.push('跳转实现应恰好 1 处，实际 ' + jumpCount + ' 处');
  if (page.indexOf("getElementById('sync-public-open')") < 0) problems.push('日历卡跳转按钮未接线');
  if (page.indexOf('去「iPhone 日历订阅」卡开通公网入口') >= 0) problems.push('brief 仍指向日历卡');
  if (page.indexOf('公网入口在「手机 App」卡里管理') >= 0) problems.push('托管卡仍残留公网入口 brief');
  return {pass: problems.length === 0, detail: problems.join('; ') || '公网单套实现结构契约 11 项检查全部通过'};
}

// settings.js 双通道实现：错误态露重试、危险地址过滤、切走设置页停轮询、两段式关闭、带 provider 开通。
function scenarioTunnelErrorShowsRetry() {
  const problems = [];
  const a = page.indexOf('// 公网访问：双通道');
  const b = page.indexOf('function setHostingResult', a);
  if (a < 0 || b < 0) return {pass: false, detail: '找不到 settings.js 公网实现区段'};
  const impl = page.slice(a, b);
  const required = [
    ['id="tunnel-retry"', '错误态重试按钮'],
    ["getElementById('tunnel-retry')", '重试按钮接线'],
    ['id="tunnel-close"', '两段式关闭按钮'],
    ['确认关闭？（4 秒内再点一次）', '两段式关闭确认文案'],
    ['/api/tunnel/stop', '关闭接口'],
    ['/api/tunnel/start', '开通接口'],
    ['provider: provider', '开通带通道参数'],
    ['confirm: true', '写操作 confirm'],
    ['safeUrl(', '危险地址过滤'],
    ["document.body.dataset.tab !== 'settings'", '切走设置页停轮询'],
    ['stopTunnelPolling()', '停止轮询收口'],
    ['这个地址每次重启会变', 'Cloudflare 地址提示'],
    ['tunnel-provider-', '通道按钮 id 前缀'],
    ['Cloudflare（不用注册）', 'Cloudflare 通道定义'],
    ['Tailscale（固定地址）', 'Tailscale 通道定义']
  ];
  required.forEach(function (item) {
    if (impl.indexOf(item[0]) < 0) problems.push('公网实现缺：' + item[1] + '（' + item[0] + '）');
  });
  if (page.indexOf('id="tunnel-card"') < 0) problems.push('全站没有 #tunnel-card');
  return {pass: problems.length === 0, detail: problems.join('; ') || '公网双通道实现 16 项检查全部通过'};
}

function scenarioUpdatePollTimeout() {
  const env = makeEnv(['update-status', 'update-apply', 'update-abandon'], {fastTimers: true});
  const fetches = {count: 0};
  global.fetch = function () {
    fetches.count += 1;
    return Promise.resolve({ok: true, status: 200, json: function () { return Promise.resolve({ok: true, download: {state: 'downloading', done: 0, total: 100}}); }});
  };
  const mod = buildApiModule(env);
  const updates = extractUpdateModule(env, mod.api);
  updates.pollUpdateDownload();
  return new Promise(function (resolve) { setTimeout(resolve, 100); }).then(function () {
    const status = env.elements['update-status'];
    const apply = env.elements['update-apply'];
    const abandon = env.elements['update-abandon'];
    const settled = fetches.count;
    // done 永远 0：第 1 次算有进展（lastDone 从 -1 起步），之后 90 次无进展触发上限。
    const baseOk = fetches.count === 91
      && status.textContent.indexOf('下载超时') >= 0
      && apply.disabled === false
      && abandon.hidden === true;
    return new Promise(function (resolve) { setTimeout(resolve, 150); }).then(function () {
      return {
        pass: baseOk && fetches.count === settled,
        detail: 'count=' + fetches.count + ' status=' + status.textContent
          + ' apply.disabled=' + apply.disabled + ' abandon.hidden=' + abandon.hidden,
      };
    });
  });
}

function scenarioUpdateAbandonStopsPolling() {
  const env = makeEnv(['update-status', 'update-apply', 'update-abandon']);
  const fetches = {count: 0};
  global.fetch = function () {
    fetches.count += 1;
    const done = fetches.count * 10;  // 持续有进展：不会触发无进展上限，全靠主动放弃来停。
    return Promise.resolve({ok: true, status: 200, json: function () { return Promise.resolve({ok: true, download: {state: 'downloading', done: done, total: 1000000}}); }});
  };
  const mod = buildApiModule(env);
  const updates = extractUpdateModule(env, mod.api);
  updates.pollUpdateDownload();
  return new Promise(function (resolve) { setTimeout(resolve, 60); }).then(function () {
    const status = env.elements['update-status'];
    const apply = env.elements['update-apply'];
    const abandon = env.elements['update-abandon'];
    if (status.textContent.indexOf('正在下载') < 0 || abandon.hidden !== false) {
      return {pass: false, detail: '轮询中状态不对：status=' + status.textContent + ' abandon.hidden=' + abandon.hidden};
    }
    updates.abandonUpdateDownload();
    const settled = fetches.count;
    const okAfter = status.textContent.indexOf('已放弃下载') >= 0 && apply.disabled === false && abandon.hidden === true;
    // 900ms 大于一个轮询周期：若取消没真正清掉 pending 定时器，count 会涨。
    return new Promise(function (resolve) { setTimeout(resolve, 900); }).then(function () {
      return {
        pass: okAfter && fetches.count === settled,
        detail: 'settled=' + settled + ' now=' + fetches.count + ' status=' + status.textContent
          + ' apply.disabled=' + apply.disabled + ' abandon.hidden=' + abandon.hidden,
      };
    });
  });
}

// ===== W6：cURL 粘贴提取与失败引导（行为级）=====
// extractFn 按行锚定提取：单行函数取整行、多行函数取到行首 }。
// 不用花括号扫描器——被提取函数的正则里含引号字符，扫描器会被带偏。
const CURL_SAMPLE_COOKIE = '_did=web_1; JSESSIONID=abc';

function extractFn(src, name) {
  const marker = 'function ' + name + '(';
  const start = src.indexOf(marker);
  if (start < 0) throw new Error('找不到函数：' + name);
  const lineEnd = src.indexOf('\n', start);
  const firstLine = (lineEnd >= 0 ? src.slice(start, lineEnd) : src.slice(start)).trimEnd();
  if (firstLine.endsWith('}')) return firstLine;
  const end = src.indexOf('\n}', start);
  if (end >= 0) return src.slice(start, end + 2);
  throw new Error('无法提取函数：' + name);
}

function buildExtractCookie() {
  return new Function(extractFn(page, 'extractCookieFromCurl') + '\nreturn extractCookieFromCurl;')();
}

function buildPasteHandler(env) {
  const src = extractFn(page, 'extractCookieFromCurl') + '\n' + extractFn(page, 'handleCurlPaste');
  return new Function('document', src + '\nreturn handleCurlPaste;')(env.document);
}

function pasteEvent(text) {
  return {
    clipboardData: { getData: function () { return text; } },
    prevented: false,
    preventDefault: function () { this.prevented = true; },
  };
}

function scenarioCurlVariants() {
  const extract = buildExtractCookie();
  const samples = [
    "curl 'https://i.chaoxing.com/nd/home?m=true' -H 'cookie: " + CURL_SAMPLE_COOKIE + "' -H 'accept: */*'",
    'curl "https://i.chaoxing.com/nd/home" -H "cookie: ' + CURL_SAMPLE_COOKIE + '"',
    "curl 'https://i.chaoxing.com/api/aspace/list' --header 'Cookie: " + CURL_SAMPLE_COOKIE + "'",
  ];
  const results = samples.map(function (s) { return extract(s); });
  const pass = results.every(function (r) {
    return r && r.looksLikeCurl === true && r.found === true && r.cookie === CURL_SAMPLE_COOKIE;
  });
  return { pass: pass, detail: JSON.stringify(results) };
}

function scenarioCurlPasteFillsEmpty() {
  const env = makeEnv(['cx-manual-cred', 'cx-manual-cred-result']);
  const handle = buildPasteHandler(env);
  const event = pasteEvent("curl 'https://i.chaoxing.com/x' -H 'cookie: " + CURL_SAMPLE_COOKIE + "'");
  handle(event);
  const input = env.elements['cx-manual-cred'];
  const result = env.elements['cx-manual-cred-result'];
  const pass = event.prevented === true && input.value === CURL_SAMPLE_COOKIE
    && result.textContent.indexOf('已从 cURL 中提取出登录凭证，请确认后保存') >= 0;
  return { pass: pass, detail: 'prevented=' + event.prevented + ' value=' + input.value + ' result=' + result.textContent };
}

function scenarioCurlPasteNoCookieHeader() {
  const env = makeEnv(['cx-manual-cred', 'cx-manual-cred-result']);
  const handle = buildPasteHandler(env);
  const noHeader = pasteEvent("curl 'https://i.chaoxing.com/nd/home' -H 'accept: */*'");
  handle(noHeader);
  const input = env.elements['cx-manual-cred'];
  const result = env.elements['cx-manual-cred-result'];
  const plain = pasteEvent('这不是 curl，只是普通文本');
  handle(plain);
  const pass = noHeader.prevented === true && plain.prevented === false
    && input.value === '' && result.textContent.indexOf('这段内容里没有登录凭证') >= 0;
  return { pass: pass, detail: 'noHeader.prevented=' + noHeader.prevented + ' plain.prevented=' + plain.prevented
    + ' value=[' + input.value + '] result=' + result.textContent };
}

function scenarioCurlPasteKeepsExisting() {
  const env = makeEnv(['cx-manual-cred', 'cx-manual-cred-result']);
  env.elements['cx-manual-cred'].value = 'old=1; stay=2';
  const handle = buildPasteHandler(env);
  const event = pasteEvent("curl 'https://i.chaoxing.com/x' -H 'cookie: " + CURL_SAMPLE_COOKIE + "'");
  handle(event);
  const input = env.elements['cx-manual-cred'];
  const result = env.elements['cx-manual-cred-result'];
  const pass = event.prevented === true && input.value === 'old=1; stay=2'
    && result.textContent.indexOf('没有自动覆盖') >= 0;
  return { pass: pass, detail: 'value=' + input.value + ' result=' + result.textContent };
}

function scenarioDecryptFailedGuidance() {
  const src = ['cxFailureNote', 'chaoxingCategoryHelp', 'cxBrowserName', 'cxManualNextStep']
    .map(function (name) { return extractFn(page, name); }).join('\n');
  const note = new Function(src + '\nreturn cxFailureNote;')();
  const decrypt = note('decrypt_failed', 'Edge', '请改用另一个受支持浏览器登录学习通后重试；如果仍失败，可用“手工粘贴 Cookie”。');
  const other = note('no_chaoxing_cookie', 'Edge', '请用 Edge 打开 https://i.chaoxing.com/ 并登录 chaoxing.com。');
  const pass = decrypt.indexOf('F12') >= 0 && decrypt.indexOf('Copy as cURL') >= 0
    && decrypt.indexOf('App-Bound') >= 0 && decrypt.indexOf('复制为 cURL') >= 0
    && decrypt.indexOf('不是你的配置问题') >= 0
    && decrypt.indexOf('请改用另一个受支持浏览器') < 0
    && other.indexOf('请用 Edge 打开 https://i.chaoxing.com/') >= 0;
  return { pass: pass, detail: 'decrypt=[' + decrypt + '] other=[' + other + ']' };
}

function scenarioManualSavePostsCookie() {
  const marker = "var manualCookieButton=document.getElementById('cx-manual-cred-save');";
  const start = page.indexOf(marker);
  if (start < 0) return { pass: false, detail: '找不到手动保存接线代码' };
  const tail = page.slice(start);
  const endMatch = /function\(\)\{manualCookieButton\.disabled=false;\}\);\r?\n\};/.exec(tail);
  if (!endMatch) return { pass: false, detail: '找不到接线块结尾' };
  const block = tail.slice(0, endMatch.index + endMatch[0].length);
  const env = makeEnv(['cx-manual-cred', 'cx-manual-cred-save', 'cx-manual-cred-result']);
  const calls = [];
  const apiMock = function (path, options) {
    calls.push({ path: path, body: options && options.body ? String(options.body) : '' });
    if (path === '/api/chaoxing/cookie/validate') return Promise.resolve({ ok: true, result: '有效' });
    return Promise.resolve({ ok: true });
  };
  const factory = new Function('document', 'api', 'refreshChaoxingCookie', block + '\n');
  factory(env.document, apiMock, function () {});
  const input = env.elements['cx-manual-cred'];
  const result = env.elements['cx-manual-cred-result'];
  const button = env.elements['cx-manual-cred-save'];
  if (typeof button.onclick !== 'function') return { pass: false, detail: '接线后 onclick 仍未设置' };
  input.value = '';
  button.onclick();
  if (result.textContent !== '请粘贴学习通登录凭证。') return { pass: false, detail: '空输入分支：' + result.textContent };
  input.value = CURL_SAMPLE_COOKIE;
  button.onclick();
  return new Promise(function (resolve) {
    setTimeout(function () {
      const posted = calls.length >= 1 && calls[0].path === '/api/settings'
        && JSON.parse(calls[0].body || '{}').chaoxing_cookie === CURL_SAMPLE_COOKIE;
      const validated = calls.length >= 2 && calls[1].path === '/api/chaoxing/cookie/validate';
      const pass = posted && validated && input.value === ''
        && result.textContent === '登录凭证已保存，校验通过。' && button.disabled === false;
      resolve({ pass: pass, detail: 'calls=' + JSON.stringify(calls) + ' result=' + result.textContent + ' disabled=' + button.disabled });
    }, 80);
  });
}

// ===== P0-1：防火墙一键放行脚本（行为级）=====
// 后端 /api/pair/info 的 firewall 是 host_actions.firewall_allows() 的原始结果：
// {checked, allowed, port, command}。checked=false 只代表「非提权读不到规则清单」。
function renderFirewallForScenario(firewall, seed) {
  const env = makeEnv(['pair-firewall', 'pair-firewall-text', 'pair-firewall-cmd', 'pair-firewall-allow']);
  if (seed && seed.allowDisabled) env.elements['pair-firewall-allow'].disabled = true;
  const src = extractFunction(page, 'renderFirewallPanel');
  const run = new Function('document', src + '\nreturn renderFirewallPanel;')(env.document);
  run(firewall);
  return env;
}

function scenarioFirewallRenderStoresPort() {
  const env = renderFirewallForScenario({
    checked: true, allowed: false, port: 18766,
    command: 'netsh advfirewall firewall add rule name=notice-hub'
  });
  const box = env.elements['pair-firewall'];
  const text = env.elements['pair-firewall-text'].textContent;
  const pass = box.dataset.port === '18766' && box.hidden === false
    && text.indexOf('还没放行端口 18766') >= 0
    && env.elements['pair-firewall-cmd'].textContent.indexOf('netsh advfirewall') >= 0;
  return { pass: pass, detail: 'dataset.port=' + box.dataset.port + ' hidden=' + box.hidden + ' text=' + text };
}

// 首次（非提权、还没有结果文件）checked=false：入口必须露出来，提示语说「要管理员权限才能确认」，
// 不能把「没查成」说成「确认未放行」；已放行（allowed=true）时整块收起。
function scenarioFirewallRenderUncheckedKeepsEntry() {
  const unchecked = renderFirewallForScenario(
    { checked: false, allowed: false, port: 18766, command: 'netsh advfirewall firewall add rule name=notice-hub' },
    { allowDisabled: true });
  const box = unchecked.elements['pair-firewall'];
  const text = unchecked.elements['pair-firewall-text'].textContent;
  const allowEnabled = unchecked.elements['pair-firewall-allow'].disabled === false;
  const uncheckedWording = text.indexOf('管理员权限才能确认') >= 0 && text.indexOf('还没放行') < 0;
  const granted = renderFirewallForScenario({ checked: true, allowed: true, port: 18766 });
  const hiddenWhenAllowed = granted.elements['pair-firewall'].hidden === true;
  const pass = box.hidden === false && allowEnabled && uncheckedWording && hiddenWhenAllowed;
  return { pass: pass, detail: 'hidden=' + box.hidden + ' allowEnabled=' + allowEnabled
    + ' allowedHidden=' + hiddenWhenAllowed + ' text=' + text };
}

// ===== issue #4：网页里直接填 NapCat 目录（不改 .env）=====
function scenarioNapcatDirNormalize() {
  const normalize = new Function(extractFunction(page, 'normalizeNapcatDir') + '\nreturn normalizeNapcatDir;')();
  const cases = [
    ['"D:\\NapCat\\qqnt"', 'D:\\NapCat\\qqnt'],
    ["'D:/NapCat/'", 'D:\\NapCat\\'],
    ['D:/NapCat/x', 'D:\\NapCat\\x'],
    ['\\\\SERVER\\share\\napcat', '\\\\SERVER\\share\\napcat'],
    ['/opt/napcat', '/opt/napcat'],
    ['  D:\\NapCat  ', 'D:\\NapCat'],
    ['', '']
  ];
  const bad = [];
  cases.forEach(function (item) {
    const got = normalize(item[0]);
    if (got !== item[1]) bad.push(JSON.stringify(item[0]) + '→' + JSON.stringify(got) + '，期望 ' + JSON.stringify(item[1]));
  });
  return { pass: bad.length === 0, detail: bad.join(' | ') || '全部通过' };
}

// 保存成功但没认出引擎时，输入框不能被清空（会被用户当成「没保存上」）。
function scenarioNapcatDirSaveKeepsTypedPath() {
  const env = makeEnv(['napcat-dir', 'napcat-dir-save', 'napcat-dir-result']);
  env.elements['napcat-dir'].value = '"D:/NapCat"';
  const calls = [];
  const setupApiMock = function (path, options) {
    calls.push({ path: path, body: options && options.body ? String(options.body) : '' });
    if (path === '/api/settings') return Promise.resolve({ ok: true, napcat_dir: 'D:\\NapCat' });
    return Promise.resolve({ ok: true, napcat_root: '' });
  };
  const src = extractFunction(page, 'normalizeNapcatDir') + '\n' + extractFunction(page, 'saveNapcatDir');
  const run = new Function('document', 'setupApi', 'checkSetup', src + '\nreturn saveNapcatDir;')(
    env.document, setupApiMock, function () {});
  run();
  return new Promise(function (resolve) {
    setTimeout(function () {
      const input = env.elements['napcat-dir'];
      const result = env.elements['napcat-dir-result'];
      const posted = calls.length >= 1 && calls[0].path === '/api/settings'
        && JSON.parse(calls[0].body || '{}').napcat_dir === 'D:\\NapCat';
      const pass = posted && input.value === 'D:\\NapCat'
        && result.textContent.indexOf('没找到 NapCat 引擎') >= 0
        && env.elements['napcat-dir-save'].disabled === false;
      resolve({ pass: pass, detail: 'posted=' + posted + ' value=' + input.value
        + ' result=' + result.textContent + ' calls=' + calls.map(function (c) { return c.path; }).join(',') });
    }, 80);
  });
}

// ===== issue #5：微信 4.1.x 手动密钥引导 =====
function scenarioWechatKeySaveValidatesAndPosts() {
  const env = makeEnv(['wx-key', 'wx-key-save', 'wx-key-status']);
  const calls = [];
  const apiMock = function (path, options) {
    calls.push({path: path, body: options && options.body ? String(options.body) : ''});
    return Promise.resolve({ok: true});
  };
  const src = ['normalizeWechatKey', 'wechatKeyProblem', 'setWechatKeyStatus', 'saveWechatKey']
    .map(function (name) { return extractFunction(page, name); }).join('\n');
  const run = new Function('document', 'api', src + '\nreturn saveWechatKey;')(env.document, apiMock);
  // ① 位数不对：不发请求，直接说清楚
  env.elements['wx-key'].value = 'abcd';
  run();
  const badText = env.elements['wx-key-status'].textContent;
  const noPost = calls.length === 0;
  // ② 带空格 + 大写的合法密钥：归一化成 64 位小写再提交
  env.elements['wx-key'].value = ' 0123456789ABCDEF0123456789abcdef0123456789ABCDEF0123456789abcdef ';
  run();
  return new Promise(function (resolve) {
    setTimeout(function () {
      const body = calls.length === 1 ? JSON.parse(calls[0].body || '{}') : {};
      const posted = calls.length === 1 && calls[0].path === '/api/settings'
        && body.wechat_key === '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef';
      const cleared = env.elements['wx-key'].value === '';
      const statusText = env.elements['wx-key-status'].textContent;
      // 手动密钥保存会 reload reader，立刻生效；本轮删掉了「重启群务台后生效」这句误导文案，
      // 所以既要认新文案，也要守住「不许再让用户去重启」。
      const okText = statusText.indexOf('已保存并生效') >= 0 && statusText.indexOf('重启') < 0;
      resolve({
        pass: noPost && badText.indexOf('64 位十六进制') >= 0 && posted && cleared && okText,
        detail: 'bad=[' + badText + '] noPost=' + noPost + ' posted=' + posted
          + ' input=[' + env.elements['wx-key'].value + '] status=' + env.elements['wx-key-status'].textContent
      });
    }, 60);
  });
}

// 后端还没支持 wechat_key 时（现版本会回「没有可保存的键」）：必须说清楚是版本问题，别让人以为密钥填错了。
function scenarioWechatKeySaveExplainsUnsupportedBackend() {
  const env = makeEnv(['wx-key', 'wx-key-save', 'wx-key-status']);
  const apiMock = function () { return Promise.reject(new Error('没有可保存的键')); };
  const src = ['normalizeWechatKey', 'wechatKeyProblem', 'setWechatKeyStatus', 'saveWechatKey']
    .map(function (name) { return extractFunction(page, name); }).join('\n');
  const run = new Function('document', 'api', src + '\nreturn saveWechatKey;')(env.document, apiMock);
  env.elements['wx-key'].value = '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef';
  run();
  return new Promise(function (resolve) {
    setTimeout(function () {
      const status = env.elements['wx-key-status'].textContent;
      const pass = status.indexOf('还不能在页面里保存密钥') >= 0
        && env.elements['wx-key'].value !== ''
        && env.elements['wx-key-save'].disabled === false;
      resolve({pass: pass, detail: 'status=' + status + ' inputKept=' + (env.elements['wx-key'].value !== '')});
    }, 60);
  });
}

function scenarioFirewallScriptWiringUsesDataset() {
  const start = page.indexOf('var firewallScriptButton = document.getElementById(\'pair-firewall-script\');');
  if (start < 0) return { pass: false, detail: '找不到下载按钮接线' };
  const endMarker = 'downloadFirewallScript(box && box.dataset ? box.dataset.port : \'\');\n  });';
  const end = page.indexOf(endMarker, start);
  if (end < 0) return { pass: false, detail: '找不到接线结尾' };
  const block = page.slice(start, end + endMarker.length);
  const listeners = {};
  const box = { dataset: { port: '18766' } };
  const btn = { addEventListener: function (type, fn) { listeners[type] = fn; } };
  const fakeDoc = { getElementById: function (id) { return id === 'pair-firewall-script' ? btn : (id === 'pair-firewall' ? box : null); } };
  let got = null;
  const run = new Function('document', 'downloadFirewallScript', block + '\n');
  run(fakeDoc, function (port) { got = port; });
  if (typeof listeners.click !== 'function') return { pass: false, detail: 'click 监听未挂上' };
  listeners.click();
  const pass = got === '18766';
  return { pass: pass, detail: 'download 收到的端口=' + got };
}

function scenarioFirewallScriptBlobAsciiPort() {
  const env = makeEnv(['pair-firewall-script-hint']);
  const origCreate = env.document.createElement.bind(env.document);
  env.document.createElement = function (tag) {
    const el = origCreate(tag);
    el.click = function () { el.clicked = true; };
    return el;
  };
  env.document.body = { appendChild: function () {}, removeChild: function () {} };
  let captured = null;
  function FakeBlob(parts, opts) {
    captured = { text: parts.join(''), type: (opts && opts.type) || '' };
  }
  const fakeURL = { createObjectURL: function () { return 'blob:fake'; }, revokeObjectURL: function () {} };
  const fakeWindow = { setTimeout: function () {} };
  const src = extractFn(page, 'buildFirewallCmd') + '\n' + extractFn(page, 'downloadFirewallScript');
  const run = new Function('document', 'Blob', 'URL', 'window', src + '\nreturn downloadFirewallScript;');
  const dl = run(env.document, FakeBlob, fakeURL, fakeWindow);
  dl('18766');
  if (!captured) return { pass: false, detail: 'Blob 未生成' };
  const asciiOnly = !/[^\x00-\x7F]/.test(captured.text);
  const hint = env.elements['pair-firewall-script-hint'];
  const pass = captured.text.indexOf('localport=18766') >= 0
    && captured.text.indexOf('name="notice-hub 18766"') >= 0
    && captured.text.indexOf('@echo off') === 0
    && captured.text.indexOf('pause') >= 0
    && asciiOnly
    && captured.type.indexOf('us-ascii') >= 0
    && hint.dataset.state === 'ok';
  return { pass: pass, detail: 'ascii=' + asciiOnly + ' type=' + captured.type + ' hint=' + hint.textContent + ' text=' + JSON.stringify(captured.text) };
}

function scenarioFirewallScriptDownloadFailureHint() {
  const env = makeEnv(['pair-firewall-script-hint']);
  const origCreate = env.document.createElement.bind(env.document);
  env.document.createElement = function (tag) {
    const el = origCreate(tag);
    el.click = function () {};
    return el;
  };
  env.document.body = { appendChild: function () {}, removeChild: function () {} };
  function FakeBlob() {}
  const fakeURL = { createObjectURL: function () { throw new Error('blocked by browser'); }, revokeObjectURL: function () {} };
  const fakeWindow = { setTimeout: function () {} };
  const src = extractFn(page, 'buildFirewallCmd') + '\n' + extractFn(page, 'downloadFirewallScript');
  const run = new Function('document', 'Blob', 'URL', 'window', src + '\nreturn downloadFirewallScript;');
  const dl = run(env.document, FakeBlob, fakeURL, fakeWindow);
  dl('18766');
  const hint = env.elements['pair-firewall-script-hint'];
  const pass = hint.dataset.state === 'error'
    && hint.textContent.indexOf('拦截') >= 0
    && hint.textContent.indexOf('blocked by browser') >= 0;
  return { pass: pass, detail: 'hint=' + hint.textContent };
}

function extractColdStartModule(env) {
  const parts = ['scrollTargetIntoView', 'applyNapcatQrState', 'updateColdStartGuide', 'setColdStartTasks', 'setColdStartQq', 'pointToConnect']
    .map((name) => extractFunction(page, name)).join('\n');
  const prelude = "var COLD_KEY = 'nh_coldstart_guide_closed';\n"
    + "var coldState = {tasks: null, qqOnline: false};\n"
    + "var napcatRunning = false;\n"
    + "var motionFrozen = false;\n"
    + "function motionIsPaused() { return motionFrozen; }\n";
  const factory = new Function('document', 'window', 'localStorage',
    prelude + parts
    + '\nreturn {applyNapcatQrState: applyNapcatQrState, updateColdStartGuide: updateColdStartGuide, setColdStartTasks: setColdStartTasks, setColdStartQq: setColdStartQq, pointToConnect: pointToConnect, freezeMotion: function (v) { motionFrozen = v; }};');
  return factory(env.document, env.window, env.localStorage);
}

function scenarioColdStartGuide() {
  const ids = ['connect', 'qrbox', 'qr', 'qr-note', 'coldstart-guide', 'auto-setup', 'go', 'refresh-qr', 'restart-napcat', 'qq-next-step', 'napcat-engine'];
  const store = {};
  const env = makeEnv(ids, {storage: store});
  const mod = extractColdStartModule(env);
  const guide = env.elements['coldstart-guide'];
  const checks = [];

  mod.updateColdStartGuide();
  checks.push(['未知数据时先不显示引导', guide.hidden === true]);
  mod.setColdStartTasks(0);
  checks.push(['零待办且未接入 → 显示引导', guide.hidden === false]);
  mod.setColdStartQq(true);
  checks.push(['已接入 QQ → 隐藏引导', guide.hidden === true]);
  mod.setColdStartQq(false);
  checks.push(['又变回未接入 → 再显示', guide.hidden === false]);
  store['nh_coldstart_guide_closed'] = '1';
  mod.updateColdStartGuide();
  checks.push(['手动关闭后不再出现', guide.hidden === true]);
  delete store['nh_coldstart_guide_closed'];
  mod.setColdStartTasks(4);
  checks.push(['有数据后不出现', guide.hidden === true]);

  const note = env.elements['qr-note'];
  const image = env.elements['qr'];
  mod.applyNapcatQrState(false);
  checks.push(['未启动 NapCat → 不放空二维码图片', image.hidden === true && note.hidden === false]);
  checks.push(['未启动 NapCat → 文案指向可点的入口', note.textContent.indexOf('还没启动 NapCat') === 0]);
  mod.applyNapcatQrState(true);
  checks.push(['已启动 → 说明保持不动（由 refreshQr 收尾）', note.hidden === false && note.textContent.indexOf('还没启动 NapCat') === 0]);

  mod.setColdStartQq(false);
  mod.setColdStartTasks(0);
  mod.pointToConnect();
  const box = env.elements['qrbox'];
  const panel = env.elements['connect'];
  checks.push(['引导点击后滚到二维码块', !!box.scrollOptions && box.scrollOptions.block === 'center']);
  checks.push(['滚动同时高亮 QQ 接入卡片', panel.classList.contains('is-spotlight')]);

  mod.freezeMotion(true);
  panel.classList.remove('is-spotlight');
  box.scrollOptions = null;
  mod.pointToConnect();
  checks.push(['减少动效时仍然滚动', !!box.scrollOptions && box.scrollOptions.behavior === 'auto']);
  checks.push(['减少动效时不脉冲', panel.classList.contains('is-spotlight') === false]);

  const failed = checks.filter((row) => !row[1]);
  return {
    pass: failed.length === 0,
    detail: failed.length ? failed.map((row) => row[0]).join('；') : '冷启动引导 12 项检查全部通过',
  };
}

function extractWizardModule(env, options) {
  options = options || {};
  // 只取这个闭包里跟弹窗互斥有关的两个函数；s1/s2/s3/s4、w、公网定时器由外面注入同名变量模拟 page.html 的闭包。
  const parts = ['openWizard', 'closeWizard'].map((name) => extractFunction(page, name)).join('\n');
  const prelude = "var WKEY = 'nh_setup_wizard_seen';\n"
    + "var w = document.getElementById('platform-setup-wizard');\n"
    + "var s1 = document.getElementById('platform-setup-step-1');\n"
    + "var s2 = document.getElementById('platform-setup-step-2');\n"
    + "var s3 = document.getElementById('platform-setup-step-3');\n"
    + "var s4 = document.getElementById('platform-setup-step-4');\n"
    + "var wizardTunnel = { timer: null };\n"
    + "function pubTunnelStop(ui) { if (ui && ui.timer !== null) { window.clearInterval(ui.timer); ui.timer = null; } }\n"
    + "function pubTunnelSync() { return Promise.resolve({}); }\n"
    + "var wizardSearch = " + JSON.stringify(options.search || '') + ";\n"
    + "function show(step) { s1.hidden = step !== 1; s2.hidden = step !== 2; s4.hidden = step !== 4; s3.hidden = step !== 3; }\n"
    + "function refreshBanner() {}\n"
    + "var location = { search: wizardSearch };\n";
  const factory = new Function('document', 'window', 'localStorage',
    prelude + parts + '\nreturn {openWizard: openWizard, closeWizard: closeWizard};');
  return factory(env.document, env.window, env.localStorage);
}

async function scenarioWizardPauseResets() {
  const failed = [];
  for (const mode of ['resolve', 'reject', 'throw', 'missing']) {
    const box = {hidden: false};
    let finish;
    let calls = 0;
    const pending = new Promise((resolve, reject) => {finish = mode === 'reject' ? reject : resolve;});
    const fetchStatus = mode === 'missing' ? undefined : function(){
      calls++;
      if(mode === 'throw')throw new Error('offline');
      return pending;
    };
    const factory = new Function('document', 'fetchHostingStatus',
      "var w={hidden:false}, wizardTunnel={}, WKEY='seen', journeyPaused=false;"
      + "var localStorage={setItem(){}}, pubTunnelStop=function(){}, refreshBanner=function(){};"
      + "function journeyDismissed(){return false;}"
      + extractFunction(page, 'closeWizard') + '\n' + extractFunction(page, 'renderJourney')
      + '\nreturn {close:closeWizard, render:renderJourney, paused:()=>journeyPaused};');
    const mod = factory({getElementById(){return box;}}, fetchStatus);
    const result = mod.close();
    if(mode === 'resolve' || mode === 'reject'){
      if(!mod.paused())failed.push(mode + ': 刷新期间没有暂停');
      mod.render({ok:true});
      if(!box.hidden)failed.push(mode + ': 暂停期间仍画 journey');
      finish(mode === 'reject' ? new Error('offline') : {});
    }
    await result;
    if(mod.paused())failed.push(mode + ': 暂停没有复位');
    if(calls !== (mode === 'missing' ? 0 : 1))failed.push(mode + ': 刷新次数错误');
  }
  return {pass:failed.length === 0, detail:failed.join('；') || '暂停期间不画，成功和失败都复位'};
}

function scenarioOnboardingWizardMutex() {
  const store = {};
  const env = makeEnv(
    ['platform-setup-wizard', 'platform-setup-step-1', 'platform-setup-step-2', 'platform-setup-step-3', 'platform-setup-step-4', 'onboarding', 'announce-modal'],
    { storage: store },
  );
  const mod = extractWizardModule(env);
  const checks = [];
  const onboarding = env.elements['onboarding'];
  // 模拟 initOnboarding：欢迎框按服务端 required 先弹出来（带着 NapCat 封号风险提示）。
  onboarding.hidden = false;

  mod.openWizard();
  checks.push(['打开向导不主动藏掉欢迎框（封号风险提示不能丢）', onboarding.hidden === false]);
  checks.push(['向导本身要显示', env.elements['platform-setup-wizard'].hidden === false]);

  mod.closeWizard();
  checks.push(['关掉向导后欢迎框仍在（首装就能看到风险提示）', onboarding.hidden === false]);
  checks.push(['关向导记下「看过」，不再每次都弹', store['nh_setup_wizard_seen'] === '1']);

  // nowiz=1（截图/自查专用）时向导不出现，但也绝不能顺手改欢迎框的显隐。
  const env2 = makeEnv(['platform-setup-wizard', 'platform-setup-step-1', 'platform-setup-step-2', 'platform-setup-step-3', 'platform-setup-step-4', 'onboarding'], { storage: {}, search: '?nowiz=1' });
  const mod2 = extractWizardModule(env2, { search: '?nowiz=1' });
  env2.elements['onboarding'].hidden = false;
  env2.elements['platform-setup-wizard'].hidden = true;
  mod2.openWizard();
  checks.push(['nowiz=1 时不打开向导', env2.elements['platform-setup-wizard'].hidden === true]);
  checks.push(['nowiz=1 时不动欢迎框', env2.elements['onboarding'].hidden === false]);

  const failed = checks.filter((row) => !row[1]);
  return {
    pass: failed.length === 0,
    detail: failed.length ? failed.map((row) => row[0]).join('；') : '向导与欢迎框互斥 6 项检查全部通过',
  };
}

function scenarioWizardTunnelStep() {
  const checks = [];
  const wizard = page.split('id="platform-setup-wizard"', 2)[1].split('id="announce-modal"', 1)[0];
  checks.push(['向导里有第 4 步「手机在外也能连」', wizard.includes('id="platform-setup-step-4"')]);
  checks.push(['第 4 步有开通按钮', wizard.includes('id="platform-setup-tunnel-start"') && wizard.includes('开通公网入口')]);
  checks.push(['第 4 步有可达性播报区', wizard.includes('id="platform-setup-tunnel-status"') && wizard.includes('role="status"')]);
  checks.push(['第 4 步有「以后再说」', wizard.includes('id="platform-setup-tunnel-later"')]);
  checks.push(['第 4 步换上自己的标题（不顶着「选择要接入的平台」）',
    page.includes('var WIZ_COPY={') && page.includes('title.textContent=copy[0]') && page.includes('手机在外也能连（可选）')]);
  checks.push(['show() 认第 4 步', page.includes('s4.hidden=step!==4')]);
  checks.push(['点过「以后再说」的人下次直接跳到收尾页', page.includes('localStorage.getItem(TUNNEL_SKIP_KEY)?3:4')]);
  checks.push(['「以后再说」记下跳过键', page.includes("localStorage.setItem(TUNNEL_SKIP_KEY,'1')")]);
  checks.push(['关向导会停掉公网轮询', page.includes('pubTunnelStop(wizardTunnel)')]);

  const start = page.indexOf('function pubTunnelSnapshot(');
  const end = page.indexOf('\nfunction show(', start);
  if (start < 0 || end < 0) throw new Error('找不到公网开通实现区段');
  const src = page.slice(start, end);
  const timers = [];
  const statusQueue = [
    { providers: { cloudflare: { state: 'starting' } } },
    { providers: { cloudflare: { state: 'starting' } } },
    { providers: { cloudflare: { state: 'ready', url: 'https://demo.trycloudflare.com' } } },
  ];
  const posts = [];
  const fakeWindow = {
    setInterval(fn, ms) { timers.push({ fn: fn, ms: ms }); return timers.length; },
    clearInterval() {},
  };
  const get = function () { return Promise.resolve(statusQueue.shift() || { providers: { cloudflare: { state: 'idle' } } }); };
  const fetchStub = function (url, options) {
    posts.push({ url: url, body: JSON.parse(options.body) });
    return Promise.resolve({ json: function () { return Promise.resolve({ ok: true }); } });
  };
  const factory = new Function('get', 'fetch', 'window', 'tok',
    src + '\nreturn {pubTunnelStart: pubTunnelStart, pubTunnelTick: pubTunnelTick};');
  const mod = factory(get, fetchStub, fakeWindow, function () { return 'test-token'; });
  const ui = { timer: null, status: { textContent: '' }, box: { hidden: true }, url: { value: '' }, primary: { disabled: false, dataset: {}, textContent: '' } };

  return mod.pubTunnelStart(ui).then(function () {
    checks.push(['开通请求带 confirm 与 cloudflare',
      posts.length === 1 && posts[0].url === '/api/tunnel/start' && posts[0].body.confirm === true && posts[0].body.provider === 'cloudflare']);
    checks.push(['开通中按钮禁用并播报进度', ui.primary.disabled === true && ui.status.textContent.indexOf('正在开通') === 0]);
    checks.push(['开通中按 2 秒轮询', timers.length === 1 && timers[0].ms === 2000]);
    return mod.pubTunnelTick(ui);
  }).then(function () {
    checks.push(['还在开通时继续轮询', ui.timer !== null]);
    return mod.pubTunnelTick(ui);
  }).then(function () {
    checks.push(['就绪后停止轮询', ui.timer === null]);
    checks.push(['就绪后显示地址', ui.box.hidden === false && ui.url.value === 'https://demo.trycloudflare.com']);
    checks.push(['就绪后按钮变「继续」',
      ui.primary.textContent === '继续' && ui.primary.dataset.mode === 'continue' && ui.primary.disabled === false]);
    checks.push(['就绪后播报可访问', ui.status.textContent.indexOf('已开通') >= 0]);
    const failed = checks.filter((row) => !row[1]);
    return {
      pass: failed.length === 0,
      detail: failed.length ? failed.map((row) => row[0]).join('；') : '向导公网步骤 15 项检查全部通过',
    };
  });
}

function scenarioUpdateGuideMutex() {
  const checks = [];
  const guide = page.split('id="update-guide"', 2)[1].split('id="onboarding"', 1)[0];
  checks.push(['引导页标题是「手机在外也能连」', guide.includes('id="update-guide-title"') && guide.includes('手机在外也能连')]);
  checks.push(['引导页两条通道都写了', guide.includes('Cloudflare') && guide.includes('Tailscale')]);
  checks.push(['引导页有开通与「稍后」', guide.includes('id="update-guide-start"') && guide.includes('id="update-guide-later"')]);
  checks.push(['引导页是可访问的对话框', guide.includes('role="dialog"') && guide.includes('aria-modal="true"') && guide.includes('aria-labelledby="update-guide-title"')]);
  checks.push(['引导页有播报区', guide.includes('id="update-guide-tunnel-status"') && guide.includes('role="status"')]);

  const parts = ['currentVersion', 'closeUpdateGuide', 'maybeOpenUpdateGuide']
    .map((name) => extractFunction(page, name)).join('\n');

  function build(store, version, options) {
    options = options || {};
    const elements = {};
    ['update-guide', 'update-guide-tunnel-status', 'update-guide-tunnel-url-box', 'update-guide-tunnel-url', 'update-guide-start', 'announce-modal', 'platform-setup-wizard', 'onboarding', 'journey']
      .forEach((id) => { elements[id] = makeElement(id); });
    elements['update-guide'].hidden = true;
    elements['platform-setup-wizard'].hidden = true;
    elements['onboarding'].hidden = true;
    elements['journey'].hidden = true;
    const document = {
      getElementById(id) { if (!elements[id]) elements[id] = makeElement(id); return elements[id]; },
      createElement() { return makeElement(''); },
      querySelector() { return version ? { textContent: version } : null; },
      querySelectorAll() { return []; },
      addEventListener() {},
    };
    const localStorage = {
      getItem(key) { return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null; },
      setItem(key, value) { store[key] = String(value); },
    };
    const synced = [];
    const factory = new Function('document', 'window', 'localStorage', 'location', 'ug', 'w', 'UPDKEY', 'updateTunnel', 'pubTunnelStop', 'pubTunnelSync',
      parts + '\nreturn {currentVersion: currentVersion, closeUpdateGuide: closeUpdateGuide, maybeOpenUpdateGuide: maybeOpenUpdateGuide};');
    const mod = factory(
      document, { setInterval: setInterval, clearInterval: clearInterval }, localStorage,
      { search: options.search || '' }, elements['update-guide'], elements['platform-setup-wizard'],
      'nh_update_guide_version', { timer: null }, function () {}, function () { synced.push(1); },
    );
    return { mod: mod, elements: elements, synced: synced, store: store };
  }

  // 版本未知（自由版没有徽章）：本次不弹，也不写版本键，免得真版本来了反而不弹。
  const noVersion = build({}, '');
  noVersion.mod.maybeOpenUpdateGuide();
  checks.push(['版本未知时不弹引导页', noVersion.elements['update-guide'].hidden === true]);
  checks.push(['版本未知时不写版本键', !Object.prototype.hasOwnProperty.call(noVersion.store, 'nh_update_guide_version')]);

  // 版本对不上、也没别的弹层挡着：弹出来，并先把公告弹窗藏掉（只延后不丢弃）。
  const fresh = build({}, ' · 1.1.0-beta.19');
  fresh.mod.maybeOpenUpdateGuide();
  checks.push(['新版本首次打开会弹引导页', fresh.elements['update-guide'].hidden === false]);
  checks.push(['弹引导页时让开公告弹窗', fresh.elements['announce-modal'].hidden === true]);
  checks.push(['弹引导页时查一次公网现状', fresh.synced.length === 1]);
  checks.push(['弹出来时还没写版本键', !Object.prototype.hasOwnProperty.call(fresh.store, 'nh_update_guide_version')]);

  fresh.mod.closeUpdateGuide();
  checks.push(['点「稍后」后关掉引导页', fresh.elements['update-guide'].hidden === true]);
  checks.push(['点「稍后」后记下本版本', fresh.store['nh_update_guide_version'] === '1.1.0-beta.19']);

  // 本版本已经点过「稍后」：不再弹。
  const seen = build({ 'nh_update_guide_version': '1.1.0-beta.19' }, ' · 1.1.0-beta.19');
  seen.mod.maybeOpenUpdateGuide();
  checks.push(['本版本点过「稍后」后不再弹', seen.elements['update-guide'].hidden === true]);

  // 首装向导开着：这次先不弹，也先不记版本，下次打开还能看到。
  const blocked = build({}, ' · 1.1.0-beta.19');
  blocked.elements['platform-setup-wizard'].hidden = false;
  blocked.mod.maybeOpenUpdateGuide();
  checks.push(['首装向导开着时先不弹引导页', blocked.elements['update-guide'].hidden === true]);
  checks.push(['被向导挡住时不写版本键（下次还能弹）', !Object.prototype.hasOwnProperty.call(blocked.store, 'nh_update_guide_version')]);

  // ?nowiz=1（截图/自查专用）不弹。
  const nowiz = build({}, ' · 1.1.0-beta.19', { search: '?nowiz=1' });
  nowiz.mod.maybeOpenUpdateGuide();
  checks.push(['nowiz=1 时不弹引导页', nowiz.elements['update-guide'].hidden === true]);

  // 「还差几步」journey 已经顶在最前面：引导页让路，两个全屏遮罩不叠（叠了按钮会被挡死）。
  const journeyFirst = build({}, ' · 1.1.0-beta.19');
  journeyFirst.elements['journey'].hidden = false;
  journeyFirst.mod.maybeOpenUpdateGuide();
  checks.push(['journey 开着时先不弹引导页', journeyFirst.elements['update-guide'].hidden === true]);
  checks.push(['被 journey 挡住时不写版本键', !Object.prototype.hasOwnProperty.call(journeyFirst.store, 'nh_update_guide_version')]);
  const renderJourneySrc = extractFunction(page, 'renderJourney');
  checks.push(['引导页可见时 journey 自己不画', renderJourneySrc.includes("overlayAhead('update-guide')")]);
  checks.push(['首装向导开着时 journey 也不画', renderJourneySrc.includes("overlayAhead('platform-setup-wizard')")]);
  const closeSrc = extractFunction(page, 'closeUpdateGuide');
  checks.push(['关掉引导页后叫 journey 补画', closeSrc.includes('fetchHostingStatus()')]);
  const closeWizardSrc = extractFunction(page, 'closeWizard');
  checks.push(['关掉首装向导后也叫 journey 补画', closeWizardSrc.includes('fetchHostingStatus()')]);

  // 公告侧：引导页可见时公告弹窗让位；函数末尾不把 modal 永久压住（公告只延后不丢弃）。
  const render = extractFunction(page, 'renderAnnounceModal');
  checks.push(['公告判据带上引导页', render.includes('updateGuide&&!updateGuide.hidden')]);
  checks.push(['公告只是延后、不被永久压住', !render.includes('modal.hidden = false')]);

  const failed = checks.filter((row) => !row[1]);
  return {
    pass: failed.length === 0,
    detail: failed.length ? failed.map((row) => row[0]).join('；') : '升级引导页与公告互斥 ' + checks.length + ' 项检查全部通过',
  };
}

// 取一个「写在别的函数体内、结尾缩进」的函数：按花括号配平切，不用行首 } 作结束标记。
function extractBracedFunction(src, name) {
  const marker = 'function ' + name + '(';
  const start = src.indexOf(marker);
  if (start < 0) throw new Error('找不到函数：' + name);
  let depth = 0;
  for (let i = src.indexOf('{', start); i < src.length; i += 1) {
    if (src[i] === '{') depth += 1;
    else if (src[i] === '}') { depth -= 1; if (depth === 0) return src.slice(start, i + 1); }
  }
  throw new Error('函数未闭合：' + name);
}

// 缺陷复现：微信数据目录清空（合法状态＝回到自动查找）→ 切 tab 再回来 → 保存。
// 旧实现靠「值非空」推断改过没，清空后判不出，重渲染时被服务端旧目录回填，保存又把旧目录写回去，
// 界面还说「已保存，立即生效。」。这里跑真实 harvest/restore/saveWechatDir 代码。
function scenarioWechatDirClearedSurvivesRerender() {
  const SERVER_DIR = 'D:\\WeChat Files\\wxid_old';
  const doc = { getElementById(id) { return elements[id] || null; } };
  const elements = {};
  const mod = new Function(
    'document',
    extractFunction(page, 'harvestSettingsInputs') + '\n' +
    extractFunction(page, 'restoreSettingsInputs') + '\n' +
    extractFunction(page, 'markSettingsDirty') + '\n' +
    extractFunction(page, 'bindSettingsDirtyTracking') +
    '\nreturn {harvest: harvestSettingsInputs, restore: restoreSettingsInputs, bind: bindSettingsDirtyTracking};'
  )(doc);

  // 1) 用户在输入框里把目录删空（input 事件 → 显式标记）
  const oldInput = makeElement('wx-dir-input');
  oldInput.type = 'text';
  oldInput.value = SERVER_DIR;
  const listeners = {};
  const root = {
    dataset: {},
    addEventListener(type, fn) { listeners[type] = fn; },
    querySelectorAll() { return [oldInput]; },
  };
  mod.bind(root);
  oldInput.value = '';
  if (listeners.input) listeners.input({ target: oldInput });
  const marked = oldInput.dataset.nhDirty === '1';
  const keep = mod.harvest(root);
  const kept = Object.prototype.hasOwnProperty.call(keep, 'wx-dir-input');

  // 2) 切到别的 tab 再回来＝设置页重建：新节点先空着；只有 keep 里没这个 id 才会被服务端旧值回填
  const env = makeEnv(['wx-dir-input', 'wx-dir-save', 'wx-dir-status', 'wx-groups', 'wx-status']);
  const fresh = env.elements['wx-dir-input'];
  elements['wx-dir-input'] = fresh;
  fresh.type = 'text';
  fresh.value = kept ? '' : SERVER_DIR;
  mod.restore(keep);
  const keptEmpty = fresh.value === '';

  // 3) 点保存：跑真实 saveWechatDir（连同真实 setWechatDirStatus 的文案）
  fresh.dataset.nhDirty = '1'; // 同一会话里改完直接存的情况，也要在成功后清标记
  const calls = [];
  const apiMock = function (path, options) {
    const body = options && options.body ? String(options.body) : '';
    calls.push({ path: path, body: body });
    if (path === '/api/settings') {
      const posted = JSON.parse(body || '{}');
      return Promise.resolve({ ok: true, wechat_dir: typeof posted.wechat_dir === 'string' ? posted.wechat_dir : SERVER_DIR });
    }
    return Promise.resolve({ ok: true });
  };
  const runSave = new Function(
    'document', 'api', 'refreshWechatStatus', 'wxLoaded',
    extractBracedFunction(page, 'saveWechatDir') + '\n' + extractFunction(page, 'setWechatDirStatus') +
    '\nreturn saveWechatDir;'
  )(env.document, apiMock, function () {}, false);
  runSave();

  return new Promise(function (resolve) {
    setTimeout(function () {
      const posted = calls.filter(function (row) { return row.path === '/api/settings'; });
      const body = posted.length ? JSON.parse(posted[0].body) : {};
      const status = env.elements['wx-dir-status'].textContent;
      const checks = [
        ['清空后输入框被打上「未保存」标记', marked],
        ['重建设置页时清空值没被丢掉', kept],
        ['重渲染后输入框仍为空', keptEmpty],
        ['保存发出去的是空目录，不是旧目录', body.wechat_dir === ''],
        ['保存后输入框仍是空的', fresh.value === ''],
        ['状态行文案与真实结果一致（现在自动查找微信数据目录）', status.indexOf('现在自动查找微信数据目录') >= 0],
        ['保存成功后清掉未保存标记', fresh.dataset.nhDirty === undefined],
      ];
      const failed = checks.filter(function (row) { return !row[1]; });
      resolve({
        pass: failed.length === 0,
        detail: failed.length
          ? failed.map(function (row) { return row[0]; }).join('；') + ' | posted=' + JSON.stringify(body) + ' status=' + status
          : '清空目录 7 项检查全部通过',
      });
    }, 30);
  });
}

// 「去配置」跳转：等目标稳定（出现、有高度、连续两帧不变）才滚；版面滚动后再变要校正一次；
// 比视口高的目标用 block:'start'，顶边落在页头下 8px。用逐帧 height 序列驱动假 rAF 全跑真实代码。
function scenarioWizardScroll() {
  const header = { getBoundingClientRect() { return { top: 0, bottom: 76, height: 76 }; } };
  function buildTarget() {
    return {
      isConnected: true, style: {}, tabindexSet: false, _h: 0, scrolls: [], focuses: [],
      get offsetHeight() { return this._h; },
      scrollIntoView(options) { this.scrolls.push(options); },
      focus(options) { this.focuses.push(options || {}); },
      setAttribute() { this.tabindexSet = true; },
      getBoundingClientRect() { return { top: 100, bottom: 100 + this._h, height: this._h }; },
    };
  }
  // 每个用例自己一条 rAF 队列：每帧先设定这一帧的目标高度，再执行队列里的一个回调。
  function buildModule(target, options) {
    options = options || {};
    const rafQ = [];
    const raf = function (fn) { rafQ.push(fn); };
    const pump = function (frames, heights) {
      for (let i = 0; i < frames; i += 1) {
        if (heights) target._h = heights[Math.min(i, heights.length - 1)];
        const fn = rafQ.shift();
        if (!fn) break;
        fn();
      }
      return rafQ.length;
    };
    // page 里的函数用裸 requestAnimationFrame / document.getElementById / document.querySelector，
    // 在这里全部注入：rAF 走假队列，getElementById 按用例需要返回。
    const doc = {
      querySelector(sel) { return sel === '.masthead-row' ? header : null; },
      getElementById(id) { return options.getElementById ? options.getElementById(id) : (id === 'mock-target' ? target : null); },
    };
    const win = { requestAnimationFrame: raf, innerHeight: options.vh || 200 };
    const parts = ['var requestAnimationFrame = window.requestAnimationFrame;\n'];
    if (options.wizard) {
      parts.push('var setupTargets = {qq: "mock-target"};\nfunction closeWizard() {}\n');
      parts.push(extractFunction(page, 'scrollTargetIntoView') + '\n');
      parts.push(extractFunction(page, 'scrollWizardTarget') + '\n');
      parts.push(extractFunction(page, 'settleScrollWizardTarget') + '\n');
      parts.push(extractFunction(page, 'openPlatformSetupTarget') + '\nreturn {open: openPlatformSetupTarget};');
    } else {
      parts.push(extractFunction(page, 'scrollTargetIntoView') + '\n');
      parts.push(extractFunction(page, 'scrollWizardTarget') + '\n');
      parts.push(extractFunction(page, 'settleScrollWizardTarget') + '\nreturn {settle: settleScrollWizardTarget};');
    }
    return { mod: new Function('document', 'window', parts.join('\n'))(doc, win), pump };
  }
  const checks = [];
  {
    // A：普通高度目标（< 视口）稳定后 center，且不重复滚
    const t = buildTarget();
    const run = buildModule(t);
    run.mod.settle('mock-target');
    run.pump(10, [0, 0, 0, 150, 150, 150, 150, 150, 150, 150]);
    checks.push(['稳定后滚一次且用 center', t.scrolls.length === 1 && t.scrolls[0].block === 'center']);
    checks.push(['滚后不动就不校正', t.scrolls.length === 1]);
    checks.push(['滚后 focus 关掉自动拽滚动', t.focuses.length === 1 && t.focuses[0].preventScroll === true]);
    checks.push(['滚后打 tabindex=-1', t.tabindexSet]);
  }
  {
    // B：比视口还高的目标用 block:'start'，顶边落在页头下 8px（实测 8767 的 #connect 就是这种）
    const t = buildTarget();
    const run = buildModule(t);
    run.mod.settle('mock-target');
    run.pump(10, [0, 500, 500, 500, 500, 500, 500, 500, 500, 500]);
    checks.push(['超高目标滚一次', t.scrolls.length === 1]);
    checks.push(['超高目标用 start 对齐顶边', t.scrolls.length && t.scrolls[0].block === 'start']);
    checks.push(['超高目标补了 scroll-margin-top=页头+8', t.style.scrollMarginTop === '84px']);
  }
  {
    // C：滚完后版面再长高 → 校正一次；再变第三次也不校（只一次）。视口用 1000，别让 360 被误判成超高。
    const t = buildTarget();
    const run = buildModule(t, { vh: 1000 });
    run.mod.settle('mock-target');
    run.pump(16, [0, 0, 0, 150, 150, 150, 150, 150, 360, 360, 360, 360, 400, 400, 400, 400]);
    checks.push(['版面再变只校正一次', t.scrolls.length === 2]);
    checks.push(['两次都用 center', t.scrolls.length === 2 && t.scrolls[1].block === 'center']);
  }
  {
    // D：目标一直不在 DOM，4s 帧上限后自动停，不滚也不死循环
    const rafQ = [];
    const raf = function (fn) { rafQ.push(fn); };
    const emptyDoc = { getElementById() { return null; }, querySelector() { return null; } };
    const factory = new Function(
      'document', 'window',
      'var requestAnimationFrame = window.requestAnimationFrame;\n'
      + extractFunction(page, 'settleScrollWizardTarget') + '\n'
      + 'return {settle: settleScrollWizardTarget};'
    );
    factory(emptyDoc, { requestAnimationFrame: raf, innerHeight: 200 }).settle('ghost');
    let driven = 0;
    for (; driven < 300; driven += 1) {
      const fn = rafQ.shift();
      if (!fn) break;
      fn();
    }
    // 帧上限 240：watched 在自增前判断，幽灵目标跑满一轮后第 242 个 tick 直接 return，不再入队。
    checks.push(['目标不存在时不滚、不超帧上限即停', driven <= 242 && rafQ.length === 0]);
  }
  {
    // E：真实入口：openPlatformSetupTarget('qq') 切设置 tab 并 settle 到 connect
    // （1.1.0 修过 QQ「去配置」落到任务页签：目标 #connect 在设置页里，切错就滚不到）
    const t = buildTarget();
    let tabClicked = false;
    let wrongTabClicked = false;
    const run = buildModule(t, { vh: 1000, wizard: true, getElementById(id) {
      if (id === 'tab-settings-button') return { click() { tabClicked = true; } };
      if (id === 'tab-tasks-button') return { click() { wrongTabClicked = true; } };
      return t;
    } });
    run.mod.open('qq');
    run.pump(10, [0, 0, 0, 300, 300, 300, 300, 300, 300, 300]);
    checks.push(['入口切了设置 tab，没切去任务 tab', tabClicked && !wrongTabClicked]);
    checks.push(['入口滚到了目标一次（center）', t.scrolls.length === 1 && t.scrolls[0].block === 'center']);
  }
  {
    // F：共用 helper scrollTargetIntoView 自身的分挡：矮目标 center（保持历史落点），
    // 超高目标 start + scroll-margin-top=页头底边+8；behavior 原样透传、不传就不加这个键。
    function stubModule(headerBottom, vh) {
      const head = { getBoundingClientRect() { return { top: 0, bottom: headerBottom, height: headerBottom }; } };
      const doc = { querySelector(sel) { return sel === '.masthead-row' ? head : null; } };
      const factory = new Function(
        'document', 'window',
        extractFunction(page, 'scrollTargetIntoView') + '\nreturn scrollTargetIntoView;'
      );
      return factory(doc, { innerHeight: vh });
    }
    const shortCard = buildTarget();
    shortCard._h = 452; // 更新卡片的实测高度量级
    const shortOpts = stubModule(76, 1000)(shortCard, 'smooth');
    checks.push(['矮目标 helper 给 center', shortOpts === 'center' && shortCard.scrolls[0].block === 'center']);
    checks.push(['矮目标不给 margin、behavior 透传 smooth', !shortCard.style.scrollMarginTop && shortCard.scrolls[0].behavior === 'smooth']);
    const tallCard = buildTarget();
    tallCard._h = 3027; // 8767 实测的 #connect
    const tallOpts = stubModule(76, 1000)(tallCard);
    checks.push(['超高目标 helper 给 start', tallOpts === 'start' && tallCard.scrolls[0].block === 'start']);
    checks.push(['超高目标 margin=页头底边+8', tallCard.style.scrollMarginTop === '84px']);
    checks.push(['不传 behavior 就不加该键（保持浏览器默认）', !('behavior' in tallCard.scrolls[0])]);
  }
  const failed = checks.filter(function (row) { return !row[1]; });
  return {
    pass: failed.length === 0,
    detail: failed.length
      ? failed.map(function (row) { return row[0]; }).join('；')
      : '向导滚动与共用判据 ' + checks.length + ' 项检查全部通过',
  };
}

// 手工任务纠错：manual: 前缀的任务不出 AI 快捷纠错三键、给「删除这条待办」（POST action:delete 的入口），
// 编辑类按钮保留；非 manual 任务照旧。跑真实 correctionPanel 渲染（渲染期不点按钮，不碰网络）。
function scenarioManualDelete() {
  const tdEnv = makeTrackingEnv([]);
  const doc = {
    querySelector() { return null; },
    createElement(tag) { return tdEnv.document.createElement(tag); },
  };
  const factory = new Function(
    'document', 'window',
    'var CATEGORY_LABELS = {urgent: "紧急", action: "待办", academic: "学业", info: "通知"};\n'
    + extractFunction(page, 'el') + '\n'
    + extractFunction(page, 'correctionButton') + '\n'
    + extractFunction(page, 'correctionPanel') + '\n'
    + 'function sendCorrection() {}\n'
    + 'return {panel: correctionPanel};'
  );
  const mod = factory(doc, { setTimeout });
  function collectText(node) {
    let out = String(node.textContent || '');
    (node.children || []).forEach(function (c) { out += ' ' + collectText(c); });
    return out;
  }
  const manual = mod.panel({ id: 9, task_key: 'manual:test-pinned', category: 'info' });
  const auto = mod.panel({ id: 10, category: 'info' });
  const manualText = collectText(manual);
  const autoText = collectText(auto);
  const checks = [
    ['manual 任务不出现「不是通知」', manualText.indexOf('不是通知') < 0],
    ['manual 任务不出现「不是待办」', manualText.indexOf('不是待办') < 0],
    ['manual 任务不出现「标记重复」', manualText.indexOf('标记重复') < 0],
    ['manual 任务有「删除这条待办」', manualText.indexOf('删除这条待办') >= 0],
    ['manual 保留「保存分类」', manualText.indexOf('保存分类') >= 0],
    ['manual 保留「保存时间」', manualText.indexOf('保存时间') >= 0],
    ['manual 提示不写入学习记录', manualText.indexOf('不会写入任何学习记录') >= 0],
    ['非 manual 任务照旧「不是通知」', autoText.indexOf('不是通知') >= 0],
    ['非 manual 任务照旧「标记重复」', autoText.indexOf('标记重复') >= 0],
    ['非 manual 任务不出现「删除这条待办」', autoText.indexOf('删除这条待办') < 0],
  ];
  const failed = checks.filter(function (row) { return !row[1]; });
  return {
    pass: failed.length === 0,
    detail: failed.length
      ? failed.map(function (row) { return row[0]; }).join('；') + ' | manualText=' + manualText.slice(0, 120)
      : '手工任务纠错 ' + checks.length + ' 项检查全部通过',
  };
}

// QQ 接入卡五状态：跑真实 applyQQConnectState，断言每个状态只露一个主按钮（+可选次按钮），其余全 hidden。
function scenarioQQButtons() {
  const buttonIds = ['auto-setup', 'go', 'refresh-qr', 'restart-napcat', 'napcat-connect'];
  const ids = buttonIds.concat(['qq-next-step', 'napcat-engine']);
  const env = makeEnv(ids);
  const factory = new Function(
    'document', 'window',
    extractFunction(page, 'applyQQConnectState') + '\nreturn applyQQConnectState;'
  );
  const apply = factory(env.document, env.window);
  const visible = function () {
    return buttonIds.filter(function (id) { return env.elements[id].hidden === false; });
  };
  const checks = [];
  apply('pending', '');
  checks.push(['检查期四个按钮全 hidden', visible().length === 0]);
  checks.push(['检查期显示「正在检查」', env.elements['napcat-engine'].textContent.indexOf('正在检查') === 0]);
  checks.push(['检查期下一步说明让位给 #status', env.elements['qq-next-step'].hidden === true]);
  apply('engine_missing', '');
  checks.push(['没装引擎 → 主按钮=auto-setup（走指纹确认那条链路）', JSON.stringify(visible()) === JSON.stringify(['auto-setup'])]);
  checks.push(['没装引擎 → 主按钮文案', env.elements['auto-setup'].textContent === '下载安装并启动']);
  checks.push(['没装引擎 → 下一步说明', env.elements['qq-next-step'].textContent.indexOf('还没装引擎') === 0]);
  checks.push(['没装引擎 → 说明露出来', env.elements['qq-next-step'].hidden === false]);
  apply('installed_not_running', '');
  checks.push(['装了没跑 → 只剩主按钮 go', JSON.stringify(visible()) === JSON.stringify(['go'])]);
  checks.push(['装了没跑 → 主按钮文案换了', env.elements['go'].textContent === '启动引擎并显示二维码']);
  apply('running_need_login', '');
  checks.push(['在跑未登录 → 主按钮=刷新 + 次按钮=重启', JSON.stringify(visible()) === JSON.stringify(['refresh-qr', 'restart-napcat'])]);
  checks.push(['在跑未登录 → 主按钮文案', env.elements['refresh-qr'].textContent === '刷新二维码']);
  checks.push(['在跑未登录 → 安装按钮不露头', env.elements['auto-setup'].hidden === true && env.elements['go'].hidden === true]);
  apply('running_online', '123456');
  checks.push(['已登录 → 只剩重启次按钮', JSON.stringify(visible()) === JSON.stringify(['restart-napcat'])]);
  checks.push(['已登录 → 说明带账号', env.elements['qq-next-step'].textContent.indexOf('已接入：123456') === 0]);
  checks.push(['已登录 → 主按钮区没内容', env.elements['go'].hidden === true && env.elements['refresh-qr'].hidden === true]);
  // 第五态：手动起的 NapCat 在跑、接口没通。唯一该点的就是「帮我连上它」，安装/启动/刷新都必须收起来。
  apply('running_no_api', '');
  checks.push(['接口没通 → 只有「帮我连上它」是主按钮', JSON.stringify(visible()) === JSON.stringify(['napcat-connect', 'restart-napcat'])]);
  checks.push(['接口没通 → 主按钮文案', env.elements['napcat-connect'].textContent === '帮我连上它']);
  checks.push(['接口没通 → 引擎行说清是接口没通', env.elements['napcat-engine'].textContent.indexOf('连不上它的接口') > 0]);
  checks.push(['接口没通 → 安装/启动/刷新都不露头', env.elements['auto-setup'].hidden === true && env.elements['go'].hidden === true && env.elements['refresh-qr'].hidden === true]);
  checks.push(['接口没通 → 下一步指向「帮我连上它」', env.elements['qq-next-step'].textContent.indexOf('帮我连上它') >= 0]);
  // 未知状态仍然兜底到 pending，不能因为多了第五态就把兜底弄丢。
  apply('something_new', '');
  checks.push(['未知状态兜底成检查期', visible().length === 0]);
  // 失败态由 #status 说（它带具体原因），#qq-next-step 让位，不能两句都在讲下一步。
  apply('running_need_login', '', true);
  checks.push(['失败态 → #status 说话时下一步说明让位', env.elements['qq-next-step'].hidden === true]);
  const failed = checks.filter(function (row) { return !row[1]; });
  return {
    pass: failed.length === 0,
    detail: failed.length
      ? failed.map(function (row) { return row[0]; }).join('；')
      : 'QQ 接入五状态 ' + checks.length + ' 项检查全部通过',
  };
}

const report = {};
// 更新横幅三态（行为级）：后台下载中只说进度、不提重启；已下好才给重启入口；没有更新时横幅保持隐藏。
function makeBannerEnv(seenTag) {
  const elements = {};
  function makeNode(tagName) {
    return {
      tagName: String(tagName || '').toUpperCase(), id: '', className: '', type: '',
      textContent: '', hidden: false, children: [], attrs: {},
      appendChild(child) { this.children.push(child); },
      setAttribute(name, value) { this.attrs[name] = String(value); },
      addEventListener() {}, removeAttribute() {},
      classList: { add() {}, remove() {}, toggle() { return false; }, contains() { return false; } },
    };
  }
  const document = {
    getElementById(id) { if (!elements[id]) elements[id] = makeNode('div'); return elements[id]; },
    createElement(tag) { return makeNode(tag); },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {},
  };
  const store = {};
  if (seenTag) store.seen_update_tag = String(seenTag);
  const localStorage = {
    getItem(key) { return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null; },
    setItem(key, value) { store[key] = String(value); },
  };
  return { document: document, localStorage: localStorage };
}

// 只跑真实的 renderUpdateBanner：从 settings.js 切出「版本更新」区段，塞进 state 后渲染一次。
function renderBannerFor(state, seenTag) {
  const env = makeBannerEnv(seenTag);
  const start = page.indexOf('var updateState = null;');
  const end = page.indexOf('\nfunction setupUpdateCard', start);
  if (start < 0 || end < 0) throw new Error('找不到更新横幅区段');
  const src = page.slice(start, end);
  const factory = new Function('document', 'window', 'localStorage', 'state',
    src + '\nupdateState = state;\nrenderUpdateBanner();\nreturn document.getElementById("update-banner");');
  const banner = factory(env.document, { location: { href: 'http://127.0.0.1/' } }, env.localStorage, state);
  const flat = [];
  (function walk(node) {
    flat.push(String(node.textContent || ''));
    (node.children || []).forEach(walk);
  })(banner);
  return { hidden: !!banner.hidden, text: flat.join(' ').replace(/\s+/g, ' ').trim() };
}

function scenarioUpdateBannerThreeStates() {
  const problems = [];
  const version = '1.1.0-beta.18';
  const tag = 'v1.1.0-beta.19';
  const base = {
    ok: true,
    current: { version: version, tag: 'v' + version },
    latest: { tag: tag, version: '1.1.0-beta.19', name: '' },
    has_update: true,
  };

  const downloading = renderBannerFor(Object.assign({}, base, {
    pending: null, download: { state: 'downloading', done: 42, total: 100, tag: tag },
  }), '');
  if (downloading.hidden) problems.push('下载中横幅被隐藏了');
  if (downloading.text.indexOf('正在后台下载') < 0) problems.push('下载中没说「正在后台下载」：' + downloading.text);
  if (downloading.text.indexOf('42%') < 0) problems.push('下载中没带百分比：' + downloading.text);
  if (downloading.text.indexOf('重启并更新') >= 0) problems.push('下载中不该提「重启并更新」：' + downloading.text);
  if (downloading.text.indexOf('查看进度') < 0) problems.push('下载中按钮该是「查看进度」：' + downloading.text);

  const downloaded = renderBannerFor(Object.assign({}, base, {
    pending: null, download: { state: 'done', done: 100, total: 100, path: 'x.zip', tag: tag },
  }), '');
  if (downloaded.hidden) problems.push('已下好横幅被隐藏了');
  if (downloaded.text.indexOf('已下好') < 0) problems.push('已下好没说「已下好」：' + downloaded.text);
  if (downloaded.text.indexOf('重启并更新') < 0) problems.push('已下好没给重启入口：' + downloaded.text);

  // 刚检查到新版本、还没开始下载：不能冒充「已下好」。
  const fresh = renderBannerFor(Object.assign({}, base, { pending: null, download: { state: 'idle' } }), '');
  if (fresh.hidden) problems.push('发现新版本横幅被隐藏了');
  if (fresh.text.indexOf('已下好') >= 0) problems.push('没下载就说已下好：' + fresh.text);

  const idle = renderBannerFor({
    ok: true, current: { version: version, tag: 'v' + version }, latest: null,
    has_update: false, pending: null, download: { state: 'idle', done: 0, total: 0 },
  }, '');
  if (!idle.hidden) problems.push('没有更新时横幅没隐藏');
  if (idle.text) problems.push('没有更新时横幅还有文字：' + idle.text);

  // 关掉之后必须真的关得掉（记 seen_update_tag 后同一条更新不再弹）。
  const seenAgain = renderBannerFor(Object.assign({}, base, {
    pending: null, download: { state: 'downloading', done: 42, total: 100, tag: tag },
  }), tag);
  if (!seenAgain.hidden) problems.push('点过关闭后横幅又回来了');
  const pending = Object.assign({}, base, { pending: { version: '1.1.0' } });
  if (!renderBannerFor(pending, '1.1.0').hidden) problems.push('待装态点过关闭后横幅又回来了');
  if (renderBannerFor(pending, '').hidden) problems.push('未读的待装态横幅被隐藏了');

  return {
    pass: problems.length === 0,
    detail: problems.join('; ') || '横幅三态：下载中带百分比不提重启 / 已下好给重启入口 / 无更新隐藏 / 关闭后不再弹',
  };
}

// 横幅百分比必须跟着轮询走：下载中每拉一次 /api/update 就重渲染一次横幅，进度不再是页面刚打开时的旧值；
// 拉到最后一次返回 done 时横幅切成「已下好」，并且定时器真的停掉（不再发请求）。
function scenarioUpdateBannerPercentFollowsPolling() {
  const problems = [];
  const tag = 'v1.1.0-beta.19';
  const env = makeBannerEnv('');
  env.window = {
    setTimeout: setTimeout, clearTimeout: clearTimeout,
    setInterval: setInterval, clearInterval: clearInterval,
    location: { href: 'http://127.0.0.1/', reload() {} },
  };
  const banner = env.document.getElementById('update-banner');
  // 真 DOM 里给 textContent 赋值会清掉子节点；这个桩要照做，否则每次重渲染的旧文案会累加，断言就没有意义。
  let bannerOwnText = '';
  Object.defineProperty(banner, 'textContent', {
    get() { return bannerOwnText; },
    set(value) { bannerOwnText = String(value); banner.children = []; },
  });
  function bannerText() {
    const flat = [];
    (function walk(node) {
      flat.push(String(node.textContent || ''));
      (node.children || []).forEach(walk);
    })(banner);
    return flat.join(' ').replace(/\s+/g, ' ').trim();
  }
  const fetches = { count: 0 };
  global.fetch = function () {
    fetches.count += 1;
    const step = fetches.count;
    const download = step === 1 ? { state: 'downloading', done: 20, total: 100, tag: tag }
      : step === 2 ? { state: 'downloading', done: 60, total: 100, tag: tag }
        : { state: 'done', done: 100, total: 100, path: 'x.zip', tag: tag };
    return Promise.resolve({ ok: true, status: 200, json: function () {
      return Promise.resolve({
        ok: true,
        current: { version: '1.1.0-beta.18', tag: 'v1.1.0-beta.18' },
        latest: { tag: tag, version: '1.1.0-beta.19', name: '' },
        has_update: true, pending: null, download: download,
      });
    } });
  };
  const mod = buildApiModule(env);
  const updates = extractUpdateModule(env, mod.api);
  function wait(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }
  const snapshots = [];
  updates.pollUpdateDownload();
  return wait(150).then(function () {
    snapshots.push(bannerText());
    return wait(900);  // > 一个轮询周期（700ms），等第二次拉取回来
  }).then(function () {
    snapshots.push(bannerText());
    return wait(900);
  }).then(function () {
    snapshots.push(bannerText());
    const settled = fetches.count;
    // 900ms 大于一个轮询周期：若 done 之后定时器没清，count 还会涨。
    return wait(900).then(function () {
      const first = snapshots[0], second = snapshots[1], third = snapshots[2];
      if (first.indexOf('20%') < 0) problems.push('第一次进度没进横幅：' + first);
      if (second.indexOf('60%') < 0) problems.push('第二次进度没进横幅（百分比停在旧值）：' + second);
      if (second.indexOf('20%') >= 0) problems.push('第二次横幅还留着旧百分比：' + second);
      if (third.indexOf('已下好') < 0) problems.push('下载完成后横幅没切成已下好：' + third);
      if (third.indexOf('重启并更新') < 0) problems.push('已下好没给重启入口：' + third);
      if (settled !== 3) problems.push('轮询次数不对（期望 20% → 60% → done 共 3 次）：' + settled);
      if (fetches.count !== settled) problems.push('下载完成后定时器没停：settled=' + settled + ' now=' + fetches.count);
      return {
        pass: problems.length === 0,
        detail: problems.join('; ')
          || ('横幅百分比跟随轮询：20% → 60% → 已下好，完成后定时器已停（' + settled + ' 次拉取）'),
      };
    });
  });
}


// ===== 定时器开了必须关得上 =====
// ① 「后端还在检查更新」的自排续查：每进一次 refreshUpdate 都不许多一条链；checking 结束或切走设置页必须停。
function scenarioUpdateCheckingPollKeepsSingleTimer() {
  const problems = [];
  const env = makeEnv(['update-status']);
  const stats = {created: 0, cleared: 0, live: []};
  let refreshCalls = 0;
  env.document.body = {dataset: {tab: 'settings'}};
  env.window.setTimeout = function (fn, ms) {
    stats.created += 1;
    const h = {fn: null, ms: ms};
    h.fn = function () { const i = stats.live.indexOf(h); if (i >= 0) stats.live.splice(i, 1); fn(); };
    stats.live.push(h);
    return h;
  };
  env.window.clearTimeout = function (h) { const i = stats.live.indexOf(h); if (i >= 0) { stats.live.splice(i, 1); stats.cleared += 1; } };
  const mod = buildApiModule(env);
  const updates = extractUpdateModule(env, function (url) { refreshCalls += 1; return new Promise(function () {}); });
  const payload = {
    ok: true, checking: true,
    current: {version: '1.1.0-beta.18', tag: 'v1.1.0-beta.18'},
    latest: null, pending: null, has_update: false, download: {state: 'idle'},
  };
  updates.applyUpdateState(Object.assign({}, payload));
  updates.applyUpdateState(Object.assign({}, payload));
  updates.applyUpdateState(Object.assign({}, payload));
  if (stats.live.length !== 1) problems.push('连续三次 checking 状态留下了 ' + stats.live.length + ' 个续查计时器（应为 1）');
  if (stats.cleared !== 2) problems.push('前两个计时器没被清掉（cleared=' + stats.cleared + '，应为 2）');
  // 切走设置页：到点也只许停，不许再打一次 /api/update。
  const pending = stats.live[0];
  env.document.body.dataset.tab = 'tasks';
  if (pending) pending.fn();
  if (refreshCalls !== 0) problems.push('切走设置页后还在续查（refresh 次数=' + refreshCalls + '）');
  // checking 结束：计时器要当场清掉，不能再自己醒来。
  env.document.body.dataset.tab = 'settings';
  updates.applyUpdateState(Object.assign({}, payload));
  const beforeDone = stats.live.length;
  updates.applyUpdateState(Object.assign({}, payload, {checking: false}));
  if (stats.live.length !== 0) problems.push('checking 结束后还留着 ' + stats.live.length + ' 个计时器');
  if (beforeDone !== 1) problems.push('恢复设置页后没有重新排一次续查（live=' + beforeDone + '）');
  return {
    pass: problems.length === 0,
    detail: problems.join('; ') || ('检查更新续查单计时器：created=' + stats.created + ' cleared=' + stats.cleared + '，切页/结束都停'),
  };
}

// ② loadSettings 中途抛异常：扫描定时器必须仍然在（旧的清了、新的必须由 finally 补回来），
//    否则「设置页抖一下」就会让钉钉/微信扫描永久停更。
function scenarioScanTimersSurviveSettingsThrow() {
  const problems = [];
  const srcStart = page.indexOf('function loadSettings() {');
  const srcEnd = page.indexOf('/* 「项目地址 / 什么是 agent？」入口', srcStart);
  if (srcStart < 0 || srcEnd < 0) return {pass: false, detail: '找不到 loadSettings 区段'};
  const loadSrc = page.slice(srcStart, srcEnd);
  if (page.indexOf('function ensureScanTimers(') < 0) {
    return {pass: false, detail: 'settings.js 里没有 ensureScanTimers 收口函数（扫描定时器还是内联创建）'};
  }
  const ensureSrc = extractFunction(page, 'ensureScanTimers');
  const factory = new Function('document', 'window', 'setInterval', 'clearInterval', 'api', 'updateScanStatus',
    'var dingtalkScanTimer = null;\nvar wechatScanTimer = null;\nvar wxScanHoldUntil = 0;\n'
    + ensureSrc + '\n' + loadSrc
    + '\nreturn {run: loadSettings, timers: function () { return {dt: dingtalkScanTimer, wx: wechatScanTimer}; }};');
  const created = [];
  const fakeInterval = function (fn, ms) { const h = {fn: fn, ms: ms}; created.push(h); return h; };
  const throwingDocument = {
    querySelector() { return null; },
    getElementById(id) { throw new Error('DOM 构建失败：' + id); },
    createElement() { throw new Error('DOM 构建失败'); },
  };
  const fakeWindow = {scrollY: 0, scrollTo() {}, setTimeout() { return 0; }, clearTimeout() {}};
  const mod = factory(throwingDocument, fakeWindow, fakeInterval, function () {}, function () { return new Promise(function () {}); }, function () {});
  let threw = null;
  try { mod.run(); } catch (error) { threw = error; }
  if (!threw) problems.push('构造用的假 DOM 没让它抛异常，这条用例没锁到失败路径');
  const timers = mod.timers();
  if (timers.dt === null || timers.wx === null) {
    problems.push('面板构建抛异常后扫描定时器没了（dt=' + String(timers.dt) + ' wx=' + String(timers.wx) + '）——扫描会永久停更');
  }
  if (created.length !== 2) problems.push('抛异常后重建的定时器数量=' + created.length + '（应为 2）');
  // 重建只能靠 finally：loadSettings 主体里不许再内联建扫描定时器（否则异常时又漏）。
  if (loadSrc.indexOf('} finally {') < 0 || loadSrc.indexOf('ensureScanTimers();') < 0) {
    problems.push('loadSettings 没有 try/finally + ensureScanTimers() 收口');
  }
  if ((loadSrc.match(/(dingtalk|wechat)ScanTimer\s*=\s*setInterval/g) || []).length) {
    problems.push('loadSettings 里又内联建扫描定时器了');
  }
  return {
    pass: problems.length === 0,
    detail: problems.join('; ') || ('loadSettings 抛异常后两个扫描定时器都已由 finally 重建（重建 ' + created.length + ' 条）'),
  };
}

// ③ 社区在线状态行：占位「社区在线：…」默认可见（page.html:2233），拿到答案后一律有结论 ——
//    关掉上报、取不到数都**照样可见**，只让文案说实话；要防的只有「重上一次的数字」（旧数字必须消失）。
async function scenarioCommunityRowStaysVisibleAndTruthful() {
  const problems = [];
  const src = extractFunction(page, 'initCommunityCounts');
  if (!src || src.indexOf('rail-community') < 0) return {pass: false, detail: '找不到 guide.js 的 initCommunityCounts'};
  // 静态占位「社区在线：…」要藏：进门第一件事就是 hidden=true（在 api 检查之前）。
  const hideAt = src.indexOf('line.hidden=true;');
  const apiCheckAt = src.indexOf('typeof api');
  if (hideAt < 0 || apiCheckAt < 0 || hideAt > apiCheckAt) {
    problems.push('进门前没先把静态占位藏起来（line.hidden=true 应在 typeof api 检查之前）');
  }
  const line = makeElement('rail-community');
  line.hidden = false;  // page.html:2233 的占位默认可见
  line.textContent = '社区在线：…';
  const fakeDocument = {getElementById(id) { return id === 'rail-community' ? line : null; }};
  const fakeWindow = {setTimeout(fn) { fn(); return 0; }};
  let mode = 'online';
  let countCalls = 0;
  const api = function (url) {
    if (url.indexOf('/api/settings') === 0) return Promise.resolve({community_telemetry: mode !== 'closed'});
    countCalls += 1;
    if (mode === 'offline') return Promise.reject(new Error('网络不通'));
    if (mode === 'unavailable') return Promise.resolve({counts: {online: null, today: null}});
    return Promise.resolve({counts: {online: 3, today: 5}});
  };
  const factory = new Function('document', 'window', 'api', src + '\nreturn initCommunityCounts;');
  const run = factory(fakeDocument, fakeWindow, api);
  const settle = function () { return new Promise(function (resolve) { setTimeout(resolve, 5); }); };
  run();
  await settle();
  if (line.hidden) problems.push('有真数字时状态行却藏着');
  if (String(line.textContent).indexOf('3') < 0) problems.push('在线数没写进状态行：' + line.textContent);
  // 由在线转「未开启」：行必须还在，文案换成「未开启」，上一轮的数字不许留着。
  mode = 'closed';
  run();
  await settle();
  if (line.hidden) problems.push('上报开关关掉后整行消失了（应可见 + 文案「未开启」）');
  if (String(line.textContent).indexOf('未开启') < 0) problems.push('关掉上报后没说「未开启」：' + line.textContent);
  if (String(line.textContent).indexOf('3') >= 0) problems.push('关掉上报后还挂着上一轮的数字：' + line.textContent);
  // 双 null（刚打开还没攒出数）：可见，文案「暂不可用」。
  line.textContent = '社区在线：3 人在用';  // 故意留一行旧数字，验证会被覆盖
  mode = 'unavailable';
  run();
  await settle();
  if (line.hidden) problems.push('两个数都是 null 时整行消失了（应可见 +「暂不可用」）');
  if (String(line.textContent) !== '社区在线：暂不可用') problems.push('双 null 的文案不对：' + line.textContent);
  // 请求挂了（重试一次仍失败）：可见，同样是「暂不可用」，不留旧数字。
  line.textContent = '社区在线：3 人在用';
  mode = 'offline';
  run();
  await settle();
  if (line.hidden) problems.push('取数失败后整行消失了（应可见 +「暂不可用」）');
  if (String(line.textContent) !== '社区在线：暂不可用') problems.push('取数失败后的文案不对：' + line.textContent);
  if (countCalls < 4) problems.push('假 api 没被真的调到（countCalls=' + countCalls + '），用例没锁到行为');
  return {
    pass: problems.length === 0,
    detail: problems.join('; ') || ('社区状态行：有结论就可见、文案说实话，不重上一次数字（计数请求 ' + countCalls + ' 次）'),
  };
}


function scenarioFeedbackDismissal() {
  const nodes = {};
  ['action-feedback','feedback-message','feedback-retry','feedback-close'].forEach(id => nodes[id] = makeElement(id));
  let timer;
  const win = {clearTimeout() { timer=null; },setTimeout(fn) { timer=fn; return 1; },NHNotify:{push(){}}};
  const run = new Function('document','window','NHNotify','loadTasks',
    'var feedbackLoadingTimer;'+['hideActionFeedback','showFeedback','renderActionFeedback'].map(n=>extractFunction(page,n)).join('\n')+
    ';return {show:showFeedback,render:renderActionFeedback};');
  const api = run({getElementById:id=>nodes[id]},win,win.NHNotify,()=>{});
  api.show('失败','error');
  if(nodes['action-feedback'].hidden) return {pass:false,detail:'失败反馈未显示'};
  nodes['feedback-close'].onclick();
  if(!nodes['action-feedback'].hidden) return {pass:false,detail:'关闭未隐藏反馈'};
  api.show('失败','error'); api.show('已保存','success');
  if(!nodes['action-feedback'].hidden) return {pass:false,detail:'成功后旧失败仍显示'};
  api.render('正在保存','loading'); timer();
  return {pass:nodes['action-feedback'].dataset.state==='error' && nodes['feedback-message'].textContent.includes('尚未确认'),detail:'关闭、成功清旧错误、等待超时'};
}
function scenarioTokenHintExpires() {
  const hint=makeElement('token-hint');const timers=new Map();let i=0;
  const run=new Function('document','window','var tokenHintTimer;'+extractFunction(page,'showTokenHint')+';return showTokenHint;');
  const show=run({getElementById:()=>hint},{setTimeout(fn,ms){if(ms!==8000)throw Error('收起时间不对');timers.set(++i,fn);return i;},clearTimeout(id){timers.delete(id);}});
  show('第一次');show('第二次');
  if(timers.size!==1 || hint.textContent!=='第二次' || hint.hidden)return {pass:false,detail:'重复提示或定时器未合并'};
  [...timers.values()][0]();return {pass:hint.hidden,detail:'重复显示只保留最新提示，8秒收起'};
}
async function scenarioCompletionRefreshFailure() {
  const card=makeElement('task-1');const button={disabled:false};card.querySelectorAll=()=>[button];
  const classes = new Set();
  card.classList = {add(...names){names.forEach(n=>classes.add(n));},remove(...names){names.forEach(n=>classes.delete(n));},contains(n){return classes.has(n);}};
  const run=new Function('document','api','syncTasks','animateConfirmedTaskCompletion','showFeedback',extractFunction(page,'runTaskMutation')+';return runTaskMutation;');
  const mutate=run({getElementById:()=>card},()=>Promise.resolve({ok:true}),()=>Promise.reject(Error('刷新失败')),()=>{card.classList.add('completion-confirmed');card.classList.add('completing');return Promise.resolve();},()=>{});
  await mutate({id:1},{action:'done'},'正在完成','已完成',button,()=>{});
  return {pass:!button.disabled&&!card.classList.contains('completion-confirmed')&&!card.classList.contains('completing'),detail:'服务端确认但刷新失败时清除淡出并恢复按钮'};
}
function scenarioSearchRestoresLatestSnapshot() {
  const start=page.indexOf('  function applyFilter() {'),end=page.indexOf('\n  }',start);
  const panel={hidden:true,dataset:{},getAttribute(){return 'sample';}};
  const search={value:'词'};const empty={};
  const run=new Function('search','panels','items','collectPanels','emptyNode','statusCardEl','setGroupsFiltered','countLabel','sync','panelText','buttonFor','setActive','var observer = null;'+page.slice(start,end+4)+';return applyFilter;');
  const apply=run(search,[panel],[],()=>{},()=>empty,()=>null,()=>{},null,()=>{},()=> '词',()=>null,()=>{});
  apply();search.value='';apply();
  if(!panel.hidden||'wasHidden' in panel.dataset)return {pass:false,detail:'第一次搜索未恢复或未清快照'};
  panel.hidden=false;search.value='词';apply();search.value='';apply();
  return {pass:!panel.hidden&&!('wasHidden' in panel.dataset),detail:'两次搜索之间修改显隐，恢复第二次快照'};
}
function scenarioModalEscapeAndFocus() {
  const results=[];
  for(const id of ['star-prompt','journey']) {
    let observer,keydown;
    const trigger={isConnected:true,focus(){doc.activeElement=this;}};
    const a={disabled:false,tabIndex:0,getClientRects(){return [1];},focus(){doc.activeElement=this;}};
    const b={...a,focus(){doc.activeElement=this;}};
    const box={hidden:true,querySelectorAll(){return [a,b];},addEventListener(name,fn){keydown=fn;}};
    const doc={activeElement:trigger,getElementById(){return box;}};
    const init=new Function('document','MutationObserver','var journeyPaused=false;'+extractFunction(page,'initDismissibleModal')+';return initDismissibleModal;');
    init(doc,function(fn){observer=fn;this.observe=()=>{};})(id);
    box.hidden=false;observer();if(doc.activeElement!==a)return {pass:false,detail:id+' 未聚焦首项'};
    doc.activeElement=b;keydown({key:'Tab',preventDefault(){}});if(doc.activeElement!==a)return {pass:false,detail:id+' Tab逃出'};
    keydown({key:'Tab',shiftKey:true,preventDefault(){}});if(doc.activeElement!==b)return {pass:false,detail:id+' ShiftTab逃出'};
    keydown({key:'Escape',preventDefault(){},stopPropagation(){}});
    results.push(box.hidden && doc.activeElement===trigger);
  }
  return {pass:results.every(Boolean),detail:'两个弹窗首项聚焦、双向Tab闭环、Esc关闭并还焦'};
}

function scenarioOfflineButtonsRecover() {
  // 服务端挂了但网络好：恢复不能只指望 online 事件——它永远不会来。
  // 任何请求成功时 api() 都会调 nhNet.ok()，这里就走那条路，全程不发 online。
  const ids = ['net-bar','net-bar-text','net-bar-retry','agent-send','agent-voice','trip-routine-open','trip-schedule-open','trip-schedule-save','trip-schedule-shot'];
  const env = makeEnv(ids);
  env.document.body = makeElement('body');
  env.document.querySelectorAll = function (sel) {
    if (sel !== '[data-offline-disabled]') return [];
    return ids.map(function (id) { return env.elements[id]; })
      .filter(function (node) { return node && node.disabled && node.dataset.offlineDisabled === '1'; });
  };
  let onlineHandlers = 0;
  env.window.addEventListener = function (name) { if (name === 'online') onlineHandlers += 1; };
  env.window.dispatchEvent = function () {};
  const start = page.indexOf('window.nhNet = (function () {');
  const end = page.indexOf('})();', start);
  if (start < 0 || end < 0) throw new Error('找不到 nhNet 定义');
  const nhNet = new Function('document', 'window', 'navigator', page.slice(start, end) + '})(); return window.nhNet;')(
    env.document, env.window, {onLine: true}
  );
  if (!nhNet || typeof nhNet.fail !== 'function') return {pass: false, detail: 'nhNet 没建起来'};
  nhNet.fail();
  const greyed = ['agent-send', 'agent-voice'].every(function (id) {
    return env.elements[id].disabled && env.elements[id].dataset.offlineDisabled === '1';
  });
  if (!greyed) return {pass: false, detail: '服务连不上时动作按钮没置灰'};
  env.window.nhNet = nhNet;
  global.fetch = function () {
    return Promise.resolve({ok: true, status: 200, json: function () { return Promise.resolve({ok: true}); }});
  };
  return buildApiModule(env).api('/api/quick', {}).then(function () {
    const restored = ['agent-send', 'agent-voice'].every(function (id) {
      return !env.elements[id].disabled && env.elements[id].dataset.offlineDisabled === undefined;
    });
    return {
      pass: restored,
      detail: restored
        ? '没收到 online 事件，一次请求成功就把按钮放开了（online 监听数 ' + onlineHandlers + '）'
        : '请求成功后按钮仍是灰的',
    };
  }, function (error) {
    return {pass: false, detail: '请求路径抛错：' + (error && error.message)};
  });
}

function scenarioFeedbackRetryStaysClickable() {
  const nodes = {};
  ['action-feedback','feedback-message','feedback-retry','feedback-close'].forEach(function (id) { nodes[id] = makeElement(id); });
  const win = {clearTimeout() {}, setTimeout() { return 1; }, NHNotify: {push() {}}};
  const run = new Function('document', 'window', 'NHNotify',
    'var feedbackLoadingTimer;'
    + ['hideActionFeedback', 'showFeedback', 'renderActionFeedback'].map(function (n) { return extractFunction(page, n); }).join('\n')
    + ';return {show: showFeedback};');
  const api = run({getElementById: function (id) { return nodes[id]; }}, win, win.NHNotify);
  let calls = 0;
  api.show('保存失败', 'error', function () { calls += 1; return Promise.reject(new Error('又失败')); });
  const button = nodes['feedback-retry'];
  if (button.hidden) return {pass: false, detail: '带重试的失败没出现重试按钮'};
  button.onclick();
  if (calls !== 1) return {pass: false, detail: '点了重试却没调起重试'};
  if (!button.disabled) return {pass: false, detail: '点下去没有防连点'};
  return Promise.resolve().then(function () { return Promise.resolve(); }).then(function () {
    return {pass: button.disabled === false, detail: button.disabled ? '重试失败后按钮一直灰着' : '重试失败后按钮仍可再点'};
  });
}

function scenarioResuggestDoesNotLockItself() {
  const marker = "if(target.closest('[data-gsel-resuggest]')){";
  const start = page.indexOf(marker);
  if (start < 0) throw new Error('找不到「重新识别」分支');
  const stop = page.indexOf('\n        }', start);
  if (stop < 0) throw new Error('「重新识别」分支没找到结尾');
  const block = page.slice(start, stop + 10);
  let calls = 0;
  const target = {closest: function (sel) { return sel === '[data-gsel-resuggest]' ? {} : null; }};
  new Function('target', 'groupRetry', 'kind', block)(target, function () { calls += 1; }, 'wechat');
  if (calls !== 1) return {pass: false, detail: '点击「重新识别」没有触发拉取'};
  if (block.indexOf('button.disabled') >= 0) return {pass: false, detail: '又把自己置灰了：拉取完成没有回调负责还原'};
  return {pass: true, detail: '点击「重新识别」触发一次拉取，且不把自己锁成灰色的终态'};
}

async function scenarioRoutineChipDropReordersStops() {
  const problems = [];
  const slop = (page.match(/var CHIP_DRAG_SLOP = \d+;/) || [''])[0];
  if (!slop) problems.push('trip.js 里找不到 CHIP_DRAG_SLOP（短按/拖动的分界没了）');
  // trip.js 的函数都缩在 IIFE 里，不像 page.html 那样顶格收尾，所以按花括号配对切，不靠 '\n}'。
  function sliceFn(src, name) {
    const start = src.indexOf('function ' + name + '(');
    if (start < 0) return '';
    let depth = 0;
    for (let i = src.indexOf('{', start); i >= 0 && i < src.length; i += 1) {
      if (src[i] === '{') depth += 1;
      else if (src[i] === '}') {
        depth -= 1;
        if (depth === 0) return src.slice(start, i + 1);
      }
    }
    return '';
  }
  const parts = ['chipPressKind', 'routineSlotAt', 'moveRoutineStop', 'appendRoutineStop'].map(function (name) {
    const src = sliceFn(page, name);
    if (!src) problems.push('trip.js 里找不到 ' + name);
    return src || '';
  });
  if (problems.length) return {pass: false, detail: problems.join('; ')};
  const factory = new Function(
    slop + '\n' + parts.join('\n') +
    '\nreturn {chipPressKind: chipPressKind, routineSlotAt: routineSlotAt,' +
    ' moveRoutineStop: moveRoutineStop, appendRoutineStop: appendRoutineStop};'
  );
  const lib = factory();
  // 手指短按不许变成拖：chip 上抖 3px 还是「点一下」。
  if (lib.chipPressKind(3, 0) !== 'tap') problems.push('3px 的抖动被当成拖动了（短按会变拖）');
  if (lib.chipPressKind(0, 8) !== 'drag') problems.push('8px 的移动没被当成拖动');

  // 落点：三行各 40px 高，比中线决定「插这一行前面还是后面」。
  const rows = [0, 1, 2].map(function (i) {
    return {getBoundingClientRect() { return {top: i * 40, bottom: i * 40 + 40, height: 40, left: 0, right: 200}; }};
  });
  const fakeDoc = {getElementById(id) {
    if (id !== 'trip-list') return null;
    return {querySelectorAll(sel) {
      if (sel !== '.trip-card') return [];
      return [{querySelectorAll(inner) { return inner === '.trip-row' ? rows : []; }}];
    }};
  }};
  const first = lib.routineSlotAt(fakeDoc, 100, 15);
  if (!first || first.trip !== 0 || first.index !== 0) {
    problems.push('指针在第一行上半时落点不是「第一个」（' + JSON.stringify(first && {t: first.trip, i: first.index}) + '）');
  }
  const second = lib.routineSlotAt(fakeDoc, 100, 72);
  if (!second || second.index !== 2) problems.push('指针在第二行下半时落点不是 index=2');
  if (lib.routineSlotAt(fakeDoc, 900, 72) !== null) problems.push('指针在卡片外（地图那边）也算出落点了，会凭空插一条');

  // 真顺序：A,B,C 三站，把 B 拖到第一位 → 必须变成 B,A,C，而且不多出一条。
  const plan = {trips: [{stops: [{title: 'A'}, {title: 'B'}, {title: 'C'}]}]};
  if (!lib.moveRoutineStop(plan, 'B', 0, 0)) problems.push('moveRoutineStop 说没挪成');
  const order = plan.trips[0].stops.map(function (s) { return s.title; }).join(',');
  if (order !== 'B,A,C') problems.push('把 B 拖到第一位后顺序不是 B,A,C，是 ' + order);
  if (plan.trips[0].stops.length !== 3) problems.push('挪完站数变了：' + plan.trips[0].stops.length);

  // 轻点一下 = 插到最后一趟末尾。
  const plan2 = {trips: [{stops: [{title: 'A'}]}, {stops: [{title: 'C'}]}]};
  if (!lib.appendRoutineStop(plan2, 'A')) problems.push('轻点插入没成功');
  const tail = plan2.trips[1].stops.map(function (s) { return s.title; }).join(',');
  if (tail !== 'C,A') problems.push('轻点插入后最后一趟不是 C,A，是 ' + tail);

  // 触发路径：chip 上带着要挪的那一站，且触屏走 pointer（手机上 HTML5 dragstart 不触发）。
  const chips = sliceFn(page, 'renderRoutineChips');
  if (!chips || chips.indexOf('data-routine-title') < 0) problems.push('chip 上没有 data-routine-title，拖动时不知道该挪哪一站');
  if (page.indexOf("addEventListener('pointerdown'") < 0) problems.push('chip 上没有 pointerdown 监听（触屏拖不起来）');
  if (page.indexOf('trip-drop-line') < 0) problems.push('没有落点插入线（trip-drop-line）');
  // 挪完行程和地图都要重画：dropRoutine 里两处都得在。
  const drop = sliceFn(page, 'dropRoutine');
  if (!drop || drop.indexOf('renderTrips(state.plan)') < 0) problems.push('dropRoutine 没重画行程列表');
  if (!drop || drop.indexOf('drawMap(state.plan)') < 0) problems.push('dropRoutine 没重画地图');
  return {
    pass: problems.length === 0,
    detail: problems.join('; ') || '拖动插进行程：落点按行中线算、顺序真的变成 B,A,C、短按仍是插入、触屏走 pointer、松手重画行程与地图'
  };
}

async function scenarioRoutineChipDropSavesOrder() {
  const problems = [];
  function sliceFn(src, name) {
    const start = src.indexOf('function ' + name + '(');
    if (start < 0) return '';
    let depth = 0;
    for (let i = src.indexOf('{', start); i >= 0 && i < src.length; i += 1) {
      if (src[i] === '{') depth += 1;
      else if (src[i] === '}') {
        depth -= 1;
        if (depth === 0) return src.slice(start, i + 1);
      }
    }
    return '';
  }
  const parts = ['pushPlanOrder', 'planTitles', 'dropRoutine', 'moveRoutineStop', 'appendRoutineStop'].map(function (name) {
    const src = sliceFn(page, name);
    if (!src) problems.push('trip.js 里找不到 ' + name);
    return src || '';
  });
  if (problems.length) return {pass: false, detail: problems.join('; ')};

  // dropRoutine 只认 state / renderTrips / drawMap / orderNote / api 这几样，替身摆好再跑。
  function build(apiImpl) {
    const body =
      "var PLAN = '/api/agent/plan';\n" +
      "var state = {plan: {date: '2026-11-17', trips: [{stops: [{title: 'A'}, {title: 'B'}, {title: 'C'}]}]}};\n" +
      "var notes = [], calls = [], rendered = null;\n" +
      "function orderNote(text) { notes.push(text); }\n" +
      "function renderTrips() {}\n" +
      "function drawMap() {}\n" +
      "function render(plan) { rendered = plan; state.plan = plan; }\n" +
      "function api(url, opts) { calls.push({url: url, opts: opts}); return apiImpl(url, opts); }\n" +
      parts.join('\n') +
      "\nreturn {drop: dropRoutine, state: function () { return state; }, notes: function () { return notes; }," +
      " calls: function () { return calls; }, rendered: function () { return rendered; }};";
    return new Function('apiImpl', body)(apiImpl);
  }

  // 存成功：松手发一次 POST，带上当天日期和用户排出来的顺序；页面上按服务端重算的 plan 重画。
  const serverPlan = {date: '2026-11-17', trips: [{stops: [{title: 'B'}, {title: 'A'}, {title: 'C'}]}]};
  const ok = build(function () { return Promise.resolve({ok: true, plan: serverPlan}); });
  await ok.drop('B', {trip: 0, index: 0});
  const calls = ok.calls();
  const call = calls[0];
  const payload = call ? JSON.parse(call.opts.body || '{}') : {};
  if (calls.length !== 1) problems.push('松手发了 ' + calls.length + ' 次请求（该只发一次）');
  if (!call || call.url !== '/api/agent/plan') problems.push('松手后没有往 /api/agent/plan 存顺序');
  if (!call || call.opts.method !== 'POST') problems.push('存顺序用的不是 POST');
  if (payload.action !== 'order') problems.push('请求里没有 action=order');
  if (payload.date !== '2026-11-17') problems.push('请求里没带日期，服务端不知道存哪一天：' + JSON.stringify(payload.date));
  if ((payload.order || []).join(',') !== 'B,A,C') {
    problems.push('请求里的顺序不是用户排的 B,A,C，是 ' + JSON.stringify(payload.order));
  }
  if (ok.rendered() !== serverPlan) problems.push('存成功后没按服务端重算的 plan 重画（页面在自己猜存成什么样）');
  if (!ok.notes().some(function (t) { return t.indexOf('存下来') >= 0; })) {
    problems.push('存成功后没告诉用户顺序已经存下');
  }

  // 存失败：页面上的顺序不许回滚（回滚等于「什么都没发生」），但要说清这次没存上。
  const bad = build(function () { return Promise.reject(new Error('服务没开')); });
  await bad.drop('B', {trip: 0, index: 0});
  const after = bad.state().plan.trips[0].stops.map(function (s) { return s.title; }).join(',');
  if (after !== 'B,A,C') problems.push('存失败时把页面上的顺序回滚了（现在是 ' + after + '）');
  if (!bad.notes().some(function (t) { return t.indexOf('没存上') >= 0; })) {
    problems.push('存失败时没说清顺序没存上');
  }
  // 轻点一条本来就在最后一趟末尾的：什么都不该变，也不该白存一次没人改过的顺序。
  const same = build(function () { return Promise.resolve({ok: true, plan: serverPlan}); });
  await same.drop('C', null);
  if (same.calls().length !== 0) problems.push('明明没动，还是发了一次保存请求');
  if (!same.notes().some(function (t) { return t.indexOf('没动') >= 0; })) {
    problems.push('没挪动的时候没说清「没动」，看着像真的挪了');
  }
  return {
    pass: problems.length === 0,
    detail: problems.join('; ') || '松手后 POST action=order（带当天日期与用户排的顺序），成功按服务端 plan 重画、失败保留页面顺序并明说没存上'
  };
}

// surfaceHide 的两条锁死行为（page.html 的浮层出场帮手）。
// 全局 @media(prefers-reduced-motion:reduce) 把 animation:none!important 打到底，
// animationend 在减少动态的机器上永远不来，兜底的 setTimeout(finish,180) 是唯一出路——
// 所以这两条一条走 reduce 早退、一条走「两个出口都到，done 只跑一次」，只测正常路径等于没测。
function scenarioSurfaceHideReducedMotion() {
  const env = makeEnv([]);
  env.window.matchMedia = function () { return { matches: true }; };
  const surfaceHide = new Function('window', 'return ' + page.slice(
    page.indexOf('function surfaceHide(el, done) {'),
    page.indexOf('window.surfaceShow', page.indexOf('function surfaceHide(el, done) {'))
  ))(env.window);
  if (typeof surfaceHide !== 'function') return {pass: false, detail: '找不到 surfaceHide 定义'};
  let doneCalls = 0;
  const el = makeElement('probe');
  el.hidden = false;
  el.classList.add('surface-in');
  surfaceHide(el, function () { doneCalls += 1; });
  const ok = el.hidden === true && doneCalls === 1 && !el.classList.contains('surface-out');
  return {
    pass: ok,
    detail: ok ? '减少动态时 surfaceHide 立刻 hidden 并回调 done，不挂过渡也不留 surface-out'
      : '减少动态下没立刻收起（hidden=' + el.hidden + '，done 调了 ' + doneCalls + ' 次）'
  };
}

function scenarioSurfaceHideDoneRunsExactlyOnce() {
  const env = makeEnv([]);
  // animationend 正常会来、reduce 又没开：两个出口（animationend + 180ms 兜底）都会到。
  env.window.matchMedia = function () { return { matches: false }; };
  let fastForward = null;
  const realSetTimeout = env.window.setTimeout;
  env.window.setTimeout = function (fn, ms) { fastForward = fn; return realSetTimeout(fn, ms); };
  const surfaceHide = new Function('window', 'return ' + page.slice(
    page.indexOf('function surfaceHide(el, done) {'),
    page.indexOf('window.surfaceShow', page.indexOf('function surfaceHide(el, done) {'))
  ))(env.window);
  if (typeof surfaceHide !== 'function') return {pass: false, detail: '找不到 surfaceHide 定义'};
  let doneCalls = 0;
  const el = makeElement('probe');
  el.hidden = false;
  const listeners = {};
  el.addEventListener = function (name, fn) { (listeners[name] = listeners[name] || []).push(fn); };
  surfaceHide(el, function () { doneCalls += 1; });
  if (el.hidden !== false) return {pass: false, detail: '还没放完动画元素就被提前收起了'};
  // 出口一：动画真放完了。
  (listeners.animationend || []).forEach(function (fn) { fn(); });
  if (doneCalls !== 1 || el.hidden !== true || el.classList.contains('surface-out')) {
    return {pass: false, detail: 'animationend 之后没干净收尾（done=' + doneCalls + '，hidden=' + el.hidden + '）'};
  }
  // 出口二：兜底定时器也到了（后台标签页里 animationend 会丢，两个出口都会触发）。
  if (fastForward) fastForward();
  const ok = doneCalls === 1 && el.hidden === true && !el.classList.contains('surface-out');
  return {
    pass: ok,
    detail: ok ? 'animationend 与 180ms 兜底都到，done 只跑一次，元素收起且 surface-out 已摘掉'
      : '两个出口都触发后 done 跑了 ' + doneCalls + ' 次（应为 1）'
  };
}

const scenarios = [
  ['surface_hide_reduced_motion_hides_now', scenarioSurfaceHideReducedMotion],
  ['surface_hide_done_runs_exactly_once', scenarioSurfaceHideDoneRunsExactlyOnce],
  ['wizard_pause_resets', scenarioWizardPauseResets],
  ['offline_buttons_recover', scenarioOfflineButtonsRecover],
  ['feedback_retry_stays_clickable', scenarioFeedbackRetryStaysClickable],
  ['resuggest_does_not_lock_itself', scenarioResuggestDoesNotLockItself],
  ['feedback_dismissal', scenarioFeedbackDismissal],
  ['token_hint_expires', scenarioTokenHintExpires],
  ['completion_refresh_failure', scenarioCompletionRefreshFailure],
  ['search_latest_snapshot', scenarioSearchRestoresLatestSnapshot],
  ['modal_escape_focus', scenarioModalEscapeAndFocus],
  ['onboarding_wizard_mutex', scenarioOnboardingWizardMutex],
  ['api_timeout', scenarioApiTimeout],
  ['api_cancel', scenarioApiCancel],
  ['api_success_unchanged', scenarioApiSuccess],
  ['group_pull_timeout', scenarioGroupPullTimeout],
  ['group_pull_cancel', scenarioGroupPullCancel],
  ['announce_failure_hint', scenarioAnnounceFailure],
  ['announce_failure_keeps_items', scenarioAnnounceFailureKeepsItems],
  ['announce_panel_position', scenarioAnnouncePanelPosition],
  ['bell_read_state', scenarioBellReadState],
  ['tunnel_poll_stops_at_ready', scenarioTunnelPollStopsAtReady],
  ['update_checking_single_timer', scenarioUpdateCheckingPollKeepsSingleTimer],
  ['scan_timers_survive_settings_throw', scenarioScanTimersSurviveSettingsThrow],
  ['community_row_stays_visible_and_truthful', scenarioCommunityRowStaysVisibleAndTruthful],
  ['routine_chip_drop_reorders_stops', scenarioRoutineChipDropReordersStops],
  ['routine_chip_drop_saves_order', scenarioRoutineChipDropSavesOrder],
  ['tunnel_error_shows_retry', scenarioTunnelErrorShowsRetry],
  ['wizard_tunnel_step', scenarioWizardTunnelStep],
  ['update_guide_mutex', scenarioUpdateGuideMutex],
  ['update_poll_timeout', scenarioUpdatePollTimeout],
  ['update_abandon_stops_pending', scenarioUpdateAbandonStopsPolling],
  ['update_banner_three_states', scenarioUpdateBannerThreeStates],
  ['update_banner_percent_follows_polling', scenarioUpdateBannerPercentFollowsPolling],
  ['curl_variants_extract_same_cookie', scenarioCurlVariants],
  ['curl_paste_fills_empty_input', scenarioCurlPasteFillsEmpty],
  ['curl_paste_reports_missing_header', scenarioCurlPasteNoCookieHeader],
  ['curl_paste_keeps_existing_input', scenarioCurlPasteKeepsExisting],
  ['decrypt_failed_guidance_curl_steps', scenarioDecryptFailedGuidance],
  ['manual_save_posts_cookie_and_validates', scenarioManualSavePostsCookie],
  ['firewall_render_stores_port_from_api', scenarioFirewallRenderStoresPort],
  ['firewall_render_unchecked_keeps_entry', scenarioFirewallRenderUncheckedKeepsEntry],
  ['napcat_dir_normalize', scenarioNapcatDirNormalize],
  ['napcat_dir_save_keeps_typed_path', scenarioNapcatDirSaveKeepsTypedPath],
  ['wechat_key_save_validates_and_posts', scenarioWechatKeySaveValidatesAndPosts],
  ['wechat_key_save_explains_unsupported_backend', scenarioWechatKeySaveExplainsUnsupportedBackend],
  ['firewall_script_wiring_uses_dataset_port', scenarioFirewallScriptWiringUsesDataset],
  ['firewall_script_blob_ascii_port', scenarioFirewallScriptBlobAsciiPort],
  ['firewall_script_download_failure_hint', scenarioFirewallScriptDownloadFailureHint],
  ['cold_start_guide', scenarioColdStartGuide],
  ['wechat_dir_cleared_survives_rerender', scenarioWechatDirClearedSurvivesRerender],
  ['wizard_target_scroll', scenarioWizardScroll],
  ['manual_task_correction_swapped', scenarioManualDelete],
  ['qq_connect_state_buttons', scenarioQQButtons],
];
let chain = Promise.resolve();
scenarios.forEach(function (entry) {
  chain = chain.then(function () {
    return Promise.resolve().then(entry[1]).then(function (result) {
      report[entry[0]] = result;
    }, function (error) {
      report[entry[0]] = {pass: false, detail: '场景崩溃：' + (error && (error.stack || error.message) || error)};
    });
  });
});
chain.then(function () {
  process.stdout.write(JSON.stringify(report));
}, function (error) {
  process.stdout.write(JSON.stringify({fatal: String(error && (error.stack || error.message) || error)}));
  process.exitCode = 2;
});
"""

    report: dict = {}

    @classmethod
    def setUpClass(cls) -> None:
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("本机没有 node，跳过行为级 JS 测试")
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "harness.js"
            script.write_text(cls.HARNESS_JS, encoding="utf-8")
            result = subprocess.run(
                [node, str(script), str(Path(webapp.__file__).resolve().parent / "web_static" / "page.html")],
                capture_output=True,
                timeout=90,
            )
        if result.returncode != 0:
            raise AssertionError(
                f"harness 崩溃，退出码 {result.returncode}\nstderr:\n{result.stderr.decode('utf-8', 'replace')}"
            )
        try:
            cls.report = json.loads(result.stdout.decode("utf-8"))
        except json.JSONDecodeError as error:
            raise AssertionError(
                "harness 输出不是 JSON："
                f"{error}\nstdout:\n{result.stdout.decode('utf-8', 'replace')}\n"
                f"stderr:\n{result.stderr.decode('utf-8', 'replace')}"
            ) from error
        if "fatal" in cls.report:
            raise AssertionError("harness fatal: " + str(cls.report["fatal"]))

    def _assert_scenario(self, name: str) -> None:
        case = self.report.get(name)
        self.assertIsInstance(case, dict, "harness 没跑场景：" + name)
        self.assertTrue(case.get("pass"), f"{name} 失败：{case.get('detail')}")

    def test_feedback_error_can_close_and_success_clears_it(self) -> None:
        self._assert_scenario("feedback_dismissal")

    def test_token_hint_repeated_show_expires_once(self) -> None:
        self._assert_scenario("token_hint_expires")

    def test_completion_refresh_failure_restores_card_and_controls(self) -> None:
        self._assert_scenario("completion_refresh_failure")

    def test_search_restores_latest_panel_visibility(self) -> None:
        self._assert_scenario("search_latest_snapshot")

    def test_modal_escape_returns_focus_and_tab_stays_inside(self) -> None:
        self._assert_scenario("modal_escape_focus")

    def test_surface_hide_hides_at_once_under_reduced_motion(self) -> None:
        """浮层收起（行为级）：减少动效时立即隐藏并仍回调 done，不依赖 animationend。"""
        self._assert_scenario("surface_hide_reduced_motion_hides_now")

    def test_surface_hide_done_runs_exactly_once_when_both_exits_fire(self) -> None:
        """浮层收起（行为级）：animationend 与 180ms 兜底都触发时 done 只跑一次，元素收起且 surface-out 已摘掉。"""
        self._assert_scenario("surface_hide_done_runs_exactly_once")

    def test_offline_buttons_come_back_on_a_successful_request(self) -> None:
        self._assert_scenario("offline_buttons_recover")

    def test_feedback_retry_button_survives_a_failed_retry(self) -> None:
        self._assert_scenario("feedback_retry_stays_clickable")

    def test_resuggest_does_not_grey_itself_out(self) -> None:
        self._assert_scenario("resuggest_does_not_lock_itself")

    def test_api_default_timeout_shows_readable_error(self) -> None:
        self._assert_scenario("api_timeout")

    def test_api_signal_cancel_reports_cancelled(self) -> None:
        self._assert_scenario("api_cancel")

    def test_api_success_path_unchanged(self) -> None:
        self._assert_scenario("api_success_unchanged")

    def test_group_pull_timeout_recovers_button_and_copy(self) -> None:
        self._assert_scenario("group_pull_timeout")

    def test_group_pull_cancel_recovers_button_and_copy(self) -> None:
        self._assert_scenario("group_pull_cancel")

    def test_announcement_failure_shows_notice_hint(self) -> None:
        """公告拉取失败（行为级）：走右上角悬浮通知，可关闭、不重复吵、成功刷新后恢复公告。"""
        self._assert_scenario("announce_failure_hint")

    def test_announcement_failure_keeps_previous_items(self) -> None:
        """公告拉取失败（行为级）：不清空旧列表（网络抖动不能让公告瞬间消失又出现）。"""
        self._assert_scenario("announce_failure_keeps_items")

    def test_announce_panel_position_follows_bell(self) -> None:
        """面板定位（行为级）：top=铃铛底+8、right=视口-铃铛右，四边 clamp 进视口。"""
        self._assert_scenario("announce_panel_position")

    def test_bell_unread_state_counts_sorts_and_caps(self) -> None:
        """铃铛未读（行为级）：按日期倒序、重复标记不重复计、localStorage 只留最近 200 条、脏数据当全未读。"""
        self._assert_scenario("bell_read_state")

    def test_wizard_does_not_hide_onboarding_risk_notice(self) -> None:
        """首装向导与欢迎框互斥（行为级）：开/关向导都不藏欢迎框，封号风险提示第一次就能看到。"""
        self._assert_scenario("onboarding_wizard_mutex")

    def test_tunnel_poll_stops_at_ready_and_cancel_exits(self) -> None:
        """公网入口（行为级）：start 只发一次 → 轮询到 ready 立刻停、「取消」当场停并收起、危险地址不展示。"""
        self._assert_scenario("tunnel_poll_stops_at_ready")

    def test_tunnel_error_shows_retry_and_keeps_retrying(self) -> None:
        """公网入口失败态（行为级）：error 停轮询并露出「重试」、不显示也没法复制地址；点重试重新 start；
        retryable=false 时不出「重试」，但面板必须留着让用户看到原因。"""
        self._assert_scenario("tunnel_error_shows_retry")

    def test_wizard_tunnel_step_offers_one_click_and_remembers_skip(self) -> None:
        """首装向导公网步骤（行为级）：第 4 步有开通按钮与播报区；开通后按 2 秒轮询、ready 立刻停并显示地址；
        「以后再说」记下跳过键，下次不再拦这一步。"""
        self._assert_scenario("wizard_tunnel_step")

    def test_wizard_pause_resets_and_suppresses_journey_during_refresh(self):
        self._assert_scenario("wizard_pause_resets")

    def test_update_guide_shows_once_per_version_and_yields_to_announce(self) -> None:
        """升级引导页（行为级）：版本未知或 ?nowiz=1 不弹也不记；新版本弹一次并让开公告弹窗；
        点「稍后」记下本版本；首装向导开着时先不弹、也不写键（下次还能看到）。"""
        self._assert_scenario("update_guide_mutex")

    def test_update_download_poll_stops_after_no_progress_limit(self) -> None:
        self._assert_scenario("update_poll_timeout")

    def test_update_download_abandon_stops_pending_polling(self) -> None:
        self._assert_scenario("update_abandon_stops_pending")


    def test_update_banner_shows_download_progress_without_restart_hint(self) -> None:
        """横幅「后台下载中」（行为级）：文案带已下百分比、按钮是「查看进度」，不许出现「重启并更新」。"""
        self._assert_scenario("update_banner_three_states")

    def test_update_banner_says_new_version_is_downloaded(self) -> None:
        """横幅「新版本已下好」（行为级）：说清已下好并指向托盘『重启并更新』，不再让人干等。"""
        self._assert_scenario("update_banner_three_states")

    def test_update_banner_hidden_when_no_update(self) -> None:
        """横幅（行为级）：没有更新时保持隐藏、不带任何文字；点过关闭后同一条更新不再弹。"""
        self._assert_scenario("update_banner_three_states")

    def test_update_banner_percent_follows_download_progress(self) -> None:
        """横幅百分比跟着轮询刷新（行为级）：下载中每拉一次接口就重渲染一次，20% → 60% 跟着变；
        拉到最后返回 done 后切成「已下好」，且定时器真的停掉（不再发请求）。"""
        self._assert_scenario("update_banner_percent_follows_polling")

    def test_update_checking_poll_keeps_single_timer(self) -> None:
        """「后端还在检查更新」的自排续查（行为级）：连续三次 checking 状态只留一个计时器（旧的被清），
        切走设置页到点不再打接口，checking 结束时计时器当场清掉。"""
        self._assert_scenario("update_checking_single_timer")

    def test_scan_timers_survive_settings_throw(self) -> None:
        """设置面板构建中途抛异常（行为级）：钉钉/微信扫描轮询仍由 finally 重建，不会永久停更。"""
        self._assert_scenario("scan_timers_survive_settings_throw")

    def test_community_row_stays_visible_and_truthful(self) -> None:
        """社区在线状态行（行为级）：关掉上报、双 null、取数失败都照样可见，只说「未开启」/「暂不可用」；
        由在线转「未开启」后旧数字必须消失（不重上一次的「12 人在用」）。"""
        self._assert_scenario("community_row_stays_visible_and_truthful")

    def test_curl_paste_extracts_cookie_from_three_variants(self) -> None:
        """三种 cURL 变体（单引号 -H / 双引号 -H / --header）抽出同一份 Cookie。"""
        self._assert_scenario("curl_variants_extract_same_cookie")

    def test_routine_chip_drag_inserts_at_dropped_position(self) -> None:
        """常做的事拖进行程：落点按行中线算，把 B 拖到第一位后顺序真的变成 B,A,C；短按仍是插入。"""
        self._assert_scenario("routine_chip_drop_reorders_stops")

    def test_routine_chip_drop_persists_order_through_api(self) -> None:
        """松手要把顺序存进服务端（行为级）：POST /api/agent/plan 带 action=order、当天日期和用户排的顺序，
        成功按服务端重算的 plan 重画；失败保留页面上的顺序并明说没存上（不回滚成「什么都没发生」）。"""
        self._assert_scenario("routine_chip_drop_saves_order")

    def test_curl_paste_fills_empty_input_with_confirmation_hint(self) -> None:
        """空输入框粘贴 cURL：自动填入登录凭证并提示「已从 cURL 中提取出登录凭证，请确认后保存」。"""
        self._assert_scenario("curl_paste_fills_empty_input")

    def test_curl_paste_reports_missing_cookie_header(self) -> None:
        """cURL 里没有 cookie 头：明确报「这段内容里没有 Cookie」、不填值；普通文本粘贴不被拦截。"""
        self._assert_scenario("curl_paste_reports_missing_header")

    def test_curl_paste_keeps_existing_input_content(self) -> None:
        """输入框已有内容时不静默覆盖，给出清空后重粘的提示。"""
        self._assert_scenario("curl_paste_keeps_existing_input")

    def test_decrypt_failed_guidance_shows_curl_manual_steps(self) -> None:
        """v20/解密失败引导：说明不是配置问题 + F12→Copy as cURL→粘贴；其它 category 文案不变。"""
        self._assert_scenario("decrypt_failed_guidance_curl_steps")

    def test_manual_cookie_save_posts_cookie_and_validates(self) -> None:
        """手动保存链路（行为级）：空输入提示粘贴；有值 POST chaoxing_cookie → 校验 → 归位。"""
        self._assert_scenario("manual_save_posts_cookie_and_validates")

    def test_firewall_render_stores_api_port_for_script(self) -> None:
        """配对页把 /api/pair/info 里的真实端口存到防火墙框（脚本下载用，不写死）。"""
        self._assert_scenario("firewall_render_stores_port_from_api")

    def test_firewall_unchecked_still_shows_entry_and_says_needs_admin(self) -> None:
        """首次非提权（checked=false）也要看得到「一键放行」，且不许把「没查成」说成「没放行」。"""
        self._assert_scenario("firewall_render_unchecked_keeps_entry")

    def test_napcat_dir_input_is_editable_and_normalizes_pasted_paths(self) -> None:
        """issue #4：网页里直接填 NapCat 目录（不改 .env）；粘贴来的引号、正反斜杠要自动洗干净。"""
        self.assertIn('<input id="napcat-dir" type="text" autocomplete="off" placeholder="NapCat 安装目录">', PAGE_SOURCE)
        self.assertIn('<button id="napcat-dir-save" class="btn" type="button">保存并重载</button>', PAGE_SOURCE)
        self.assertIn("setupApi('/api/settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({napcat_dir: value})})", PAGE_SOURCE)
        self._assert_scenario("napcat_dir_normalize")

    def test_napcat_dir_save_keeps_typed_path_when_engine_not_found(self) -> None:
        """issue #4：保存成功但没认出引擎时不能清空输入框，否则用户会以为没保存上。"""
        self._assert_scenario("napcat_dir_save_keeps_typed_path")

    def test_wizard_scroll_fixed(self) -> None:
        """「去配置」不能赌固定 150ms：等稳定才滚、版面再变校正一次、超高目标顶部落页头下。"""
        self._assert_scenario("wizard_target_scroll")
        # 四个旧 `block:'center'` 直调点全部收编到共用 helper（超高目标的判据集中在那里）。
        self.assertIn("if (focused) setTimeout(function () { scrollTargetIntoView(focused); }, 40);", PAGE_SOURCE)
        self.assertIn("scrollTargetIntoView(target, motionPreferencePaused ? 'auto' : 'smooth');", PAGE_SOURCE)
        self.assertIn("scrollTargetIntoView(target, frozen ? 'auto' : 'smooth');", PAGE_SOURCE)
        self.assertIn("if(box)scrollTargetIntoView(box);", PAGE_SOURCE)
        self.assertIn("scrollTargetIntoView(target,'smooth');}", PAGE_SOURCE)

    def test_wechat_dir_cleared_survives_settings_rerender(self) -> None:
        """清空微信数据目录是合法状态：切 tab 再回来不能被旧目录回填，保存必须发空串。"""
        self._assert_scenario("wechat_dir_cleared_survives_rerender")

    def test_manual_shows_delete(self) -> None:
        """manual: 任务没有 AI 误判可言：三键换成「删除这条待办」（action:delete 契约），编辑键保留。"""
        self._assert_scenario("manual_task_correction_swapped")

    def test_wechat_key_guide_is_collapsed_and_saves_from_page(self) -> None:
        """issue #5：微信卡片里首选让 agent 代做，手动步骤降级为默认收起的「次选」，填 64 位十六进制直接存，不用改 .env。"""
        self.assertIn('<details id="wx-key-guide"><summary>次选：自己手动取（展开手动步骤）</summary>', PAGE_SOURCE)
        self.assertNotIn('<details id="wx-key-guide" open', PAGE_SOURCE)
        # agent-first：可转发的那句话必须在手动步骤之前，否则「首选」名不副实。
        self.assertIn("首选：让 AI 助手代做", PAGE_SOURCE)
        # 整句要能直接执行：给的是后端真实命令行入口与写入的 .env 路径，不许再指向跑不通的排查脚本。
        self.assertIn(r"帮我取微信密钥并写进群务台的 .env：在 F:\\qq-notice-hub 目录用 .venv\\Scripts\\python.exe 运行 -m noticehub.wechat_receiver --write-env F:\\notice-hub\\.env", PAGE_SOURCE)
        self.assertIn("成功会打印「已写入」，失败会说明卡在哪一步。", PAGE_SOURCE)
        # 旧句（前者是泛泛一句，后者指向 tools\wechat_probe.py 的死路）都不许残留。
        self.assertNotIn("写进 .env 的 NOTICEHUB_WECHAT_KEY", PAGE_SOURCE)
        self.assertNotIn(r"tools\\wechat_probe.py --help", PAGE_SOURCE)
        self.assertNotIn("先读 tools", PAGE_SOURCE)
        self.assertLess(PAGE_SOURCE.index("首选：让 AI 助手代做"), PAGE_SOURCE.index('<details id="wx-key-guide">'))
        self.assertEqual(PAGE_SOURCE.count('<details id="wx-key-guide">'), 1)
        self.assertIn('id="wx-key" type="text"', PAGE_SOURCE)
        self.assertIn('id="wx-key-save" class="btn primary" type="button">保存密钥</button>', PAGE_SOURCE)
        self.assertIn("https://github.com/search?q=wechat-dump-rs&amp;type=repositories", PAGE_SOURCE)
        self.assertIn("api('/api/settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({wechat_key: key})})", PAGE_SOURCE)
        # 状态行只读后端脱敏的 manual_key，不回显密钥内容
        self.assertIn("refreshWechatKeyStatus(!!v.manual_key)", PAGE_SOURCE)
        self.assertNotIn("wx-key').value=v.wechat_key", PAGE_SOURCE)
        self._assert_scenario("wechat_key_save_validates_and_posts")

    def test_wechat_key_save_says_so_when_backend_has_no_field(self) -> None:
        """后端还没接 wechat_key 时回「没有可保存的键」：页面要说清是版本问题，不许假装成功。"""
        self._assert_scenario("wechat_key_save_explains_unsupported_backend")

    def test_firewall_script_button_passes_stored_port(self) -> None:
        """下载按钮把 dataset.port（接口端口）传给生成函数。"""
        self._assert_scenario("firewall_script_wiring_uses_dataset_port")

    def test_firewall_script_blob_pure_ascii_with_localport(self) -> None:
        """.cmd 内容：localport=真实端口、@echo off 开头、纯 ASCII 无中文（cmd GBK 会乱码）。"""
        self._assert_scenario("firewall_script_blob_ascii_port")

    def test_firewall_script_download_failure_shows_hint(self) -> None:
        """下载被浏览器拦截时给明确提示，不留死路。"""
        self._assert_scenario("firewall_script_download_failure_hint")

    def test_cold_start_guide_points_at_qr_entry(self) -> None:
        """冷启动引导（行为级）：零待办且未接入才出现、可关闭；未启动 NapCat 时二维码位给按钮；点引导滚到二维码并高亮，减少动效时只滚不脉冲。"""
        self._assert_scenario("cold_start_guide")


class CalendarPrimaryAddressTests(unittest.TestCase):
    """B3：五六个网卡里必须给出唯一推荐地址，前端三个位置才不会各用一条。"""

    def _calendars(self, hosts, public=""):
        with mock.patch.object(webapp, "_lan_hosts", return_value=list(hosts)), \
             mock.patch.object(webapp, "_tailscale_calendar_url", return_value=public):
            return webapp._advertised_calendars(8766, "secret")

    def test_public_url_wins_when_funnel_is_up(self):
        calendars = self._calendars(["192.168.8.10", "172.27.32.1"], public="https://abc.ts.net/calendar.ics")
        self.assertEqual(webapp._primary_calendar_url(calendars), "https://abc.ts.net/calendar.ics?token=secret")
        self.assertEqual(calendars[0]["kind"], "public")
        self.assertFalse(calendars[0]["virtual"])

    def test_no_public_prefers_real_lan_over_virtual_and_tailscale(self):
        hosts = ["100.78.12.25", "100.127.59.175", "172.27.32.1", "192.168.56.1", "10.4.5.6", "192.168.8.10"]
        calendars = self._calendars(hosts)
        self.assertEqual(webapp._primary_calendar_url(calendars), "http://192.168.8.10:8766/calendar.ics?token=secret")
        # 虚拟网卡标出来，且排到最后
        virtual = {item["name"]: item["virtual"] for item in calendars}
        self.assertTrue(virtual["局域网 172.27.32.1"])
        self.assertTrue(virtual["局域网 192.168.56.1"])
        self.assertFalse(virtual["局域网 192.168.8.10"])
        self.assertFalse(virtual["局域网 10.4.5.6"])
        self.assertEqual(calendars[-1]["virtual"], True)
        # 真实 192.168 → 10.x → 100.x → 虚拟
        kinds = [item["url"].split("//")[1].split(":")[0] for item in calendars]
        self.assertEqual(kinds, ["192.168.8.10", "10.4.5.6", "100.127.59.175", "100.78.12.25", "172.27.32.1", "192.168.56.1"])

    def test_order_is_stable_across_calls(self):
        hosts = ["192.168.8.10", "10.4.5.6", "192.168.8.9", "100.78.12.25"]
        first = [item["url"] for item in self._calendars(hosts)]
        second = [item["url"] for item in self._calendars(list(reversed(hosts)))]
        self.assertEqual(first, second)

    def test_only_virtual_hosts_falls_back_to_first_without_raising(self):
        calendars = self._calendars(["172.27.32.1", "192.168.56.1"])
        primary = webapp._primary_calendar_url(calendars)
        self.assertEqual(primary, calendars[0]["url"])
        self.assertIn(":8766/calendar.ics", primary)


class ListenBacklogTest(unittest.TestCase):
    """半连接队列要够首屏并发用。

    socketserver 默认 backlog 是 5；Windows 上队列一满，新连接直接被拒（浏览器报
    ERR_CONNECTION_REFUSED），首页十几个脚本里随机缺几个，页面初始化整段不跑。
    真机上表现为「偶尔页面像坏的」——就是本用例锁住的行为。
    """

    def test_backlog_is_big_enough_for_a_first_paint(self) -> None:
        server_cls = webapp._BoundedThreadingHTTPServer
        self.assertGreaterEqual(server_cls.request_queue_size, 64)

    def test_started_server_actually_uses_that_backlog(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = Settings(
            web_host="127.0.0.1",
            web_port=0,
            web_token="secret",
            data_dir=Path(tmp.name) / "data",
        )
        server = webapp.TaskWebServer(settings, Store(Path(tmp.name) / "api.sqlite3"))
        self.assertTrue(server.start(), "TaskWebServer 启动失败")
        self.addCleanup(server.stop)
        assert server.server is not None
        self.assertIsInstance(server.server, webapp._BoundedThreadingHTTPServer)
        self.assertGreaterEqual(server.server.request_queue_size, 64)


class MetaPortSourceTests(unittest.TestCase):

    """`/api/meta` 必须说清端口从哪来：用户改了 .env 却听在别的端口，界面才有话可讲。"""

    def _free_port(self) -> int:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    def _serve(self, dotenv_port: int, env_port: int | None = None) -> dict:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        # 别的用例里 Settings.from_env() 会把 .env 灌进 os.environ（load_env_file 用 setdefault，
        # 灌进去就不退）。这两个键一旦残留，本用例读到的就是别人的端口和令牌 —— 全量跑时
        # 表现为 401/端口对不上。先摘干净，跑完再放回原值。
        for key in ("QQ_DIGEST_WEB_PORT", "QQ_DIGEST_WEB_TOKEN"):
            previous = os.environ.pop(key, None)
            if previous is not None:
                self.addCleanup(os.environ.__setitem__, key, previous)
        env_path = root / ".env"
        env_path.write_text(
            "QQ_DIGEST_WEB_PORT=%d\nQQ_DIGEST_WEB_TOKEN=secret\n" % dotenv_port, encoding="utf-8"
        )
        patcher = mock.patch.dict(
            os.environ,
            {"QQ_DIGEST_WEB_PORT": str(env_port)} if env_port is not None else {},
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        if env_port is None:
            os.environ.pop("QQ_DIGEST_WEB_PORT", None)
        settings = Settings.from_env(env_file=env_path)
        self.assertEqual(settings.web_port, env_port if env_port is not None else dotenv_port)

        store = Store(root / "meta.sqlite3")
        server = TaskWebServer(settings, store)
        self.assertTrue(server.start())
        self.addCleanup(server.stop)
        base = "http://127.0.0.1:%d" % settings.web_port
        request = urllib.request.Request(base + "/api/meta", headers={"X-Token": "secret"})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_source_is_dotenv_when_only_env_file_sets_port(self) -> None:
        port = self._free_port()
        body = self._serve(port)
        self.assertEqual(body["web_port_source"], "dotenv")
        self.assertEqual(body["web_port"], port)

    def test_source_is_env_when_environment_overrides_dotenv(self) -> None:
        dotenv_port = self._free_port()
        env_port = self._free_port()
        body = self._serve(dotenv_port, env_port)
        self.assertEqual(body["web_port"], env_port, "生效的是环境变量那一个")
        self.assertEqual(body["web_port_source"], "env")


class SetupAdviceTests(unittest.TestCase):
    """`/api/setup/status` 的「卡在哪 + 下一步」：用户第一个拦路点必须有话可讲、有处可点。"""

    MACHINE_WORDS = ("未配置", "incomplete", "pending", "not_installed", "needs_setup", "offline")

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "学院通知群"},
            web_host="127.0.0.1",
            web_port=0,
            web_token="secret",
            data_dir=Path(self.tmp.name) / "data",
        )
        self.store = Store(Path(self.tmp.name) / "advice.sqlite3")
        self.server = TaskWebServer(self.settings, self.store)
        self.assertTrue(self.server.start())
        self.addCleanup(self.server.stop)
        assert self.server.server is not None
        self.base = f"http://127.0.0.1:{self.server.server.server_address[1]}"

    def _payload(self, **patches) -> dict:
        with mock.patch("noticehub.webapp._Handler._napcat", return_value={"ok": False, "error": "连不上"}), \
             mock.patch("noticehub.webapp.napcat_admin.detect_boot", return_value=None), \
             mock.patch("noticehub.webapp.napcat_admin.running_pids", return_value=[]), \
             mock.patch("noticehub.webapp._napcat_port_accepts", return_value=False):
            request = urllib.request.Request(self.base + "/api/setup/status", headers={"X-Token": "secret"})
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))

    def test_every_platform_gets_plain_headline_and_valid_action(self) -> None:
        import noticehub.napcat_admin as na
        from noticehub import routes

        body = self._payload()
        api_paths = set(routes.GET_ROUTES) | set(routes.POST_ROUTES)
        for name in ("qq", "wechat", "dingtalk", "chaoxing", "push", "llm"):
            entry = body["platforms"][name]
            with self.subTest(platform=name):
                headline = entry["headline"]
                self.assertTrue(headline, "每个平台都要有一句人话")
                self.assertLessEqual(len(headline), 40)
                for word in self.MACHINE_WORDS:
                    self.assertNotIn(word, headline, "不许用机器词当文案")
                self.assertIsInstance(entry["action"], dict)
                action = entry["action"]
                self.assertIn(action["kind"], ("panel", "api", "manual"))
                self.assertTrue(action["label"])
                if action["kind"] == "api":
                    self.assertIn(action["target"], api_paths, "api 动作必须是真实存在的路由")
                if action["kind"] == "manual":
                    self.assertTrue(action["hint"], "manual 必须写清点哪里")
                if action["kind"] == "panel":
                    self.assertTrue(action["target"], "panel 必须给出落点")

    def _qq_entry(self, stage: str, engine_state: str, login_state: str) -> dict:
        napcat = {
            "ok": False, "online": login_state == "online", "napcat_installed": engine_state != "not_installed",
            "napcat_running": engine_state == "running", "stage": stage, "error": "x",
        }
        with mock.patch("noticehub.webapp._Handler._napcat_status_payload", return_value=napcat):
            request = urllib.request.Request(self.base + "/api/setup/status", headers={"X-Token": "secret"})
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))["platforms"]["qq"]

    def test_qq_running_no_api_points_at_the_connect_api(self) -> None:
        entry = self._qq_entry("running_no_api", "installed_not_running", "not_logged_in")
        self.assertEqual(entry["action"]["kind"], "api")
        self.assertEqual(entry["action"]["target"], "/api/napcat/connect")
        self.assertIn("连不上", entry["headline"])

    def test_qq_each_engine_state_has_its_own_action(self) -> None:
        cases = (
            ("not_installed", "not_installed", "not_logged_in"),
            ("stopped", "installed_not_running", "not_logged_in"),
            ("waiting_qr", "running", "waiting_scan"),
            ("online", "running", "online"),
        )
        for stage, engine_state, login_state in cases:
            entry = self._qq_entry(stage, engine_state, login_state)
            with self.subTest(engine_state=engine_state, login_state=login_state):
                if login_state == "online":
                    self.assertIsNone(entry["action"], "已登录的 QQ 不该再催用户做事")
                else:
                    self.assertEqual(entry["action"]["kind"], "panel")

    def test_configured_platform_has_no_action(self) -> None:
        with mock.patch.object(self.settings, "wechat_enabled", True), mock.patch.object(
            self.settings, "wechat_group_whitelist", ("g1",)
        ), mock.patch.object(self.settings, "push_channels", return_value=["webhook"]), mock.patch.object(
            type(self.settings), "llm_active", new_callable=mock.PropertyMock, return_value=True
        ):
            body = self._payload()
        for name in ("wechat", "push", "llm"):
            with self.subTest(platform=name):
                self.assertIsNone(body["platforms"][name]["action"])

    def test_building_advice_runs_no_subprocess_and_is_fast(self) -> None:
        """这个接口在设置页首屏被拉：只做静态映射，不许新增子进程/网络探测。"""
        import time as _time

        with mock.patch.object(webapp.subprocess, "run") as run, mock.patch.object(
            webapp.subprocess, "Popen"
        ) as popen, mock.patch.object(webapp.subprocess, "check_output") as check_output:
            started = _time.monotonic()
            self._payload()
            elapsed = _time.monotonic() - started
        self.assertEqual(run.call_count, 0)
        self.assertEqual(popen.call_count, 0)
        self.assertEqual(check_output.call_count, 0)
        self.assertLess(elapsed, 3.0, "首屏接口不该超过 3 秒，实测 %.2fs" % elapsed)
class NapcatHonestWiringTests(unittest.TestCase):
    """NapCat 链路：假成功、缓存失效不对称、异常被吞、两处答案不一致、最坏 30 秒。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.settings = Settings(
            group_whitelist=("g1",),
            web_host="127.0.0.1",
            web_token="secret",
            web_port=0,
            data_dir=root / "data",
            env_file=root / ".env",
            napcat_qr_path=root / "qr.png",
        )
        self.handler = object.__new__(webapp._Handler)
        self.handler.server = mock.Mock(settings=self.settings)
        self.handler.headers = {"Content-Length": "0"}
        self.replies: list = []
        self.handler._json = lambda status, payload: self.replies.append((status, payload))

    # ---- ① 「帮我连上它」写不进 .env：ok 仍是 true，但必须带一句用户看得懂的话
    def _connect(self, written: dict) -> dict:
        self.replies.clear()
        with mock.patch.object(
            webapp.napcat_admin,
            "discover_api_url",
            return_value={"ok": True, "api_url": "http://127.0.0.1:3001", "tried": ["http://127.0.0.1:3001"]},
        ), mock.patch.object(webapp, "update_env_file", return_value=written), mock.patch.object(
            webapp, "_clear_napcat_live_cache"
        ), mock.patch.object(
            webapp._Handler, "_napcat_status_payload", return_value={"ok": True}
        ), mock.patch.dict(os.environ, {}, clear=False):
            self.handler._route_post_api_napcat_connect("")
        self.assertEqual(len(self.replies), 1, self.replies)
        return self.replies[0][1]

    def test_connect_without_env_file_is_a_partial_success_with_a_warning(self) -> None:
        body = self._connect({"ok": False, "error": ".env not found"})
        self.assertTrue(body["ok"], "这一次确实连上了，不能报失败")
        self.assertFalse(body["persisted"])
        self.assertTrue(str(body["warning"]), "没写进 .env 必须告诉用户，否则重启后以为程序坏了")
        self.assertIn("重启", str(body["warning"]))
        self.assertIn(".env not found", str(body["warning"]))

    def test_connect_that_persists_carries_no_warning(self) -> None:
        body = self._connect({"ok": True})
        self.assertTrue(body["ok"])
        self.assertTrue(body["persisted"])
        self.assertEqual(body["warning"], "")

    # ---- ② 缓存失效必须和 napcat_admin 的七键口径一致
    def test_clearing_live_cache_also_clears_napcat_admin_probe_cache(self) -> None:
        with mock.patch.object(webapp.napcat_admin, "clear_probe_cache") as clear:
            webapp._clear_napcat_live_cache()
        clear.assert_called_once()

    # ---- ③ 二维码读不出来，不能和「还没生成」混成一个 404
    def _qrcode(self, *, side_effect=None, ret=None):
        admin = mock.Mock()
        admin.qrcode_bytes.return_value = ret
        if side_effect is not None:
            admin.qrcode_bytes.side_effect = side_effect
        self.replies.clear()
        with mock.patch("noticehub.webapp.napcat_admin", admin):
            self.handler._route_get_api_napcat_qrcode("")
        self.assertEqual(len(self.replies), 1, self.replies)
        return self.replies[0]

    def test_qrcode_read_error_is_not_reported_as_missing(self) -> None:
        status, body = self._qrcode(side_effect=RuntimeError("文件被占用"))
        self.assertEqual(status, 404)
        self.assertTrue(str(body["error"]).startswith("二维码读取失败"), body)
        self.assertIn("文件被占用", str(body["error"]))

    def test_qrcode_missing_file_still_says_not_found(self) -> None:
        status, body = self._qrcode(ret=None)
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "QR code not found")

    # ---- ④ 总览与 /api/napcat/status 对「手动起的 NapCat」必须同一个答案
    def test_overview_does_not_downgrade_manual_napcat_to_unknown(self) -> None:
        with mock.patch.object(
            webapp._Handler, "_napcat_status_payload", return_value={"ok": False, "online": False, "stage": "running_no_api"}
        ), mock.patch.object(
            webapp._Handler, "_meta_info", return_value={"groups": []}
        ), mock.patch.object(
            webapp._Handler, "_wechat_status_payload", return_value={"enabled": False}
        ), mock.patch.object(
            webapp._Handler, "_dingtalk_status_payload", return_value={"enabled": False}
        ), mock.patch.object(
            webapp._Handler, "_chaoxing_status_payload", return_value={"has_cookie": False}
        ), mock.patch.object(webapp, "tunnel") as tunnel:
            tunnel.tailscale_executable.return_value = None
            body = self.handler._overview_payload()
        napcat = body["sources"]["napcat"]
        self.assertEqual(napcat["state"], "needs_setup")
        self.assertEqual(napcat["detail"], "检测到 NapCat 在跑，但接口没通")
        self.assertEqual(napcat["action"], "点「帮我连上它」")
        self.assertNotIn("引擎状态未知", napcat["detail"], "同一台机器不能两个答案")

    # ---- ⑤ 最坏 30 秒：第一次就没通，不许再花一次超时去问登录信息
    def test_status_does_not_pay_the_timeout_twice(self) -> None:
        calls = []

        def boom(settings, action, timeout=None):
            calls.append((action, timeout))
            raise ConnectionError("NapCat 没在运行")

        with mock.patch.object(webapp, "_request_napcat_action", side_effect=boom), mock.patch.object(
            webapp, "_napcat_port_accepts", return_value=False
        ), mock.patch.object(webapp.napcat_admin, "qr_stamp", return_value=""), mock.patch.object(
            webapp.napcat_admin, "qr_age_seconds", return_value=None
        ), mock.patch.object(webapp.napcat_admin, "qr_stale", return_value=False), mock.patch.object(
            webapp.napcat_admin, "detect_boot", return_value=None
        ), mock.patch.object(webapp.napcat_admin, "running_pids", return_value=[]), mock.patch.object(
            webapp.napcat_admin, "probe_failure_hint", return_value="连不上"
        ):
            payload = self.handler._napcat_status_payload()

        self.assertEqual([one[0] for one in calls], ["get_status"], "第一次失败就别再发第二次")
        self.assertFalse(payload["ok"])
class NapcatQrFailureNoticeTest(unittest.TestCase):
    """取不到二维码时，页面要分清「还没出码」和「读不出来」（node 跑真实 loadQrImage）。"""

    HARNESS = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[2], 'utf-8');

function extractFunction(src, name) {
  const marker = 'function ' + name + '(';
  const start = src.indexOf(marker);
  if (start < 0) throw new Error('找不到函数：' + name);
  const end = src.indexOf('\n}', start);
  if (end < 0) throw new Error('函数未以行首 } 结束：' + name);
  return src.slice(start, end + 2);
}

const stateMatch = source.match(/var qrErrorProbe = \{[^}]*\};/);
if (!stateMatch) throw new Error('没找到 qrErrorProbe 状态声明，失败退避会失效');
const textMatch = source.match(/var QR_NOT_READY_TEXT = '[^']*';/);
if (!textMatch) throw new Error('没找到 QR_NOT_READY_TEXT 文案常量');
const body = stateMatch[0] + '\n' + textMatch[0] + '\n' + extractFunction(source, 'qrFailureText') + '\n' + extractFunction(source, 'loadQrImage');
const report = {};

async function scenario(name, body404, failFetch) {
  const note = { hidden: true, textContent: '' };
  const image = { hidden: false, src: '', onload: null, onerror: null };
  const document = {
    getElementById(id) {
      if (id === 'qr') return image;
      if (id === 'qr-note') return note;
      if (id === 'qq-qr-details') return { open: false };
      return null;
    },
  };
  let fetches = 0;
  const fetchStub = function () {
    fetches += 1;
    if (failFetch) return Promise.reject(new Error('offline'));
    return Promise.resolve({ json: function () { return Promise.resolve(body404); } });
  };
  const names = ['document', 'fetch', 'setupToken', 'qrStaleActive', 'Date', 'Math', 'Promise', 'encodeURIComponent'];
  const values = [document, fetchStub, 'secret', false, Date, Math, Promise, encodeURIComponent];
  const factory = new Function(names.join(','), body + '\nreturn { loadQrImage: loadQrImage };');
  const run = factory.apply(null, values);
  run.loadQrImage();
  if (typeof image.onerror !== 'function') throw new Error('loadQrImage 没挂 onerror');
  image.onerror();
  for (let i = 0; i < 60; i += 1) await Promise.resolve();
  report[name] = { hidden: note.hidden, text: note.textContent, fetches: fetches, src: image.src };
}

(async () => {
  await scenario('read_error', { ok: false, error: '二维码读取失败：PermissionError: 拒绝访问' }, false);
  await scenario('plain_missing', { ok: false, error: 'QR code not found' }, false);
  await scenario('probe_offline', { ok: false, error: 'QR code not found' }, true);
  process.stdout.write(JSON.stringify(report));
})();
"""

    report: dict = {}

    @classmethod
    def setUpClass(cls) -> None:
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("本机没有 node，跳过二维码失败提示测试")
        guide = Path(__file__).resolve().parents[1] / "noticehub" / "web_static" / "js" / "guide.js"
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "qr-harness.js"
            script.write_text(cls.HARNESS, encoding="utf-8")
            result = subprocess.run([node, str(script), str(guide)], capture_output=True, timeout=60)
        if result.returncode != 0:
            raise AssertionError(
                "harness 崩溃，退出码 %d\nstderr:\n%s"
                % (result.returncode, result.stderr.decode("utf-8", "replace"))
            )
        cls.report = json.loads(result.stdout.decode("utf-8"))

    def _row(self, name: str) -> dict:
        row = self.report.get(name)
        self.assertIsNotNone(row, "没有拿到场景 %s" % name)
        return row

    def test_qrcode_read_error_is_shown_verbatim(self) -> None:
        row = self._row("read_error")
        self.assertFalse(row["hidden"])
        self.assertTrue(row["text"].startswith("二维码读取失败"), row)
        self.assertIn("PermissionError", row["text"], "具体原因要原样给到用户，别吞成一句「暂时取不到」")

    def test_plain_missing_qrcode_keeps_the_old_wording(self) -> None:
        row = self._row("plain_missing")
        self.assertEqual(row["text"], "二维码暂时取不到。点上面的「重启引擎」再来一次，或等它自动换新。")

    def test_failed_probe_probes_once_and_does_not_retry_in_a_loop(self) -> None:
        row = self._row("probe_offline")
        self.assertEqual(row["fetches"], 1, "onerror 里只探一次，不许在这里重试")
        self.assertEqual(row["text"], "二维码暂时取不到。点上面的「重启引擎」再来一次，或等它自动换新。")

    def test_qrcode_image_request_carries_the_token(self) -> None:
        row = self._row("plain_missing")
        self.assertIn("/api/napcat/qrcode?token=secret", row["src"])


class _BoundedFakeResponse:
    """假的网络响应：只有 read()/read1()，读空返回 b""（与真流一致），不碰真实 socket。"""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.reads = 0

    def read(self, want: int = -1) -> bytes:
        self.reads += 1
        return self._chunks.pop(0) if self._chunks else b""

    def read1(self, want: int = -1) -> bytes:
        return self.read(want)

    def __enter__(self) -> "_BoundedFakeResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


class NapcatBoundedReadTests(unittest.TestCase):
    """NapCat 响应体必须有界读：超限/滴流超时当失败返回，不许把半截数据当完整 JSON 解析。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.settings = Settings(
            group_whitelist=("g1",),
            web_host="127.0.0.1",
            web_token="secret",
            web_port=0,
            data_dir=root / "data",
            env_file=root / ".env",
            napcat_qr_path=root / "qr.png",
            napcat_api_url="http://127.0.0.1:3001",
            napcat_api_token="secret",
            http_timeout=15,
        )

    def test_oversized_napcat_response_is_a_failure_not_a_json_decode_error(self) -> None:
        oversized = _BoundedFakeResponse([b"x" * (webapp.RESPONSE_BODY_LIMIT + 1)])
        with mock.patch.object(webapp, "_napcat_port_accepts", return_value=True), mock.patch(
            "noticehub.webapp.urllib.request.urlopen", return_value=oversized
        ):
            with self.assertRaises(RuntimeError) as caught:
                webapp._request_napcat_action(self.settings, "get_status")
        message = str(caught.exception)
        self.assertIn("太慢", message, "要给可执行的提示，不是一串解析异常")
        self.assertIn("重启引擎", message)
        self.assertNotIn("JSONDecodeError", message)
        self.assertEqual(oversized.reads, 1, "到上限就停，不许继续读到内存爆掉")

        # 同一套常量下正常小响应仍要照常解析，别把好请求也拦了。
        good = _BoundedFakeResponse([json.dumps({"status": "ok", "retcode": 0, "data": {"x": 1}}).encode("utf-8")])
        with mock.patch.object(webapp, "_napcat_port_accepts", return_value=True), mock.patch(
            "noticehub.webapp.urllib.request.urlopen", return_value=good
        ):
            self.assertEqual(webapp._request_napcat_action(self.settings, "get_status"), {"x": 1})

    def test_dripping_napcat_response_fails_fast_instead_of_hanging(self) -> None:
        handler = object.__new__(webapp._Handler)
        handler.server = mock.Mock(settings=self.settings)
        response = _BoundedFakeResponse([b'{"status":', b'"ok"}'])
        ticks = iter([0.0, 1.0, 2.0, 999.0])

        def fake_monotonic() -> float:
            return next(ticks, 999.0)

        started = time.perf_counter()
        with mock.patch.object(webapp, "_napcat_port_accepts", return_value=True), mock.patch(
            "noticehub.webapp.urllib.request.urlopen", return_value=response
        ), mock.patch.object(webapp.time, "monotonic", side_effect=fake_monotonic):
            result = handler._napcat("get_status")
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 5.0, "滴流响应不许把请求线程拖到天荒地老")
        self.assertFalse(result["ok"], "超时要当失败返回，不能报成功")
        self.assertIn("太慢", result["error"])
        self.assertIn("重启引擎", result["error"], "失败文案要给出下一步点哪里")
        self.assertNotIn("JSONDecodeError", result["error"])
        self.assertEqual(response.reads, 2, "deadline 到了就不该再读第三块")


class PageTemplateReloadTest(unittest.TestCase):
    """page.html 什么时候跟着磁盘走：开发模式跟，生产只读一次。"""

    def _run_with_env(self, dev_mode, page, cache, mutate):
        """跑一次「先读、改文件、再读」，返回两次的内容。"""
        with mock.patch.object(webapp, "_PAGE_HTML_PATH", page), \
                mock.patch.object(webapp, "_PAGE_CACHE", cache), \
                mock.patch.dict(os.environ, {"NOTICEHUB_DEV_RELOAD": "1"} if dev_mode else {}):
            if not dev_mode:
                os.environ.pop("NOTICEHUB_DEV_RELOAD", None)
            before = webapp.page_html()
            mutate()
            return before, webapp.page_html()

    def test_dev_mode_rereads_after_the_file_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / "page.html"
            page.write_text("<html>v1</html>", encoding="utf-8")
            cache = {"mtime": page.stat().st_mtime_ns - 10, "text": "<html>v1</html>"}

            def mutate():
                page.write_text("<html>v2</html>", encoding="utf-8")
                stamp = page.stat().st_mtime_ns
                os.utime(page, ns=(stamp, stamp + 5_000_000))

            before, after = self._run_with_env(True, page, cache, mutate)
            self.assertIn("v1", before)
            self.assertIn("v2", after, "开着 NOTICEHUB_DEV_RELOAD 时改完页面应该立刻生效")

    def test_without_dev_mode_the_template_stays_as_imported(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / "page.html"
            page.write_text("<html>v1</html>", encoding="utf-8")
            cache = {"mtime": page.stat().st_mtime_ns, "text": "<html>v1</html>"}

            def mutate():
                page.write_text("<html>v2</html>", encoding="utf-8")
                stamp = page.stat().st_mtime_ns
                os.utime(page, ns=(stamp, stamp + 5_000_000))

            before, after = self._run_with_env(False, page, cache, mutate)
            self.assertIn("v1", before)
            self.assertIn("v1", after, "没开开发模式时不重读：生产上每个请求读一遍页面没意义")

