"""调度和历史回填。推送仍由调用方的 _publish 完成。"""

from __future__ import annotations

import datetime as dt
import hashlib
from typing import Any, Callable

from noticehub.domain.time import iso, parse_iso
from qq_live_digest.summarizer import (
    Digest,
    analyse_records,
    build_digest,
    classify_task_item,
    is_task_eligible,
    payload_to_item,
    urgent_analyses,
)

# 事件抽取置信度 → 待办置信度上限；与 LLM 分类置信度取较低者（low 不产生待办）。
EVENT_CONFIDENCE_CAP = {"low": 0.30, "medium": 0.55, "high": 0.80}
FORCE_FLUSH_MULTIPLIER = 6


def tick(host: Any, now: dt.datetime | None = None) -> list[Digest]:
    stamp = now if now is not None else host.now()
    host.last_tick_at = stamp
    host._maybe_catchup(stamp)
    host._maybe_deadline_reminders(stamp)
    host._maybe_weekly_review(stamp)
    host._maybe_candidate_reminders(stamp)
    produced: list[Digest] = []
    host._maybe_prune(stamp)
    host._maybe_snapshot(stamp)

    records = host.store.unprocessed_messages(limit=host.settings.max_batch)
    if not records:
        host._retry()
        return produced

    analyses = analyse_records(records)
    history = host.store.recent_item_texts(hours=host.settings.dedupe_hours)
    urgent = urgent_analyses(analyses, host.settings, now=stamp)
    if urgent:
        urgent_ids = {item["msg_id"] for item in urgent}
        subset = [record for record in records if record["msg_id"] in urgent_ids]
        digest = build_digest(host.settings, subset, kind="urgent", now=stamp, history=history)
        host._publish(digest, when=stamp)
        produced.append(digest)
        records = [record for record in records if record["msg_id"] not in urgent_ids]
        if not records:
            host._retry()
            return produced

    immediate_records = [
        record
        for record in records
        if host.settings.is_immediate_group(str(record.get("group_id") or ""))
    ]
    if immediate_records:
        allowed, reason = host._push_gate(stamp)
        if allowed:
            immediate_ids = {record["msg_id"] for record in immediate_records}
            digest = build_digest(
                host.settings, immediate_records, kind="window", now=stamp, history=history
            )
            host._publish(digest, when=stamp)
            produced.append(digest)
            records = [record for record in records if record["msg_id"] not in immediate_ids]
            if not records:
                host._retry()
                return produced
        else:
            host._hold_batch(stamp, len(immediate_records), reason)

    oldest = parse_iso(records[0].get("received_at")) or stamp
    elapsed = (stamp - oldest).total_seconds()
    window_seconds = host.settings.window_minutes * 60
    force = len(records) >= host.settings.max_batch or elapsed >= window_seconds * FORCE_FLUSH_MULTIPLIER
    if elapsed < window_seconds and not force:
        return produced

    allowed, reason = host._push_gate(stamp)
    if not allowed:
        host._hold_batch(stamp, len(records), reason)
        host._retry()
        return produced

    digest = build_digest(host.settings, records, kind="window", now=stamp, history=history)
    if digest.items:
        host._publish(digest, when=stamp)
    else:
        digest.kind = "silent"
        digest.id = host.store.insert_digest(
            kind="silent",
            window_start=iso(digest.window_start),
            window_end=iso(digest.window_end),
            body="",
            payload=[],
            message_count=digest.message_count,
            llm_used=False,
        )
        host.store.mark_processed(digest.message_ids, digest.id)
        host.logger.info("窗口内 %d 条消息无重点，静默归档（未推送）。", digest.message_count)
    produced.append(digest)
    host._retry()
    return produced


def sync_tasks(host: Any, digest: Digest) -> list[dict[str, Any]]:
    """把摘要里的行动项写入待办库；提醒类不重复建任务。"""
    if digest.kind in {"morning", "evening"}:
        return []
    changed: list[dict[str, Any]] = []
    for item in digest.items:
        # 先合并事件再判门槛：否则「没有 deadline 的纯信息」在 is_task_eligible 就被筛掉了。
        event_level = host._merge_extracted_event(item)
        if not is_task_eligible(item, host.settings):
            continue
        category = str(item.get("category") or "info")
        action = str(item.get("action") or "")
        deadline = item.get("deadline_dt")
        if category not in {"urgent", "action"} and not action and not isinstance(deadline, dt.datetime):
            continue  # 纯信息不进待办
        if host._event_level_blocks(event_level, item):
            continue  # 时间只是事件猜出来的 medium 纯信息，不当待办
        message = item["message"]
        summary = str(item.get("summary") or message.text[:60])
        if "链路自测" in summary or "链路自测" in message.text:
            continue  # 自测消息不进待办
        key = str(item.get("msg_id") or "")
        if not key:
            raw = f"{summary}|{deadline}|{message.text[:200]}"
            key = "auto:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
        groups = [str(name) for name in (item.get("duplicate_groups") or []) if str(name).strip()]
        if not groups and item.get("group_name"):
            groups = [str(item["group_name"])]
        classification = classify_task_item(item, host.settings)
        record = item.get("record") if isinstance(item.get("record"), dict) else {}
        record_source = str(record.get("source") or "")
        source = record_source if record_source in {"dingtalk", "chaoxing", "wechat"} else str(classification.get("source") or "qq_message")
        event = str(record.get("event") or "")
        if "file" in event:
            source = "file"
        elif "image" in event:
            source = "image"
        task_id = host.store.upsert_task(
            task_key=key,
            summary=summary,
            audience=str(item.get("audience") or ""),
            condition=str(item.get("condition") or ""),
            details=[str(value) for value in (item.get("details") or []) if str(value).strip()],
            action=action,
            category=category,
            importance=int(item.get("importance") or 3),
            deadline=iso(deadline) if isinstance(deadline, dt.datetime) else "",
            groups=groups,
            sender=str(message.sender or ""),
            evidence=str(item.get("evidence") or ""),
            digest_id=int(digest.id or 0),
            status=str(classification.get("status") or "open"),
            confidence=min(
                float(classification.get("confidence") or 0.0),
                EVENT_CONFIDENCE_CAP.get(event_level, 1.0),
            ),
            source=source,
            classification_reason=str(classification.get("reason") or ""),
        )
        if task_id:
            item["task_id"] = task_id
            item["task_url"] = host._task_url(task_id)
            changed.append({"id": task_id, **classification})
    if changed:
        candidates = sum(1 for item in changed if item.get("status") == "candidate")
        host.logger.info(
            "待办库更新：%d 条（候选 %d，摘要 #%s）。", len(changed), candidates, digest.id
        )
    return changed


def import_existing_tasks(host: Any, *, days: int = 7, now: Callable[[], dt.datetime]) -> int:
    """把历史摘要里的行动项回填到待办库，重复执行安全。"""
    cutoff = now() - dt.timedelta(days=max(1, int(days)))
    today_start = now().replace(hour=0, minute=0, second=0, microsecond=0)
    total = 0
    for row in host.store.recent_digests(limit=400):
        kind = str(row.get("kind") or "")
        if kind in {"morning", "evening", "silent"}:
            continue
        created = parse_iso(row.get("created_at"))
        if created and created < cutoff:
            continue
        items = [payload_to_item(entry) for entry in (row.get("items") or []) if isinstance(entry, dict)]
        filtered: list[dict[str, Any]] = []
        for item in items:
            # 与 sync_tasks 同一条规则：先合并事件再判门槛，历史回填也吃这份时间。
            # not_before 让「今天上午已过」的事件让位给后面还没过的那条。
            event_level = host._merge_extracted_event(item, not_before=today_start)
            if not is_task_eligible(item, host.settings):
                continue
            deadline = item.get("deadline_dt")
            if isinstance(deadline, dt.datetime) and deadline < today_start:
                continue  # 已经过期的历史事项不再回填
            summary = str(item.get("summary") or "")
            text = str(item["message"].text or "")
            if "链路自测" in text or "链路自测" in summary:
                continue
            if text.rstrip().endswith(("?", "？")) or summary.rstrip().endswith(("?", "？")):
                continue  # 疑问句不是待办
            if not isinstance(deadline, dt.datetime) and str(item.get("category")) not in {"urgent", "action"}:
                continue
            if host._event_level_blocks(event_level, item):
                continue  # 与 sync_tasks 同一条收紧规则
            filtered.append(item)
        if not filtered:
            continue
        digest = Digest(
            kind=kind,
            window_start=parse_iso(row.get("window_start")),
            window_end=parse_iso(row.get("window_end")),
            items=filtered,
            message_count=int(row.get("message_count") or len(items)),
            id=int(row.get("id") or 0),
        )
        total += len(host._sync_tasks(digest))
    return total
