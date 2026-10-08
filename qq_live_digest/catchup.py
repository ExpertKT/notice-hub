"""NapCat/OneBot 历史消息补采。

普通 OneBot HTTP 上报只覆盖在线期间；服务重启、NapCat 掉线、电脑关机期间
的消息不会自动重放。这里通过 NapCat 的 get_group_msg_history 读取最近消息，
按 msg_id 去重后写回本地库，让正常的摘要流程继续处理。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import urllib.error
import urllib.request
from typing import Any, Callable

from .config import Settings
from .receiver import flatten_message
from .attachments import attachments_from_history
from .store import Store
from .timeutil import iso, now_local, parse_iso

LOGGER = logging.getLogger(__name__)


class NapCatError(RuntimeError):
    """NapCat API 调用失败。"""


class NapCatClient:
    def __init__(self, base_url: str, token: str = "", timeout: int = 15) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.token = str(token or "")
        self.timeout = max(5, int(timeout or 15))

    def call(self, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.base_url:
            raise NapCatError("NapCat API 地址为空")
        data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            f"{self.base_url}/{action.lstrip('/')}",
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:200]
            raise NapCatError(f"{action} HTTP {error.code}: {detail}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise NapCatError(f"{action} 请求失败: {error}") from error
        try:
            result = json.loads(raw or "{}")
        except json.JSONDecodeError as error:
            raise NapCatError(f"{action} 返回不是 JSON: {raw[:200]}") from error
        if int(result.get("retcode") or 0) != 0:
            detail = result.get("message") or result.get("wording") or result
            raise NapCatError(f"{action} 返回错误: {detail}")
        return result

    def group_history(self, group_id: str, count: int, message_seq: int | None = None) -> list[dict[str, Any]]:
        payload = {"group_id": str(group_id), "count": int(count)}
        if message_seq is not None:
            payload["message_seq"] = int(message_seq)
        result = self.call("get_group_msg_history", payload)
        data = result.get("data") or {}
        messages = data.get("messages") or []
        return [item for item in messages if isinstance(item, dict)]

    def login_info(self) -> dict[str, Any]:
        result = self.call("get_login_info")
        data = result.get("data") or {}
        return data if isinstance(data, dict) else {}


def history_to_record(
    message: dict[str, Any],
    settings: Settings,
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
    if not group_id or (allowed_groups is None and not settings.accepts_group(group_id)) or (allowed_groups is not None and group_id not in allowed_groups):
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
    stamp = iso(message.get("time")) or iso(now_local())

    return {
        "msg_id": f"onebot:{group_id}:{message_id}",
        "source": "onebot-catchup",
        "event": "GROUP_MESSAGE_CREATE",
        "group_id": group_id,
        "group_name": settings.group_name(group_id),
        "sender_id": user_id,
        "sender_name": sender_name,
        "ts": stamp,
        "received_at": iso(received_at or message.get("time") or now_local()),
        "content": content,
    }


def backfill(
    settings: Settings,
    store: Store,
    logger: logging.Logger | None = None,
    *,
    client: Any = None,
    now: dt.datetime | None = None,
    force: bool = False,
    stats: dict[str, int] | None = None,
    attachment_sink: Callable[[Any], Any] | None = None,
) -> int:
    """补采白名单群最近消息，返回新增条数。

    client 只需要实现 login_info() 和 group_history(group_id, count)，
    便于测试时注入假客户端。
    """
    logger = logger or LOGGER
    if not force and not settings.catchup_enabled:
        return 0

    groups = [str(item) for item in settings.group_whitelist if str(item or "").strip()]
    if not groups:
        logger.warning("历史补采未执行：群白名单为空。")
        return 0

    api = client or NapCatClient(settings.napcat_api_url, settings.napcat_api_token, settings.http_timeout)
    stamp = now or now_local()
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


def _insert_history_record(store: Store, record: dict[str, Any]) -> bool:
    return bool(store.insert_message(msg_id=record["msg_id"], group_id=record["group_id"], content=record["content"], source_text=record.get("source_text", ""), ts=record["ts"], received_at=record["received_at"], source=record["source"], event=record["event"], sender_id=record["sender_id"], sender_name=record["sender_name"], group_name=record["group_name"]))


def available_floor(settings: Settings, group_id: str, *, client: Any = None, page: int = 1000, max_pages: int = 20) -> dict[str, Any]:
    api = client or NapCatClient(settings.napcat_api_url, settings.napcat_api_token, settings.http_timeout)
    sequence = None; oldest = None; seen = set(); total = pages = 0; error = ""
    try:
        for _ in range(max(1, int(max_pages))):
            messages = api.group_history(str(group_id), int(page), sequence); pages += 1
            if not messages: break
            total += len(messages)
            candidate = min(messages, key=lambda item: float(item.get("time") or 0))
            if oldest is None or (parse_iso(candidate.get("time")) and (parse_iso(oldest.get("time")) is None or parse_iso(candidate.get("time")) < parse_iso(oldest.get("time")))): oldest = candidate
            try: next_seq = int(candidate.get("message_seq"))
            except (TypeError, ValueError): break
            if next_seq in seen: break
            seen.add(next_seq); sequence = next_seq
    except Exception as exc: error = str(exc)
    return {"group_id": str(group_id), "floor_ts": iso(oldest.get("time")) if oldest else None, "floor_message_seq": oldest.get("message_seq") if oldest else None, "total_seen": total, "pages": pages, "error": error}


def backfill_range(settings: Settings, store: Store, *, groups=None, since=None, until=None, max_messages=20000, client=None, progress=None, cancel=None) -> dict[str, Any]:
    selected = [str(item) for item in (settings.group_whitelist if groups is None else groups) if str(item or "").strip()]
    start, end = parse_iso(since), parse_iso(until); api = client or NapCatClient(settings.napcat_api_url, settings.napcat_api_token, settings.http_timeout)
    total_inserted = total_scanned = 0; details = []; stopped = False; limit = max(0, int(max_messages))
    for index, group_id in enumerate(selected):
        detail = {"group_id": group_id, "inserted": 0, "scanned": 0, "floor_ts": None, "capped": False, "error": ""}; sequence = None; oldest = None; seen = set()
        try:
            while total_scanned < limit and not (cancel and cancel()):
                messages = api.group_history(group_id, 1000, sequence)
                if not messages: break
                page_oldest = min(messages, key=lambda item: float(item.get("time") or 0))
                if oldest is None or (parse_iso(page_oldest.get("time")) and (parse_iso(oldest.get("time")) is None or parse_iso(page_oldest.get("time")) < parse_iso(oldest.get("time")))): oldest = page_oldest
                for message in messages:
                    if total_scanned >= limit: detail["capped"] = True; break
                    total_scanned += 1; detail["scanned"] += 1; stamp = parse_iso(message.get("time"))
                    if stamp is None or (start and stamp < start) or (end and stamp > end): continue
                    record = history_to_record(message, settings, received_at=stamp, allowed_groups={group_id})
                    if record and _insert_history_record(store, record): total_inserted += 1; detail["inserted"] += 1
                page_ts = parse_iso(page_oldest.get("time"))
                if detail["capped"] or (start and page_ts and page_ts < start): break
                try: next_seq = int(page_oldest.get("message_seq"))
                except (TypeError, ValueError): break
                if next_seq in seen: break
                seen.add(next_seq); sequence = next_seq
            if total_scanned >= limit: detail["capped"] = True
        except Exception as exc: detail["error"] = str(exc)
        detail["floor_ts"] = iso(oldest.get("time")) if oldest else None; details.append(detail)
        if progress: progress("history", index + 1, len(selected), group_id)
        if cancel and cancel(): stopped = True; break
    return {"ok": not any(item["error"] for item in details), "inserted": total_inserted, "scanned": total_scanned, "groups": details, "error": "已取消" if stopped else ""}
