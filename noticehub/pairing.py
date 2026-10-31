"""手机 App 配对：手机屏幕上显示一个短码，用户在电脑上点「允许」，手机就拿到访问令牌。

手机端不用输入网址、也不用抄令牌：App 里已经带好电脑地址，令牌由这一步直接发下去。
码本身不代表权限——只有拿着同一个 code + secret 的那台手机会拿到令牌，
电脑上点「允许」才是唯一的授权动作。
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any

PAIR_TTL_SECONDS = 300
MAX_PENDING = 5
START_COOLDOWN_SECONDS = 3.0
# S6/S7 红队：/api/pair/start 免鉴权，外人刷满 5 条就能让机主 5 分钟配不了对。
# 同一来源 10 分钟最多发起 3 次；机主可在设置页一键清空挂起的请求。
START_WINDOW_SECONDS = 600.0
START_MAX_PER_WINDOW = 3

_LOCK = threading.Lock()
_REQUESTS: dict[str, dict[str, Any]] = {}
_LAST_START: dict[str, float] = {}
_START_HISTORY: dict[str, list[float]] = {}


class PairingRateLimited(RuntimeError):
    """同一来源发起得太频繁（映射成 HTTP 429，而不是「操作失败」）。"""


def _now(now: float | None) -> float:
    return time.time() if now is None else float(now)


def _prune(now: float) -> None:
    for code, request in list(_REQUESTS.items()):
        if now - float(request["created_at"]) > PAIR_TTL_SECONDS:
            _REQUESTS.pop(code, None)
    # 冷却记录也要跟着消失，否则字典只增不减（红队 S7：内存型限流不清理）。
    for key, last in list(_LAST_START.items()):
        if now - last >= START_WINDOW_SECONDS:
            _LAST_START.pop(key, None)
    for key, moments in list(_START_HISTORY.items()):
        kept = [moment for moment in moments if now - moment < START_WINDOW_SECONDS]
        if kept:
            _START_HISTORY[key] = kept
        else:
            _START_HISTORY.pop(key, None)


def _new_code() -> str:
    while True:
        code = f"{secrets.randbelow(1_000_000):06d}"
        if code not in _REQUESTS:
            return code


def start(device: str = "", source: str = "", *, now: float | None = None) -> dict[str, Any]:
    """手机发起配对，返回要显示在手机屏幕上的短码。"""
    moment = _now(now)
    key = source or device or "unknown"
    with _LOCK:
        _prune(moment)
        last = _LAST_START.get(key, 0.0)
        if moment - last < START_COOLDOWN_SECONDS:
            raise ValueError("刚刚已经发起过配对，请稍等几秒再试。")
        history = _START_HISTORY.get(key, [])
        if len(history) >= START_MAX_PER_WINDOW:
            raise PairingRateLimited("配对请求过于频繁，请稍后再试")
        if sum(1 for item in _REQUESTS.values() if item["status"] == "pending") >= MAX_PENDING:
            raise ValueError("电脑上还有没处理的配对请求，请先在电脑上点「允许」或「拒绝」。")
        _LAST_START[key] = moment
        _START_HISTORY.setdefault(key, []).append(moment)
        code = _new_code()
        _REQUESTS[code] = {
            "code": code,
            "secret": secrets.token_urlsafe(24),
            "device": str(device or "")[:80],
            "source": str(source or "")[:60],
            "created_at": moment,
            "status": "pending",
        }
        return {"code": code, "secret": _REQUESTS[code]["secret"], "expires_in": PAIR_TTL_SECONDS}


def status(code: str, secret: str, *, now: float | None = None) -> dict[str, Any] | None:
    """手机轮询配对结果。必须同时对上 code 与 secret，别人的码问不出任何东西。"""
    moment = _now(now)
    with _LOCK:
        _prune(moment)
        request = _REQUESTS.get(str(code or ""))
        if request is None or not secrets.compare_digest(str(request["secret"]), str(secret or "")):
            return None
        return {"status": request["status"], "device": request["device"]}


def pending(*, now: float | None = None) -> list[dict[str, Any]]:
    """电脑端设置页要展示的「等待批准的手机」。"""
    moment = _now(now)
    with _LOCK:
        _prune(moment)
        items = [item for item in _REQUESTS.values() if item["status"] == "pending"]
        return [
            {
                "code": item["code"],
                "device": item["device"],
                "source": item["source"],
                "waiting_seconds": int(max(0.0, moment - float(item["created_at"]))),
            }
            for item in sorted(items, key=lambda row: row["created_at"])
        ]


def decide(code: str, approved: bool, *, now: float | None = None) -> dict[str, Any]:
    """电脑端点「允许」或「拒绝」。"""
    moment = _now(now)
    with _LOCK:
        _prune(moment)
        request = _REQUESTS.get(str(code or ""))
        if request is None:
            return {"ok": False, "error": "这个配对码已经过期了，让手机重新发起一次。"}
        if request["status"] != "pending":
            return {"ok": False, "error": "这个配对请求已经处理过了。"}
        request["status"] = "approved" if approved else "denied"
        return {"ok": True, "status": request["status"]}


def forget(code: str) -> None:
    with _LOCK:
        _REQUESTS.pop(str(code or ""), None)


def clear_pending(*, now: float | None = None) -> int:
    """机主一键清空挂起的配对请求，返回清掉几条。

    同时裁掉冷却记录里「已经没有任何请求的 source」：否则被外人刷过之后，
    机主清空了列表却还要再等 10 分钟才能重新配对。
    """
    moment = _now(now)
    with _LOCK:
        _prune(moment)
        cleared = len(_REQUESTS)
        _REQUESTS.clear()
        # 列表清空后已经没有任何 source 还挂着请求：冷却记录一并裁掉，机主立刻能重新配对。
        _LAST_START.clear()
        _START_HISTORY.clear()
        return cleared


def reset() -> None:
    """测试用：清空所有配对状态。"""
    with _LOCK:
        _REQUESTS.clear()
        _LAST_START.clear()
        _START_HISTORY.clear()
