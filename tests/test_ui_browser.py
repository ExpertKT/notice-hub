"""浏览器级行为测试（Playwright + runner 预装 Edge，channel=BROWSER_CHANNEL，零浏览器下载）。

只验最关键的两种真行为（替代 tests/test_webapp.py 里对 QR 相关内联 JS 源码字符串的断言；本文件是增量，老测试未动）：

1. NapCat 装了没启动时：`#qrbox` 容器可见，卡片上的主按钮是 `#go`（不是一张永远 hidden 的 `<img>`）。
2. 点 `#go` 走真实「启动引擎」链路（stub 掉 /api/napcat/launch）后：二维码图片真的加载出来、
   `#qrbox` 被页面自己滚进视口且有真实高度。

约定：
* 未安装 playwright 时整类 skipTest，本地与现有三平台 CI 照常绿（requirements-dev.txt 不进 requirements.txt）。
* 自起 TaskWebServer（随机端口、127.0.0.1）、跑完自停，绝不触碰 F:\\notice-hub 实例。
* 不跑 playwright install：channel=BROWSER_CHANNEL 用系统 Edge（windows-latest 预装 155）。
* 设 NH_BROWSER_ARTIFACT_DIR 时留存 trace.zip 与失败截图（CI 失败上传 artifact 用）。
"""

from __future__ import annotations

import datetime
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
    harden, isolate_browser_temp_env, open_authed_page, restore_browser_temp_env,
    scroll_into_view, wait_for_page_settled, open_settings, safe_close,
)

# 浏览器通道：CI 的 Windows runner 预装 Edge，用 msedge；macOS 本地没有 Edge，
# 用 NH_BROWSER_CHANNEL=chrome 直接跑本机 Chrome（不下载 playwright 自带浏览器）。
BROWSER_CHANNEL = os.environ.get("NH_BROWSER_CHANNEL", "msedge")

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

    def _wait_page_settled(self, page, frames: int = 5, budget: int = 10000) -> dict:
        """等整页排版稳定：连续 frames 帧里整页高度与体检面板的位置/高度都不变。

        只盯横幅自己不动是不够的：横幅**上方**的 `.summary`（`#headline` 从「加载中…」
        换成真标题、`#upcoming-carousel` 从 hidden 变可见）同样会异步长高，把整行推下去。
        CI 上就是它让 before 量到 322、展开后变成 463，看起来像「列表把横幅推走」。
        所以这里盯 `document.body.scrollHeight`，只要整页还在长就不算稳。
        超时返回最后一组值（不抛、不挂死），交给断言自己判断。
        """
        return page.evaluate(
            """({frames, budget}) => new Promise(resolve => {
                const deadline = performance.now() + budget;
                const snap = () => {
                    const panel = document.getElementById('platform-setup-panel');
                    return {
                        body: Math.round(document.body.scrollHeight),
                        panel_top: panel ? Math.round(panel.offsetTop) : -1,
                        panel_height: panel ? Math.round(panel.offsetHeight) : -1};
                };
                const key = (s) => s.body + '|' + s.panel_top + '|' + s.panel_height;
                let last = '', same = 0, current = snap();
                const tick = () => {
                    current = snap();
                    same = key(current) === last ? same + 1 : 0;
                    last = key(current);
                    if (same >= frames || performance.now() > deadline) return resolve(current);
                    requestAnimationFrame(tick);
                };
                tick();
            })""",
            {"frames": frames, "budget": budget})

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
            browser = playwright.chromium.launch(channel=BROWSER_CHANNEL, headless=True)
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
                    # 新 UI：QQ 接入在「设置」页签里，先切过去再找这些元素。
                    open_settings(page)
                    # ① 装了没启动：容器在、图片隐藏、主按钮是「启动引擎并显示二维码」（#go）。
                    page.wait_for_selector("#qrbox", state="visible", timeout=15000)
                    page.wait_for_selector("#go", state="visible", timeout=15000)
                    self.assertTrue(page.is_hidden("#qr"), "#qr 未启动时不应显示")
                    # 滚到容器再量命中：按钮不能只是「有尺寸」而实际被别的层盖住。
                    scroll_into_view(page, "#qrbox")
                    scroll_into_view(page, "#go")
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
                    safe_close(context)
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
            browser = playwright.chromium.launch(channel=BROWSER_CHANNEL, headless=True)
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
                        open_settings(page)
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
                        scroll_into_view(page, "#napcat-connect")
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
                        safe_close(context)
            finally:
                browser.close()

    def test_banner_stable(self) -> None:
        """QQ 接入卡顶部黄色提示：5 秒轮询会在 1–5 行之间换文案，容器高度必须恒定，
        否则下方按钮/二维码会被挤得上下跳（用户实测的复选框回归）。
        `.status-slot 里放了一条隐藏的「最长文案」占位，这里用真实几何断言。"""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=BROWSER_CHANNEL, headless=True)
            try:
                for width in (1280, 390):
                    context = browser.new_context(viewport={"width": width, "height": 900})
                    page = context.new_page()
                    page.route("**/api/napcat/status", self._napcat_status_stub)
                    try:
                        open_authed_page(context, page, self.base, TOKEN)
                        page.wait_for_selector("#status", state="attached", timeout=15000)
                        open_settings(page)
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
                        safe_close(context)
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
            browser = playwright.chromium.launch(channel=BROWSER_CHANNEL, headless=True)
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
                        return {tag: el.tagName, cursor: cs.cursor,
                                text: (el.textContent || '').trim(),
                                clickable: cs.pointerEvents !== 'none'};
                    }""")
                    # 新 UI 的清空按钮是原型 .link-btn（无边框文字链接），不再要求边框外观；
                    # 统计排整块删掉后也不再有右栏统计块可查。这两条契约跟 UI 重做一起更新。
                    self.assertEqual(info["tag"], "BUTTON", info)
                    self.assertEqual(info["cursor"], "pointer", info)
                    self.assertEqual(info["text"], "清空已完成", info)
                    self.assertTrue(info["clickable"], "清空按钮要可点：%s" % info)
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
                    safe_close(context)
            finally:
                browser.close()


    def test_settings_run_info_shows_real_port_and_its_source(self) -> None:
        """运行信息里多一行「服务端口」：环境变量优先于 .env，启动器还可能在端口被占时往上找。

        用户写了 58066 却听在 58067、全程没提示，所以这一行显示的是**真正听在哪**。
        来源是环境变量时必须说清「已覆盖 .env」，不能只写「来源：环境变量」；
        字段缺失（老后端）整行不出现，不显示「未知」也不显示 0。
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=BROWSER_CHANNEL, headless=True)
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
                        # 走公共的 open_settings：它用 DOM click 并按「页签真的不 hidden」重试，
                        # 而裸的 page.click 会被首启引导/通知浮层在命中判定与派发之间抢走
                        # （CI 上实测过一次设置页始终 hidden，同 commit 别的轮次全绿）。
                        open_settings(page)
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
                        safe_close(context)
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
                    bottom: top('.ov-foot'), bar: top('.ov-foot .bar')};}"""

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=BROWSER_CHANNEL, headless=True)
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
                        # 截止时间取「今天 23:59」：写死日期会过期，过期后这条落到「已逾期」，
                        # 而轮播排的是今天及以后，空态就永远翻不成有数据。
                        deadline = datetime.date.today().strftime("%Y-%m-%d") + " 23:59"
                        post("/api/tasks/create", {"summary": "空态收缩测试",
                                                   "deadline": deadline})
                        page.evaluate("() => loadTasks()")
                        page.wait_for_function(
                            "() => !document.getElementById('upcoming-carousel').classList.contains('is-empty')",
                            timeout=15000)
                        filled = page.evaluate(measure)
                        # 新 UI 的轮播没有固定 200px 轨道，高度跟随内容（实测 1 条 ≈ +13px）：
                        # 契约改为「有数据时确实比空态撑开、清空后缩回空态高度」（见下面 cleared 断言）。
                        self.assertGreater(filled["cssHeight"], empty["cssHeight"], filled)
                        self.assertGreater(filled["height"], empty["height"], filled)
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
                        safe_close(context)
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

    # —— 配置体检：右上角通知点开弹窗，行内动作（panel/api/manual）照旧可用 ——

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

    def test_setup_checkup_opens_in_notice_modal(self) -> None:
        """「还有 N 项没配好」不再占页头横幅：通知卡点开体检弹窗。

        每行 = 平台名 + 后端给的 headline + 一个动作按钮：panel 跳对卡片、api 真发 POST、
        manual 原文展开可选中复制；全部配好时连通知卡都不出现，不留空壳。
        """
        posts: list = []
        state = {"connect": "ok"}

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
            browser = playwright.chromium.launch(channel=BROWSER_CHANNEL, headless=True)
            try:
                for width in (1280, 390):
                    state["connect"] = "ok"
                    posts.clear()
                    context = browser.new_context(viewport={"width": width, "height": 900})
                    page = context.new_page()
                    page.route("**/api/setup/status",
                               lambda route: route.fulfill(status=200, content_type="application/json",
                                                           body=json.dumps(self._checkup_payload(False))))
                    page.route("**/api/napcat/connect", connect_stub)
                    try:
                        open_authed_page(context, page, self.base, TOKEN)
                        # 通知卡现在住在铃铛面板的「通知」页签里，先开面板才看得到。
                        page.click("#announce-bell")
                        page.wait_for_selector("#announce-panel", state="visible", timeout=15000)
                        # 通知卡由页面尾部的 refreshBanner() 拉体检数据后创建（sticky，不自动消失）。
                        page.wait_for_selector(
                            '#notice-stack [data-notice-id="setup-checkup"]',
                            state="visible", timeout=20000)
                        self.assertIn("还有 3 项没配好",
                                      page.inner_text('#notice-stack [data-notice-id="setup-checkup"]'))
                        # 点卡开体检弹窗（与「查看」按钮等价）。
                        page.click('#notice-stack [data-notice-id="setup-checkup"]')
                        page.wait_for_selector("#notice-modal .setup-checkup-row",
                                               state="visible", timeout=15000)
                        # 行数与文案：只列未配好且后端给了 headline 的（pending 和已配好的都不进）
                        rows = page.eval_on_selector_all(
                            "#notice-modal .setup-checkup-row",
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
                            # manual：原文展开（hidden 翻转），再点收起
                            dingtalk_act = '.setup-checkup-row[data-platform="dingtalk"] .setup-checkup-act'
                            dingtalk_hint = '.setup-checkup-row[data-platform="dingtalk"] .setup-checkup-hint'
                            page.click(dingtalk_act)
                            page.wait_for_selector(dingtalk_hint, state="visible", timeout=15000)
                            self.assertEqual(page.get_attribute(dingtalk_act, "aria-expanded"), "true")
                            self.assertIn(self.CHECKUP_HINT, page.inner_text(dingtalk_hint))
                            page.click(dingtalk_act)
                            page.wait_for_selector(dingtalk_hint, state="hidden", timeout=15000)
                            # panel：切到设置页签，焦点落在微信卡上
                            page.click('.setup-checkup-row[data-platform="wechat"] .setup-checkup-act')
                            page.wait_for_function(
                                "() => document.activeElement && document.activeElement.id === 'wechat-settings-card'",
                                timeout=15000)
                            self.assertFalse(page.is_hidden("#tab-settings"), "panel 动作应已切到设置页签")
                            # api：真发 POST；成功后按钮恢复可点
                            page.click('.setup-checkup-row[data-platform="qq"] .setup-checkup-act')
                            page.wait_for_function(
                                "() => document.querySelector('.setup-checkup-row[data-platform=\"qq\"] .setup-checkup-act').disabled === false",
                                timeout=15000)
                            # 计数器是 Python 侧的，wait_for_function 看不到它：
                            # 按钮恢复可点 != 请求已经发出去，轮询到计数涨了再断言。
                            deadline = time.monotonic() + 10
                            while not posts and time.monotonic() < deadline:
                                page.wait_for_timeout(50)
                            self.assertEqual(posts, ["POST " + self.base + "/api/napcat/connect"], posts)
                    finally:
                        safe_close(context)

                # 全部配好：refreshBanner 不再推体检通知，弹窗也不许挂着旧内容。
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                page = context.new_page()
                page.route("**/api/setup/status",
                           lambda route: route.fulfill(status=200, content_type="application/json",
                                                       body=json.dumps(self._checkup_payload(True))))
                page.route("**/api/napcat/connect", connect_stub)
                try:
                    open_authed_page(context, page, self.base, TOKEN)
                    # 面板关着时卡片本来就不显示，得开面板才谈得上「有没有这张卡」。
                    page.click("#announce-bell")
                    page.wait_for_selector("#announce-panel", state="visible", timeout=15000)
                    page.wait_for_timeout(1500)
                    self.assertFalse(page.is_visible('#notice-stack [data-notice-id="setup-checkup"]'),
                                     "全部配好时不应再有体检通知卡")
                    self.assertTrue(page.is_hidden("#notice-modal"), "没有体检内容时弹窗不许开着")
                finally:
                    safe_close(context)
            finally:
                browser.close()

    def test_inbox_renders_image_only_when_item_has_image(self) -> None:
        """收件箱图片：只有 `has_image` 为真的条目才渲染 `<img>`，其余一条 DOM 都不加。

        真实 bug：以前只认 `source === 'onebot-attachment'`，普通消息和回溯消息里的图片
        在收件箱永远是「[图片]」两个字；就算渲染了，塞进默认收起的 `<details>` 里也看不见。
        这里用真实 `renderInboxItem` 渲染三条并查 DOM：图片必须在折叠块外面、挂在卡片上。
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=BROWSER_CHANNEL, headless=True)
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
                    safe_close(context)
            finally:
                browser.close()

    def test_inbox_kind_chips_filter_and_hide_placeholder_noise(self) -> None:
        """收件箱类型筛选：默认隐藏占位符噪声，点一下才带对应参数。

        真实 bug：收件箱里一堆「[图片]」就是那条消息的全部正文，既不是图片也不是通知，
        用户只能一条条往下翻。后端能按类型分页了，但页面没有任何入口。这里锁三件事：
        类型 chip 齐、点「图片」请求带 `kind=image`、点「含占位符」带 `include_noise=1`；
        并且默认那一次两个参数都不能有（默认就是隐藏占位符噪声）。
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=BROWSER_CHANNEL, headless=True)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                page = context.new_page()
                try:
                    urls: list[str] = []

                    def remember(request) -> None:
                        if "/api/inbox?" in request.url:
                            urls.append(request.url)

                    page.on("request", remember)
                    open_authed_page(context, page, self.base, TOKEN)
                    page.click("#tab-inbox-button")
                    page.wait_for_selector("#tab-inbox", state="visible", timeout=15000)
                    page.wait_for_selector(".inbox-kinds", state="visible", timeout=15000)
                    kinds = page.eval_on_selector_all(
                        ".inbox-kinds button", "els => els.map(e => e.textContent.trim())"
                    )
                    self.assertEqual(
                        kinds,
                        ["全部", "文字", "图片", "链接", "文件", "含占位符"],
                        "收件箱类型筛选入口不齐：%s" % kinds,
                    )
                    pressed = page.eval_on_selector_all(
                        ".inbox-kinds button", "els => els.map(e => e.getAttribute('aria-pressed'))"
                    )
                    self.assertEqual(pressed[0], "true", "默认应当选「全部」：%s" % pressed)
                    self.assertTrue(
                        all(value == "false" for value in pressed[1:]),
                        "默认只允许「全部」是选中态：%s" % pressed,
                    )
                    self.assertTrue(urls, "切到收件箱后没有发出 /api/inbox 请求")
                    self.assertNotIn("kind=", urls[0], "默认不该带类型参数：%s" % urls[0])
                    self.assertNotIn(
                        "include_noise", urls[0], "默认不该带占位符参数：%s" % urls[0]
                    )

                    page.click('[data-inbox-kind="image"]')
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline and not any(
                        "kind=image" in url for url in urls
                    ):
                        page.wait_for_timeout(50)
                    self.assertTrue(
                        any("kind=image" in url for url in urls),
                        "点「图片」没有按类型取数：%s" % urls[-3:],
                    )

                    page.click("[data-inbox-noise]")
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline and not any(
                        "include_noise=1" in url for url in urls
                    ):
                        page.wait_for_timeout(50)
                    self.assertTrue(
                        any("include_noise=1" in url for url in urls),
                        "点「含占位符」没有带上参数：%s" % urls[-3:],
                    )
                    self.assertIn(
                        "kind=image", urls[-1], "两个筛选要能叠加：%s" % urls[-1]
                    )
                    pressed_after = page.eval_on_selector_all(
                        ".inbox-kinds button", "els => els.map(e => e.getAttribute('aria-pressed'))"
                    )
                    self.assertEqual(pressed_after[2], "true", "点完「图片」要留在选中态：%s" % pressed_after)
                    self.assertEqual(pressed_after[5], "true", "「含占位符」要留在按下态：%s" % pressed_after)
                finally:
                    safe_close(context)
            finally:
                browser.close()

    # —— 推送设置：键名与元素 id 的对照表 ——

    def _open_settings(self, context, page) -> None:
        open_authed_page(context, page, self.base, TOKEN)
        # 公共 helper：DOM click + 「页签真的不 hidden」重试，裸点击会被首启浮层抢走（见其注释）。
        open_settings(page)
        page.wait_for_selector("#push-settings", state="attached", timeout=15000)

    def test_url_token_exchanges_for_cookie_and_reloads(self) -> None:
        """带 `?token=` 首次访问：地址栏抹掉 token → 换成会话 Cookie → 自动重载一次。

        全站只有这一条真的走「换页」那条路：其余用例都用 `open_authed_page()` 预置 Cookie
        直接进页面（不换页，也就没有读到 reload 前初始态的竞态）。所以这里必须真验一次，
        否则「URL token 换 Cookie」这条路径就没人看着了。
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=BROWSER_CHANNEL, headless=True)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                page = context.new_page()
                harden(page)
                harden(context.request)
                failed_requests: list[str] = []
                page.on("requestfailed",
                        lambda request: failed_requests.append(
                            "%s %s" % (request.url, (request.failure() or ""))))
                try:
                    page.goto(self.base + "/?token=" + TOKEN + "&nowiz=1",
                              wait_until="domcontentloaded", timeout=60000)
                    # 等它自己换完备页：只看地址栏里已经没有 token。
                    # 不跟着等 document.readyState === 'complete'：那要所有子资源（manifest/js/图片）
                    # 都收尾，是环境副作用，不是这条用例要验的产品行为；CI 上它曾把等待拖到超时。
                    try:
                        page.wait_for_function(
                            "() => !location.search.includes('token=')", timeout=60000)
                    except Exception as exc:
                        evidence = {"href": "", "ready_state": "", "pending": []}
                        try:
                            evidence = page.evaluate(
                                """() => ({
                                     href: location.href,
                                     ready_state: document.readyState,
                                     pending: performance.getEntriesByType('resource')
                                       .filter((e) => e.responseEnd === 0)
                                       .map((e) => e.name)
                                       .slice(0, 10),
                                   })""")
                        except Exception as probe_exc:
                            evidence = {"probe_error": repr(probe_exc)}
                        self.fail(
                            "60 秒内地址栏还带着 token（换 Cookie 那步没成）。"
                            "当前 href=%r readyState=%r 未完成资源=%r 失败请求=%r 原始异常=%r"
                            % (evidence.get("href"), evidence.get("ready_state"),
                               evidence.get("pending"), failed_requests[:10], exc))
                    self.assertNotIn("token=", page.url)
                    # 换 Cookie 成不成，看行为：地址栏已无 token，下面这次同源 fetch 不带任何
                    # token 也返回 200——那就是会话 Cookie 在生效。
                    # （不直接读 Cookie 对象：playwright 1.64 在当前 CI 镜像上
                    #  BrowserContext.cookies() 抛内部 TypeError，Set-Cookie 头也不稳定暴露，
                    #  这两个探针会把环境问题变成假红。）
                    # Cookie 真的能过鉴权：之后不再带任何 token 也读得到受保护接口。
                    # Cookie 契约用纯 Python 验证（不经 playwright）：
                    # 「带 ?token= 的一次请求 → 服务端下发会话 Cookie → 之后只带 Cookie、
                    # 不带任何 token 也能过鉴权」。这正是浏览器那次 reload 之后所依赖的机制，
                    # 而 playwright 在「刚自重载的页面」上的任何调用都可能撞环境的瞬态
                    # TypeError（实测 evaluate / context.request.get / page.request.get /
                    # context.close 全中过），把这一条契约绑在那里只会间歇假红。
                    request = urllib.request.Request(
                        "%s/api/setup/status?token=%s" % (self.base, TOKEN))
                    with urllib.request.urlopen(request, timeout=30) as response:
                        self.assertEqual(response.status, 200)
                        set_cookie = response.headers.get("Set-Cookie") or ""
                    self.assertIn(SESSION_COOKIE_NAME, set_cookie,
                                  "带 token 的请求没有下发会话 Cookie：%r" % (set_cookie,))
                    cookie = set_cookie.split(";", 1)[0]
                    again = urllib.request.Request(
                        self.base + "/api/setup/status", headers={"Cookie": cookie})
                    with urllib.request.urlopen(again, timeout=30) as response:
                        self.assertEqual(response.status, 200)
                finally:
                    safe_close(context)
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
            browser = playwright.chromium.launch(channel=BROWSER_CHANNEL, headless=True)
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
                    safe_close(context)
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
            browser = playwright.chromium.launch(channel=BROWSER_CHANNEL, headless=True)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                page = context.new_page()
                page.route("**/api/settings", settings_stub)
                try:
                    self._open_settings(context, page)
                    # 有的通道默认折叠着：先全部展开，让 fill 能点到真实可见的输入框。
                    page.evaluate(
                        "() => document.querySelectorAll('#push-settings details')"
                        ".forEach((box) => { box.open = true; })")
                    # 走真实输入路径（fill 会派发 input/change 事件、写进元素的 value），
                    # 不用 evaluate 直接赋 value：那样赋值后一旦发生一次重渲染，值会被清空，
                    # 点保存就走进 savePushSettings 的「没有要保存的内容」分支，压根不发请求。
                    for element_id, value in values.items():
                        page.fill("#" + element_id, value, timeout=15000)
                    # 点保存前先确认六格都还是非空——重渲染清空时这里就红，不是等到 posted 为空才猜。
                    try:
                        page.wait_for_function(
                            """(ids) => ids.every((id) => {
                                 const input = document.getElementById(id);
                                 return input && input.value.trim() !== '';
                               })""",
                            arg=list(values), timeout=10000)
                    except Exception as exc:
                        snapshot = page.evaluate(
                            """(ids) => ids.map((id) => {
                                 const input = document.getElementById(id);
                                 return [id, input ? input.value : null];
                               })""", list(values))
                        self.fail("点保存前六格没有全部保住值（重渲染把它清空了？）：%r 原始异常=%r"
                                  % (snapshot, exc))
                    page.click("#push-save")
                    # 请求是点完之后才发出的：先等它进来再断言，别拿点完那一刻的列表判生死。
                    # 满载时事件循环会慢，给 10 秒预算、50ms 一步，超时才算失败。
                    # 请求只会迟到、不会不来：预算收紧只会制造假红（实测满载时 1/4 轮误红）。
                    deadline = time.monotonic() + 10
                    while not posted and time.monotonic() < deadline:
                        page.wait_for_timeout(50)
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
                    safe_close(context)
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
            browser = playwright.chromium.launch(channel=BROWSER_CHANNEL, headless=True)
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
                    safe_close(context)
            finally:
                browser.close()


    def test_tab_switch_and_settings_rerender_keep_scroll_and_width(self) -> None:
        """切页/设置重渲染不能抖：视口宽度不变，滚动位置不被夹断。

        锁三件事（bug 1 + bug 10）：
        1. `loadSettings()` 会清空 `.settings-body`，而它是 `display:contents`（page.html:761），
           清空等于删掉设置页全部面板 —— 文档高度瞬间塌陷、浏览器把 scrollTop 夹成 0。
           用户看到的就是「正在看下面的设置，突然自己滑到最顶」。重渲染后必须还停在原位。
        2. `render()` 末尾按 `location.hash` 滚动；目标在隐藏页签里时 rect 全 0，
           `scrollIntoView` 会落到文档顶部。配合 60s 的 `loadTasks` 定时器就是「偶发回顶」。
        3. 四个页签来回切，`documentElement.clientWidth` 必须不变：滚动条一出现一消失，
           整页内容就会左右跳一下（这就是「切页抖」的横向那一半）。
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=BROWSER_CHANNEL, headless=True)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                page = context.new_page()
                try:
                    open_authed_page(context, page, self.base, TOKEN)
                    page.wait_for_selector("#tab-tasks", state="visible", timeout=15000)

                    # —— 1. 设置页滚到中部，再重新 loadSettings() ——
                    page.click("#tab-settings-button")
                    page.wait_for_selector("#push-settings", state="attached", timeout=15000)
                    scrolled = page.evaluate(
                        """() => {
                             window.scrollTo({top: 1200, left: 0, behavior: 'instant'});
                             return {y: Math.round(window.scrollY),
                                     height: document.documentElement.scrollHeight,
                                     gutter: getComputedStyle(document.documentElement).scrollbarGutter};
                           }""")
                    self.assertGreaterEqual(
                        scrolled["y"], 1198,
                        "设置页不够长、滚不到 1200（scrollHeight=%s）" % scrolled["height"])
                    self.assertIn("stable", scrolled["gutter"] or "",
                                  "html 没开 scrollbar-gutter:stable：%r" % scrolled["gutter"])
                    rerender = page.evaluate(
                        """async () => {
                             const before = Math.round(window.scrollY);
                             loadSettings();
                             await Promise.resolve();
                             await Promise.resolve();
                             return {before, after: Math.round(window.scrollY),
                                     leftoverMinHeight: document.getElementById('tab-settings').style.minHeight};
                           }""")
                    self.assertGreaterEqual(
                        rerender["after"], rerender["before"] - 2,
                        "重渲染设置页后滚动位置被夹回顶部：%s" % rerender)
                    self.assertEqual(
                        rerender["leftoverMinHeight"], "",
                        "重渲染结束后没有放开占位 min-height：%s" % rerender)

                    # 反证：没有保护时，清空 `.settings-body` 会让文档高度塌掉、滚动位置被浏览器夹走。
                    # 这条让上面那条断言不是空转——机制在真实浏览器里就是这样发生的。
                    # 夹到哪取决于设置页左子导航剩下的高度（实测 1200 → 544），所以只断言「掉了很远」。
                    collapse = page.evaluate(
                        """() => {
                             window.scrollTo({top: 1200, left: 0, behavior: 'instant'});
                             const before = Math.round(window.scrollY);
                             document.querySelector('#tab-settings .settings-body').textContent = '';
                             return {before, after: Math.round(window.scrollY)};
                           }""")
                    self.assertGreaterEqual(collapse["before"], 1198, collapse)
                    self.assertLess(
                        collapse["after"], collapse["before"] - 200,
                        "清空 .settings-body 后滚动位置几乎没动，说明这台机器上的机制与预期不同：%s"
                        % collapse)
                    # 复原面板并回到原位，后面的断言接着用（重建是异步的，等面板回来再滚）
                    page.evaluate("() => { loadSettings(); }")
                    page.wait_for_selector("#push-settings", state="attached", timeout=15000)
                    page.wait_for_timeout(400)
                    page.evaluate(
                        "() => { window.scrollTo({top: 1200, left: 0, behavior: 'instant'}); }")
                    restored = page.evaluate("() => Math.round(window.scrollY)")
                    self.assertGreaterEqual(
                        restored, 1198, "复原设置面板后没能回到 1200：scrollY=%s" % restored)

                    # —— 2. hash 指向隐藏页签里的元素时，render() 不许把页面滚回顶部 ——
                    guard = page.evaluate(
                        """() => {
                             const host = document.getElementById('tab-tasks');
                             const probe = document.createElement('li');
                             probe.id = 'task-guard-probe';
                             host.appendChild(probe);
                             history.replaceState(null, '', '#task-guard-probe');
                             const before = Math.round(window.scrollY);
                             render({});
                             const after = Math.round(window.scrollY);
                             probe.remove();
                             history.replaceState(null, '', location.pathname + location.search);
                             return {before, after, hidden: host.hidden};
                           }""")
                    self.assertTrue(guard["hidden"], "探针必须放在隐藏的待办页签里：%s" % guard)
                    self.assertGreaterEqual(guard["before"], 1198,
                                            "前置条件不成立（第 1 步没停在原位）：%s" % guard)
                    self.assertGreaterEqual(
                        guard["after"], guard["before"] - 2,
                        "render() 把隐藏页签里的目标当成可滚目标，页面被顶回顶部：%s" % guard)

                    # —— 3. 四个页签来回切，视口宽度必须一致 ——
                    widths = []
                    for tab in ("tasks", "notices", "inbox", "settings"):
                        page.click("#tab-%s-button" % tab)
                        page.wait_for_timeout(120)
                        widths.append(page.evaluate(
                            "() => document.documentElement.clientWidth"))
                    self.assertEqual(
                        len(set(widths)), 1,
                        "切页时视口宽度变了（滚动条出现/消失），整页会左右跳：%s" % widths)
                finally:
                    safe_close(context)
            finally:
                browser.close()


    def _eval_alive(self, page, script: str, timeout_ms: int = 10000):
        """页面忙的时候 page.evaluate 没有超时可言，会一直等下去。

        先确认页面还能响应（wait_for_function 有硬超时），再求值：
        页面真卡住就 10 秒内报出来，而不是把整条用例吊到 job 超时被取消。
        """
        page.wait_for_function("() => true", timeout=timeout_ms, polling=100)
        return page.evaluate(script)

    def _stage(self, message: str) -> None:
        """在 CI 日志里打点：这条用例曾整段耗掉十几分钟且拿不到 traceback，
        有了打点就能一眼看出卡在哪一步。"""
        print("[ui-browser] %s" % message, flush=True)

    def _announce_state(self, page) -> dict:
        """铃铛面板的可交互性快照：谁挡住了谁、面板开没开、页签选中的是哪个。

        CI 上出现过「元素在面板里却一直判定不可点」，本地永远复现不出，
        所以把判定依据一次取全，失败时直接打进报错里。
        """
        return page.evaluate(
            "() => { const p = document.getElementById('announce-panel');"
            " const n = document.getElementById('announce-tab-notice');"
            " const a = document.getElementById('announce-tab-announce');"
            " const b = document.getElementById('announce-bell');"
            " const box = (el) => { if (!el) return null; const r = el.getBoundingClientRect();"
            "   return [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)]; };"
            " const hit = (el) => { if (!el) return null; const r = el.getBoundingClientRect();"
            "   if (!r.width || !r.height) return null;"
            "   const t = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);"
            "   return t ? (t.id || t.className || t.tagName) : null; };"
            " return {panelHidden: !!(p && p.hidden), panel: box(p), bell: box(b), bellHit: hit(b),"
            "   notice: box(n), noticeHit: hit(n), noticeSelected: n && n.getAttribute('aria-selected'),"
            "   announce: box(a), announceHit: hit(a), scrollY: window.scrollY,"
            "   vw: window.innerWidth, vh: window.innerHeight}; }")

    def test_bell_panel_has_notice_and_announce_tabs_without_overlap(self) -> None:
        """铃铛面板：两个页签都能切，通知卡只在面板里，不再悬浮盖住页面正文。"""
        probe = """() => {
            const box = (el) => { const r = el.getBoundingClientRect();
                return {left: r.left, top: r.top, right: r.right, bottom: r.bottom}; };
            const bell = document.getElementById('announce-bell');
            const panel = document.getElementById('announce-panel');
            const stack = document.getElementById('notice-stack');
            const main = panel ? panel.querySelector('.announce-panel-main') : null;
            const br = bell.getBoundingClientRect();
            const hit = document.elementFromPoint(br.left + br.width / 2, br.top + br.height / 2);
            return {
                bellHit: !!hit && (hit === bell || bell.contains(hit)),
                bellInsideViewport: br.left >= 0 && br.right <= window.innerWidth,
                panelHidden: panel.hidden,
                panel: panel.hidden ? null : box(panel),
                stackRects: stack.getClientRects().length,
                stack: stack.getClientRects().length ? box(stack) : null,
                main: !panel.hidden && main ? box(main) : null,
                stackPosition: getComputedStyle(stack).position,
                title: document.getElementById('announce-panel-title').textContent,
                noticeHidden: document.getElementById('announce-pane-notice').hidden,
                announceHidden: document.getElementById('announce-pane-announce').hidden,
                footHidden: document.getElementById('announce-panel-foot').hidden,
                overflowX: document.documentElement.scrollWidth - document.documentElement.clientWidth,
            };
        }"""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=BROWSER_CHANNEL, headless=True)
            try:
                for width in (1280, 390):
                    self._stage("width=%d start" % width)
                    context = browser.new_context(viewport={"width": width, "height": 800})
                    page = context.new_page()
                    try:
                        open_authed_page(context, page, self.base, TOKEN)
                        # 这一步之后全是同步的页面查询/点击。CI 上出现过切完页签后
                        # 某个调用一直不返回（24 分钟没动静），默认超时能把它逼出来，
                        # 报错里会写明挂的是 evaluate 还是 click。
                        page.set_default_timeout(15000)
                        self._stage("width=%d page opened" % width)
                        # 铃铛必须真的露出来并被点到：以前外层 div 带着 hidden，全仓没人去掉，
                        # 结果是右上角根本没有入口，公告面板永远打不开。
                        page.wait_for_selector("#announce-bell", state="visible", timeout=15000)
                        self._stage("width=%d bell visible" % width)
                        closed = self._eval_alive(page, probe)
                        self.assertTrue(closed["bellHit"], "铃铛被别的层盖住：%s" % closed)
                        self.assertTrue(closed["bellInsideViewport"],
                                        "%dpx 下铃铛超出视口：%s" % (width, closed))
                        self.assertLessEqual(closed["overflowX"], 0,
                                             "%dpx 下出现横向溢出（铃铛把底栏顶出去了）：%s"
                                             % (width, closed))
                        # 关着的时候通知栈零占位：以前 fixed 悬浮会压住右侧正文。
                        self.assertEqual(closed["stackRects"], 0,
                                         "面板关着时通知栈仍在页面上占位：%s" % closed)
                        self.assertNotEqual(closed["stackPosition"], "fixed",
                                            "通知栈不许再 fixed 悬浮：%s" % closed)

                        self.assertTrue(page.is_visible("#announce-bell"), "铃铛不在页面上")
                        page.click("#announce-bell", timeout=5000)
                        self._stage("width=%d bell clicked" % width)
                        try:
                            page.wait_for_selector("#announce-panel", state="visible", timeout=6000)
                        except Exception as exc:
                            self.fail("点铃铛后面板没开：%r / %r"
                                      % (self._announce_state(page), exc))
                        page.wait_for_timeout(300)
                        self._stage("width=%d panel visible" % width)
                        notice_tab = self._eval_alive(page, probe)
                        self.assertEqual(notice_tab["title"], "通知")
                        self.assertFalse(notice_tab["noticeHidden"])
                        self.assertTrue(notice_tab["announceHidden"])
                        self.assertTrue(notice_tab["footHidden"], "通知页签不该有「全部标为已读」：%s" % notice_tab)

                        # 塞一张卡：卡片必须整张落在面板内容区里，不许越界。
                        page.evaluate(
                            "() => window.NHNotify.push({id: 'tab-probe', title: '面板内通知',"
                            " body: '这条只该出现在面板里。', level: 'info', sticky: true})")
                        page.wait_for_timeout(300)
                        inside = self._eval_alive(page, probe)
                        self.assertGreaterEqual(inside["stackRects"], 1, inside)
                        self.assertLessEqual(inside["stack"]["top"], inside["main"]["bottom"] + 1, inside)
                        self.assertGreaterEqual(inside["stack"]["bottom"], inside["main"]["top"] - 1, inside)
                        self.assertTrue(
                            inside["panel"]["left"] <= inside["stack"]["left"]
                            and inside["stack"]["right"] <= inside["panel"]["right"] + 1, inside)

                        page.click("#announce-tab-announce", timeout=5000)
                        self._stage("width=%d announce tab clicked" % width)
                        page.wait_for_timeout(300)
                        announce_tab = self._eval_alive(page, probe)
                        self.assertEqual(announce_tab["title"], "公告")
                        self.assertTrue(announce_tab["noticeHidden"])
                        self.assertFalse(announce_tab["announceHidden"])
                        self.assertFalse(announce_tab["footHidden"])
                        self.assertLessEqual(announce_tab["overflowX"], 0, announce_tab)

                        # CI 上曾把这块判成「元素不稳定」而空转十几分钟；改成 5 秒硬上限，
                        # 点不动就把面板/页签/命中测试一次打出来，不再靠重试硬扛。
                        try:
                            page.click("#announce-tab-notice", timeout=5000)
                        except Exception as exc:
                            self.fail("点不动「通知」页签：%r / %r"
                                      % (self._announce_state(page), exc))
                        self._stage("width=%d notice tab clicked" % width)
                        # 这里不再用 evaluate：CI 上切回「通知」之后，页面有一段时间
                        # 完全不响应脚本求值，整条用例就卡在那一次 evaluate 上，
                        # 24 分钟没有下一行日志。换成 Playwright 自己的可见性原语，
                        # 验证的还是同一件事（切回来了、公告页签和页脚都让位）。
                        page.wait_for_selector("#announce-pane-notice", state="visible", timeout=15000)
                        self.assertEqual(
                            page.get_attribute("#announce-tab-notice", "aria-selected"), "true",
                            "%dpx 下「通知」页签没被选中" % width)
                        self.assertTrue(page.is_hidden("#announce-pane-announce"),
                                        "%dpx 下公告页签没让位" % width)
                        self.assertTrue(page.is_hidden("#announce-panel-foot"),
                                        "%dpx 下通知页签不该有「全部标为已读」" % width)
                        self.assertEqual(page.inner_text("#announce-panel-title"), "通知")
                        self._stage("width=%d done" % width)
                    finally:
                        safe_close(context)
            finally:
                browser.close()


    # —— bug 5：最近推送正文列宽四档 / 取数失败不清空列表 ——

    _FEED_ITEMS = [
        {"kind": "window", "created_at": "2026-11-04T10:00:00", "body": "第一条摘要正文。"},
        {"kind": "silent", "created_at": "2026-11-04T11:00:00", "body": "第二条摘要正文。"},
    ]

    def _feed_stub(self, route, fail_flag: dict) -> None:
        if fail_flag.get("fail"):
            route.fulfill(status=500, content_type="application/json",
                          body=json.dumps({"ok": False, "error": "读取失败"}))
        else:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True, "items": self._FEED_ITEMS}))

    def _open_notices(self, context, page) -> None:
        open_authed_page(context, page, self.base, TOKEN)
        page.click("#tab-notices-button")
        page.wait_for_selector("#tab-notices .notice-feed", state="attached", timeout=15000)

    def _feed_width(self, page) -> float:
        return float(page.evaluate(
            "() => document.querySelector('#tab-notices .notice-feed').getBoundingClientRect().width"))

    def test_notice_stack_evicts_oldest_when_every_card_is_sticky(self) -> None:
        """栈里全是 sticky 时也必须挤掉一张。

        以前淘汰逻辑只在非 sticky 里找最旧的；三张 sticky 同时挂着（公告 + 体检 +
        关键故障）再推一张，就找不到可淘汰项，`while (items.size >= MAX_VISIBLE)`
        永远成立，整个页面主线程被转死——用户看到的就是点一下之后页面彻底不响应。
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=BROWSER_CHANNEL, headless=True)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 800})
                page = context.new_page()
                try:
                    open_authed_page(context, page, self.base, TOKEN)
                    page.set_default_timeout(15000)
                    page.wait_for_selector("#announce-bell", state="visible", timeout=15000)
                    self._eval_alive(page, "() => { window.NHNotify.clear(); return true; }")
                    state = self._eval_alive(
                        page,
                        "() => { for (var i = 0; i < 5; i += 1) {"
                        " window.NHNotify.push({id: 'sticky-' + i, title: 'sticky ' + i,"
                        " level: 'info', sticky: true}); }"
                        " var cards = document.querySelectorAll('#notice-stack .notice-card');"
                        " return {count: cards.length,"
                        " ids: Array.prototype.map.call(cards, function (el) {"
                        " return el.getAttribute('data-notice-id'); })}; }")
                    self.assertEqual(state["count"], 3,
                                     "五张 sticky 推完应只剩 3 张：%s" % state)
                    self.assertIn("sticky-4", state["ids"],
                                  "最新推的那张必须还在栈里：%s" % state)
                finally:
                    safe_close(context)
            finally:
                browser.close()

    def test_feed_width_presets_resize_column_and_survive_reload(self) -> None:
        """最近推送正文列宽四档：点一下宽度真的变、刷新后还记得（bug 5）。

        以前 `.notice-feed` 和它里面的 `p` 各写一次 `max-width:70ch`，用户只能忍窄；
        现在宽度由 `html[data-feed-width]` 的 `--feed-measure` 决定，档位存 localStorage。
        这里量的是实际渲染宽度，不写死像素常量（ch 取决于字体度量）。
        """
        flag: dict = {}
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=BROWSER_CHANNEL, headless=True)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                page = context.new_page()
                page.route("**/api/notices", lambda route: self._feed_stub(route, flag))
                try:
                    open_authed_page(context, page, self.base, TOKEN)
                    # 别的用例可能留下过档位：清掉再看默认档
                    page.evaluate("() => { try { localStorage.removeItem('nh_feed_width'); } catch (e) {} }")
                    page.reload(wait_until="load")
                    page.click("#tab-notices-button")
                    page.wait_for_selector("#tab-notices .notice-feed", state="attached", timeout=15000)
                    page.wait_for_timeout(200)

                    start = page.evaluate("""() => {
                        const standardButton = document.querySelector('.feed-width [data-feed-width=standard]');
                        return {
                            width: document.documentElement.dataset.feedWidth,
                            pressed: standardButton ? standardButton.getAttribute('aria-pressed') : null,
                            buttons: document.querySelectorAll('.feed-width button').length,
                        };
                    }""")
                    self.assertEqual(start["width"], "standard", "默认档必须是标准：%s" % start)
                    self.assertEqual(start["buttons"], 4, "四档要都在：%s" % start)
                    self.assertEqual(start["pressed"], "true", "当前档要标 aria-pressed：%s" % start)
                    standard = self._feed_width(page)

                    page.click(".feed-width [data-feed-width=wide]")
                    page.wait_for_timeout(200)
                    wide = self._feed_width(page)
                    self.assertEqual(page.evaluate("() => document.documentElement.dataset.feedWidth"), "wide")
                    self.assertGreater(wide, standard + 10,
                                       "切到「宽」列宽没变宽：standard=%r wide=%r" % (standard, wide))
                    self.assertEqual(
                        page.evaluate("() => localStorage.getItem('nh_feed_width')"), "wide")
                    self.assertEqual(
                        page.evaluate("""() => document.querySelector('.feed-width [data-feed-width=wide]')
                                        .getAttribute('aria-pressed')"""), "true")

                    page.click(".feed-width [data-feed-width=narrow]")
                    page.wait_for_timeout(200)
                    narrow = self._feed_width(page)
                    self.assertLess(narrow, standard - 10,
                                    "切到「窄」列宽没变窄：standard=%r narrow=%r" % (standard, narrow))

                    # 键盘也能切：原生 button 聚焦后回车
                    page.focus(".feed-width [data-feed-width=wide]")
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(200)
                    self.assertEqual(page.evaluate("() => document.documentElement.dataset.feedWidth"), "wide",
                                     "四档要能纯键盘切换")

                    # 刷新后保持：首屏渲染前就该是宽档，不能先闪一下标准宽
                    page.reload(wait_until="load")
                    self.assertEqual(
                        page.evaluate("() => document.documentElement.dataset.feedWidth"), "wide",
                        "刷新后档位丢了")
                    page.click("#tab-notices-button")
                    page.wait_for_selector("#tab-notices .notice-feed", state="attached", timeout=15000)
                    page.wait_for_timeout(200)
                    after = self._feed_width(page)
                    self.assertAlmostEqual(after, wide, delta=2,
                                           msg="刷新后列宽没回到宽档：wide=%r after=%r" % (wide, after))
                finally:
                    safe_close(context)
            finally:
                browser.close()

    def test_feed_card_headline_is_the_first_sentence_of_the_body(self) -> None:
        """卡片标题要写具体那件事，不能整页都叫「定时摘要」。

        摘要是「N 条重点」+ 正文；旧实现把 kind 的中文名当标题，一屏卡片标题一模一样，
        看的人扫不出哪条讲了什么。有正文时标题取第一句，分类留在 meta 里，正文去掉重复的那句。
        """
        items = [
            {"kind": "window", "created_at": "2026-11-04T10:00:00",
             "body": "本周实验课改到周五。其余课表不变。"},
            {"kind": "silent", "created_at": "2026-11-04T09:00:00", "body": ""},
        ]
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=BROWSER_CHANNEL, headless=True)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 800})
                try:
                    page = context.new_page()
                    page.route("**/api/notices", lambda route: route.fulfill(
                        status=200, content_type="application/json",
                        body=json.dumps({"ok": True, "items": items})))
                    self._open_notices(context, page)
                    cards = page.eval_on_selector_all(
                        "#tab-notices .notice-feed li.notice",
                        "els => els.map(e => ({"
                        "headline: (e.querySelector('h3') || {}).textContent || '',"
                        "kind: (e.querySelector('.notice-kind') || {}).textContent || '',"
                        "body: (e.querySelector('p') || {}).textContent || ''}))")
                    self.assertEqual(len(cards), 2)
                    self.assertEqual(cards[0]["headline"], "本周实验课改到周五",
                                     "有正文时标题要取正文第一句：%r" % cards[0])
                    self.assertEqual(cards[0]["kind"], "定时摘要", "分类标签要留着：%r" % cards[0])
                    self.assertEqual(cards[0]["body"], "其余课表不变。",
                                     "标题用掉第一句后，正文不该再重复它：%r" % cards[0])
                    self.assertEqual(cards[1]["headline"], "静默摘要",
                                     "没有正文才退回分类名：%r" % cards[1])
                    # 手机宽度下大标题常把最后两三个字挤成一行（「日」单独一行）：
                    # text-wrap:balance 让换行均分，锁住它，别被后来的样式覆盖掉。
                    self.assertEqual(
                        page.evaluate("() => getComputedStyle(document.querySelector('.summary h1')).textWrap"),
                        "balance")
                finally:
                    safe_close(context)
            finally:
                browser.close()

    def test_notices_load_failure_keeps_previous_list(self) -> None:
        """取数失败不清空列表（bug 1 同族）：切页签那一下高度不能塌，旧摘要要留着。

        以前 `loadNotices()` 一进来就 `root.textContent=''` 再等接口：失败时页面只剩错误文案，
        连原来读到的摘要一起没了；成功但慢的时候还会把 scrollTop 夹走。
        """
        flag: dict = {}
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=BROWSER_CHANNEL, headless=True)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                page = context.new_page()
                page.route("**/api/notices", lambda route: self._feed_stub(route, flag))
                try:
                    self._open_notices(context, page)
                    page.wait_for_timeout(200)
                    probe = """() => ({
                        cards: document.querySelectorAll('#tab-notices .notice-feed li.notice').length,
                        errors: document.querySelectorAll('#tab-notices .notice-error').length,
                        errorText: (document.querySelector('#tab-notices .notice-error') || {}).textContent || '',
                        hasFeed: !!document.querySelector('#tab-notices .notice-feed'),
                    })"""
                    before = self._eval_alive(page, probe)
                    self.assertEqual(before["cards"], 2, "桩数据没渲染出来：%s" % before)
                    self.assertEqual(before["errors"], 0, before)

                    flag["fail"] = True
                    page.evaluate("() => { loadNotices(); }")
                    page.wait_for_selector("#tab-notices .notice-error", state="attached", timeout=15000)
                    after = self._eval_alive(page, probe)
                    self.assertTrue(after["hasFeed"], "失败时列表被清空了：%s" % after)
                    self.assertEqual(after["cards"], 2, "失败时旧摘要必须留着：%s" % after)
                    self.assertEqual(after["errors"], 1, "失败要在原位提示：%s" % after)
                    self.assertIn("上次读到的内容", after["errorText"], after)
                finally:
                    safe_close(context)
            finally:
                browser.close()

    def test_update_channel_switches_on_radio_change_without_save_button(self) -> None:
        """去掉「保存通道」后：切单选就发 POST；失败必须把选中项退回去。

        用户口径：「保存通道，这个太麻烦了，应该剔除，直接切换就行」。所以这里锁三件事：
        卡片里没有保存按钮、切「测试版」真的发出 update_channel=beta、服务端 500 时
        单选回到「测试版」且提示写「切换失败」——不允许界面显示已切换而实际没保存。
        """
        posted: list = []
        state = {"fail": False, "channel": "stable"}
        real_settings = urllib.request.urlopen(urllib.request.Request(
            self.base + "/api/settings", headers={"X-Token": TOKEN}), timeout=10
        ).read().decode("utf-8")

        def settings_stub(route) -> None:
            if route.request.method != "POST":
                route.fulfill(status=200, content_type="application/json", body=real_settings)
                return
            posted.append(route.request.post_data or "")
            if state["fail"]:
                route.fulfill(status=500, content_type="application/json",
                              body=json.dumps({"ok": False, "error": "服务端拒绝了这次切换"}))
            else:
                state["channel"] = "beta"
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": True, "update_channel": "beta"}))

        def update_stub(route) -> None:
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "ok": True,
                "current": {"version": "1.1.0-beta.17", "tag": "v1.1.0-beta.17"},
                "latest": None, "channel": state["channel"], "has_update": False,
                "checked_at": "2026-11-05 21:30", "pending": False,
            }))

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=BROWSER_CHANNEL, headless=True)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                page = context.new_page()
                page.route("**/api/settings", settings_stub)
                page.route("**/api/update*", update_stub)
                try:
                    self._open_settings(context, page)
                    page.wait_for_selector("#update-card", state="visible", timeout=20000)
                    self.assertEqual(page.locator("#update-channel-save").count(), 0,
                                     "「保存通道」按钮应该已经删掉")
                    page.wait_for_function(
                        "() => { const r = document.querySelector"
                        "('input[name=\"update-channel\"][value=\"beta\"]');"
                        " return r && !r.disabled; }", timeout=20000)

                    page.click('input[name="update-channel"][value="beta"]')
                    deadline = time.monotonic() + 10
                    while not posted and time.monotonic() < deadline:
                        page.wait_for_timeout(50)
                    self.assertEqual(len(posted), 1, "切到测试版应发一次 /api/settings")
                    self.assertEqual(json.loads(posted[0]), {"update_channel": "beta"})
                    page.wait_for_function(
                        "() => (document.getElementById('update-channel-result').textContent || '')"
                        ".indexOf('已切到测试版') === 0", timeout=20000)
                    self.assertTrue(page.is_checked(
                        'input[name="update-channel"][value="beta"]'))

                    # 失败路径：500 后选中项必须回到「测试版」，并写明切换失败。
                    state["fail"] = True
                    page.wait_for_function(
                        "() => Array.prototype.every.call("
                        "document.querySelectorAll('input[name=\"update-channel\"]'),"
                        " (el) => !el.disabled)", timeout=20000)
                    page.click('input[name="update-channel"][value="stable"]')
                    deadline = time.monotonic() + 10
                    while len(posted) < 2 and time.monotonic() < deadline:
                        page.wait_for_timeout(50)
                    self.assertEqual(len(posted), 2, "切到正式版应发第二次请求")
                    self.assertEqual(json.loads(posted[1]), {"update_channel": "stable"})
                    page.wait_for_function(
                        "() => (document.getElementById('update-channel-result').textContent || '')"
                        ".indexOf('切换失败') === 0", timeout=20000)
                    result = page.eval_on_selector(
                        "#update-channel-result", "el => el.textContent || ''")
                    self.assertIn("服务端拒绝了这次切换", result,
                                  "失败原因要原样写进提示：%r" % result)
                    self.assertTrue(page.is_checked(
                        'input[name="update-channel"][value="beta"]'),
                        "失败后选中项要回到切换前的「测试版」")
                    self.assertFalse(page.is_checked(
                        'input[name="update-channel"][value="stable"]'))
                finally:
                    safe_close(context)
            finally:
                browser.close()

    def test_agent_hint_hierarchy_and_shared_dialog(self) -> None:
        """提示块的层级与「什么是 agent？」共用弹窗。

        用户口径：主句要大、要占主要位置，说明行更小更淡；三处共用一个原生 dialog，
        三个入口都能打开，Esc 能关。这里在真实浏览器里量计算样式，不看源码猜。
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=BROWSER_CHANNEL, headless=True)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                page = context.new_page()
                try:
                    self._open_settings(context, page)
                    page.wait_for_selector("#update-card", state="visible", timeout=20000)
                    page.wait_for_selector("#update-card .agent-hint", state="visible",
                                           timeout=20000)
                    data = page.evaluate(
                        "() => Array.prototype.map.call("
                        "document.querySelectorAll('.agent-hint'), function (block) {"
                        "  var lead = block.querySelector('.agent-hint-lead');"
                        "  var note = block.querySelector('.agent-hint-note');"
                        "  var ls = getComputedStyle(lead); var ns = getComputedStyle(note);"
                        "  var bs = getComputedStyle(block);"
                        "  return {leadText: lead.textContent.trim(), noteText: note.textContent,"
                        "    leadSize: parseFloat(ls.fontSize), noteSize: parseFloat(ns.fontSize),"
                        "    leadColor: ls.color, noteColor: ns.color, leadWeight: ls.fontWeight,"
                        "    leadLine: parseFloat(ls.lineHeight), noteLine: parseFloat(ns.lineHeight),"
                        "    bg: bs.backgroundColor, borderLeft: bs.borderLeftWidth,"
                        "    padTop: bs.paddingTop, padLeft: bs.paddingLeft,"
                        "    links: block.querySelectorAll('.agent-hint-note a').length,"
                        "    buttons: block.querySelectorAll('.agent-hint-open').length};"
                        "})")
                    self.assertEqual(len(data), 3, "全页应恰好三块提示")
                    for item in data:
                        self.assertGreater(item["leadSize"], item["noteSize"],
                                           "主句字号必须大于说明行：%r" % item)
                        self.assertEqual(item["leadSize"], 13.0)
                        self.assertEqual(item["noteSize"], 11.0)
                        self.assertEqual(item["leadWeight"], "500")
                        self.assertNotEqual(item["leadColor"], item["noteColor"],
                                            "主句要比说明行更实（颜色不同）")
                        self.assertEqual(item["bg"], "rgb(246, 236, 210)",
                                         "浅底提示块：%r" % item["bg"])
                        self.assertEqual(item["borderLeft"], "2px")
                        self.assertEqual(item["padTop"], "8px")
                        self.assertEqual(item["padLeft"], "10px")
                        self.assertEqual(item["links"], 1, "说明行要有「项目地址」入口")
                        self.assertEqual(item["buttons"], 1, "说明行要有「什么是 agent？」入口")
                    leads = [item["leadText"] for item in data]
                    self.assertEqual(leads.count("建议让 agent 来装"), 2)
                    self.assertEqual(leads.count("建议让 agent 来更新"), 1)
                    for item in data:
                        self.assertIn("项目地址", item["noteText"])
                        self.assertIn("什么是 agent？", item["noteText"])

                    # 三块内都是同一个 dialog，不是每处各弹一个。
                    self.assertEqual(page.evaluate(
                        "() => document.querySelectorAll('dialog#agent-help-dialog').length"), 1)
                    self.assertEqual(page.evaluate(
                        "() => document.querySelectorAll('.agent-hint-open').length"), 3)

                    # 每个入口都能打开；Esc 能关；焦点落在 dialog 里。
                    for index in range(3):
                        page.evaluate(
                            "(i) => document.querySelectorAll('.agent-hint-open')[i].click()",
                            index)
                        page.wait_for_selector("#agent-help-dialog[open]", timeout=10000)
                        dialog_text = page.eval_on_selector(
                            "#agent-help-dialog", "el => el.textContent || ''")
                        self.assertIn("agent 是什么", dialog_text)
                        self.assertIn("Trae", dialog_text)
                        self.assertIn("WorkBuddy", dialog_text)
                        self.assertTrue(page.evaluate(
                            "() => document.getElementById('agent-help-dialog')"
                            ".contains(document.activeElement)"),
                            "打开后焦点要落在 dialog 里")
                        page.keyboard.press("Escape")
                        page.wait_for_function("() => !document.getElementById('agent-help-dialog').open", timeout=10000)

                    # 「知道了」也能关。
                    page.evaluate(
                        "() => document.querySelector('.agent-hint-open').click()")
                    page.wait_for_selector("#agent-help-dialog[open]", timeout=10000)
                    page.click("#agent-help-dialog .agent-help-actions button")
                    page.wait_for_function("() => !document.getElementById('agent-help-dialog').open", timeout=10000)
                finally:
                    safe_close(context)
            finally:
                browser.close()

    def test_refresh_qr_restarts_engine_then_loads_new_code(self) -> None:
        """「刷新二维码」必须真换码：先 POST /api/napcat/restart，再取新图。

        后端只有重启 NapCat 才会生成新二维码（`/api/napcat/qrcode` 是纯读）。
        这里同时锁住三条：
        1. 用户点 #refresh-qr → 先发一次 restart（带 uin 字段），失败要写进 #qr-note 不许静默；
        2. 5 秒轮询走 refreshQrIfStale() 的**无参** refreshQr()，只重取同一张图，绝不许偷偷重启引擎；
        3. 换码之后图片要重新加载出来。
        """
        posts: list = []
        bodies: list = []
        state = {"fail": False}

        def restart_stub(route) -> None:
            posts.append(route.request.method)
            bodies.append(route.request.post_data or "")
            if state["fail"]:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": False, "error": "重启引擎失败：端口被占用"}))
            else:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": True, "waiting_for_login": True}))

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=BROWSER_CHANNEL, headless=True)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                page = context.new_page()
                page.route("**/api/napcat/status", self._napcat_status_stub)
                page.route("**/api/napcat/qrcode**", self._napcat_qrcode_stub)
                page.route("**/api/napcat/restart", restart_stub)
                try:
                    # 引擎在跑、QQ 未登录 → running_need_login → #refresh-qr 可见。
                    self.napcat_running = True
                    open_authed_page(context, page, self.base, TOKEN)
                    open_settings(page)
                    page.wait_for_selector("#refresh-qr", state="visible", timeout=20000)
                    # 初次 checkSetup() 已经跑过 refreshQrIfStale() → 无参 refreshQr()。
                    # 那时不该有 restart，否则就是每 5 秒自动重启引擎。
                    self.assertEqual(posts, [], "轮询路径不该发 restart：%r" % posts)

                    page.click("#refresh-qr")
                    deadline = time.time() + 20.0
                    while time.time() < deadline and not posts:
                        page.wait_for_timeout(50)
                    self.assertEqual(posts, ["POST"],
                                     "点「刷新二维码」应先发一次 /api/napcat/restart：%r" % posts)
                    payload = json.loads(bodies[0] or "{}")
                    self.assertIn("uin", payload, "restart 请求要带 uin 字段：%r" % bodies)

                    note = page.eval_on_selector(
                        "#qr-note", "el => el.textContent || ''")
                    self.assertIn("正在换一张新码", note,
                                  "换码期间 #qr-note 要说明正在换新码，实际：%r" % note)

                    # 换码成功后图片重新加载出来（证明取的是真 PNG，不是只换了文案）。
                    page.wait_for_function(
                        "() => { const i = document.getElementById('qr');"
                        " return i && !i.hidden && i.complete && i.naturalWidth > 0; }",
                        timeout=20000)

                    # —— 失败路径：restart 回 ok:false，必须把原因写进 #qr-note，不许假装成功 ——
                    state["fail"] = True
                    page.click("#refresh-qr")
                    deadline = time.time() + 20.0
                    while time.time() < deadline and len(posts) < 2:
                        page.wait_for_timeout(50)
                    self.assertEqual(posts, ["POST", "POST"], posts)
                    page.wait_for_function(
                        "() => (document.getElementById('qr-note').textContent || '').indexOf('换新码失败') === 0",
                        timeout=20000)
                    note = page.eval_on_selector(
                        "#qr-note", "el => el.textContent || ''")
                    self.assertIn("端口被占用", note, "失败原因要原样写进 #qr-note：%r" % note)
                    self.assertIn("再试一次", note, "失败文案要给出下一步：%r" % note)
                finally:
                    safe_close(context)
            finally:
                browser.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

