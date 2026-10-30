"""向每个已配置的推送通道发一条测试消息。"""

from __future__ import annotations

from typing import Any, Callable


def test_push_channels(settings: Any, *, build: Callable[[Any], list[Any]]) -> dict[str, Any]:
    try:
        pushers = build(settings)
    except Exception as error:  # noqa: BLE001 - 初始化失败要回到页面，不能把请求打断
        return {"ok": False, "error": f"推送通道初始化失败：{error}", "results": []}
    if not pushers:
        return {"ok": False, "error": "还没有配置任何推送通道", "results": []}
    results: list[dict[str, Any]] = []
    for pusher in pushers:
        try:
            pusher.send("QQ 群消息台 · 测试消息", "看到这条消息，说明提醒已经能送到你手机上了。")
        except Exception as error:  # noqa: BLE001
            results.append({
                "channel": str(getattr(pusher, "name", "通道")),
                "target": str(getattr(pusher, "target", "")),
                "ok": False,
                "error": str(error)[:200] or "发送失败",
            })
            continue
        results.append({
            "channel": str(getattr(pusher, "name", "通道")),
            "target": str(getattr(pusher, "target", "")),
            "ok": True,
            "error": "",
        })
    return {"ok": all(item["ok"] for item in results), "results": results}
