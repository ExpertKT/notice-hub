from __future__ import annotations

import datetime as dt
import io
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest import webapp  # noqa: E402
from qq_live_digest import updater  # noqa: E402
from qq_live_digest import ics  # noqa: E402
from qq_live_digest import pairing  # noqa: E402
from qq_live_digest.config import Settings  # noqa: E402
from qq_live_digest.qr import matrix  # noqa: E402
from qq_live_digest.store import Store  # noqa: E402
from qq_live_digest.timeutil import iso  # noqa: E402
from qq_live_digest.webapp import PAGE_HTML, TaskWebServer, group_tasks, overview  # noqa: E402

NOW = dt.datetime(2026, 9, 30, 9, 0, 0)


class DashboardMotionTest(unittest.TestCase):
    @staticmethod
    def _current_stylesheet() -> str:
        return PAGE_HTML.rsplit("<style>", 1)[1].split("</style>", 1)[0]

    def test_dashboard_has_bounded_perspective_and_three_depth_levels(self) -> None:
        css = self._current_stylesheet()
        perspectives = [int(value) for value in re.findall(r"perspective:\s*(\d+)px", css)]
        self.assertTrue(perspectives)
        self.assertTrue(all(800 <= value <= 1400 for value in perspectives))
        defaults = css.split("@media(prefers-reduced-motion:reduce)", 1)[0]
        depths = {name: int(value) for name, value in re.findall(r"--depth-([a-z-]+):\s*(\d+)px", defaults)}
        self.assertEqual(len(depths) + 1, 3)
        self.assertTrue(all(0 <= value <= 24 for value in depths.values()))
        self.assertGreaterEqual(max(depths.values()) - min(depths.values()), 6)
        self.assertRegex(css, r"translateZ\(var\(--depth-(?:mid|top)\)\)")
        width_caps = [int(value) for value in re.findall(r"width:min\(100% - \d+px,(\d+)px\)", css)]
        self.assertIn(960, width_caps)
        self.assertRegex(css, r"min-height:\s*44px")
        self.assertRegex(css, r"summary\{[^}]*min-height:44px")
        self.assertRegex(css, r"\.login-choice label\{[^}]*min-height:44px")
        self.assertRegex(css, r"#groups label\{[^}]*min-height:44px")
        self.assertRegex(css, r"\.login-more summary\{min-height:44px")
        self.assertIn("button:focus-visible", css)
        self.assertIn("outline:2px solid #12695b!important;outline-offset:2px!important", css)
        self.assertNotIn("outline:3px solid", PAGE_HTML)
        self.assertNotIn("outline-offset:3px", PAGE_HTML)
        self.assertIn("min-width:0", css)
        self.assertIn("width:calc(100% - 32px)", css)

    def test_background_flow_is_composited_and_pauses_with_motion_settings(self) -> None:
        css = self._current_stylesheet()
        self.assertIn("body:before,body:after{", css)
        self.assertIn("position:fixed", css)
        self.assertIn("inset:-30%", css)
        self.assertIn("z-index:-1", css)
        self.assertIn("pointer-events:none", css)
        self.assertIn("will-change:transform", css)
        self.assertIn("animation:bg-silk-a 52s", css)
        self.assertIn("animation:bg-silk-b 68s", css)
        for name in ("bg-silk-a", "bg-silk-b"):
            keyframes = css.split("@keyframes " + name + "{", 1)[1].split("}}", 1)[0]
            self.assertIn("transform:translate3d(", keyframes)
            self.assertNotIn("box-shadow", keyframes)
            self.assertNotIn("filter:", keyframes)
        self.assertIn("html[data-motion=paused] *::before", css)
        self.assertIn("prefers-reduced-motion:reduce", css)
        # 可见性契约：body 必须透明。body 一旦自己刷底色，z-index:-1 的流动层会被它整块盖住，
        # 动画照跑但页面看起来完全静止（实测踩过）。底色由 html 承担。
        self.assertRegex(css, r"html\{[^}]*background:var\(--page\)")
        self.assertRegex(css, r"body\{[^}]*background:transparent[^}]*\}")
        # 斜向 repeating-linear-gradient 会在平铺接缝处露出竖向色阶（实测截图上有硬边，观感「脏」）：
        # 两个背景层的 background 声明里一律只用 radial-gradient 柔光。
        background = ""
        for rule in ("body:before{", "body:after{"):
            match = re.search(r"(?m)^" + re.escape(rule) + r"([^}]*)\}", css)
            self.assertIsNotNone(match, rule)
            declaration = match.group(1)
            self.assertNotIn("repeating-linear-gradient", declaration)
            background += " " + declaration
        # 配色契约（用户原话「颜色丑、整体不配合、不方便阅读」的客观复现）：上一版混了蓝 58,120,170
        # 与紫 120,90,180，alpha 又高到 .42，实测顶部空白带与底色色差 ΔE 7.1、次级文字对比度掉到
        # 4.02:1（低于 WCAG 4.5:1）。现在只准用本页两个强调色 teal 18,105,91 / amber 180,140,60。
        self.assertIn("rgba(18,105,91,.24)", background)
        self.assertIn("rgba(180,140,60,.16)", background)
        self.assertNotIn("58,120,170", background)
        self.assertNotIn("120,90,180", background)
        alphas = [float(value) for value in re.findall(r"rgba\([^)]*?,\s*(0?\.\d+)\)", background)]
        self.assertTrue(alphas, "背景柔光声明里应当有带小数 alpha 的 rgba 颜色")
        # 峰值 alpha .24 时两层柔光叠加处次级文字对底色约 4.7:1（仍达 WCAG AA 4.5:1）；.42 时只有 4.02:1。
        self.assertLessEqual(max(alphas), 0.25, "背景柔光 alpha 超过 .25 会把底色冲脏（实测 ΔE 7.1、次级文字对比度掉到 4.02:1）")
        # 幅度契约：绸缎必须真的在走。同一组 alpha 下位移从 ±6% 提到 ±11% 后，20 秒逐像素差
        # mean 0.97 -> 1.45、顶部空白带 |Δ|>=8 的像素占比 36% -> 54%（实测 A/B 对照）。
        self.assertIn("translate3d(-11%,-7%,0) rotate(-8deg)", css)
        self.assertIn("translate3d(11%,7%,0) rotate(8deg)", css)

    def test_color_tokens_separate_surfaces_and_typography_scales(self) -> None:
        css = self._current_stylesheet()
        # 洁净度（用户反馈「颜色很脏」）：页面底/次级面/浅色强调必须各自差一档，
        # 分隔线对白底要到 WCAG 图形 3:1，所以这些 token 值就此锁住。
        for token in (
            "--page:#e8ebee",
            "--paper-alt:#f2f4f6",
            "--muted:#3d4f47",
            "--line:#b0b8bd",
            "--teal-soft:#d6ede7",
            "--red-soft:#ffeeed",
            "--amber-soft:#fff3db",
        ):
            self.assertIn(token, css)
        self.assertNotIn("--page:#edf2ef", css)
        self.assertNotIn("--line:#c9d4ce", css)
        self.assertNotIn("--paper-alt:#f5f8f6", css)
        # 排版阶梯：概览主标题最大、分组标题更小更轻，三档随宽度单调（手机 28 < 基准 32 < 宽屏 34）。
        self.assertRegex(css, r"\.summary h1\{[^}]*font-size:32px")
        self.assertRegex(css, r"\.section-title\{[^}]*font-size:17px")
        self.assertIn(".summary h1{font-size:34px}", css)
        self.assertIn(".summary h1{font-size:28px}", css)
        self.assertIn(".summary{margin-top:24px;padding:24px 32px}", css)
        self.assertIn('width:4px;background:#667c71}', css)
        # 悬停微交互：按钮抬 1px + 浅影，并且必须在两套关动效规则里被压回原位。
        self.assertIn(".btn:not(:disabled):hover,.correct-btn:not(:disabled):hover{transform:translateY(-1px);box-shadow:0 4px 10px rgba(20,49,39,.12)}", css)
        self.assertIn(".surface,.task:hover,.btn:hover,.correct-btn:hover{transform:none!important}", css)
        self.assertIn("html[data-motion=paused] .btn:hover", css)


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
        for theme in ("dark", "light"):
            self.assertRegex(css, r"(?m)^html\[data-theme=" + theme + r"\]\{")
        self.assertNotIn("@media(prefers-color-scheme:dark)", css)
        self.assertNotIn("html[data-theme=dark] :root", css)
        self.assertNotIn("html[data-theme=light] :root", css)
        options = re.findall(r'name="pref-theme" type="radio" value="([^"]+)"', PAGE_HTML)
        self.assertEqual(options, ["light", "dark"])
        self.assertIn("document.documentElement.dataset.theme='light'", PAGE_HTML)

    def test_history_cancel_control_tracks_cancelled_terminal_state(self) -> None:
        self.assertIn('<button id="history-cancel" class="btn" type="button" hidden>取消回溯</button>', PAGE_HTML)
        self.assertIn("/api/history/fetch/cancel", PAGE_HTML)
        self.assertIn("status.stage === 'cancelled'", PAGE_HTML)
        self.assertIn("已取消", PAGE_HTML)
        self.assertIn("if (status.running)", PAGE_HTML)

    def test_dark_mode_preserves_dark_native_controls_and_readable_surfaces(self) -> None:
        css = self._current_stylesheet()
        self.assertNotRegex(css, r"html\{[^}]*color-scheme:light")
        self.assertIn("html[data-theme=dark]{", css)
        self.assertIn("color-scheme:dark", self._rule(css, "html[data-theme=dark]{"))
        self.assertNotIn("html:not([data-theme=light])", css)
        self.assertNotIn("html:not([data-theme])", css)
        self.assertEqual(css.count("{"), css.count("}"))
        dark = css[css.index("/* 深色主题修正"):]
        dark = re.sub(r"/\*.*?\*/", "", dark, flags=re.S)
        for selector, rule in (
            ("html[data-theme=dark] .journey-next", r"color:var\(--muted\)"),
            ("html[data-theme=dark] .summary p", r"color:var\(--muted\)"),
            ("html[data-theme=dark] .task .context", r"color:var\(--muted\)"),
            ("html[data-theme=dark] .task .meta", r"color:var\(--muted\)"),
            ("html[data-theme=dark] .journey", r"background:var\(--paper-alt\)"),
            ("html[data-theme=dark] .summary", r"background:var\(--paper-alt\)"),
            ("html[data-theme=dark] .calendar-day", r"background:var\(--paper-alt\)"),
        ):
            match = re.search(re.escape(selector) + r"[^{}]*\{([^}]*)\}", dark)
            self.assertIsNotNone(match, selector)
            self.assertRegex(match.group(1), rule)
        task_rule = re.search(r"html\[data-theme=dark\] \.task\{([^}]*)\}", dark)
        self.assertIsNotNone(task_rule, "html[data-theme=dark] .task")
        self.assertRegex(task_rule.group(1), r"background:var\(--paper\)")
        self.assertNotRegex(css, r"\.correct-select[^}]*color-scheme:light")

    def test_dark_tokens_reach_contrast_thresholds(self) -> None:
        css = self._current_stylesheet()
        dark = self._rule(css, "html[data-theme=dark]{")
        for token in (
            "--page:#0a0c12",
            "--paper:#141925",
            "--paper-alt:#1a2234",
            "--ink:#f3f5fa",
            "--muted:#9aa4b5",
            "--line:#6a7284",
            "--teal:#2ba895",
            "--teal-soft:#193f39",
            "--red:#ff6b6f",
            "--red-soft:#522120",
            "--amber:#ffb84d",
            "--amber-soft:#433416",
        ):
            self.assertIn(token, dark, token)
        # 上一版沿用亮色思路取 #303747，对卡片只有 1.47:1，卡片边界基本看不见。
        self.assertNotIn("--line:#303747", css)
        # 实算背书：次级文字 ≥4.5:1、分隔线 ≥3:1（含柔光抬亮后的最坏底色）。
        for surface in ("#0a0c12", "#141925", "#1a2234"):
            self.assertGreaterEqual(self._contrast("#9aa4b5", surface), 4.5, surface)
            self.assertGreaterEqual(self._contrast("#f3f5fa", surface), 4.5, surface)
            self.assertGreaterEqual(self._contrast("#6a7284", surface), 3.0, surface)
        for chip in ("#193f39", "#522120", "#433416"):
            self.assertGreaterEqual(self._contrast("#9aa4b5", chip), 4.5, chip)

    def test_dark_glow_alpha_stays_below_the_ceiling(self) -> None:
        css = self._current_stylesheet()
        for selector in ("html[data-theme=dark] body:before{", "html[data-theme=dark] body:after{"):
            declaration = self._rule(css, selector)
            self.assertNotIn("repeating-linear-gradient", declaration)
            alphas = [float(value) for value in re.findall(r"rgba\([^)]*?,\s*(0?\.\d+)\)", declaration)]
            self.assertTrue(alphas, selector)
            self.assertLessEqual(max(alphas), 0.25, selector)
        # 暗底柔光用同色系 teal 43,168,149 / amber 255,184,77，且 alpha 明显低于亮色的 .24/.16。
        self.assertIn("rgba(43,168,149,.16)", self._rule(css, "html[data-theme=dark] body:before{"))
        self.assertIn("rgba(255,184,77,.12)", self._rule(css, "html[data-theme=dark] body:after{"))

    def test_theme_boots_before_first_paint_and_defaults_to_light(self) -> None:
        head = PAGE_HTML.split("<head>", 1)[1].split("</head>", 1)[0]
        first_script = head.split("<script>", 1)[1].split("</script>", 1)[0]
        self.assertIn("nh_theme", first_script)
        self.assertIn("document.documentElement.dataset.theme", first_script)
        self.assertIn("t==='dark'?'dark':'light'", first_script)
        self.assertLess(head.index("nh_theme"), head.index("<style>"))
        self.assertIn('id="pref-theme"', PAGE_HTML)
        for value in ("light", "dark"):
            self.assertIn('value="' + value + '"', PAGE_HTML)
        self.assertNotIn('value="system"', PAGE_HTML)
        self.assertNotIn("delete document.documentElement.dataset.theme", PAGE_HTML)
        self.assertNotIn("localStorage.removeItem('nh_theme')", PAGE_HTML)
        self.assertIn("function initThemePreference()", PAGE_HTML)

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
        self.assertIn('<fieldset id="pref-theme" class="theme-choice-list">', PAGE_HTML)
        self.assertNotIn('id="pref-theme" aria-label="选择配色主题"', PAGE_HTML)
        self.assertNotIn('<select id="pref-theme"', PAGE_HTML)
        for value in ("light", "dark"):
            self.assertIn(
                '<input name="pref-theme" type="radio" value="' + value + '"', PAGE_HTML
            )
        self.assertNotIn('value="system"', PAGE_HTML)
        for name in ("light", "dark"):
            self.assertIn('class="theme-preview theme-preview-' + name + '"', PAGE_HTML)
            self.assertIn(".theme-preview-" + name + " i:nth-child(1){", css)
            self.assertIn(".theme-preview-" + name + " i:nth-child(3){", css)
        # 选中态必须由 JS 打在选择卡片上，不能只靠原生 radio。
        self.assertIn("card.classList.toggle('selected', input.checked)", PAGE_HTML)
        self.assertIn(".theme-option.selected{", css)
        # 三个卡片在窄屏要退成单列，否则会挤成三条细缝。
        self.assertIn(".theme-choice-list{grid-template-columns:minmax(0,1fr)}", css)

    def test_field_containers_are_block_level(self) -> None:
        """class="field" 之前在样式表里根本不存在，导致链接/输入框/按钮挤在一行互相压边。"""
        css = self._all_css()
        field_rule = re.search(r"(?m)^\.field\{([^}]*)\}", css)
        self.assertIsNotNone(field_rule, "missing .field rule")
        self.assertIn("display:block", field_rule.group(1))
        # 输入框要整行，否则又会出现相邻元素压边。
        input_rule = re.search(r"(?m)^\.field>input,\.field>select,\.field>textarea\{([^}]*)\}", css)
        self.assertIsNotNone(input_rule, "missing .field input rule")
        self.assertIn("display:block", input_rule.group(1))
        self.assertIn("width:100%", input_rule.group(1))
        # 清除按钮必须搬出 label，否则和输入框同一行重叠。
        self.assertIn(
            '<label id="cx-manual-cookie" class="field"><span>Cookie</span>'
            '<input id="cx-cookie" type="password" autocomplete="off" placeholder="粘贴浏览器 Cookie">'
            "</label>",
            PAGE_HTML,
        )
        self.assertNotIn('<input id="cx-cookie" type="password" autocomplete="off" placeholder="粘贴浏览器 Cookie"><button id="cx-clear"', PAGE_HTML)

    def test_chaoxing_manual_cookie_has_the_same_three_step_guidance(self) -> None:
        """方式二要和方式一同规格：三步、状态点、打开登录页按钮。"""
        start = PAGE_HTML.index('id="cx-manual-option"')
        end = PAGE_HTML.index("我的课程", start)
        manual = PAGE_HTML[start:end]
        for step in ("login", "copy", "save"):
            self.assertIn('id="cx-manual-step-' + step + '-state"', manual)
        self.assertIn("document.cookie", manual)
        self.assertIn('href="https://i.chaoxing.com/"', manual)
        self.assertIn("打开学习通登录页", manual)
        self.assertIn("function setCxManualStep(step,state,text)", PAGE_HTML)
        self.assertIn("function updateCxManualGuide(hasCookie,expired)", PAGE_HTML)
        self.assertIn("updateCxManualGuide(!!(v.chaoxing_cookie", PAGE_HTML)
        # 凭证提示不能少：cookie 等同于密码。
        self.assertIn("Cookie 等同于登录凭证", manual)

    def test_llm_state_copy_distinguishes_off_from_missing_key(self) -> None:
        """用户报 AI 摘要默认落本地模型：配置好了却还说「没有用 AI」会让人以为坏了。

        provider='off' 才是真的不用 AI；选了服务商只差钥匙时要指路，别只报「没在用」。
        """
        self.assertIn("if(state.provider==='off'||!state.enabled)", PAGE_HTML)
        self.assertNotIn("var item=llmProvider(state.provider)||{};if(!state.enabled){", PAGE_HTML)
        self.assertIn("想让它替你读消息、写待办，就在上面选一家", PAGE_HTML)
        self.assertIn("还差最后一步——钥匙", PAGE_HTML)
        # 不用钥匙的服务商（本机模型）不该显示「已配置：***」的钥匙框。
        self.assertIn("if(!item.needs_key){keyInput.placeholder='这家不用钥匙';", PAGE_HTML)
        self.assertIn("keyRow.hidden=true", PAGE_HTML)

    def test_napcat_status_text_does_not_claim_the_api_is_up(self) -> None:
        """引擎=程序文件已就位，接口可用=能连上 3001；两者混为一谈就是「没运行」旁边躺着「已运行」。"""
        self.assertIn("连不上 NapCat 的接口（127.0.0.1:3001）", PAGE_HTML)
        self.assertIn("上面「NapCat 引擎」只表示程序文件已就位，不代表接口已经可用", PAGE_HTML)
        self.assertIn("NapCat 进程已经在运行，正在确认接口和登录状态…", PAGE_HTML)
        self.assertIn("已找到 NapCat 引擎（程序文件）", PAGE_HTML)
        # 旧文案把「进程在跑」当成「登录状态可用」，必须消失。
        self.assertNotIn("QQ 未登录：NapCat 在运行，但还没有可用的登录状态", PAGE_HTML)

    def test_mobile_calendar_starts_collapsed_with_a_toggle_button(self) -> None:
        css = self._current_stylesheet()
        self.assertIn('id="calendar-toggle"', PAGE_HTML)
        self.assertIn("function setCollapsed(collapsed)", PAGE_HTML)
        self.assertIn("window.matchMedia('(max-width:480px)').matches", PAGE_HTML)
        self.assertIn("toggle.textContent=collapsed?'查看月历':'收起月历'", PAGE_HTML)
        # 桌面默认不显示这个按钮；手机（≤480px）才显示，并且折叠时只藏面板内部。
        self.assertIn(".month-controls .calendar-toggle{display:none}", css)
        mobile = css.split("@media(max-width:480px){", 1)[1]
        self.assertIn(".month-controls .calendar-toggle{display:inline-flex", mobile)
        self.assertRegex(mobile, r"#calendar-panel\.calendar-collapsed \.weekday-row[^}]*#calendar[^}]*")
        # 按钮必须在 #calendar-panel 里面：面板会被 taskSurface.appendChild(calendarPanel) 整体搬进「待办」
        # 标签页，按钮放在外面就会留在原地、和面板分家。
        self.assertIn("taskSurface.appendChild(calendarPanel)", PAGE_HTML)
        panel = PAGE_HTML.split('id="calendar-panel"', 1)[1].split("</section>", 1)[0]
        self.assertIn('id="calendar-toggle"', panel)

    def test_sync_address_alternates_are_rendered_and_switchable(self) -> None:
        self.assertIn('id="sync-alts" class="sync-alts" hidden', PAGE_HTML)
        self.assertIn(".sync-alt{", self._current_stylesheet())
        self.assertIn("function renderAddressChoices(calendars, current)", PAGE_HTML)
        self.assertIn("renderAddressChoices(data.calendars, calendar.url)", PAGE_HTML)
        self.assertIn("button.className = 'btn ghost sync-alt'", PAGE_HTML)
        self.assertIn("input.dataset.testScheme = new URL(item.url).protocol", PAGE_HTML)
        self.assertIn("setupApi('/api/sync/test?url=' + encodeURIComponent(requestUrl))", PAGE_HTML)
        self.assertNotIn("fetch(requestUrl, {method: 'GET', cache: 'no-store'})", PAGE_HTML)

    def test_reduced_motion_removes_depth_and_active_transform(self) -> None:
        reduced = self._current_stylesheet().split("@media(prefers-reduced-motion:reduce){", 1)[1]
        rules: dict[str, str] = {}
        for selector, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", reduced):
            for name in selector.split(","):
                rules[name.strip()] = declarations
        depth_values = dict(re.findall(r"(--depth-[a-z-]+):\s*(\d+)px", rules.get(":root", "")))
        self.assertEqual(depth_values, {"--depth-mid": "0", "--depth-top": "0"})
        summary = dict(re.findall(r"([a-z-]+):([^;]+)", rules.get(".summary", "")))
        self.assertEqual(summary.get("animation"), "none!important")
        self.assertEqual(summary.get("transform"), "none!important")
        for selector in (".camera-enter", ".task.is-focused", ".task:focus-within", ".surface", "button:active"):
            declarations = dict(re.findall(r"([a-z-]+):([^;]+)", rules.get(selector, "")))
            self.assertEqual(declarations.get("transform"), "none!important", selector)
        durations = [int(value) for value in re.findall(r"transition-duration:(\d+)ms!important", reduced)]
        self.assertTrue(durations)
        self.assertTrue(all(value <= 100 for value in durations))

    def test_interactive_feedback_and_backend_urgency_are_wired(self) -> None:
        css = self._current_stylesheet()
        for selector in (
            "button:not(:disabled):hover", "button:not(:disabled):active", "a[href]:hover",
            "summary:hover", "select:not(:disabled):hover", "input:not(:disabled):active",
            "#groups .group-row:hover", ".preference:hover", ".history-groups label:hover",
        ):
            self.assertIn(selector, css)
        self.assertIn("transition-duration:100ms", css)
        self.assertIn("button:disabled,input:disabled,select:disabled{cursor:not-allowed;opacity:.5}", css)
        self.assertIn("outline:2px solid #12695b!important;outline-offset:2px!important", css)
        self.assertIn("html[data-motion=paused]", css)
        node_start = PAGE_HTML.index("function taskNode(task)")
        node_end = PAGE_HTML.index("function section(title", node_start)
        task_node = PAGE_HTML[node_start:node_end]
        self.assertIn("task.effective_urgent", task_node)
        self.assertIn("aria-pressed", task_node)
        self.assertIn("task.urgent_override", task_node)
        self.assertIn("sendUrgentOverride", PAGE_HTML)
        self.assertIn("/api/tasks/urgent", PAGE_HTML)
        self.assertIn("/api/sync/info", PAGE_HTML)
        self.assertIn("/api/sync/qr.png?text=", PAGE_HTML)
        self.assertIn("webcal:", PAGE_HTML)

    def test_task_hitboxes_stay_fixed_across_hover_focus_and_active_states(self) -> None:
        css = self._current_stylesheet()
        task_states = re.search(
            r"\.task:not\(\.done\):not\(\[aria-busy=true\]\):hover,\.task:not\(\.done\):not\(\[aria-busy=true\]\)\.is-focused,\.task:not\(\.done\):not\(\[aria-busy=true\]\):focus-within\{([^}]*)\}",
            css,
        )
        self.assertIsNotNone(task_states)
        self.assertIn("transform:none", task_states.group(1))
        self.assertIn(".task.is-focused,.task:focus-within{transform:none}", css)

        pressed = re.search(r"\.task \.check:active:not\(:disabled\)\{([^}]*)\}", css)
        self.assertIsNotNone(pressed)
        self.assertIn("transform:none", pressed.group(1))
        self.assertIn("background:#d2e9e2", pressed.group(1))
        self.assertIn("box-shadow:inset 0 0 0 2px", pressed.group(1))

    def test_calendar_follows_today_on_mobile_and_uses_a_wide_desktop_column(self) -> None:
        css = self._current_stylesheet()
        render_start = PAGE_HTML.index("function render(data)")
        render_end = PAGE_HTML.index("function syncTasks()", render_start)
        renderer = PAGE_HTML[render_start:render_end]
        self.assertRegex(renderer, r"var blocks = \[todayBlock,\s*calendarPanel,\s*overdueBlock")
        self.assertIn("section('已过期', overdue, {className: 'overdue', collapsed: true})", renderer)
        base_css = css.split("@media(min-width:900px)", 1)[0]
        calendar_rules = re.findall(r"\.calendar-day\{([^}]*)\}", base_css)
        self.assertTrue(calendar_rules)
        baseline_height = int(re.findall(r"min-height:(\d+)px", calendar_rules[-1])[-1])
        self.assertGreaterEqual(baseline_height, 56)
        self.assertIn("@media(min-width:1024px)", css)
        self.assertIn("width:min(calc(100% - 64px),1760px);max-width:1760px", css)
        self.assertIn(".workspace{grid-template-columns:minmax(0,1fr) minmax(320px,560px);gap:24px}", css)
        self.assertRegex(css, r"\.side-rail\{grid-column:2;grid-row:1;grid-template-columns:minmax\(0,1fr\)")
        self.assertRegex(css, r"#calendar-panel\{position:static;width:auto;min-width:0")

    def test_summary_static_state_has_no_depth_transform(self) -> None:
        css = self._current_stylesheet()
        self.assertIn("--depth-mid:0px", css)
        self.assertRegex(css, r"\.summary\{[^}]*transform:none;animation:summary-enter")
        self.assertIn("to{opacity:1;transform:none}", css)
        self.assertNotIn("to{opacity:1;transform:translate3d(0,0,var(--depth-mid))", css)

    def test_completion_feedback_runs_after_confirmation_and_before_refresh(self) -> None:
        helper_start = PAGE_HTML.index("function animateConfirmedTaskCompletion")
        mutation_start = PAGE_HTML.index("function runTaskMutation", helper_start)
        action_start = PAGE_HTML.index("function sendAction", mutation_start)
        helper = PAGE_HTML[helper_start:mutation_start]
        mutation = PAGE_HTML[mutation_start:action_start]
        confirmed = mutation.index("if (!result.ok)")
        animation = mutation.index("var completionAnimation", confirmed)
        refreshed = mutation.index("return syncTasks().then", animation)
        self.assertLess(confirmed, animation)
        self.assertLess(animation, refreshed)
        self.assertIn("payload.action === 'done'", mutation)
        self.assertRegex(helper, re.compile(r"classList\.add\('completion-confirmed'\).*setTimeout\(resolve, 240\).*classList\.add\('completing'\).*setTimeout\(resolve, 200\)", re.S))

    def test_recent_push_feed_uses_type_time_and_body_not_count_summary(self) -> None:
        start = PAGE_HTML.index("function loadNotices()")
        end = PAGE_HTML.index("\nfunction", start + 1)
        feed = PAGE_HTML[start:end]
        self.assertIn("item.kind", feed)
        self.assertIn("item.created_at", feed)
        self.assertIn("item.body", feed)
        self.assertNotIn("item.summary", feed)
        self.assertNotIn("!data.ok", feed)
        self.assertIn("暂无推送记录。", feed)

    def test_inbox_attachment_renderer_keeps_text_and_hides_missing_image(self) -> None:
        start = PAGE_HTML.index("function renderInboxItem(")
        end = PAGE_HTML.index("\nfunction", start + 1)
        renderer = PAGE_HTML[start:end]
        self.assertIn("details.appendChild(el('p', '', content))", renderer)
        self.assertIn("item.source === 'onebot-attachment'", renderer)
        self.assertIn("/api/inbox/image?", renderer)
        self.assertNotIn("/^onebot:.+:attach:[0-9a-f]{16}$/", renderer)
        self.assertIn("image.onerror = function () { image.remove(); }", renderer)

    def test_inbox_hides_zero_count_filters_and_uses_one_line_empty_states(self) -> None:
        start = PAGE_HTML.index("function loadInbox(")
        end = PAGE_HTML.index("function loadInboxTab", start)
        inbox = PAGE_HTML[start:end]
        self.assertIn("countsBar.hidden = filterCounts.all === 0", inbox)
        self.assertIn("button.hidden = count === 0", inbox)
        self.assertIn("当前筛选条件下暂无通知。", inbox)
        self.assertIn("暂无判定结果。", inbox)
        self.assertNotIn("allCount", inbox)
        self.assertNotIn("先回溯聊天记录", inbox)

    def test_upcoming_carousel_and_motion_preference_are_local_and_interruptible(self) -> None:
        start = PAGE_HTML.index("var upcomingGroups =")
        end = PAGE_HTML.index("function animateConfirmedTaskCompletion", start)
        carousel = PAGE_HTML[start:end]
        self.assertIn("(data.today || []).concat(data.week || [], data.later || [])", carousel)
        self.assertNotIn("dueTodayCount", carousel)
        self.assertIn("? '今天 · '", carousel)
        self.assertIn("}, 3000);", carousel)
        self.assertIn("addEventListener('mouseenter'", carousel)
        self.assertIn("upcomingFocused = upcomingRoot.contains(document.activeElement)", carousel)
        self.assertIn("button.addEventListener('focus'", carousel)
        self.assertIn("button.addEventListener('blur'", carousel)
        self.assertIn("document.addEventListener('visibilitychange'", carousel)
        settings_start = PAGE_HTML.index("function setMotionPreference")
        settings_end = PAGE_HTML.index("reducedMotionQuery.addEventListener", settings_start)
        setting = PAGE_HTML[settings_start:settings_end]
        self.assertIn("localStorage.setItem('nh_motion'", setting)
        self.assertNotIn("fetch(", setting)
        self.assertIn('id="pref-motion-enabled"', PAGE_HTML)
        css = self._current_stylesheet()
        self.assertIn("summary-glow-breathe 4s", css)
        self.assertIn("summary-shadow-breathe 4s", css)
        self.assertIn("badge-breathe 4s ease-in-out 600ms", css)
        self.assertIn("transform:scale(1.12)", css)
        self.assertIn("box-shadow:0 0 0 5px", css)
        self.assertIn("height:200px", css)
        self.assertIn("#upcoming-carousel.is-switching", css)
        self.assertIn("root.classList.add('is-switching')", carousel)
        self.assertIn("renderUpcomingSlide(true)", carousel)
        self.assertIn("html[data-motion=paused]", css)

    def test_upcoming_height_and_motion_fallback_contract(self) -> None:
        css = self._current_stylesheet()
        fixed = re.search(r"\.upcoming-carousel\{[^}]*height:(\d+)px[^}]*overflow:hidden", css)
        self.assertIsNotNone(fixed, "轮播必须定高并 overflow:hidden，避免切换顶动下方元素")
        # 固定高度要容得下渲染器可能产生的最坏内容：表头 + 3 条(每条 -webkit-line-clamp:2) + 「另有 N 件」1 行 + 间距 ≈ 176px
        self.assertGreaterEqual(int(fixed.group(1)), 190)
        self.assertIn("-webkit-line-clamp:2", css)
        narrow = re.search(r"@media\(max-width:700px\)\{\.upcoming-carousel\{height:(\d+)px", css)
        self.assertIsNotNone(narrow, "窄屏也要定高，否则切换同样会顶动下方元素")
        self.assertGreaterEqual(int(narrow.group(1)), 190)
        reduced = css.split("@media(prefers-reduced-motion:reduce){", 1)[1]
        self.assertIn(".upcoming-carousel{transition-duration:0ms!important}", reduced)
        self.assertIn("html[data-motion=paused] *,html[data-motion=paused] *::before", css)
        start = PAGE_HTML.index("function renderUpcomingSlide(animate)")
        end = PAGE_HTML.index("function renderUpcoming(data", start)
        slide = PAGE_HTML[start:end]
        self.assertIn("root.classList.remove('is-switching')", slide)
        self.assertIn("root.classList.add('is-switching')", slide)
        # 换内容必须延后到定时器里（先淡出 → 换 → 再淡入）；若在同一帧换完并删类，浏览器只绘制最终态，过渡永远不可见
        self.assertIn("upcomingSwitchTimer = window.setTimeout(", slide)
        self.assertIn("}, UPCOMING_SWITCH_MS);", slide)
        self.assertIn("if (!animate || motionIsPaused())", slide)
        self.assertNotIn("window.requestAnimationFrame", slide)
        # JS 的延时必须与 CSS 里声明的过渡时长一致
        fade = re.search(r"#upcoming-date,#upcoming-tasks\{transition:opacity (\d+)ms", css)
        self.assertIsNotNone(fade, "轮播淡入淡出必须有明确的 transition 时长")
        self.assertIn("var UPCOMING_SWITCH_MS = %s;" % fade.group(1), PAGE_HTML)

    def test_summary_breathe_keeps_text_still_and_moves_only_the_aura(self) -> None:
        css = self._current_stylesheet()
        card = re.search(r"\.summary\{[^}]*\}", css)
        self.assertIsNotNone(card)
        # 实测（CDP 冻结动画相位 + 截图的边缘能量）：卡片一旦位移/缩放，文字层就被合成器按小数设备像素重采样，
        # edge_mean 从静止的 6.95 掉到 5.78（纯 translateY）或 4.63（translateY + scale(1.004)），
        # 用户看到的就是「有时糊有时清晰、字微微闪烁」。所以文字的容器绝不许动。
        self.assertNotIn("breathe", card.group(0))
        self.assertNotIn("@keyframes summary-breathe", css)
        after = css.split(".summary::after{", 1)[1].split("}", 1)[0]
        self.assertIn("summary-glow-breathe 4s", after)
        self.assertIn("pointer-events:none", after)
        self.assertIn("z-index:-1", after)  # 光晕压在卡片下面，绝不覆盖文字
        # 呼吸必须有肉眼可见的幅度：只改几个百分点等于「动画没掉了」（v2026.10.12 就是这么翻车的）
        glow = re.search(r"@keyframes summary-glow-breathe\{([^@]*)\}\}", css)
        self.assertIsNotNone(glow)
        glow_scales = [float(v) for v in re.findall(r"scale\(([0-9.]+)\)", glow.group(1))]
        glow_opacities = [float(v) for v in re.findall(r"opacity:([0-9.]+)", glow.group(1))]
        self.assertGreaterEqual(max(glow_scales) - min(glow_scales), 0.08)
        self.assertGreaterEqual(max(glow_opacities) - min(glow_opacities), 0.4)
        self.assertNotIn("box-shadow", glow.group(1))  # 绘制属性会让浏览器每帧重绘整卡文字
        ground = re.search(r"\.summary::before\{[^}]*\}", css)
        self.assertIsNotNone(ground, "需要一层无文字的地面阴影层来承接阴影的扩散")
        self.assertIn("summary-shadow-breathe 4s", ground.group(0))
        self.assertNotIn("box-shadow", ground.group(0))
        shadow = re.search(r"@keyframes summary-shadow-breathe\{([^@]*)\}\}", css)
        self.assertIsNotNone(shadow)
        shadow_scales = [float(v) for v in re.findall(r"scale\(([0-9.]+)\)", shadow.group(1))]
        self.assertGreaterEqual(max(shadow_scales) - min(shadow_scales), 0.08)
        self.assertNotIn("box-shadow", shadow.group(1))

    def test_header_brand_mark_is_the_app_icon_not_a_letter(self) -> None:
        mark = re.search(r'<svg class="brand-mark".*?</svg>', PAGE_HTML, re.S)
        self.assertIsNotNone(mark, "页头的小 logo 必须是品牌 mark，而不是字母方块")
        check = "M112.64 307.2 L184.32 378.88 L276.48 235.52"
        self.assertIn(check, mark.group(0))
        # 与托盘/任务栏用的图标同源（assets/icon.svg），避免页头又变成另一套画法
        icon = (PROJECT_ROOT / "assets" / "icon.svg").read_text(encoding="utf-8")
        self.assertIn(check, icon)
        self.assertNotIn('<span class="brand-mark"', PAGE_HTML)

    def test_transport_errors_are_translated_before_display(self) -> None:
        # QQ 未登录时 NapCat 的 OneBot 端口拒绝连接，服务端 JSON 里保留原始 socket 文本；
        # 界面必须只显示一句人话，且翻译只写一份（friendlyError），不许各面板各写一套。
        self.assertIn("function friendlyError(raw)", PAGE_HTML)
        self.assertIn("10061|积极拒绝|Connection refused|ECONNREFUSED", PAGE_HTML)
        self.assertIn("x.error = friendlyError(x.error)", PAGE_HTML)  # setupApi
        self.assertIn("body.error = friendlyError(body.error)", PAGE_HTML)  # api
        self.assertIn("friendlyError((error && error.message) || error)", PAGE_HTML)
        self.assertIn("连接被拒绝|QQ 未登录", PAGE_HTML)  # checkSetup 的兜底判定也要认这句人话

    def test_group_subscription_ui_uses_suggestions_and_truthful_sources(self) -> None:
        self.assertIn("/api/groups/suggest", PAGE_HTML)
        self.assertIn("/api/napcat/groups", PAGE_HTML)
        self.assertIn('id="group-search" class="group-search" type="search"', PAGE_HTML)
        self.assertIn("其它 ", PAGE_HTML)
        self.assertIn("全选建议", PAGE_HTML)
        self.assertIn("清空", PAGE_HTML)
        self.assertIn("suggestion.source==='llm'?'AI 识别'", PAGE_HTML)
        self.assertIn("suggestion.source==='heuristic'?'按关键词识别'", PAGE_HTML)
        self.assertIn("未选择任何群，将清空所有订阅", PAGE_HTML)
        self.assertIn("前往 QQ 接入", PAGE_HTML)

    def test_chaoxing_cookie_setup_orders_recommended_auto_then_collapsed_manual(self) -> None:
        start = PAGE_HTML.index('id="cx-auto-option"')
        end = PAGE_HTML.index("root.appendChild(pairCard())", start)
        chaoxing = PAGE_HTML[start:end]
        self.assertIn('id="cx-manual-option"', chaoxing)
        self.assertIn('<details id="cx-manual-option"', chaoxing)
        self.assertIn("<summary>自动获取失败时再试这条</summary>", chaoxing)
        self.assertIn('id="cx-manual-cookie"', chaoxing)
        self.assertIn("方式一：浏览器自动获取", chaoxing)
        self.assertIn(">推荐</span>", chaoxing)
        self.assertLess(chaoxing.index('id="cx-auto-option"'), chaoxing.index('id="cx-manual-option"'))
        self.assertIn("方式二：手工粘贴 Cookie", chaoxing)
        self.assertIn("保持本页打开", chaoxing)
        self.assertIn("数据库及日志副本", chaoxing)
        self.assertIn("会提示下一步", chaoxing)
        self.assertNotIn("② 完全退出该浏览器（含托盘图标和后台进程）", chaoxing)
        self.assertNotIn("③ 回到这里点“重试自动获取”", chaoxing)
        self.assertIn("allow pasting", chaoxing)
        self.assertIn("document.cookie", chaoxing)
        self.assertIn("HttpOnly 登录 Cookie 不会出现在这里", chaoxing)
        self.assertIn('href="https://i.chaoxing.com/"', chaoxing)
        for step in ("login", "exit", "retry"):
            self.assertIn('id="cx-step-' + step + '-state"', chaoxing)
        for step in ("login", "copy", "save"):
            self.assertIn('id="cx-manual-step-' + step + '-state"', chaoxing)
        for category in ("browser_unavailable", "browser_locked", "no_chaoxing_cookie", "decrypt_failed"):
            self.assertIn(category, chaoxing)
        self.assertIn("renderCxAttempts(v.attempts||[])", chaoxing)
        self.assertIn("method:'POST'", chaoxing)
        self.assertNotIn("cx-manual-cookie').hidden=false", chaoxing)

    def test_chaoxing_wechat_enable_toggles_post_only_the_enabled_field(self) -> None:
        start = PAGE_HTML.index("function saveEnabledToggle")
        end = PAGE_HTML.index("saveEnabledToggle('wx-enabled'", start)
        toggle = PAGE_HTML[start:end]
        self.assertIn("input.addEventListener('change'", toggle)
        self.assertIn("var payload={};payload[key]=enabled", toggle)
        self.assertIn("body:JSON.stringify(payload)", toggle)
        self.assertIn("input.checked=!enabled", toggle)
        self.assertIn("保存失败，已恢复原状态：", toggle)
        self.assertIn("saveEnabledToggle('wx-enabled','wx-result','wechat_enabled')", PAGE_HTML)
        self.assertIn("saveEnabledToggle('cx-enabled','cx-result','chaoxing_enabled')", PAGE_HTML)

    def test_chaoxing_auto_cookie_success_preserves_enabled_checkbox(self) -> None:
        start = PAGE_HTML.index("document.getElementById('cx-auto-cookie').onclick")
        end = PAGE_HTML.index("document.getElementById('cx-load-courses').onclick", start)
        handler = PAGE_HTML[start:end]
        self.assertIn("document.getElementById('cx-enabled').checked=true", handler)
        self.assertNotIn("checked=!!s.chaoxing_enabled", handler)

    def test_wechat_dingtalk_subscription_ui_shows_recommendations(self) -> None:
        for source in ("wx", "dt"):
            self.assertIn('id="' + source + '-select-suggested"', PAGE_HTML)
            self.assertIn('id="' + source + '-suggest-source"', PAGE_HTML)
        self.assertIn("按关键词识别", PAGE_HTML)
        self.assertIn("appendRecommendation(meta,row)", PAGE_HTML)
        self.assertIn("data-suggested=\"true\"", PAGE_HTML)
        self.assertIn("建议订阅", PAGE_HTML)

    def test_task_mutation_waits_for_server_and_syncs_calendar_before_success(self) -> None:
        start = PAGE_HTML.index("function runTaskMutation")
        end = PAGE_HTML.index("function sendAction", start)
        mutation = PAGE_HTML[start:end]
        confirmed = mutation.index("if (!result.ok)")
        refreshed = mutation.index("return syncTasks().then", confirmed)
        announced = mutation.index("showFeedback(success, 'success')", confirmed)
        self.assertLess(confirmed, refreshed)
        self.assertLess(refreshed, announced)
        self.assertIn("showFeedback('操作未完成：' + error.message, 'error', retry)", mutation)
        self.assertIn("window.syncCalendarTasks(data)", PAGE_HTML)

    def test_settings_and_hosting_require_successful_response_and_status(self) -> None:
        self.assertIn("if(!result.ok)throw new Error(result.error||'服务端未保存设置')", PAGE_HTML, "settings success requires a confirmed response")
        save_start = PAGE_HTML.index("function savePreferences")
        save_end = PAGE_HTML.index("\n  setPreferencesBusy(true);", save_start)
        saving = PAGE_HTML[save_start:save_end]
        self.assertLess(saving.index("if(!result.ok)"), saving.index("showFeedback('设置已保存','success')"))
        self.assertIn("showFeedback('设置未保存：'+error.message,'error'", saving, "settings failures must be visible")
        start = PAGE_HTML.index("function changeHosting")
        end = PAGE_HTML.index("document.getElementById('hosting-start'", start)
        hosting = PAGE_HTML[start:end]
        self.assertIn("if(!result.ok)throw new Error(result.error||'服务端未确认操作')", hosting, "hosting success requires a confirmed response")
        self.assertGreaterEqual(hosting.count("refreshHosting().then(function(status)"), 2, "start/stop must read hosting status after action")
        self.assertIn("!!status.hosting_active!==desired", hosting, "start/stop must verify the requested state")


    def test_pinned_panel_and_pin_control_are_wired(self) -> None:
        self.assertIn('id="pinned-panel"', PAGE_HTML)
        self.assertIn('id="pinned-list"', PAGE_HTML)
        self.assertIn('id="pinned-empty"', PAGE_HTML)
        self.assertIn("function renderPinned(data)", PAGE_HTML)
        self.assertIn("renderPinned(data);", PAGE_HTML)
        self.assertIn("'/api/tasks/pin'", PAGE_HTML)
        self.assertIn("pin-toggle", PAGE_HTML)
        # 置顶面板必须在 QQ 接入上方（用户要求：月历右边、QQ 接入上面）
        self.assertLess(PAGE_HTML.index('id="pinned-panel"'), PAGE_HTML.index('id="connect"'))
        # 紧急按钮不再写死宽度：min-width:88px 会让两个字比按钮窄一大截
        self.assertNotIn("min-width:88px", PAGE_HTML)


    def test_new_task_cards_and_calendar_month_animate(self) -> None:
        self.assertIn(".task.is-new{animation:task-enter", PAGE_HTML)
        self.assertIn("@keyframes task-enter{from{opacity:0", PAGE_HTML)
        self.assertIn("var lastRenderedTaskIds = null;", PAGE_HTML)
        self.assertIn("if (previousIds && !previousIds[cardId]) card.classList.add('is-new');", PAGE_HTML)
        # 翻月走的 animateSurface 需要 #calendar 自己带 transition，否则只跳不变
        self.assertIn("gap:4px;transition:transform 220ms", PAGE_HTML)


    def test_overview_can_switch_between_due_and_ongoing(self) -> None:
        css = self._current_stylesheet()
        # 概览顶部必须有两个可点的模式按钮（旧实现是写死的 <span class="eyebrow">最近到期</span>）
        self.assertNotIn('<span class="eyebrow">最近到期</span>', PAGE_HTML)
        self.assertIn('<div class="upcoming-mode" role="group" aria-label="今日概览显示内容">', PAGE_HTML)
        self.assertIn('id="upcoming-mode-due"', PAGE_HTML)
        self.assertIn('id="upcoming-mode-now"', PAGE_HTML)
        self.assertIn('>最近到期</button>', PAGE_HTML)
        self.assertIn('>正在进行</button>', PAGE_HTML)
        self.assertIn('aria-label="今日概览：最近到期与正在进行"', PAGE_HTML)
        # 选择要记住（刷新后仍然是你选的那个）
        self.assertIn("var UPCOMING_MODE_KEY = 'qq_digest_overview_mode';", PAGE_HTML)
        self.assertIn("try { window.localStorage.setItem(UPCOMING_MODE_KEY, upcomingMode); } catch (error) {}", PAGE_HTML)
        self.assertIn("button.addEventListener('click', function () { setUpcomingMode(mode); });", PAGE_HTML)
        # 「正在进行」= 事务区间：从收到通知起持续到截止时间，跨天算；当天写明「几点到几点」的按那个时段算
        self.assertIn("function parseTimeWindow(task) {", PAGE_HTML)
        self.assertIn(r"var pattern = /(\d{1,2})\s*[:：]\s*(\d{2})/g;", PAGE_HTML)
        self.assertIn("if (!/[-–—~～]|至/.test(between)) return null;", PAGE_HTML)
        # 同一时刻重复出现（23:59 … 23:59）不算窗口，否则最后一分钟会误报「正在进行」
        self.assertIn("if (end === start) return null;", PAGE_HTML)
        self.assertIn("function parseStamp(value) {", PAGE_HTML)
        self.assertIn("function ongoingSpan(task, now) {", PAGE_HTML)
        self.assertIn("function collectUpcomingNow(data) {", PAGE_HTML)
        # 截止时间常常只是开始时刻，所以当天时段要和 deadline 取并集（如 13:30 截止 + 13:30-14:55 上课）
        self.assertIn("if (!start || bounds.start < start) start = bounds.start;", PAGE_HTML)
        self.assertIn("if (!end || bounds.end > end) end = bounds.end;", PAGE_HTML)
        self.assertIn("if (!end) return null;", PAGE_HTML)
        self.assertIn("if (now < start || now > end) return null;", PAGE_HTML)
        self.assertIn("var span = ongoingSpan(task, now);", PAGE_HTML)
        self.assertIn("upcomingNow = collectUpcomingNow(data);", PAGE_HTML)
        # 两种模式用同一套渲染：'now' 分支按「还剩多久截止 / 几点结束」展示，空的时候也要留在原地（否则按钮会跟着消失）
        self.assertIn("if (group.mode === 'now') {", PAGE_HTML)
        self.assertIn("document.getElementById('upcoming-date').textContent = '正在进行的事项';", PAGE_HTML)
        self.assertIn("list.appendChild(el('li', 'upcoming-more', '现在没有正在进行的日程'));", PAGE_HTML)
        self.assertIn("+ ' · ' + entry.span.hint", PAGE_HTML)
        self.assertIn("var upcomingDue = [];", PAGE_HTML)
        # 正在进行也要按页轮换，并且每页都装满：旧实现只渲染前三条 + 「另有 N 件」（用户报的「没有轮换 + 显示不全」）
        self.assertIn("var UPCOMING_PAGE_SIZE = 3;", PAGE_HTML)
        self.assertIn("function nowUpcomingPages() {", PAGE_HTML)
        self.assertIn("pages: Math.ceil(upcomingNow.length / UPCOMING_PAGE_SIZE),", PAGE_HTML)
        self.assertIn("? nowUpcomingPages()", PAGE_HTML)
        self.assertNotIn("group.now.slice(0, 3)", PAGE_HTML)
        self.assertNotIn("'另有 ' + (group.now.length - 3)", PAGE_HTML)
        self.assertIn("group.page.forEach(function (entry) {", PAGE_HTML)
        self.assertIn("+ (group.pages > 1 ? ' · 第 ' + (upcomingIndex + 1) + '/' + group.pages + ' 页' : '');", PAGE_HTML)
        self.assertIn("document.getElementById('upcoming-prev').hidden = group.pages < 2;", PAGE_HTML)
        self.assertIn("document.getElementById('upcoming-next').hidden = group.pages < 2;", PAGE_HTML)
        # 轮询刷新数据时保留当前页，否则轮播每刷一次就被拨回第一页
        self.assertIn("    : upcomingIndex;", PAGE_HTML)
        # 样式：选中的那个按钮要有明显状态（浅绿底 + teal 描边），键盘可达
        self.assertIn(".upcoming-mode{display:inline-flex;gap:4px;margin:0 0 4px}", css)
        self.assertIn(".upcoming-mode-btn.is-active{background:var(--teal-soft);border-color:var(--teal);color:var(--teal)}", css)
        self.assertIn(".upcoming-mode-btn:focus-visible{outline:2px solid var(--teal);outline-offset:1px}", css)

    def test_calendar_shows_time_caps_three_events_and_opens_a_day_panel(self) -> None:
        css = self._current_stylesheet()
        self.assertIn("var MAX_CALENDAR_EVENTS=3;", PAGE_HTML)
        self.assertIn("cell.setAttribute('data-has-events','true')", PAGE_HTML)
        self.assertIn("cell.setAttribute('tabindex','0')", PAGE_HTML)
        self.assertIn("cell.setAttribute('aria-controls','calendar-detail')", PAGE_HTML)
        self.assertIn("more.className='calendar-more'", PAGE_HTML)
        self.assertIn("var rest=items.length-MAX_CALENDAR_EVENTS;", PAGE_HTML)
        self.assertIn("more.textContent='+'+rest;", PAGE_HTML)
        self.assertIn("cell.classList.add('today')", PAGE_HTML)
        self.assertIn("var text=(time?time+' ':'')+String(task.summary||task.text||'未命名任务');", PAGE_HTML)
        self.assertIn("chip.textContent=text;", PAGE_HTML)
        self.assertIn("function openCalendarDay(cell,key,day)", PAGE_HTML)
        self.assertIn("function closeCalendarDetail()", PAGE_HTML)
        # 每一天的点击/键盘处理器必须绑定到本迭代的日期，否则所有格子都会打开最后一天（var 捕获事故）
        self.assertIn("for(let d=1;d<=days;d++){let key=dayKey(y,m,d);", PAGE_HTML)
        self.assertIn('id="calendar-detail"', PAGE_HTML)
        self.assertIn(
            ".calendar-event{display:block;margin-top:3px;padding:3px 4px;border-radius:3px;background:#e5f3ec;"
            "color:#1f4738;font-size:11px;line-height:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
            css,
        )
        self.assertIn(".calendar-day.today{", css)
        self.assertIn(".calendar-more{", css)
        event_rules = re.findall(r"\.calendar-event\{[^}]*\}", css)
        self.assertTrue(event_rules, "月历事项必须有生效的样式规则")
        # -webkit-box + line-clamp 会把 11px 字的下缘切掉，用户看到的是事项条底部锯齿
        for rule in event_rules:
            self.assertNotIn("-webkit-line-clamp", rule, "单行截断改用 nowrap + ellipsis，别再裁掉字形下缘")
            self.assertNotIn("display:-webkit-box", rule, "月历事项不用 -webkit-box 排版")
        self.assertTrue(any("text-overflow:ellipsis" in rule for rule in event_rules), "月历事项必须单行省略")

    def test_calendar_day_panel_toggles_shut_and_has_a_close_button(self) -> None:
        css = self._current_stylesheet()
        # 再点同一个日期格必须收起（此前一进来就 closeCalendarDetail() 再打开，等于永远关不掉）
        self.assertIn("if(cell.classList.contains('is-open')){closeCalendarDetail(); return;}", PAGE_HTML)
        self.assertIn("head.className='calendar-detail-head'", PAGE_HTML)
        self.assertIn("close.className='calendar-detail-close'", PAGE_HTML)
        self.assertIn("close.textContent='关闭'", PAGE_HTML)
        self.assertIn("close.addEventListener('click',function(){closeCalendarDetail(); cell.focus();})", PAGE_HTML)
        self.assertIn("calendarDetail.appendChild(head);", PAGE_HTML)
        self.assertNotIn("calendarDetail.appendChild(heading);", PAGE_HTML)
        self.assertIn(".calendar-detail-head{", css)
        self.assertIn(".calendar-detail-close{", css)
        self.assertIn("min-height:32px", css)

    def test_task_reflow_and_calendar_detail_animate_instead_of_jumping(self) -> None:
        """勾选待办后剩余卡片上移、月历当天详情展开/收起都必须有过渡（用户报的硬切）。"""
        css = self._current_stylesheet()
        # 卡片重排用 FLIP：重建前记文档坐标，重建后把卡片从旧位置平移回新位置再过渡
        self.assertIn("var beforeRects = Object.create(null);", PAGE_HTML)
        self.assertIn(
            "beforeRects[card.id] = {left: rect.left + window.scrollX, top: rect.top + window.scrollY, section: sectionKey(card)};",
            PAGE_HTML,
        )
        self.assertIn("if (!dx && !dy) return;", PAGE_HTML)
        # 换分组的卡片（例如刚完成、去「已完成」）不参与 FLIP，否则会横跨整页飞几千像素
        self.assertIn("if (!before || before.section !== sectionKey(card)) return;", PAGE_HTML)
        self.assertIn("return title ? title.textContent.trim() : '';", PAGE_HTML)
        self.assertIn("card.style.transition = 'transform 240ms cubic-bezier(.2, .8, .2, 1)';", PAGE_HTML)
        self.assertIn("card.addEventListener('transitionend', clearFlip, {once: true});", PAGE_HTML)
        # 过渡没被真正启动（帧被节流/降级）时也必须把内联样式收干净，否则残留 transform 会错位
        self.assertIn("window.setTimeout(clearFlip, 280);", PAGE_HTML)
        # 关动效/暂停时不退化成「瞬移」
        self.assertIn("if (!motionIsPaused()) {", PAGE_HTML)
        # 详情面板的空间也要过渡，否则收起时下面的内容瞬间跳上来
        self.assertIn("max-height:var(--detail-h,640px)", css)
        self.assertIn("calendarDetail.style.setProperty('--detail-h',calendarDetail.offsetHeight+'px');", PAGE_HTML)
        self.assertIn("calendarDetail.style.removeProperty('--detail-h');", PAGE_HTML)
        # 小数行高会把文字与圆角边框放到半像素上（用户报的「日程下端锯齿」）
        for needle in (
            ".calendar-detail{margin-top:10px;padding:10px 12px;border:1px solid #b8cbc1;border-radius:6px;"
            "background:#f7faf8;color:#263b32;font-size:13px;line-height:20px;overflow:hidden}",
            ".calendar-detail h3{margin:0;font-size:14px;line-height:20px}",
            ".calendar-day strong{font-size:13px;line-height:16px;font-variant-numeric:tabular-nums}",
            ".calendar-more{display:block;margin-top:3px;padding:2px 4px;color:#42554e;font-size:10px;"
            "line-height:14px;text-align:right}",
            ".calendar-note{margin:8px 0 0;color:#4b5f55;font-size:12px;line-height:16px}",
            "color:#4a6055;font-size:12px;line-height:16px;font-weight:650",
        ):
            self.assertIn(needle, css)

    def test_pinned_panel_can_be_resized_and_remembers_the_height(self) -> None:
        css = self._current_stylesheet()
        self.assertIn('id="pinned-resize"', PAGE_HTML)
        self.assertIn('role="separator"', PAGE_HTML)
        self.assertIn('aria-valuenow="280"', PAGE_HTML)
        self.assertIn("function initPinnedResize()", PAGE_HTML)
        self.assertIn("list.style.maxHeight=height+'px'", PAGE_HTML)
        self.assertIn("localStorage.setItem(storeKey,String(height))", PAGE_HTML)
        self.assertIn("localStorage.getItem(storeKey)", PAGE_HTML)
        self.assertIn("handle.addEventListener('keydown'", PAGE_HTML)
        self.assertIn("handle.addEventListener('pointermove'", PAGE_HTML)
        self.assertIn(".pinned-resize{display:flex", css)
        self.assertIn(".pinned-panel.is-resizing .pinned-resize{border-style:solid;border-color:#12695b}", css)

    def test_hosting_shortcut_and_onboarding_journey_are_wired(self) -> None:
        self.assertIn('id="host-state"', PAGE_HTML)
        self.assertIn('id="host-quick"', PAGE_HTML)
        self.assertIn('id="journey"', PAGE_HTML)
        self.assertIn('id="journey-steps"', PAGE_HTML)
        self.assertIn('id="journey-action"', PAGE_HTML)
        self.assertIn('id="journey-dismiss"', PAGE_HTML)
        self.assertIn("function fetchHostingStatus()", PAGE_HTML)
        self.assertIn("api('/api/hosting/'+desired", PAGE_HTML)
        self.assertIn("status.hosting_active", PAGE_HTML)
        self.assertIn("status.napcat_running", PAGE_HTML)
        self.assertIn("status.napcat_online", PAGE_HTML)
        self.assertIn("Number(status.groups_selected||0)>0", PAGE_HTML)
        self.assertIn("Array.isArray(info.calendars)&&info.calendars.length", PAGE_HTML)
        self.assertIn("localStorage.getItem('qq_digest_journey_hidden')", PAGE_HTML)
        self.assertIn("window.setInterval(fetchHostingStatus, 10000)", PAGE_HTML)
        self.assertIn("initPinnedResize();", PAGE_HTML)
        self.assertIn("initJourney();", PAGE_HTML)
        self.assertLess(PAGE_HTML.index('id="journey"'), PAGE_HTML.index('id="action-feedback"'))

    def test_calendar_month_turn_and_event_titles_are_explained(self) -> None:
        css = self._current_stylesheet()
        self.assertIn("@keyframes calendar-turn{from{opacity:0;transform:translateX(var(--turn-x,18px))}", css)
        self.assertIn("#calendar.calendar-turn{animation:calendar-turn 240ms var(--ease-out) both}", css)
        self.assertIn("root.style.setProperty('--turn-x',direction>0?'18px':'-18px')", PAGE_HTML)
        self.assertNotIn("animateSurface(root,direction*12)", PAGE_HTML)
        self.assertIn("chip.title=text+'（点日期格看当天全部事项）'", PAGE_HTML)
        self.assertIn("more.title='这天还有 '+rest+' 条，点日期格看当天全部'", PAGE_HTML)
        self.assertIn("每格最多显示 3 条（带截止时刻）", PAGE_HTML)

    def test_brand_click_and_connect_buttons_explain_themselves(self) -> None:
        # 页头标题不再是链接：没有首页链接，也没有「回到顶部 + 重新弹引导」的点击行为
        self.assertNotIn('id="brand-home"', PAGE_HTML)
        self.assertNotIn("回到顶部；重新显示「开始使用」引导", PAGE_HTML)
        self.assertNotIn("已回到顶部，并重新显示", PAGE_HTML)
        # logo 单独做成按钮：点一下（或键盘 Enter/Space）转一圈，只动 mark
        self.assertIn('id="brand-mark"', PAGE_HTML)
        self.assertIn('class="brand-mark-button"', PAGE_HTML)
        self.assertIn("@keyframes brand-spin{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}", self._current_stylesheet())
        self.assertIn("mark.classList.add('is-spinning')", PAGE_HTML)
        self.assertIn("localStorage.removeItem('qq_digest_journey_hidden')", PAGE_HTML)
        self.assertIn('class="connect-hint"', PAGE_HTML)
        self.assertIn('title="第一次用点这个：下载安装 NapCat、启动引擎并显示登录二维码"', PAGE_HTML)
        self.assertIn('title="只启动引擎并显示登录二维码；不会退出电脑版 QQ"', PAGE_HTML)
        self.assertIn('title="二维码过期或看不清时重新获取一张"', PAGE_HTML)
        self.assertIn(".connect-hint{margin:8px 0 0;color:#42554e;font-size:12px;line-height:1.6}", self._current_stylesheet())

    def test_right_rail_panels_report_week_actions_and_health(self) -> None:
        css = self._current_stylesheet()
        # 三块面板都在右栏（#connect 之后、</aside> 之前）
        connect_at = PAGE_HTML.index('id="connect"')
        for panel in ('id="rail-summary"', 'id="rail-actions"', 'id="rail-health"'):
            self.assertGreater(PAGE_HTML.index(panel), connect_at)
        self.assertLess(PAGE_HTML.index('id="rail-health"'), PAGE_HTML.index('</aside>'))
        # 本周小结：三个数字 + 七根柱，柱子要有无障碍名称
        self.assertIn('id="rail-bars" class="rail-bars" role="img" aria-label="本周每天到期的待办条数"', PAGE_HTML)
        self.assertIn("var stats = data.stats || {};", PAGE_HTML)
        self.assertIn("['rail-stat-open', stats.open]", PAGE_HTML)
        self.assertIn("['rail-stat-done', stats.done]", PAGE_HTML)
        self.assertIn("['rail-stat-overdue', stats.overdue]", PAGE_HTML)
        # 快捷操作：每个按钮都绑定到页面里已有的能力，不留摆设
        self.assertIn(
            "var bindings = [['rail-copy-today', railCopyToday], ['rail-export-ics', railExportIcs], "
            "['rail-show-qr', railShowSubscription], ['rail-motion', railToggleMotion], "
            "['rail-recheck', renderRailHealth], ['rail-settings', showSettingsTab]];",
            PAGE_HTML,
        )
        self.assertIn("navigator.clipboard.writeText(text)", PAGE_HTML)
        self.assertIn("document.execCommand('copy')", PAGE_HTML)
        self.assertIn("link.href = '/calendar.ics' + (token ? '?token=' + encodeURIComponent(token) : '')", PAGE_HTML)
        self.assertIn("setMotionPreference(motionPreferencePaused)", PAGE_HTML)
        # 服务自检行完全来自聚合接口，面板只发这一个请求。
        render_start = PAGE_HTML.index('function renderRailHealth()')
        render_end = PAGE_HTML.index('function initRailPanels()', render_start)
        render_health = PAGE_HTML[render_start:render_end]
        self.assertIn('id="rail-recheck"', PAGE_HTML)
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
        self.assertIn("settingsButton.onclick = showSettingsTab", PAGE_HTML)
        # 接线：渲染时刷新小结，初始化时绑定按钮
        self.assertRegex(PAGE_HTML, r"renderPinned\(data\);\s+renderRailSummary\(data\);")
        self.assertIn("initRailPanels();", PAGE_HTML)
        # 样式沿用既有 token，不引入新色
        for selector in (".rail-panel{", ".rail-bar.is-today .rail-bar-fill{", ".health-row[data-state=bad] .health-dot{"):
            self.assertIn(selector, css)
        # 「消息推送」那行会多出一个「去设置」按钮；按钮若落回第 1 列会把该列撑宽，标题被推右
        self.assertIn(".health-row .btn-small{grid-column:2 / -1", css)

    def test_subscription_qr_refresh_refetches_the_address(self) -> None:
        """订阅二维码的内容就是订阅地址；刷新必须重新取地址，并把「地址没变」说清楚。"""
        self.assertIn('id="sync-refresh" class="btn" type="button">刷新二维码</button>', PAGE_HTML)
        self.assertIn("function refreshSyncInfo(manual)", PAGE_HTML)
        self.assertIn("window.__nhRefreshSyncInfo = function () { refreshSyncInfo(false); };", PAGE_HTML)
        self.assertIn("if (window.__nhRefreshSyncInfo) window.__nhRefreshSyncInfo();", PAGE_HTML)
        self.assertIn("订阅地址没变，二维码内容与上次相同。", PAGE_HTML)
        self.assertIn("地址已更新，二维码已换新。", PAGE_HTML)
        # 同一 URL 的图片不会真正重新请求：带时间戳强制重取
        self.assertIn("encodeURIComponent(setupToken) + '&t=' + Date.now();", PAGE_HTML)

    def test_create_task_form_and_calendar_detail_transition_are_wired(self) -> None:
        css = self._current_stylesheet()
        # 新建待办：按钮 → 内联表单 → POST /api/tasks/create → 刷新列表
        self.assertIn(
            'id="rail-new-task" class="btn primary" type="button" aria-expanded="false" aria-controls="rail-new-form"',
            PAGE_HTML,
        )
        self.assertIn('<form id="rail-new-form" class="rail-new-form" hidden>', PAGE_HTML)
        self.assertIn('id="rail-new-summary" class="rail-new-input" type="text" maxlength="200"', PAGE_HTML)
        self.assertIn('id="rail-new-deadline" class="rail-new-input" type="datetime-local"', PAGE_HTML)
        self.assertIn('<button id="rail-new-save" class="btn primary" type="submit">保存</button>', PAGE_HTML)
        self.assertIn('<button id="rail-new-cancel" class="btn" type="button">取消</button>', PAGE_HTML)
        self.assertIn("api('/api/tasks/create', {", PAGE_HTML)
        self.assertIn("body: JSON.stringify({summary: summary, deadline: deadline})", PAGE_HTML)
        self.assertIn("return syncTasks();", PAGE_HTML)
        self.assertIn("railShowNewForm(document.getElementById('rail-new-form').hidden)", PAGE_HTML)
        self.assertIn("newForm.onsubmit = function (event) { event.preventDefault(); railCreateTask(); };", PAGE_HTML)
        self.assertIn("event.key === 'Escape'", PAGE_HTML)
        self.assertIn(".rail-new-form[hidden]{display:none}", css)
        # 月历当天详情：展开/收起都有过渡，且关键帧只动 opacity/transform（不重采样文字）
        for frame in ("calendar-detail-in", "calendar-detail-out"):
            self.assertIn("@keyframes " + frame + "{", css)
            body = re.search(r"@keyframes " + frame + r"\{(.*?)\}", css, re.S)
            self.assertIsNotNone(body)
            self.assertNotIn("box-shadow", body.group(1))
            self.assertNotIn("filter", body.group(1))
            self.assertIn("opacity", body.group(1))
            self.assertIn("transform", body.group(1))
        self.assertIn(".calendar-detail.is-opening{animation:calendar-detail-in 240ms var(--ease-in) forwards}", css)
        self.assertIn(".calendar-detail.is-closing{animation:calendar-detail-out 200ms var(--ease-out) forwards}", css)
        self.assertIn("calendarDetail.classList.add('is-opening')", PAGE_HTML)
        self.assertIn("calendarDetail.classList.add('is-closing')", PAGE_HTML)
        self.assertIn("calendarDetail.dataset.closing='1'", PAGE_HTML)
        self.assertIn("getComputedStyle(calendarDetail).animationName==='none'", PAGE_HTML)
        # 表单展开/收起同样要有过渡（max-height + opacity），且逐帧不动 box-shadow/filter
        for frame in ("rail-form-enter", "rail-form-exit"):
            self.assertIn("@keyframes " + frame + "{", css)
            body = re.search(r"@keyframes " + frame + r"\{(.*?)\}", css, re.S)
            self.assertIsNotNone(body)
            self.assertNotIn("box-shadow", body.group(1))
            self.assertNotIn("filter", body.group(1))
            self.assertIn("opacity", body.group(1))
            self.assertIn("max-height", body.group(1))
        self.assertIn(".rail-new-form[data-state=entering]{animation:rail-form-enter 220ms var(--ease-in) forwards}", css)
        self.assertIn(".rail-new-form[data-state=exiting]{animation:rail-form-exit 180ms var(--ease-out) forwards}", css)
        self.assertIn("form.dataset.state = 'entering';", PAGE_HTML)
        self.assertIn("getComputedStyle(form).animationName === 'none'", PAGE_HTML)
        self.assertIn("clearTimeout(Number(form.dataset.pending))", PAGE_HTML)


class TaskStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "tasks.sqlite3")

    def test_upsert_keeps_done_status(self) -> None:
        task_id = self.store.upsert_task(
            task_key="m1",
            summary="提交实验报告",
            category="action",
            deadline=iso(NOW + dt.timedelta(hours=9)),
        )
        self.assertGreater(task_id, 0)
        self.assertTrue(self.store.set_task_status(task_id, True))
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
        self.assertEqual([task["summary"] for task in grouped["today"]], ["today"])
        self.assertEqual([task["summary"] for task in grouped["week"]], ["week"])
        self.assertEqual([task["summary"] for task in grouped["later"]], ["later"])
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

    def test_candidate_is_separate_from_today(self) -> None:
        self.store.upsert_task(
            task_key="candidate",
            summary="可能要交报名表",
            category="action",
            deadline=iso(NOW + dt.timedelta(hours=1)),
            status="candidate",
            confidence=0.6,
        )
        grouped = group_tasks(self.store.list_tasks(), NOW)
        self.assertEqual(len(grouped["candidates"]), 1)
        self.assertEqual(grouped["today"], [])


class TaskApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "api.sqlite3")
        self.task_id = self.store.upsert_task(
            task_key="m1",
            summary="提交实验报告",
            category="action",
            deadline=iso(dt.datetime(2026, 9, 30, 18, 0)),
            groups=["测仪2602班群"],
            evidence="请各班班长今天18:00前提交实验报告",
        )
        self.settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "学院通知群"},
            web_host="127.0.0.1",
            web_port=0,
            web_token="secret",
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
            ("qq_live_digest.webapp.hosting.quit_user_qq",
             {"ok": True, "pids": [], "already_closed": True, "forced": False, "remaining": [], "error": None}),
            ("qq_live_digest.webapp.hosting.start_user_qq",
             {"ok": True, "pid": None, "already_running": True, "error": None}),
            ("qq_live_digest.webapp.hosting._processes", []),
            ("qq_live_digest.webapp.hosting._bridge", {"ok": True, "error": None}),
        ):
            patcher = mock.patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

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
        with mock.patch("qq_live_digest.webapp.now_local", return_value=NOW):
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
        with mock.patch("qq_live_digest.webapp.now_local", return_value=NOW):
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

    def test_selfcheck_api_returns_fixed_eight_rows(self) -> None:
        expected_keys = ["qq", "hosting", "dingtalk", "wechat", "chaoxing", "attachments", "calendar", "push"]
        expected_labels = ["QQ 登录", "引擎托管", "钉钉", "微信", "学习通 Cookie", "附件解析", "公网日历", "消息推送"]
        patches = (
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
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
                data = self._get("/api/selfcheck", "secret")
        self.assertTrue(data["ok"])
        self.assertEqual([row["key"] for row in data["items"]], expected_keys)
        self.assertEqual([row["label"] for row in data["items"]], expected_labels)
        self.assertTrue(all(set(row) == {"key", "label", "state", "value", "hint"} for row in data["items"]))
        self.assertTrue(all(row["state"] == "ok" for row in data["items"]))

    def test_selfcheck_api_explains_unselected_groups_and_missing_wechat(self) -> None:
        patches = (
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
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
                items = self._get("/api/selfcheck", "secret")["items"]
        by_key = {row["key"]: row for row in items}
        self.assertEqual(by_key["dingtalk"]["state"], "warn")
        self.assertEqual(by_key["dingtalk"]["value"], "已启用，但没勾群")
        self.assertIn("没勾群时监听不会启动", by_key["dingtalk"]["hint"])
        self.assertEqual(by_key["wechat"]["state"], "warn")
        self.assertEqual(by_key["wechat"]["value"], "未找到微信数据")
        self.assertIn("本机没找到微信", by_key["wechat"]["hint"])
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
        from qq_live_digest.webapp import _redact_secrets

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
        with mock.patch("qq_live_digest.webapp._tailscale_public_app_url", return_value=""), mock.patch(
            "qq_live_digest.webapp._lan_hosts",
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

    def test_pair_info_keeps_lan_first_and_includes_funnel_for_remote_access(self) -> None:
        public = "https://box.ts.net"
        with mock.patch("qq_live_digest.webapp._tailscale_public_app_url", return_value=public), mock.patch(
            "qq_live_digest.webapp._lan_hosts", return_value=["100.109.123.59", "192.168.1.5"]
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
        self.assertIn("同一个 Tailscale VPN", " ".join(info["steps"]))
        self.assertIn("Funnel", " ".join(info["steps"]))

    def test_pair_info_recommends_first_non_lan_candidate_when_no_lan_exists(self) -> None:
        with mock.patch("qq_live_digest.webapp._tailscale_public_app_url", return_value=""), mock.patch(
            "qq_live_digest.webapp._lan_hosts", return_value=["8.8.8.8"]
        ):
            info = self._get("/api/pair/info", token="secret")
        self.assertTrue(info["candidates"])
        self.assertEqual(info["candidates"][0]["kind"], "other")
        self.assertTrue(info["candidates"][0]["recommended"])
        self.assertEqual(info["base"], info["candidates"][0]["url"])

    def test_pair_host_kind_accepts_non_ip_text(self) -> None:
        with mock.patch("qq_live_digest.webapp._tailscale_public_app_url", return_value=""):
            self.assertEqual(webapp._pair_host_kind("不是IP的字符串"), "other")

    def test_pair_page_uses_recommended_candidate_with_base_fallback(self) -> None:
        self.assertIn("Array.isArray(data.candidates)", PAGE_HTML)
        self.assertIn("candidates.find(function (item) { return item.recommended; })", PAGE_HTML)
        self.assertIn("id=\"pair-candidate\"", PAGE_HTML)
        self.assertIn("candidateSelect.onchange", PAGE_HTML)
        self.assertIn("var pairUrl = (recommended && recommended.url) || data.base || '';", PAGE_HTML)
        self.assertIn("input.value = pairUrl;", PAGE_HTML)
        self.assertIn("encodeURIComponent(pairUrl)", PAGE_HTML)
        self.assertIn("默认选同一 Wi-Fi 下的局域网地址", PAGE_HTML)
        # 这句文案在 webapp.py 里出现两次（服务端错误、前端 JS 的 else 分支），
        # 只断言文案本身钉不牢：改掉前端那处、服务端那处还在，断言照样绿。
        # 连上它所在的那一整行 JS，才能确定钉的是前端那一处。
        self.assertIn("qrMessage.textContent = '没有检测到可供手机访问的地址。';", PAGE_HTML)

    def test_pair_decision_validates_the_code_and_pair_info_is_copyable(self) -> None:
        pairing.reset()
        self.addCleanup(pairing.reset)
        status, _, _ = self._raw_request("/api/pair/approve", method="POST", payload={"code": "123"})
        self.assertEqual(status, 400)
        with mock.patch("qq_live_digest.webapp._tailscale_public_app_url", return_value=""), mock.patch(
            "qq_live_digest.webapp._tailscale_executable", return_value=""
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
                mock.patch("qq_live_digest.webapp._tailscale_public_app_url", return_value=""),
                mock.patch("qq_live_digest.webapp._tailscale_executable", return_value=""),
                mock.patch("qq_live_digest.webapp._lan_hosts", return_value=["100.78.42.0"]),
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
                mock.patch("qq_live_digest.webapp._tailscale_public_app_url", return_value=""),
                mock.patch("qq_live_digest.webapp._tailscale_executable", return_value=""),
                mock.patch("qq_live_digest.webapp._lan_hosts", return_value=["100.78.42.0"]),
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
            self.assertIn("data.qr_error", PAGE_HTML)
            self.assertIn("qr.onerror", PAGE_HTML)
            self.assertIn("二维码加载失败", PAGE_HTML)
        finally:
            server.token = old_token

    def _open(self, request: urllib.request.Request, attempts: int = 3):
        """发起一次本机请求，对「连接被本机重置」做有限重试。

        Windows 回环连接偶发被本机重置（CI 上出现过 ConnectionAbortedError /
        WinError 10053），与新开一条连接无关，所以这里重试；请求内容与断言都不变。
        """
        error: Exception | None = None
        for _ in range(attempts):
            try:
                return urllib.request.urlopen(request, timeout=5)
            except (ConnectionAbortedError, ConnectionResetError) as caught:
                error = caught
                time.sleep(0.2)
        raise error  # type: ignore[misc]

    def _get(self, path: str, token: str = "") -> dict:
        headers = {"X-Token": token} if token else {}
        request = urllib.request.Request(self.base + path, headers=headers)
        with self._open(request) as response:
            return json.loads(response.read().decode("utf-8"))

    def _post(self, path: str, payload: dict, token: str = "secret") -> dict:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"X-Token": token, "Content-Type": "application/json"},
            method="POST",
        )
        with self._open(request) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_meta_exposes_update_channel_and_page_badge(self) -> None:
        self.assertEqual(self._get("/api/meta", "secret")["channel"], "")
        self.settings.update_channel = "beta"
        self.assertEqual(self._get("/api/meta", "secret")["channel"], "beta")
        request = urllib.request.Request(self.base + "/", headers={"X-Token": "secret"})
        with self._open(request) as response:
            body = response.read().decode("utf-8")
        self.assertIn("BETA 测试版", body)
        self.assertIn('class="channel-badge channel-badge-beta"', body)
        self.assertIn("--brand-ink:#B5481E", body)  # beta 的页头 logo 换成测试版琥珀色
        self.assertRegex(PAGE_HTML, r"\.channel-badge\s*\{[^}]*\}")
        # 通道标在副标题那一行，标题行保持干净
        self.assertIn('<span class="brand-sub"><small>群消息 · 待办与日历</small><!--CHANNEL_BADGE--></span>', PAGE_HTML)
        self.settings.update_channel = ""
        with self._open(request) as response:
            body = response.read().decode("utf-8")
        self.assertNotIn("BETA 测试版", body)
        self.assertNotIn("--brand-ink:#B5481E", body)  # 正式版仍是原本的绿色 logo（SVG 里只是 var() 的兜底，不算注入）

    def test_chaoxing_auto_cookie_success_enables_and_saves_cookie(self) -> None:
        read_result = {"ok": True, "cookie": "sid=test", "source": "edge", "attempts": []}
        with mock.patch(
            "qq_live_digest.webapp.browser_cookie.read_chaoxing_cookie",
            return_value=read_result,
        ), mock.patch("qq_live_digest.webapp.ChaoxingClient.my_courses", return_value=[]):
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
            "qq_live_digest.webapp.browser_cookie.read_chaoxing_cookie",
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
            "qq_live_digest.napcat_admin.restart",
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
        self.assertIn('id="restart-napcat"', PAGE_HTML)
        self.assertIn("function runRestart()", PAGE_HTML)
        self.assertIn("/api/napcat/restart", PAGE_HTML)
        self.assertIn("lastQrStamp=null;", PAGE_HTML)

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
        with mock.patch("qq_live_digest.napcat_admin.diagnose", return_value=report) as doctor:
            data = self._get("/api/napcat/doctor", token="secret")
        doctor.assert_called_once()
        self.assertFalse(data["ok"])
        self.assertIn("httpServer", data["broken"])
        self.assertIn("重启引擎", data["hint"])
        # 页面要在卡住时把断点显示出来，否则用户还是只看到「什么都没发生」
        self.assertIn("/api/napcat/doctor", PAGE_HTML)
        self.assertIn("当前卡在", PAGE_HTML)

    def test_manual_scan_routes_require_token_and_return_status_contract(self) -> None:
        for source in ("dingtalk", "wechat"):
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

    def test_scan_status_endpoints_expose_progress_shape(self) -> None:
        for source in ("dingtalk", "wechat"):
            setattr(self.settings, f"{source}_status", {
                "scan_state": "running",
                "scan_stage": "正在读取测试库",
                "scan_started_at": "2026-10-11 14:00:00",
                "scan_started_monotonic": time.monotonic() - 1,
            })
            patcher = mock.patch("qq_live_digest.webapp.wechat_locate_storage", return_value=None) if source == "wechat" else mock.patch("qq_live_digest.webapp.DingTalkLocalReader")
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
            self.assertIn("var %sScanTimer = null;" % name, PAGE_HTML, "%s 轮询句柄要先声明" % name)
            self.assertIn(
                "if (%sScanTimer !== null) { clearInterval(%sScanTimer); %sScanTimer = null; }" % (name, name, name),
                PAGE_HTML,
                "%s 轮询必须在设置页重建前清掉" % name,
            )
            self.assertIn(
                "%sScanTimer = setInterval(function(){updateScanStatus('%s');},1500);" % (name, name),
                PAGE_HTML,
                "%s 轮询的句柄要真的存进变量，否则 clearInterval 清的是 null" % name,
            )
        # 同一处轮询绑定时历史上被整行复制过两次（一个句柄追不住两个定时器）
        for name in ("dingtalk", "wechat"):
            self.assertEqual(
                PAGE_HTML.count("setInterval(function(){updateScanStatus('%s');},1500);" % name),
                1,
                "%s 轮询只能绑定一处" % name,
            )

    def test_wechat_groups_include_suggestions_and_preserve_existing_fields(self) -> None:
        self.settings.llm_enabled = False
        self.settings.wechat_group_whitelist = ("w1",)
        self.settings.wechat_group_aliases = {"w1": "学院通知"}
        self.store.insert_message(msg_id="w-sample", source="wechat", group_id="w1", group_name="课程通知群", content="本周课程安排", received_at=NOW)
        with mock.patch("qq_live_digest.webapp.wechat_locate_storage", return_value=Path(self.tmp.name)), mock.patch(
            "qq_live_digest.webapp.wechat_group_names", return_value={"w1": "课程通知群", "w2": "社团活动群"}
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
        with mock.patch("qq_live_digest.webapp.DingTalkLocalReader") as reader_type:
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
        with mock.patch("qq_live_digest.webapp.wechat_locate_storage", return_value=Path(self.tmp.name)), mock.patch(
            "qq_live_digest.webapp.wechat_group_names", return_value={"w1": "课程群"}
        ), mock.patch("qq_live_digest.group_suggest._llm", side_effect=classify):
            result = self._get("/api/wechat/groups", "secret")
        self.assertTrue(result["ok"])
        self.assertEqual(captured["samples"], {"w1": ["最近课程安排", "旧消息"]})
        self.assertEqual(result["groups"][0]["source"], "llm")

    def test_recommendation_failure_keeps_group_list_and_returns_human_fallback(self) -> None:
        self.settings.llm_enabled = False
        with mock.patch("qq_live_digest.webapp.wechat_locate_storage", return_value=Path(self.tmp.name)), mock.patch(
            "qq_live_digest.webapp.wechat_group_names", return_value={"w1": "课程通知群"}
        ), mock.patch("qq_live_digest.webapp.group_suggest.suggest_for_source", side_effect=RuntimeError("synthetic")):
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
        with mock.patch("qq_live_digest.webapp.announce.active") as active:
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

        with mock.patch("qq_live_digest.webapp.announce.active", side_effect=read_local_cache) as active:
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

        with mock.patch("qq_live_digest.webapp.announce.active", side_effect=active_for_channel) as active:
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

        with mock.patch("qq_live_digest.webapp.announce.active", return_value=stale) as active:
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

        with mock.patch("qq_live_digest.webapp.announce.active") as active:
            response = self._get("/api/announcements", "secret")
        active.assert_not_called()
        self.assertEqual(response["items"], fresh["items"])
        self.assertTrue(response["from_cache"])
        self.assertFalse(response["checking"])

    def test_announcements_return_cached_result_within_six_hours(self) -> None:
        data_dir = Path(self.tmp.name) / "announcement-throttle"
        self.settings.data_dir = data_dir
        checked_at = dt.datetime.now(dt.timezone.utc).isoformat()
        cached = {"ok": True, "items": [{"id": "cached", "title": "缓存公告", "seen_required": False}], "from_cache": False, "checked_at": checked_at, "error": ""}
        state_path = data_dir / "updates" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(json.dumps({"announce_checked_at": checked_at, "announce_response": cached, "other_state": 7}), encoding="utf-8")

        with mock.patch("qq_live_digest.webapp.announce.active") as active:
            result = self._get("/api/announcements", "secret")
            again = self._get("/api/announcements", "secret")
        active.assert_not_called()
        self.assertEqual(result["items"], cached["items"])
        self.assertTrue(result["from_cache"])
        self.assertEqual(again["items"], cached["items"])
        self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["other_state"], 7)
        self.assertEqual(webapp.ANNOUNCEMENT_REFRESH_SECONDS, 6 * 60 * 60)

    def test_announcement_refresh_failures_stay_http_200_and_are_not_shown_as_red_errors(self) -> None:
        for suffix, outcome in (("returned", {"ok": False, "items": [], "from_cache": False, "checked_at": "", "error": "网络不通"}), ("raised", RuntimeError("synthetic exception"))):
            with self.subTest(outcome=suffix):
                data_dir = Path(self.tmp.name) / ("announcement-error-" + suffix)
                self.settings.data_dir = data_dir
                active_effect = outcome if isinstance(outcome, BaseException) else lambda: outcome
                with mock.patch("qq_live_digest.webapp.announce.active", side_effect=active_effect) as active:
                    first_status, _, _ = self._raw_request("/api/announcements")
                    state = self._wait_for_announcement_state(data_dir)
                    status, _, body = self._raw_request("/api/announcements")
                    active.assert_called_once()
                    # 失败缓存只压 10 分钟：到期必须重试，否则一次失败会顶满 6 小时，
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
                self.assertIn("公告暂时不可用。", page.decode("utf-8"))
                self.assertNotIn("网络不通", page.decode("utf-8"))

    # ---- 设置 →「版本更新」（task-151 / task-152）----

    def _update_state_file(self, data_dir: Path) -> Path:
        return data_dir / "updates" / "state.json"

    def _seed_update_state(self, data_dir: Path, *, age_seconds: float, tag: str = "v9.9.9") -> Path:
        state_path = self._update_state_file(data_dir)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({
            "announce_checked_at": "2026-10-11T00:00:00Z",
            "announce_response": {"ok": True, "items": [], "from_cache": False, "checked_at": "2026-10-11T00:00:00Z", "error": ""},
            "update_checked_epoch": time.time() - age_seconds,
            "update_result": {
                "ok": True, "checked_at": "2026-10-11 12:00:00", "channel": "beta",
                "current": {"version": "1.0.0", "tag": "v1.0.0"},
                "latest": {"tag": tag, "version": tag[1:], "name": "群务台 " + tag, "notes": "", "notes_html": "",
                           "published_at": "2026-10-11T01:02:03Z", "asset_name": "QQ-Notice-Hub.zip", "size": 1048576,
                           "sha256": "ab" * 32, "download_url": "https://example.invalid/x.zip", "html_url": "https://example.invalid/rel"},
                "has_update": True, "error": "",
            },
        }, ensure_ascii=False), encoding="utf-8")
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
        with mock.patch("qq_live_digest.webapp.updater") as fake:
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
        state_path = self._seed_update_state(data_dir, age_seconds=60)
        with mock.patch("qq_live_digest.webapp.updater") as fake:
            fake.current_version.return_value = "1.0.0"
            payload = self._get("/api/update", "secret")
        fake.check.assert_not_called()
        self.assertFalse(payload["checking"])
        self.assertEqual(payload["latest"]["tag"], "v9.9.9")
        # 公告与更新共用 state.json：查更新不能把公告的键写丢
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertIn("announce_response", state)
        self.assertIn("update_result", state)

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
                state_path = self._seed_update_state(data_dir, age_seconds=30)
                self.settings.update_channel = "beta" if channel == "stable" else "stable"
                info = {
                    "ok": True, "checked_at": "2026-10-15 12:00:00", "channel": channel,
                    "current": {"version": "1.0.0", "tag": "v1.0.0"}, "latest": {},
                    "has_update": False, "error": "",
                }
                with mock.patch("qq_live_digest.webapp.updater") as fake:
                    fake.check.return_value = info
                    fake.current_version.return_value = "1.0.0"
                    fake.resolve_channel.side_effect = lambda value=None: value or "beta"
                    self.assertTrue(self._post("/api/settings", {"update_channel": channel})["ok"])
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    self.assertNotIn("update_result", state)
                    self.assertNotIn("update_checked_epoch", state)
                    self.assertIn("announce_response", state)
                    payload = self._get("/api/update", "secret")
                    self.assertTrue(payload["checking"])
                    for _ in range(200):
                        state = json.loads(state_path.read_text(encoding="utf-8"))
                        if (state.get("update_result") or {}).get("channel") == channel:
                            break
                        time.sleep(0.01)
                    self.assertEqual(state["update_result"]["channel"], channel)
                fake.check.assert_called_once_with(channel=channel, timeout=8.0)
                self.assertEqual(env_path.read_text(encoding="utf-8").strip(), "QQ_DIGEST_UPDATE_CHANNEL=" + channel)
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
        self.assertEqual(os.environ.get("QQ_DIGEST_UPDATE_CHANNEL"), "")
        self.assertEqual(env_path.read_text(encoding="utf-8"), "QQ_DIGEST_UPDATE_CHANNEL=\n")

    def test_stale_update_cache_triggers_background_check(self) -> None:
        data_dir = Path(self.tmp.name) / "update-stale"
        self.settings.data_dir = data_dir
        state_path = self._seed_update_state(data_dir, age_seconds=7 * 3600)
        info = {
            "ok": True, "checked_at": "2026-10-11 13:00:00", "channel": "beta",
            "current": {"version": "1.0.0", "tag": "v1.0.0"},
            "latest": {"tag": "v9.9.10", "version": "9.9.10", "name": "", "notes": "", "published_at": "",
                       "asset_name": "QQ-Notice-Hub.zip", "size": 1, "sha256": "", "download_url": "https://example.invalid/y.zip", "html_url": ""},
            "has_update": True, "error": "",
        }
        with mock.patch("qq_live_digest.webapp.updater") as fake:
            fake.check.return_value = info
            fake.current_version.return_value = "1.0.0"
            payload = self._get("/api/update", "secret")
            self.assertTrue(payload["checking"])
            for _ in range(200):
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if (state.get("update_result") or {}).get("latest", {}).get("tag") == "v9.9.10":
                    break
                time.sleep(0.01)
        self.assertEqual(state["update_result"]["latest"]["tag"], "v9.9.10")

    def test_update_current_version_falls_back_to_unknown(self) -> None:
        self.settings.data_dir = Path(self.tmp.name) / "update-old"
        with mock.patch("qq_live_digest.webapp.updater") as fake:
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
        with mock.patch("qq_live_digest.webapp.updater") as fake:
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
        with mock.patch("qq_live_digest.webapp.updater") as fake:
            fake.check.return_value = info
            payload = self._get("/api/update?check=1", "secret")
        self.assertFalse(payload["has_update"])
        self.assertIn("还没发布", payload["notice"])
        self.assertEqual(payload["latest"]["tag"], "v1.0.0")

    def test_update_download_refuses_without_a_known_release(self) -> None:
        self.settings.data_dir = Path(self.tmp.name) / "update-nodl"
        with mock.patch("qq_live_digest.webapp.updater") as fake:
            result = self._post("/api/update/download", {})
        self.assertFalse(result["ok"])
        self.assertFalse(result["started"])
        self.assertIn("检查更新", result["error"])
        fake.download.assert_not_called()

    def test_update_download_progress_then_done(self) -> None:
        data_dir = Path(self.tmp.name) / "update-dl"
        self.settings.data_dir = data_dir
        self._seed_update_state(data_dir, age_seconds=10)
        started = threading.Event()
        release = threading.Event()

        def slow_download(info, dest, *, timeout=0, on_progress=None):
            on_progress(5, 10)
            started.set()
            release.wait(5)
            archive = Path(dest) / "QQ-Notice-Hub-v9.9.9.zip"
            archive.write_bytes(b"zip")
            return {"ok": True, "path": str(archive), "bytes": 10, "sha256": "", "error": ""}

        with mock.patch("qq_live_digest.webapp.updater") as fake:
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
        with mock.patch("qq_live_digest.webapp.updater.download", side_effect=fast_download):
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
        with mock.patch("qq_live_digest.webapp.updater") as fake:
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

        with mock.patch("qq_live_digest.webapp.updater") as fake, mock.patch("qq_live_digest.webapp.threading.Timer", CapturedTimer):
            fake.apply_async.return_value = {"ok": True, "script": "C:\\Temp\\update.ps1", "error": ""}
            result = self._post("/api/update/apply", {})
        self.assertTrue(result["ok"])
        self.assertTrue(result["restarting"])
        args, kwargs = fake.apply_async.call_args
        self.assertEqual(Path(args[1]), archive)
        self.assertEqual(kwargs.get("data_dir"), data_dir)
        self.assertEqual(len(timers), 1)
        self.assertEqual(timers[0][0], 2.0)

    def test_update_page_copy_and_banner_are_present(self) -> None:
        self.assertIn("版本更新", PAGE_HTML)
        self.assertIn("下载更新包", PAGE_HTML)
        self.assertIn("检查更新", PAGE_HTML)
        self.assertIn("更新包下载好后不会自己装；点托盘『重启并更新』才会换上新版，你的数据和订阅不会丢", PAGE_HTML)
        self.assertIn("点右下角托盘图标 →『重启并更新』，约 10–30 秒后会自动换好并重开", PAGE_HTML)
        self.assertIn("已就绪，等待重启", PAGE_HTML)
        self.assertNotIn("下载完成，正在安装", PAGE_HTML)
        self.assertNotIn("api('/api/update/apply'", PAGE_HTML)
        self.assertIn("refreshUpdate(false);", PAGE_HTML)
        self.assertIn("pending", PAGE_HTML)
        self.assertIn('id="update-banner"', PAGE_HTML)
        self.assertIn("发现新版本", PAGE_HTML)
        self.assertIn("查看完整更新说明", PAGE_HTML)
        self.assertIn("applyButton.hidden = !showLatest || !!pending", PAGE_HTML)
        self.assertIn("pending ? '更新包已就绪", PAGE_HTML)
        self.assertIn("点托盘『重启并更新』", PAGE_HTML)
        self.assertIn('rel="noopener noreferrer"', PAGE_HTML)
        # 页面里出现的 notes_html 是服务端预渲染好的，客户端不再自己解析 markdown
        self.assertIn("notes_html", PAGE_HTML)

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
        with mock.patch("qq_live_digest.webapp.announce.active", return_value=payload_in):
            payload = self._get("/api/announcements?refresh=1", "secret")
        self.assertEqual(payload["current_version"], "1.1.0-beta.1")
        self.assertEqual(len(payload["items"]), 1)
        self.assertFalse(payload["checking"])
        # 旧数据没有 current_version 时也要给个能显示的值（弹窗上写「当前版本 X」）
        fallback_dir = Path(self.tmp.name) / "ann-modal-fallback"
        self.settings.data_dir = fallback_dir
        payload_in.pop("current_version")
        with mock.patch("qq_live_digest.webapp.announce.active", return_value=payload_in):
            fallback = self._get("/api/announcements?refresh=1", "secret")
        self.assertTrue(fallback["current_version"])

    def test_announcement_popup_markup_and_safe_rendering(self) -> None:
        self.assertIn('id="announce-modal"', PAGE_HTML)
        self.assertIn("我知道了", PAGE_HTML)
        self.assertIn("announce-modal-version", PAGE_HTML)
        self.assertIn("announce-modal-link", PAGE_HTML)
        self.assertIn("nh_ann_seen", PAGE_HTML)
        self.assertIn("if (item.seen_required) return false;", PAGE_HTML)
        self.assertIn('rel="noopener noreferrer"', PAGE_HTML)
        modal_js = PAGE_HTML.split("function appendAnnounceText", 1)[1].split("function refreshAnnouncements", 1)[0]
        self.assertIn("createTextNode", modal_js)
        self.assertNotIn("innerHTML", modal_js)  # 正文来自网络：只许 textContent / createTextNode

    def test_announcement_ui_uses_text_and_required_items_cannot_be_dismissed(self) -> None:
        self.assertIn("detail.textContent = String(item.body", PAGE_HTML)
        self.assertIn("body.textContent = String(item.body", PAGE_HTML)
        self.assertIn("if (!item.seen_required)", PAGE_HTML)
        close_branch = PAGE_HTML.split("if (!item.seen_required) {", 1)[1].split("\n  }", 1)[0]
        self.assertIn("String(item.id)", close_branch)
        self.assertIn("localStorage.setItem('seen_announce_ids'", close_branch)
        self.assertIn("localStorage.getItem('seen_announce_ids')", PAGE_HTML)
        self.assertIn("localStorage.setItem('seen_announce_ids'", PAGE_HTML)
        self.assertIn("item.level || 'info'", PAGE_HTML)
        self.assertIn("'暂无公告。'", PAGE_HTML)
        self.assertNotIn("detail.innerHTML", PAGE_HTML)
        self.assertIn('id="announce-banner"', PAGE_HTML)
        self.assertIn("id = 'announce-list'", PAGE_HTML)

    def test_donation_qr_serves_jpeg_and_missing_is_404(self) -> None:
        asset = Path(__file__).resolve().parents[1] / "assets" / "donate-wechat.jpg"
        with mock.patch("qq_live_digest.webapp.RUNTIME_ROOT", asset.parent.parent):
            status, headers, body = self._raw_request("/api/donate/qr.png", token="")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "image/jpeg")
        self.assertEqual(len(body), asset.stat().st_size)
        with mock.patch("qq_live_digest.webapp.RUNTIME_ROOT", Path(self.tmp.name)):
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
        for text in ("支持开发者", "/api/donate/qr.png", "欢迎使用群务台", "我已阅读并理解", "确认失败，请重试", "/api/onboarding", 'id="onboarding-dismiss"', "稍后再说", 'id="onboarding-error"', "dismiss.onclick=closeOnboarding"):
            self.assertIn(text, PAGE_HTML)
        self.assertIn("本软件新增部分按 PolyForm Noncommercial 1.0.0 许可", PAGE_HTML)
        self.assertIn("商用需授权，请联系 jkl2755908073@qq.com", PAGE_HTML)
        self.assertIn("上游 wc985732-lang/qq-live-digest 的部分仍适用 MIT，详情见 NOTICE", PAGE_HTML)
        self.assertNotIn("本软件按 MIT 许可", PAGE_HTML)

    def test_requires_token(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as context:
            self._get("/api/tasks")
        self.assertEqual(context.exception.code, 401)

    def test_list_tasks_and_complete(self) -> None:
        with mock.patch("qq_live_digest.webapp.now_local", return_value=NOW):
            data = self._get("/api/tasks", token="secret")
            self.assertEqual(len(data["today"]), 1)
            self.assertEqual(data["today"][0]["summary"], "提交实验报告")
            self.assertIn("今天", data["today"][0]["deadline_text"])

            payload = json.dumps({"done": True}).encode("utf-8")
            request = urllib.request.Request(
                f"{self.base}/api/tasks/{self.task_id}",
                data=payload,
                headers={"X-Token": "secret", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                result = json.loads(response.read().decode("utf-8"))
            self.assertTrue(result["ok"])

            data = self._get("/api/tasks", token="secret")
            self.assertEqual(len(data["done"]), 1)
            self.assertEqual(len(data["today"]), 0)

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

        with mock.patch("qq_live_digest.webapp.now_local", return_value=NOW):
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
        with mock.patch("qq_live_digest.webapp.now_local", return_value=NOW):
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

        with mock.patch("qq_live_digest.webapp.now_local", return_value=NOW):
            data = self._get("/api/tasks", token="secret")
        self.assertEqual([item["id"] for item in data["pinned"]], [pinned_id])
        self.assertTrue(data["pinned"][0]["pinned"])
        self.assertNotIn(other_id, [item["id"] for item in data["pinned"]])
        self.assertFalse(next(item for item in data["today"] + data["week"] + data["later"] if item["id"] == other_id)["pinned"])

        status, _, body = self._raw_request("/api/tasks/pin", "POST", {"task_id": str(pinned_id), "pinned": False})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["pinned"])
        with mock.patch("qq_live_digest.webapp.now_local", return_value=NOW):
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

        with mock.patch("qq_live_digest.webapp.now_local", return_value=NOW):
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
        with mock.patch("qq_live_digest.webapp._lan_ipv4", return_value="192.168.8.10"), mock.patch("qq_live_digest.webapp._tailscale_calendar_url", return_value=""):
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

    def test_sync_info_prefers_public_address_and_keeps_lan_alternate(self) -> None:
        port = self.server.server.server_address[1]
        with mock.patch("qq_live_digest.webapp._lan_ipv4", return_value="100.78.7.108"), mock.patch(
            "qq_live_digest.webapp._tailscale_calendar_url", return_value="https://demo.tail1234.ts.net/notice.ics"
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
                "Web": {
                    "demo-machine.demo-tailnet.ts.net:443": {
                        "Handlers": {
                            "/": {"Proxy": "http://127.0.0.1:8787"},
                            "/notice.ics": {"Proxy": "http://127.0.0.1:8766/calendar.ics"},
                        }
                    }
                }
            }
        )
        with mock.patch("qq_live_digest.webapp.shutil.which", return_value=__file__), mock.patch(
            "qq_live_digest.webapp.subprocess.run", return_value=mock.Mock(stdout=serve)
        ) as run:
            self.assertEqual(webapp._tailscale_calendar_url(8766), "https://demo-machine.demo-tailnet.ts.net/notice.ics")
            self.assertEqual(webapp._tailscale_calendar_url(9999), "")
            for call in run.call_args_list:
                self.assertTrue(call.kwargs["creationflags"] & 0x08000000)

    def test_sync_info_reports_unavailable_lan_address_as_json(self) -> None:
        with mock.patch("qq_live_digest.webapp._lan_hosts", return_value=[]), mock.patch("qq_live_digest.webapp._tailscale_calendar_url", return_value=""):
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
        with mock.patch("qq_live_digest.webapp._lan_ipv4") as lan_probe:
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
            deadline=iso(NOW + dt.timedelta(hours=3)),
            groups=["班级群"],
            evidence="记得看一下报名表",
            status="candidate",
            confidence=0.66,
            classification_reason="可能出现弱行动词",
        )
        with mock.patch("qq_live_digest.webapp.now_local", return_value=NOW):
            data = self._get("/api/tasks", token="secret")
        self.assertEqual(len(data["candidates"]), 1)
        self.assertEqual(data["candidates"][0]["id"], candidate_id)
        self.assertIn("把握", data["candidates"][0]["confidence_text"])
        self.assertIn("弱行动词", data["candidates"][0]["confidence_reason"])

        payload = json.dumps({"action": "confirm"}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base}/api/tasks/{candidate_id}",
            data=payload,
            headers={"X-Token": "secret", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            result = json.loads(response.read().decode("utf-8"))
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
        self.assertIn("群消息待办", body)
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
        for path, content_type in (
            ("/manifest.webmanifest", "application/manifest+json"),
            ("/icon.svg", "image/svg+xml"),
            ("/sw.js", "text/javascript"),
        ):
            with urllib.request.urlopen(self.base + path, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(content_type, response.headers.get_content_type())
                self.assertTrue(response.read())

    def test_brand_icons_use_single_green_palette(self) -> None:
        from qq_live_digest.webapp import ICON_SVG

        for legacy in ("1B2A6B", "F59F00", "4f7cff", "8b5cf6", "3B5BDB"):
            self.assertNotIn(legacy, PAGE_HTML)
            self.assertNotIn(legacy, ICON_SVG)
        self.assertIn("%23173b34", PAGE_HTML)
        self.assertIn("#12695b", ICON_SVG)

    def test_task_correction_records_feedback(self) -> None:
        payload = json.dumps({
            "action": "correct", "correction": "category", "value": "academic"
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base}/api/tasks/{self.task_id}",
            data=payload,
            headers={"X-Token": "secret", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            result = json.loads(response.read().decode("utf-8"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "correct")
        task = self.store.get_task(self.task_id)
        assert task is not None
        self.assertEqual(task["category"], "academic")
        events = self.store.list_task_events(self.task_id)
        self.assertIn("corrected", [item["event"] for item in events])


    def test_snooze_defaults_to_next_morning(self) -> None:
        payload = json.dumps({"action": "snooze"}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base}/api/tasks/{self.task_id}",
            data=payload,
            headers={"X-Token": "secret", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            result = json.loads(response.read().decode("utf-8"))
        self.assertTrue(result["ok"])
        task = self.store.get_task(self.task_id)
        assert task is not None
        parsed = dt.datetime.fromisoformat(task["snooze_until"])
        self.assertEqual((parsed.hour, parsed.minute), (7, 30))
        self.assertEqual(parsed.date(), dt.date.today() + dt.timedelta(days=1))

    def test_duplicate_correction_merges_source_groups(self) -> None:
        copy_id = self.store.upsert_task(
            task_key="m2",
            summary="提交实验报告",
            category="action",
            groups=["班级闲聊群"],
        )
        payload = json.dumps({"action": "correct", "correction": "duplicate"}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base}/api/tasks/{copy_id}",
            data=payload,
            headers={"X-Token": "secret", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertTrue(json.loads(response.read().decode("utf-8"))["ok"])
        original = self.store.get_task(self.task_id)
        assert original is not None
        self.assertIn("班级闲聊群", original["groups"])
        copy = self.store.get_task(copy_id)
        assert copy is not None
        self.assertEqual(copy["duplicate_of"], self.task_id)
        self.assertEqual(copy["duplicate_summary"], "提交实验报告")

    def test_napcat_status_unreachable_returns_error_json(self) -> None:
        # running_pids 未打桩时会真去列进程（本机一次 ≈4.4 s），客户端 5 s 超时就会偶发变红。
        with mock.patch("qq_live_digest.webapp._Handler._napcat", return_value={"ok": False, "error": "connection refused"}), \
             mock.patch("qq_live_digest.webapp.napcat_admin.running_pids", return_value=[]):
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
        self.assertFalse(webapp._napcat_port_accepts("http://127.0.0.1:%d" % port))
        self.assertTrue(webapp._napcat_port_accepts("http://127.0.0.1"))  # 没端口可探就别拦

    def test_napcat_status_carries_qr_stamp(self) -> None:
        status = {"ok": True, "data": {"online": True, "good": True, "user_id": 7}}
        login = {"ok": True, "data": {"user_id": 7, "nickname": "me"}}

        def fake(action):
            return status if action == "get_status" else login

        with mock.patch("qq_live_digest.webapp._Handler._napcat", side_effect=fake), mock.patch("qq_live_digest.webapp.napcat_admin.qr_stamp", return_value="111-222"), mock.patch("qq_live_digest.webapp.napcat_admin.detect_boot", return_value=None):
            data = self._get("/api/napcat/status", token="secret")
        self.assertTrue(data["ok"])
        self.assertTrue(data["online"])
        self.assertEqual(data["qr_stamp"], "111-222")

    def test_napcat_status_reports_a_running_engine_while_waiting_for_login(self) -> None:
        """端口要等扫码登录成功才监听，所以「连不上」未必是故障。

        引擎进程还在时，状态里要带 napcat_running，页面才能说「正在等你扫码」，
        而不是让用户以为坏了、去反复点「启动并登录」（那正是二维码风暴的起因）。
        """
        with mock.patch("qq_live_digest.webapp._Handler._napcat", return_value={"ok": False, "error": "NapCat 没在运行（连不上 http://127.0.0.1:3001）"}), \
             mock.patch("qq_live_digest.webapp.napcat_admin.qr_stamp", return_value="111-222"), \
             mock.patch("qq_live_digest.webapp.napcat_admin.detect_boot", return_value=None), \
             mock.patch("qq_live_digest.webapp.napcat_admin.running_pids", return_value=[4242]) as pids:
            data = self._get("/api/napcat/status", token="secret")
        pids.assert_called_once()
        self.assertFalse(data["ok"])
        self.assertTrue(data["napcat_running"])
        self.assertEqual(data["qr_stamp"], "111-222")
        self.assertIn("var waiting = !!(e && e.napcatRunning);", PAGE_HTML)
        self.assertIn("NapCat 正在等你用手机扫码登录", PAGE_HTML)
        self.assertIn("先扫码登录 QQ，登录成功后会自动读取群列表。", PAGE_HTML)

    def test_napcat_status_reports_no_engine_when_none_is_running(self) -> None:
        with mock.patch("qq_live_digest.webapp._Handler._napcat", return_value={"ok": False, "error": "NapCat 没在运行"}), \
             mock.patch("qq_live_digest.webapp.napcat_admin.qr_stamp", return_value=""), \
             mock.patch("qq_live_digest.webapp.napcat_admin.detect_boot", return_value=None), \
             mock.patch("qq_live_digest.webapp.napcat_admin.running_pids", return_value=[]):
            data = self._get("/api/napcat/status", token="secret")
        self.assertFalse(data["ok"])
        self.assertFalse(data["napcat_running"])

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
        with mock.patch("qq_live_digest.webapp._Handler._napcat", return_value=result):
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
        expected = b"OTHER=keep\r\nQQ_DIGEST_GROUPS=123\nQQ_DIGEST_GROUP_ALIASES=123=" + "通知群".encode("utf-8") + b"\r\nTAIL=\xe4\xb8\xad\n"
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
        self.assertIn(b"QQ_DIGEST_GROUPS=111,222", written)
        self.assertIn("QQ_DIGEST_GROUP_ALIASES=111=通知群".encode("utf-8"), written)
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
        self.assertRegex(page, r"image\.src\s*=\s*'/api/napcat/qrcode\?token='\s*\+\s*encodeURIComponent\(setupToken\)")
        self.assertIn("image.onload = function ()", page)
        self.assertIn("image.onerror = function ()", page)
        self.assertIn("二维码暂不可用", page)

    def test_group_list_loading_is_connection_gated_and_reports_failure(self) -> None:
        request = urllib.request.Request(f"{self.base}/setup?token=secret")
        with urllib.request.urlopen(request, timeout=5) as response:
            page = response.read().decode("utf-8")
        check_start = page.index("function checkSetup()")
        check_setup = page[check_start:page.index("function installNapcat", check_start)]
        groups_start = page.index("function loadGroups()")
        group_loader = page[groups_start:page.index("function run()", groups_start)]
        # 拒绝响应时要把 napcat_running 带在错误上抛出去：catch 靠它区分「引擎没跑」
        # 和「引擎在跑、只是还在等扫码」——后者不该报成故障。
        self.assertIn("if(!x.ok){var notReady=Error(x.error||'连接不可用');notReady.napcatRunning=!!x.napcat_running;throw notReady;}", check_setup)
        self.assertIn("连接检查失败：", check_setup)
        self.assertIn("群列表暂不可用：", check_setup)
        self.assertIn("loadGroups();", check_setup)
        self.assertIn("qrWasVisible = true;", check_setup)
        engine_note = check_setup.index("typeof x.napcat_installed!=='undefined'")
        rejected_response = check_setup.index("if(!x.ok){var notReady=Error(")
        self.assertLess(engine_note, rejected_response)
        self.assertIn("document.getElementById('napcat-engine')", check_setup)
        self.assertIn("QQ_DIGEST_NAPCAT_DIR", check_setup)
        self.assertIn("if(!r.ok)throw Error", check_setup)
        self.assertNotIn("install-box').hidden", check_setup)
        self.assertRegex(group_loader, r"if\s*\(\s*!\w+\.ok\s*\)\s*throw\s+Error\(")
        self.assertRegex(group_loader, r"\.catch\(function\(e\)\{[^}]*message\.textContent='群列表读取失败：'\+e\.message")
        self.assertIn("groups-connect-link", group_loader)
        self.assertNotRegex(page, r"setInterval\( *loadGroups")

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
        run_start = page.index("function run()")
        run_end = page.index("document.getElementById('go').onclick=run", run_start)
        run_path = page[run_start:run_end]
        self.assertIn("setupApi('/api/napcat/launch'", run_path)
        self.assertIn("正在启动 NapCat", run_path)
        self.assertIn("启动失败：", run_path)
        self.assertIn("document.getElementById('go').onclick=run", page)

    def test_napcat_launch_passes_uin_and_empty_as_none(self) -> None:
        expected = {"ok": True, "already_running": False, "pid": 12, "command": [], "profile_dir": "x", "boot": {}, "error": None}
        fake = mock.Mock()
        fake.launch.return_value = expected
        with mock.patch("qq_live_digest.webapp.napcat_admin", fake):
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
        with mock.patch("qq_live_digest.webapp.napcat_admin", fake):
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
        self.assertIn("QQ_DIGEST_HOSTING_QUIT_QQ=0", raw); self.assertIn("KEEP=yes", raw)

    def test_task71_unauthorized_routes_have_no_side_effects(self) -> None:
        with (
            mock.patch("qq_live_digest.webapp.catchup") as catchup_module,
            mock.patch("qq_live_digest.webapp.inbox") as inbox_module,
            mock.patch.object(self.store, "inbox_items") as inbox_items,
            mock.patch.object(self.store, "inbox_counts") as inbox_counts,
        ):
            requests = [
                ("GET", "/api/history/floor?group_id=g1", None),
                ("GET", "/api/history/fetch/status", None),
                ("GET", "/api/inbox/classify/status", None),
                ("GET", "/api/inbox", None),
                ("GET", "/api/inbox/image?msg_id=onebot%3Ag1%3Aattach%3A0123456789abcdef", None),
                ("POST", "/api/history/fetch", {"groups": ["g1"]}),
                ("POST", "/api/history/fetch/cancel", {}),
                ("POST", "/api/inbox/classify", {"groups": ["g1"]}),
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

    def test_history_floor_validates_and_returns_local_cache_boundary(self) -> None:
        fake = mock.Mock()
        fake.available_floor.return_value = {"floor_ts": "2026-10-01T08:00:00", "total_seen": 42, "error": ""}
        with mock.patch("qq_live_digest.webapp.catchup", fake):
            result = self._get("/api/history/floor?group_id=g1", "secret")
            self.assertEqual(result, {"ok": True, "floor_ts": "2026-10-01T08:00:00", "total_seen": 42, "error": ""})
            fake.available_floor.assert_called_once_with(self.settings, "g1")
            request = urllib.request.Request(self.base + "/api/history/floor?group_id=", headers={"X-Token": "secret"})
            with self.assertRaises(urllib.error.HTTPError) as context:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(context.exception.code, 400)
            context.exception.close()

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

        with mock.patch("qq_live_digest.webapp.catchup.backfill_range", side_effect=backfill) as backfill_mock, mock.patch("qq_live_digest.webapp.inbox.classify_messages", side_effect=classify) as classify_mock:
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
            mock.patch("qq_live_digest.webapp.catchup.backfill_range", side_effect=backfill),
            mock.patch("qq_live_digest.webapp.inbox.classify_messages", side_effect=classify),
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

        status, _, _ = self._raw_request("/api/inbox/image?msg_id=../../secret", token="secret")
        self.assertEqual(status, 404)
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

    def test_inbox_promoted_filter_uses_store_flag_and_counts(self) -> None:
        row = {"msg_id": "m-promoted", "verdict": "notice", "task_id": 12, "content": "测试通知"}
        with mock.patch.object(self.store, "inbox_items", return_value=[row]) as items, mock.patch.object(self.store, "inbox_counts", return_value={"notice": 4, "suspect": 2, "noise": 1, "promoted": 1}):
            result = self._get("/api/inbox?verdict=promoted", "secret")
        self.assertTrue(result["ok"])
        self.assertEqual(result["items"], [row])
        self.assertEqual(result["total"], 1)
        self.assertIsNone(items.call_args.kwargs["verdict"])
        self.assertIs(items.call_args.kwargs["promoted"], True)

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
        self.assertEqual(env_path.read_text(encoding="utf-8"), "QQ_DIGEST_CATCHUP_ENABLED=1\nQQ_DIGEST_CATCHUP_HOURS=72\nKEEP=yes\n")

    def test_push_settings_form_is_present_and_prefilled_from_meta(self) -> None:
        for marker in (
            'id="push-settings"',
            'id="push-wxpusher_app_token"',
            'id="push-wxpusher_uids"',
            'id="push-wxpusher_topic_ids"',
            'id="push-serverchan_keys"',
            'id="push-pushplus_tokens"',
            'id="push-webhook_urls"',
            'id="push-save"',
            'id="push-clear"',
            "var pushFields=['wxpusher_app_token','wxpusher_uids','wxpusher_topic_ids','serverchan_keys','pushplus_tokens','webhook_urls'];",
            "function applyPushState(state)",
            "function savePushSettings(all)",
            "applyPushState(data.push);",
            "api('/api/settings',{method:'POST'",
        ):
            self.assertIn(marker, PAGE_HTML)
        # 表单只回掩码占位，不把密钥写进页面
        self.assertIn("input.placeholder=(item&&item.set)?('已配置：'+item.masked.join('、')+'（留空不修改）'):'未配置';", PAGE_HTML)

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
        self.assertIn("QQ_DIGEST_WEBHOOKS=https://example.com/hook/abcd", text)
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
        self.assertNotIn("QQ_DIGEST_WEBHOOKS=https://example.com/hook/abcd", env_path.read_text(encoding="utf-8"))

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
            'id="llm-api-key"',
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
            self.assertIn(marker, PAGE_HTML)
        # 新手引导：每个通道都要有「一步一步怎么做」，说明里不出现接口术语
        self.assertIn("提醒怎么送到手机", PAGE_HTML)
        self.assertIn("不配也能用", PAGE_HTML)
        self.assertIn("<ol class=\"push-steps\">", PAGE_HTML)
        self.assertIn("data-help=\"wxpusher\"", PAGE_HTML)
        self.assertIn("data-help=\"serverchan\"", PAGE_HTML)
        self.assertIn("data-help=\"pushplus\"", PAGE_HTML)
        for jargon in ("OpenAI 兼容", "App Token", "SendKey", "Topic ID", "compatible-mode"):
            self.assertNotIn(jargon, PAGE_HTML)

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
        self.assertIn("QQ_DIGEST_LLM=1", text)
        self.assertIn("QQ_DIGEST_LLM_ENDPOINT=https://api.deepseek.com/chat/completions", text)
        self.assertIn("QQ_DIGEST_LLM_MODEL=deepseek-flash", text)
        self.assertIn("QQ_DIGEST_LLM_API_KEY=sk-test-abcd1234", text)
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
        self.assertIn("QQ_DIGEST_LLM=0", env_path.read_text(encoding="utf-8"))

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
        self.assertIn("启动时自动回溯最近 N 天", html)
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
        with mock.patch("qq_live_digest.webapp.hosting", fake):
            self.assertTrue(self._get("/api/hosting/status", "secret")["ok"])
            for endpoint in ("/api/hosting/start", "/api/hosting/stop"):
                request = urllib.request.Request(self.base + endpoint, data=b"{}", headers={"X-Token":"secret", "Content-Type":"application/json"}, method="POST")
                with urllib.request.urlopen(request, timeout=5) as response: self.assertTrue(json.loads(response.read())["ok"])
        fake.start.assert_called_once_with(self.server.settings, uin=None); fake.stop.assert_called_once_with(self.server.settings)

    def test_hosting_module_failure_returns_json(self) -> None:
        with mock.patch("qq_live_digest.webapp.hosting", None):
            data = self._get("/api/hosting/status", "secret")
        self.assertFalse(data["ok"])

    def test_dashboard_contains_login_choices_and_explanation(self) -> None:
        request = urllib.request.Request(f"{self.base}/?token=secret")
        with urllib.request.urlopen(request, timeout=5) as response:
            page = response.read().decode("utf-8")
        for text in ("扫码登录", "用指定 QQ 号快速登录", "独立资料目录", "同一 QQ 号不能同时登录两台电脑", "QQ 小号接收通知"):
            self.assertIn(text, page)
        self.assertIn('id="auto-setup"', page)
        self.assertIn("自动下载并安装 NapCat", page)
        self.assertIn("第一次使用且还没安装 NapCat：先点「自动下载并安装 NapCat」，安装后再点「启动并登录」", page)
        self.assertIn("已有 NapCat 引擎时点「一键接入」完成接入", page)
        self.assertNotIn("第一次用：点「一键接入」——它会装好引擎", page)
        for text in ("不会动你平时使用的电脑版 QQ", "移除该目录即可卸载"):
            self.assertIn(text, page)

    def test_auto_setup_preserves_steps_restart_and_error_feedback(self) -> None:
        request = urllib.request.Request(f"{self.base}/?token=secret")
        with urllib.request.urlopen(request, timeout=5) as response:
            page = response.read().decode("utf-8")
        start = page.index("function runAutoSetup()")
        auto_setup = page[start:page.index("function run()", start)]
        self.assertIn("setupApi('/api/napcat/autosetup'", auto_setup)
        self.assertIn("method:'POST'", auto_setup)
        self.assertIn("result.steps", auto_setup)
        self.assertIn("result.waiting_for_login", auto_setup)
        self.assertIn("请用手机 QQ 扫描下面的二维码登录", auto_setup)
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
        self.assertIn("var lastQrStamp = null;", page)
        self.assertIn("function refreshQrIfStale(stamp)", page)
        # 状态里带回二维码标记，页面只在标记变化时才重取图片，不再每 5 秒闪一次
        self.assertIn("refreshQrIfStale(x.qr_stamp);", page)
        self.assertIn("if (box && !box.hidden) { checkSetup(); } }, 5000);", page)
        self.assertNotIn("if (box && !box.hidden) { refreshQr(); checkSetup(); }", page)
        # 扫码成功后自动刷新一次整页，并用 sessionStorage 标记防止刷新循环
        self.assertIn("sessionStorage.setItem('qq_login_reloaded','1'); window.location.reload();", page)
        self.assertIn("sessionStorage.removeItem('qq_login_reloaded');", page)

    def test_napcat_install_requires_token(self) -> None:
        request = urllib.request.Request(f"{self.base}/api/napcat/install", data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(context.exception.code, 401)

    def test_napcat_install_passes_through_result(self) -> None:
        expected = {"ok": True, "installed": True, "already_installed": False, "root": "x", "version": "v4.18.33", "bytes": 12, "error": None}
        fake = mock.Mock(); fake.install.return_value = expected
        with mock.patch("qq_live_digest.webapp.napcat_admin", fake):
            request = urllib.request.Request(f"{self.base}/api/napcat/install", data=b"{}", headers={"X-Token":"secret", "Content-Type":"application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(json.loads(response.read()), expected)
        fake.install.assert_called_once_with(self.server.settings)

    def test_napcat_autosetup_passes_through_admin_result(self) -> None:
        expected = {
            "ok": True,
            "steps": [{"name": "找到 NapCat", "ok": True, "detail": "F:\\NapCat"}],
            "qrcode_path": "C:\\tmp\\qrcode.png",
            "uin": "10001",
            "applied_via": "webui",
            "restart_required": False,
            "error": None,
        }
        fake = mock.Mock()
        fake.auto_setup.return_value = expected
        with mock.patch("qq_live_digest.webapp.napcat_admin", fake):
            request = urllib.request.Request(f"{self.base}/api/napcat/autosetup", data=b"{}", headers={"X-Token": "secret", "Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
        self.assertEqual(data, expected)
        fake.auto_setup.assert_called_once_with(self.server.settings)

    def test_napcat_autosetup_without_module_reports_error(self) -> None:
        with mock.patch("qq_live_digest.webapp.napcat_admin", None):
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
        with mock.patch("qq_live_digest.webapp.napcat_admin", fake):
            request = urllib.request.Request(f"{self.base}/api/napcat/qrcode?token=secret")
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), png)

if __name__ == "__main__":
    unittest.main()
