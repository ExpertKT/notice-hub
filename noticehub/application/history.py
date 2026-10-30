"""历史补采编排。NapCat HTTP 仍由调用方传入的客户端完成。"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable

from noticehub.adapters.qq.onebot import flatten_message, onebot_record
from noticehub.domain.message import MessageEvent
from noticehub.domain.time import iso, parse_iso
from noticehub.infrastructure.clock import SystemClock

LOGGER = logging.getLogger("qq_live_digest.catchup")


def _now() -> dt.datetime:
    return SystemClock().now()


def _napcat(settings: Any, client: Any) -> Any:
    if client is not None:
        return client
    from qq_live_digest.catchup import NapCatClient

    return NapCatClient(settings.napcat_api_url, settings.napcat_api_token, settings.http_timeout)


def history_to_record(
    message: dict[str, Any],
    settings: Any,
    *,
    received_at: Any = None,
    allowed_groups: set[str] | None = None,
) -> dict[str, Any] | None:
    """把 NapCat 历史消息转换成和实时上报一致的消息记录。"""
    if str(message.get("post_type") or "message") != "message":
        return None
    if str(message.get("message_type") or "group") != "group":
        return None

    group_id = str(message.get("group_id") or "")
    if not group_id or (allowed_groups is None and not settings.accepts_group(group_id)) or (
        allowed_groups is not None and group_id not in allowed_groups
    ):
        return None

    user_id = str(message.get("user_id") or "")
    self_id = str(message.get("self_id") or "")
    if user_id and self_id and user_id == self_id:
        return None

    content = flatten_message(message.get("message"))
    if not content:
        content = flatten_message(message.get("raw_message"))
    if not content:
        return None

    message_id = str(message.get("message_id") or message.get("real_id") or "")
    if not message_id:
        return None

    sender = message.get("sender") or {}
    sender = sender if isinstance(sender, dict) else {}
    sender_name = str(sender.get("card") or sender.get("nickname") or user_id or "群成员")
    stamp = iso(message.get("time")) or iso(_now())
    event = MessageEvent(
        msg_id=f"onebot:{group_id}:{message_id}",
        group_id=group_id,
        source="onebot-catchup",
        content=content,
        ts=stamp,
        received_at=iso(received_at or message.get("time") or _now()),
        event="GROUP_MESSAGE_CREATE",
        sender_id=user_id,
        sender_name=sender_name,
        group_name=settings.group_name(group_id),
    )
    return onebot_record(event)


def backfill(
    settings: Any,
    store: Any,
    logger: logging.Logger | None = None,
    *,
    client: Any = None,
    now: dt.datetime | None = None,
    force: bool = False,
    stats: dict[str, int] | None = None,
    attachment_sink: Callable[[Any], Any] | None = None,
) -> int:
    """补采白名单群最近消息，返回新增条数。"""
    logger = logger or LOGGER
    if not force and not settings.catchup_enabled:
        return 0

    groups = [str(item) for item in settings.group_whitelist if str(item or "").strip()]
    if not groups:
        logger.warning("历史补采未执行：群白名单为空。")
        return 0

    api = _napcat(settings, client)
    stamp = now or _now()
    cutoff = stamp - dt.timedelta(hours=max(1, int(settings.catchup_hours)))

    try:
        login = api.login_info()
    except Exception as error:  # noqa: BLE001 - 登录信息失败不应影响补采
        logger.warning("历史补采：读取登录信息失败：%s", error)
        login = {}
    self_id = str((login or {}).get("user_id") or "")

    inserted_total = 0
    ok_groups = 0
    failed_groups = 0
    for group_id in groups:
        try:
            messages = api.group_history(group_id, settings.catchup_count)
        except Exception as error:  # noqa: BLE001 - 单个群失败不影响其他群
            failed_groups += 1
            logger.warning("历史补采失败 group=%s：%s", group_id, error)
            continue
        ok_groups += 1

        inserted = 0
        for message in sorted(messages, key=lambda item: float(item.get("time") or 0)):
            message_ts = parse_iso(message.get("time"))
            if message_ts is not None and message_ts < cutoff:
                continue
            if self_id and str(message.get("user_id") or "") == self_id:
                continue
            record = history_to_record(message, settings, received_at=message_ts)
            if not record:
                continue
            if store.insert_message(
                msg_id=record["msg_id"],
                group_id=record["group_id"],
                content=record["content"],
                source_text=record.get("source_text", ""),
                ts=record["ts"],
                received_at=record["received_at"],
                source=record["source"],
                event=record["event"],
                sender_id=record["sender_id"],
                sender_name=record["sender_name"],
                group_name=record["group_name"],
            ):
                inserted += 1
                if attachment_sink is not None:
                    try:
                        from qq_live_digest.attachments import attachments_from_history

                        for attachment in attachments_from_history(message, settings):
                            attachment_sink(attachment)
                    except Exception:  # noqa: BLE001 - 补附件失败不影响补采
                        logger.exception("补采附件入队失败：%s", record["msg_id"])

        if inserted:
            logger.info("历史补采 group=%s 新增 %d 条", settings.group_name(group_id), inserted)
        inserted_total += inserted

    if inserted_total:
        logger.info("历史补采完成：新增 %d 条（回溯 %d 小时）", inserted_total, settings.catchup_hours)
    elif force:
        logger.info("历史补采完成：没有新消息。")
    else:
        logger.debug("历史补采完成：没有新消息。")
    if stats is not None:
        stats.update(
            {
                "groups": len(groups),
                "ok_groups": ok_groups,
                "failed_groups": failed_groups,
                "inserted": inserted_total,
            }
        )
    return inserted_total


def _insert_history_record(store: Any, record: dict[str, Any]) -> bool:
    return bool(
        store.insert_message(
            msg_id=record["msg_id"],
            group_id=record["group_id"],
            content=record["content"],
            source_text=record.get("source_text", ""),
            ts=record["ts"],
            received_at=record["received_at"],
            source=record["source"],
            event=record["event"],
            sender_id=record["sender_id"],
            sender_name=record["sender_name"],
            group_name=record["group_name"],
        )
    )


def available_floor(
    settings: Any,
    group_id: str,
    *,
    client: Any = None,
    page: int = 1000,
    max_pages: int = 20,
) -> dict[str, Any]:
    api = _napcat(settings, client)
    sequence = None
    oldest = None
    seen: set[int] = set()
    total = pages = 0
    error = ""
    try:
        for _ in range(max(1, int(max_pages))):
            messages = api.group_history(str(group_id), int(page), sequence)
            pages += 1
            if not messages:
                break
            total += len(messages)
            candidate = min(messages, key=lambda item: float(item.get("time") or 0))
            if oldest is None or (
                parse_iso(candidate.get("time"))
                and (parse_iso(oldest.get("time")) is None or parse_iso(candidate.get("time")) < parse_iso(oldest.get("time")))
            ):
                oldest = candidate
            try:
                next_seq = int(candidate.get("message_seq"))
            except (TypeError, ValueError):
                break
            if next_seq in seen:
                break
            seen.add(next_seq)
            sequence = next_seq
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
    return {
        "group_id": str(group_id),
        "floor_ts": iso(oldest.get("time")) if oldest else None,
        "floor_message_seq": oldest.get("message_seq") if oldest else None,
        "total_seen": total,
        "pages": pages,
        "error": error,
    }


def backfill_range(
    settings: Any,
    store: Any,
    *,
    groups: Any = None,
    since: Any = None,
    until: Any = None,
    max_messages: int = 20000,
    client: Any = None,
    progress: Callable[..., Any] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    selected = [str(item) for item in (settings.group_whitelist if groups is None else groups) if str(item or "").strip()]
    start, end = parse_iso(since), parse_iso(until)
    api = _napcat(settings, client)
    total_inserted = total_scanned = 0
    details: list[dict[str, Any]] = []
    stopped = False
    limit = max(0, int(max_messages))
    for index, group_id in enumerate(selected):
        detail: dict[str, Any] = {
            "group_id": group_id,
            "inserted": 0,
            "scanned": 0,
            "floor_ts": None,
            "capped": False,
            "error": "",
        }
        sequence = None
        oldest = None
        seen: set[int] = set()
        try:
            while total_scanned < limit and not (cancel and cancel()):
                messages = api.group_history(group_id, 1000, sequence)
                if not messages:
                    break
                page_oldest = min(messages, key=lambda item: float(item.get("time") or 0))
                if oldest is None or (
                    parse_iso(page_oldest.get("time"))
                    and (
                        parse_iso(oldest.get("time")) is None
                        or parse_iso(page_oldest.get("time")) < parse_iso(oldest.get("time"))
                    )
                ):
                    oldest = page_oldest
                for message in messages:
                    if total_scanned >= limit:
                        detail["capped"] = True
                        break
                    total_scanned += 1
                    detail["scanned"] += 1
                    stamp = parse_iso(message.get("time"))
                    if stamp is None or (start and stamp < start) or (end and stamp > end):
                        continue
                    record = history_to_record(message, settings, received_at=stamp, allowed_groups={group_id})
                    if record and _insert_history_record(store, record):
                        total_inserted += 1
                        detail["inserted"] += 1
                page_ts = parse_iso(page_oldest.get("time"))
                if detail["capped"] or (start and page_ts and page_ts < start):
                    break
                try:
                    next_seq = int(page_oldest.get("message_seq"))
                except (TypeError, ValueError):
                    break
                if next_seq in seen:
                    break
                seen.add(next_seq)
                sequence = next_seq
            if total_scanned >= limit:
                detail["capped"] = True
        except Exception as exc:  # noqa: BLE001
            detail["error"] = str(exc)
        detail["floor_ts"] = iso(oldest.get("time")) if oldest else None
        details.append(detail)
        if progress:
            progress("history", index + 1, len(selected), group_id)
        if cancel and cancel():
            stopped = True
            break
    return {
        "ok": not any(item["error"] for item in details),
        "inserted": total_inserted,
        "scanned": total_scanned,
        "groups": details,
        "error": "已取消" if stopped else "",
    }


def run_catchup(
    host: Any,
    *,
    force: bool = False,
    now: dt.datetime | None = None,
    backfill_fn: Callable[..., int],
    retry_base: int,
) -> int:
    stamp = now or _now()
    host.last_catchup_attempt_at = stamp
    stats: dict[str, int] = {}
    try:
        inserted = backfill_fn(
            host.settings,
            host.store,
            host.logger,
            now=stamp,
            force=force,
            stats=stats,
            attachment_sink=host._submit_attachment,
        )
    except Exception:  # noqa: BLE001 - 补采失败不应影响常驻服务
        host.logger.exception("历史补采异常")
        return 0
    groups = int(stats.get("groups", 0) or 0)
    ok_groups = int(stats.get("ok_groups", 0) or 0)
    if groups and ok_groups == 0:
        host._catchup_failures += 1
        retry_seconds = min(30 * 60, retry_base * (2 ** (host._catchup_failures - 1)))
        host.logger.warning(
            "历史补采全部失败（%d 个群），%d 分钟后自动重试。",
            groups,
            max(1, retry_seconds // 60),
        )
        return 0
    host._catchup_failures = 0
    host.last_catchup_at = stamp
    return inserted
