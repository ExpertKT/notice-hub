"""冒烟：冷装 → 首页 → 版本徽标 → 设置页 → 无 console 错误 → 截图（desktop/mobile 各一份）。"""

from __future__ import annotations

from harness import scenario


@scenario("demo/smoke", viewports=("desktop", "mobile"), tags=("fakes:none", "smoke"))
def run(app) -> None:
    """新装实例能打开、能显示当前版本、设置页能开、页面没有 console 错误。"""
    app.goto("/")
    app.wait_for(".brand", timeout=10)
    app.assert_text("title", "群消息待办")
    app.shot("01-home")

    # 新用户第一次打开会先看到「选择要接入的平台」向导：profile 里没有 nh_setup_wizard_seen 时自动弹
    # （page.html:2868）。它是全屏遮罩，会盖住 tab 栏——按真实用户的做法点「稍后配置」再继续。
    if app.page.evaluate("String(!localStorage.getItem('nh_setup_wizard_seen'))") == "true":
        app.wait_for("#platform-setup-wizard", timeout=10)
        app.click("#platform-setup-step-1 .platform-setup-later")
        app.wait_for("#platform-setup-wizard", state="hidden", timeout=10)

    # 冷装默认 `QQ_DIGEST_UPDATE_CHANNEL=off`（不联网检查更新），这种安装的页头**没有**版本徽标
    # （webapp.py:2694 只在 beta/stable 通道渲染）。要验徽标，就先切成 beta 再重新加载。
    result = app.api_post("/api/settings", {"update_channel": "beta"})
    assert result.get("ok"), f"切换更新通道失败：{result}"

    # 版本号从接口拿，不写死（每发一版必红的坑）：/api/update 的 current.version 就是页头徽标用的值。
    update = app.api_get("/api/update") or {}
    version = str(((update.get("current") or {}).get("version")) or "").strip()
    assert version and version != "未知", f"/api/update 没给出当前版本：{update}"

    app.goto("/")
    if app.viewport == "mobile":
        # 窄屏下 `.brand-sub` 整体 display:none（page.html:322），徽标只在 DOM 里不可见。
        app.wait_for(".channel-badge", state="attached", timeout=10)
    else:
        app.wait_for(".channel-badge", timeout=10)
    app.assert_text(".channel-badge", version)
    app.shot("02-home-with-badge")

    app.click("#tab-settings-button")
    app.wait_for("#tab-settings", state="visible", timeout=10)
    app.assert_attr("#tab-settings-button", "aria-selected", "true")
    app.shot("03-settings")

    app.assert_no_console_errors()
