"""极小的进程内 TTL 缓存：只读探针共用一个，别再各写一份时间戳判断。"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

_lock = threading.Lock()
_entries: dict[str, tuple[float, Any]] = {}


def get(key: str, ttl: float, producer: Callable[[], Any], *, cache_empty: bool = True) -> Any:
    """取缓存；过期或没有就调 producer 重算。

    cache_empty=False 用于「失败也要立刻能重试」的探针（比如 netsh 读不到）。
    """
    now = time.monotonic()
    with _lock:
        hit = _entries.get(key)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
    value = producer()
    if cache_empty or value:
        with _lock:
            _entries[key] = (time.monotonic(), value)
    return value


def clear(key: str | None = None) -> None:
    """丢掉一个键；不传就清空。"""
    with _lock:
        if key is None:
            _entries.clear()
        else:
            _entries.pop(key, None)
