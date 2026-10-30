"""待办用例。库表读写留在传入的 Store，这里只做契约里的参数检查和分组。"""

from __future__ import annotations

import datetime as dt
import json
import secrets
from typing import Any, Mapping

from noticehub.domain.time import iso, parse_iso
from noticehub.infrastructure.clock import SystemClock
from qq_live_digest.store import effective_urgent


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


def create_task(store: Any, summary: Any, deadline: Any = "") -> dict[str, Any]:
    """手动新建待办。非法参数抛 ValueError，文案与现在的 400 一致。"""
    if not isinstance(summary, str):
        raise ValueError("summary 必须是文本")
    text = " ".join(summary.split())
    if not text:
        raise ValueError("请填写待办内容")
    if len(text) > 200:
        raise ValueError("待办内容请控制在 200 字以内")
    if deadline and not isinstance(deadline, str):
        raise ValueError("deadline 必须是文本")
    stamp = ""
    if deadline:
        parsed = parse_iso(deadline)
        if parsed is None:
            raise ValueError("截止时间格式不对，请重新选择")
        stamp = iso(parsed)
    task_id = store.upsert_task(
        task_key=f"manual:{secrets.token_hex(8)}",
        summary=text,
        deadline=stamp,
        source="manual",
        confidence=1.0,
        classification_reason="页面手动新建",
    )
    return {"ok": True, "task_id": task_id, "summary": text, "deadline": stamp}


_MISSING = object()
_TASK_ACTIONS = {"confirm", "dismiss", "done", "reopen", "snooze", "delete"}


def snooze_default_until(settings: Any, now: dt.datetime) -> str:
    """稍后提醒的默认时间：次日早晨的截止提醒时刻，否则次日同一时间。"""
    target = now + dt.timedelta(days=1)
    if settings.deadline_reminders_enabled:
        try:
            hour, minute = (int(part) for part in str(settings.deadline_morning).split(":", 1))
        except (TypeError, ValueError):
            hour, minute = 7, 30
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            target = target.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return iso(target)


def apply_task_action(
    store: Any,
    task_id: int,
    action: str = "",
    done: Any = _MISSING,
    until: str = "",
    *,
    settings: Any = None,
    now: Any = None,
) -> dict[str, Any]:
    """确认、忽略、完成、重开、稍后、删除。缺省 action 时 done 映射为 done/reopen。"""
    name = str(action or "").strip().lower()
    if not name and done is not _MISSING:
        name = "done" if bool(done) else "reopen"
    if name not in _TASK_ACTIONS:
        raise ValueError("unsupported action")
    detail: dict[str, Any] = {}
    if name == "snooze":
        if not until:
            clock = now() if callable(now) else now
            if not isinstance(clock, dt.datetime):
                clock = SystemClock().now()
            until = snooze_default_until(settings, clock)
        detail = {"until": until}
    changed = bool(store.apply_task_action(int(task_id), name, detail=detail))
    task = store.get_task(task_id) if changed else None
    return {
        "ok": changed,
        "id": task_id,
        "action": name,
        "done": bool(task and task.get("status") == "done"),
        "status": (task or {}).get("status", ""),
    }


def set_task_pinned(store: Any, task_id: str, pinned: bool) -> dict[str, Any]:
    """置顶或取消置顶。已完成任务的拒绝文案留在 Store。"""
    parsed = task_id_text(task_id)
    if type(pinned) is not bool:
        raise ValueError("pinned 必须是 true 或 false")
    return store.set_task_pinned(parsed, pinned)


def deadline_label(value: dt.datetime | None, now: dt.datetime) -> tuple[str, bool]:
    if not isinstance(value, dt.datetime):
        return "", False
    overdue = value < now
    if value.date() == now.date():
        return (f"今天 {value:%H:%M} 已过" if overdue else f"今天 {value:%H:%M} 截止"), overdue
    if value.date() == now.date() + dt.timedelta(days=1):
        return f"明天 {value:%H:%M} 截止", False
    return f"{value:%m-%d %H:%M} 截止", overdue and value.date() < now.date()


def confidence_text(task: dict[str, Any]) -> str:
    confidence = float(task.get("confidence") or 0.0)
    if confidence >= 0.75:
        level = "把握较高"
    elif confidence >= 0.55:
        level = "把握中等"
    else:
        level = "把握较低"
    return f"{level} {round(confidence * 100)}%"


def wechat_group_label(
    group_id: str,
    names: Mapping[str, str] | None,
    aliases: Mapping[str, str] | None = None,
) -> str:
    """微信群显示名：别名 > 真实群名 > 群 ID 前 8 位。"""
    key = str(group_id or "")
    if not key:
        return ""
    alias = str((aliases or {}).get(key) or "").strip()
    if alias:
        return alias
    name = str((names or {}).get(key) or "").strip()
    if name and name != key:
        return name
    return key[:8]


def group_tasks(tasks: list[dict[str, Any]], now: dt.datetime) -> dict[str, Any]:
    candidate: list[dict[str, Any]] = []
    today: list[dict[str, Any]] = []
    week: list[dict[str, Any]] = []
    later: list[dict[str, Any]] = []
    done: list[dict[str, Any]] = []
    horizon = now + dt.timedelta(days=7)
    for task in sorted(tasks, key=lambda item: not effective_urgent(item)):
        status = str(task.get("status") or "open")
        if status in {"dismissed", "expired"}:
            continue
        deadline = parse_iso(task.get("deadline"))
        payload = dict(task)
        payload["effective_urgent"] = effective_urgent(task)
        payload["deadline_text"], payload["overdue"] = deadline_label(deadline, now)
        snooze_until = parse_iso(payload.get("snooze_until"))
        payload["snoozed"] = bool(isinstance(snooze_until, dt.datetime) and snooze_until > now)
        payload["snooze_text"] = (
            snooze_until.strftime("%m-%d %H:%M") if payload["snoozed"] and snooze_until else ""
        )
        payload["done"] = status == "done"
        if status == "candidate":
            payload["confidence_text"] = confidence_text(payload)
            detail = str(payload.get("candidate_detail") or "")
            try:
                parsed = json.loads(detail) if detail else {}
            except json.JSONDecodeError:
                parsed = {}
            payload["confidence_reason"] = str(parsed.get("reason") or "需要你确认后再进入正式待办。")
            candidate.append(payload)
            continue
        if payload["done"]:
            done.append(payload)
            continue
        if deadline is None:
            later.append(payload)
        elif deadline.date() <= now.date():
            today.append(payload)
        elif deadline <= horizon:
            week.append(payload)
        else:
            later.append(payload)
    done.sort(key=lambda item: str(item.get("done_at") or ""), reverse=True)
    return {
        "candidates": candidate[:40],
        "today": today[:40],
        "week": week[:40],
        "later": later[:40],
        "done": done[:20],
    }


def overview(tasks: list[dict[str, Any]], grouped: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    open_tasks = [task for task in tasks if str(task.get("status") or "open") == "open"]
    candidates = grouped.get("candidates") or []
    done_count = sum(1 for task in tasks if str(task.get("status") or "") == "done")
    today = grouped["today"]
    if today:
        nearest = min(
            (parse_iso(task.get("deadline")) for task in today if parse_iso(task.get("deadline"))),
            default=None,
        )
        overdue = sum(1 for task in today if task.get("overdue"))
        if overdue:
            headline = f"今天 {len(today)} 件 · 逾期 {overdue} 件"
        elif nearest:
            headline = f"今天 {len(today)} 件，最近一件 {nearest:%H:%M} 截止"
        else:
            headline = f"今天 {len(today)} 件"
    elif candidates:
        headline = f"{len(candidates)} 条通知待确认"
    elif open_tasks:
        headline = "今天没有到期的事"
    else:
        headline = "所有待办都清空了"
    if any(task.get("overdue") for task in today):
        subline = "逾期事项已收起，可展开后查看"
    elif today:
        subline = "按截止时间从上到下处理"
    elif candidates:
        subline = "确认后才会进入正式待办"
    elif open_tasks:
        subline = "当前没有到期事项，可按自己的节奏推进"
    else:
        subline = "可以休息一下，新的通知会自动汇总"
    actionable = len(open_tasks) + len(candidates) + done_count
    progress = round(done_count * 100 / actionable) if actionable else 0
    return {"headline": headline, "subline": subline, "progress": progress}


def task_stats(store: Any) -> dict[str, int]:
    """GET /api/tasks 与 GET /api/meta 共用这一份计数。"""
    stats = store.task_stats()
    return {
        "total": int(stats["total"]),
        "candidate": int(stats["candidate"]),
        "open": int(stats["open"]),
        "done": int(stats["done"]),
        "dismissed": int(stats["dismissed"]),
        "expired": int(stats["expired"]),
        "overdue": int(stats["overdue"]),
    }


def list_task_board(store: Any, settings: Any, *, now: dt.datetime | None = None) -> dict[str, Any]:
    """GET /api/tasks。分组和文案与原来的 group_tasks + overview 相同。"""
    stamp = now if now is not None else SystemClock().now()
    tasks = store.list_tasks()
    names = store.group_names()
    aliases = settings.group_aliases
    wechat_aliases = settings.wechat_group_aliases
    for task in tasks:
        is_wechat = str(task.get("source") or "") == "wechat"
        task["groups"] = [
            wechat_group_label(group_id, names, wechat_aliases)
            if is_wechat
            else store.group_label(group_id, names, aliases)
            for group_id in (task.get("groups") or [])
        ]
    grouped = group_tasks(tasks, stamp)
    payload = overview(tasks, grouped, stamp)
    payload["stats"] = task_stats(store)
    payload.update(grouped)
    payload["pinned"] = sorted(
        (task for task in tasks if task.get("pinned")),
        key=lambda task: (
            str(task.get("deadline") or "9999-99-99"),
            -int(task.get("importance") or 0),
            -int(task.get("id") or 0),
        ),
    )
    return payload
