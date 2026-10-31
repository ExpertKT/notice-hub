# -*- coding: utf-8 -*-
"""QQ 接入卡：后端每个状态取值都必须在页面上有交代（防止「某个状态没人管」）。

为什么要有这条：状态取值是后端定的枚举，前端靠 if/else 拼文案。曾经出现过
「按钮文案让你点『启动引擎并显示二维码』，可那个按钮在这个状态下是 hidden」，
单测和接口验证全过，靠人眼看出来的。这里把「每个取值渲染一遍 + 四项断言」钉死。

取值清单**从源码里读**（`webapp.py` 的 engine_state/login_state、`napcat_admin.py` 的 stage、
`guide.js` 的状态表），不在测试里手抄。后端新增一个取值而场景表没跟上，测试立刻红。
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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qq_live_digest.store import Store  # noqa: E402
from qq_live_digest.config import Settings  # noqa: E402
from qq_live_digest.webapp import TaskWebServer  # noqa: E402
from tests import (  # noqa: E402
    isolate_browser_temp_env, restore_browser_temp_env, wait_for_page_settled,
)

try:  # pragma: no cover - 运行环境缺 playwright 时整类跳过
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
    HAVE_PLAYWRIGHT = True
except Exception:  # noqa: BLE001
    HAVE_PLAYWRIGHT = False

# 同 tests/test_ui_browser.py：CI 的 browser job 设 NH_REQUIRE_BROWSER=1，
# 那时缺 playwright 直接失败，不许静默跳过（静默跳过 = 防线不存在）。
REQUIRE_BROWSER = os.environ.get("NH_REQUIRE_BROWSER") == "1"
if REQUIRE_BROWSER and not HAVE_PLAYWRIGHT:  # pragma: no cover
    raise RuntimeError(
        "NH_REQUIRE_BROWSER=1 但没装 playwright：先 pip install -r requirements-dev.txt。"
        "状态覆盖不许被 skip 掉。")

TOKEN = "state-coverage-token"
WEBAPP_SOURCE = (ROOT / "qq_live_digest" / "webapp.py").read_text(encoding="utf-8")
NAPCAT_SOURCE = (ROOT / "qq_live_digest" / "napcat_admin.py").read_text(encoding="utf-8")
GUIDE_SOURCE = (ROOT / "qq_live_digest" / "web_static" / "js" / "guide.js").read_text(encoding="utf-8")


def backend_engine_states() -> set[str]:
    """`engine_state = "…"` 的所有取值（后端 → 前端的枚举源）。"""
    return set(re.findall(r'engine_state\s*=\s*"([a-z_]+)"', WEBAPP_SOURCE))


def backend_login_states() -> set[str]:
    """`login_state = "online" if … else ("waiting_scan" if … else "not_logged_in")` 里的取值。

    先把 `engine_state == "..."` 这种判断条件剔掉，否则会把判断里的值（如 running）也当取值。
    """
    line = re.search(r'^\s*login_state\s*=\s*.*$', WEBAPP_SOURCE, re.M)
    assert line, "webapp.py 里找不到 login_state 的赋值，测试无法取到取值清单"
    assignment = re.sub(r'engine_state\s*==\s*"[a-z_]+"', "", line.group(0))
    return set(re.findall(r'"([a-z_]+)"', assignment))


def backend_stages() -> set[str]:
    """`stage` 的所有取值：napcat 侧的 "stage": "…" 加上 webapp.py 里的。"""
    stages = set(re.findall(r'"stage":\s*"([a-z_]+)"', NAPCAT_SOURCE))
    stages |= set(re.findall(r'"stage":\s*"([a-z_]+)"', WEBAPP_SOURCE))
    return stages


def frontend_plans() -> dict[str, str]:
    """guide.js 里 `var plans = {…}` 的 {状态名: 引擎行文案}。"""
    block = re.search(r"var plans = \{(.*?)\n  \};", GUIDE_SOURCE, re.S)
    assert block, "guide.js 里找不到状态表（var plans = {…}），测试无法核对覆盖"
    plans = {}
    for line in block.group(1).split("\n"):
        key = re.match(r"\s{4}([a-z_]+):\s*\{", line)
        engine = re.search(r"engine:\s*'([^']*)'", line)
        if key and engine:
            plans[key.group(1)] = engine.group(1)
    return plans


def frontend_button_ids() -> list[str]:
    """guide.js 里那个「哪些按钮归状态机管」的清单。"""
    block = re.search(r"var QQ_CONNECT_BUTTON_IDS = \[(.*?)\];", GUIDE_SOURCE, re.S)
    assert block, "guide.js 里找不到 QQ_CONNECT_BUTTON_IDS，测试无法核对按钮归属"
    return re.findall(r"'([a-z-]+)'", block.group(1))


# 后端 engine_state → 该状态下 `/api/napcat/status` 会回什么（前端据此选状态）
ENGINE_SCENARIOS: dict[str, dict] = {
    # 还没装引擎：napcat_admin 也探测不到
    "engine_missing": {"ok": True, "napcat_installed": False, "napcat_running": False, "online": False},
    # 装了 NapCat 但本程序没接管（admin 探测不到安装目录）
    "not_installed": {"ok": False, "stage": "not_installed", "napcat_installed": False,
                      "napcat_running": False, "online": False},
    # 装了、没启动
    "installed_not_running": {"ok": True, "napcat_installed": True, "napcat_running": False, "online": False},
    # 在跑，接口通，等登录
    "running": {"ok": True, "napcat_installed": True, "napcat_running": True, "online": False},
}

# 后端 engine_state=running 的三种前端落点（同一个后端取值，三个不同界面）
RUNNING_VARIANTS = {
    "running_need_login": {"ok": True, "napcat_installed": True, "napcat_running": True, "online": False},
    "running_online": {"ok": True, "napcat_installed": True, "napcat_running": True,
                       "online": True, "nickname": "123456789"},
    "running_no_api": {"ok": False, "stage": "running_no_api", "napcat_installed": True,
                       "napcat_running": True, "online": False},
}

# 前端状态 → 期望落点（状态名 → guide.js plans 里的键）。engine_missing 有两来源，同一落点。
EXPECTED_STATE = {
    "engine_missing": "engine_missing",
    "not_installed": "engine_missing",
    "installed_not_running": "installed_not_running",
    "running": "running_need_login",
}

# 有主按钮的状态（running_online 按设计没有主按钮，只剩「重启引擎」）
STATES_WITH_PRIMARY = {"engine_missing", "installed_not_running", "running_need_login", "running_no_api"}

# 检查期那句兜底文案：稳定状态下不许还挂着它
LOADING_TEXT = "正在检查 QQ 登录状态…"

MEASURE_JS = """
(ids) => {
  const visible = (el) => {
    if (!el || el.hidden) return false;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const text = (id) => {
    const el = document.getElementById(id);
    return el && visible(el) ? (el.textContent || '').trim() : '';
  };
  const buttons = ids.map((id) => {
    const el = document.getElementById(id);
    return {id: id, exists: !!el, visible: visible(el),
            label: el ? (el.textContent || '').trim() : '',
            primary: !!(el && el.classList.contains('primary'))};
  });
  return {
    buttons: buttons,
    engine: (document.getElementById('napcat-engine') || {}).textContent || '',
    status: text('status'),
    nextStep: text('qq-next-step'),
    qrNote: text('qr-note'),
  };
}
"""


@unittest.skipUnless(HAVE_PLAYWRIGHT, "playwright 未安装（pip install -r requirements-dev.txt）")
class QQConnectStateCoverageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # 必须在启动 Playwright 之前：Node 驱动用 TEMP/TMP 建 artifacts 目录，
        # %TEMP% 不可写会 EPERM。共用逻辑在 tests/__init__.py。
        cls._temp_env_restore = isolate_browser_temp_env()
        cls.plans = frontend_plans()
        cls.button_ids = frontend_button_ids()
        cls.tmp = tempfile.TemporaryDirectory()
        store = Store(Path(cls.tmp.name) / "api.sqlite3")
        settings = Settings(web_host="127.0.0.1", web_port=0, web_token=TOKEN,
                            data_dir=Path(cls.tmp.name) / "data")
        cls.server = TaskWebServer(settings, store)
        assert cls.server.start(), "TaskWebServer 启动失败"
        assert cls.server.server is not None
        cls.base = "http://127.0.0.1:%d" % cls.server.server.server_address[1]
        body = b"{}"
        request = urllib.request.Request(
            cls.base + "/api/onboarding/accept", data=body,
            headers={"X-Token": TOKEN, "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            assert response.status == 200
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="msedge", headless=True)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.browser.close()
        finally:
            try:
                cls.playwright.stop()
            finally:
                # 服务端的请求线程是 daemon（server_close 不 join），先等它落零再删目录；
                # Windows 释放 sqlite 句柄有几十毫秒延迟，所以最多重试 5 次、最后一次照抛。
                httpd = getattr(cls.server, "server", None)
                cls.server.stop()
                deadline = time.monotonic() + 5
                while httpd is not None and time.monotonic() < deadline:
                    if not getattr(httpd, "_active_connections", 0):
                        break
                    time.sleep(0.05)
                last_error = None
                for _ in range(5):
                    try:
                        cls.tmp.cleanup()
                    except PermissionError as error:
                        last_error = error
                        time.sleep(0.1)
                    else:
                        break
                else:
                    raise last_error
                restore_browser_temp_env(cls._temp_env_restore)

    def render(self, payload: dict) -> dict:
        """按给定 /api/napcat/status 返回渲染一次 QQ 卡，返回可断言的状态。"""
        context = self.browser.new_context(viewport={"width": 1280, "height": 1000})
        page = context.new_page()

        def status_route(route, request):
            body = json.dumps(dict(payload, napcat_root="fixture"))
            try:
                route.fulfill(status=200, content_type="application/json", body=body)
            except Exception:  # noqa: BLE001 - 页面已关闭时忽略在途轮询
                pass

        page.route("**/api/napcat/status", status_route)
        try:
            page.goto(self.base + "/?token=" + TOKEN + "&nowiz=1",
                      wait_until="domcontentloaded", timeout=30000)
            # 带 token 首次访问页面必然自己 reload 一次（抹掉地址栏 token、改走 Cookie），
            # 不等它落定就去看状态，会读到换页前的初始态——CI 上就是这么红的。
            wait_for_page_settled(page)
            self._wait_for_state_applied(page, payload)
            return page.evaluate(MEASURE_JS, self.button_ids)
        finally:
            context.close()

    @staticmethod
    def _wait_for_state_applied(page, payload: dict) -> str:
        """等 `checkSetup()` 真的把状态画出来，而不是 sleep 一个拍脑袋的时长。

        以前这里写的是 `wait_for_timeout(900)`：本机快、900ms 足够；CI 的 runner 慢，
        900ms 时引擎行还停在初始的「正在检查 NapCat 引擎…」，于是断言全打在默认态上
        （CI 就是这么红的：同一个 payload 在不同用例里一会儿过一会儿红）。
        所以改成轮询到「引擎行不再是检查期文案」，超时把现场原样报出来。
        """
        deadline = time.monotonic() + 25
        engine = ""
        while True:
            try:
                engine = page.evaluate(
                    "() => { const el = document.getElementById('napcat-engine');"
                    " return el ? String(el.textContent || '') : ''; }")
            except PlaywrightError as error:
                # 等待期间页面又换了一次页时，evaluate 会报 Execution context was destroyed：
                # 那不是失败，等新页面出来再看。
                engine = "换页中：%s" % str(error).split("\n")[0][:60]
            if engine and "正在检查" not in engine:
                return engine
            if time.monotonic() >= deadline:
                raise AssertionError(
                    "25 秒内 checkSetup 没把 QQ 卡画出来（引擎行仍是 %r）。"
                    "桩 payload=%s；说明 /api/napcat/status 没命中，或页面脚本没跑起来。"
                    % (engine, payload))
            page.wait_for_timeout(50)

    def assert_state_is_stated(self, state: str, view: dict) -> None:
        """四项通用断言：可见按钮、状态文案、不点名 hidden 按钮、不挂检查期兜底句。"""
        shown = [b for b in view["buttons"] if b["visible"]]
        self.assertTrue(shown, "状态 %s 下一个可见按钮都没有：%s" % (state, view))
        self.assertTrue(view["status"], "状态 %s 没有状态文案（#status 为空或不可见）：%s" % (state, view))
        self.assertNotEqual(view["status"], LOADING_TEXT,
                            "状态 %s 还挂着检查期的兜底文案：%s" % (state, view))
        primary = [b for b in shown if b["primary"]]
        if state in STATES_WITH_PRIMARY:
            self.assertEqual(len(primary), 1,
                             "状态 %s 应当有且只有一个可见主按钮：%s" % (state, view))
        # 文案里点名的按钮，必须真的能点：hidden 的按钮名字不许出现在可见文案里
        copy = " ".join(filter(None, [view["status"], view["nextStep"],
                                      view["engine"], view["qrNote"]]))
        for button in view["buttons"]:
            if button["visible"] or not button["label"]:
                continue
            self.assertNotIn(button["label"], copy,
                             "状态 %s 的文案让用户点「%s」，可那个按钮是 hidden：%s"
                             % (state, button["label"], copy))

    # —— ① 后端每个 engine_state 都要有落点 ——
    def test_every_backend_engine_state_has_a_scenario(self) -> None:
        declared = backend_engine_states()
        self.assertTrue(declared, "从 webapp.py 读不到 engine_state 取值（正则失效了？）")
        self.assertEqual(declared, set(ENGINE_SCENARIOS),
                         "后端 engine_state 取值变了，本测试的场景表没跟上：\n"
                         "后端=%s\n场景表=%s\n新增取值请补场景并断言它在页面上的落点。"
                         % (sorted(declared), sorted(ENGINE_SCENARIOS)))

    def test_every_scenario_renders_a_managed_state(self) -> None:
        for engine_state, payload in ENGINE_SCENARIOS.items():
            with self.subTest(engine_state=engine_state):
                expected = EXPECTED_STATE[engine_state]
                view = self.render(payload)
                self.assertIn(expected, self.plans,
                              "前端状态表里没有 %s（guide.js 的 plans）" % expected)
                self.assertEqual(view["engine"], self.plans[expected],
                                 "engine_state=%s 没落到 %s：引擎行是 %r，期望 %r"
                                 % (engine_state, expected, view["engine"], self.plans[expected]))
                self.assert_state_is_stated(expected, view)

    # —— ② 同一个 engine_state=running 的三个落点（等登录 / 已登录 / 接口未连） ——
    def test_running_engine_variants(self) -> None:
        for state, payload in RUNNING_VARIANTS.items():
            with self.subTest(state=state):
                self.assertIn(state, self.plans, "前端状态表里没有 %s" % state)
                view = self.render(payload)
                self.assertEqual(view["engine"], self.plans[state],
                                 "%s 没落到自己的状态：引擎行是 %r" % (state, view["engine"]))
                self.assert_state_is_stated(state, view)

    # —— ③ 后端每个 stage 取值，落不到专属状态也必须落到有按钮的兜底 ——
    def test_every_backend_stage_falls_back_to_an_actionable_state(self) -> None:
        stages = backend_stages()
        self.assertTrue(stages, "从后端读不到 stage 取值（正则失效了？）")
        for stage in sorted(stages):
            for running in (False, True):
                with self.subTest(stage=stage, napcat_running=running):
                    payload = {"ok": False, "stage": stage, "napcat_running": running,
                               "napcat_installed": True, "online": False}
                    view = self.render(payload)
                    self.assertNotEqual(view["engine"], self.plans["pending"],
                                        "stage=%s 停在了检查期状态（没有任何人管它）：%s"
                                        % (stage, view))
                    shown = [b for b in view["buttons"] if b["visible"]]
                    self.assertTrue(shown, "stage=%s 一个可见按钮都没有：%s" % (stage, view))
                    self.assertTrue(view["status"], "stage=%s 没有状态文案：%s" % (stage, view))
                    self.assertNotEqual(view["status"], LOADING_TEXT,
                                        "stage=%s 还挂着检查期兜底文案：%s" % (stage, view))

    # —— ④ 未知取值必须落到兜底，且兜底是可操作的 ——
    def test_unknown_stage_falls_back_to_an_actionable_state(self) -> None:
        payload = {"ok": False, "stage": "brand_new_stage_nobody_handles",
                   "napcat_running": True, "napcat_installed": True, "online": False}
        view = self.render(payload)
        self.assertNotEqual(view["engine"], self.plans["pending"], view)
        shown = [b for b in view["buttons"] if b["visible"]]
        self.assertTrue(shown, "未知 stage 下没有可见按钮：%s" % view)
        self.assertTrue(view["status"], "未知 stage 下没有状态文案：%s" % view)

    # —— ⑤ 前端状态表里的每个状态都得能被渲染出来（没有死状态） ——
    def test_no_unreachable_frontend_state(self) -> None:
        reached = set(EXPECTED_STATE.values()) | set(RUNNING_VARIANTS)
        unreachable = set(self.plans) - reached - {"pending"}
        self.assertFalse(unreachable,
                         "guide.js 里这些状态没有任何后端取值能触发（死状态）：%s" % sorted(unreachable))

    # —— ⑥ 后端每个 login_state 取值都要被某个场景覆盖 ——
    def test_every_backend_login_state_is_covered(self) -> None:
        declared = backend_login_states()
        self.assertTrue(declared, "从 webapp.py 读不到 login_state 取值（正则失效了？）")
        # online / waiting_scan 由 running 的变体覆盖；not_logged_in 由未登录场景覆盖
        covered = {"online": RUNNING_VARIANTS["running_online"],
                   "waiting_scan": RUNNING_VARIANTS["running_need_login"],
                   "not_logged_in": ENGINE_SCENARIOS["installed_not_running"]}
        self.assertEqual(declared, set(covered),
                         "后端 login_state 取值变了，本测试没跟上：后端=%s 覆盖=%s"
                         % (sorted(declared), sorted(covered)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
