"""今况。三端共用的一句，只根据已经取出的待办、最近推送和任务槽快照来写。"""

from __future__ import annotations

import datetime as dt
import os
from typing import Any

AGENT_BRIEF_PATH = "/api/agent/brief"
_AGENT_GET = ("_route_get_api_agent_brief", False)

from noticehub.domain.time import iso, parse_iso
from noticehub.store import effective_urgent

_EMPTY = "所有待办都清空了"


def agent_shell_enabled() -> bool:
    """默认打开；只有 NOTICEHUB_AGENT_SHELL=0 才关。未设置或空串都算开。"""
    return os.environ.get("NOTICEHUB_AGENT_SHELL", "").strip() != "0"


def optional_get(path: str) -> tuple[str, bool] | None:
    """静态路由表之外的今况地址。关闭时返回 None，调用方按原来的 404 处理。"""
    if path == AGENT_BRIEF_PATH and agent_shell_enabled():
        return _AGENT_GET
    return None


def agent_brief(
    tasks: list[dict[str, Any]],
    *,
    now: dt.datetime,
    latest_digest: dict[str, Any] | None = None,
    job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """返回 sentence、action、count、items、updated_at。

    count 是浮球角标：有待确认时用待确认条数，否则用今天的待办件数。
    items 最多 3 条，字段只留 id、summary、deadline、source。
    """
    candidates, today, later_open = _partition(tasks, now)
    sentence = _sentence(candidates, today, later_open, now, latest_digest)
    prefix = _job_prefix(job)
    if prefix and sentence == _EMPTY:
        sentence = prefix
    elif prefix:
        sentence = prefix + sentence
    if candidates:
        action = "confirm"
        count = len(candidates)
        picked = candidates
    elif today or later_open:
        action = "open"
        count = len(today)
        picked = today or later_open
    else:
        action = "none"
        count = 0
        picked = []
    return {
        "sentence": sentence,
        "action": action,
        "count": count,
        "items": [_item(task) for task in _ordered(picked)[:3]],
        "updated_at": iso(now),
    }


def _partition(
    tasks: list[dict[str, Any]], now: dt.datetime
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    today: list[dict[str, Any]] = []
    later_open: list[dict[str, Any]] = []
    for task in tasks:
        status = str(task.get("status") or "open")
        if status in {"dismissed", "expired", "done"}:
            continue
        if status == "candidate":
            candidates.append(task)
            continue
        if status != "open":
            continue
        deadline = parse_iso(task.get("deadline"))
        if deadline is not None and deadline.date() <= now.date():
            today.append(task)
        else:
            later_open.append(task)
    return candidates, today, later_open


def _sentence(
    candidates: list[dict[str, Any]],
    today: list[dict[str, Any]],
    later_open: list[dict[str, Any]],
    now: dt.datetime,
    latest_digest: dict[str, Any] | None,
) -> str:
    parts: list[str] = []
    if today:
        text = f"今天 {len(today)} 件待办"
        overdue = sum(1 for task in today if _overdue(task, now))
        if overdue:
            text += f"，逾期 {overdue} 件"
        parts.append(text)
    if candidates:
        parts.append(f"{len(candidates)} 条还要你确认")
    if parts:
        return "，".join(parts)
    if later_open:
        return "今天没有到期的事"
    count = _digest_count(latest_digest)
    if count:
        return f"刚整理了 {count} 条重点"
    return _EMPTY


def _overdue(task: dict[str, Any], now: dt.datetime) -> bool:
    deadline = parse_iso(task.get("deadline"))
    return bool(deadline is not None and deadline < now)


def _digest_count(latest_digest: dict[str, Any] | None) -> int:
    if not isinstance(latest_digest, dict):
        return 0
    try:
        count = int(latest_digest.get("item_count") or 0)
    except (TypeError, ValueError):
        return 0
    return count if count > 0 else 0


def _job_prefix(job: dict[str, Any] | None) -> str:
    if not isinstance(job, dict) or not job.get("running"):
        return ""
    kind = str(job.get("kind") or "")
    if kind == "history":
        return "正在回溯群消息。"
    if kind == "classify":
        return "正在提取通知。"
    return ""


def _ordered(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        tasks,
        key=lambda task: (
            0 if effective_urgent(task) else 1,
            str(task.get("deadline") or "9999-99-99"),
            -int(task.get("id") or 0),
        ),
    )


def _item(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": task.get("id"),
        "summary": str(task.get("summary") or ""),
        "deadline": str(task.get("deadline") or ""),
        "source": str(task.get("source") or ""),
    }
