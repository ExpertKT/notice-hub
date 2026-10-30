"""07 待办：已完成卡片的「删除」（两段式确认，真删而不是挪进「已忽略」）。

为什么：这条以前走的是 dismiss（把任务挪进「已忽略」并污染忽略率）；现在应该是真删，
而且第一次点击只能进确认态，不能直接删掉。

复现：
    python tools/e2e/run.py --only 07_tasks_done_delete
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


@scenario("07_tasks_done_delete", viewports=("desktop",), tags=("fakes:none",))
def run(app):
    app.goto("/")
    _dismiss_wizard(app)
    created = app.api_post("/api/tasks/create", {"summary": "E2E：删掉这条已完成的"})
    task_id = str(created.get("task_id") or "")
    assert task_id, f"新建待办没返回 task_id：{created}"
    app.api_post(f"/api/tasks/{task_id}", {"action": "done"})

    app.goto("/")
    _dismiss_wizard(app)
    done_card = f"#tab-tasks details.section.done #task-{task_id}"
    _wait_js(app, "!!document.querySelector(%r)" % done_card, "卡片进入已完成分组")
    app.click("#tab-tasks details.section.done > summary")
    _wait_js(app, "(function(){var d=document.querySelector('#tab-tasks details.section.done');return !!d&&d.open;})()",
             "已完成分组展开")

    button = done_card + " button[aria-label='删除这条已完成任务']"
    app.wait_for(button, state="visible", timeout=10)
    app.click(button)
    app.assert_text(button, "确认删除？（4 秒内再点一次）")
    # 第一次点击不许真删。
    assert app.count(done_card) == 1, "第一次点击就把卡片删掉了——应该是两段式确认"
    app.shot("delete-confirm")

    app.click(button)
    _wait_js(app, "!document.querySelector(%r)" % done_card, "卡片被真正删除")
    app.assert_text("#stat-done", "已完成 0")
    app.shot("deleted")

    remaining = app.api_get("/api/tasks") or {}
    for value in remaining.values():
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and str(item.get("id")) == task_id:
                    raise AssertionError(f"界面删掉了，但接口里任务还在：{item}")
    app.assert_no_console_errors()
