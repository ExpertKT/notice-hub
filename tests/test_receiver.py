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

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from noticehub.config import Settings  # noqa: E402
from noticehub.receiver import OneBotReceiver, flatten_message, parse_onebot_event  # noqa: E402

JSON = "application/json"
GROUP = "123456"


def _segments() -> list[dict]:
    """正文三段：@全体成员、一句话、一张图。"""
    return [
        {"type": "at", "data": {"qq": "all", "name": "全体成员"}},
        {"type": "text", "data": {"text": " 请今天18:00前提交材料 "}},
        {"type": "image", "data": {"url": "http://example.com/a.png"}},
    ]


def group_event(**overrides) -> dict:
    """一条完整的群上报事件；用例只覆盖需要区分的那几个字段。"""
    event = dict(
        post_type="message", message_type="group", message_id=1024,
        group_id=123456, user_id=999, self_id=111, time=1789000000,
        sender={"card": "辅导员", "nickname": "老师"}, message=_segments(),
    )
    event.update(overrides)
    return event


def parse_settings(**overrides) -> Settings:
    """解析类用例的默认配置：白名单一个群，并给它一个人看的别名。"""
    base = {"group_whitelist": (GROUP,), "group_aliases": {GROUP: "学院通知群"}}
    base.update(overrides)
    return Settings(**base)


def receiver_settings(**overrides) -> Settings:
    """真起 HTTP 的用例：环回地址、端口交给系统分配、带令牌。"""
    base = {
        "group_whitelist": (GROUP,),
        "onebot_enabled": True,
        "onebot_host": "127.0.0.1",
        "onebot_port": 0,
        "onebot_token": "secret-token",
    }
    base.update(overrides)
    return Settings(**base)


def sign(body: bytes, token: bytes = b"secret-token") -> str:
    """NapCat 那套签名：x-signature=sha1=<hmac_sha1(token, body)>。"""
    return hmac.new(token, body, hashlib.sha1).hexdigest()


def post_json(url: str, body: bytes, headers: dict | None = None) -> dict:
    """发一个上报请求，把响应体解成 dict。"""
    request = urllib.request.Request(url, data=body, headers={"Content-Type": JSON, **(headers or {})})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def read_to_eof(connection: socket.socket) -> bytes:
    """把一个原始 socket 读到对端关闭；用来验手写的 HTTP 报文。"""
    chunks = []
    while True:
        part = connection.recv(4096)
        if not part:
            return b"".join(chunks)
        chunks.append(part)


class OneBotParseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = parse_settings()

    def test_flatten_message(self) -> None:
        self.assertIn("@全体成员", flatten_message(group_event()["message"]))
        self.assertIn("[图片]", flatten_message(group_event()["message"]))
        self.assertEqual(flatten_message("纯文本"), "纯文本")

    def test_raw_message_fallback(self) -> None:
        payload = {**group_event(), "message": "", "raw_message": "CQ 原始文本"}
        record = parse_onebot_event(payload, self.settings)
        assert record is not None
        self.assertEqual(record["content"], "CQ 原始文本")

    def test_hash_id_when_message_id_missing(self) -> None:
        # 没有 message_id 时用内容算一个稳定 id，同一条消息去重才有效。
        payload = {key: value for key, value in group_event().items() if key != "message_id"}
        record = parse_onebot_event(payload, self.settings)
        assert record is not None
        self.assertTrue(record["msg_id"].startswith("onebot:123456:hash-"))

    def test_ignore_non_group_and_self(self) -> None:
        self.assertIsNone(
            parse_onebot_event({"post_type": "message", "message_type": "private"}, self.settings)
        )
        event = group_event()
        self.assertIsNone(parse_onebot_event({**event, "user_id": event["self_id"]}, self.settings))
        self.assertIsNone(parse_onebot_event({"post_type": "notice"}, self.settings))

    def test_parse_group_event(self) -> None:
        record = parse_onebot_event(group_event(), self.settings)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["msg_id"], "onebot:123456:1024")
        self.assertEqual(record["group_name"], "学院通知群")
        self.assertEqual(record["sender_name"], "辅导员")
        self.assertIn("18:00", record["content"])


class OneBotReceiverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.received: list[dict] = []
        self.settings = receiver_settings()
        self.receiver = OneBotReceiver(self.settings, self._on_message)

    def _on_message(self, record: dict) -> bool:
        self.received.append(record)
        return True

    def _start(self) -> None:
        """起接收器（端口 0 由系统分配）并登记清理；端口从 self.receiver.server 取。"""
        self.receiver.start()
        self.addCleanup(self.receiver.stop)

    def _endpoints(self) -> tuple[str, str]:
        """把服务起起来，返回 (上报告警地址, 健康检查地址)。"""
        self._start()
        assert self.receiver.server is not None
        port = self.receiver.server.server_address[1]
        return f"http://127.0.0.1:{port}/onebot/event", f"http://127.0.0.1:{port}/health"

    @staticmethod
    def _health() -> dict:
        """健康检查要回的字段：补采开关与上次补采时间。"""
        return {"catchup_enabled": True, "last_catchup_at": "2026-09-29T01:14:56+08:00"}

    @staticmethod
    def _get_json(url: str) -> dict:
        with urllib.request.urlopen(url, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_handle_payload_whitelist_and_dedupe(self) -> None:
        status, body = self.receiver.handle_payload(group_event())
        self.assertEqual(status, 200)
        self.assertTrue(body["inserted"])
        self.assertEqual(len(self.received), 1)

        # 白名单外的群要被静默丢掉，且不能进 received。
        outside = group_event(group_id=777)
        status, body = self.receiver.handle_payload(outside)
        self.assertEqual(status, 204)
        self.assertTrue(body["ignored"])
        self.assertEqual(len(self.received), 1)

    def test_http_server_auth(self) -> None:
        self.receiver.health_provider = self._health
        url, health_url = self._endpoints()
        data = json.dumps(group_event()).encode("utf-8")

        # 不带令牌必须 403。
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(
                urllib.request.Request(url, data=data, headers={"Content-Type": JSON}), timeout=5)
        self.assertEqual(error.exception.code, 403)

        payload = post_json(url, data, {"Authorization": "Bearer secret-token"})
        self.assertTrue(payload["ok"])
        self.assertEqual(len(self.received), 1)

        health_payload = self._get_json(health_url)
        self.assertTrue(health_payload["ok"])
        self.assertTrue(health_payload["catchup_enabled"])
        self.assertEqual(health_payload["last_catchup_at"], "2026-09-29T01:14:56+08:00")

    def test_health_reports_failure_when_the_provider_breaks(self) -> None:
        """健康提供方坏了不能还报健康：页面会显示在线，而消息其实进不来。"""

        def broken() -> dict:
            raise RuntimeError("database gone")

        self.receiver.health_provider = broken
        _, health_url = self._endpoints()
        payload = self._get_json(health_url)
        self.assertFalse(payload["ok"])
        self.assertIn("健康检查失败", payload["error"])

    def test_http_server_accepts_napcat_signature(self) -> None:
        """NapCat 上报不带 Authorization，只带 x-signature=sha1=<hmac_sha1(token, body)>。"""
        url, _ = self._endpoints()
        data = json.dumps(group_event()).encode("utf-8")
        digest = sign(data)

        payload = post_json(url, data, {"X-Signature": f"sha1={digest}"})
        self.assertTrue(payload["inserted"])
        self.assertEqual(len(self.received), 1)

        # 签名不对就一律 403，不能因为带了个头就放行。
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(
                urllib.request.Request(
                    url, data=data, headers={"Content-Type": JSON, "X-Signature": "sha1=" + "0" * 40}),
                timeout=5)
        self.assertEqual(error.exception.code, 403)

    def test_empty_token_keeps_working_but_warns_once(self) -> None:
        """红队 S18 的收敛口径：环回 + 空令牌仍放行（否则 NapCat 的真消息会被静默拒收），
        但启动时必须 warning 一次，把「接口无鉴权」记为已知残留风险。"""
        settings = parse_settings(onebot_host="127.0.0.1", onebot_port=0, onebot_token="")
        receiver = OneBotReceiver(settings, self._on_message)
        with self.assertLogs("noticehub.receiver", level="WARNING") as logs:
            self.assertTrue(receiver.start())
        self.addCleanup(receiver.stop)
        self.assertIn("未设置访问令牌", "\n".join(logs.output))
        assert receiver.server is not None
        port = receiver.server.server_address[1]
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/onebot/event",
            data=json.dumps(group_event()).encode("utf-8"),
            headers={"Content-Type": JSON},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertTrue(json.loads(response.read().decode("utf-8"))["inserted"])
        self.assertEqual(len(self.received), 1)

    def test_non_loopback_without_token_refuses_to_start(self) -> None:
        settings = parse_settings(onebot_host="0.0.0.0", onebot_token="")
        receiver = OneBotReceiver(settings, self._on_message)
        with self.assertLogs("noticehub.receiver", level="ERROR") as logs:
            self.assertFalse(receiver.start())
        self.assertIsNone(receiver.server)
        self.assertIn("QQ_DIGEST_ONEBOT_TOKEN", "\n".join(logs.output))

    def test_token_comparison_is_constant_time(self) -> None:
        """S19：令牌比较必须走 hmac.compare_digest（长度不等也不能短路成普通 ==）。"""
        import inspect

        from noticehub import receiver as receiver_module

        source = inspect.getsource(receiver_module._Handler._authorized)
        # access_token 查询串、Authorization 头、X-Signature 三处都必须走常量时间比较
        self.assertGreaterEqual(source.count("hmac.compare_digest("), 2)
        self.assertNotIn("candidate == token", source)
        self.assertNotIn("token in params", source)

    def test_http_server_accepts_chunked_report(self) -> None:
        """NapCat may send reports with Transfer-Encoding: chunked."""
        url, _ = self._endpoints()
        port = int(url.rsplit(":", 1)[1].split("/", 1)[0])
        body = json.dumps(group_event(), ensure_ascii=False).encode("utf-8")
        digest = sign(body)
        chunked = b"%x\r\n%s\r\n0\r\n\r\n" % (len(body), body)
        # 手写 HTTP 报文：urllib 不会替我们发 chunked，只能自己拼。
        head = [
            b"POST /onebot/event HTTP/1.1",
            f"Host: 127.0.0.1:{port}".encode("ascii"),
            b"Content-Type: application/json",
            b"Transfer-Encoding: chunked",
            f"X-Signature: sha1={digest}".encode("ascii"),
            b"Connection: close",
        ]
        request = b"\r\n".join(head) + b"\r\n\r\n" + chunked
        with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
            connection.sendall(request)
            response = read_to_eof(connection)
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
        from noticehub import napcat_admin

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
