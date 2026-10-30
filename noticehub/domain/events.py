"""把抽取好的事件并进摘要条目。不读时钟，不写库。"""

from __future__ import annotations

import datetime as dt
from typing import Any, Mapping, Sequence

from noticehub.domain.time import parse_iso


def merge_extracted_event(
    item: dict[str, Any],
    events: Sequence[Mapping[str, Any]],
    *,
    not_before: dt.datetime | None = None,
    confidence_rank: Mapping[str, int],
) -> str:
    """合并策略与原来的 DigestService._merge_extracted_event 相同。无事件返回空串。"""
    best_level = ""
    deadlines: list[dt.datetime] = []
    for event in events:
        confidence = str(event.get("confidence") or "low")
        if confidence_rank.get(confidence, 0) < confidence_rank["medium"]:
            continue
        if confidence_rank.get(confidence, 0) > confidence_rank.get(best_level, -1):
            best_level = confidence
        deadline = parse_iso(event.get("when_start"))
        if isinstance(deadline, dt.datetime):
            deadlines.append(deadline)
        what = str(event.get("what") or "").strip()
        if what and not str(item.get("summary") or "").strip():
            item["summary"] = what
        where = str(event.get("where") or "").strip()
        if where:
            if not str(item.get("condition") or "").strip():
                item["condition"] = f"地点：{where}"
            details = item.get("details")
            if not isinstance(details, list):
                details = []
                item["details"] = details
            entry = f"地点：{where}"
            if entry not in details:
                details.append(entry)
    if not best_level:
        return ""
    if deadlines and not isinstance(item.get("deadline_dt"), dt.datetime):
        usable = [value for value in deadlines if not_before is None or value >= not_before]
        item["deadline_dt"] = min(usable or deadlines)
        item["event_deadline"] = True
    return best_level


def event_level_blocks(event_level: str, item: Mapping[str, Any]) -> bool:
    """事件只到 medium、时间又是事件补出来的纯信息，不当待办。"""
    if event_level != "medium" or not item.get("event_deadline"):
        return False
    return not str(item.get("action") or "") and str(item.get("category") or "info") not in {"urgent", "action"}
