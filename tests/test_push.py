from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest.config import Settings  # noqa: E402
from qq_live_digest.push import PushError, PushManager, Pusher, WxPusherPusher, chunk_text  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
