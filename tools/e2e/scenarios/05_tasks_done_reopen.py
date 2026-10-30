"""05 待办：标完成 → 进「已完成」分组 → 再恢复，计数跟着变。

走的是卡片上的 .check 按钮（用户唯一入口），不是直接打接口；完成/恢复都要等界面自己刷新。

注意：`<details>` 收起时，里面的卡片仍量得到非零 getBoundingClientRect（Chromium 行为），
所以这里不靠 `wait_for(..., "visible"/"hidden")` 判断分组开合，直接读 `details.open`。
"""

import time

from harness import scenario


def _dismiss_wizard(app) -> None:
    if app.count("#platform-setup-wizard") and app.page.evaluate(
        "(function(){var w=document.getElementById('platform-setup-wizard');return !!(w&&!w.hidden);})()"
    ):
        app.click("#platform-setup-wizard .platform-setup-later")
        app.wait_for("#platform-setup-wizard", state="hidden", timeout=10)


def _wait_until(app, expression: str, desc: str, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if app.page.evaluate(expression):
            return
        time.sleep(0.1)
    raise AssertionError(f"等待{desc}超时（{timeout}s）")


@scenario("05_tasks_done_reopen", viewports=("desktop",), tags=("fakes:none",))
def run(app):
    app.goto("/")
    _dismiss_wizard(app)
    app.wait_for("#tab-tasks", state="visible", timeout=15)

    created = app.api_post("/api/tasks/create", {"summary": "E2E：完成再恢复的待办"})
    task_id = str(created.get("task_id") or "")
    assert task_id, f"新建待办没返回 task_id：{created}"

    card = f"#tab-tasks #task-{task_id}"
    app.goto("/")
    _dismiss_wizard(app)
    app.wait_for(card, state="visible", timeout=15)
    app.assert_attr(card + " .check", "aria-label", "标记完成")
    app.shot("open-task")

    # 标完成：卡片挪进「已完成」分组（details 收起），计数变 1。
    app.click(card + " .check")
    _wait_until(
        app,
        "(function(){var c=document.querySelector(%r);if(!c)return false;"
        "var d=c.closest('details');return !!d&&!d.open;})()"
        % f"#tab-tasks details.section.done #task-{task_id}",
        "卡片进入已完成的折叠分组",
    )
    app.assert_text("#stat-done", "已完成 1")

    # 展开「已完成」才看得到卡片；按钮语义必须变成「恢复待办」。
    app.click("#tab-tasks details.section.done > summary")
    _wait_until(
        app,
        "(function(){var d=document.querySelector('#tab-tasks details.section.done');"
        "return !!d&&d.open;})()",
        "已完成分组展开",
    )
    app.assert_attr(f"#tab-tasks details.section.done #task-{task_id} .check", "aria-label", "恢复待办")
    app.shot("done-expanded")

    # 恢复：卡片离开「已完成」，计数归零。
    app.click(f"#tab-tasks details.section.done #task-{task_id} .check")
    _wait_until(
        app,
        "(function(){return !document.querySelector(%r);})()"
        % f"#tab-tasks details.section.done #task-{task_id}",
        "卡片离开已完成分组",
    )
    app.assert_text("#stat-done", "已完成 0")
    app.goto("/")
    _dismiss_wizard(app)
    app.wait_for(card, state="visible", timeout=15)
    app.assert_attr(card + " .check", "aria-label", "标记完成")
    app.shot("reopened")

    app.assert_no_console_errors()
