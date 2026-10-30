"""W3 平台替身：推送收包端（假 WxPusher / ServerChan / PushPlus / 通用 webhook）。

剧本要断言「推送真的发出去了」，所以这里做的是**真的 HTTP 收包端**，
把每次请求的 method/path/query/header/body 原样记下来，写进 `<run>/push_sink/requests.json`，
并暴露 `GET /__requests?limit=N` 与 `GET /__state` 供剧本取。

形状参照 `tests/test_push.py:403-421` 的 `_stub_webhook_handler`（那是单元测试里的
一次性收端）；这里补上「可独立启动 + 落盘 + 取数接口 + 三种应答」。

应答模式（`mode=`）
-------------------
* `ok`      —— 200 + `{}`（和真机器人一致：业务错误也走 200）
* `fail`    —— 500 + `{"ok": false}`（验证产品的失败重试/失败计数）
* `slow`    —— 先睡 `delay` 秒再回 200（验证超时与不阻塞）

随机端口启动，避开 8765/8766/8767/8768 与 3000/5000/8000；`stop()` 关端口并释放。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ._rundir import default_run_dir

MAX_RECORDS = 500


class _Recorder:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.items: list[dict[str, Any]] = []

    def add(self, record: dict[str, Any]) -> None:
        with self.lock:
            self.items.append(record)
            del self.items[:-MAX_RECORDS]
            try:
                self.path.write_text(
                    json.dumps(self.items, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except OSError:
                pass  # 落盘失败不影响收包；内存里仍在

    def snapshot(self, limit: int = 0) -> list[dict[str, Any]]:
        with self.lock:
            rows = list(self.items)
        return rows[-limit:] if limit > 0 else rows

    def clear(self) -> None:
        with self.lock:
            self.items.clear()
            try:
                self.path.write_text("[]", encoding="utf-8")
            except OSError:
                pass


def _make_handler(recorder: _Recorder, mode: str, delay: float, response: dict[str, Any]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "nh-fake-push/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002 - 基类签名
            return

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass

        def _capture(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            text = raw.decode("utf-8", errors="replace")
            try:
                body: Any = json.loads(text) if text else None
            except json.JSONDecodeError:
                body = text
            recorder.add({
                "method": method,
                "path": self.path,
                "headers": {key: value for key, value in self.headers.items()},
                "body": body,
                "raw_body": text if isinstance(body, str) else None,
                "at": time.time(),
            })

        def _respond(self) -> None:
            if mode == "slow":
                time.sleep(max(0.0, delay))
            if mode == "fail":
                self._json(500, {"ok": False, "error": "fake push sink failure"})
                return
            self._json(200, dict(response))

        def do_GET(self) -> None:  # noqa: N802 - 基类命名
            path = self.path.split("?", 1)[0]
            if path == "/__requests":
                limit = 0
                if "?" in self.path:
                    for part in self.path.split("?", 1)[1].split("&"):
                        if part.startswith("limit="):
                            try:
                                limit = int(part.split("=", 1)[1])
                            except ValueError:
                                limit = 0
                rows = recorder.snapshot(limit)
                self._json(200, {"ok": True, "count": len(rows), "items": rows})
                return
            if path == "/__state":
                self._json(200, {"ok": True, "count": len(recorder.items), "mode": mode,
                                 "file": str(recorder.path)})
                return
            self._json(404, {"ok": False, "error": "not found", "path": path})

        def do_POST(self) -> None:  # noqa: N802 - 基类命名
            path = self.path.split("?", 1)[0]
            if path == "/__clear":
                recorder.clear()
                self._json(200, {"ok": True, "count": 0})
                return
            self._capture("POST")
            self._respond()

        def do_PUT(self) -> None:  # noqa: N802 - 基类命名
            self._capture("PUT")
            self._respond()

    return Handler


class PushSink:
    def __init__(
        self,
        *,
        mode: str = "ok",
        delay: float = 1.5,
        response: dict[str, Any] | None = None,
        root: Path | str | None = None,
        run_id: str | None = None,
    ) -> None:
        if mode not in {"ok", "fail", "slow"}:
            raise ValueError("mode 只支持 ok / fail / slow，拿到 %r" % (mode,))
        self.mode = mode
        self.delay = delay
        self._created_run_dir = True
        self.root = default_run_dir("push_sink", run_id) if root is None else Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.requests_file = self.root / "requests.json"
        if not self.requests_file.exists():
            self.requests_file.write_text("[]", encoding="utf-8")
        self.recorder = _Recorder(self.requests_file)
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), _make_handler(self.recorder, mode, delay, response or {})
        )
        self.thread = threading.Thread(target=self.server.serve_forever, name="fake-push", daemon=True)

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:%d" % self.port

    def start(self) -> "PushSink":
        self.thread.start()
        return self

    def requests(self, limit: int = 0) -> list[dict[str, Any]]:
        return self.recorder.snapshot(limit)

    def count(self) -> int:
        return len(self.recorder.snapshot())

    def stop(self) -> None:
        try:
            self.server.shutdown()
        finally:
            self.server.server_close()
            self.thread.join(timeout=5)
            if self._created_run_dir:
                # 收包记录留给剧本/报告看；若调用方要清，用 remove_tree()
                pass

    def remove_tree(self) -> None:
        import shutil

        if self._created_run_dir:
            shutil.rmtree(self.root, ignore_errors=True)
            try:
                self.root.parent.rmdir()
            except OSError:
                pass


def start(
    mode: str = "ok",
    delay: float = 1.5,
    response: dict[str, Any] | None = None,
    root: Path | str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """统一入口：起收包端并返回 handle 字典。"""
    fake = PushSink(mode=mode, delay=delay, response=response, root=root, run_id=run_id).start()
    return {
        "name": "push",
        "kind": "http",
        "port": fake.port,
        "base_url": fake.base_url,
        "env": {"QQ_DIGEST_WEBHOOK_URL": fake.base_url + "/hook"},
        "info": {
            "mode": mode,
            "requests_file": str(fake.requests_file),
            "webhook_url": fake.base_url + "/hook",
        },
        "impl": fake,
        "stop": fake.stop,
    }
