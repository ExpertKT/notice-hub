"""10 微信目录不存在：保存必须 400，并把后端原文逐字显示出来。

为什么：用户填错目录时，页面只说「保存失败」等于没说。后端已经给出可照做的原文
（「微信数据目录不存在：<路径>，请填微信「设置 → 文件管理」里显示的存储位置目录」），
页面必须原样呈现，不能吞掉或改写。

复现：
    python tools/e2e/run.py --only 10_settings_wechat_dir_missing
"""

import json
import time
import urllib.error
import urllib.request

from harness import scenario

BAD_DIR = r"Z:\not-a-real-wechat-dir-e2e"


def _dismiss_wizard(app) -> None:
    if app.count("#platform-setup-wizard") and app.page.evaluate(
        "(function(){var w=document.getElementById('platform-setup-wizard');return !!(w&&!w.hidden);})()"
    ):
        app.click("#platform-setup-wizard .platform-setup-later")
        app.wait_for("#platform-setup-wizard", state="hidden", timeout=10)


def _wait_js(app, expression: str, desc: str, timeout: float = 20) -> None:
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


@scenario("10_settings_wechat_dir_missing", viewports=("desktop",), tags=("fakes:wechat",))
def run(app):
    expected = f"微信数据目录不存在：{BAD_DIR}，请填微信「设置 → 文件管理」里显示的存储位置目录"

    # 先验接口：必须 400、错误体逐字等于后端原文（不是「失败」两个字）。
    payload = json.dumps({"wechat_dir": BAD_DIR}).encode("utf-8")
    request = urllib.request.Request(
        app.base_url + "/api/settings",
        data=payload,
        headers={"X-Token": app.install.token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raise AssertionError(f"保存不存在的目录居然返回 {response.status} 成功：{response.read()[:200]!r}")
    except urllib.error.HTTPError as error:
        assert error.code == 400, f"保存不存在的目录应返回 400，实际 {error.code}"
        body = json.loads(error.read().decode("utf-8") or "{}")
        assert body.get("ok") is False, f"400 响应体 ok 不是 false：{body}"
        assert body.get("error") == expected, "后端原文和约定不一致：%r" % (body.get("error"),)

    # 再验界面：走真实输入框 + 保存按钮，状态行必须原样显示那句话。
    _open_wechat(app)
    # 设置卡会在后台刷新并回填 wechat_dir（loadSettings 会把输入框改回已保存的值）。
    # 先填再点会在两步之间被打断——实测点下去时输入框已经被刷回旧目录，保存的是旧目录，
    # 于是「保存成功」也过、脚本假绿。这里把「写值 + 触发 input/change + 点保存」放进同一次
    # evaluate，中间没有可被打断的窗口。
    written = app.page.evaluate(
        "(function(){var input=document.getElementById('wx-dir-input');"
        "var button=document.getElementById('wx-dir-save');"
        "if(!input||!button)return null;"
        "input.value=%s;"
        "input.dispatchEvent(new Event('input',{bubbles:true}));"
        "input.dispatchEvent(new Event('change',{bubbles:true}));"
        "var before=input.value;"
        "button.click();"
        "return before;})()" % json.dumps(BAD_DIR)
    )
    assert written == BAD_DIR, f"输入框里没留住坏目录，实际 {written!r}"
    _wait_js(
        app,
        "String((document.getElementById('wx-dir-status')||{}).textContent||'').indexOf(%s)>=0" % json.dumps(expected),
        "#wx-dir-status 显示后端原文",
        timeout=20,
    )
    shown = app.text("#wx-dir-status")
    # 页面允许在前面加自己的前缀（现在的实现是「保存失败：」），但后端原文必须逐字出现在
    # 末尾：被截断、被改写成「保存失败」、或把路径吞掉都算失败。
    assert shown.strip().endswith(expected), f"#wx-dir-status 没原样保留后端原文：{shown!r}"
    assert "已保存" not in shown, f"不存在的目录却显示保存成功：{shown!r}"
    dir_state = app.page.evaluate(
        "(function(){var n=document.getElementById('wx-dir-status');return n?String(n.dataset.state||''):'';})()"
    )
    assert dir_state == "error", f"保存失败的目录状态行不是 error 态（data-state={dir_state!r}）"
    app.shot("dir-missing")

    # 只读用户看得见的文字（body.innerText）：textContent 会把内联 <script> 源码也算进来，
    # 里面有 'undefined'/'NetworkError' 这类词，用它断言等于拿源码当界面。
    english = app.page.evaluate(
        "(function(){var t=document.body.innerText||'';"
        "return ['Failed to fetch','Traceback','NetworkError','undefined']"
        ".filter(function(word){return t.indexOf(word)>=0;}).join(',');})()"
    )
    assert not english, f"页面上出现了英文/内部报错：{english}"
    # console：这条剧本故意 POST 一个不存在的目录，浏览器必然记一条
    # 「Failed to load resource: … 400 (Bad Request)」——那是预期内的。
    # 但除此之外不许有任何 JS 异常/其它 console 错误（不能为了过而整块不看）。
    app.settle(0.3)
    console = list(dict.fromkeys(list(app.page.page_errors) + list(app.page.console_errors)))
    expected_400 = [item for item in console if "400 (Bad Request)" in item]
    unexpected = [item for item in console if "400 (Bad Request)" not in item]
    assert not unexpected, "页面有预期之外的 console 错误：" + " | ".join(unexpected)
    assert expected_400, "预期的那次 400（保存不存在的目录）在 console 里没出现，说明请求可能根本没发出去"

    # run.py 收尾会统一跑 assert_no_failed_requests()：这条剧本眼里的「失败请求」正是
    # 它要验的那一次 400，所以把它从记录里摘掉（上面已经正向断言它确实发生过），
    # 否则「验证 400」这件事本身永远过不了。
    page = app.page
    page.failed_requests[:] = [
        item for item in page.failed_requests
        if not (item.startswith("POST 400") and "/api/settings" in item)
    ]
    page.console_errors[:] = [item for item in page.console_errors if "400 (Bad Request)" not in item]
