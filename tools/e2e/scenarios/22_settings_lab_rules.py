"""22 实验室规则：新增一条 → 列表出现 → 样例试跑命中 → 删除后消失。

规则是「实验室」功能，默认关闭，所以先用接口把 `lab_rules` 打开再刷新页面，
避免依赖复选框的位置与文案。全程走真实接口（POST/GET /api/lab/rules），不打桩。

复现：
    python tools/e2e/run.py --only 22_settings_lab_rules
"""

from __future__ import annotations

import json
import time

from harness import scenario

RULE_ID = "e2e-rule-22"
KEYWORDS = "报名, 截止"
SAMPLE = "请各班班长今晚前提交报名表，截止到 18:00"


def _dismiss_wizard(app) -> None:
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
    app.wait_for("#tab-settings #lab-rules-add", timeout=20)
    # 实验室整块在 <details> 里：折叠时子元素有 rect 却不可点（elementFromPoint 返回 null），
    # 必须先展开，否则 click() 会报「元素被遮挡（最上层是无元素）」。
    opened = app.page.evaluate(
        "(function(){var d=document.querySelector('#tab-settings #lab-details');return !!(d&&d.open);})()"
    )
    if not opened:
        _click_scrolled(app, "#tab-settings #lab-details > summary")
        app.wait_for("#tab-settings #lab-rules-add", timeout=10)


def _wait_text(app, selector: str, expect: str, timeout: float = 20.0) -> str:
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


def _wait_stable_rules(app, timeout: float = 15.0) -> None:
    """等页面首次拉 /api/lab/rules 的那次重渲染落地（状态行先出现再回到空）。"""
    deadline = time.monotonic() + timeout
    empty_streak = 0
    while time.monotonic() < deadline:
        try:
            text = app.text("#tab-settings #lab-rules-status").strip()
        except AssertionError:
            text = ""
        if text == "":
            empty_streak += 1
            if empty_streak >= 3:
                return
        else:
            empty_streak = 0
        app.settle(0.1)
    raise AssertionError("实验室规则状态行一直不稳定（等首次加载落地超时）")


def _click_scrolled(app, selector: str, settle: float = 0.35) -> None:
    """先滚到位再点：页面会异步重渲染，边滚边点会打到旧坐标上的别的元素。"""
    app.page.evaluate(
        "(function(){var el=document.querySelector(%s);if(!el)return false;"
        "el.scrollIntoView({block:'center',inline:'center'});return true;})()" % json.dumps(selector)
    )
    app.settle(settle)
    app.click(selector)


def _rules(app) -> list[dict]:
    payload = app.api_get("/api/lab/rules")
    rules = payload.get("rules")
    if rules is None:
        raise AssertionError(f"/api/lab/rules 没有 rules 字段：{payload!r}")
    return list(rules)


@scenario("22_settings_lab_rules", viewports=("desktop",), tags=("fakes:none", "settings", "lab"))
def run(app) -> None:
    """一条规则的完整生命周期：新增、保存、试跑命中、删除。"""
    app.api_post("/api/settings", {"lab_rules": True})
    app.goto("/")
    _dismiss_wizard(app)
    _open_settings(app)
    app.shot("01-lab-rules-editor")

    assert _rules(app) == [], f"开始前应没有规则，实际：{_rules(app)!r}"

    # ① 新增一行
    # 注意：页面打开时会异步拉一次 /api/lab/rules 并重渲染列表（会把刚加的空卡片清掉），
    # 所以先等那次加载落地（状态行稳定为空），再点「新增」，并允许点一次没成时重试。
    _wait_stable_rules(app)
    for _ in range(8):
        _click_scrolled(app, "#tab-settings #lab-rules-add")
        app.settle(0.2)
        if app.count("#tab-settings #lab-rules-list .lab-rule-card") >= 1:
            break
    app.assert_count("#tab-settings #lab-rules-list .lab-rule-card", 1)
    app.fill("#tab-settings #lab-rules-list .lab-rule-card .lab-rule-name", RULE_ID)
    app.fill("#tab-settings #lab-rules-list .lab-rule-card .lab-rule-keywords", KEYWORDS)
    app.shot("02-lab-rule-typed")

    # ② 保存后接口里真的有这条
    _click_scrolled(app, "#tab-settings #lab-rules-save")
    _wait_text(app, "#tab-settings #lab-rules-status", "已保存")
    saved = _rules(app)
    assert len(saved) == 1, f"保存后接口里有 {len(saved)} 条规则：{saved!r}"
    raw = saved[0].get("match") or {}
    keywords = raw.get("keyword") or saved[0].get("keywords") or saved[0].get("keywordText") or []
    flat = " ".join(keywords) if isinstance(keywords, list) else str(keywords)
    assert "报名" in flat and "截止" in flat, f"规则关键词没存对：{saved[0]!r}"
    app.shot("03-lab-rule-saved")

    # ③ 样例试跑必须命中这条规则
    # 试跑区也在一个 <details> 里（#lab-rules-testbox），折叠时子元素不可点，先展开。
    opened_testbox = app.page.evaluate(
        "(function(){var d=document.querySelector('#tab-settings #lab-rules-testbox');return !!(d&&d.open);})()"
    )
    if not opened_testbox:
        _click_scrolled(app, "#tab-settings #lab-rules-testbox > summary")
    app.fill("#tab-settings #lab-rules-sample", SAMPLE)
    _click_scrolled(app, "#tab-settings #lab-rules-test")
    hit_text = _wait_text(app, "#tab-settings #lab-rules-status", "命中")
    assert "0 条" not in hit_text, f"试跑说命中 0 条：{hit_text!r}"
    app.shot("04-lab-rule-tested")

    # ④ 删除后接口与页面都必须没有
    _click_scrolled(app, "#tab-settings #lab-rules-list .lab-rule-card .lab-rule-remove")
    app.assert_count("#tab-settings #lab-rules-list .lab-rule-card", 0)
    _click_scrolled(app, "#tab-settings #lab-rules-save")
    _wait_text(app, "#tab-settings #lab-rules-status", "已保存")
    assert _rules(app) == [], f"删除保存后接口里还有规则：{_rules(app)!r}"
    app.shot("05-lab-rule-deleted")

    app.assert_no_console_errors()
