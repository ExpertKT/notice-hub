"""入站消息。适配器只负责填这个类型，不写库。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class MessageEvent:
    msg_id: str
    group_id: str = ""
    source: str = "qqbot"
    content: str = ""
    source_text: str = ""
    ts: Any = None
    received_at: Any = None
    event: str = ""
    sender_id: str = ""
    sender_name: str = ""
    group_name: str = ""
    at_me: bool = False

    @classmethod
    def from_mapping(cls, record: Mapping[str, Any]) -> "MessageEvent":
        return cls(
            msg_id=str(record.get("msg_id") or "").strip(),
            group_id=str(record.get("group_id") or ""),
            source=str(record.get("source") or "qqbot"),
            content=str(record.get("content") or ""),
            source_text=str(record.get("source_text") or ""),
            ts=record.get("ts"),
            received_at=record.get("received_at"),
            event=str(record.get("event") or ""),
            sender_id=str(record.get("sender_id") or ""),
            sender_name=str(record.get("sender_name") or ""),
            group_name=str(record.get("group_name") or ""),
            at_me=bool(record.get("at_me")),
        )
