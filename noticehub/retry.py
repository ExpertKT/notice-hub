"""外部调用的有限次重试，以及「哪些错值得再试一次」的判定。

重试的是调用方传进来的函数，本模块自己不发请求。副作用只有 `time.sleep`。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
from typing import Any, Callable

__all__ = [
    "TRANSIENT_HTTP_CODES",
    "LLMError",
    "LLMRequestError",
    "LLMResponseError",
    "is_transient_http",
    "llm_should_retry",
    "call_with_retries",
]

LOGGER = logging.getLogger(__name__)

# 值得再试一次的 HTTP 状态码：限流、网关抖动、服务端临时不可用。
# 400/401/403/404 这类「再说一遍也一样」的错不在里面。
TRANSIENT_HTTP_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class LLMError(RuntimeError):
    """模型/视觉调用的统一基类；异常文本就是给人看的那句话。"""


class LLMRequestError(LLMError):
    """请求本身没成功。`status` 为 0 表示没有 HTTP 状态码（网络层/超时）。"""

    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = int(status or 0)


class LLMResponseError(LLMError):
    """请求通了，但拿回来的东西不能当结果用（非 JSON、结构不对）。"""


def is_transient_http(status: int) -> bool:
    """这个状态码值不值得重试；容忍字符串形式的数字。"""
    try:
        return int(status) in TRANSIENT_HTTP_CODES
    except (TypeError, ValueError):
        return False


def _http_status_of(error: BaseException) -> int | None:
    """从 urllib 的 HTTPError 里取状态码；不是就 None。"""
    if isinstance(error, urllib.error.HTTPError):
        try:
            return int(error.code)
        except (TypeError, ValueError):
            return None
    return None


def llm_should_retry(error: BaseException) -> bool:
    """判定顺序很关键：HTTP 状态码先于「OSError 一律可重试」，否则 401 会被误判。"""
    status = _http_status_of(error)
    if status is not None:
        return is_transient_http(status)
    if isinstance(error, LLMRequestError):
        return error.status == 0 or is_transient_http(error.status)
    if isinstance(error, LLMResponseError):
        return True
    if isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError, OSError)):
        return True
    if isinstance(error, json.JSONDecodeError):
        return True
    return False


def _delay_for(backoff: float, attempt: int) -> float:
    """第 attempt 次重试前的等待：backoff * 2^(attempt-1)，负数按 0 处理。"""
    base = max(0.0, float(backoff))
    return base * (2 ** (attempt - 1))


def call_with_retries(
    func: Callable[[], Any],
    *,
    retries: int = 2,
    backoff: float = 1.5,
    should_retry: Callable[[BaseException], bool] | None = None,
    logger: logging.Logger | None = None,
    label: str = "外部调用",
) -> Any:
    """最多试 `max(1, retries+1)` 次；最后一次失败原样抛出那个异常。

    `should_retry` 为 None 时任何异常都会重试；判假则立刻抛出，不再等待。
    只接 Exception，KeyboardInterrupt/SystemExit 直接穿透。
    """
    try:
        attempts = max(1, int(retries) + 1)
    except (TypeError, ValueError):
        attempts = 1
    log = logger or LOGGER
    decided = should_retry or (lambda _error: True)
    last: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            return func()
        except Exception as error:  # noqa: BLE001 - 只接 Exception，中断类穿透
            last = error
            if attempt >= attempts or not decided(error):
                raise
            log.warning("%s第 %d/%d 次失败，准备重试：%s", label, attempt, attempts, error)
            delay = _delay_for(backoff, attempt)
            if delay > 0:
                time.sleep(delay)

    # 循环里要么返回要么抛出，走到这里只可能是 attempts 异常，兜一层保险。
    raise LLMError("%s失败：%s" % (label, last))
