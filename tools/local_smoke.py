#!/usr/bin/env python3
"""手工冒烟：不碰 QQ、不出网，把「收消息 → 去重 → 摘要 → 推送 → 投递记录」走一遍。

用法就一条命令，失败返回 1、成功返回 0：
    python tools/local_smoke.py
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from noticehub.config import Settings  # noqa: E402
from noticehub.service import DigestService  # noqa: E402
from noticehub.timeutil import iso, now_local  # noqa: E402

GROUP_ID = "123456"
TOKEN = "smoke-token"
# 本地假 webhook 收到的推送正文，按到达顺序堆在这里。
hits: list[dict] = []


class _Sink(BaseHTTPRequestHandler):
    """兜底推送目标：只收不处理，把 JSON 记进 hits，永远回 200。"""

    def log_message(self, *args):  # noqa: D102, ANN002 - 静默，别污染冒烟输出
        return

    def do_POST(self) -> None:  # noqa: N802 - 基类命名
        size = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(size).decode("utf-8", errors="replace")
        try:
            hits.append(json.loads(body))
        except json.JSONDecodeError:
            hits.append({"raw": body})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


def send(url: str, payload: dict, *, token: str = "") -> tuple[int, dict]:
    """POST 一段 JSON，返回 (状态码, 解析后的响应体)。"""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read().decode("utf-8") or "{}")


def group_message(message_id: int, text: str, sender: str = "辅导员") -> dict:
    """拼一条最小可用的 OneBot 群消息事件。"""
    return {
        "post_type": "message",
        "message_type": "group",
        "message_id": message_id,
        "group_id": int(GROUP_ID),
        "user_id": 999,
        "self_id": 111,
        "time": int(time.time()),
        "sender": {"card": sender, "nickname": sender},
        "message": [{"type": "text", "data": {"text": text}}],
    }


def late_notice() -> dict:
    """补录一条 11 分钟前的普通通知，用它逼出「窗口合并」那次推送。"""
    stamp = iso(now_local() - dt.timedelta(minutes=11))
    return {
        "msg_id": "smoke-window-1",
        "source": "onebot",
        "event": "GROUP_MESSAGE_CREATE",
        "group_id": GROUP_ID,
        "group_name": "学院通知群",
        "sender_id": "999",
        "sender_name": "学委",
        "ts": stamp,
        "received_at": stamp,
        "content": "【学院通知】关于2026年国庆节放假安排的通知",
    }


def pump(service: DigestService, want: int, seconds: float = 30.0) -> None:
    """手动推 tick，直到攒够 want 条推送或者超时。"""
    deadline = time.time() + seconds
    while time.time() < deadline and len(hits) < want:
        service.tick()
        time.sleep(0.5)


def body_of(item: dict) -> str:
    return str(item.get("content") or "")


class Checks:
    """攒失败原因，末尾统一打印；顺序与问题出现顺序一致。"""

    def __init__(self) -> None:
        self.reasons: list[str] = []

    def __bool__(self) -> bool:
        return bool(self.reasons)

    def need(self, ok: bool, reason: str) -> None:
        if not ok:
            self.reasons.append(reason)


def main() -> int:
    sink = ThreadingHTTPServer(("127.0.0.1", 0), _Sink)
    threading.Thread(target=sink.serve_forever, daemon=True).start()
    sink_url = f"http://127.0.0.1:{sink.server_address[1]}/hook"

    tmp = tempfile.TemporaryDirectory(prefix="qq-digest-smoke-")
    root = Path(tmp.name)
    settings = Settings(
        group_whitelist=(GROUP_ID,),
        group_aliases={GROUP_ID: "学院通知群"},
        webhook_urls=(sink_url,),
        window_minutes=1,
        poll_seconds=5,
        min_score=3,
        delivery_retry_seconds=0,
        onebot_enabled=True,
        onebot_host="127.0.0.1",
        onebot_port=0,
        onebot_token=TOKEN,
        data_dir=root / "data",
        log_dir=root / "logs",
    )
    service = DigestService(settings)
    service.start()
    assert service.receiver.server is not None
    endpoint = f"http://127.0.0.1:{service.receiver.server.server_address[1]}/onebot/event"

    checks = Checks()
    try:
        # 1) 带鉴权的闲聊：能入库，但不该推给任何人
        status, body = send(endpoint, group_message(1001, "哈哈哈哈", sender="同学A"), token=TOKEN)
        print(f"[1] 闲聊上报 status={status} inserted={body.get('inserted')}")
        checks.need(status == 200, "闲聊消息上报失败")

        # 2) 不带令牌的请求必须被挡下
        try:
            send(endpoint, group_message(1002, "紧急通知：请立即提交材料"))
            checks.need(False, "无 token 请求未被拒绝")
        except urllib.error.HTTPError as error:
            print(f"[2] 无 token 请求被拒绝 status={error.code}")
            checks.need(error.code == 403, f"鉴权返回码异常：{error.code}")

        # 3) 紧急通知：不等合并窗口，立刻推
        status, body = send(endpoint, group_message(1003, "请各班班长今天18:00前提交材料"), token=TOKEN)
        print(f"[3] 紧急通知上报 status={status} inserted={body.get('inserted')}")

        # 4) 普通通知补录成 11 分钟前，触发按窗口合并的那次推送
        service.on_message(late_notice())

        pump(service, want=2)

        print(f"[5] 收到推送 {len(hits)} 条")
        for index, item in enumerate(hits, start=1):
            content = body_of(item)
            print(f"    推送{index}: {content.splitlines()[0] if content else item}")
            checks.need("哈哈哈哈" not in content, "闲聊内容被推送")
        checks.need(len(hits) >= 2, "未收到预期的 2 条推送（紧急 + 窗口）")
        checks.need(any("提交材料" in body_of(item) for item in hits), "紧急通知未推送")
        checks.need(any("国庆节" in body_of(item) for item in hits), "窗口摘要未推送")

        counts = service.store.counts()
        print(f"[6] 统计：{counts}")
        checks.need(counts["deliveries_sent"] >= 2, "投递记录不足 2 条")
    finally:
        service.stop()
        sink.shutdown()
        sink.server_close()
        tmp.cleanup()

    if checks:
        print("\n冒烟失败：")
        for reason in checks.reasons:
            print(f"  - {reason}")
        return 1
    print("\n冒烟通过：消息接收 -> 摘要 -> 推送 -> 去重记录 全链路正常。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
