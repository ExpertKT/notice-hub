"""本地时间门面：系统内一律用「本地时区的 naive datetime」，精度到秒。

这里只做四件事：取现在、把各种入参磨成 naive 本地时间、转成 ISO 串、从串解析回来。
除 `now_local` 外都是纯函数——同样的入参调多少次都是同一个结果，不读时钟、不碰盘。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

__all__ = ["iso", "now_local", "parse_iso", "to_naive_local"]

# 毫秒时间戳的下界：秒级时间戳要到 2286 年才会越过它，所以「比它大」就按毫秒解释。
# 注意是严格大于——恰好等于这个值时仍当秒处理。
_MILLISECOND_CUTOFF = 10_000_000_000


def now_local() -> dt.datetime:
    """当前本地墙钟时间，naive、微秒截断。"""
    return dt.datetime.now().replace(microsecond=0)


def _drop_micros(moment: dt.datetime) -> dt.datetime:
    """统一出口：aware 先落回本地时区，然后去掉 tzinfo 与微秒。"""
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return moment.replace(microsecond=0)


def _from_epoch(number: float) -> dt.datetime | None:
    """Unix 时间戳 → 本地 naive 时间；非正数、毫秒、溢出、NaN 都有各自的出路。"""
    if number <= 0:
        return None
    seconds = number / 1000.0 if number > _MILLISECOND_CUTOFF else number
    try:
        return dt.datetime.fromtimestamp(seconds)
    except (OverflowError, OSError, ValueError):
        return None


def _from_text(text: str) -> dt.datetime | None:
    """先按 ISO 8601 认，认不出再退一步当浮点秒。"""
    cleaned = text.strip()
    if not cleaned:
        return None
    candidate = cleaned[:-1] + "+00:00" if cleaned.endswith(("Z", "z")) else cleaned
    try:
        return dt.datetime.fromisoformat(candidate)
    except ValueError:
        pass
    try:
        return _from_epoch(float(cleaned))
    except (TypeError, ValueError, OverflowError):
        return None


def to_naive_local(value: Any) -> dt.datetime | None:
    """把 None/bool/int/float/str/datetime 磨成 naive 本地时间；认不出就 None。

    bool 是 int 的子类，必须先判掉——`True` 不该被当成 1 秒。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dt.datetime):
        return _drop_micros(value)
    if isinstance(value, (int, float)):
        moment = _from_epoch(float(value))
        return _drop_micros(moment) if moment is not None else None
    if isinstance(value, str):
        moment = _from_text(value)
        return _drop_micros(moment) if moment is not None else None
    return None


def parse_iso(text: Any) -> dt.datetime | None:
    """字符串（也容忍数字）→ naive 本地时间；与 `to_naive_local` 同一套语义。"""
    return to_naive_local(text)


def iso(value: Any) -> str:
    """转成 `2026-09-29T01:00:00` 这种秒精度串；转不出东西时给空串，不给 "None"。"""
    moment = to_naive_local(value)
    return moment.isoformat(timespec="seconds") if moment is not None else ""
