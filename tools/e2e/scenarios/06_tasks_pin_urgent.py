"""06 待办：置顶 / 取消置顶 / 紧急切换 / 恢复跟随自动判断。

为什么：置顶和紧急是用户手动盖过 AI 判断的唯一入口，按钮状态与后端不一致时
用户会以为「点了没生效」或「取消了还生效」。

复现：
    python tools/e2e/run.py --only 06_tasks_pin_urgent
"""

import time

from harness import scenario


def _dismiss_wizard(app) -> None:
    if app.count("#platform-setup-wizard") and app.page.evaluate(
        "(function(){var w=document.getElementById('platform-setup-wizard');return !!(w&&!w.hidden);})()"
    ):
        app.click("#platform-setup-wizard .platform-setup-later")
        app.wait_for("#platform-setup-wizard", state="hidden", timeout=10)


def _wait_js(app, expression: str, desc: str, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if app.page.evaluate(expression):
            return
        time.sleep(0.1)
    raise AssertionError(f"等待{desc}超时（{timeout}s）")


def _task_row(app, task_id: str) -> dict:
    data = app.api_get("/api/tasks") or {}
    for value in data.values():
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, dict) and str(item.get("id")) == str(task_id):
                return item
    return {}


@scenario("06_tasks_pin_urgent", viewports=("desktop",), tags=("fakes:none",))
def run(app):
    app.goto("/")
    _dismiss_wizard(app)
    created = app.api_post("/api/tasks/create", {"summary": "E2E：置顶与紧急"})
    task_id = str(created.get("task_id") or "")
    assert task_id, f"新建待办没返回 task_id：{created}"

    app.goto("/")
    _dismiss_wizard(app)
    card = f"#tab-tasks #task-{task_id}"
    app.wait_for(card, state="visible", timeout=15)
    pin = card + " .pin-toggle"
    urgent = card + " .urgent-toggle"

    # 置顶
    app.assert_attr(pin, "aria-pressed", "false")
    app.click(pin)
    _wait_js(app, "(function(){var b=document.querySelector(%r);return !!b&&b.getAttribute('aria-pressed')==='true';})()" % pin,
             "置顶按钮变成已置顶")
    app.assert_text(pin, "已置顶")
    assert _task_row(app, task_id).get("pinned") is True, "界面显示已置顶，但接口里 pinned 不是 true"
    app.shot("pinned")

    # 取消置顶
    app.click(pin)
    _wait_js(app, "(function(){var b=document.querySelector(%r);return !!b&&b.getAttribute('aria-pressed')==='false';})()" % pin,
             "置顶按钮恢复未置顶")
    assert not _task_row(app, task_id).get("pinned"), "界面显示未置顶，但接口里 pinned 还是 true"

    # 紧急：开 → 出现「跟随自动判断」复位按钮 → 复位
    app.click(urgent)
    _wait_js(app, "(function(){var b=document.querySelector(%r);return !!b&&b.getAttribute('aria-pressed')==='true';})()" % urgent,
             "紧急按钮变成已标记")
    app.wait_for(card + " .urgent-reset", state="visible", timeout=10)
    app.shot("urgent")

    app.click(card + " .urgent-reset")
    _wait_js(app, "!document.querySelector(%r)" % (card + " .urgent-reset"), "紧急复位按钮消失（回到跟随自动判断）")
    assert _task_row(app, task_id).get("urgent_override") in (None, ""), (
        "复位后 urgent_override 应该为空（跟随自动判断），实际：%r" % (_task_row(app, task_id).get("urgent_override"),)
    )
    app.shot("urgent-reset")

    app.assert_no_console_errors()
