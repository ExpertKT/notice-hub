"""扫描三态：禁用 / 已在扫描 / 扫描器不可用，各自文案必须准确，不许一律报「扫描启动失败」。

为什么要这条：历史上这三种情况都被同一句「扫描启动失败」盖住了，用户照着提示改也没用。
这里用 CDP Fetch 直接替换 `/api/wechat/scan`（POST）与 `/api/wechat/status`（GET）的响应，
不依赖真微信、也不改产品代码。

复现：
    python tools/e2e/run.py --only 12_settings_scan_states
"""

from __future__ import annotations

import base64
import json
import time

from harness import scenario

SCAN_ALREADY = {"ok": True, "started": False, "already_running": True,
                "message": "扫描正在进行中（已用 12.3 秒）·E2E哨兵，等这一轮跑完会自动刷新。"}
SCAN_DISABLED = {"ok": True, "started": False, "already_running": False,
                 "error": "微信扫描没有启用·E2E哨兵：先勾选「我的群」并保存，或先启用微信"}
SCAN_UNAVAILABLE = {"ok": False, "started": False, "already_running": False,
                    "error": "扫描器不可用·E2E哨兵：微信没有在运行，或密钥还没读出来"}

STATUS_DISABLED = {"ok": True, "state": "未启用·E2E哨兵", "available": False, "running": False,
                   "scan_state": "disabled", "scan_stage": "", "groups": 0,
                   "last_read_at": "", "last_error": "", "error_hint": ""}


class _Net:
    """一段可控的网络期：命中的请求由剧本回，其余放行；需要时把某些请求挂住不响应。"""

    def __init__(self, app) -> None:
        self.app = app
        self.held: list[str] = []

    def __enter__(self):
        self.app.page.cdp.call("Fetch.enable", patterns=[{"urlPattern": "*", "requestStage": "Request"}])
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def serve(self, path_fragment: str, payload: dict, *, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        while time.monotonic() < deadline:
            for event in self.app.page.cdp.pump(0.1):
                if event.get("method") != "Fetch.requestPaused":
                    continue
                params = event.get("params") or {}
                url = str((params.get("request") or {}).get("url") or "")
                request_id = params.get("requestId")
                if not request_id:
                    continue
                if path_fragment not in url:
                    self.app.page.cdp.call("Fetch.continueRequest", requestId=request_id)
                    continue
                self.app.page.cdp.call(
                    "Fetch.fulfillRequest",
                    requestId=request_id,
                    responseCode=200,
                    responseHeaders=[{"name": "Content-Type", "value": "application/json; charset=utf-8"},
                                     {"name": "Cache-Control", "value": "no-store"}],
                    body=base64.b64encode(body).decode("ascii"),
                )
                return
        raise AssertionError(f"{timeout}s 内没等到 {path_fragment} 请求")

    def wait_status_stub(self, selector: str, needle: str, payload: dict, *,
                         timeout: float = 8.0) -> str:
        """一边排水一边把每个 /api/wechat/status 都用桩回掉，直到 selector 出现 needle。

        为什么要「每个都回」而不是只回第一个：页面有两个消费者打同一个接口——
        `refreshWechatStatus()` 写 `#wx-status`，`updateScanStatus()` 每 1.5 秒只写 `#wx-scan`。
        只回第一个很可能被后者吃掉，桩就永远进不了 `#wx-status`。挂住（hold）也不行：
        fetch 永久 pending，页面不重绘。
        """
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            for event in self.app.page.cdp.pump(0.1):
                if event.get("method") != "Fetch.requestPaused":
                    continue
                params = event.get("params") or {}
                url = str((params.get("request") or {}).get("url") or "")
                request_id = params.get("requestId")
                if not request_id:
                    continue
                if "/api/wechat/status" not in url:
                    self.app.page.cdp.call("Fetch.continueRequest", requestId=request_id)
                    continue
                self.app.page.cdp.call(
                    "Fetch.fulfillRequest",
                    requestId=request_id,
                    responseCode=200,
                    responseHeaders=[{"name": "Content-Type", "value": "application/json; charset=utf-8"},
                                     {"name": "Cache-Control", "value": "no-store"}],
                    body=base64.b64encode(body).decode("ascii"),
                )
            # 直接读 textContent：app.text/app.count 会 settle，settle 会吞掉 Fetch.requestPaused。
            last = str(self.app.page.evaluate(
                "(function(){var n=document.querySelector(%s);return n?n.textContent:'';})()"
                % json.dumps(selector)
            ) or "")
            if needle in last:
                return last
        raise AssertionError(f"{selector} 在 {timeout}s 内没出现「{needle}」；当前：{last!r}")

    def pump_hold_status(self, seconds: float) -> None:
        """排水：放行别的请求，把 /api/wechat/status 挂住（页面轮询就不会覆盖文案）。"""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            for event in self.app.page.cdp.pump(0.1):
                if event.get("method") != "Fetch.requestPaused":
                    continue
                params = event.get("params") or {}
                url = str((params.get("request") or {}).get("url") or "")
                request_id = params.get("requestId")
                if not request_id:
                    continue
                if "/api/wechat/status" in url:
                    self.held.append(request_id)
                    continue
                self.app.page.cdp.call("Fetch.continueRequest", requestId=request_id)

    def wait_text(self, selector: str, needle: str, *, timeout: float = 8.0) -> str:
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            self.pump_hold_status(0.25)
            last = self.app.text(selector) if self.app.count(selector) else ""
            if needle in last:
                return last
        raise AssertionError(f"{selector} 在 {timeout}s 内没出现「{needle}」；当前：{last!r}")

    def _fulfill_held(self) -> None:
        body = json.dumps(STATUS_DISABLED, ensure_ascii=False).encode("utf-8")
        for request_id in self.held:
            self.app.page.cdp.call(
                "Fetch.fulfillRequest",
                requestId=request_id,
                responseCode=200,
                responseHeaders=[{"name": "Content-Type", "value": "application/json; charset=utf-8"},
                                 {"name": "Cache-Control", "value": "no-store"}],
                body=base64.b64encode(body).decode("ascii"),
            )
        self.held.clear()

    def close(self) -> None:
        self._fulfill_held()
        self.app.page.cdp.call("Fetch.disable")


def _dismiss_wizard(app) -> None:
    """首访向导会盖住设置页；关不掉就让后面的断言自己失败。"""
    visible = app.page.evaluate(
        "(function(){var w=document.getElementById('platform-setup-wizard');"
        "return !!(w&&!w.hidden);})()"
    )
    if visible:
        app.click("#platform-setup-wizard .platform-setup-later")
        app.wait_for("#platform-setup-wizard", state="hidden", timeout=10)


def _open_settings_wechat(app) -> None:
    app.click("#tab-settings-button")
    app.wait_for("#tab-settings", state="visible", timeout=10)
    app.wait_for("#wechat-settings-card", timeout=15)
    app.wait_for("#wx-scan-now", timeout=15)


@scenario("12_settings_scan_states", viewports=("desktop",), tags=("fakes:none", "settings"))
def run(app) -> None:
    """三种扫描状态各给对的话：禁用、已在扫描、扫描器不可用。"""
    app.goto("/")
    _dismiss_wizard(app)
    _open_settings_wechat(app)
    app.shot("01-wechat-card")

    # ① 禁用：status 是 disabled，状态行必须说「扫描未启用」，不是失败。
    #    桩文案带「·E2E哨兵」（冷装默认值没有这串）：哨兵出现才证明桩真的生效，
    #    而不是恰好和冷装默认值同值也能绿。
    #    必须主动触发一次 refreshWechatStatus（点「保存目录」会触发），因为页面加载时
    #    那次真实状态请求发生在拦截之前，它已经把真数据画进 #wx-status 了。
    net = _Net(app).__enter__()
    try:
        app.page.evaluate(
            "(function(){var i=document.getElementById('wx-dir-input');"
            "if(i)i.value='';document.getElementById('wx-dir-save').click();})()"
        )
        status_text = net.wait_status_stub("#wx-status", STATUS_DISABLED["state"], STATUS_DISABLED)
        assert STATUS_DISABLED["state"] in status_text, f"状态行没透出桩里的 state：{status_text!r}"
        scan_text = str(app.page.evaluate(
            "(function(){var n=document.getElementById('wx-scan');return n?n.textContent:'';})()"
        ) or "")
        assert scan_text.strip().startswith("扫描未启用"), f"禁用态不是「扫描未启用」开头：{scan_text!r}"
    finally:
        net.close()
    app.shot("02-status-disabled")

    # ② 已在扫描：点「立即扫描一次」要告诉用户「正在进行中」，且不能报失败
    net = _Net(app).__enter__()
    try:
        app.page.evaluate("document.getElementById('wx-scan-now').click()")
        net.serve("/api/wechat/scan", SCAN_ALREADY)
        text = net.wait_text("#wx-scan", "扫描正在进行中")
        assert text.strip().startswith("扫描正在进行中"), f"已在扫描却没说「正在进行中」：{text!r}"
        assert SCAN_ALREADY["message"] in text, f"文案没原样透出（哨兵没到）：{text!r}"
    finally:
        net.close()
    # ② 里被挂住的状态请求在 close 时才回桩，它们会在渲染侧晚一步写 #wx-scan（「扫描未启用」）；
    # 等它们落地再进 ③，否则 ③ 的文案会被这次迟到渲染盖掉。
    app.settle(0.8)
    app.shot("03-scan-already-running")

    # ③ 扫描器不可用：这才是真失败，且要带出原因
    net = _Net(app).__enter__()
    try:
        app.page.evaluate("document.getElementById('wx-scan-now').click()")
        net.serve("/api/wechat/scan", SCAN_UNAVAILABLE)
        text = net.wait_text("#wx-scan", "扫描启动失败")
        assert text.strip().startswith("扫描启动失败"), f"不可用态没说「扫描启动失败」：{text!r}"
        assert SCAN_UNAVAILABLE["error"] in text, f"失败原因没带出来（哨兵没到）：{text!r}"
    finally:
        net.close()
    app.shot("04-scan-unavailable")

    app.assert_no_console_errors()
