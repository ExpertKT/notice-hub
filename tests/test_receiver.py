from __future__ import annotations

import hashlib
import hmac

import json
import socket
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest.config import Settings  # noqa: E402
from qq_live_digest.receiver import OneBotReceiver, flatten_message, parse_onebot_event  # noqa: E402

GROUP_EVENT = {
    "post_type": "message",
    "message_type": "group",
    "message_id": 1024,
    "group_id": 123456,
    "user_id": 999,
    "self_id": 111,
    "time": 1789000000,
    "sender": {"card": "辅导员", "nickname": "老师"},
    "message": [
        {"type": "at", "data": {"qq": "all", "name": "全体成员"}},
        {"type": "text", "data": {"text": " 请今天18:00前提交材料 "}},
        {"type": "image", "data": {"url": "http://example.com/a.png"}},
    ],
}


class OneBotParseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(group_whitelist=("123456",), group_aliases={"123456": "学院通知群"})

    def test_flatten_message(self) -> None:
        self.assertIn("@全体成员", flatten_message(GROUP_EVENT["message"]))
        self.assertIn("[图片]", flatten_message(GROUP_EVENT["message"]))
        self.assertEqual(flatten_message("纯文本"), "纯文本")

    def test_parse_group_event(self) -> None:
        record = parse_onebot_event(GROUP_EVENT, self.settings)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["msg_id"], "onebot:123456:1024")
        self.assertEqual(record["group_name"], "学院通知群")
        self.assertEqual(record["sender_name"], "辅导员")
        self.assertIn("18:00", record["content"])

    def test_ignore_non_group_and_self(self) -> None:
        self.assertIsNone(parse_onebot_event({"post_type": "message", "message_type": "private"}, self.settings))
        self.assertIsNone(
            parse_onebot_event({**GROUP_EVENT, "user_id": GROUP_EVENT["self_id"]}, self.settings)
        )
        self.assertIsNone(parse_onebot_event({"post_type": "notice"}, self.settings))

    def test_hash_id_when_message_id_missing(self) -> None:
        payload = {k: v for k, v in GROUP_EVENT.items() if k != "message_id"}
        record = parse_onebot_event(payload, self.settings)
        assert record is not None
        self.assertTrue(record["msg_id"].startswith("onebot:123456:hash-"))

    def test_raw_message_fallback(self) -> None:
        payload = {**GROUP_EVENT, "message": "", "raw_message": "CQ 原始文本"}
        record = parse_onebot_event(payload, self.settings)
        assert record is not None
        self.assertEqual(record["content"], "CQ 原始文本")


class OneBotReceiverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.received: list[dict] = []
        self.settings = Settings(
            group_whitelist=("123456",),
            onebot_enabled=True,
            onebot_host="127.0.0.1",
            onebot_port=0,
            onebot_token="secret-token",
        )
        self.receiver = OneBotReceiver(self.settings, self._on_message)

    def _on_message(self, record: dict) -> bool:
        self.received.append(record)
        return True

    def test_handle_payload_whitelist_and_dedupe(self) -> None:
        status, body = self.receiver.handle_payload(GROUP_EVENT)
        self.assertEqual(status, 200)
        self.assertTrue(body["inserted"])
        self.assertEqual(len(self.received), 1)

        outside = {**GROUP_EVENT, "group_id": 777}
        status, body = self.receiver.handle_payload(outside)
        self.assertEqual(status, 204)
        self.assertTrue(body["ignored"])
        self.assertEqual(len(self.received), 1)

    def test_http_server_auth(self) -> None:
        self.receiver.health_provider = lambda: {
            "catchup_enabled": True,
            "last_catchup_at": "2026-09-29T01:14:56+08:00",
        }
        self.receiver.start()
        self.addCleanup(self.receiver.stop)
        assert self.receiver.server is not None
        port = self.receiver.server.server_address[1]
        url = f"http://127.0.0.1:{port}/onebot/event"
        data = json.dumps(GROUP_EVENT).encode("utf-8")

        request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(error.exception.code, 403)

        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "Authorization": "Bearer secret-token"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(len(self.received), 1)

        health = urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5)
        with health:
            health_payload = json.loads(health.read().decode("utf-8"))
        self.assertTrue(health_payload["ok"])
        self.assertTrue(health_payload["catchup_enabled"])
        self.assertEqual(health_payload["last_catchup_at"], "2026-09-29T01:14:56+08:00")


    def test_http_server_accepts_napcat_signature(self) -> None:
        """NapCat 上报不带 Authorization，只带 x-signature=sha1=<hmac_sha1(token, body)>。"""
        self.receiver.start()
        self.addCleanup(self.receiver.stop)
        assert self.receiver.server is not None
        port = self.receiver.server.server_address[1]
        url = f"http://127.0.0.1:{port}/onebot/event"
        data = json.dumps(GROUP_EVENT).encode("utf-8")
        digest = hmac.new(b"secret-token", data, hashlib.sha1).hexdigest()

        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "X-Signature": f"sha1={digest}"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["inserted"])
        self.assertEqual(len(self.received), 1)

        bad = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "X-Signature": "sha1=" + "0" * 40},
        )
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(bad, timeout=5)
        self.assertEqual(error.exception.code, 403)

    def test_empty_token_keeps_working_but_warns_once(self) -> None:
        """红队 S18 的收敛口径：环回 + 空令牌仍放行（否则 NapCat 的真消息会被静默拒收），
        但启动时必须 warning 一次，把「接口无鉴权」记为已知残留风险。"""
        settings = Settings(
            group_whitelist=("123456",), onebot_host="127.0.0.1", onebot_port=0, onebot_token=""
        )
        receiver = OneBotReceiver(settings, self._on_message)
        with self.assertLogs("qq_live_digest.receiver", level="WARNING") as logs:
            self.assertTrue(receiver.start())
        self.addCleanup(receiver.stop)
        self.assertIn("未设置访问令牌", "\n".join(logs.output))
        assert receiver.server is not None
        port = receiver.server.server_address[1]
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/onebot/event",
            data=json.dumps(GROUP_EVENT).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertTrue(json.loads(response.read().decode("utf-8"))["inserted"])
        self.assertEqual(len(self.received), 1)

    def test_non_loopback_without_token_refuses_to_start(self) -> None:
        settings = Settings(group_whitelist=("123456",), onebot_host="0.0.0.0", onebot_token="")
        receiver = OneBotReceiver(settings, self._on_message)
        with self.assertLogs("qq_live_digest.receiver", level="ERROR") as logs:
            self.assertFalse(receiver.start())
        self.assertIsNone(receiver.server)
        self.assertIn("QQ_DIGEST_ONEBOT_TOKEN", "\n".join(logs.output))

    def test_token_comparison_is_constant_time(self) -> None:
        """S19：令牌比较必须走 hmac.compare_digest（长度不等也不能短路成普通 ==）。"""
        import inspect

        from qq_live_digest import receiver as receiver_module

        source = inspect.getsource(receiver_module._Handler._authorized)
        # access_token 查询串、Authorization 头、X-Signature 三处都必须走常量时间比较
        self.assertGreaterEqual(source.count("hmac.compare_digest("), 2)
        self.assertNotIn("candidate == token", source)
        self.assertNotIn("token in params", source)

    def test_http_server_accepts_chunked_report(self) -> None:
        """NapCat may send reports with Transfer-Encoding: chunked."""
        self.receiver.start()
        self.addCleanup(self.receiver.stop)
        assert self.receiver.server is not None
        port = self.receiver.server.server_address[1]
        body = json.dumps(GROUP_EVENT, ensure_ascii=False).encode("utf-8")
        digest = hmac.new(b"secret-token", body, hashlib.sha1).hexdigest()
        chunked = b"%x\r\n%s\r\n0\r\n\r\n" % (len(body), body)
        request = (
            b"POST /onebot/event HTTP/1.1\r\n"
            + f"Host: 127.0.0.1:{port}\r\n".encode("ascii")
            + b"Content-Type: application/json\r\n"
            + b"Transfer-Encoding: chunked\r\n"
            + f"X-Signature: sha1={digest}\r\n".encode("ascii")
            + b"Connection: close\r\n\r\n"
            + chunked
        )
        with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
            connection.sendall(request)
            response = b""
            while True:
                part = connection.recv(4096)
                if not part:
                    break
                response += part
        header, _, payload = response.partition(b"\r\n\r\n")
        self.assertIn(b" 200 ", header.split(b"\r\n", 1)[0])
        parsed = json.loads(payload.decode("utf-8"))
        self.assertTrue(parsed["inserted"])
        self.assertEqual(len(self.received), 1)


class NapcatTokenContractTest(unittest.TestCase):
    """对端配合的契约：NapCat 写进配置的 httpClients.token 必须就是接收器校验的令牌。

    没有这条，收端单方面收紧就会变成「服务健康、群消息全丢」——所以 S18 收敛成
    「环回空令牌照旧放行 + warning」，这里锁住的是「配了令牌就一定两边一致」。
    """

    def test_napcat_client_token_is_the_receiver_token(self) -> None:
        from qq_live_digest import napcat_admin

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                onebot_token="tok-123",
                onebot_port=8765,
                napcat_api_url="http://127.0.0.1:3001",
            )
            result = napcat_admin.ensure_onebot_config(settings, engine=Path(tmp))
            self.assertTrue(result["ok"], result)
            config = json.loads((Path(tmp) / "config" / "onebot11.json").read_text(encoding="utf-8"))
        clients = config["network"]["httpClients"]
        self.assertEqual(clients[0]["token"], settings.onebot_token)
        self.assertEqual(clients[0]["url"], "http://127.0.0.1:8765")


if __name__ == "__main__":
    unittest.main()
