"""待办资格。不写库。安静群判断走调用方传入的 settings 方法。"""

from __future__ import annotations

from typing import Any


def _qq_digest():
    import qq_digest

    return qq_digest


def _item_group_id(item: dict[str, Any]) -> str:
    record = item.get("record")
    if not isinstance(record, dict):
        record = {}
    return str(item.get("group_id") or record.get("group_id") or "")


def _item_group_name(item: dict[str, Any]) -> str:
    message = item.get("message")
    return str(item.get("group_name") or getattr(message, "source", "") or "")


def is_quiet_group_item(item: dict[str, Any], settings: Any) -> bool:
    group_id = _item_group_id(item)
    if group_id and settings.is_quiet_group(group_id):
        return True
    return settings.is_quiet_group_name(_item_group_name(item))


def is_notice_item(item: dict[str, Any], settings: Any) -> bool:
    """低优先级群里只保留像通知的内容，普通讨论不进入摘要。"""
    message = item.get("message")
    text = str(getattr(message, "text", "") or "")
    if bool(item.get("colloquial_question")):
        return False
    qq_digest = _qq_digest()
    if qq_digest.is_colloquial_question(text) and not qq_digest.has_directive_notice(text):
        return False
    tags = set(item.get("tags") or ())
    if tags & {"authority", "broadcast"}:
        return True
    if qq_digest.has_directive_notice(text):
        return True
    return bool(tags & {"action", "academic", "notice"})


def is_task_eligible(item: dict[str, Any], settings: Any) -> bool:
    """历史回填、待办同步和截止提醒共用的防误报门槛。"""
    quiet = is_quiet_group_item(item, settings)
    if quiet and not is_notice_item(item, settings):
        return False
    message = item.get("message")
    text = str(getattr(message, "text", "") or "")
    qq_digest = _qq_digest()
    if qq_digest.is_colloquial_question(text) and not qq_digest.has_directive_notice(text):
        return False
    category = str(item.get("category") or "info")
    action = str(item.get("action") or "")
    deadline = item.get("deadline_dt") or item.get("deadline")
    if is_quiet_group_item(item, settings):
        return category in {"urgent", "action"} or bool(
            action and qq_digest.has_directive_notice(text)
        )
    return category in {"urgent", "action"} or bool(action) or bool(deadline)
