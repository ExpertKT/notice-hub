"""01 冒烟：冷装首开能起来、四个 tab 能切、页面无异常、无失败请求。

这是所有剧本的地基：它红了，别的问题都不可信。
"""

from harness import scenario

TABS = [
    ("#tab-tasks-button", "#tab-tasks"),
    ("#tab-notices-button", "#tab-notices"),
    ("#tab-inbox-button", "#tab-inbox"),
    ("#tab-settings-button", "#tab-settings"),
]


def _dismiss_wizard(app) -> None:
    if app.count("#platform-setup-wizard") and app.page.evaluate(
        "(function(){var w=document.getElementById('platform-setup-wizard');return !!(w&&!w.hidden);})()"
    ):
        app.click("#platform-setup-wizard .platform-setup-later")
        app.wait_for("#platform-setup-wizard", state="hidden", timeout=10)


@scenario("01_smoke_boot", viewports=("desktop", "mobile"), tags=("fakes:none",))
def run(app):
    app.goto("/")
    app.wait_for("#tab-tasks-button")

    # 冷装首开可能带引导层；先收掉再测 tab（否则点击会被遮罩吞掉）。
    _dismiss_wizard(app)

    app.assert_count("#tab-tasks-button", 1)
    app.assert_attr("#tab-tasks-button", "aria-selected", "true")

    for button, panel in TABS:
        app.click(button)
        app.wait_for(panel, state="visible", timeout=10)
        app.assert_attr(button, "aria-selected", "true")
        # 其它面板必须同时收起：切了还留着就是叠层。
        for _, other in TABS:
            if other != panel:
                app.wait_for(other, state="hidden", timeout=10)

    app.click("#tab-tasks-button")
    app.wait_for("#tab-tasks", state="visible", timeout=10)
    app.shot("tabs")

    app.assert_no_console_errors()
    app.assert_no_failed_requests()
