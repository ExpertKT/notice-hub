"""浏览器级行为测试（Playwright + runner 预装 Edge，channel="msedge"，零浏览器下载）。

只验最关键的两种真行为（替代 tests/test_webapp.py 里对 QR 相关内联 JS 源码字符串的断言；本文件是增量，老测试未动）：

1. NapCat 装了没启动时：`#qrbox` 容器可见，卡片上的主按钮是 `#go`（不是一张永远 hidden 的 `<img>`）。
2. 点 `#go` 走真实「启动引擎」链路（stub 掉 /api/napcat/launch）后：二维码图片真的加载出来、
   `#qrbox` 被页面自己滚进视口且有真实高度。

约定：
* 未安装 playwright 时整类 skipTest，本地与现有三平台 CI 照常绿（requirements-dev.txt 不进 requirements.txt）。
* 自起 TaskWebServer（随机端口、127.0.0.1）、跑完自停，绝不触碰 F:\\notice-hub 实例。
* 不跑 playwright install：channel="msedge" 用系统 Edge（windows-latest 预装 155）。
* 设 NH_BROWSER_ARTIFACT_DIR 时留存 trace.zip 与失败截图（CI 失败上传 artifact 用）。
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
import unittest
import urllib.request
from io import BytesIO
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 推送设置那张「后端键名 → 输入框 id」对照表，直接从产品代码里读出来（不在这里另抄一份）：
# 抄一份的话，产品和测试会一起错，正是这个 bug 之前的模样。
SETTINGS_JS_SOURCE = (PROJECT_ROOT / "noticehub" / "web_static" / "js" / "settings.js").read_text(
    encoding="utf-8")
PUSH_FIELD_PAIRS = re.findall(r"\['([a-z_]+)','(push-[a-z_\-]+)'\]", SETTINGS_JS_SOURCE)

from noticehub.store import Store  # noqa: E402
from noticehub.config import Settings  # noqa: E402
from noticehub.webapp import SESSION_COOKIE_NAME, TaskWebServer  # noqa: E402
from tests import (  # noqa: E402
    isolate_browser_temp_env, open_authed_page, restore_browser_temp_env, wait_for_page_settled,
)

try:
    from playwright.sync_api import sync_playwright
    HAVE_PLAYWRIGHT = True
except ImportError:  # pragma: no cover - 无 playwright 的环境走 skip
    HAVE_PLAYWRIGHT = False

# CI 的 browser job 设 NH_REQUIRE_BROWSER=1：那时「缺 playwright」不许再静默跳过，
# 否则这条防线在 CI 里等于不存在（没人知道自己没被保护）。本地不设，没装依赖也能跑别的。
REQUIRE_BROWSER = os.environ.get("NH_REQUIRE_BROWSER") == "1"
if REQUIRE_BROWSER and not HAVE_PLAYWRIGHT:  # pragma: no cover - 只在 CI 缺依赖时触发
    raise RuntimeError(
        "NH_REQUIRE_BROWSER=1 但没装 playwright：先 pip install -r requirements-dev.txt。"
        "这道检查不允许被 skip 掉。")

TOKEN = "browser-test-token"


@unittest.skipUnless(HAVE_PLAYWRIGHT, "playwright 未安装（pip install -r requirements-dev.txt）")
class QrViewportBrowserTest(unittest.TestCase):
    _temp_env_restore: dict = {}

    @classmethod
    def setUpClass(cls) -> None:
        # Playwright 的 Node 驱动用 os.tmpdir()（TEMP/TMP 环境变量）建 artifacts 目录，
        # 不看 Python 的 tempfile.tempdir；%TEMP% 不可写的机器上会 EPERM。
        # 共用逻辑在 tests/__init__.py，别在这里再写一份。
        cls._temp_env_restore = isolate_browser_temp_env()

    @classmethod
    def tearDownClass(cls) -> None:
        restore_browser_temp_env(cls._temp_env_restore)
        cls._temp_env_restore = {}

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        # 清理顺序（addCleanup 是后进先出）：先 _stop_server，再 _remove_temp_dir。
        self.addCleanup(self._remove_temp_dir)
        self.artifact_dir = os.environ.get("NH_BROWSER_ARTIFACT_DIR", "").strip()
        store = Store(Path(self.tmp.name) / "api.sqlite3")
        settings = Settings(
            web_host="127.0.0.1",
            web_port=0,
            web_token=TOKEN,
            data_dir=Path(self.tmp.name) / "data",
        )
        self.server = TaskWebServer(settings, store)
        self.assertTrue(self.server.start(), "TaskWebServer 启动失败")
        self.addCleanup(self._stop_server)
        assert self.server.server is not None
        self.base = "http://127.0.0.1:%d" % self.server.server.server_address[1]
        # 冷装首开有 onboarding 遮罩层（<div id="onboarding">）。本测试虽不点击页面，
        # 仍走真实接口先把引导标记为已读，让浏览器里的首屏状态与「用户已过引导」一致、可复现。
        accept = urllib.request.Request(
            self.base + "/api/onboarding/accept", data=b"{}",
            headers={"X-Token": TOKEN, "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(accept, timeout=10) as response:
            self.assertEqual(response.status, 200)
        # 服务端 hosting 动作一律打桩：本类只验浏览器渲染，不起 QQ/NapCat、不查进程
        # （照抄 tests/test_webapp.py TaskApiTest.setUp 的做法）。
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
        # 浏览器侧 NapCat 运行态：点「启动并获取二维码」之后才变 true，模拟真实一键接入。
        self.napcat_running = False

    # —— 用例清理：先让服务停稳，再删隔离目录 ——

    def _stop_server(self) -> None:
        """停服务，并等正在处理的请求真正退干净。

        服务端是 `_BoundedThreadingHTTPServer`（`daemon_threads = True`），
        `server_close()` 只关监听套接字、**不会** join 请求线程：页面 5s/10s 的轮询
        可能在 teardown 时仍有一个请求正在写 `api.sqlite3`，此时删目录就是
        `PermissionError: [WinError 32]`。所以这里盯着它自己的活动连接计数等落零。
        `Store` 不用关：它的连接是 `_connect()` 里每次调用自开自关，没有常驻句柄。
        """
        httpd = getattr(self.server, "server", None)  # stop() 会把它置 None，先抓住
        self.server.stop()
        deadline = time.monotonic() + 5
        while httpd is not None and time.monotonic() < deadline:
            if not getattr(httpd, "_active_connections", 0):
                break
            time.sleep(0.05)

    def _remove_temp_dir(self) -> None:
        """删隔离目录；Windows 释放 sqlite 文件句柄有几十毫秒延迟，所以有限次重试。

        最多 5 次、每次 100ms；最后一次仍失败就照常抛出——不 ignore，句柄泄漏必须暴露。
        """
        last_error: PermissionError | None = None
        for _ in range(5):
            try:
                self.tmp.cleanup()
            except PermissionError as error:
                last_error = error
                time.sleep(0.1)
            else:
                return
        raise last_error

    # —— 浏览器侧接口 stub（请求不落到服务端，CI runner 上没有 NapCat 也能跑） ——

    def _napcat_status_stub(self, route) -> None:
        running = self.napcat_running
        body = json.dumps({
            "ok": True,
            "online": False,
            "napcat_running": running,
            "napcat_installed": True,
            "napcat_root": "browser-test-fixture",
            "qr_stamp": "fixture-qr-running" if running else "fixture-qr-idle",
        })
        route.fulfill(status=200, content_type="application/json", body=body)

    def _napcat_launch_stub(self, route) -> None:
        # 启动引擎成功、正在等扫码登录 —— 页面随后会 checkSetup() + showQrAfterAction()。
        self.napcat_running = True
        body = json.dumps({"ok": True})
        route.fulfill(status=200, content_type="application/json", body=body)

    def _napcat_qrcode_stub(self, route) -> None:
        # /api/napcat/qrcode 正常由 napcat_admin 读登录二维码文件；冷装没有 NapCat，
        # 这里喂一张真 PNG（PIL 现生成），让页面的 img.onload 路径真实走通。
        from PIL import Image
        buffer = BytesIO()
        Image.new("RGB", (64, 64), "white").save(buffer, format="PNG")
        route.fulfill(status=200, content_type="image/png", body=buffer.getvalue())

    def test_qr_box_renders_inside_viewport(self) -> None:
        trace_path = os.path.join(self.artifact_dir, "trace.zip") if self.artifact_dir else ""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 800})
                context.tracing.start(screenshots=True, snapshots=True)
                page = context.new_page()
                page.route("**/api/napcat/status", self._napcat_status_stub)
                page.route("**/api/napcat/launch", self._napcat_launch_stub)
                page.route("**/api/napcat/qrcode**", self._napcat_qrcode_stub)
                try:
                    # nowiz=1：跳过「选择要接入的平台」首装向导（page.html:3496 openWizard
                    # 里 nowiz=1 直接 return），否则模态框会盖住整页拦截点击。
                    open_authed_page(context, page, self.base, TOKEN)
                    # ① 装了没启动：容器在、图片隐藏、主按钮是「启动引擎并显示二维码」（#go）。
                    page.wait_for_selector("#qrbox", state="visible", timeout=15000)
                    page.wait_for_selector("#go", state="visible", timeout=15000)
                    self.assertTrue(page.is_hidden("#qr"), "#qr 未启动时不应显示")
                    # 滚到容器再量命中：按钮不能只是「有尺寸」而实际被别的层盖住。
                    page.eval_on_selector("#qrbox", "el => el.scrollIntoView({block: 'center'})")
                    hit = page.eval_on_selector(
                        "#go",
                        """el => {
                            const r = el.getBoundingClientRect();
                            const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
                            return {ok: !!hit && (hit === el || el.contains(hit)),
                                    hitTag: hit ? hit.tagName + '#' + hit.id : null,
                                    top: r.top, bottom: r.bottom, height: r.height};}""")
                    self.assertGreater(hit["height"], 0, hit)
                    self.assertTrue(hit["ok"], hit)

                    # ② 点启动按钮：走真实 run() → checkSetup() + showQrAfterAction()，
                    # 后者把 #qrbox scrollIntoView 进视口，refreshQr() 让真 PNG 走 img.onload。
                    page.click("#go")
                    page.wait_for_selector("#qr:not([hidden])", state="visible", timeout=15000)
                    page.wait_for_function(
                        "() => { const i = document.getElementById('qr');"
                        " return i && i.complete && i.naturalWidth > 0; }", timeout=15000)
                    self.assertTrue(page.is_hidden("#go"), "启动后启动按钮应隐藏")
                    # 页面自己的 scrollIntoView 可能是平滑滚动，等它停稳再量
                    page.wait_for_function(
                        "() => { const r = document.getElementById('qrbox').getBoundingClientRect();"
                        " return r.top >= 0 && r.bottom <= window.innerHeight; }", timeout=15000)
                    metrics = page.eval_on_selector(
                        "#qrbox",
                        """el => {
                            const r = el.getBoundingClientRect();
                            return {top: r.top, bottom: r.bottom, height: r.height,
                                    viewportHeight: window.innerHeight};}""")
                    self.assertGreater(metrics["height"], 0, metrics)
                    self.assertGreaterEqual(metrics["top"], 0, metrics)
                    self.assertLessEqual(metrics["bottom"], metrics["viewportHeight"], metrics)
                except Exception:
                    if self.artifact_dir:
                        os.makedirs(self.artifact_dir, exist_ok=True)
                        page.screenshot(path=os.path.join(self.artifact_dir, "qr-viewport-failure.png"),
                                        full_page=True)
                    raise
                finally:
                    if trace_path:
                        os.makedirs(self.artifact_dir, exist_ok=True)
                        context.tracing.stop(path=trace_path)
                    else:
                        context.tracing.stop()
                    context.close()
            finally:
                browser.close()


    def _connect_status_stub(self, route) -> None:
        """「手动起的 NapCat 在跑、接口没通」：页面必须给出唯一可点的「帮我连上它」。"""
        body = json.dumps({
            "ok": False,
            "online": False,
            "napcat_running": False,
            "napcat_installed": True,
            "napcat_root": "browser-test-fixture",
            "stage": "running_no_api",
            "can_connect": True,
            "connect_url": "/api/napcat/connect",
            "napcat_api_url": "http://127.0.0.1:3001",
            "napcat_api_candidates": ["http://127.0.0.1:3001", "http://127.0.0.1:3000"],
            "error": "检测到这台机器上有 NapCat 在跑（像你手动启动的那份），但本程序连不上它的 OneBot 接口。"
                     "点「帮我连上它」：程序会去读 NapCat 自己的配置，按它实际开的端口接上，不用你自己找端口。",
        })
        route.fulfill(status=200, content_type="application/json", body=body)

    def test_running_no_api_offers_one_click_to_connect(self) -> None:
        """第五态：引擎在跑但接口没通。

        此前这个状态在 plans 里没有对应项，四个按钮全 hidden、提示停在「正在检查 NapCat 引擎…」，
        整张卡没有一个能点的东西。现在只露「帮我连上它」主按钮（+「重启引擎」次按钮）；
        点下去必须真的 POST /api/napcat/connect：成功报出新地址，失败把 WebUI 地址摆出来。
        """
        posts: list = []
        state = {"mode": "ok"}

        def connect_stub(route) -> None:
            posts.append(route.request.method)
            if state["mode"] == "ok":
                body = json.dumps({
                    "ok": True, "api_url": "http://127.0.0.1:3000",
                    "tried": ["http://127.0.0.1:3001", "http://127.0.0.1:3000"],
                    "persisted": True, "persist_error": "",
                })
            else:
                body = json.dumps({
                    "ok": False, "stage": "running_no_api",
                    "webui_url": "http://127.0.0.1:6099",
                    "tried": [{"url": "http://127.0.0.1:3001"}, {"url": "http://127.0.0.1:3000"}],
                    "error": "3001 和 3000 都没连上：NapCat 的 HTTP 服务可能没开。",
                })
            route.fulfill(status=200, content_type="application/json", body=body)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                for width in (1280, 390):
                    # 每个宽度都从「成功」这一档重新走：state/posts 跨宽度共用，不清就会串档。
                    state["mode"] = "ok"
                    posts.clear()
                    context = browser.new_context(viewport={"width": width, "height": 900})
                    page = context.new_page()
                    page.route("**/api/napcat/status", self._connect_status_stub)
                    page.route("**/api/napcat/connect", connect_stub)
                    try:
                        open_authed_page(context, page, self.base, TOKEN)
                        page.wait_for_selector("#napcat-connect", state="visible", timeout=15000)
                        # 一个区域只留一个主行动：安装/启动/刷新全收起，重启留作次按钮。
                        for hidden_id in ("#auto-setup", "#go", "#refresh-qr"):
                            self.assertTrue(page.is_hidden(hidden_id), hidden_id)
                        self.assertFalse(page.is_hidden("#restart-napcat"), "重启引擎应作为次按钮留着")
                        self.assertIn("帮我连上它", page.inner_text("#napcat-connect"))
                        # 引擎行只留一个短状态词：整句「在跑但连不上接口」由上方的黄框说，别同一句说两遍。
                        self.assertIn("NapCat 进程在跑", page.inner_text("#napcat-engine"))
                        self.assertIn("接口未连", page.inner_text("#napcat-engine"))
                        # 二维码区不能自相矛盾（引擎明明在跑，却写「还没启动」），也不能指向本状态 hidden 的按钮。
                        qr_note = page.inner_text("#qr-note")
                        self.assertNotIn("还没启动", qr_note)
                        self.assertNotIn("启动引擎并显示二维码", qr_note)
                        self.assertIn("接口连上之后", qr_note)
                        # 有尺寸且不被挡住：中心点命中的必须是它自己。
                        page.eval_on_selector("#napcat-connect", "el => el.scrollIntoView({block: 'center'})")
                        hit = page.eval_on_selector("#napcat-connect", """el => {
                            const r = el.getBoundingClientRect();
                            const found = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
                            return {ok: !!found && (found === el || el.contains(found)),
                                    tag: found ? found.tagName + '#' + found.id : null, height: r.height};}""")
                        self.assertGreater(hit["height"], 0, hit)
                        self.assertTrue(hit["ok"], hit)
                        # ① 成功：确实 POST 出去了，并把接上的地址说出来。
                        page.click("#napcat-connect")
                        page.wait_for_function(
                            "() => (document.getElementById('steps').textContent || '').indexOf('已连上 NapCat 的接口') >= 0",
                            timeout=15000)
                        self.assertEqual(posts, ["POST"], posts)
                        # ② 失败：状态里有原因和试过的地址，另有 WebUI 地址可点（不是一句报错就完事）。
                        state["mode"] = "fail"
                        page.wait_for_function(
                            "() => !document.getElementById('napcat-connect').disabled", timeout=15000)
                        page.click("#napcat-connect")
                        page.wait_for_selector("#napcat-connect-help", state="visible", timeout=15000)
                        status_text = page.inner_text("#status")
                        self.assertIn("3001 和 3000 都没连上", status_text)
                        self.assertIn("http://127.0.0.1:3001", status_text)
                        self.assertIn("6099", page.inner_text("#napcat-connect-help"))
                        href = page.eval_on_selector("#napcat-connect-help a", "el => el.href")
                        self.assertIn("6099", href)
                        # 同一件事不在两个位置说两遍：底部只留一句指路，不重复黄框的错误原文。
                        steps_text = page.inner_text("#steps")
                        self.assertNotIn("3001 和 3000 都没连上", steps_text)
                        self.assertIn("见上面的 WebUI 链接", steps_text)
                        self.assertEqual(posts, ["POST", "POST"], posts)
                    finally:
                        context.close()
            finally:
                browser.close()

    def test_banner_stable(self) -> None:
        """QQ 接入卡顶部黄色提示：5 秒轮询会在 1–5 行之间换文案，容器高度必须恒定，
        否则下方按钮/二维码会被挤得上下跳（用户实测的复选框回归）。
        `.status-slot 里放了一条隐藏的「最长文案」占位，这里用真实几何断言。"""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                for width in (1280, 390):
                    context = browser.new_context(viewport={"width": width, "height": 900})
                    page = context.new_page()
                    page.route("**/api/napcat/status", self._napcat_status_stub)
                    try:
                        open_authed_page(context, page, self.base, TOKEN)
                        page.wait_for_selector("#status", state="attached", timeout=15000)
                        measured = page.evaluate("""() => {
                            const el = document.getElementById('status');
                            const slot = document.querySelector('.status-slot');
                            const below = document.querySelector('.connect-actions');
                            if (!el || !slot || !below) return null;
                            const texts = [
                              'QQ 未登录',
                              '还没装 NapCat：点上面的「下载安装并启动」；或把手头的 NapCat 放进程序数据目录下的 napcat\\\\ 后点「重新检测」。',
                              '等待 QQ 登录（已等 12 秒）：接口要等登录成功才会开，现在连不上是正常的。用手机 QQ 扫下面的二维码，扫完在手机上点「确认登录」。二维码一会就过期，图换了就扫最新的那张；等了 90 秒还没动静，点「重启引擎」再扫。',
                              'NapCat 的接口端口（127.0.0.1:3001）在监听，但接口调用失败：这是配置问题，不是没登录，继续扫码解决不了。请核对连接密码是否和 NapCat 里填的一致，以及 3001 端口是否被别的程序占用（换端口或关掉占用它的程序），改完点「重启引擎」。'
                            ];
                            const rows = [];
                            for (const t of texts) {
                              el.textContent = t;
                              rows.push({card: el.getBoundingClientRect().height,
                                         slot: slot.getBoundingClientRect().height,
                                         below: below.getBoundingClientRect().top});
                            }
                            return rows;
                        }""")
                        self.assertIsNotNone(measured, "没找到 #status / .status-slot / .connect-actions")
                        cards = [r["card"] for r in measured]
                        slots = [r["slot"] for r in measured]
                        tops = [r["below"] for r in measured]
                        self.assertLessEqual(max(cards) - min(cards), 1,
                                             "黄框高度随文案变化：%s" % measured)
                        self.assertLessEqual(max(slots) - min(slots), 1,
                                             "占位槽高度随文案变化：%s" % measured)
                        self.assertLessEqual(max(tops) - min(tops), 1,
                                             "下方按钮被挤动：%s" % measured)
                        # 占位那条必须是「最长文案」：换成它自己的文字时高度也不能变矮
                        self.assertGreater(min(cards), 0, measured)
                    finally:
                        context.close()
            finally:
                browser.close()


    def test_task_summary_drops_duplicate_stats_and_keeps_one_button(self) -> None:
        """今日概览那排统计已整排删除（与标题「逾期 36 件」、进度条重复），只留「清空已完成」。

        这里锁的是新契约，不是把老用例删掉：
        ① 三个 #stat-* 元素不许回来（有人偷偷加回来就红）；
        ② 「清空已完成」仍是真 <button>、有边框、光标是手型（被改成纯文字或伪元素也红）；
        ③ 点它仍是两段式确认：第一次只改文案、不发请求；4 秒内再点一次才真的 POST。
        右栏的另一块统计（rail-stat-*）不在这次改动范围，仍然要有。
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                page = context.new_page()
                posts: list = []

                def clear_stub(route) -> None:
                    posts.append(route.request.method)
                    route.fulfill(status=200, content_type="application/json",
                                  body=json.dumps({"ok": True, "removed": 0}))

                try:
                    page.route("**/api/tasks/clear-finished", clear_stub)
                    open_authed_page(context, page, self.base, TOKEN)
                    page.wait_for_selector("#tasks-clear-done", state="visible", timeout=15000)
                    left = page.evaluate(
                        "() => ['stat-open','stat-overdue','stat-done']"
                        ".filter(id => document.getElementById(id))")
                    self.assertEqual(left, [], "统计数字不许回到这一排：%s" % left)
                    info = page.evaluate("""() => {
                        const el = document.getElementById('tasks-clear-done');
                        const cs = getComputedStyle(el);
                        const rail = document.getElementById('rail-stat-open');
                        return {tag: el.tagName, border: parseFloat(cs.borderTopWidth) || 0,
                                cursor: cs.cursor, text: (el.textContent || '').trim(),
                                rail: !!rail};
                    }""")
                    self.assertEqual(info["tag"], "BUTTON", info)
                    self.assertGreater(info["border"], 0, "清空按钮要有按钮外观：%s" % info)
                    self.assertEqual(info["cursor"], "pointer", info)
                    self.assertEqual(info["text"], "清空已完成", info)
                    self.assertTrue(info["rail"], "右栏那块统计不该一起删掉：%s" % info)
                    # 两段式：第一次点只改文案，一次请求都不发。
                    page.click("#tasks-clear-done")
                    page.wait_for_function(
                        "() => document.getElementById('tasks-clear-done').textContent.indexOf('确认清空') === 0",
                        timeout=5000)
                    self.assertEqual(posts, [], "第一次点就发请求了，两段式确认被破坏：%s" % posts)
                    self.assertIn("4 秒内再点一次", page.inner_text("#tasks-clear-done"))
                    # 第二次点才真清。
                    page.click("#tasks-clear-done")
                    page.wait_for_function(
                        "() => document.getElementById('tasks-clear-done').textContent !== '确认清空？'",
                        timeout=5000)
                    self.assertEqual(posts, ["POST"], "第二次点没发请求：%s" % posts)
                finally:
                    context.close()
            finally:
                browser.close()


    def test_settings_run_info_shows_real_port_and_its_source(self) -> None:
        """运行信息里多一行「服务端口」：环境变量优先于 .env，启动器还可能在端口被占时往上找。

        用户写了 58066 却听在 58067、全程没提示，所以这一行显示的是**真正听在哪**。
        来源是环境变量时必须说清「已覆盖 .env」，不能只写「来源：环境变量」；
        字段缺失（老后端）整行不出现，不显示「未知」也不显示 0。
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                real = urllib.request.urlopen(urllib.request.Request(
                    self.base + "/api/meta", headers={"X-Token": TOKEN}), timeout=10
                ).read().decode("utf-8")
                for case in ("dotenv", "env", "missing"):
                    payload = json.loads(real)
                    if case == "missing":
                        payload.pop("web_port", None)
                        payload.pop("web_port_source", None)
                    else:
                        payload["web_port"] = 8766 if case == "dotenv" else 58067
                        payload["web_port_source"] = case
                    body = json.dumps(payload)
                    context = browser.new_context(viewport={"width": 1280, "height": 900})
                    page = context.new_page()

                    # Playwright 会把 (route, request) 一起传进来，handler 只能收 route，
                    # 所以体用闭包工厂绑住，别写成 lambda 默认参数（第二个参数会被 Request 顶掉）。
                    def make_meta_stub(payload):
                        def handler(route):
                            route.fulfill(status=200, content_type="application/json", body=payload)
                        return handler

                    try:
                        page.route("**/api/meta", make_meta_stub(body))
                        open_authed_page(context, page, self.base, TOKEN)
                        page.click("#tab-settings-button")
                        # 先确认设置页真的切出来了，再等具体那一行；失败信息直接指向「没切过去」。
                        page.wait_for_selector("#tab-settings", state="visible", timeout=15000)
                        if case == "missing":
                            # 老后端：这一行整行不出现（别的行照旧）。
                            page.wait_for_selector("text=运行信息", timeout=15000)
                            page.wait_for_timeout(400)
                            self.assertEqual(
                                page.evaluate("() => document.querySelectorAll('.kv[data-row=\"web-port\"]').length"),
                                0, "字段缺失时不该显示这一行")
                        else:
                            page.wait_for_selector('.kv[data-row="web-port"]', state="visible", timeout=15000)
                            text = page.inner_text('.kv[data-row="web-port"]')
                            if case == "dotenv":
                                self.assertEqual(text.replace("\n", " ").strip(), "服务端口 8766", text)
                            else:
                                self.assertEqual(text.replace("\n", " ").strip(),
                                                 "服务端口 58067（来自环境变量，已覆盖 .env）", text)
                                self.assertIn("已覆盖 .env", text)
                    finally:
                        context.close()
            finally:
                browser.close()


    def test_upcoming_empty_state_shrinks_to_content(self) -> None:
        """今日概览的轮播：0 件时不能继续占着 200px 的固定轨道（那片白看着像没加载出来）。

        有数据时高度必须照旧（1280=200、390=220），否则等于把「切换顶动下方元素」的老坑挖回来。
        另外从空态填 1 条再清空，上方元素（标题/副标题）的 top 不能被这一来一回顶走。
        """
        def post(path: str, payload: dict) -> dict:
            data = json.dumps(payload).encode()
            request = urllib.request.Request(
                self.base + path, data=data, method="POST",
                headers={"X-Token": TOKEN, "Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read().decode())

        def get(path: str) -> dict:
            request = urllib.request.Request(self.base + path, headers={"X-Token": TOKEN})
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read().decode())

        def mark_all_done() -> None:
            for bucket in ("today", "later", "pinned"):
                for task in get("/api/tasks").get(bucket, []):
                    post("/api/tasks/" + str(task["id"]), {"action": "done"})

        measure = """() => {
            const box = document.getElementById('upcoming-carousel');
            const rect = box.getBoundingClientRect();
            const top = (sel) => { const el = document.querySelector(sel);
                return el ? Math.round(el.getBoundingClientRect().top * 10) / 10 : null; };
            return {height: Math.round(rect.height * 10) / 10,
                    cssHeight: parseFloat(getComputedStyle(box).height),
                    top: Math.round(rect.top * 10) / 10,
                    empty: box.classList.contains('is-empty'),
                    headline: top('#headline'), subline: top('#subline'),
                    card: top('.summary'),
                    bottom: top('.summary-bottom'), bar: top('.summary-bottom .bar')};}"""

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                for width, tall in ((1280, 200), (390, 220)):
                    mark_all_done()
                    context = browser.new_context(viewport={"width": width, "height": 900})
                    page = context.new_page()
                    try:
                        open_authed_page(context, page, self.base, TOKEN)
                        # 概览卡的呼吸动效会给祖先加缩放，量绝对 top 会飘 1–2px（transform 不改布局，
                        # 但会缩放 getBoundingClientRect）。所以位置断言一律容 2px，高度断言用计算值。
                        page.wait_for_selector("#upcoming-carousel", state="visible", timeout=15000)
                        page.wait_for_function(
                            "() => document.getElementById('upcoming-carousel').classList.contains('is-empty')",
                            timeout=15000)
                        empty = page.evaluate(measure)
                        self.assertTrue(empty["empty"], empty)
                        self.assertLess(empty["cssHeight"], tall - 40,
                                        "空态还撑着固定高度：%s" % empty)
                        self.assertGreater(empty["height"], 0, empty)
                        self.assertIn("0 件", page.inner_text("#upcoming-count"))
                        # 390 下按钮和进度条不能被挤到奇怪位置：紧跟在收缩后的轮播下面（间距 0–120px）。
                        gap = empty["bottom"] - (empty["top"] + empty["height"])
                        self.assertGreaterEqual(gap, 0, empty)
                        self.assertLess(gap, 120, empty)

                        # 填 1 条：回到固定轨道，字号/行距不变
                        post("/api/tasks/create", {"summary": "空态收缩测试",
                                                   "deadline": "2026-10-31 23:59"})
                        page.evaluate("() => loadTasks()")
                        page.wait_for_function(
                            "() => !document.getElementById('upcoming-carousel').classList.contains('is-empty')",
                            timeout=15000)
                        filled = page.evaluate(measure)
                        # 定高断言用计算值：外层有呼吸动效的缩放，getBoundingClientRect 会差几个像素。
                        self.assertEqual(filled["cssHeight"], tall, filled)
                        self.assertGreater(filled["height"], empty["height"] + 40, filled)
                        self.assertFalse(filled["empty"], filled)
                        # 「上方元素被顶走」要按卡片内相对位置量：概览卡在栅格里是垂直居中的，
                        # 卡片长高本身会让整卡上移半格，绝对 top 不可比（实测空的 191.6、有数据 179）。
                        self.assertLessEqual(abs((filled["headline"] - filled["card"])
                                                 - (empty["headline"] - empty["card"])), 2, (empty, filled))

                        mark_all_done()
                        page.evaluate("() => loadTasks()")
                        page.wait_for_function(
                            "() => document.getElementById('upcoming-carousel').classList.contains('is-empty')",
                            timeout=15000)
                        cleared = page.evaluate(measure)
                        # 清空后标题/副标题的文案本身会变（「今天 0 件」vs「今天 1 件」），绝对 top 不可比；
                        # 要比的是布局：轮播高度回到空态值，且按钮仍在轮播下面、不重叠。
                        self.assertEqual(cleared["cssHeight"], empty["cssHeight"],
                                         "清空后没有回到原来的空态高度：%s vs %s" % (cleared, empty))
                        # 副标题文案会随数据出现/消失，绝对间距会变；这里只保证按钮落在轮播下面、不重叠、不跑到屏外。
                        self.assertGreaterEqual(cleared["bottom"] - (cleared["top"] + cleared["height"]), 0, (empty, cleared))
                        self.assertLess(cleared["bottom"] - (cleared["top"] + cleared["height"]), 120, (empty, cleared))
                    finally:
                        context.close()
            finally:
                browser.close()


    # —— 配置体检：横幅点开就地展开，不跳页、不弹窗 ——

    CHECKUP_HINT = "在钉钉开放平台 → 应用开发 → 企业内应用，复制 AppKey 和 AppSecret 填到设置页。"

    def _checkup_payload(self, done: bool = False) -> dict:
        if done:
            platforms = {key: {"configured": True, "status": "ok", "headline": "", "action": None}
                         for key in ("qq", "wechat", "dingtalk", "chaoxing", "push", "llm")}
            return {"ok": True, "platforms": platforms}
        return {"ok": True, "platforms": {
            "qq": {"configured": False, "status": "ok", "engine_state": "running",
                   "headline": "NapCat 在跑，但它的 OneBot 接口没连上",
                   "action": {"kind": "api", "label": "帮我连上它",
                              "target": "/api/napcat/connect", "hint": ""}},
            "wechat": {"configured": False, "status": "ok",
                       "headline": "微信接入还没打开",
                       "action": {"kind": "panel", "label": "去打开微信接入",
                                  "target": "wechat-settings-card", "hint": ""}},
            "dingtalk": {"configured": False, "status": "ok",
                         "headline": "钉钉还没填 AppKey",
                         "action": {"kind": "manual", "label": "看怎么填",
                                    "target": "", "hint": self.CHECKUP_HINT}},
            "chaoxing": {"configured": True, "status": "ok", "headline": "", "action": None},
            "push": {"configured": True, "status": "ok", "headline": "", "action": None},
            "llm": {"configured": False, "status": "pending", "headline": "", "action": None},
        }}

    def test_setup_checkup_expands_in_place(self) -> None:
        """横幅「继续配置 →」不该直接跳第一个平台，而是就地展开体检列表。

        每行 = 平台名 + 后端给的 headline + 一个动作按钮：panel 跳对卡片、api 真发 POST、
        manual 原文展开可选中复制。全部配好时整块横幅消失，不留空壳。
        """
        posts: list = []
        status_calls = {"n": 0}
        state = {"done": False, "connect": "ok"}

        def status_stub(route) -> None:
            status_calls["n"] += 1
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(self._checkup_payload(state["done"])))

        def connect_stub(route) -> None:
            posts.append(route.request.method + " " + route.request.url.split("?")[0])
            if state["connect"] == "ok":
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": True, "api_url": "http://127.0.0.1:3000",
                                               "tried": [], "persisted": True}))
            else:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": False, "stage": "running_no_api",
                                               "error": "3001 和 3000 都没连上"}))

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                for width in (1280, 390):
                    state["done"] = False
                    state["connect"] = "ok"
                    posts.clear()
                    context = browser.new_context(viewport={"width": width, "height": 900})
                    page = context.new_page()
                    page.route("**/api/setup/status", status_stub)
                    page.route("**/api/napcat/connect", connect_stub)
                    try:
                        open_authed_page(context, page, self.base, TOKEN)
                        page.wait_for_selector("#platform-setup-panel", state="visible", timeout=15000)
                        banner = "#platform-setup-banner"
                        self.assertEqual(page.get_attribute(banner, "aria-expanded"), "false")
                        self.assertTrue(page.is_hidden("#setup-checkup-list"))
                        # 收起态：文字说的是「展开看看」，不能写暗示跳转的「继续配置」。
                        self.assertIn("查看还差什么", page.inner_text(banner))
                        self.assertNotIn("继续配置", page.inner_text(banner))
                        # 收起时列表里不能有任何可 Tab 到的东西（靠 [hidden]{display:none!important} 天然满足，
                        # 这条断言就是防止有人改成 visibility/max-height 后忘了收焦点）。
                        focusable = page.eval_on_selector_all(
                            "#setup-checkup-list button, #setup-checkup-list a[href], #setup-checkup-list pre",
                            "els => els.filter(e => e.offsetParent !== null).length")
                        self.assertEqual(focusable, 0, "收起态列表里还有可聚焦元素")
                        # 前置：把横幅说明压长，逼它在窄布局/CI 字体下换成多行——
                        # 换行会改变整行高度，是「横幅被列表推走」最容易出现的排版条件。
                        page.evaluate("""() => {
                            const el = document.getElementById('platform-setup-message');
                            if (el) el.textContent = '这一行故意写得很长，用来逼横幅在窄屏和 CI 的字体下换行：'
                                + '它换行之后整行会变高，正好是列表展开时最容易让横幅位置漂移的条件。'
                                + '如果列表跑到横幅上面、或者横幅被纵向居中，这条断言就会在这里抓住。';
                        }""")
                        page.wait_for_timeout(50)
                        # 展开前后横幅自己的位置不许动（列表在它下方撑开）。
                        # 量 offsetTop（布局位置，不受 focus 引起的滚动干扰）：CI 上曾因点击后浏览器
                        # 把按钮滚进视野，getBoundingClientRect().top 直接差了 235px，而布局其实没动。
                        # 量之前先把滚动归零，两种坐标就都稳定可比。
                        before = page.evaluate("""() => {
                            window.scrollTo(0, 0);
                            const el = document.getElementById('platform-setup-banner');
                            return {top: Math.round(el.offsetTop), y: Math.round(window.scrollY)};}""")
                        page.click(banner)
                        page.wait_for_selector("#setup-checkup-list .setup-checkup-row", timeout=15000)
                        self.assertEqual(page.get_attribute(banner, "aria-expanded"), "true")
                        self.assertIn("收起", page.inner_text(banner))
                        after = page.evaluate("""() => {
                            window.scrollTo(0, 0);
                            const el = document.getElementById('platform-setup-banner');
                            return {top: Math.round(el.offsetTop), y: Math.round(window.scrollY)};}""")
                        self.assertLessEqual(abs(after["top"] - before["top"]), 1, (before, after))
                        self.assertEqual(after["y"], before["y"], "展开把页面滚走了")
                        # 行数与文案：只列未配好且后端给了 headline 的（pending 和已配好的都不进）
                        rows = page.eval_on_selector_all(
                            "#setup-checkup-list .setup-checkup-row",
                            """els => els.map(e => ({
                                   key: e.dataset.platform,
                                   name: (e.querySelector('strong') || {}).textContent || '',
                                   headline: (e.querySelector('.setup-checkup-headline') || {}).textContent || '',
                                   kind: (e.querySelector('.setup-checkup-act') || {dataset: {}}).dataset.kind || '',
                                   label: (e.querySelector('.setup-checkup-act') || {}).textContent || ''}))""")
                        self.assertEqual([r["key"] for r in rows], ["qq", "wechat", "dingtalk"], rows)
                        self.assertEqual([r["kind"] for r in rows], ["api", "panel", "manual"], rows)
                        self.assertEqual(rows[0]["headline"], "NapCat 在跑，但它的 OneBot 接口没连上")
                        self.assertEqual(rows[2]["label"], "看怎么填")
                        if width == 1280:
                            # panel：点下去跳到微信卡（tab 切过去 + 焦点落在卡片上）
                            page.click('.setup-checkup-row[data-platform="wechat"] .setup-checkup-act')
                            page.wait_for_function(
                                "() => document.activeElement && document.activeElement.id === 'wechat-settings-card'",
                                timeout=15000)
                            # api：真发 POST；成功后再拉一次体检
                            calls_before = status_calls["n"]
                            page.click('.setup-checkup-row[data-platform="qq"] .setup-checkup-act')
                            page.wait_for_function(
                                "() => document.querySelector('.setup-checkup-row[data-platform=\"qq\"] .setup-checkup-act').disabled === false",
                                timeout=15000)
                            # 计数器是 Python 侧的，CDP 的 wait_for_function 看不到它：
                            # 按钮恢复可点 != 请求已经发出去，所以轮询到计数涨了再断言，别抢跑。
                            self.assertEqual(posts, ["POST " + self.base + "/api/napcat/connect"], posts)
                            deadline = time.monotonic() + 10
                            while status_calls["n"] <= calls_before and time.monotonic() < deadline:
                                page.wait_for_timeout(50)
                            self.assertGreater(status_calls["n"], calls_before, "成功后应刷新体检")
                            # api 失败：错误留在这一行、按钮恢复、可重试；不整块重渲染把错误冲掉
                            state["connect"] = "fail"
                            page.click('.setup-checkup-row[data-platform="qq"] .setup-checkup-act')
                            page.wait_for_selector('.setup-checkup-row[data-platform="qq"] .setup-checkup-error',
                                                   state="visible", timeout=15000)
                            page.wait_for_timeout(300)
                            err = page.inner_text('.setup-checkup-row[data-platform="qq"] .setup-checkup-error')
                            self.assertIn("3001 和 3000 都没连上", err)
                            self.assertIn("可以再点一次", err)
                            disabled = page.eval_on_selector(
                                '.setup-checkup-row[data-platform="qq"] .setup-checkup-act',
                                "el => el.disabled")
                            self.assertFalse(disabled, "失败后按钮要能重试")
                            page.click('.setup-checkup-row[data-platform="qq"] .setup-checkup-act')
                            page.wait_for_function(
                                "() => document.querySelectorAll('.setup-checkup-row[data-platform=\"qq\"] .setup-checkup-error:not([hidden])').length === 1",
                                timeout=15000)
                            # 同上一处：POST 计数是 Python 侧的，等到 3 次再断言。
                            deadline = time.monotonic() + 10
                            while len(posts) < 3 and time.monotonic() < deadline:
                                page.wait_for_timeout(50)
                            self.assertEqual(len(posts), 3, posts)
                            # manual：键盘 Enter 也能展开；hint 原文照抄、可选中复制
                            manual_btn = '.setup-checkup-row[data-platform="dingtalk"] .setup-checkup-act'
                            page.focus(manual_btn)
                            self.assertEqual(page.get_attribute(manual_btn, "aria-expanded"), "false")
                            page.keyboard.press("Enter")
                            page.wait_for_selector('.setup-checkup-row[data-platform="dingtalk"] .setup-checkup-hint',
                                                   state="visible", timeout=10000)
                            self.assertEqual(page.get_attribute(manual_btn, "aria-expanded"), "true")
                            hint = page.inner_text('.setup-checkup-row[data-platform="dingtalk"] .setup-checkup-hint')
                            self.assertEqual(hint, self.CHECKUP_HINT, "hint 必须原样展示，不许前端改写")
                            selectable = page.eval_on_selector(
                                '.setup-checkup-row[data-platform="dingtalk"] .setup-checkup-hint',
                                """el => {
                                    const style = getComputedStyle(el);
                                    const range = document.createRange();
                                    range.selectNodeContents(el);
                                    const sel = window.getSelection();
                                    sel.removeAllRanges(); sel.addRange(range);
                                    return {userSelect: style.userSelect || style.webkitUserSelect,
                                            picked: sel.toString(), whiteSpace: style.whiteSpace};}""")
                            self.assertNotEqual(selectable["userSelect"], "none", "hint 必须可选中复制")
                            self.assertEqual(selectable["picked"].strip(), self.CHECKUP_HINT)
                        # 截图（展开态，两种宽度）
                        if self.artifact_dir:
                            os.makedirs(self.artifact_dir, exist_ok=True)
                            page.screenshot(path=os.path.join(
                                self.artifact_dir, "checkup-%d.png" % width))
                    finally:
                        context.close()
                # 全部配好：整块横幅消失，列表不留空壳
                state["done"] = True
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                page = context.new_page()
                page.route("**/api/setup/status", status_stub)
                try:
                    open_authed_page(context, page, self.base, TOKEN)
                    page.wait_for_timeout(800)
                    self.assertTrue(page.is_hidden("#platform-setup-panel"), "都配好了横幅必须消失")
                    self.assertEqual(page.evaluate(
                        "() => document.querySelectorAll('#setup-checkup-list *').length"), 0)
                    self.assertTrue(page.is_hidden("#setup-checkup-list"))
                finally:
                    context.close()
            finally:
                browser.close()

    def test_inbox_renders_image_only_when_item_has_image(self) -> None:
        """收件箱图片：只有 `has_image` 为真的条目才渲染 `<img>`，其余一条 DOM 都不加。

        真实 bug：以前只认 `source === 'onebot-attachment'`，普通消息和回溯消息里的图片
        在收件箱永远是「[图片]」两个字；就算渲染了，塞进默认收起的 `<details>` 里也看不见。
        这里用真实 `renderInboxItem` 渲染三条并查 DOM：图片必须在折叠块外面、挂在卡片上。
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                context = browser.new_context(viewport={"width": 390, "height": 844})
                page = context.new_page()
                try:
                    open_authed_page(context, page, self.base, TOKEN)
                    page.wait_for_selector("#inbox-items", state="attached", timeout=15000)
                    measured = page.evaluate("""() => {
                        const base = {content: '[图片]', received_at: '2026-10-31 01:00',
                                      group_name: '测试群', verdict: 'notice',
                                      method: 'llm', reason: '含图片'};
                        const render = (extra) => {
                          const box = document.createElement('div');
                          box.appendChild(renderInboxItem(Object.assign({}, base, extra)));
                          document.body.appendChild(box);
                          return box;
                        };
                        const has = render({msg_id: 'm-has', has_image: true});
                        const none = render({msg_id: 'm-none', has_image: false});
                        const missing = render({msg_id: 'm-missing'});
                        const read = (box) => {
                          const img = box.querySelector('img');
                          const link = box.querySelector('a.inbox-attachment-link');
                          return {imgs: box.querySelectorAll('img').length,
                                  links: box.querySelectorAll('a.inbox-attachment-link').length,
                                  src: img ? img.getAttribute('src') : null,
                                  alt: img ? img.getAttribute('alt') : null,
                                  loading: img ? img.getAttribute('loading') : null,
                                  href: link ? link.getAttribute('href') : null,
                                  target: link ? link.getAttribute('target') : null,
                                  rel: link ? link.getAttribute('rel') : null};
                        };
                        const img = has.querySelector('img');
                        const details = has.querySelector('details');
                        const out = {
                          has: read(has), none: read(none), missing: read(missing),
                          hasText: has.textContent.indexOf('[图片]') >= 0,
                          // 图片不能待在那个默认收起的折叠块里
                          inDetails: !!(img && img.closest('details')),
                          // 而且要排在 details 之后（DOM 顺序）
                          afterDetails: !!(img && details &&
                            (details.compareDocumentPosition(img) & Node.DOCUMENT_POSITION_FOLLOWING))
                        };
                        // 取不到图：外层 <a> 也要一起走，别留空占位
                        img.dispatchEvent(new Event('error'));
                        out.linksAfterError = has.querySelectorAll('a.inbox-attachment-link').length;
                        out.imgsAfterError = has.querySelectorAll('img').length;
                        has.remove(); none.remove(); missing.remove();
                        return out;
                    }""")
                    has, none, missing = measured["has"], measured["none"], measured["missing"]
                    self.assertEqual(has["imgs"], 1, measured)
                    self.assertTrue((has["src"] or "").startswith("/api/inbox/image"),
                                    "图片要指向 /api/inbox/image：%s" % measured)
                    self.assertEqual(has["loading"], "lazy", measured)
                    self.assertEqual(has["alt"], "测试群 的图片", measured)
                    self.assertEqual(has["target"], "_blank", measured)
                    self.assertTrue((has["href"] or "").startswith("/api/inbox/image"), measured)
                    # 不展开折叠块就得看得见图
                    self.assertFalse(measured["inDetails"], "图片不许塞进 <details>：%s" % measured)
                    self.assertTrue(measured["afterDetails"], "图片要排在 details 之后：%s" % measured)
                    # 没有图片的条目：既没有 img，也没有空链接
                    self.assertEqual(none["imgs"], 0, "has_image=false 不许渲染图片：%s" % measured)
                    self.assertEqual(none["links"], 0, "has_image=false 不许留空链接：%s" % measured)
                    self.assertEqual(missing["imgs"], 0, "has_image 缺失不许渲染图片：%s" % measured)
                    self.assertEqual(missing["links"], 0, "has_image 缺失不许留空链接：%s" % measured)
                    # onerror 一次带走 img + 外层 a
                    self.assertEqual(measured["imgsAfterError"], 0, measured)
                    self.assertEqual(measured["linksAfterError"], 0, measured)
                    self.assertTrue(measured["hasText"], "正文里的 [图片] 不能因此丢：%s" % measured)
                finally:
                    context.close()
            finally:
                browser.close()

    # —— 推送设置：键名与元素 id 的对照表 ——

    def _open_settings(self, context, page) -> None:
        open_authed_page(context, page, self.base, TOKEN)
        page.click("#tab-settings-button")
        # 点完先确认页签真的切过去了，再等具体字段；坏的时候报「设置页没出来」，不是干等 15 秒。
        page.wait_for_selector("#tab-settings", state="visible", timeout=15000)
        page.wait_for_selector("#push-settings", state="attached", timeout=15000)

    def test_url_token_exchanges_for_cookie_and_reloads(self) -> None:
        """带 `?token=` 首次访问：地址栏抹掉 token → 换成会话 Cookie → 自动重载一次。

        全站只有这一条真的走「换页」那条路：其余用例都用 `open_authed_page()` 预置 Cookie
        直接进页面（不换页，也就没有读到 reload 前初始态的竞态）。所以这里必须真验一次，
        否则「URL token 换 Cookie」这条路径就没人看着了。
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                page = context.new_page()
                try:
                    page.goto(self.base + "/?token=" + TOKEN + "&nowiz=1",
                              wait_until="domcontentloaded", timeout=20000)
                    # 等它自己换完备页：地址栏里已经没有 token，且文档就绪
                    page.wait_for_function(
                        "() => !location.search.includes('token=') && document.readyState === 'complete'",
                        timeout=20000)
                    self.assertNotIn("token=", page.url)
                    cookies = {c["name"]: c["value"] for c in context.cookies(self.base)}
                    self.assertIn(SESSION_COOKIE_NAME, cookies, cookies)
                    # Cookie 真的能过鉴权：之后不再带任何 token 也读得到受保护接口
                    status = page.evaluate("""async () => {
                        const r = await fetch('/api/setup/status', {credentials: 'same-origin'});
                        return r.status;
                    }""")
                    self.assertEqual(status, 200)
                finally:
                    context.close()
            finally:
                browser.close()

    def test_push_field_table_ids_all_exist(self) -> None:
        """对照表里每个元素 id 都必须在页面上真的存在。

        这里的 id 来自产品代码里的 `pushFields`（正则读出来的），所以只要有人改了输入框 id
        却没改表、或反过来，这条就会红。旧写法 `getElementById('push-'+name)` 让这种错配
        静默跳过，六格坏三格而 402 条测试全绿——这条就是补那个洞。
        """
        self.assertEqual(len(PUSH_FIELD_PAIRS), 6, "推送对照表应恰好六项：%r" % (PUSH_FIELD_PAIRS,))
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                page = context.new_page()
                try:
                    self._open_settings(context, page)
                    found = page.evaluate(
                        """(ids) => {
                             const out = {};
                             ids.forEach((id) => { out[id] = !!document.getElementById(id); });
                             return out;
                           }""",
                        [element_id for _key, element_id in PUSH_FIELD_PAIRS])
                    for key, element_id in PUSH_FIELD_PAIRS:
                        self.assertTrue(found.get(element_id),
                                        "对照表说 %r 对应 #%s，但页面上没有这个元素：%r"
                                        % (key, element_id, found))
                finally:
                    context.close()
            finally:
                browser.close()

    def test_push_settings_saves_all_six_fields_under_backend_keys(self) -> None:
        """六格各填一个值点保存：发给后端的必须是对照表的**键名**和输入框里的**值**。

        重点锁三个改过名的框（应用密钥 / 用户密钥 / 机器人地址）：改名后取值断了，
        用户填的东西发不出去，界面上还不报错。
        """
        posted: list = []
        real_settings = urllib.request.urlopen(urllib.request.Request(
            self.base + "/api/settings", headers={"X-Token": TOKEN}), timeout=10
        ).read().decode("utf-8")

        def settings_stub(route) -> None:
            if route.request.method == "POST":
                posted.append(route.request.post_data or "")
                body = json.dumps({"ok": True, "push": {}, "channels": ["wxpusher"]})
            else:
                body = real_settings
            route.fulfill(status=200, content_type="application/json", body=body)

        values = {
            "push-wxpusher_key": "wx-key-aaa",
            "push-wxpusher_uids": "UID_bbb",
            "push-wxpusher_topic_ids": "12345",
            "push-serverchan_keys": "SCT-ccc",
            "push-pushplus_key": "plus-key-ddd",
            "push-robot-urls": "https://example.com/robot?sign=eee",
        }
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                page = context.new_page()
                page.route("**/api/settings", settings_stub)
                try:
                    self._open_settings(context, page)
                    # 有的通道默认折叠着，用脚本赋值绕开「可见性」限制，测的仍是保存链路。
                    page.evaluate(
                        """(values) => {
                             Object.keys(values).forEach((id) => {
                               const input = document.getElementById(id);
                               if (input) input.value = values[id];
                             });
                           }""", values)
                    page.click("#push-save")
                    deadline = time.monotonic() + 10
                    while not posted and time.monotonic() < deadline:
                        page.wait_for_timeout(100)
                    self.assertTrue(posted, "点保存后没有发出 /api/settings 请求")
                    body = json.loads(posted[0])
                    self.assertEqual(body, {
                        "wxpusher_app_token": "wx-key-aaa",
                        "wxpusher_uids": "UID_bbb",
                        "wxpusher_topic_ids": "12345",
                        "serverchan_keys": "SCT-ccc",
                        "pushplus_tokens": "plus-key-ddd",
                        "webhook_urls": "https://example.com/robot?sign=eee",
                    }, "保存请求的键名/取值不对：%r" % (body,))
                finally:
                    context.close()
            finally:
                browser.close()

    def test_push_configured_placeholders_use_masked_state(self) -> None:
        """已配置的通道要显示「已配置：掩码（留空不修改）」，三个改过名的框同样要显示。"""
        meta_payload = json.loads(urllib.request.urlopen(urllib.request.Request(
            self.base + "/api/meta", headers={"X-Token": TOKEN}), timeout=10).read().decode("utf-8"))
        affirmed = {
            "wxpusher_app_token": "…aaaa",
            "pushplus_tokens": "…bbbb",
            "webhook_urls": "https://example.co…cccc",
        }
        meta_payload["push"] = {
            key: ({"set": True, "count": 1, "masked": [affirmed[key]]} if key in affirmed
                  else {"set": False, "count": 0, "masked": []})
            for key, _element_id in PUSH_FIELD_PAIRS
        }
        meta_payload["channels"] = ["wxpusher", "pushplus", "webhook"]
        body = json.dumps(meta_payload)

        def meta_stub(route) -> None:
            route.fulfill(status=200, content_type="application/json", body=body)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                page = context.new_page()
                page.route("**/api/meta", meta_stub)
                try:
                    self._open_settings(context, page)
                    deadline = time.monotonic() + 10
                    placeholders = {}
                    while time.monotonic() < deadline:
                        placeholders = page.evaluate(
                            """(ids) => {
                                 const out = {};
                                 ids.forEach((id) => {
                                   const input = document.getElementById(id);
                                   out[id] = input ? input.placeholder : null;
                                 });
                                 return out;
                               }""",
                            ["push-wxpusher_key", "push-pushplus_key", "push-robot-urls"])
                        if all((value or "").startswith("已配置：") for value in placeholders.values()):
                            break
                        page.wait_for_timeout(100)
                    for key, element_id in (("wxpusher_app_token", "push-wxpusher_key"),
                                            ("pushplus_tokens", "push-pushplus_key"),
                                            ("webhook_urls", "push-robot-urls")):
                        text = placeholders.get(element_id) or ""
                        self.assertIn("已配置：", text, "#%s 没显示已配置状态：%r" % (element_id, placeholders))
                        self.assertIn(affirmed[key], text, "#%s 没显示掩码：%r" % (element_id, placeholders))
                        self.assertIn("（留空不修改）", text, "#%s 少了留空提示：%r" % (element_id, placeholders))
                finally:
                    context.close()
            finally:
                browser.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()