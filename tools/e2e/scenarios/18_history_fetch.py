"""18 历史回溯：把 NapCat 缓存里的历史消息真拉进库，页面报「扫描 N 条，新增 N 条」。

替身：tags=("fakes:onebot",)。run.py 在冷装前起假 NapCat，并把
`QQ_DIGEST_NAPCAT_API_URL` / OneBot 接收器写进 .env；剧本往替身的历史缓存里塞 3 条
（10001 两条、10002 一条），再从页面走「保存订阅 → 回溯聊天记录」。

为什么不用注入事件：注入走的是实时接收器，验不出「从 NapCat 历史接口往回捞」这条路径。

复现：
    python tools/e2e/run.py --only 18_history_fetch
"""

from __future__ import annotations

import time

from fakes.onebot import build_event
from harness import scenario

GROUP_A = "10001"
GROUP_B = "10002"
# (message_id, group_id, user_id, sender, text, 距今秒数)
MESSAGES = [
    ("810001", GROUP_A, 2001, "辅导员", "【通知】周三下午三点在 302 开班会。", 300),
    ("810002", GROUP_A, 2002, "学委", "【通知】周五前交实验报告。", 200),
    ("810003", GROUP_B, 2002, "学委", "【通知】下周一升旗仪式 7:30 集合。", 100),
]


def _dismiss_wizard(app) -> None:
    if app.count("#platform-setup-wizard") and app.page.evaluate(
        "(function(){var w=document.getElementById('platform-setup-wizard');return !!(w&&!w.hidden);})()"
    ):
        app.click("#platform-setup-wizard .platform-setup-later")
        app.wait_for("#platform-setup-wizard", state="hidden", timeout=10)


def _seed_history(app) -> None:
    """把 3 条历史消息塞进假 NapCat 的缓存（不经过实时接收器）。"""
    fake = app.fakes["onebot"].impl
    now = int(time.time())
    for message_id, group_id, user_id, sender, text, age in MESSAGES:
        fake.state.add_history(
            build_event(
                text,
                group_id=group_id,
                user_id=user_id,
                sender=sender,
                message_id=int(message_id),
                timestamp=now - age,
            )
        )


def _wait_history_done(app, *, timeout: float = 60.0) -> dict:
    """等回溯任务真正结束（标题从「正在…」变成「回溯任务状态」）。"""
    deadline = time.monotonic() + timeout
    snapshot: dict = {}
    while time.monotonic() < deadline:
        snapshot = app.page.evaluate(
            "(function(){var box=document.getElementById('history-job');"
            "var title=document.getElementById('history-job-title');"
            "var note=document.getElementById('history-job-note');"
            "return {hidden:!!(box&&box.hidden),title:title?title.textContent.trim():'',"
            "note:note?note.textContent.trim():''};})()"
        )
        if not snapshot["hidden"] and snapshot["title"] == "回溯任务状态":
            return snapshot
        app.settle(0.5)
    raise AssertionError(f"回溯任务 {timeout}s 内没有结束：{snapshot}")


@scenario("18_history_fetch", viewports=("desktop",), tags=("fakes:onebot",))
def run(app) -> None:
    _seed_history(app)
    app.goto("/")
    _dismiss_wizard(app)

    # 假 NapCat 已登录 → 群列表出现（两个群都来自替身的 fixture）。
    app.wait_for("#groupbox", state="visible", timeout=30)
    app.wait_for("#groups .group-row", state="visible", timeout=30)
    app.assert_count("#groups .group-row", 2)

    # 两个群都订阅；其它群在折叠区里，先展开再点，走真实鼠标。
    app.page.evaluate(
        "(function(){var d=document.querySelector('#groups details.other-groups');if(d)d.open=true;})()"
    )
    app.click("#groups input[value='10001']")
    app.click("#groups input[value='10002']")
    app.click("#save")
    app.wait_for("#result", state="visible", timeout=10)
    app.assert_text("#result", "订阅已保存并生效")
    app.shot("01-subscribed")

    # 回溯页：群列表默认勾选当前订阅，全选后开跑。
    app.click("#tab-inbox-button")
    app.wait_for("#tab-inbox", state="visible", timeout=10)
    app.wait_for("#history-groups input[type=checkbox]", state="attached", timeout=30)
    app.assert_count("#history-groups input[type=checkbox]", 2)
    app.click("#history-all-groups")
    app.click("#history-fetch")

    job = _wait_history_done(app)
    app.shot("02-history-done")
    assert job["note"] == "扫描 3 条，新增 3 条", f"回溯结果不对：{job['note']!r}"

    rows = app.db("SELECT COUNT(*) AS n FROM messages")
    assert rows and rows[0]["n"] == 3, f"库里 messages 应为 3 条，实际 {rows}"

    # 替身确实被调过 get_group_msg_history，不是页面自己编的数。
    actions = [call["action"] for call in app.fakes["onebot"].impl.state.calls]
    assert actions.count("get_group_msg_history") >= 2, f"替身没收到按群回溯请求：{actions}"
