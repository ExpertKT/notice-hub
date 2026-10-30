"""02 首次运行：平台选择引导层必须出现、可「稍后配置」关掉，且关掉后主界面可用。

用户第一次打开程序看到的就是这一层；它不能挡死、不能关不掉、关掉后不能再弹回来挡操作。
"""

from harness import scenario

WIZARD = "#platform-setup-wizard"


def _wizard_visible(app) -> bool:
    return bool(app.page.evaluate(
        "(function(){var w=document.getElementById('platform-setup-wizard');return !!(w&&!w.hidden);})()"
    ))


@scenario("02_first_run_wizard", viewports=("desktop",), tags=("fakes:none",))
def run(app):
    app.goto("/")
    app.wait_for(WIZARD, state="visible", timeout=15)
    app.shot("wizard-open")

    # 文案得说清这是「选平台」，不是别的弹窗。
    app.assert_text(WIZARD, "选择要接入的平台")
    # 每一步都有自己的「稍后配置」，但**当前这屏只应看到一个出口**（其他的属于隐藏步骤）。
    visible_exits = app.page.evaluate(
        "(function(){return [].slice.call(document.querySelectorAll('#platform-setup-wizard .platform-setup-later'))"
        ".filter(function(b){return b.offsetParent!==null&&!b.hidden;}).length;})()"
    )
    assert visible_exits == 1, f"引导层当前屏应只有 1 个「稍后配置」出口，实际 {visible_exits} 个"

    app.click(WIZARD + " .platform-setup-later")
    app.wait_for(WIZARD, state="hidden", timeout=10)
    assert not _wizard_visible(app), "点了「稍后配置」但引导层还在"
    app.shot("wizard-dismissed")

    # 关掉之后主界面必须真的能用：切到设置再切回来。
    app.wait_for("#tab-tasks-button", state="visible", timeout=10)
    app.click("#tab-settings-button")
    app.wait_for("#tab-settings", state="visible", timeout=10)
    app.click("#tab-tasks-button")
    app.wait_for("#tab-tasks", state="visible", timeout=10)

    # 引导层关掉后不该再自己弹回来挡住页面。
    assert not _wizard_visible(app), "关掉后引导层又自己弹回来了"

    app.assert_no_console_errors()
    app.assert_no_failed_requests()
