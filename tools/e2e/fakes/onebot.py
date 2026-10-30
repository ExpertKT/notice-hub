"""W3 平台替身：OneBot v11 HTTP 服务（本地假 NapCat）。

用途
----
E2E 剧本里替代真 NapCat：被测实例把它当 NapCat 的 OneBot HTTP API 用
（`QQ_DIGEST_NAPCAT_API_URL=http://127.0.0.1:<port>`），同时提供 `/__inject`
让剧本把「群里来了一条消息」推进被测实例的 OneBot 事件接收端
（默认 `http://127.0.0.1:8765/onebot/event`）。

协议形状来源
------------
应答形状照 `tests/test_catchup.py:41` 的 `FakeNapCat`（它只实现了 Python 方法，
这里补成 HTTP）+ `qq_live_digest/catchup.py:36-73` 的真实调用方式：
`POST {base}/{action}`，body JSON，`Authorization: Bearer <token>`，
应答 `{"status": "ok", "retcode": 0, "data": ...}`。
事件字段照 `tools/local_smoke.py:61-72` 的 `onebot_event`。

随机端口
--------
一律 `bind(("127.0.0.1", 0))`，不占用 8765/8766/8767/8768 与 3000/5000/8000。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULT_GROUPS = [
    {"group_id": 10001, "group_name": "学院通知群", "member_count": 3, "max_member_count": 200},
    {"group_id": 10002, "group_name": "2024 级年级群", "member_count": 5, "max_member_count": 500},
]

DEFAULT_MEMBERS = {
    10001: [
        {"user_id": 2001, "nickname": "辅导员", "card": "辅导员"},
        {"user_id": 2002, "nickname": "学委", "card": "学委"},
        {"user_id": 111, "nickname": "E2E 机器人", "card": ""},
    ],
    10002: [
        {"user_id": 2002, "nickname": "学委", "card": "学委"},
        {"user_id": 111, "nickname": "E2E 机器人", "card": ""},
    ],
}

LOGIN = {"user_id": 111, "nickname": "E2E 机器人"}


def build_event(
    text: str,
    *,
    group_id: int | str = 10001,
    user_id: int = 2001,
    sender: str = "辅导员",
    message_id: int | None = None,
    self_id: int = 111,
    timestamp: int | None = None,
) -> dict[str, Any]:
    """造一条 OneBot v11 群消息事件（字段同 tools/local_smoke.py:61-72）。"""
    return {
        "post_type": "message",
        "message_type": "group",
        "message_id": int(message_id if message_id is not None else int(time.time() * 1000) % 10**9),
        "group_id": int(group_id),
        "user_id": int(user_id),
        "self_id": int(self_id),
        "time": int(timestamp if timestamp is not None else time.time()),
        "sender": {"card": sender, "nickname": sender},
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text,
    }


class _State:
    def __init__(self, token: str, target_url: str, target_token: str) -> None:
        self.lock = threading.Lock()
        self.token = token
        self.target_url = target_url
        self.target_token = target_token
        self.groups = [dict(item) for item in DEFAULT_GROUPS]
        self.members = {key: [dict(row) for row in value] for key, value in DEFAULT_MEMBERS.items()}
        self.history: dict[str, list[dict[str, Any]]] = {str(g["group_id"]): [] for g in self.groups}
        self.calls: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.injected: list[dict[str, Any]] = []
        self.next_message_id = 90000

    def record_call(self, action: str, payload: dict[str, Any]) -> None:
        with self.lock:
            self.calls.append({"action": action, "payload": payload, "at": time.time()})

    def add_history(self, event: dict[str, Any]) -> None:
        group = str(event.get("group_id") or "")
        with self.lock:
            self.history.setdefault(group, []).append(event)

    def take_message_id(self) -> int:
        with self.lock:
            self.next_message_id += 1
            return self.next_message_id


def _make_handler(state: _State):
    class Handler(BaseHTTPRequestHandler):
        server_version = "nh-fake-onebot/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002 - 基类签名
            return

        # ---- 基础工具 ----
        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                parsed = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}

        def _authorized(self) -> bool:
            token = state.token
            if not token:
                return True
            header = str(self.headers.get("Authorization") or "")
            candidate = header[7:].strip() if header.startswith("Bearer ") else header.strip()
            query = ""
            if "?" in self.path:
                for part in self.path.split("?", 1)[1].split("&"):
                    if part.startswith("access_token="):
                        query = part.split("=", 1)[1]
            return candidate == token or query == token

        def _ok(self, data: Any) -> None:
            self._json(200, {"status": "ok", "retcode": 0, "data": data})

        def _fail(self, retcode: int, message: str) -> None:
            self._json(200, {"status": "failed", "retcode": retcode, "message": message})

        # ---- 路由 ----
        def do_GET(self) -> None:  # noqa: N802 - 基类命名
            path = self.path.split("?", 1)[0]
            if path in ("/health", "/__health"):
                self._json(200, {"ok": True, "kind": "fake-onebot"})
                return
            if path == "/__state":
                with state.lock:
                    self._json(200, {
                        "ok": True,
                        "calls": len(state.calls),
                        "sent": len(state.sent),
                        "injected": len(state.injected),
                        "groups": [g["group_id"] for g in state.groups],
                        "last_calls": [c["action"] for c in state.calls[-10:]],
                    })
                return
            self._json(404, {"ok": False, "error": "not found", "path": path})

        def do_POST(self) -> None:  # noqa: N802 - 基类命名
            path = self.path.split("?", 1)[0].strip("/")
            if path == "__inject":
                self._inject()
                return
            if not self._authorized():
                self._json(401, {"status": "failed", "retcode": 401, "message": "invalid token"})
                return
            payload = self._body()
            state.record_call(path, payload)
            handler = getattr(self, "_action_" + path.replace(".", "_"), None)
            if handler is None:
                self._fail(1404, "unsupported action: " + path)
                return
            handler(payload)

        # ---- 注入：剧本把一条群消息推进被测实例 ----
        def _inject(self) -> None:
            payload = self._body()
            raw_event = payload.get("event")
            if isinstance(raw_event, dict):
                event = dict(raw_event)
            else:
                event = build_event(
                    str(payload.get("text") or ""),
                    group_id=payload.get("group_id", 10001),
                    user_id=int(payload.get("user_id", 2001)),
                    sender=str(payload.get("sender") or "辅导员"),
                    message_id=payload.get("message_id"),
                    timestamp=payload.get("timestamp"),
                )
            event.setdefault("post_type", "message")
            event.setdefault("message_type", "group")
            target_url = str(payload.get("target_url") or state.target_url or "")
            target_token = str(payload.get("target_token") or state.target_token or "")
            with state.lock:
                state.injected.append({"event": event, "target_url": target_url, "at": time.time()})
            # 同时进历史，get_group_msg_history 能取到（回溯资源）
            state.add_history(event)
            if not target_url:
                # 没有 target_url 时事件一条都没送到被测实例：必须算失败，
                # 否则「没转发」也能让剧本变绿（验工实测过这个假阳性）。
                self._json(200, {"ok": False, "forwarded": False, "reason": "没有 target_url，事件没有转发给被测实例"})
                return
            request = urllib.request.Request(
                target_url,
                data=json.dumps(event, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    **({"Authorization": "Bearer " + target_token} if target_token else {}),
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    text = response.read().decode("utf-8", errors="replace")
                    self._json(200, {"ok": True, "forwarded": True, "status": response.status, "body": text[:2000]})
            except urllib.error.HTTPError as error:
                text = error.read().decode("utf-8", errors="replace")
                self._json(200, {"ok": True, "forwarded": True, "status": error.code, "body": text[:2000]})
            except Exception as error:  # noqa: BLE001 - 剧本要看原因
                self._json(200, {"ok": True, "forwarded": False, "error": "%s: %s" % (type(error).__name__, error)})

        # ---- OneBot v11 actions ----
        def _action_get_login_info(self, payload: dict[str, Any]) -> None:
            self._ok(dict(LOGIN))

        def _action_get_status(self, payload: dict[str, Any]) -> None:
            self._ok({"online": True, "good": True})

        def _action_get_version_info(self, payload: dict[str, Any]) -> None:
            self._ok({"app_name": "fake-onebot", "app_version": "e2e", "protocol_version": "v11"})

        def _action_get_group_list(self, payload: dict[str, Any]) -> None:
            self._ok([dict(item) for item in state.groups])

        def _action_get_group_info(self, payload: dict[str, Any]) -> None:
            group_id = str(payload.get("group_id") or "")
            for item in state.groups:
                if str(item["group_id"]) == group_id:
                    self._ok(dict(item))
                    return
            self._fail(1404, "group not found: " + group_id)

        def _action_get_group_member_list(self, payload: dict[str, Any]) -> None:
            group_id = int(payload.get("group_id") or 0)
            self._ok([dict(row) for row in state.members.get(group_id, [])])

        def _action_send_group_msg(self, payload: dict[str, Any]) -> None:
            message_id = state.take_message_id()
            with state.lock:
                state.sent.append({"message_id": message_id, "payload": payload, "at": time.time()})
            self._ok({"message_id": message_id})

        def _action_get_msg(self, payload: dict[str, Any]) -> None:
            wanted = str(payload.get("message_id") or "")
            with state.lock:
                for rows in state.history.values():
                    for row in rows:
                        if str(row.get("message_id")) == wanted:
                            self._ok(dict(row))
                            return
            self._fail(1404, "message not found: " + wanted)

        def _action_get_group_msg_history(self, payload: dict[str, Any]) -> None:
            group_id = str(payload.get("group_id") or "")
            count = int(payload.get("count") or 20)
            with state.lock:
                rows = list(state.history.get(group_id, []))
            self._ok({"messages": rows[-count:] if count > 0 else rows})

    return Handler


class OneBotFake:
    """一个可随机端口启动、可注入事件、可优雅停的 OneBot v11 替身。"""

    def __init__(self, token: str = "", target_url: str = "", target_token: str = "") -> None:
        self.state = _State(token, target_url, target_token)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.state))
        self.thread = threading.Thread(target=self.server.serve_forever, name="fake-onebot", daemon=True)

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:%d" % self.port

    def start(self) -> "OneBotFake":
        self.thread.start()
        return self

    def stop(self) -> None:
        try:
            self.server.shutdown()
        finally:
            self.server.server_close()
            self.thread.join(timeout=5)

    def inject(self, text: str, **kwargs: Any) -> dict[str, Any]:
        """便捷方法：POST /__inject（与剧本走同一条路径）。"""
        body = json.dumps({"text": text, **kwargs}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/__inject", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))


def start(token: str = "", target_url: str = "", target_token: str = "") -> dict[str, Any]:
    """统一入口：起服务并返回 handle 字典（与 fakes/__init__.start 的契约一致）。"""
    fake = OneBotFake(token=token, target_url=target_url, target_token=target_token).start()
    return {
        "name": "onebot",
        "kind": "http",
        "port": fake.port,
        "base_url": fake.base_url,
        "env": {"QQ_DIGEST_NAPCAT_API_URL": fake.base_url},
        "info": {"token": token, "target_url": target_url},
        "impl": fake,
        "stop": fake.stop,
    }
