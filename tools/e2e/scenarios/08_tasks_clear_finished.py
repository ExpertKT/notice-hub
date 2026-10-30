"""08 待办：「清空已完成、已忽略、已过期」（两段式确认，范围写清）。

为什么：清空是破坏性操作。文案必须写清清掉的是哪三类，第一次点击只能进确认态，
第二次才真清，且清完计数归零、不能把没完成的任务一起清掉。

复现：
    python tools/e2e/run.py --only 08_tasks_clear_finished
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


@scenario("08_tasks_clear_finished", viewports=("desktop",), tags=("fakes:none",))
def run(app):
    app.goto("/")
    _dismiss_wizard(app)
    keep = str(app.api_post("/api/tasks/create", {"summary": "E2E：这条要留着没完成"})["task_id"])
    gone = str(app.api_post("/api/tasks/create", {"summary": "E2E：这条已完成要被清掉"})["task_id"])
    ignored = str(app.api_post("/api/tasks/create", {"summary": "E2E：这条被忽略也要清掉"})["task_id"])
    app.api_post(f"/api/tasks/{gone}", {"action": "done"})
    app.api_post(f"/api/tasks/{ignored}", {"action": "dismiss"})

    app.goto("/")
    _dismiss_wizard(app)
    app.wait_for("#tasks-clear-done", state="visible", timeout=15)
    app.assert_text("#stat-done", "已完成 1")

    clear = "#tasks-clear-done"
    app.click(clear)
    app.assert_text(clear, "确认清空？已完成、已忽略、已过期（4 秒内再点一次）")
    # 第一次点击不许真清。
    assert app.count(f"#tab-tasks details.section.done #task-{gone}") == 1, "第一次点击就把已完成清掉了"
    app.shot("clear-confirm")

    app.click(clear)
    _wait_js(app, "!document.querySelector(%r)" % f"#tab-tasks details.section.done #task-{gone}", "已完成被清空")
    app.assert_text("#stat-done", "已完成 0")
    app.assert_text(clear, "清空已完成")

    # 没完成的那条必须还在。
    app.wait_for(f"#tab-tasks #task-{keep}", state="visible", timeout=15)
    data = app.api_get("/api/tasks") or {}
    ids = set()
    for value in data.values():
        if isinstance(value, list):
            ids.update(str(item.get("id")) for item in value if isinstance(item, dict))
    assert gone not in ids, "清空后已完成任务还留在接口里"
    assert ignored not in ids, "清空后被忽略任务还留在接口里"
    assert keep in ids, "清空把没完成的任务也清掉了"
    app.shot("cleared")

    app.assert_no_console_errors()
