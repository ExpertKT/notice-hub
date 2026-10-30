"""26 公告铃铛：角标 → 面板 → 全部标为已读 → 角标清零 → 刷新后仍然清零。

为什么要在剧本里拦 `/api/announcements`：
冷装实例没有公告缓存，首次加载会去外网拉，拿不到就是 `{ok:false, items:[]}`，
角标永远不出现——那样剧本要么恒绿（什么都没测到），要么被网络抖动弄红。
所以这里用一段**剧本私有的 CDP Fetch 期**（照 `12_settings_scan_states.py` 的 `_Net`
写法，不改 harness）把公告接口换成固定三条，专门测「角标 ↔ 面板 ↔ 已读状态」这条链路。

已读状态只认 localStorage 的 `nh_ann_read`：
顶部横幅看的是 `seen_ann_ids`、桌面弹窗看的是 `nh_ann_seen`，所以进来先把这两个 key
预置成「这三条公告早就看过/关掉了」，只留未读计数——否则横幅会遮住铃铛、弹窗会盖住页面。

复现：
    python -X utf8 tools/e2e/run.py --from-source 26_announce_and_bell
"""

from __future__ import annotations

import base64
import json
import time

from harness import scenario

ANNOUNCE_STUB = {
    "ok": True,
    "checking": False,
    "source": "e2e",
    "current_version": "1.1.0-beta.15",
    "items": [
        {
            "id": "e2e-ann-1",
            "date": "2026-10-30",
            "version": "1.1.0-beta.15",
            "level": "info",
            "title": "E2E 公告一：首条",
            "body": "第一条公告正文\n- 要点一\n- 要点二",
        },
        {
            "id": "e2e-ann-2",
            "date": "2026-10-29",
            "version": "1.1.0-beta.14",
            "level": "info",
            "title": "E2E 公告二：第二条",
            "body": "第二条公告正文",
        },
        {
            "id": "e2e-ann-3",
            "date": "2026-10-28",
            "version": "1.1.0-beta.13",
            "level": "warning",
            "title": "E2E 公告三：第三条",
            "body": "第三条公告正文",
        },
    ],
}
STUB_IDS = [item["id"] for item in ANNOUNCE_STUB["items"]]
BADGE_JS = (
    "(function(){var b=document.getElementById('announce-bell-badge');if(!b)return null;"
    "return {hidden:!!b.hidden,text:String(b.textContent||''),"
    "display:getComputedStyle(b).display};})()"
)
PANEL_ITEMS_JS = "document.querySelectorAll('#announce-panel-list article.announce-panel-item').length"


class _Net:
    """剧本私有的网络期：命中 `/api/announcements` 的请求回固定 JSON，其余放行。

    为什么不直接把 harness 的 collect 拿来用：`Page.collect` 只认 console/网络事件，
    `Fetch.requestPaused` 会被当未知事件丢掉，请求就永远挂着。所以这里自己排水，
    非拦截事件再塞回 `cdp.events` 交给 harness 常规处理（console 错误照样能断言到）。
    """

    def __init__(self, app) -> None:
        self.app = app
        self.page = app.page
        self.payload = ANNOUNCE_STUB
        self.hits: list[str] = []
        self.enabled = False

    def __enter__(self) -> "_Net":
        self.page.cdp.call(
            "Fetch.enable",
            patterns=[{"urlPattern": "*api/announcements*", "requestStage": "Request"}],
        )
        self.enabled = True
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False

    def close(self) -> None:
        if not self.enabled:
            return
        try:
            self.pump(0.2)
        except Exception:  # noqa: BLE001 - 收尾失败不该盖住真正的失败原因
            pass
        self.enabled = False
        try:
            self.page.cdp.call("Fetch.disable")
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ 排水
    def _serve(self, event: dict) -> None:
        params = event.get("params") or {}
        request_id = params.get("requestId")
        url = str((params.get("request") or {}).get("url") or "")
        if not request_id:
            return
        body = base64.b64encode(
            json.dumps(self.payload, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        self.page.cdp.call(
            "Fetch.fulfillRequest",
            requestId=request_id,
            responseCode=200,
            responseHeaders=[
                {"name": "Content-Type", "value": "application/json; charset=utf-8"},
                {"name": "Cache-Control", "value": "no-store"},
            ],
            body=body,
        )
        self.hits.append(url)

    def pump(self, seconds: float = 0.2) -> None:
        events = list(self.page.cdp.events) + self.page.cdp.pump(seconds)
        keep: list[dict] = []
        for event in events:
            if event.get("method") == "Fetch.requestPaused":
                self._serve(event)
            else:
                keep.append(event)
        self.page.cdp.events = keep

    def pump_until(self, expression: str, ok, *, what: str, timeout: float = 25.0):
        """一边排水一边求值，直到条件成立；每次求值前后都把暂停的请求回掉。"""
        deadline = time.monotonic() + timeout
        value = None
        while time.monotonic() < deadline:
            self.pump(0.15)
            value = self.page.evaluate(expression)
            if ok(value):
                self.pump(0.25)  # 别把还在路上的请求丢给下一次 collect
                return value
        raise AssertionError(f"{what}：{timeout}s 内没达成，最后一次看到的是 {value!r}")


def _dismiss_wizard(app) -> None:
    visible = app.page.evaluate(
        "(function(){var w=document.getElementById('platform-setup-wizard');return !!(w&&!w.hidden);})()"
    )
    if visible:
        app.click("#platform-setup-wizard .platform-setup-later")
        app.wait_for("#platform-setup-wizard", state="hidden", timeout=10)


def _seed_read_state(app) -> None:
    """只留「未读」，把横幅与弹窗挡掉：它们一个遮铃铛、一个盖页面，跟本条要测的链路无关。"""
    result = app.page.evaluate(
        "(function(){var ids=%s;"
        "localStorage.setItem('nh_ann_seen',JSON.stringify(ids));"
        "localStorage.setItem('seen_ann_ids',JSON.stringify(ids));"
        "localStorage.removeItem('nh_ann_read');"
        "return {seen:JSON.parse(localStorage.getItem('nh_ann_seen')).length,"
        "dismissed:JSON.parse(localStorage.getItem('seen_ann_ids')).length,"
        "read:localStorage.getItem('nh_ann_read')};})()" % json.dumps(STUB_IDS)
    )
    assert result["seen"] == 3 and result["dismissed"] == 3, f"预置公告已读状态失败：{result!r}"
    assert result["read"] is None, f"`nh_ann_read` 应该先清空，实际 {result['read']!r}"


def _navigate(app, net: _Net) -> None:
    """自己导航：不能用 app.goto，它的 wait_ready 末尾 collect 会吃掉暂停中的请求。"""
    app.page.cdp.call("Page.navigate", url=app.base_url + "/", timeout=30.0)
    net.pump_until(
        "document.readyState",
        lambda state: state == "complete",
        what="页面加载完成（readyState=complete）",
    )


def _open_panel(app, net: _Net) -> None:
    """点铃铛开面板。

    先把 `announceForcedAt` 设成「刚刷过」：打开面板会强制拉一次
    `/api/announcements?refresh=1`（30 秒节流），而 `Page.click` 结尾的 collect
    会把这次暂停的请求吞掉。本条测的是「角标/面板/已读」，不是「打开时要不要刷新」，
    所以把这次刷新按节流规则挡掉，避免留下个永远挂着的请求。
    """
    app.page.evaluate("window.announceForcedAt=Date.now();")
    app.click("#announce-bell")
    app.wait_for("#announce-panel", state="visible")
    net.pump(0.2)


def _badge(app) -> dict:
    value = app.page.evaluate(BADGE_JS)
    assert value, "页面上没有公告角标 #announce-bell-badge"
    return value


@scenario("26_announce_and_bell", viewports=("desktop",), tags=("fakes:none", "announce", "settings"))
def run(app) -> None:
    """角标 → 面板 → 全部已读 → 清零 → 刷新后仍清零（localStorage）。"""
    # 先落一次真实页面：App.open 只装会话 Cookie，不导航，`about:blank` 上写
    # localStorage 会抛 SecurityError（不透明源）；这一跳同时把「真实公告接口」跑掉，
    # 后面再带着拦截重载，才轮到桩数据说话。
    app.goto("/")
    _seed_read_state(app)
    _dismiss_wizard(app)

    with _Net(app) as net:
        # ① 首屏：接口返回 3 条未读 → 角标必须显示 3
        _navigate(app, net)
        net.pump_until(
            BADGE_JS,
            lambda v: isinstance(v, dict) and not v["hidden"] and v["text"] == "3",
            what="角标显示 3 条未读",
        )
        badge = _badge(app)
        assert badge["display"] != "none", f"角标在但被 CSS 藏了：{badge!r}"
        label = str(app.page.evaluate("document.getElementById('announce-bell').getAttribute('aria-label')") or "")
        assert "3 条未读" in label, f"铃铛 aria-label 没报未读数：{label!r}"
        app.shot("01-bell-unread")

        # ② 打开面板：三条都要列出来，而且都是未读
        _open_panel(app, net)
        app.assert_count("#announce-panel-list article.announce-panel-item", 3)
        app.assert_text("#announce-panel-count", "3 条未读")
        unread = app.count("#announce-panel-list article.announce-panel-item.is-unread")
        assert unread == 3, f"面板里未读条数应为 3，实际 {unread}"
        disabled = app.page.evaluate(
            "!!document.getElementById('announce-panel-read-all').disabled"
        )
        assert not disabled, "有未读时「全部标为已读」不该是禁用状态"
        app.shot("02-panel-open")

        # ③ 全部标为已读：角标清零、面板计数清空、按钮转禁用、三条都变已读
        app.click("#announce-panel-read-all")
        net.pump(0.2)
        badge = _badge(app)
        assert badge["hidden"], f"标为已读后角标还显示着：{badge!r}"
        stored = app.page.evaluate("localStorage.getItem('nh_ann_read')")
        read_ids = json.loads(stored or "[]")
        assert sorted(read_ids) == sorted(STUB_IDS), f"已读记录不对：{read_ids!r}"
        app.assert_count("#announce-panel-list article.announce-panel-item.is-unread", 0)
        assert app.text("#announce-panel-count").strip() == "", (
            f"已全部已读后计数还写着：{app.text('#announce-panel-count')!r}"
        )
        assert app.page.evaluate("!!document.getElementById('announce-panel-read-all').disabled"), (
            "没有未读时「全部标为已读」应该禁用"
        )
        app.shot("03-after-read-all")

        # ④ 刷新：公告还是那三条（接口仍被拦着，不会因为「拉不到公告」而假清零），
        #    但已读存在 localStorage 里，所以角标必须仍然是 0。
        _navigate(app, net)
        net.pump_until(
            PANEL_ITEMS_JS,
            lambda count: count == 3,
            what="刷新后公告列表重新渲染出 3 条",
        )
        net.pump(0.3)
        badge = _badge(app)
        assert badge["hidden"] and badge["text"] == "0", (
            f"刷新后角标没保持清零（已读应存在 localStorage）：{badge!r}"
        )
        label = str(app.page.evaluate("document.getElementById('announce-bell').getAttribute('aria-label')") or "")
        assert "未读" not in label, f"刷新后铃铛仍报未读：{label!r}"
        _open_panel(app, net)
        app.assert_count("#announce-panel-list article.announce-panel-item", 3)
        assert app.text("#announce-panel-count").strip() == "", (
            f"刷新后面板计数没保持清空：{app.text('#announce-panel-count')!r}"
        )
        assert app.count("#announce-panel-list article.announce-panel-item.is-unread") == 0, (
            "刷新后面板里又出现了未读条目"
        )
        app.shot("04-after-reload")

    assert net.hits, "整条链路里一次公告接口请求都没拦到，剧本没测到东西"
    app.assert_no_console_errors()
