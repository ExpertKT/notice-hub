"""27 一键接入：安装包指纹确认（首次安装必须先看一眼 sha256 再装）。

为什么要这条：指纹确认是「先停下、让用户确认一次」的安全闸。它一旦静默跳过、或确认按钮点下去
没把完整 sha 传回后端，用户就可能在不知情的情况下装了一个来源不明的包。

按钮不写死：接入卡是「状态驱动」的（同一时刻只露一个主按钮），所以先读 /api/setup/status 的
platforms.qq.engine_state / login_state，决定当前该点哪个主按钮，点之前断言它真的可见、
没有被别的元素盖住，再去拦它发出的那个请求。

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

# 状态 → 主按钮 + 它点下去会打的那个接口（按钮 id 与接口都必须对得上，否则拦不到东西）。
PRIMARY_BY_STATE = {
    "engine_missing": ("#auto-setup", "/api/napcat/autosetup"),
    "not_installed": ("#auto-setup", "/api/napcat/autosetup"),
    "installed_not_running": ("#go", "/api/napcat/launch"),
}
RUNNING_NEED_LOGIN = ("#refresh-qr", None)   # 刷新二维码不发 autosetup/install，验不了指纹
RUNNING_ONLINE = (None, None)                # 已接入：没有主按钮

# 只有这些状态能进「首次安装确认指纹」这条路。
FINGERPRINT_STATES = ("engine_missing", "not_installed")


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
    # 接入卡按状态只画一个主按钮：等「至少有一个按钮可见」，别等某个写死的 id。
    _wait_js(
        app,
        "(function(){var ids=['auto-setup','go','refresh-qr','restart-napcat'];"
        "return ids.some(function(id){var b=document.getElementById(id);"
        "return b&&!b.hidden&&b.offsetParent!==null;});})()",
        "QQ 接入卡画出主按钮",
        timeout=20,
    )


def _qq_state(app) -> tuple[str, str]:
    """当前 QQ 接入状态：以接口为准（页面按同一份状态决定露哪个按钮）。"""
    deadline = time.monotonic() + 20
    payload: dict = {}
    while time.monotonic() < deadline:
        payload = app.api_get("/api/setup/status") or {}
        qq = ((payload.get("platforms") or {}).get("qq") or {})
        if qq.get("engine_state"):
            return str(qq.get("engine_state")), str(qq.get("login_state") or "")
        time.sleep(0.2)
    raise AssertionError(f"/api/setup/status 没给出 QQ engine_state：{str(payload)[:300]}")


def _resolve_primary(app) -> tuple[str, str | None, str]:
    """按接口状态算出「该点哪个主按钮 + 它会打哪个接口」，并确认卡片上露的正是它。"""
    engine, login = _qq_state(app)
    if engine == "running":
        selector, endpoint = RUNNING_NEED_LOGIN if login != "online" else RUNNING_ONLINE
    else:
        selector, endpoint = PRIMARY_BY_STATE.get(engine, (None, None))

    visible = app.page.evaluate(
        "(function(){var ids=['auto-setup','go','refresh-qr','restart-napcat'];"
        "var shown=[];ids.forEach(function(id){var b=document.getElementById(id);"
        "if(b&&!b.hidden&&b.offsetParent!==null)shown.push('#'+id);});return shown;})()"
    )
    if selector is None:
        raise AssertionError(
            f"状态 engine_state={engine} login_state={login} 不该有主按钮，但卡片露了 {visible}"
        )
    assert selector in (visible or []), f"状态={engine}/{login} 应露出 {selector}，实际露出 {visible}"
    # 同一时刻只该有一个主按钮：多露一个就是「状态驱动」没生效。
    assert len(visible) <= 2, f"接入卡同时露出 {len(visible)} 个按钮（应为 1 主 + 至多 1 次）：{visible}"
    return selector, endpoint, engine


def _click(app, selector: str) -> None:
    """拦截期内的点击：只用 CDP 直发 + JS click，不走 App.click 的 settle（会吞掉 paused 事件）。

    先做一次真实命中检查，确认按钮没被遮住（否则这个剧本会假装点到了）。
    """
    info = app.page.evaluate(
        "(function(){var el=document.querySelector(%r);if(!el)return null;"
        "if(el.hidden)return {hidden:true};"
        "el.scrollIntoView({block:'center',inline:'center'});"
        "var r=el.getBoundingClientRect();"
        "if(r.width<=0||r.height<=0)return {size:false,hidden:!!el.hidden};"
        "var hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);"
        "return {size:true,onTop:!!hit&&(hit===el||el.contains(hit)),"
        "hit:hit?(hit.id?'#'+hit.id:hit.tagName.toLowerCase()):null};})()"
        % selector
    )
    assert info, f"找不到按钮：{selector}"
    assert not info.get("hidden"), f"按钮被状态卡隐藏了（hidden=true）：{selector}"
    assert info.get("size"), f"元素不可点击（不存在或尺寸为 0）：{selector}"
    assert info["onTop"], f"元素被遮挡，点击不会生效：{selector}（该点最上层是 {info['hit']}）"
    app.page.cdp.call(
        "Runtime.evaluate",
        expression="(function(){var el=document.querySelector(%r);if(el)el.click();return true;})()" % selector,
        userGesture=True,
    )


def _fingerprint_failed(app, selector: str) -> AssertionError:
    """点完按钮却没有确认按钮时，把用户此刻真正看得到的文案带出来。"""
    shown = app.page.evaluate(
        "(function(){var out={};"
        "['steps','install-result','status','qq-next-step'].forEach(function(id){"
        "var el=document.getElementById(id);"
        "if(el&&!el.hidden)out[id]=String(el.textContent||'').trim().slice(0,160);});"
        "return out;})()"
    )
    return AssertionError(
        f"后端回了 stage=fingerprint_required，但页面没画出 #fingerprint-confirm，用户没地方确认。"
        f"点的是 {selector}；页面可见文案：{shown}"
    )


@scenario("27_napcat_fingerprint", viewports=("desktop",), tags=("fakes:none",))
def run(app):
    _open_connect(app)
    app.shot("settings-connect")

    selector, endpoint, engine = _resolve_primary(app)
    assert engine in FINGERPRINT_STATES, (
        f"这条剧本验的是「首次安装确认指纹」，需要 engine_state ∈ {FINGERPRINT_STATES}，实际 {engine}"
    )
    assert endpoint, f"冷装状态却没解析出接口：{selector}"

    # ---- 第 1–4 条：有指纹 → 步骤区显示前 16 位 + 出现真按钮 + 点下去带完整 sha ----
    with _Net(app) as net:
        _click(app, selector)
        first = net.until(endpoint, lambda entry: {
            "ok": False,
            "stage": "fingerprint_required",
            "error": "首次安装要先确认一次安装包指纹：来源 " + DOWNLOAD_URL,
            "reason": "",
            "expected_sha256": FAKE_SHA,
            "download_url": DOWNLOAD_URL,
            "fingerprint_known": False,
            "steps": [],
        })
        assert first["body"].strip() in ("", "{}"), f"首次请求不该带确认值，实际：{first['body']!r}"

        try:
            _wait_js(app, "!!document.getElementById('fingerprint-confirm')", "指纹确认按钮出现", timeout=15)
        except AssertionError:
            raise _fingerprint_failed(app, selector) from None

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

        # 用 _click 而不是 App.click：后者会 settle 掉 Fetch.requestPaused，
        # 确认按钮发出的那次 autosetup 就会被我们自己的拦截器吞掉（请求永远停在 paused）。
        _click(app, "#fingerprint-confirm")
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
        selector2, endpoint2, _ = _resolve_primary(app)
        _click(app, selector2)
        net2.until(endpoint2, lambda entry: {
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
                 "#steps 提示「重新点一次」", timeout=15)
        assert app.count("#fingerprint-confirm") == 0, "没有指纹值却仍然画出了「确认安装」按钮"
        app.shot("fingerprint-missing")

    app.assert_no_console_errors()
