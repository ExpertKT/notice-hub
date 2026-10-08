from __future__ import annotations

import datetime as dt
import io
import json
import re
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
        self.assertIn("animation:bg-flow-a 54s", css)
        self.assertIn("animation:bg-flow-b 72s", css)
        for name in ("bg-flow-a", "bg-flow-b"):
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
        # 幅度契约：流动感必须肉眼可见（用户两次追问「背景要有流动动画」）。第一版 alpha 只有 .12-.17、
        # 位移只有 ±2-4%，实测逐像素差 1.08-1.94，太弱；这里锁住下限，防止以后又被改回去。
        for alpha in (".22", ".18", ".19", ".16"):
            self.assertIn(alpha + ")", css)
        flow_a = css.split("@keyframes bg-flow-a{", 1)[1].split("}}", 1)[0]
        flow_b = css.split("@keyframes bg-flow-b{", 1)[1].split("}}", 1)[0]
        self.assertIn("translate3d(-4%,-3%,0)", flow_a)
        self.assertIn("translate3d(5%,-4%,0)", flow_a)
        self.assertIn("translate3d(4%,3%,0)", flow_b)
        self.assertIn("translate3d(2%,5%,0)", flow_b)

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
        self.assertIn("(data.week || []).concat(data.later || [])", carousel)
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
            ".calendar-event{display:-webkit-box;margin-top:3px;padding:3px 4px;border-radius:3px;background:#e5f3ec;"
            "color:#1f4738;font-size:11px;line-height:1.3;overflow:hidden;overflow-wrap:anywhere;-webkit-line-clamp:1;-webkit-box-orient:vertical}",
            css,
        )
        self.assertIn(".calendar-day.today{", css)
        self.assertIn(".calendar-more{", css)
        event_rules = re.findall(r"\.calendar-event\{[^}]*\}", css)
        self.assertTrue(event_rules, "月历事项必须有生效的样式规则")
        self.assertTrue(any("-webkit-line-clamp:1" in rule for rule in event_rules), "月历事项必须单行截断")
        for rule in event_rules:
            self.assertNotIn("-webkit-line-clamp:2", rule, "多行截断会让相邻日期糊成一片")

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
        self.assertIn('id="brand-home"', PAGE_HTML)
        self.assertIn("showFeedback('已回到顶部，并重新显示「开始使用」引导。','')", PAGE_HTML)
        self.assertIn("localStorage.removeItem('qq_digest_journey_hidden')", PAGE_HTML)
        self.assertIn('class="connect-hint"', PAGE_HTML)
        self.assertIn('title="第一次用点这个：下载安装 NapCat、启动引擎并显示登录二维码"', PAGE_HTML)
        self.assertIn('title="只启动引擎并显示登录二维码；不会退出电脑版 QQ"', PAGE_HTML)
        self.assertIn('title="二维码过期或看不清时重新获取一张"', PAGE_HTML)
        self.assertIn(".connect-hint{margin:8px 0 0;color:#42554e;font-size:12px;line-height:1.6}", self._current_stylesheet())


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
        )
        self.server = TaskWebServer(self.settings, self.store)
        self.assertTrue(self.server.start())
        self.addCleanup(self.server.stop)
        assert self.server.server is not None
        self.base = f"http://127.0.0.1:{self.server.server.server_address[1]}"

    def test_hosting_status_exposes_the_fields_the_dashboard_reads(self) -> None:
        status = self._get("/api/hosting/status", "secret")
        for field in ("ok", "hosting_active", "napcat_running", "napcat_online", "groups_selected"):
            self.assertIn(field, status)
        self.assertIsInstance(status["groups_selected"], int)
        self.assertTrue(status["ok"])

    def _get(self, path: str, token: str = "") -> dict:
        headers = {"X-Token": token} if token else {}
        request = urllib.request.Request(self.base + path, headers=headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def _post(self, path: str, payload: dict, token: str = "secret") -> dict:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"X-Token": token, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def _raw_request(self, path: str, method: str = "GET", payload: dict | None = None, token: str = "secret") -> tuple[int, object, bytes]:
        headers = {"X-Token": token} if token else {}
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

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
        self.assertIn("STATUS:NEEDS-ACTION", text)
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
        with mock.patch("qq_live_digest.webapp._Handler._napcat", return_value={"ok": False, "error": "connection refused"}):
            data = self._get("/api/napcat/status", token="secret")
        self.assertFalse(data["ok"])
        self.assertIn("connection refused", data["error"])

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
        self.assertRegex(check_setup, r"if\s*\(\s*!\w+\.ok\s*\)\s*throw\s+Error\(")
        self.assertIn("连接检查失败：", check_setup)
        self.assertIn("群列表暂不可用：", check_setup)
        self.assertIn("if(ok) loadGroups()", check_setup)
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
                ("POST", "/api/history/fetch", {"groups": ["g1"]}),
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

    def test_inbox_ui_has_four_tabs_accessible_workflow_and_no_external_assets(self) -> None:
        html = urllib.request.urlopen(urllib.request.Request(self.base + "/?token=secret", headers={"X-Token": "secret"}), timeout=5).read().decode("utf-8")
        self.assertIn("id=\"tab-inbox-button\"", html)
        self.assertIn("回溯聊天记录", html)
        self.assertIn("确定通知", html)
        self.assertIn("疑似通知", html)
        self.assertIn("启动时自动回溯最近 N 天", html)
        self.assertIn("var order = ['tasks', 'notices', 'inbox', 'settings'];", html)
        self.assertNotIn("https://", html)

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
        self.assertIn("result.restart_required", auto_setup)
        self.assertIn("需要重启 notice-hub 后生效", auto_setup)
        self.assertIn("一键接入失败：", auto_setup)
        self.assertIn("document.getElementById('auto-setup').onclick=runAutoSetup", page)

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
