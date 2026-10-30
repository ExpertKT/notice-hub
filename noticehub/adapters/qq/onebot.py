"""OneBot v11 群消息。只产出 MessageEvent，不写库。"""

from __future__ import annotations

import hashlib
from typing import Any, Callable

from noticehub.domain.message import MessageEvent
from noticehub.domain.time import iso

SEGMENT_LABEL = {
    "image": "[图片]",
    "face": "[表情]",
    "record": "[语音]",
    "video": "[视频]",
    "forward": "[合并转发]",
    "json": "[卡片消息]",
    "file": "[文件]",
    "mface": "[表情]",
    "dice": "[骰子]",
    "rps": "[猜拳]",
}


def flatten_message(message: Any) -> str:
    if isinstance(message, str):
        return message.strip()
    parts: list[str] = []
    for segment in message or []:
        if not isinstance(segment, dict):
            parts.append(str(segment))
            continue
        kind = str(segment.get("type") or "")
        data = segment.get("data") or {}
        if kind == "text":
            parts.append(str(data.get("text") or ""))
        elif kind == "at":
            name = data.get("name") or data.get("qq") or "某人"
            parts.append(f"@{name}")
        elif kind == "reply":
            continue
        elif kind == "file":
            name = str(data.get("file") or data.get("file_name") or "").strip()
            parts.append(f"[文件]{name}" if name else "[文件]")
        else:
            parts.append(SEGMENT_LABEL.get(kind, f"[{kind}]"))
    return "".join(parts).strip()


def parse_onebot_message(payload: Any, settings: Any, *, now: Callable[[], Any]) -> MessageEvent | None:
    """把 OneBot v11 群消息转成 MessageEvent。非群消息返回 None。"""
    if not isinstance(payload, dict):
        return None
    if str(payload.get("post_type") or "") != "message":
        return None
    if str(payload.get("message_type") or "") != "group":
        return None

    user_id = str(payload.get("user_id") or "")
    if user_id and user_id == str(payload.get("self_id") or ""):
        return None

    group_id = str(payload.get("group_id") or "")
    if not group_id:
        return None

    content = flatten_message(payload.get("message"))
    if not content:
        content = flatten_message(payload.get("raw_message"))
    if not content:
        return None

    sender = payload.get("sender") or {}
    sender_name = str(sender.get("card") or sender.get("nickname") or user_id or "群成员")
    message_id = str(payload.get("message_id") or "")
    if not message_id:
        digest = hashlib.sha1(f"{group_id}:{user_id}:{payload.get('time')}:{content}".encode("utf-8"))
        message_id = "hash-" + digest.hexdigest()[:16]

    return MessageEvent(
        msg_id=f"onebot:{group_id}:{message_id}",
        group_id=group_id,
        source="onebot",
        content=content,
        ts=iso(payload.get("time")) or iso(now()),
        received_at=iso(now()),
        event="GROUP_MESSAGE_CREATE",
        sender_id=user_id,
        sender_name=sender_name,
        group_name=settings.group_name(group_id),
    )


def onebot_record(event: MessageEvent) -> dict[str, Any]:
    """旧回调仍收这条记录。键与原来的 parse_onebot_event 相同。"""
    return {
        "msg_id": event.msg_id,
        "source": event.source,
        "event": event.event,
        "group_id": event.group_id,
        "group_name": event.group_name,
        "sender_id": event.sender_id,
        "sender_name": event.sender_name,
        "ts": event.ts,
        "received_at": event.received_at,
        "content": event.content,
    }


def qqbot_group_event(event_name: str, message: Any, *, now: Callable[[], Any]) -> MessageEvent:
    """官方机器人的群消息。私聊不是这条路径。"""
    author = getattr(message, "author", None)
    return MessageEvent(
        msg_id=str(getattr(message, "id", "") or ""),
        group_id=str(getattr(message, "group_openid", "") or ""),
        source="qqbot",
        content=str(getattr(message, "content", "") or ""),
        ts=iso(getattr(message, "timestamp", None)) or iso(now()),
        received_at=iso(now()),
        event=event_name,
        sender_id=str(getattr(author, "member_openid", "") or ""),
        sender_name=str(getattr(author, "username", "") or ""),
    )


def qqbot_group_record(event: MessageEvent) -> dict[str, Any]:
    """旧回调仍收这条记录。键与原来的 _record_group 相同。"""
    return {
        "msg_id": event.msg_id,
        "source": event.source,
        "event": event.event,
        "group_id": event.group_id,
        "sender_id": event.sender_id,
        "sender_name": event.sender_name,
        "ts": event.ts,
        "received_at": event.received_at,
        "content": event.content,
    }
