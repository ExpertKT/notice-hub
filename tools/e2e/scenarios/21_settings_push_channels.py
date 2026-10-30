"""21 推送通道：设置页「测试」必须把消息真的发到本地收包端，失败要透出后端原文。

覆盖三条：
1. 填 webhook 地址 → 保存 → 状态行「已保存」，`/api/meta` 里 webhook 变已配置；
2. 点「测试」→ 本地收包端真的收到一次 POST，body 里有标题/内容；
3. 收包端故意回 500 → 状态行必须显示失败与原因，不许假装成功。

复现：
    python tools/e2e/run.py --only 21_settings_push_channels
"""

from __future__ import annotations

import json
import time

from harness import scenario


def _dismiss_wizard(app) -> None:
    """首访向导会盖住设置页；关不掉就让后面的断言自己失败。"""
    visible = app.page.evaluate(
        "(function(){var w=document.getElementById('platform-setup-wizard');"
        "return !!(w&&!w.hidden);})()"
    )
    if visible:
        app.click("#platform-setup-wizard .platform-setup-later")
        app.wait_for("#platform-setup-wizard", state="hidden", timeout=10)


def _open_settings(app) -> None:
    app.click("#tab-settings-button")
    app.wait_for("#tab-settings", state="visible", timeout=10)
    app.wait_for("#push-test", timeout=20)


def _wait_text(app, selector: str, expect: str, timeout: float = 20.0) -> str:
    """等某元素文案出现关键字；等不到把最后一次文案原样带出来。"""
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            last = app.text(selector)
        except AssertionError:
            last = ""
        if expect in last:
            return last
        app.settle(0.1)
    raise AssertionError(f"{selector} 等不到「{expect}」（{timeout}s）；最后一次：{last!r}")


@scenario("21_settings_push_channels", viewports=("desktop",), tags=("fakes:push", "settings"))
def run(app) -> None:
    """webhook 通道：真发、真收、失败真报。"""
    sink = app.fakes["push"]
    sink_url = sink.base_url.rstrip("/") + "/hook"

    app.goto("/")
    _dismiss_wizard(app)
    _open_settings(app)
    app.shot("01-push-card")

    # ① 保存：地址写进配置，回读确认服务端真的存了
    app.fill("#push-webhook_urls", sink_url)
    app.click("#push-save")
    saved = _wait_text(app, "#push-result", "已保存")
    meta = app.api_get("/api/meta")
    webhook_state = (meta.get("push") or {}).get("webhook_urls") or {}
    assert webhook_state.get("set") is True, f"保存后 /api/meta 仍显示 webhook 未配置：{webhook_state!r}"
    assert webhook_state.get("count") == 1, f"webhook 条数不对：{webhook_state!r}"
    app.shot("02-push-saved")

    # ② 测试：本地收包端必须真的收到一次 POST
    before = sink.impl.count()
    app.click("#push-test")
    ok_text = _wait_text(app, "#push-result", "测试消息已发出")
    records = sink.impl.requests()
    assert len(records) == before + 1, f"收包端收到 {len(records) - before} 次请求（应为 1）：{saved!r} / {ok_text!r}"
    record = records[-1]
    assert record.get("method") == "POST", f"收包方法不对：{record.get('method')!r}"
    body = record.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            pass
    text = json.dumps(body, ensure_ascii=False)
    assert isinstance(body, dict), f"测试消息 body 不是 JSON 对象：{text[:300]}"
    title = str(body.get("title") or "")
    content = str(body.get("content") or "")
    assert title.strip(), f"测试消息 body 里 title 为空：{text[:300]}"
    assert content.strip(), f"测试消息 body 里 content 为空：{text[:300]}"
    # 哨兵：产品测试消息的固定正文，只有真的把测试文案发出去才会出现。
    sentinel = "看到这条消息，说明提醒已经能送到你手机上了。"
    assert sentinel in content, f"content 里没有测试消息的固定正文（哨兵没到）：{content[:200]!r}"
    assert "测试消息" in title, f"标题不是测试消息：{title!r}"
    app.shot("03-push-tested")

    # ③ 收包端回 500：必须承认失败并带出原因，不许装成功
    failing = app.fake("push", mode="fail")
    fail_url = failing.base_url.rstrip("/") + "/hook"
    app.fill("#push-webhook_urls", fail_url)
    app.click("#push-save")
    _wait_text(app, "#push-result", "已保存")
    app.click("#push-test")
    fail_text = _wait_text(app, "#push-result", "没发成功")
    assert "失败" in fail_text, f"失败态没说明哪个通道失败：{fail_text!r}"
    assert "Webhook" in fail_text or "webhook" in fail_text.lower(), f"失败态没点名通道：{fail_text!r}"
    assert "测试消息已发出" not in fail_text, f"失败态却报成成功：{fail_text!r}"
    assert failing.impl.count() >= 1, "失败态的收包端一次请求都没收到"
    app.shot("04-push-failed")

    app.assert_no_console_errors()
