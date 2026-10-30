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


# SEQUENCE 必须是 32 位有符号整数；以 2020-01-01T00:00:00Z 为基准压小，
# 既保持单调递增，又不会在 2038 年溢出。
_SEQUENCE_EPOCH = 1_577_836_800


def _utc_stamp(value: dt.datetime) -> str:
    """本地 naive 时间 → 真正的 UTC 时间戳（拼接 Z 之前必须先换算）。"""
    return value.astimezone().astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# 日历对外的时区固定是 +08:00（X-WR-TIMEZONE / VTIMEZONE 都这么写）。
# 所以 DTSTART 的数值必须先换算到 +08:00，否则在 UTC 的机器上会整体早 8 小时
# （issue #6：同一个 VEVENT 里 DTSTAMP 是 UTC、DTSTART 却是本地数值贴上海标签）。
CST = dt.timezone(dt.timedelta(hours=8))


def _local_tzinfo() -> dt.tzinfo:
    """本机当前时区（单独抽出来，测试可以替换成 UTC 模拟 Docker/CI 机器）。"""
    return dt.datetime.now().astimezone().tzinfo


def _to_cst_naive(value: dt.datetime, local_tz: dt.tzinfo | None = None) -> dt.datetime:
    """机器本地 naive 时间 → 上海时间 naive。

    naive 输入先按本机时区解释（测试可传 ``local_tz`` 显式指定），再换算到 +08:00；
    已带 tzinfo 的输入直接换算，不再套本地时区（否则会二次偏移）。
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=local_tz if local_tz is not None else _local_tzinfo())
    return value.astimezone(CST).replace(tzinfo=None)


def render_calendar(tasks: Iterable[dict[str, Any]], *, now: dt.datetime | None = None) -> str:
    stamp = (now or dt.datetime.now()).replace(microsecond=0)
    stamp_utc = _utc_stamp(stamp)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//qq-live-digest//Task Calendar//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
    lines += _prop("X-WR-CALNAME", _escape_text("群务台"))
    lines += _prop("X-WR-TIMEZONE", "Asia/Shanghai")
    # RFC 7986：给客户端一个刷新建议。iOS 只把它当参考，还会受
    # 「设置 → 日历 → 账户 → 抓取新数据」影响；补上它能少一些「网页改了、手机没变」。
    lines += _prop("X-PUBLISHED-TTL", "PT15M")
    lines += _prop("REFRESH-INTERVAL", "PT15M", ";VALUE=DURATION")
    # RFC 5545 3.2.19: 每个被 DTSTART 引用的 TZID 都必须由 VTIMEZONE 定义。
    # 缺了它 iOS 会丢掉这些带时刻的日程（用户实测只显示旧日程）。
    lines += [
        "BEGIN:VTIMEZONE",
        "TZID:Asia/Shanghai",
        "X-LIC-LOCATION:Asia/Shanghai",
        "BEGIN:STANDARD",
        "TZOFFSETFROM:+0800",
        "TZOFFSETTO:+0800",
        "TZNAME:CST",
        "DTSTART:19700101T000000",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]
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
        # RFC 5545 3.8.8.5: VEVENT 的 STATUS 只允许 TENTATIVE / CONFIRMED / CANCELLED。
        # "COMPLETED" 是 VTODO 的取值，写进 VEVENT 属于越界，严格客户端可能整条忽略
        # 或直接报错，所以已完成的事项一律映射成 CONFIRMED（=「确定会发生/已发生」），
        # 而不是输出一个非法值。dismissed/expired 才是真正的 CANCELLED。
        status_value = "CANCELLED" if status in {"dismissed", "expired"} else "CONFIRMED"
        lines.append("BEGIN:VEVENT")
        lines += _prop("UID", f"task-{task_id}@qq-live-digest")
        lines += _prop("DTSTAMP", stamp_utc)
        # SEQUENCE / LAST-MODIFIED 是客户端判断「同一个 UID 要不要更新」的依据。
        # 少了它们，iOS 会认为该事件从未变过，网页上改过的截止时间就同步不过去。
        updated = parse_iso(task.get("updated_at") or task.get("created_at") or "")
        if updated is not None:
            modified_utc = updated.astimezone().astimezone(dt.timezone.utc)
            lines += _prop("LAST-MODIFIED", modified_utc.strftime("%Y%m%dT%H%M%SZ"))
            lines += _prop("SEQUENCE", int(modified_utc.timestamp()) - _SEQUENCE_EPOCH)
        else:
            lines += _prop("SEQUENCE", 0)
        if len(deadline_raw) == 10 and deadline_raw[4] == "-" and deadline_raw[7] == "-":
            start = deadline.strftime("%Y%m%d")
            end = (deadline + dt.timedelta(days=1)).strftime("%Y%m%d")
            lines += _prop("DTSTART", start, ";VALUE=DATE")
            lines += _prop("DTEND", end, ";VALUE=DATE")
        else:
            lines += _prop("DTSTART", _to_cst_naive(deadline).strftime("%Y%m%dT%H%M%S"), ";TZID=Asia/Shanghai")
            lines += _prop("DURATION", "PT1H")
        lines += _prop("SUMMARY", summary)
        lines += _prop("DESCRIPTION", description)
        lines += _prop("STATUS", status_value)
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
