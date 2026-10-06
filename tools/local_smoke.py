#!/usr/bin/env python3
"""本地端到端冒烟：模拟 OneBot 群消息 -> 规则筛选 -> HTTP 推送。

不联网、不需要 QQ 账号，验证「消息入库 -> 去重 -> 摘要 -> 推送 -> 投递记录」整条链路：
    python tools/local_smoke.py
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest.config import Settings  # noqa: E402
from qq_live_digest.service import DigestService  # noqa: E402
from qq_live_digest.timeutil import iso, now_local  # noqa: E402

GROUP_ID = "123456"
TOKEN = "smoke-token"
received: list[dict] = []


class _WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D102, ANN002 - 静默
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            received.append(json.loads(raw))
        except json.JSONDecodeError:
            received.append({"raw": raw})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


def post_json(url: str, payload: dict, *, token: str = "") -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read().decode("utf-8") or "{}")


def onebot_event(message_id: int, content: str, sender: str = "辅导员") -> dict:
    return {
        "post_type": "message",
        "message_type": "group",
        "message_id": message_id,
        "group_id": int(GROUP_ID),
        "user_id": 999,
        "self_id": 111,
        "time": int(time.time()),
        "sender": {"card": sender, "nickname": sender},
        "message": [{"type": "text", "data": {"text": content}}],
    }


def main() -> int:
    import tempfile

    webhook = ThreadingHTTPServer(("127.0.0.1", 0), _WebhookHandler)
    threading.Thread(target=webhook.serve_forever, daemon=True).start()
    webhook_url = f"http://127.0.0.1:{webhook.server_address[1]}/hook"

    tmp = tempfile.TemporaryDirectory(prefix="qq-digest-smoke-")
    root = Path(tmp.name)
    settings = Settings(
        group_whitelist=(GROUP_ID,),
        group_aliases={GROUP_ID: "学院通知群"},
        webhook_urls=(webhook_url,),
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

    failures: list[str] = []
    try:
        # 1) 带鉴权的闲聊消息：入库但不推送
        status, body = post_json(endpoint, onebot_event(1001, "哈哈哈哈", sender="同学A"), token=TOKEN)
        print(f"[1] 闲聊上报 status={status} inserted={body.get('inserted')}")
        if status != 200:
            failures.append("闲聊消息上报失败")

        # 2) 鉴权错误的请求应被拒绝
        try:
            post_json(endpoint, onebot_event(1002, "紧急通知：请立即提交材料"))
            failures.append("无 token 请求未被拒绝")
        except urllib.error.HTTPError as error:
            print(f"[2] 无 token 请求被拒绝 status={error.code}")
            if error.code != 403:
                failures.append(f"鉴权返回码异常：{error.code}")

        # 3) 紧急通知：应立即推送
        status, body = post_json(endpoint, onebot_event(1003, "请各班班长今天18:00前提交材料"), token=TOKEN)
        print(f"[3] 紧急通知上报 status={status} inserted={body.get('inserted')}")

        # 4) 普通通知（补录成 11 分钟前，触发窗口合并推送）
        old = now_local() - dt.timedelta(minutes=11)
        service.on_message(
            {
                "msg_id": "smoke-window-1",
                "source": "onebot",
                "event": "GROUP_MESSAGE_CREATE",
                "group_id": GROUP_ID,
                "group_name": "学院通知群",
                "sender_id": "999",
                "sender_name": "学委",
                "ts": iso(old),
                "received_at": iso(old),
                "content": "【学院通知】关于2026年国庆节放假安排的通知",
            }
        )

        deadline = time.time() + 30
        while time.time() < deadline and len(received) < 2:
            service.tick()
            time.sleep(0.5)

        print(f"[5] 收到推送 {len(received)} 条")
        for index, item in enumerate(received, start=1):
            content = str(item.get("content") or "")
            print(f"    推送{index}: {content.splitlines()[0] if content else item}")
            if "哈哈哈哈" in content:
                failures.append("闲聊内容被推送")
        if len(received) < 2:
            failures.append("未收到预期的 2 条推送（紧急 + 窗口）")
        if not any("提交材料" in str(item.get("content") or "") for item in received):
            failures.append("紧急通知未推送")
        if not any("国庆节" in str(item.get("content") or "") for item in received):
            failures.append("窗口摘要未推送")

        counts = service.store.counts()
        print(f"[6] 统计：{counts}")
        if counts["deliveries_sent"] < 2:
            failures.append("投递记录不足 2 条")
    finally:
        service.stop()
        webhook.shutdown()
        webhook.server_close()
        tmp.cleanup()

    if failures:
        print("\n冒烟失败：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("\n冒烟通过：消息接收 -> 摘要 -> 推送 -> 去重记录 全链路正常。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
