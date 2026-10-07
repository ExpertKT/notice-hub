"""RFC 5545 iCalendar rendering for task deadlines."""

from __future__ import annotations

import datetime as dt
from typing import Any, Iterable

from .timeutil import parse_iso


def _escape_text(value: Any) -> str:
    text = str(value or "")
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def _fold(line: str) -> list[str]:
    """Fold at 75 UTF-8 octets, retaining complete characters."""
    result: list[str] = []
    current = ""
    current_bytes = 0
    for char in line:
        size = len(char.encode("utf-8"))
        limit = 75 if not result else 74
        if current and current_bytes + size > limit:
            result.append(current)
            current = " " + char
            current_bytes = 1 + size
        else:
            current += char
            current_bytes += size
    result.append(current)
    return result


def _prop(name: str, value: Any, params: str = "") -> list[str]:
    return _fold(f"{name}{params}:{value}")


def render_calendar(tasks: Iterable[dict[str, Any]], *, now: dt.datetime | None = None) -> str:
    stamp = (now or dt.datetime.now()).replace(microsecond=0)
    stamp_utc = stamp.strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//qq-live-digest//Task Calendar//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
    lines += _prop("X-WR-CALNAME", _escape_text("群消息待办"))
    lines += _prop("X-WR-TIMEZONE", "Asia/Shanghai")
    for task in tasks:
        deadline_raw = str(task.get("deadline") or "").strip()
        deadline = parse_iso(deadline_raw)
        if deadline is None:
            if len(deadline_raw) == 10:
                try:
                    deadline = dt.datetime.strptime(deadline_raw, "%Y-%m-%d")
                except ValueError:
                    continue
            else:
                continue
        task_id = str(task.get("id") or task.get("task_key") or "task")
        summary = _escape_text(task.get("summary"))
        description = _escape_text(task.get("evidence") or task.get("action") or "")
        status = str(task.get("status") or "open")
        status_value = "COMPLETED" if status == "done" else "CANCELLED" if status in {"dismissed", "expired"} else "NEEDS-ACTION"
        lines.append("BEGIN:VEVENT")
        lines += _prop("UID", f"task-{task_id}@qq-live-digest")
        lines += _prop("DTSTAMP", stamp_utc)
        if len(deadline_raw) == 10 and deadline_raw[4] == "-" and deadline_raw[7] == "-":
            start = deadline.strftime("%Y%m%d")
            end = (deadline + dt.timedelta(days=1)).strftime("%Y%m%d")
            lines += _prop("DTSTART", start, ";VALUE=DATE")
            lines += _prop("DTEND", end, ";VALUE=DATE")
        else:
            lines += _prop("DTSTART", deadline.strftime("%Y%m%dT%H%M%S"), ";TZID=Asia/Shanghai")
            lines += _prop("DURATION", "PT1H")
        lines += _prop("SUMMARY", summary)
        lines += _prop("DESCRIPTION", description)
        lines += _prop("STATUS", status_value)
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
