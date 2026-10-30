"""系统时钟。"""

from __future__ import annotations

import datetime as dt


class SystemClock:
    def now(self) -> dt.datetime:
        return dt.datetime.now()
