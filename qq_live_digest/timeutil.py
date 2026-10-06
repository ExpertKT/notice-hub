"""统一使用「本地时间的 naive datetime」，避免 aware/naive 混用比较报错。"""

from __future__ import annotations

import datetime as dt
from typing import Any


def now_local() -> dt.datetime:
    return dt.datetime.now()


def to_naive_local(value: Any) -> dt.datetime | None:
    """把 datetime / 时间戳 / ISO 字符串统一成本地 naive datetime。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        number = float(value)
        if number <= 0:
            return None
        if number > 10_000_000_000:
            number /= 1000
        try:
            parsed = dt.datetime.fromtimestamp(number)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = dt.datetime.fromtimestamp(float(text))
            except (OverflowError, OSError, ValueError):
                return None
    else:
        return None

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed.replace(microsecond=0)


def iso(value: Any) -> str:
    parsed = to_naive_local(value)
    return parsed.isoformat(timespec="seconds") if parsed else ""


def parse_iso(text: Any) -> dt.datetime | None:
    return to_naive_local(text)
