"""23 更新通道：stable → beta 走设置页真实单选与保存，off 走接口（UI 没有这个选项）。

为什么单独写 off：设置页目前只暴露「稳定版 / 测试版」两档，停用更新没有单选。
所以前两档必须证明「点单选 + 点保存 → 状态行 + 接口回读都一致」，
第三档用 `POST /api/settings {"update_channel":"off"}` 证明后端支持、回读一致，
并在剧本里写清「UI 缺这一档」。全程不触发检查/下载。

复现：
    python tools/e2e/run.py --only 23_settings_update_channel
"""

from __future__ import annotations

import time

from harness import scenario


def _dismiss_wizard(app) -> None:
    visible = app.page.evaluate(
        "(function(){var w=document.getElementById('platform-setup-wizard');"
        "return !!(w&&!w.hidden);})()"
    )
    if visible:
        app.click("#platform-setup-wizard .platform-setup-later")
        app.wait_for("#platform-setup-wizard", state="hidden", timeout=10)


def _open_settings(app) -> None:
    app.click("#tab-settings-button")
    app.wait_for("#tab-settings", state="visible", timeout=10)
    app.wait_for("#update-channel-save", timeout=20)


def _wait_text(app, selector: str, expect: str, timeout: float = 20.0) -> str:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            last = app.text(selector)
        except AssertionError:
            last = ""
        if expect in last:
            return last
        app.settle(0.1)
    raise AssertionError(f"{selector} 等不到「{expect}」（{timeout}s）；最后一次：{last!r}")


def _click_scrolled(app, selector: str, settle: float = 0.3) -> None:
    """先滚到位再点：长页面里直接点会打到旧坐标上的元素。"""
    app.page.evaluate(
        "(function(){var el=document.querySelector(%s);if(!el)return false;"
        "el.scrollIntoView({block:'center',inline:'center'});return true;})()" % __import__("json").dumps(selector)
    )
    app.settle(settle)
    app.click(selector)


def _channel(app) -> str:
    """通道的权威回读在 /api/meta 的 channel 字段（/api/settings 里没有这一项）。"""
    return str(app.api_get("/api/meta").get("channel") or "")


def _checked(app):
    return app.page.evaluate(
        "(function(){var el=document.querySelector('#tab-settings input[name=\\\"update-channel\\\"]:checked');"
        "return el?el.value:null;})()"
    )


def _pick(app, value: str) -> None:
    """切通道单选。

    坑：input 外面还包着一层 <label>，直接对 input 派发鼠标事件会被 label 的默认行为
    再翻转一次，结果仍是「没选中」。所以先点 label 文本；还不行就派发一次 label 的
    DOM click（与用户点文字等价），两条路都失败才报错。
    """
    if _checked(app) == value:
        return
    label = '#tab-settings label:has(input[name="update-channel"][value="%s"])' % value
    try:
        _click_scrolled(app, label)
    except AssertionError:
        pass
    for _ in range(15):
        if _checked(app) == value:
            return
        app.settle(0.1)
    app.page.evaluate(
        "(function(){var l=document.querySelector('#tab-settings label:has(input[name=\\\"update-channel\\\"]"
        "[value=\\\"%s\\\"])');if(l)l.click();})()" % value
    )
    for _ in range(15):
        if _checked(app) == value:
            return
        app.settle(0.1)
    raise AssertionError(f"单选没切到 {value!r}，实际 {_checked(app)!r}")


def _save(app, value: str) -> None:
    _pick(app, value)
    app.click("#tab-settings #update-channel-save")
    _wait_text(app, "#tab-settings #update-channel-result", "已保存")
    assert _channel(app) == value, f"保存 {value!r} 后接口回读是 {_channel(app)!r}"
    # 保存后必须**恰好一个**单选被选中（不是 0 个也不是 2 个）：这是「页面对用户
    # 只呈现一种状态」的最小要求，之前只写 <=1 会把「一个都没选中」也放过。
    checked = app.count('#tab-settings input[name="update-channel"]:checked')
    assert checked == 1, f"保存 {value!r} 后选中了 {checked} 个通道（应恰好 1 个）"
    assert _checked(app) == value, f"保存 {value!r} 后选中的是 {_checked(app)!r}"


@scenario("23_settings_update_channel", viewports=("desktop",), tags=("fakes:none", "settings", "update"))
def run(app) -> None:
    """三档通道：stable → beta → off，状态行与接口回读必须一致，且不触发下载。"""
    app.goto("/")
    _dismiss_wizard(app)
    _open_settings(app)
    app.shot("01-update-card")

    # UI 只有两档：先把这一点钉住，避免以后有人以为 off 能在页面上选
    options = app.page.evaluate(
        "(function(){return Array.prototype.map.call("
        "document.querySelectorAll('#tab-settings input[name=\"update-channel\"]'),"
        "function(el){return el.value;});})()"
    )
    assert sorted(options or []) == ["beta", "stable"], f"更新通道单选项变了：{options!r}"
    # 冷装实例首次打开时可能还没有默认选中（updateState 是异步拉的），所以这里只要求「最多一个」；
    # 严格到「恰好 1 个」的断言放在 _save() 里——保存过的通道必须单选且值正确。
    assert app.count('#tab-settings input[name="update-channel"]:checked') <= 1, "同时选中了多个通道"

    # ① stable
    _save(app, "stable")
    app.assert_text("#tab-settings #update-channel-result", "已保存")
    app.shot("02-stable")

    # ② beta
    _save(app, "beta")
    app.assert_text("#tab-settings #update-channel-result", "已保存")
    app.shot("03-beta")

    # ③ off：**产品没有这一档**。后端 `_save_update_channel_settings` 只认 stable/beta，
    # 其它值一律收敛成空串（settings_api.py 里 `if channel not in {"stable","beta"}: channel = ""`），
    # 也就是说「关闭更新」既不在 UI 里、也不在通道枚举里。这里断言这个既有行为，
    # 不假装支持 off；真要「不检查更新」得另开需求。
    payload = app.api_post("/api/settings", {"update_channel": "off"})
    assert payload.get("ok") is True, f"接口保存 off 返回失败：{payload!r}"
    assert payload.get("update_channel") == "", f"off 应被收敛成空串，实际 {payload.get('update_channel')!r}"
    assert _channel(app) != "off", f"后端不该把 off 当有效通道，回读是 {_channel(app)!r}"
    env_text = (app.install.install_dir / ".env").read_text(encoding="utf-8", errors="replace")
    env_lines = [line for line in env_text.splitlines() if line.startswith("QQ_DIGEST_UPDATE_CHANNEL=")]
    assert env_lines and env_lines[0].strip() == "QQ_DIGEST_UPDATE_CHANNEL=", f".env 里的通道没被清空：{env_lines!r}"
    # 刷新后页面不能崩，且不会出现第三个单选
    app.goto("/")
    _open_settings(app)
    assert app.count('#tab-settings input[name="update-channel"]') == 2, "刷新后通道选项数量变了"
    app.shot("04-off")

    # 全程不许真的触发下载：状态行里不能出现下载中的字样（「正在检查更新…」是页面自带的自动检查，允许）
    # 元素必须真的存在：把它 try 成空串再断言「两个词不在里面」，在元素缺失时是**恒真**的假绿。
    assert app.count("#update-status") == 1, (
        "设置页里没有更新状态行 #update-status（是元素不存在，不是文案为空）"
    )
    status_text = app.text("#update-status")
    assert "正在下载" not in status_text and "下载中" not in status_text, f"不该在下载中：{status_text!r}"

    app.assert_no_console_errors()
