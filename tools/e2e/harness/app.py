"""App：剧本手里的「浏览器 + 接口 + 数据库」三合一工具。

台词示例见 `scenarios/demo_smoke.py`；对外方法就是负责人定死的那组，别加私货。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .cdp import Browser, Page
from .install import Install
from .paths import ensure_outside_repo

VIEWPORTS = {
    "desktop": (1280, 900),
    "mobile": (390, 844),
}


class App:
    def __init__(
        self,
        install: Install,
        *,
        viewport: str = "desktop",
        out_dir: Path,
        headful: bool = False,
        fakes: dict | None = None,
        profile_tag: str = "",
    ) -> None:
        if viewport not in VIEWPORTS:
            raise ValueError(f"未知 viewport：{viewport}（可用：{', '.join(VIEWPORTS)}）")
        self.install = install
        self.viewport = viewport
        self.out_dir = Path(out_dir)
        self.headful = headful
        # M1：--reuse 时同一份安装会被多个用例共用，必须各用一份 Edge profile，
        # 否则 localStorage/Service Worker/缓存会把上个用例的状态带进来。
        self.profile_tag = str(profile_tag or "")
        self.shots: list[str] = []
        # run.py 在冷装前起的替身（name -> Handle），只读：剧本要注入事件就打 `base_url + /__inject`。
        # 停止由 run.py 的 finally 负责，这里不重复 stop()。
        self.fakes: dict = dict(fakes or {})
        self._browser: Browser | None = None
        self._page: Page | None = None
        self._fakes: list = []

    # ------------------------------------------------------------ 生命周期
    def open(self) -> "App":
        self.out_dir.mkdir(parents=True, exist_ok=True)
        width, height = VIEWPORTS[self.viewport]
        profile_dir = ensure_outside_repo(
            self.install.root / (f"edge-{self.viewport}" + (f"-{self.profile_tag}" if self.profile_tag else ""))
        )
        self._browser = Browser(headful=self.headful, profile_dir=str(profile_dir))
        try:
            self._page = self._browser.start()
            self._page.set_viewport(width, height)
            self._install_session_cookie()
        except BaseException:
            # 浏览器已经拉起来了就一定要收掉，否则 Edge 会一直占着 profile 目录
            # （run.py 拿不到 App 实例，也就不会再调 close()）。
            self.close()
            raise
        return self

    def close(self) -> None:
        while self._fakes:
            handle = self._fakes.pop()
            try:
                handle.stop()
            except Exception:  # noqa: BLE001 - 收尾失败不该盖住真正的失败
                pass
        if self._browser is not None:
            self._browser.stop()
            self._browser = None
            self._page = None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("App 还没 open()")
        return self._page

    @property
    def base_url(self) -> str:
        return self.install.base_url

    def _install_session_cookie(self) -> None:
        """令牌换 HttpOnly 会话 Cookie，浏览器地址栏里不带 token。"""
        payload = json.dumps({"token": self.install.token}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/session/exchange",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            cookie_header = response.headers.get("Set-Cookie") or ""
        match = re.match(r"([^=]+)=([^;]*)", cookie_header)
        if not match:
            raise AssertionError(f"会话交换没拿到 Set-Cookie：{cookie_header!r}")
        self.page.cdp.call(
            "Network.setCookie",
            name=match.group(1),
            value=match.group(2),
            url=self.base_url,
            path="/",
            httpOnly=True,
        )

    # ---------------------------------------------------------------- 浏览器
    def goto(self, path: str = "/") -> None:
        if not path.startswith("/"):
            path = "/" + path
        self.page.navigate(self.base_url + path)
        self.settle()

    def settle(self, seconds: float = 0.2) -> None:
        self.page.collect(timeout=seconds)

    def click(self, selector: str) -> None:
        self.page.click(selector)
        self.settle()

    def fill(self, selector: str, value: str) -> None:
        self.page.fill(selector, value)
        self.settle()

    def press(self, selector: str, key: str) -> None:
        self.page.press(selector, key)
        self.settle()

    def wait_for(self, selector: str, state: str = "visible", timeout: float = 10) -> None:
        deadline = time.monotonic() + timeout
        last = "未知"
        while time.monotonic() < deadline:
            last = self._state_of(selector)
            if last == state:
                self.settle(0.1)
                return
            self.page.collect(timeout=0.05)
            time.sleep(0.05)
        raise AssertionError(f"等待 {selector} 变 {state} 超时（{timeout}s），当前状态：{last}")

    def _state_of(self, selector: str) -> str:
        return str(
            self.page.evaluate(
                "(function(){var el=document.querySelector(%s);if(!el)return 'detached';"
                "var s=getComputedStyle(el),r=el.getBoundingClientRect();"
                "var visible=s.display!=='none'&&s.visibility!=='hidden'&&"
                "parseFloat(s.opacity||'1')>0.01&&r.width>0&&r.height>0;"
                "if(visible)return 'visible';return el.hidden?'hidden':'attached';})()"
                % json.dumps(selector)
            )
        )

    def text(self, selector: str) -> str:
        value = self.page.evaluate(
            "(function(){var el=document.querySelector(%s);return el?String(el.textContent||''):null;})()"
            % json.dumps(selector)
        )
        if value is None:
            raise AssertionError(f"找不到元素：{selector}")
        return str(value)

    def count(self, selector: str) -> int:
        return int(
            self.page.evaluate("document.querySelectorAll(%s).length" % json.dumps(selector)) or 0
        )

    # ---------------------------------------------------------------- 断言
    def assert_text(self, selector: str, expect: str) -> None:
        actual = self.text(selector)
        if expect not in actual:
            raise AssertionError(f"{selector} 文案不含「{expect}」；实际：{actual!r}")

    def assert_count(self, selector: str, expected: int) -> None:
        actual = self.count(selector)
        if actual != expected:
            raise AssertionError(f"{selector} 个数应为 {expected}，实际 {actual}")

    def assert_attr(self, selector: str, name: str, expect: str) -> None:
        actual = self.page.evaluate(
            "(function(){var el=document.querySelector(%s);return el?el.getAttribute(%s):null;})()"
            % (json.dumps(selector), json.dumps(name))
        )
        if actual != expect:
            raise AssertionError(f"{selector} 的 {name} 应为 {expect!r}，实际 {actual!r}")

    def assert_no_console_errors(self) -> None:
        self.settle(0.2)
        problems = list(self.page.page_errors) + list(self.page.console_errors)
        if problems:
            raise AssertionError("页面有 console 错误：" + " | ".join(dict.fromkeys(problems)))

    def failed_request_count(self) -> int:
        """去重后的失败请求条数（报告里要标出来，免得断言白跑看不出来）。"""
        return len(list(dict.fromkeys(self.page.failed_requests)))

    def assert_no_failed_requests(self) -> None:
        """收尾统一断言（M4）；失败请求条数用 failed_request_count() 报进报告。"""
        self.settle(0.2)
        failed = list(dict.fromkeys(self.page.failed_requests))
        if failed:
            raise AssertionError("有失败的请求：" + " | ".join(failed))

    # ---------------------------------------------------------------- 平台替身
    WECHAT_DIR_ENV = "QQ_DIGEST_WECHAT_DIR"

    def _guard_wechat(self, path: str) -> None:
        """碰微信接口前必须有显式微信目录：否则服务端 locate_storage() 会扫注册表/盘符。"""
        if "/wechat" not in path:
            return
        if self.install.has_env(self.WECHAT_DIR_ENV):
            return
        raise AssertionError(
            "拒绝在没指定微信目录的实例上访问微信接口：locate_storage() 不带参时会扫注册表和盘符，"
            f"在开发者机器上会命中真实微信目录。请给剧本加 tags=(\"fakes:wechat\",)，由 harness 注入 {self.WECHAT_DIR_ENV}。"
        )

    def fake(self, name: str, **options):
        """起一个 W3 平台替身（`tools/e2e/fakes/`），本次运行结束时自动 stop()。"""
        from fakes import start as start_fake  # 延迟导入：没用到替身的剧本不依赖它

        # 先查再起：检查不过就别把替身拉起来（否则它停不掉，端口和目录都留着）。
        if str(name).strip().lower() == "wechat" and not self.install.has_env(self.WECHAT_DIR_ENV):
            raise AssertionError(
                f"微信替身的 {self.WECHAT_DIR_ENV} 没写进被测实例的 .env；"
                "替身必须在冷装之前启动并把它给的 env 交给 cold_install(extra_env=...)。"
            )
        handle = start_fake(name, **options)
        self._fakes.append(handle)
        return handle

    # ---------------------------------------------------------------- 截图
    def shot(self, name: str) -> str:
        filename = name if name.lower().endswith(".png") else name + ".png"
        path = self.out_dir / filename
        self.page.screenshot(str(path))
        self.shots.append(str(path))
        return str(path)

    # ---------------------------------------------------------------- 接口
    def api_get(self, path: str, *, timeout: float = 30.0) -> dict:
        self._guard_wechat(path)
        request = urllib.request.Request(
            self.base_url + path, headers={"X-Token": self.install.token, "Accept": "application/json"}
        )
        return self._read_json(request, timeout=timeout)

    def api_post(self, path: str, payload: dict | None = None, *, timeout: float = 60.0) -> dict:
        self._guard_wechat(path)
        body = json.dumps(payload or {}).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers={"X-Token": self.install.token, "Content-Type": "application/json"},
            method="POST",
        )
        return self._read_json(request, timeout=timeout)

    @staticmethod
    def _read_json(request: urllib.request.Request, *, timeout: float) -> dict:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", "replace")
            raise AssertionError(f"{request.full_url} 返回 HTTP {error.code}：{raw[:300]}") from None
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            raise AssertionError(f"{request.full_url} 不是 JSON：{raw[:300]}") from None
        return data if isinstance(data, dict) else {"data": data}

    # ---------------------------------------------------------------- 数据库
    def db(self, sql: str) -> list[dict]:
        """只读查询冷装实例的 sqlite；只允许 SELECT，防止剧本手滑写库。"""
        if not sql.strip().lower().startswith("select"):
            raise ValueError("db() 只接受 SELECT（冷装库是只读的）")
        path = self.install.db_path
        if not path.is_file():
            raise AssertionError(f"冷装库里没有数据库文件：{path}")
        uri = "file:%s?mode=ro" % urllib.parse.quote(path.as_posix())
        with sqlite3.connect(uri, uri=True, timeout=10) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(sql).fetchall()]
