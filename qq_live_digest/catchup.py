"""NapCat/OneBot 历史消息补采。

HTTP 客户端留在这里。补采编排在 noticehub.application.history。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from noticehub.application.history import available_floor, backfill, backfill_range, history_to_record

LOGGER = logging.getLogger(__name__)

__all__ = [
    "NapCatError",
    "NapCatClient",
    "history_to_record",
    "backfill",
    "available_floor",
    "backfill_range",
]


class NapCatError(RuntimeError):
    """NapCat API 调用失败。"""


class NapCatClient:
    def __init__(self, base_url: str, token: str = "", timeout: int = 15) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.token = str(token or "")
        self.timeout = max(5, int(timeout or 15))

    def call(self, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.base_url:
            raise NapCatError("NapCat API 地址为空")
        data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            f"{self.base_url}/{action.lstrip('/')}",
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:200]
            raise NapCatError(f"{action} HTTP {error.code}: {detail}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise NapCatError(f"{action} 请求失败: {error}") from error
        try:
            result = json.loads(raw or "{}")
        except json.JSONDecodeError as error:
            raise NapCatError(f"{action} 返回不是 JSON: {raw[:200]}") from error
        if int(result.get("retcode") or 0) != 0:
            detail = result.get("message") or result.get("wording") or result
            raise NapCatError(f"{action} 返回错误: {detail}")
        return result

    def group_history(self, group_id: str, count: int, message_seq: int | None = None) -> list[dict[str, Any]]:
        payload = {"group_id": str(group_id), "count": int(count)}
        if message_seq is not None:
            payload["message_seq"] = int(message_seq)
        result = self.call("get_group_msg_history", payload)
        data = result.get("data") or {}
        messages = data.get("messages") or []
        return [item for item in messages if isinstance(item, dict)]

    def login_info(self) -> dict[str, Any]:
        result = self.call("get_login_info")
        data = result.get("data") or {}
        return data if isinstance(data, dict) else {}
