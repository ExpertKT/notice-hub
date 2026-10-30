"""收消息和规则接线。不启动推送。"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from noticehub.domain.message import MessageEvent


def accept_message(
    event: MessageEvent,
    record: Mapping[str, Any],
    *,
    store: Any,
    settings: Any,
    logger: Any,
    now: Callable[[], Any],
) -> bool:
    """是否新插入。重复消息返回 False，并且不跑规则。"""
    msg_id = event.msg_id
    if not msg_id:
        logger.warning("忽略没有 msg_id 的消息：%s", str(record)[:200])
        return False
    group_id = event.group_id
    source = event.source
    if source == "dingtalk":
        accepted = bool(settings.dingtalk_group_whitelist) and group_id in settings.dingtalk_group_whitelist
    elif source == "chaoxing":
        accepted = bool(settings.chaoxing_enabled and settings.chaoxing_group_whitelist) and group_id in settings.chaoxing_group_whitelist
    elif source == "wechat":
        accepted = bool(settings.wechat_enabled and settings.wechat_group_whitelist) and group_id in settings.wechat_group_whitelist
    else:
        accepted = not group_id or settings.accepts_group(group_id)
    if not accepted:
        logger.debug("忽略白名单外的群：%s", group_id)
        return False
    group_name = event.group_name or settings.group_name(group_id)
    try:
        inserted = store.insert_message(
            msg_id=msg_id,
            group_id=group_id,
            content=event.content,
            source_text=event.source_text,
            ts=event.ts,
            received_at=event.received_at or now(),
            source=source,
            event=event.event,
            sender_id=event.sender_id,
            sender_name=event.sender_name,
            group_name=group_name,
        )
    except Exception:  # noqa: BLE001 - 入库失败不能让机器人线程崩掉
        logger.exception("消息入库失败：%s", msg_id)
        return False
    if inserted:
        logger.info(
            "收到消息 group=%s sender=%s id=%s text=%s",
            group_name,
            record.get("sender_name") or record.get("sender_id") or "?",
            msg_id,
            str(record.get("content") or "")[:60].replace("\n", " "),
        )
        apply_rules(
            record,
            msg_id,
            group_id,
            group_name,
            store=store,
            settings=settings,
            logger=logger,
        )
    return inserted


def apply_rules(
    record: Mapping[str, Any],
    msg_id: str,
    group_id: str,
    group_name: str,
    *,
    store: Any,
    settings: Any,
    logger: Any,
) -> None:
    """命中就进待办。规则读不出来、匹配或建待办失败，都不影响收消息。"""
    if not settings.lab_enabled("rules"):
        return
    try:
        from qq_live_digest import rules as rules_module

        loaded = rules_module.load_rules(settings)
        parsed = rules_module.parse_rules(loaded["rules"])
        if not parsed:
            return
        hits = rules_module.match(
            {
                "content": str(record.get("content") or ""),
                "group_id": group_id,
                "sender": str(record.get("sender_id") or record.get("sender_name") or ""),
                "at_me": bool(record.get("at_me")),
            },
            parsed,
        )
    except Exception:  # noqa: BLE001 - 规则故障不能影响收消息
        logger.exception("规则匹配失败，本条消息按未命中处理：%s", msg_id)
        return
    if not hits:
        return
    text = str(record.get("content") or "").strip()
    for hit in hits:
        payload = (hit.get("action") or {}).get("payload") or {}
        summary = str(payload.get("summary") or "").strip() or text.splitlines()[0][:60] or "(规则命中)"
        category = str(payload.get("category") or "action")
        try:
            importance = int(payload.get("importance") or 3)
        except (TypeError, ValueError):
            importance = 3
        try:
            store.upsert_task(
                task_key=f"rule:{hit.get('rule_id')}:{msg_id}",
                summary=summary[:200],
                category=category,
                importance=importance,
                groups=[group_name] if group_name else [],
                sender=str(record.get("sender_name") or record.get("sender_id") or ""),
                source="rule",
                status="candidate",
                classification_reason=f"规则 {hit.get('rule_id')} 命中",
            )
        except Exception:  # noqa: BLE001 - 建待办失败不能影响收消息
            logger.exception("规则命中的消息建待办失败：%s", msg_id)
