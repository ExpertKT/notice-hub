"""统一使用「本地时间的 naive datetime」，避免 aware/naive 混用比较报错。"""

from __future__ import annotations

import datetime as dt

from noticehub.domain.time import iso, parse_iso, to_naive_local
from noticehub.infrastructure.clock import SystemClock

__all__ = ["iso", "now_local", "parse_iso", "to_naive_local"]


def now_local() -> dt.datetime:
    return SystemClock().now()
