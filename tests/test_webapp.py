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
from noticehub.version import APP_VERSION  # noqa: E402


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
        self.assertIn('font-size:var(--fs-3)', lead_css)
        self.assertIn('font-size:var(--fs-2)', note_css)
        fs3 = re.search(r"--fs-3:[^;]*?(\d+)px", PAGE_HTML).group(1)
        fs2 = re.search(r"--fs-2:[^;]*?(\d+)px", PAGE_HTML).group(1)
        self.assertGreater(int(fs3), int(fs2), "主句字号必须大于说明行")
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
        # 尺寸对齐原型：控件 28px、分组行 34px，44px 只留给日历格这种真正的大点击面；
        # 分组折叠行 / 登录方式不再硬撑 44px（min-height:0），改由字号和间距兜住。
        self.assertRegex(css, r"\.btn,[^{]*\{[^}]*min-height:28px")
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
        # 窄屏放大档：服务自检收成一列，别把「标签 | 值」压成竖排；默认档仍是两列。
        # 值放不下时整行折行，别把四字标签拆开。
        self.assertIn(
            "html:not([data-font-scale=default]) #tab-settings .health-list{grid-template-columns:minmax(0,1fr)}",
            _css_block(css, "@media(max-width:760px){"),
        )
        self.assertIn(
            "html:not([data-font-scale=default]) #tab-settings .health-row{flex-wrap:wrap}",
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
            ("html[data-theme=dark] .action-feedback[data-state=error]", r"color:var\(--bad\)"),
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
            r"\.task:not\(\.done\):not\(\[aria-busy=true\]\):hover,\s*"
            r"\.task:not\(\.done\):not\(\[aria-busy=true\]\)\.is-focused,\s*"
            r"\.task:not\(\.done\):not\(\[aria-busy=true\]\):focus-within\{([^}]*)\}",
            css,
        )
        self.assertIsNotNone(task_states)
        # 悬停/聚焦只做 2px 横移 + 换边框色，不改尺寸：命中区不随状态伸缩。
        self.assertIn("transform:translateX(2px)", task_states.group(1))
        self.assertIn("border-color:var(--sand)", task_states.group(1))
        self.assertNotIn("scale(", task_states.group(1))
        self.assertIn("box-shadow:none", task_states.group(1))
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
        self.assertRegex(renderer, r"var blocks = \[todayBlock,\s*calendarPanel,\s*overdueBlock")
        self.assertIn("section('已过期', overdue, {className: 'overdue', collapsed: true})", renderer)
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
        self.assertRegex(helper, re.compile(r"classList\.add\('completion-confirmed'\).*setTimeout\(resolve, 240\).*classList\.add\('completing'\).*setTimeout\(resolve, 200\)", re.S))

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

    def test_upcoming_carousel_and_motion_preference_are_local_and_interruptible(self) -> None:
        start = PAGE_SOURCE.index("var upcomingGroups =")
        end = PAGE_SOURCE.index("function animateConfirmedTaskCompletion", start)
        carousel = PAGE_SOURCE[start:end]
        self.assertIn("(data.today || []).concat(data.week || [], data.later || [])", carousel)
        self.assertNotIn("dueTodayCount", carousel)
        self.assertIn("? '今天 · '", carousel)
        self.assertIn("}, 3000);", carousel)
        self.assertIn("addEventListener('mouseenter'", carousel)
        self.assertIn("upcomingFocused = upcomingRoot.contains(document.activeElement)", carousel)
        self.assertIn("button.addEventListener('focus'", carousel)
        self.assertIn("button.addEventListener('blur'", carousel)
        self.assertIn("document.addEventListener('visibilitychange'", carousel)
        settings_start = PAGE_SOURCE.index("function setMotionPreference")
        settings_end = PAGE_SOURCE.index("reducedMotionQuery.addEventListener", settings_start)
        setting = PAGE_SOURCE[settings_start:settings_end]
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
        # 轮播是一条两列网格（内容 / 翻页控件），高度跟着内容走，切换不再被定高轨道夹住。
        carousel_rule = re.search(r"\.upcoming-carousel\{[^}]*\}", css)
        self.assertIsNotNone(carousel_rule)
        self.assertIn("display:grid;grid-template-columns:minmax(0,1fr) auto", carousel_rule.group(0))
        self.assertNotIn("border-top", carousel_rule.group(0), "分割线交给独立的 .ov-rule，别挂在轮播容器上")
        self.assertIn(".ov-rule{height:1px;background:var(--line);margin:var(--space-4) 0}", css)
        self.assertIn("display:grid;grid-template-columns:minmax(0,1fr) auto", carousel_rule.group(0))
        self.assertIn("align-items:start", carousel_rule.group(0))
        self.assertNotIn("height:200px", css)
        # 换页的 is-switching 类必须有对应样式：淡出 → 换内容 → 淡入，时长与 JS 的 220ms 对齐。
        self.assertIn("#upcoming-carousel.is-switching #upcoming-date", css)
        self.assertIn("#upcoming-date,#upcoming-tasks{transition:opacity 220ms", css)
        self.assertIn("root.classList.add('is-switching')", carousel)
        self.assertIn("renderUpcomingSlide(true)", carousel)
        self.assertIn("html[data-motion=paused]", css)

    def test_upcoming_height_and_motion_fallback_contract(self) -> None:
        css = self._current_stylesheet()
        # 轮播不再写死高度：CSS 里还是「内容 + 翻页控件」的两列网格，高度跟内容走；
        # 定高改由 JS 锁 min-height —— 把每一页都量一遍取最高那页，换页时下方内容才不被顶动。
        carousel_rule = re.search(r"\.upcoming-carousel\{[^}]*\}", css)
        self.assertIsNotNone(carousel_rule, "轮播必须是独立的一条规则")
        self.assertIn("display:grid;grid-template-columns:minmax(0,1fr) auto", carousel_rule.group(0))
        self.assertNotIn("border-top", carousel_rule.group(0), "分割线交给独立的 .ov-rule")
        self.assertIn("align-items:start", carousel_rule.group(0))
        self.assertIn(".upcoming-carousel[hidden]{display:none}", css)
        self.assertNotIn("height:200px", css)
        self.assertNotIn("-webkit-line-clamp", css)
        narrow = _css_block(css, "@media(max-width:760px){")
        self.assertIn(".upcoming-carousel{grid-template-columns:minmax(0,1fr)}", narrow)
        reduced = _css_block(css, "@media (prefers-reduced-motion:reduce){")
        self.assertIn("animation:none!important", reduced)
        self.assertIn("transition:none!important", reduced)
        self.assertIn("html[data-motion=paused] *,html[data-motion=paused] *::before", css)
        start = PAGE_SOURCE.index("function renderUpcomingSlide(animate)")
        end = PAGE_SOURCE.index("function renderUpcoming(data", start)
        slide = PAGE_SOURCE[start:end]
        self.assertIn("root.classList.remove('is-switching')", slide)
        self.assertIn("root.classList.add('is-switching')", slide)
        # 换内容必须延后到定时器里（先淡出 → 换 → 再淡入）；若在同一帧换完并删类，浏览器只绘制最终态，过渡永远不可见
        self.assertIn("upcomingSwitchTimer = window.setTimeout(", slide)
        self.assertIn("}, UPCOMING_SWITCH_MS);", slide)
        self.assertIn("if (!animate || motionIsPaused())", slide)
        self.assertNotIn("window.requestAnimationFrame", slide)
        # 空态是唯一的例外：0 件时收成内容高度（否则 200px 的白框看着像没加载出来）。
        # 有数据时仍然必须走上面那两条定高断言，不能被这个例外带跑。
        # 空态/暂停不再靠 CSS 增高兜底：CSS 里没有按状态写死的增高规则，定高全在 JS 里算。
        self.assertNotIn(".upcoming-carousel.is-empty", css)
        self.assertIn("root.classList.toggle('is-empty', !group);", slide)
        # 换内容延后到 UPCOMING_SWITCH_MS；关动效时同帧直接落内容，不空等一个已不存在的过渡。
        self.assertIn("var UPCOMING_SWITCH_MS = 220;", PAGE_SOURCE)
        self.assertIn("if (!animate || motionIsPaused()) { paint(); refreshUpcomingRotation(); return; }", slide)
        # 换页不再顶动下方内容靠的是 JS 锁 min-height：每页都量一遍取最高那页。
        # 注意这条必须在 renderUpcomingSlide 之外，否则上面那条 assertNotIn(rAF) 会被 resize 节流误伤。
        start = PAGE_SOURCE.index("function syncUpcomingMinHeight()")
        end = PAGE_SOURCE.index("function moveUpcoming(delta)", start)
        sync = PAGE_SOURCE[start:end]
        self.assertIn("list.getBoundingClientRect().height", sync, "每一页都要真量高度，不能按行数估")
        self.assertIn("tallest = Math.max(tallest, list.getBoundingClientRect().height);", sync)
        self.assertIn("list.style.minHeight = tallest > 0 ? Math.ceil(tallest) + 'px' : '';", sync)
        self.assertIn("if (!pages.length) {\n    list.style.minHeight = '';\n    if (dateLine) dateLine.style.minHeight = '';\n    return;\n  }",
                      sync, "空态必须把列表与日期行两处锁高都清空")
        self.assertNotIn("setTimeout", sync, "量高度必须同步跑完，中间不能交给定时器")
        self.assertIn("renderUpcomingSlide(animate === true);\n  syncUpcomingMinHeight();", PAGE_SOURCE,
                      "数据刷新与模式切换都要重算锁高")
        # 窄屏 + 大字号下日期行会折两行，只锁列表不够：日期行也要按各组真实文案量出最大值。
        self.assertIn("if (dateLine) dateLine.textContent = upcomingDateText(group);", sync,
                      "量日期高度前必须真写入该组文案，不能量到上一组留下的")
        self.assertIn("tallestDate = Math.max(tallestDate, dateLine.getBoundingClientRect().height);", sync)
        self.assertIn("if (dateLine) dateLine.style.minHeight = tallestDate > 0 ? Math.ceil(tallestDate) + 'px' : '';",
                      sync)
        # 日期文案只有一处实现：paint 与量高都走它，两边写的字必须一致。
        self.assertIn("function upcomingDateText(group)", PAGE_SOURCE)
        self.assertIn("document.getElementById('upcoming-date').textContent = upcomingDateText(group);", slide)
        self.assertIn("document.getElementById('upcoming-date').textContent = upcomingDateText(null);", slide)
        # 清空再重建节点会把横滑位置打到 0，量完要写回。
        self.assertIn("var savedScroll = list.scrollLeft;", sync)
        self.assertIn("list.scrollLeft = Math.min(savedScroll, Math.max(0, list.scrollWidth - list.clientWidth));", sync)
        # 锁高撑容器不撑卡片：卡片必须贴顶，否则短页的卡片被 stretch 出一截空框。
        self.assertIn("align-self:flex-start", re.search(r"\.upcoming-tasks li\{[^}]*\}", css).group(0))
        font_start = PAGE_SOURCE.index("function setFontScale(scale)")
        font = PAGE_SOURCE[font_start:PAGE_SOURCE.index("function initFontScalePreference", font_start)]
        self.assertIn("syncUpcomingMinHeight();", font, "换字号后行数变了，锁高要重算")
        self.assertIn("window.addEventListener('resize', function () {\n  if (upcomingResizeFrame) return;", PAGE_SOURCE,
                      "改窗口大小后要重算，用一次性 rAF 节流")
        rot_start = PAGE_SOURCE.index("function refreshUpcomingRotation()")
        # 切片止于日期文案函数之前的注释：日期文案函数自己的注释会提到 syncUpcomingMinHeight。
        rotation = PAGE_SOURCE[rot_start:PAGE_SOURCE.index("/* 日期行文案", rot_start)]
        self.assertNotIn("syncUpcomingMinHeight", rotation, "轮换定时器里不许每 3 秒量一次高度")
        # 定时器真正调的是 renderUpcomingSlide：锁高塞进 paint 一样会变成每 3 秒量一次，
        # 而且锁出来的高度还会跟着页变。上面那条切片盖不到它，这里单独锁。
        paint_start = PAGE_SOURCE.index("function renderUpcomingSlide(animate)")
        paint_body = PAGE_SOURCE[paint_start:PAGE_SOURCE.index(
            "function renderUpcoming(data, overdueCount)", paint_start)]
        self.assertNotIn("syncUpcomingMinHeight", paint_body, "换页 paint 里不许量高度")
        # 计数文案（auto 列）宽度会挤窄主列、把日期挤成两行：量高前也得写入该组计数。
        self.assertIn("function upcomingCountText(group, index)", PAGE_SOURCE)
        self.assertIn("if (countLine) countLine.textContent = upcomingCountText(group, index);", sync,
                      "量日期高度前必须写入该组的计数文案，否则量到的主列宽度不是最终态")
        self.assertLess(sync.index("countLine.textContent = upcomingCountText(group, index)"),
                        sync.index("dateLine.textContent = upcomingDateText(group)"),
                        "先写计数再写日期再量，顺序反了就白量")
        self.assertIn("document.getElementById('upcoming-count').textContent = upcomingCountText(group, upcomingIndex);",
                      slide)
        self.assertIn("document.getElementById('upcoming-count').textContent = upcomingCountText(null);", slide)

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
        self.assertRegex(card.group(0), r"animation:summary-enter 420ms var\(--ease-out\) both")
        self.assertNotIn("infinite", card.group(0))
        enter = re.search(r"@keyframes summary-enter\{(.*?)\}\n", css, re.S)
        self.assertIsNotNone(enter)
        self.assertIn("to{opacity:1;transform:none}", enter.group(1))
        # 入场只动 opacity/transform：绘制属性会让浏览器每帧重绘整卡文字。
        self.assertNotIn("box-shadow", enter.group(1))
        self.assertNotIn("filter", enter.group(1))

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
        overview_end = PAGE_HTML.index('</section>', PAGE_HTML.index('<div class="ov-foot"', overview))
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


    def test_overview_can_switch_between_due_and_ongoing(self) -> None:
        css = self._current_stylesheet()
        # 概览顶部必须有两个可点的模式按钮（旧实现是写死的 <span class="eyebrow">最近到期</span>）
        self.assertNotIn('<span class="eyebrow">最近到期</span>', PAGE_SOURCE)
        self.assertIn('<div class="upcoming-mode" role="group" aria-label="今日概览显示内容">', PAGE_SOURCE)
        self.assertIn('id="upcoming-mode-due"', PAGE_SOURCE)
        self.assertIn('id="upcoming-mode-now"', PAGE_SOURCE)
        self.assertIn('>最近到期</button>', PAGE_SOURCE)
        self.assertIn('>正在进行</button>', PAGE_SOURCE)
        self.assertIn('aria-label="今日概览：最近到期与正在进行"', PAGE_SOURCE)
        # 选择要记住（刷新后仍然是你选的那个）
        self.assertIn("var UPCOMING_MODE_KEY = 'qq_digest_overview_mode';", PAGE_SOURCE)
        self.assertIn("try { window.localStorage.setItem(UPCOMING_MODE_KEY, upcomingMode); } catch (error) {}", PAGE_SOURCE)
        self.assertIn("button.addEventListener('click', function () { setUpcomingMode(mode); });", PAGE_SOURCE)
        # 「正在进行」= 事务区间：从收到通知起持续到截止时间，跨天算；当天写明「几点到几点」的按那个时段算
        self.assertIn("function parseTimeWindow(task) {", PAGE_SOURCE)
        self.assertIn(r"var pattern = /(\d{1,2})\s*[:：]\s*(\d{2})/g;", PAGE_SOURCE)
        self.assertIn("if (!/[-–—~～]|至/.test(between)) return null;", PAGE_SOURCE)
        # 同一时刻重复出现（23:59 … 23:59）不算窗口，否则最后一分钟会误报「正在进行」
        self.assertIn("if (end === start) return null;", PAGE_SOURCE)
        self.assertIn("function parseStamp(value) {", PAGE_SOURCE)
        self.assertIn("function ongoingSpan(task, now) {", PAGE_SOURCE)
        self.assertIn("function collectUpcomingNow(data) {", PAGE_SOURCE)
        # 截止时间常常只是开始时刻，所以当天时段要和 deadline 取并集（如 13:30 截止 + 13:30-14:55 上课）
        self.assertIn("if (!start || bounds.start < start) start = bounds.start;", PAGE_SOURCE)
        self.assertIn("if (!end || bounds.end > end) end = bounds.end;", PAGE_SOURCE)
        self.assertIn("if (!end) return null;", PAGE_SOURCE)
        self.assertIn("if (now < start || now > end) return null;", PAGE_SOURCE)
        self.assertIn("var span = ongoingSpan(task, now);", PAGE_SOURCE)
        self.assertIn("upcomingNow = collectUpcomingNow(data);", PAGE_SOURCE)
        # 两种模式用同一套渲染：'now' 分支按「还剩多久截止 / 几点结束」展示，空的时候也要留在原地（否则按钮会跟着消失）
        self.assertIn("if (group.mode === 'now') {", PAGE_SOURCE)
        self.assertIn("if (group.mode === 'now') return '正在进行的事项';", PAGE_SOURCE)
        self.assertIn("document.getElementById('upcoming-date').textContent = upcomingDateText(group);", PAGE_SOURCE)
        self.assertIn("list.appendChild(el('li', 'upcoming-more', '现在没有正在进行的日程'));", PAGE_SOURCE)
        # 「正在进行」也走同一张卡：标题 + 时间（原型 .up 的 .t / .d）
        self.assertIn("list.appendChild(upcomingCard(entry.task, entry.span.hint));", PAGE_SOURCE)
        self.assertIn("item.appendChild(el('span', 'd', String(hint)));", PAGE_SOURCE)
        self.assertIn("var upcomingDue = [];", PAGE_SOURCE)
        # 正在进行也要按页轮换，并且每页都装满：旧实现只渲染前三条 + 「另有 N 件」（用户报的「没有轮换 + 显示不全」）
        self.assertIn("var UPCOMING_PAGE_SIZE = 3;", PAGE_SOURCE)
        self.assertIn("function nowUpcomingPages() {", PAGE_SOURCE)
        self.assertIn("pages: Math.ceil(upcomingNow.length / UPCOMING_PAGE_SIZE),", PAGE_SOURCE)
        self.assertIn("? nowUpcomingPages()", PAGE_SOURCE)
        self.assertNotIn("group.now.slice(0, 3)", PAGE_SOURCE)
        self.assertNotIn("'另有 ' + (group.now.length - 3)", PAGE_SOURCE)
        self.assertIn("group.page.forEach(function (entry) {", PAGE_SOURCE)
        self.assertIn("+ (group.pages > 1 ? ' · 第 ' + (index + 1) + '/' + group.pages + ' 页' : '');", PAGE_SOURCE)
        # 页码文案只有一份实现：paint 与锁高量宽都走它，两边写的字必须一致。
        self.assertIn("document.getElementById('upcoming-count').textContent = upcomingCountText(group, upcomingIndex);",
                      PAGE_SOURCE)
        self.assertIn("document.getElementById('upcoming-prev').hidden = group.pages < 2;", PAGE_SOURCE)
        self.assertIn("document.getElementById('upcoming-next').hidden = group.pages < 2;", PAGE_SOURCE)
        # 轮询刷新数据时保留当前页，否则轮播每刷一次就被拨回第一页
        self.assertIn("    : upcomingIndex;", PAGE_SOURCE)
        # 样式：选中的那个按钮要有明显状态（浅绿底 + teal 描边），键盘可达
        # 样式：选中的那个按钮要有明显状态（teal 实心 + on-accent 字），键盘可达。
        self.assertIn(".upcoming-mode{display:flex;align-items:center;gap:var(--space-2);margin:0 0 var(--space-3)}", css)
        self.assertIn(".upcoming-mode-btn.is-active{background:var(--teal);border-color:var(--teal);color:var(--on-accent);font-weight:600}", css)
        # 键盘可达：焦点环由全站统一的 :focus-visible 规则给，不再为这两枚按钮单写一条。
        self.assertIn("button:focus-visible,input:focus-visible", css)
        self.assertIn("outline:2px solid var(--teal)!important;outline-offset:2px!important", css)
        # 切换模式时按钮区不能跳动：主列顶对齐，按钮做成 24px 的小胶囊（原型 .mode）。
        carousel_css = re.search(r"\.upcoming-carousel\{[^}]*\}", css).group(0)
        self.assertIn("align-items:start", carousel_css)
        self.assertIn(".upcoming-main{min-width:0;align-self:start}", css)
        self.assertIn(".upcoming-mode-btn{\n  display:inline-flex;align-items:center;min-height:24px;padding:0 var(--space-3);", css)
        self.assertIn(".upcoming-controls{display:flex;align-items:center;align-self:center;gap:var(--space-2);flex:none}", css)
        self.assertIn(".upcoming-controls{display:flex;align-items:center;align-self:center;gap:var(--space-2);flex:none}", css)
        # 切换模式也要走同一套淡出→换内容→淡入，不能硬切
        self.assertIn("renderUpcomingSlide(animate === true)", PAGE_SOURCE)
        self.assertIn("applyUpcomingGroups(true)", PAGE_SOURCE)
        # 一件到期的都没有时卡片留在原地，否则整块连按钮一起消失，用户切不回「正在进行」
        self.assertIn("最近没有到期的事项", PAGE_SOURCE)

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
        self.assertIn("card.style.transition = 'transform 240ms cubic-bezier(.2, .8, .2, 1)';", PAGE_SOURCE)
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

    def test_lab_status_is_always_200_and_reports_unavailable_when_off(self) -> None:
        status, body = self._lab_request("GET", "/api/lab/status")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["result"], {"search": False, "replies": False, "events": False, "rules": False})

    def test_lab_routes_reject_with_403_when_switches_off(self) -> None:
        status, body = self._lab_request("GET", "/api/lab/search?q=x")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "实验功能未开启")
        self.assertEqual(self._lab_request("GET", "/api/lab/replies")[0], 403)
        self.assertEqual(self._lab_request("POST", "/api/lab/replies/ack", payload={"msg_id": "m1"})[0], 403)
        self.assertEqual(self._lab_request("POST", "/api/lab/events", payload={"text": "明天开会"})[0], 403)

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
        self.settings.lab_search = True
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

    def test_lab_replies_routes_forward_through_facade(self) -> None:
        self.settings.lab_replies = True
        listed: list[dict] = []
        acked: list[str] = []

        def list_replies(**filters: object) -> dict:
            listed.append(dict(filters))
            return {"replies": []}

        def ack_reply(msg_id: str) -> dict:
            acked.append(msg_id)
            return {"acked": msg_id}

        self._inject_lab_module("noticehub.replies", list_replies, ack_reply)
        status, body = self._lab_request("GET", "/api/lab/replies?limit=5")
        self.assertEqual(status, 200)
        self.assertEqual(listed, [{"limit": 5}])
        status, body = self._lab_request("POST", "/api/lab/replies/ack", payload={"msg_id": "onebot:g1:1"})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"], {"acked": "onebot:g1:1"})
        self.assertEqual(acked, ["onebot:g1:1"])
        self.assertEqual(self._lab_request("POST", "/api/lab/replies/ack", payload={})[0], 400)
        self.assertEqual(self._lab_request("POST", "/api/lab/replies/ack", raw=b"[1]")[0], 400)
        self.assertEqual(self._lab_request("POST", "/api/lab/replies/ack", raw=b"not json")[0], 400)

    def test_lab_events_route_forwarding_and_503_when_module_missing(self) -> None:
        seen: list[str] = []

        def extract_events(text: str) -> dict:
            seen.append(text)
            return {"candidates": []}

        self._inject_lab_module("noticehub.event_extract", extract_events)
        self.assertEqual(self._lab_request("POST", "/api/lab/events", payload={"text": "明天开会"})[0], 403)
        self.settings.lab_events = True
        status, body = self._lab_request("POST", "/api/lab/events", payload={"text": "明天下午三点半在A楼302开会"})
        self.assertEqual(status, 200)
        self.assertEqual(seen, ["明天下午三点半在A楼302开会"])
        self.assertEqual(self._lab_request("POST", "/api/lab/events", payload={"text": ""})[0], 400)
        self.assertEqual(self._lab_request("POST", "/api/lab/events", payload={"text": "长" * 2001})[0], 400)
        # 开关开着但模块没落地 → 503（模块确实在时，用 patch 把「缺失」造出来）
        from noticehub import lab as lab_module

        self.settings.lab_search = True
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
        self.assertIn("BETA 测试版", body)  # 空配置回落到 APP_CHANNEL=beta
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
        )
        for path, content_type in assets:
            with self.subTest(path=path):
                with urllib.request.urlopen(self.base + path, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn(content_type, response.headers.get_content_type())
                    self.assertTrue(response.read())

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

    def test_lab_settings_round_trip_and_hot_apply(self) -> None:
        """实验室开关从 .env 挪到设置页：勾选即存 QQ_DIGEST_LAB_*，当次生效，回读一致。"""
        env_path = Path(self.tmp.name) / ".env"
        env_path.write_text("KEEP=yes\n", encoding="utf-8")
        self.server.settings.env_file = env_path
        before = self._get("/api/settings", "secret")
        self.assertFalse(before["lab_search"])
        self.assertFalse(before["lab_rules"])

        result = self._post("/api/settings", {"lab_search": True, "lab_events": True})
        self.assertTrue(result["ok"])
        self.assertEqual(result["lab"], {"search": True, "replies": False, "events": True, "rules": False})
        # 热生效：不用重启，当前 Settings 立刻可用
        self.assertTrue(self.settings.lab_enabled("search"))
        self.assertTrue(self.settings.lab_enabled("events"))

        saved = self._get("/api/settings", "secret")
        self.assertTrue(saved["lab_search"])
        self.assertTrue(saved["lab_events"])
        self.assertFalse(saved["lab_replies"])
        self.assertFalse(saved["lab_rules"])

        text = env_path.read_text(encoding="utf-8")
        self.assertIn("NOTICEHUB_LAB_SEARCH=1", text)
        self.assertIn("NOTICEHUB_LAB_EVENTS=1", text)
        self.assertIn("KEEP=yes", text, "只写实验室四项，别的行要原样保留")

        # 关掉：写 0 并回读为 False
        self.assertTrue(self._post("/api/settings", {"lab_search": False})["ok"])
        self.assertFalse(self._get("/api/settings", "secret")["lab_search"])
        self.assertIn("NOTICEHUB_LAB_SEARCH=0", env_path.read_text(encoding="utf-8"))

        # 非布尔值拒绝；和别的设置混着存也要拒绝（避免一次请求写两个区块）
        bad_status, bad_body = self._lab_request("POST", "/api/settings", payload={"lab_rules": "yes"})
        self.assertEqual(bad_status, 400)
        self.assertFalse(bad_body["ok"])
        mixed_status, mixed = self._lab_request("POST", "/api/settings", payload={"lab_rules": True, "catchup_hours": 48})
        self.assertEqual(mixed_status, 400)
        self.assertIn("实验室设置请单独保存", mixed["error"])

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

    def test_settings_page_has_lab_section_with_four_switches(self) -> None:
        for marker in (
            "labBox.id='lab-settings-card'",
            "<summary>实验室（实验性功能）</summary>",
            '<input id="lab-search" type="checkbox">',
            '<input id="lab-replies" type="checkbox">',
            '<input id="lab-events" type="checkbox">',
            '<input id="lab-rules" type="checkbox">',
            "saveEnabledToggle('lab-search','lab-result','lab_search');",
            "api('/api/lab/status')",
        ):
            self.assertIn(marker, PAGE_SOURCE)
        # 默认收起：details 不带 open
        self.assertNotIn('<details id="lab-details" open', PAGE_SOURCE)

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
    // E：真实入口：openPlatformSetupTarget('qq') 切任务 tab 并 settle 到 connect
    const t = buildTarget();
    let tabClicked = false;
    const run = buildModule(t, { vh: 1000, wizard: true, getElementById(id) {
      return id === 'tab-tasks-button' ? { click() { tabClicked = true; } } : t;
    } });
    run.mod.open('qq');
    run.pump(10, [0, 0, 0, 300, 300, 300, 300, 300, 300, 300]);
    checks.push(['入口切了 tab', tabClicked]);
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

const scenarios = [
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

    def test_curl_paste_extracts_cookie_from_three_variants(self) -> None:
        """三种 cURL 变体（单引号 -H / 双引号 -H / --header）抽出同一份 Cookie。"""
        self._assert_scenario("curl_variants_extract_same_cookie")

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
