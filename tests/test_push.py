from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import http.server
import json
import sys
import tempfile
import threading
import unittest
import urllib.parse
from unittest import mock
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest.config import Settings  # noqa: E402
from qq_live_digest.push import (  # noqa: E402
    PushError,
    PushManager,
    Pusher,
    WebhookPusher,
    WxPusherPusher,
    build_pushers,
    chunk_text,
)
from qq_live_digest.store import Store  # noqa: E402
from qq_live_digest.summarizer import Digest, build_digest  # noqa: E402
from qq_live_digest.timeutil import iso  # noqa: E402

NOW = dt.datetime(2026, 9, 28, 18, 0, 0)


class FakePusher(Pusher):
    def __init__(self, name: str, *, tier: int = 0, fail_times: int = 0) -> None:
        super().__init__(target=f"{name}-target")
        self.name = name
        self.tier = tier
        self.fail_times = fail_times
        self.calls: list[tuple[str, str]] = []

    def send(self, title: str, body: str, *, summary: str = "", html: str = "") -> None:
        self.calls.append((title, body))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise PushError("模拟失败")


def record(msg_id: str, content: str) -> dict:
    return {
        "msg_id": msg_id,
        "source": "qqbot",
        "group_id": "g1",
        "group_name": "学院通知群",
        "sender_id": "u1",
        "sender_name": "辅导员",
        "ts": iso(NOW - dt.timedelta(minutes=5)),
        "received_at": iso(NOW - dt.timedelta(minutes=5)),
        "content": content,
    }


class ChunkTextTest(unittest.TestCase):
    def test_short_text_single_chunk(self) -> None:
        self.assertEqual(chunk_text("你好", 100), ["你好"])
        self.assertEqual(chunk_text("   ", 100), [])

    def test_long_text_is_chunked_without_loss(self) -> None:
        text = "\n".join(f"第{i}行内容" + "x" * 40 for i in range(60))
        chunks = chunk_text(text, 300)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 320)
        rebuilt = "\n".join(chunk.split("\n", 1)[1] for chunk in chunks)
        self.assertEqual(rebuilt.replace("\n", ""), text.replace("\n", ""))

    def test_single_huge_line_is_hard_split(self) -> None:
        chunks = chunk_text("y" * 1000, 100)
        self.assertGreater(len(chunks), 1)
        joined = "".join(chunk.split("\n", 1)[1] for chunk in chunks)
        self.assertEqual(joined, "y" * 1000)


class PushManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "push.sqlite3")
        self.settings = Settings(
            group_whitelist=("g1",),
            delivery_max_attempts=3,
            delivery_retry_seconds=0,
        )

    def _digest(self, content: str = "请各班班长今天18:00前提交材料") -> Digest:
        digest = build_digest(self.settings, [record("m1", content)], now=NOW)
        digest.id = self.store.insert_digest(
            kind=digest.kind,
            window_start=iso(digest.window_start),
            window_end=iso(digest.window_end),
            body=digest.body,
            payload=digest.payload(),
            message_count=digest.message_count,
            llm_used=digest.llm_used,
        )
        return digest

    def test_primary_success_skips_fallback(self) -> None:
        primary = FakePusher("qq-bot", tier=0)
        fallback = FakePusher("wxpusher", tier=1)
        manager = PushManager(self.store, self.settings, [primary, fallback])
        outcomes = manager.send_digest(self._digest())
        self.assertEqual(len(primary.calls), 1)
        self.assertEqual(fallback.calls, [])
        self.assertTrue(any(item.ok and not item.skipped for item in outcomes))
        counts = self.store.counts()
        self.assertEqual(counts["deliveries_sent"], 1)

    def test_primary_failure_uses_fallback(self) -> None:
        primary = FakePusher("qq-bot", tier=0, fail_times=1)
        fallback = FakePusher("wxpusher", tier=1)
        manager = PushManager(self.store, self.settings, [primary, fallback])
        manager.send_digest(self._digest())
        self.assertEqual(len(primary.calls), 1)
        self.assertEqual(len(fallback.calls), 1)
        self.assertEqual(self.store.counts()["deliveries_failed"], 1)
        self.assertEqual(self.store.counts()["deliveries_sent"], 1)

    def test_duplicate_digest_is_not_resent(self) -> None:
        pusher = FakePusher("webhook", tier=0)
        manager = PushManager(self.store, self.settings, [pusher])
        digest = self._digest()
        manager.send_digest(digest)
        outcomes = manager.send_digest(digest)
        self.assertEqual(len(pusher.calls), 1)
        self.assertTrue(all(item.skipped for item in outcomes))

    def test_retry_pending_resends_after_failure(self) -> None:
        pusher = FakePusher("webhook", tier=0, fail_times=1)
        manager = PushManager(self.store, self.settings, [pusher])
        manager.send_digest(self._digest())
        self.assertEqual(self.store.counts()["deliveries_failed"], 1)
        outcomes = manager.retry_pending()
        self.assertTrue(any(item.ok for item in outcomes))
        self.assertEqual(len(pusher.calls), 2)
        self.assertEqual(self.store.counts()["deliveries_sent"], 1)

    def test_retry_without_channels_is_noop(self) -> None:
        manager = PushManager(self.store, self.settings, [])
        self.assertFalse(manager.has_channels)
        self.assertEqual(manager.retry_pending(), [])


class WxPusherPusherTest(unittest.TestCase):
    def test_html_uses_content_type_two(self) -> None:
        pusher = WxPusherPusher("AT_test", uids=["UID_1"])
        with mock.patch("qq_live_digest.push.post_json", return_value={"code": 1000}) as post:
            pusher.send("标题", "正文", summary="今天18:00 交材料", html="<div>hi</div>")
        payload = post.call_args.args[1]
        self.assertEqual(payload["contentType"], 2)
        self.assertEqual(payload["content"], "<div>hi</div>")
        self.assertEqual(payload["summary"], "今天18:00 交材料")
        self.assertEqual(payload["uids"], ["UID_1"])

    def test_without_html_falls_back_to_text(self) -> None:
        pusher = WxPusherPusher("AT_test", uids=["UID_1"])
        with mock.patch("qq_live_digest.push.post_json", return_value={"code": 1000}) as post:
            pusher.send("标题", "正文")
        payload = post.call_args.args[1]
        self.assertEqual(payload["contentType"], 1)
        self.assertIn("正文", payload["content"])


class WebhookPusherTest(unittest.TestCase):
    def _send(self, url: str, response: dict, body: str = "通知正文") -> dict:
        pusher = WebhookPusher(url)
        with mock.patch("qq_live_digest.push.post_json", return_value=response) as post:
            pusher.send("通知标题", body)
        self.assertTrue(post.called)
        self.assertTrue(str(post.call_args.args[0]).startswith(("http://", "https://")))
        return post.call_args.args[1]

    def test_dingtalk_uses_markdown(self) -> None:
        payload = self._send(
            "https://oapi.dingtalk.com/robot/send?access_token=abc", {"errcode": 0}
        )
        self.assertEqual(payload["msgtype"], "markdown")
        self.assertEqual(payload["markdown"]["title"], "通知标题")
        self.assertIn("通知标题", payload["markdown"]["text"])
        self.assertIn("通知正文", payload["markdown"]["text"])
        self.assertTrue(payload["markdown"]["text"].startswith("### "))

    def test_wecom_uses_markdown_content(self) -> None:
        payload = self._send(
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc", {"errcode": 0}
        )
        self.assertEqual(payload["msgtype"], "markdown")
        self.assertIn("通知标题", payload["markdown"]["content"])
        self.assertIn("通知正文", payload["markdown"]["content"])

    def test_feishu_uses_text_msg_type(self) -> None:
        payload = self._send(
            "https://open.feishu.cn/open-apis/bot/v2/hook/abc", {"code": 0}
        )
        self.assertEqual(payload["msg_type"], "text")
        self.assertNotIn("msgtype", payload)
        self.assertIn("通知标题", payload["content"]["text"])
        self.assertIn("通知正文", payload["content"]["text"])

    def test_lark_host_matched_by_domain(self) -> None:
        payload = self._send("https://open.larksuite.com/hook/abc", {"code": 0})
        self.assertEqual(payload["msg_type"], "text")

    def test_generic_webhook_keeps_legacy_payload(self) -> None:
        payload = self._send("https://example.com/hook", {})
        self.assertEqual(payload["title"], "通知标题")
        self.assertEqual(payload["content"], "通知正文")
        self.assertEqual(payload["source"], "qq-live-digest")

    def test_dingtalk_signed_url_matches_the_documented_recipe(self) -> None:
        """钉钉加签：待签串 = 时间戳 + "\\n" + 密钥，HMAC-SHA256 → Base64 → urlencode。"""
        import base64
        import hashlib
        import hmac
        import urllib.parse

        from qq_live_digest.push import _dingtalk_signed_url

        signed = _dingtalk_signed_url(
            "https://oapi.dingtalk.com/robot/send?access_token=abc", "SECtest", timestamp=1700000000000
        )
        digest = hmac.new(b"SECtest", b"1700000000000\nSECtest", hashlib.sha256).digest()
        expected = urllib.parse.quote_plus(base64.b64encode(digest).decode("ascii"))
        self.assertIn("access_token=abc", signed)
        self.assertIn("timestamp=1700000000000", signed)
        self.assertIn("sign=" + expected, signed)

    def test_dingtalk_secret_is_stripped_from_url_and_signed_on_send(self) -> None:
        pusher = WebhookPusher("https://oapi.dingtalk.com/robot/send?access_token=abc|SECtest")
        self.assertEqual(pusher.sign_secret, "SECtest")
        self.assertNotIn("|", pusher.url)
        self.assertIn("access_token=abc", pusher.url)
        with mock.patch("qq_live_digest.push.post_json", return_value={"errcode": 0}) as post:
            pusher.send("通知标题", "通知正文")
        posted = str(post.call_args.args[0])
        self.assertIn("access_token=abc", posted)
        self.assertNotIn("SECtest", posted)      # 密钥不许出现在请求 URL 上
        self.assertIn("timestamp=", posted)
        self.assertIn("sign=", posted)

    def test_dingtalk_error_raises_with_errcode(self) -> None:
        with self.assertRaises(PushError) as raised:
            self._send(
                "https://oapi.dingtalk.com/robot/send?access_token=abc",
                {"errcode": 310000, "errmsg": "keywords not in content"},
            )
        message = str(raised.exception)
        self.assertIn("310000", message)
        self.assertIn("keywords not in content", message)

    def test_wecom_error_raises(self) -> None:
        with self.assertRaises(PushError) as raised:
            self._send(
                "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc",
                {"errcode": 40001, "errmsg": "invalid credential"},
            )
        self.assertIn("40001", str(raised.exception))

    def test_feishu_error_raises(self) -> None:
        with self.assertRaises(PushError) as raised:
            self._send(
                "https://open.feishu.cn/open-apis/bot/v2/hook/abc",
                {"code": 19021, "msg": "sign match fail"},
            )
        self.assertIn("19021", str(raised.exception))

    def test_error_message_does_not_leak_url(self) -> None:
        url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=SECRET_TOKEN"
        with self.assertRaises(PushError) as raised:
            self._send(url, {"errcode": 40001})
        self.assertNotIn("SECRET_TOKEN", str(raised.exception))

    def test_payload_shapes_are_exact(self) -> None:
        dingtalk = self._send(
            "https://oapi.dingtalk.com/robot/send?access_token=abc", {"errcode": 0}
        )
        self.assertEqual(set(dingtalk), {"msgtype", "markdown"})
        self.assertEqual(set(dingtalk["markdown"]), {"title", "text"})

        wecom = self._send(
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc", {"errcode": 0}
        )
        self.assertEqual(set(wecom), {"msgtype", "markdown"})
        self.assertEqual(set(wecom["markdown"]), {"content"})

        feishu = self._send("https://open.feishu.cn/open-apis/bot/v2/hook/abc", {"code": 0})
        self.assertEqual(set(feishu), {"msg_type", "content"})
        self.assertEqual(set(feishu["content"]), {"text"})

        generic = self._send("https://example.com/hook", {})
        self.assertEqual(set(generic), {"title", "content", "source"})

    def test_missing_business_code_raises(self) -> None:
        """HTTP 200 但响应体没有 errcode/code 时不能当成成功。"""
        for url in (
            "https://oapi.dingtalk.com/robot/send?access_token=abc",
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc",
            "https://open.feishu.cn/open-apis/bot/v2/hook/abc",
        ):
            with self.subTest(url=url), self.assertRaises(PushError):
                self._send(url, {})

    def test_non_numeric_business_code_raises(self) -> None:
        with self.assertRaises(PushError):
            self._send(
                "https://oapi.dingtalk.com/robot/send?access_token=abc",
                {"errcode": "not-a-number"},
            )

    def test_generic_webhook_ignores_business_code(self) -> None:
        payload = self._send("https://example.com/hook", {"errcode": 500, "code": 500})
        self.assertEqual(payload["content"], "通知正文")

    def test_wecom_markdown_stays_within_the_byte_limit(self) -> None:
        """企微 markdown 限 4096 字节；CJK 正文 9000 字节时必须按字节截断。"""
        payload = self._send(
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc",
            {"errcode": 0},
            body="中" * 3000,
        )
        content = payload["markdown"]["content"]
        self.assertLessEqual(len(content.encode("utf-8")), 4000)
        self.assertIn("通知标题", content)

    def test_custom_host_is_detected_by_path(self) -> None:
        cases = {
            "https://proxy.example.com/robot/send?access_token=abc": "dingtalk",
            "https://proxy.example.com/cgi-bin/webhook/send?key=abc": "wecom",
            "https://proxy.example.com/open-apis/bot/v2/hook/abc": "feishu",
            "https://proxy.example.com/other": "generic",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(WebhookPusher(url).kind, expected)

    def test_dingtalk_signature_is_pinned_to_a_known_value(self) -> None:
        """固定 timestamp/secret 下把签名固化成一个常量，防止加签算法被悄悄改掉。"""
        from qq_live_digest.push import _dingtalk_signed_url

        signed = _dingtalk_signed_url(
            "https://oapi.dingtalk.com/robot/send?access_token=abc",
            "SECtest",
            timestamp=1700000000000,
        )
        self.assertIn("sign=aZLLrriXgn05YbwaGR7knYsLeJADjr9NwLaNNKpxh4g%3D", signed)

    def test_dingtalk_signed_url_without_query_uses_question_mark(self) -> None:
        from qq_live_digest.push import _dingtalk_signed_url

        signed = _dingtalk_signed_url(
            "https://oapi.dingtalk.com/robot/send", "SECtest", timestamp=1700000000000
        )
        self.assertTrue(signed.startswith("https://oapi.dingtalk.com/robot/send?timestamp="))
        self.assertIn("&sign=", signed)

    def test_dingtalk_secret_stripped_with_extra_query_params(self) -> None:
        pusher = WebhookPusher(
            "https://oapi.dingtalk.com/robot/send?access_token=abc|SECtest&x=1"
        )
        self.assertEqual(pusher.sign_secret, "SECtest")
        self.assertNotIn("SECtest", pusher.url)
        self.assertIn("access_token=abc", pusher.url)
        self.assertIn("x=1", pusher.url)

    def test_dingtalk_without_pipe_does_not_add_signature(self) -> None:
        pusher = WebhookPusher("https://oapi.dingtalk.com/robot/send?access_token=abc")
        self.assertEqual(pusher.sign_secret, "")
        with mock.patch("qq_live_digest.push.post_json", return_value={"errcode": 0}) as post:
            pusher.send("通知标题", "通知正文")
        posted = str(post.call_args.args[0])
        self.assertEqual(posted, "https://oapi.dingtalk.com/robot/send?access_token=abc")
        self.assertNotIn("timestamp=", posted)

    def test_signing_secret_never_leaks_into_url_target_or_error(self) -> None:
        pusher = WebhookPusher("https://oapi.dingtalk.com/robot/send?access_token=abc|SECtest")
        self.assertNotIn("SECtest", pusher.url)
        self.assertNotIn("SECtest", pusher.target)
        self.assertNotIn("SECtest", pusher.describe())
        with mock.patch(
            "qq_live_digest.push.post_json",
            return_value={"errcode": 310000, "errmsg": "sign match fail"},
        ) as post:
            with self.assertRaises(PushError) as raised:
                pusher.send("通知标题", "通知正文")
        self.assertNotIn("SECtest", str(raised.exception))
        self.assertNotIn("SECtest", str(post.call_args.args[0]))


def _stub_webhook_handler(response: dict, received: list):
    """收包存进 received，再回一个 200 + JSON（业务错误也走 200，和真机器人一致）。"""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            received.append({"path": self.path, "payload": json.loads(raw.decode("utf-8"))})
            body = json.dumps(response, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    return Handler


class HttpWebhookPusherTest(unittest.TestCase):
    """真起本地 HTTP 服务收包：报文与加签参数必须真的发出去、长成该长的样子。"""

    def _start_stub(self, response: dict) -> tuple[str, list]:
        received: list = []
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _stub_webhook_handler(response, received)
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def _stop() -> None:
            # 顺序要紧：先 shutdown 让 serve_forever 退出，再 join，最后才关监听套接字。
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

        self.addCleanup(_stop)
        return f"http://127.0.0.1:{server.server_address[1]}", received

    def test_http_dingtalk_receives_markdown_and_succeeds(self) -> None:
        base, received = self._start_stub({"errcode": 0})
        WebhookPusher(f"{base}/robot/send?access_token=abc").send("通知标题", "通知正文")
        self.assertEqual(len(received), 1)
        payload = received[0]["payload"]
        self.assertEqual(payload["msgtype"], "markdown")
        self.assertEqual(payload["markdown"]["title"], "通知标题")
        self.assertIn("通知标题", payload["markdown"]["text"])
        self.assertIn("通知正文", payload["markdown"]["text"])

    def test_http_wecom_receives_markdown_content(self) -> None:
        base, received = self._start_stub({"errcode": 0})
        WebhookPusher(f"{base}/cgi-bin/webhook/send?key=abc").send("通知标题", "通知正文")
        payload = received[0]["payload"]
        self.assertEqual(payload["msgtype"], "markdown")
        self.assertIn("通知标题", payload["markdown"]["content"])
        self.assertIn("通知正文", payload["markdown"]["content"])

    def test_http_feishu_receives_text_payload(self) -> None:
        base, received = self._start_stub({"code": 0})
        WebhookPusher(f"{base}/open-apis/bot/v2/hook/abc").send("通知标题", "通知正文")
        payload = received[0]["payload"]
        self.assertEqual(payload["msg_type"], "text")
        self.assertIn("通知标题", payload["content"]["text"])
        self.assertIn("通知正文", payload["content"]["text"])

    def test_http_generic_keeps_legacy_payload(self) -> None:
        base, received = self._start_stub({})
        WebhookPusher(f"{base}/hook").send("通知标题", "通知正文")
        payload = received[0]["payload"]
        self.assertEqual(
            payload, {"title": "通知标题", "content": "通知正文", "source": "qq-live-digest"}
        )

    def test_http_dingtalk_signature_matches_independent_hmac(self) -> None:
        """服务端收到的 query 里必须带 timestamp/sign，且 sign 与测试自算的值一字不差。"""
        base, received = self._start_stub({"errcode": 0})
        secret = "SECtestsecret"
        WebhookPusher(f"{base}/robot/send?access_token=abc|{secret}").send("通知标题", "通知正文")

        query = urllib.parse.parse_qs(urllib.parse.urlparse(received[0]["path"]).query)
        self.assertEqual(query["access_token"], ["abc"])
        self.assertNotIn("|", query["access_token"][0])
        self.assertNotIn(secret, query["access_token"][0])
        self.assertIn("timestamp", query)
        self.assertIn("sign", query)

        stamp = query["timestamp"][0]
        digest = hmac.new(
            secret.encode("utf-8"), f"{stamp}\n{secret}".encode("utf-8"), hashlib.sha256
        ).digest()
        expected = base64.b64encode(digest).decode("ascii")
        self.assertEqual(query["sign"][0], expected)

    def test_http_business_error_raises_without_leaking_url_or_secret(self) -> None:
        cases = (
            ("dingtalk", "/robot/send?access_token=abc|SECtestsecret", {"errcode": 310000, "errmsg": "sign match fail"}),
            ("wecom", "/cgi-bin/webhook/send?key=SECRET_TOKEN", {"errcode": 1, "errmsg": "invalid"}),
            ("feishu", "/open-apis/bot/v2/hook/SECRET_TOKEN", {"code": 1, "msg": "denied"}),
        )
        for kind, path, response in cases:
            with self.subTest(kind=kind):
                base, _ = self._start_stub(response)
                url = f"{base}{path}"
                with self.assertRaises(PushError) as raised:
                    WebhookPusher(url).send("通知标题", "通知正文")
                message = str(raised.exception)
                self.assertNotIn(url, message)
                self.assertNotIn("SECtestsecret", message)
                self.assertNotIn("SECRET_TOKEN", message)


class BuildWebhookPushersTest(unittest.TestCase):
    def test_webhook_urls_become_the_webhook_channel(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            webhook_urls=("https://open.feishu.cn/open-apis/bot/v2/hook/abc",),
        )
        pushers = build_pushers(settings)
        self.assertEqual([pusher.name for pusher in pushers], ["webhook"])
        self.assertIsInstance(pushers[0], WebhookPusher)
        self.assertEqual(pushers[0].kind, "feishu")
        self.assertIn("webhook", settings.push_channels())


if __name__ == "__main__":
    unittest.main()
