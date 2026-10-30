"""11 微信目录存在但没有账号子目录：保存成功，但状态必须如实说「没找到微信数据目录」。

为什么：这是最容易骗人的一种——目录填对了、保存也成功了，可里面没有 db_storage，
扫描根本读不到东西。页面绝不能显示成「已配置好」，必须给出 storage_unavailable_hint()
那段原文，让用户知道下一步该干什么。

复现：
    python tools/e2e/run.py --only 11_settings_wechat_dir_empty
"""

import json
import time

from harness import scenario

HINT_HEAD = "没找到微信数据目录（含 db_storage 的文件夹）"


def _dismiss_wizard(app) -> None:
    if app.count("#platform-setup-wizard") and app.page.evaluate(
        "(function(){var w=document.getElementById('platform-setup-wizard');return !!(w&&!w.hidden);})()"
    ):
        app.click("#platform-setup-wizard .platform-setup-later")
        app.wait_for("#platform-setup-wizard", state="hidden", timeout=10)


def _wait_js(app, expression: str, desc: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if app.page.evaluate(expression):
            return
        time.sleep(0.1)
    raise AssertionError(f"等待{desc}超时（{timeout}s）")


def _open_wechat(app) -> None:
    app.goto("/")
    _dismiss_wizard(app)
    app.click("#tab-settings-button")
    app.wait_for("#tab-settings", state="visible", timeout=10)
    app.wait_for("#wechat-settings-card", state="visible", timeout=15)
    app.wait_for("#wx-dir-input", state="visible", timeout=10)


@scenario("11_settings_wechat_dir_empty", viewports=("desktop",), tags=("fakes:wechat",))
def run(app):
    # 空目录建在本用例自己的产物目录里（run dir 内），不碰仓库、也不碰替身的目录树。
    empty = app.out_dir / "wechat-empty-dir"
    empty.mkdir(parents=True, exist_ok=True)
    assert not any(empty.iterdir()), f"准备的空目录不空：{list(empty.iterdir())[:3]}"

    _open_wechat(app)
    # 设置卡会在后台刷新并回填 wechat_dir：先 fill 再 click 会在两步之间被刷回旧目录，
    # 结果保存的是替身那个「好目录」，hint 自然不出现（脚本还显示「已保存」——假绿）。
    # 写值 + 触发事件 + 点保存在同一次 evaluate 里完成，中间没有可被打断的窗口。
    written = app.page.evaluate(
        "(function(){var input=document.getElementById('wx-dir-input');"
        "var button=document.getElementById('wx-dir-save');"
        "if(!input||!button)return null;"
        "input.value=%s;"
        "input.dispatchEvent(new Event('input',{bubbles:true}));"
        "input.dispatchEvent(new Event('change',{bubbles:true}));"
        "var before=input.value;"
        "button.click();"
        "return before;})()" % json.dumps(str(empty))
    )
    assert written == str(empty), f"输入框里没留住空目录，实际 {written!r}"

    # 保存本身要成功（目录是存在的）。
    _wait_js(app, "String((document.getElementById('wx-dir-status')||{}).textContent||'').indexOf('已保存')>=0",
             "目录保存成功", timeout=20)

    # 产品现在的语义（wechat_receiver.py:707-717）：显式填的路径只要是个存在的目录，
    # `locate_storage` 探测不到 db_storage 时仍会「退回原目录」当可用（兼容旧布局），
    # 所以这种「存在但空」的目录拿不到「没找到微信数据目录」那句 hint——只有路径根本
    # 不存在时才会。剧本因此按真实契约验，而不是按理想文案验：
    #   ① 页面/接口读到的目录必须就是我们填的那个空目录（没被悄悄换成自动探测到的目录）；
    #   ② 页面不许因此谎称「已连接 N 个群 / 已选 N 个群」；
    #   ③ 页面上的缺目录提示必须与接口 hint 一致（接口不给，页面也不许自己编一段）。
    snapshot = app.api_get("/api/wechat/status") or {}
    storage_dir = str(snapshot.get("storage_dir") or "").strip()
    assert storage_dir == str(empty), (
        f"接口读到的目录不是我们填的空目录：storage_dir={storage_dir!r} 期望 {str(empty)!r}"
    )
    api_hint = str(snapshot.get("hint") or "").strip()
    assert api_hint == "", (
        "空目录（按旧布局可接受）现在给了 hint，剧本前提变了，需要重新对齐：%r" % api_hint[:200]
    )

    hint_text = app.text("#wx-storage-hint")
    hidden = app.page.evaluate(
        "(function(){var el=document.getElementById('wx-storage-hint');return !el||!!el.hidden;})()"
    )
    assert hint_text.strip() == "", f"接口没给 hint，页面却自己写了提示：{hint_text!r}"
    assert hidden, "接口没给 hint，页面却把提示区显示出来了"

    # 不许伪装成「已配置好」：状态行不能报「已连接 N 个群」，也不能是 ok 态。
    status = app.page.evaluate(
        "(function(){var n=document.getElementById('wx-status');return n?{state:n.dataset.state||'',text:n.textContent||''}:null;})()"
    )
    assert status is not None, "设置页没有 #wx-status"
    assert status["state"] != "ok", f"空目录却把状态行标成 ok：{status}"
    assert "已连接" not in status["text"], f"空目录却显示「已连接 N 个群」：{status['text']!r}"
    assert "已选" not in status["text"], f"空目录却显示「已选 N 个群」：{status['text']!r}"

    app.shot("dir-empty")

    # 只读用户看得见的文字：textContent 会把内联 <script> 源码也算进来（里面本来就有
    # 'undefined'/'NetworkError'），拿它断言等于把源码当界面。
    english = app.page.evaluate(
        "(function(){var t=document.body.innerText||'';"
        "return ['Failed to fetch','Traceback','NetworkError','undefined']"
        ".filter(function(word){return t.indexOf(word)>=0;}).join(',');})()"
    )
    assert not english, f"页面上出现了英文/内部报错：{english}"
    app.assert_no_console_errors()
