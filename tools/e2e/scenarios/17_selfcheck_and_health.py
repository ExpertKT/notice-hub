"""服务自检 + 总览健康卡：点「重新检测」→ 真实自检接口 → 平台行渲染正确 → 行内「去设置」能跳设置页。

全部用真实 DOM 与真实接口（`GET /api/selfcheck`），不 mock：这条路径慢的时候要 10–20 秒
（NapCat 端口、微信目录、Tailscale 子进程都是真探），所以等的是「列表不再 pending」而不是固定 sleep。

复现：
    python tools/e2e/run.py --only 17_selfcheck_and_health
"""

from __future__ import annotations

import time

from harness import scenario

PLATFORM_LABELS = ("QQ 登录", "微信", "钉钉")
STATES = {"ok", "warn", "bad"}


def _dismiss_wizard(app) -> None:
    """首访向导是全屏遮罩，盖着右栏；按真实用户的做法点「稍后配置」。"""
    visible = app.page.evaluate(
        "(function(){var w=document.getElementById('platform-setup-wizard');return !!(w&&!w.hidden);})()"
    )
    if visible:
        app.click("#platform-setup-step-1 .platform-setup-later")
        app.wait_for("#platform-setup-wizard", state="hidden", timeout=10)


def _wait_health(app, *, timeout: float = 120.0) -> dict:
    """等自检列表真正渲染完（不再有 pending 行）。返回列表快照。"""
    deadline = time.monotonic() + timeout
    snapshot = {"rows": [], "message": ""}
    while time.monotonic() < deadline:
        snapshot = app.page.evaluate(
            "(function(){var list=document.getElementById('rail-health-list');"
            "var msg=document.getElementById('rail-health-message');"
            "var rows=[].slice.call(list?list.querySelectorAll('.health-row'):[]).map(function(row){"
            "var label=row.querySelector('.health-label'),value=row.querySelector('.health-value');"
            "return {state:row.dataset.state||'',label:label?label.textContent.trim():'',"
            "value:value?value.textContent.trim():'',text:row.textContent.trim()};});"
            "return {rows:rows,message:msg?msg.textContent.trim():''};})()"
        )
        rows = snapshot["rows"]
        if rows and all(row["state"] != "pending" for row in rows):
            return snapshot
        app.settle(1.0)
    raise AssertionError(f"自检 {timeout}s 内没有渲染完：{snapshot}")


@scenario("17_selfcheck_and_health", viewports=("desktop",), tags=("fakes:none", "selfcheck"))
def run(app) -> None:
    """首页自检面板：状态行来自 /api/selfcheck，平台行齐全，行内按钮能跳设置。"""
    app.goto("/")
    _dismiss_wizard(app)
    app.wait_for("#rail-health", timeout=15)
    app.wait_for("#rail-recheck", timeout=15)
    app.shot("01-home")

    # 用户主动点「重新检测」：走的是真接口，不是缓存
    app.click("#rail-recheck")
    health = _wait_health(app)
    rows = health["rows"]
    message = health["message"]
    app.shot("02-selfcheck")

    assert len(rows) >= 5, f"自检只渲染出 {len(rows)} 行：{[row['label'] for row in rows]}"
    labels = [row["label"] for row in rows]
    for expect in PLATFORM_LABELS:
        assert expect in labels, f"自检缺少「{expect}」行；实际：{labels}"

    # 页面这一行不能自己编状态：每行的 state 必须等于接口同一项的 state。
    api_report = app.api_get("/api/selfcheck") or {}
    api_items = [item for item in (api_report.get("items") or []) if isinstance(item, dict)]
    assert api_items, f"/api/selfcheck 没返回 items：{api_report}"
    api_states = {str(item.get("label")): str(item.get("state")) for item in api_items}
    for row in rows:
        expected_state = api_states.get(row["label"])
        assert expected_state is not None, f"接口里没有「{row['label']}」这一项，页面却渲染了它"
        assert row["state"] == expected_state, (
            f"「{row['label']}」页面显示 {row['state']!r}，接口是 {expected_state!r}"
        )
    for row in rows:
        assert row["state"] in STATES, f"「{row['label']}」状态异常：{row['state']!r}"
        assert "读取失败" not in row["text"], f"「{row['label']}」行出现读取失败：{row['text']!r}"
    assert "读取失败" not in message, f"自检整块读取失败：{message!r}"
    assert "正在检测" not in message, f"自检停在「正在检测」：{message!r}"

    # 行数也要对得上：页面渲染的行数必须等于接口 items 条数，不能多也不能少。
    assert len(rows) == len(api_items), (
        f"页面渲染 {len(rows)} 行，接口 items 有 {len(api_items)} 条：{labels} vs "
        f"{[item.get('label') for item in api_items]}"
    )

    # 页头计数（「有 N 项需要留意」）同样得来自接口，不能是剧本自己假设的数字。
    expected_bad = sum(1 for item in api_items if str(item.get("state")) != "ok")
    if expected_bad:
        want = f"有 {expected_bad} 项需要留意，下面标了怎么修。"
        assert message == want, f"自检计数与接口不一致：页面 {message!r}，接口 state!=ok 共 {expected_bad} 项"
    else:
        assert message == "全部正常。", f"接口全 ok，页面却说 {message!r}"

    # 总览健康卡行内的「去设置」：冷装没有推送通道，推送行必然是 warn，因此按钮一定在
    button_count = app.count("#rail-health-list button")
    assert button_count == 1, f"健康行内的「去设置」按钮应有 1 个，实际 {button_count}"
    app.assert_text("#rail-health-list button", "去设置")
    app.click("#rail-health-list button")
    app.wait_for("#tab-settings", state="visible", timeout=10)
    app.assert_attr("#tab-settings-button", "aria-selected", "true")
    app.shot("03-settings-from-health")

    app.assert_no_console_errors()
