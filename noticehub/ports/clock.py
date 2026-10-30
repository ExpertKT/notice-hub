"""时钟端口。领域代码接收 datetime，不自己取当前时间。"""

from __future__ import annotations

import datetime as dt
from typing import Protocol


class Clock(Protocol):
    def now(self) -> dt.datetime:
        """naive 本地时间，不带 tzinfo。"""
