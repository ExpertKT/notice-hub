"""待办用例。库表读写留在传入的 Store，这里只做契约里的参数检查和分组。"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Mapping

from noticehub.domain.time import parse_iso
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
    payload["stats"] = store.task_stats()
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
