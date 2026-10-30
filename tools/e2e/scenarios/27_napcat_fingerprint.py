"""27 一键接入：安装包指纹确认（首次安装必须先看一眼 sha256 再装）。

为什么要这条：指纹确认是「先停下、让用户确认一次」的安全闸。它一旦静默跳过、或确认按钮点下去
没把完整 sha 传回后端，用户就可能在不知情的情况下装了一个来源不明的包。

打桩方式：照抄 12 里的 `_Net`（Fetch.enable → 命中就 fulfill、其余放行），不碰真下载、不碰真 NapCat。

复现：
    python tools/e2e/run.py --only 27_napcat_fingerprint
"""

from __future__ import annotations

import base64
import json
import time

from harness import scenario

FAKE_SHA = "0123456789abcdef" + "fedcba9876543210" + "0011223344556677" + "8899aabbccddeeff"
DOWNLOAD_URL = "https://example.invalid/napcat/NapCat.Shell.zip"

# 轮询用的稳定响应：让「检查登录状态」不要走指纹分支，免得盖掉我们要验的状态。
STATUS_STEADY = {"ok": False, "napcat_installed": False, "napcat_running": False,
                 "online": False, "stage": "not_installed", "error": "还没装 NapCat",
                 "reason": "", "expected_sha256": None, "download_url": "", "waiting_seconds": 0}


class _Net:
    """一段可控网络期：命中的请求按剧本回，其余一律放行。记录被拦下的请求体。"""

    def __init__(self, app) -> None:
        self.app = app
        self.seen: list[dict] = []

    def __enter__(self):
        self.app.page.cdp.call("Fetch.enable", patterns=[{"urlPattern": "*", "requestStage": "Request"}])
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self) -> None:
        self.app.page.cdp.call("Fetch.disable")

    def fulfill(self, request_id: str, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.app.page.cdp.call(
            "Fetch.fulfillRequest",
            requestId=request_id,
            responseCode=200,
            responseHeaders=[{"name": "Content-Type", "value": "application/json; charset=utf-8"},
                             {"name": "Cache-Control", "value": "no-store"}],
            body=base64.b64encode(body).decode("ascii"),
        )

    def until(self, needle: str, responder, *, timeout: float = 20.0) -> dict:
        """排水直到出现命中 needle 的 POST，用 responder 回包并把它记下来。

        注意：`Page.collect()` 会丢掉 `Fetch.requestPaused`，所以拦截期内的动作一律走
        `cdp.call`（事件进 cdp.events 缓冲），这里连缓冲一起排。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pending = self.app.page.cdp.drain()
            for event in pending + self.app.page.cdp.pump(0.1):
                if event.get("method") != "Fetch.requestPaused":
                    continue
                params = event.get("params") or {}
                request = params.get("request") or {}
                url = str(request.get("url") or "")
                request_id = params.get("requestId")
                if not request_id:
                    continue
                if needle in url and str(request.get("method") or "").upper() == "POST":
                    entry = {"url": url, "body": request.get("postData") or ""}
                    self.seen.append(entry)
                    self.fulfill(request_id, responder(entry))
                    return entry
                if "/api/napcat/status" in url:
                    self.fulfill(request_id, STATUS_STEADY)
                    continue
                self.app.page.cdp.call("Fetch.continueRequest", requestId=request_id)
        raise AssertionError(
            f"{timeout}s 内没等到发往 {needle} 的 POST；已拦下 {self.seen}；"
            f"#steps={self.app.text('#steps') if self.app.count('#steps') else '<无>'!r}"
        )


def _dismiss_wizard(app) -> None:
    visible = app.page.evaluate(
        "(function(){var w=document.getElementById('platform-setup-wizard');return !!(w&&!w.hidden);})()"
    )
    if visible:
        app.click("#platform-setup-wizard .platform-setup-later")
        app.wait_for("#platform-setup-wizard", state="hidden", timeout=10)


def _wait_js(app, expression: str, desc: str, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if app.page.evaluate(expression):
            return
        time.sleep(0.1)
    raise AssertionError(f"等待{desc}超时（{timeout}s）")


def _open_connect(app) -> None:
    app.goto("/")
    _dismiss_wizard(app)
    app.click("#tab-settings-button")
    app.wait_for("#tab-settings", state="visible", timeout=10)
    app.wait_for("#auto-setup", state="visible", timeout=10)


def _click(app, selector: str) -> None:
    """拦截期内的点击：只用 CDP 直发 + JS click，不走 App.click 的 settle（会吞掉 paused 事件）。

    先做一次真实命中检查，确认按钮没被遮住（否则这个剧本会假装点到了）。
    """
    info = app.page.evaluate(
        "(function(){var el=document.querySelector(%r);if(!el)return null;"
        "el.scrollIntoView({block:'center',inline:'center'});"
        "var r=el.getBoundingClientRect();"
        "if(r.width<=0||r.height<=0)return {size:false};"
        "var hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);"
        "return {size:true,onTop:!!hit&&(hit===el||el.contains(hit)),"
        "hit:hit?(hit.id?'#'+hit.id:hit.tagName.toLowerCase()):null};})()"
        % selector
    )
    assert info and info["size"], f"元素不可点击（不存在或尺寸为 0）：{selector}"
    assert info["onTop"], f"元素被遮挡，点击不会生效：{selector}（该点最上层是 {info['hit']}）"
    app.page.cdp.call(
        "Runtime.evaluate",
        expression="(function(){var el=document.querySelector(%r);if(el)el.click();return true;})()" % selector,
        userGesture=True,
    )


@scenario("27_napcat_fingerprint", viewports=("desktop",), tags=("fakes:none",))
def run(app):
    _open_connect(app)
    app.shot("settings-connect")

    # ---- 第 1–4 条：有指纹 → 步骤区显示前 16 位 + 出现真按钮 + 点下去带完整 sha ----
    with _Net(app) as net:
        _click(app, "#auto-setup")
        first = net.until("/api/napcat/autosetup", lambda entry: {
            "ok": False,
            "stage": "fingerprint_required",
            "error": "首次安装要先确认一次安装包指纹：来源 " + DOWNLOAD_URL,
            "reason": "",
            "expected_sha256": FAKE_SHA,
            "download_url": DOWNLOAD_URL,
            "fingerprint_known": False,
            "steps": [],
        })
        assert first["body"].strip() in ("", "{}"), f"首次 autosetup 不该带确认值，实际：{first['body']!r}"

        _wait_js(app, "!!document.getElementById('fingerprint-confirm')", "指纹确认按钮出现")
        steps_text = app.text("#steps")
        assert FAKE_SHA[:16] in steps_text, f"#steps 里没有指纹前 16 位；实际：{steps_text!r}"

        meta = app.page.evaluate(
            "(function(){var b=document.getElementById('fingerprint-confirm');if(!b)return null;"
            "return {tag:b.tagName,label:b.getAttribute('aria-label'),text:b.textContent};})()"
        )
        assert meta and meta["tag"] == "BUTTON", f"确认指纹的控件不是真 button：{meta}"
        assert (meta.get("label") or "").strip(), f"确认按钮没有 aria-label：{meta}"
        assert "确认" in (meta.get("text") or ""), f"确认按钮文案看不懂：{meta}"
        app.shot("fingerprint-confirm")

        app.click("#fingerprint-confirm")
        second = net.until("/api/napcat/autosetup", lambda entry: {
            "ok": True, "waiting_for_login": True, "steps": [],
        })
        payload = json.loads(second["body"] or "{}")
        assert payload.get("accept_sha256") == FAKE_SHA, (
            "点「确认安装」没把完整 sha 传回后端：%r" % (payload,)
        )

    # ---- 第 5 条：expected_sha256 为 null → 不给按钮，提示重新点一次 ----
    _open_connect(app)
    with _Net(app) as net2:
        _click(app, "#auto-setup")
        net2.until("/api/napcat/autosetup", lambda entry: {
            "ok": False,
            "stage": "fingerprint_required",
            "error": "这次没拿到指纹值，没法确认",
            "reason": "",
            "expected_sha256": None,
            "download_url": DOWNLOAD_URL,
            "fingerprint_known": False,
            "steps": [],
        })
        _wait_js(app, "String((document.getElementById('steps')||{}).textContent||'').indexOf('重新点一次')>=0",
                 "#steps 提示「重新点一次」")
        assert app.count("#fingerprint-confirm") == 0, "没有指纹值却仍然画出了「确认安装」按钮"
        app.shot("fingerprint-missing")

    app.assert_no_console_errors()
