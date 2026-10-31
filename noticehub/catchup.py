"""NapCat(OneBot v11) HTTP 客户端 + 历史补采的公开面。

编排实现在 `noticehub.application.history`；这里只做两件事：
1. 提供能直接打 OneBot 接口的客户端（出错时给出能看懂的中文句子）；
2. 把编排层的那几个函数再导出，让调用方与测试统一从 `noticehub.catchup` 取。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from .application.history import available_floor, backfill, backfill_range, history_to_record

__all__ = ["NapCatError", "NapCatClient", "history_to_record", "backfill", "available_floor", "backfill_range"]

LOGGER = logging.getLogger(__name__)

# 错误正文截断长度：够看清服务端说了什么，又不至于把日志刷爆。
_DETAIL_LIMIT = 200


class NapCatError(RuntimeError):
    """NapCat 调用失败；异常文本本身就是给人看的那句话（含动作名）。"""


def _clip(text: Any) -> str:
    return str(text or "")[:_DETAIL_LIMIT]


class NapCatClient:
    """最小的 OneBot v11 调用器：POST JSON、不重试（重试策略由调用方决定）。"""

    def __init__(self, base_url: str, token: str = "", timeout: int = 15) -> None:
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.token = str(token or "")
        self.timeout = max(5, int(timeout or 15))

    def _endpoint(self, action: str) -> str:
        return "%s/%s" % (self.base_url, str(action).lstrip("/"))

    def _request(self, action: str, payload: dict) -> dict:
        if not self.base_url:
            raise NapCatError("NapCat API 地址为空")
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer %s" % self.token
        request = urllib.request.Request(self._endpoint(action), data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            detail = ""
            try:
                detail = error.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - 读不出正文也要报出状态码
                detail = ""
            raise NapCatError("%s HTTP %s: %s" % (action, error.code, _clip(detail))) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise NapCatError("%s 请求失败: %s" % (action, error)) from error
        try:
            data = json.loads(raw)
        except ValueError as error:
            raise NapCatError("%s 返回不是 JSON: %s" % (action, _clip(raw))) from error
        if not isinstance(data, dict):
            raise NapCatError("%s 返回不是 JSON: %s" % (action, _clip(raw)))
        code = data.get("retcode", 0)
        try:
            code = int(code or 0)
        except (TypeError, ValueError):
            code = 0
        if code != 0:
            detail = data.get("message") or data.get("wording") or data
            raise NapCatError("%s 返回错误: %s" % (action, detail))
        return data

    def call(self, action: str, payload: dict | None = None) -> dict:
        """调一个 OneBot 动作，成功返回**完整响应**（不是只给 data）。"""
        return self._request(str(action), dict(payload or {}))

    def group_history(self, group_id: str, count: int, message_seq: int | None = None) -> list[dict]:
        payload: dict[str, Any] = {"group_id": str(group_id), "count": int(count)}
        if message_seq is not None:
            payload["message_seq"] = int(message_seq)
        response = self.call("get_group_msg_history", payload)
        data = response.get("data")
        if not isinstance(data, dict):
            return []
        messages = data.get("messages")
        if not isinstance(messages, list):
            return []
        return [item for item in messages if isinstance(item, dict)]

    def login_info(self) -> dict:
        response = self.call("get_login_info", {})
        data = response.get("data")
        return data if isinstance(data, dict) else {}
