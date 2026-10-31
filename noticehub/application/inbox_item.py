"""把一条收件箱消息提升成待办。分类和落库仍由传入的 promote 完成。"""

from __future__ import annotations

from typing import Any, Callable

from noticehub.domain.time import parse_iso


def promote_inbox_item(
    msg_id: Any,
    title: Any = None,
    due: Any = None,
    *,
    promote: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """msg_id 必填且不超过 256。title 不超过 500。due 为 ISO 或空。"""
    text = str(msg_id or "").strip()
    if not text or len(text) > 256:
        raise ValueError("消息编号无效")
    if title is not None and (not isinstance(title, str) or len(title) > 500):
        raise ValueError("待办标题最多 500 个字符")
    if due is not None and due != "" and not isinstance(due, str):
        raise ValueError("截止时间格式无效")
    if due and parse_iso(due) is None:
        raise ValueError("截止时间格式无效")
    result = promote(text, title=title, due=due)
    return {
        "ok": bool(result.get("ok")),
        "task_id": result.get("task_id"),
        "error": str(result.get("error") or ""),
    }
