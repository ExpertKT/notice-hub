"""25 托管开关：开始托管在冷装（无 NapCat/QQ 登录）环境下必须「快速给出可行动的失败」，
不许卡死；结束托管同样要落到确定状态。

为什么这么写：冷装机器上没有 NapCat、QQ 也没登录，「开始托管」注定要失败，
但产品必须把原因说清楚（提示去装 NapCat / 登录 QQ），并且按钮要恢复可点。
所以断言是「失败但可行动、不卡死」，不是「必须成功」。

复现：
    python tools/e2e/run.py --only 25_hosting_toggle
"""

from __future__ import annotations

import time

from harness import scenario

ACTION_HINTS = ("NapCat", "napcat", "QQ", "未确认", "失败", "无法", "没有", "未启动")
# 与 settings.js 里传给 changeHosting 的 pending 文案一字不差
PENDING_START = "正在开始托管，请稍候…"
PENDING_STOP = "正在结束托管，请稍候…"


def _dismiss_wizard(app) -> None:
    visible = app.page.evaluate(
        "(function(){var w=document.getElementById('platform-setup-wizard');return !!(w&&!w.hidden);})()"
    )
    if visible:
        app.click("#platform-setup-wizard .platform-setup-later")
        app.wait_for("#platform-setup-wizard", state="hidden", timeout=10)


def _click_scrolled(app, selector: str, settle: float = 0.3) -> None:
    app.page.evaluate(
        "(function(){var el=document.querySelector(%s);if(!el)return false;"
        "el.scrollIntoView({block:'center',inline:'center'});return true;})()"
        % __import__("json").dumps(selector)
    )
    app.settle(settle)
    app.click(selector)


def _wait_settled(app, button_id: str, pending: str, timeout: float = 45.0) -> str:
    """等托管操作落定：**被点的那个按钮**恢复可点，且结果行已离开进行中文案并稳定。

    为什么按按钮而不是按结果文案判：changeHosting 只对被点的按钮做
    `disabled=true` → `.then(... button.disabled=false)`；结果行在点击瞬间就被
    写成「正在…请稍候…」，如果只等文案稳定，会在请求还没回来时提前返回，
    下一个断言就会把「请求进行中」误判成「按钮卡死」。
    """
    deadline = time.monotonic() + timeout
    last = ""
    stable = 0
    while time.monotonic() < deadline:
        try:
            text = app.text("#tab-settings #hosting-result").strip()
        except AssertionError:
            text = ""
        busy = app.page.evaluate(
            "(function(){var b=document.getElementById(%s);return !!(b&&b.disabled);})()"
            % __import__("json").dumps(button_id)
        )
        if text and text != pending and text == last and not busy:
            stable += 1
            if stable >= 3:
                return text
        else:
            stable = 0
        last = text
        app.settle(0.25)
    raise AssertionError(
        f"托管操作 {timeout}s 没有落定（按钮 {button_id} 仍禁用或结果行仍是进行中文案：{last!r}，可能卡死）"
    )


def _status_text(app) -> str:
    try:
        return app.text("#tab-settings #hosting-status").strip()
    except AssertionError:
        return ""


@scenario("25_hosting_toggle", viewports=("desktop",), tags=("fakes:none", "settings", "hosting"))
def run(app) -> None:
    """无 NapCat 环境：开始→可行动失败；结束→落定；两步都不能把页面点死。"""
    app.goto("/")
    _dismiss_wizard(app)
    app.click("#tab-settings-button")
    app.wait_for("#tab-settings", state="visible", timeout=10)
    app.wait_for("#hosting-start", timeout=20)
    status_before = _status_text(app)
    assert "读取" not in status_before or status_before, f"托管状态行异常：{status_before!r}"
    app.shot("01-hosting-card")

    # ① 开始托管：等它落定（成功或失败都行，但必须快、必须说明原因）
    _click_scrolled(app, "#tab-settings #hosting-start")
    result = _wait_settled(app, "hosting-start", PENDING_START)
    app.shot("02-after-start")
    if "已开始" in result or "成功" in result:
        # 真开始了：状态胶囊必须跟着变
        assert _status_text(app), "开始托管成功但状态行为空"
    else:
        assert any(hint in result for hint in ACTION_HINTS), (
            f"失败提示没有可行动信息（应提到 NapCat/QQ/原因）：{result!r}"
        )
    # 按钮必须恢复可点（busy 状态结束），页面不能卡死
    disabled = app.page.evaluate(
        "(function(){var b=document.getElementById('hosting-start');return !!(b&&b.disabled);})()"
    )
    assert not disabled, "开始托管后按钮一直处于禁用状态（看起来卡死）"

    # ② 结束托管：同样落定即可，不要求一定成功
    _click_scrolled(app, "#tab-settings #hosting-stop")
    result_stop = _wait_settled(app, "hosting-stop", PENDING_STOP)
    app.shot("03-after-stop")
    disabled_stop = app.page.evaluate(
        "(function(){var b=document.getElementById('hosting-stop');return !!(b&&b.disabled);})()"
    )
    assert not disabled_stop, "结束托管后按钮一直处于禁用状态（看起来卡死）"
    assert result_stop, "结束托管后结果行为空（用户看不到结果）"
    if "已结束" in result_stop or "成功" in result_stop:
        # 真结束了：状态胶囊必须跟着回到「未开始」
        assert "未开始" in _status_text(app), (
            f"结束托管成功但状态行没回到未开始：{_status_text(app)!r}"
        )
    app.assert_no_console_errors()
