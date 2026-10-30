"""待办用例。库表读写留在传入的 Store，这里只做契约里的参数检查。"""

from __future__ import annotations

from typing import Any


def task_id_text(task_id: Any) -> int:
    """契约收字符串，Store 收整数。非法文本在这里拦住，避免 int() 变成 500。"""
    if (
        not isinstance(task_id, str)
        or not task_id.isascii()
        or not task_id.isdigit()
        or len(task_id) > 18
        or int(task_id) < 1
    ):
        raise ValueError("task_id 必须是正整数文本")
    return int(task_id)


def set_task_urgent(store: Any, task_id: str, urgent: bool | None) -> dict[str, Any]:
    """设置或清除紧急覆盖。urgent 为 None 表示清除，与「字段缺失」不是一回事。"""
    parsed = task_id_text(task_id)
    if urgent is not None and type(urgent) is not bool:
        raise ValueError("urgent 必须是 true、false 或 null")
    return store.set_task_urgent(parsed, urgent)


def set_task_pinned(store: Any, task_id: str, pinned: bool) -> dict[str, Any]:
    """置顶或取消置顶。已完成任务的拒绝文案留在 Store。"""
    parsed = task_id_text(task_id)
    if type(pinned) is not bool:
        raise ValueError("pinned 必须是 true 或 false")
    return store.set_task_pinned(parsed, pinned)
