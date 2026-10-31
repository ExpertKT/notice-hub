"""社区在线数：用 abacus 的免费计数端点数「当前在线」和「今日打开」。

计数只是锦上添花，不能影响页面本身：所以这里对外的每个函数都不抛异常，
网络不通、超时、返回看不懂，一律当没数到。
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request

from .config import parse_bool

LOGGER = logging.getLogger(__name__)

BASE_URL = "https://abacus.jasoncameron.dev"
NAMESPACE = "notice-hub"
BUCKET_SECONDS = 300
TELEMETRY_ENV = "NOTICEHUB_COMMUNITY_TELEMETRY"
LEGACY_TELEMETRY_ENV = "QQ_DIGEST_COMMUNITY_TELEMETRY"

# 同一进程里每个 key 只上报一次，免得每次刷新页面都往别人服务器打一下。
_PINGED: set[str] = set()
_DISABLED_LOGGED = False


def telemetry_enabled(enabled: bool | None = None) -> bool:
    """社区计数开关：显式传入优先，否则每次现读环境变量（默认关）。

    设置页保存时会把新值同时写进 os.environ（见 settings_api），所以这里每次现读
    就能「关掉立刻生效」，不需要重启；不缓存开关值。计数端点在境外，缺省关闭。
    """
    if enabled is not None:
        return bool(enabled)
    return parse_bool(os.environ.get(TELEMETRY_ENV, os.environ.get(LEGACY_TELEMETRY_ENV)), False)


def _note_disabled() -> None:
    """关闭态只记一次，别每次消息/页面请求都刷一行日志。"""
    global _DISABLED_LOGGED
    if not _DISABLED_LOGGED:
        _DISABLED_LOGGED = True
        LOGGER.info("社区计数已关闭（community_telemetry=False）。")


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


def ping(now: float | None = None, timeout: float = 5.0, *, enabled: bool | None = None) -> None:
    """上报一次在线：桶 key 和当日 key 各打一次，同一个 key 本进程内只打一次。

    先记账再发请求：失败了也不重试，免得网络不通时每次开页面都卡一下。
    timeout 由调用方按场景收紧——页面请求里给 1 秒就够，别为锦上添花拖慢首屏。
    开关关闭时一个请求都不发。
    """
    if not telemetry_enabled(enabled):
        _note_disabled()
        return
    for key in (bucket_key(now), day_key(now)):
        if key in _PINGED:
            continue
        _PINGED.add(key)
        try:
            _hit(key, timeout=timeout)
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


# fetch_counts 的结果缓存：/api/community/counts 是页面每次都拉的，abacus 免费计数端点
# 又在境外，两个串行 GET 最坏 10s 会把页面请求拖死。在线数本来就是 5 分钟桶，缓存 60s
# 完全够新。cache_empty=False 语义：拿不到（双 None）不缓存，下一轮请求再试。
_COUNTS_CACHE: dict[str, tuple[float, dict]] = {}
_COUNTS_CACHE_TTL = 60.0


def fetch_counts(timeout: float = 2.0, *, enabled: bool | None = None) -> dict:
    """读在线的两个数：读不到的那项是 None，调用方自己决定要不要显示。

    带进程内缓存：60 秒内的重复调用直接返回上一次结果，不打计数端点。
    开关关闭时直接返回双 None：不发请求、也不写缓存（打开后立刻能拿到新数）。
    """
    if not telemetry_enabled(enabled):
        _note_disabled()
        return {"online": None, "today": None}
    now = time.monotonic()
    cached = _COUNTS_CACHE.get("counts")
    if cached is not None and now - cached[0] < _COUNTS_CACHE_TTL:
        return cached[1]
    counts = {"online": _value(bucket_key(), timeout), "today": _value(day_key(), timeout)}
    # 只缓存「在线数拿到了」的结果：online 为空时缓存会把 null 锁住一分钟，
    # 页面整段时间都显示「暂不可用」，而下一轮请求本来就能拿到。
    if counts["online"] is not None:
        _COUNTS_CACHE["counts"] = (now, counts)
    return counts
