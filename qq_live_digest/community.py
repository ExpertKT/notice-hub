"""社区在线数：用 abacus 的免费计数端点数「当前在线」和「今日打开」。

计数只是锦上添花，不能影响页面本身：所以这里对外的每个函数都不抛异常，
网络不通、超时、返回看不懂，一律当没数到。
"""

from __future__ import annotations

import json
import time
import urllib.request

BASE_URL = "https://abacus.jasoncameron.dev"
NAMESPACE = "notice-hub"
BUCKET_SECONDS = 300

# 同一进程里每个 key 只上报一次，免得每次刷新页面都往别人服务器打一下。
_PINGED: set[str] = set()


def _timestamp(now: float | None) -> float:
    """把 now 规整成 UTC epoch 秒；不传就用当前时间。"""
    return time.time() if now is None else float(now)


def bucket_key(now: float | None = None) -> str:
    """5 分钟一个桶的 key：同一时间段进来的人算一批，在线数不随刷新抖动。"""
    return "online-%d" % int(_timestamp(now) // BUCKET_SECONDS)


def day_key(now: float | None = None) -> str:
    """按 UTC 日期分桶的 key：今日打开次数。"""
    return "today-" + time.strftime("%Y%m%d", time.gmtime(_timestamp(now)))


def _url(action: str, key: str) -> str:
    return "%s/%s/%s/%s" % (BASE_URL, action, NAMESPACE, key)


def _hit(key: str, timeout: float = 5.0) -> None:
    """打一次 hit；失败由调用方决定怎么处理。"""
    with urllib.request.urlopen(_url("hit", key), timeout=timeout) as response:
        response.read()


def ping(now: float | None = None) -> None:
    """上报一次在线：桶 key 和当日 key 各打一次，同一个 key 本进程内只打一次。

    先记账再发请求：失败了也不重试，免得网络不通时每次开页面都卡一下。
    """
    for key in (bucket_key(now), day_key(now)):
        if key in _PINGED:
            continue
        _PINGED.add(key)
        try:
            _hit(key)
        except Exception:  # noqa: BLE001 —— 计数失败绝不能冒到页面
            pass


def _value(key: str, timeout: float) -> int | None:
    """读一个计数器的值；任何一步不对就返回 None。"""
    try:
        with urllib.request.urlopen(_url("get", key), timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    value = data.get("value") if isinstance(data, dict) else None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def fetch_counts(timeout: float = 5.0) -> dict:
    """读在线的两个数：读不到的那项是 None，调用方自己决定要不要显示。"""
    return {"online": _value(bucket_key(), timeout), "today": _value(day_key(), timeout)}
