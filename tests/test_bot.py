"""`noticehub.bot` 的不联网测试：记录形状、回调容错、启动/停止语义。

只构造假消息对象直接调入口，不碰第三方库的连接、不起线程跑网络。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from noticehub import bot as bot_module  # noqa: E402


def _group_message(**overrides):
    fields = {
        "id": "m-1",
        "group_openid": "G1",
        "content": "请提交材料",
        "timestamp": "2026-09-29T01:00:00+08:00",
        "author": types.SimpleNamespace(member_openid="U1", username="小明"),
    }
    fields.update(overrides)
    return types.SimpleNamespace(**fields)


def _c2c_message(**overrides):
    fields = {
        "id": "c-1",
        "content": "在吗",
        "timestamp": "2026-09-29T01:00:00+08:00",
        "author": types.SimpleNamespace(user_openid="OPENID-1"),
    }
    fields.update(overrides)
    return types.SimpleNamespace(**fields)


def _client(on_message, on_openid=None) -> "bot_module.LiveBotClient":
    """绕开第三方客户端的构造（它会解析 appid、建会话），只装配被测字段。"""
    client = object.__new__(bot_module.LiveBotClient)
    client.ready = threading.Event()
    client.logger = logging.getLogger("noticehub.bot.test")
    client._on_message = on_message
    client._on_openid = on_openid
    return client


class GroupRecordTests(unittest.TestCase):
    def test_group_record_has_exact_shape_without_group_name(self) -> None:
        seen: list[dict] = []
        client = _client(seen.append)
        client._emit(_group_message(), "GROUP_MESSAGE_CREATE")

        self.assertEqual(len(seen), 1)
        record = seen[0]
        self.assertEqual(
            set(record),
            {"msg_id", "source", "event", "group_id", "sender_id", "sender_name", "ts", "received_at", "content"},
        )
        self.assertEqual(record["msg_id"], "m-1")
        self.assertEqual(record["source"], "qqbot")
        self.assertEqual(record["event"], "GROUP_MESSAGE_CREATE")
        self.assertEqual(record["group_id"], "G1")
        self.assertEqual(record["sender_id"], "U1")
        self.assertEqual(record["sender_name"], "小明")
        self.assertEqual(record["content"], "请提交材料")
        self.assertEqual(len(record["ts"]), 19, "秒精度 ISO 串")
        self.assertEqual(len(record["received_at"]), 19)

    def test_at_message_keeps_its_own_event_name(self) -> None:
        seen: list[dict] = []
        client = _client(seen.append)
        client._emit(_group_message(), "GROUP_AT_MESSAGE_CREATE")
        self.assertEqual(seen[0]["event"], "GROUP_AT_MESSAGE_CREATE")

    def test_missing_timestamp_falls_back_to_now(self) -> None:
        seen: list[dict] = []
        _client(seen.append)._emit(_group_message(timestamp=None), "GROUP_MESSAGE_CREATE")
        self.assertEqual(len(seen[0]["ts"]), 19)

    def test_callback_failure_is_swallowed(self) -> None:
        def boom(_record):
            raise RuntimeError("回调炸了")

        client = _client(boom)
        with self.assertLogs("noticehub.bot.test", level="ERROR") as logs:
            client._emit(_group_message(id="m-9"), "GROUP_MESSAGE_CREATE")
        self.assertIn("处理群消息回调失败：m-9", "\n".join(logs.output))
        # 再来一条还能处理：回调出错不影响后续消息。
        client._on_message = lambda record: None
        client._emit(_group_message(id="m-10"), "GROUP_MESSAGE_CREATE")


class C2CRecordTests(unittest.TestCase):
    def test_c2c_record_shape_and_hint(self) -> None:
        seen: list[dict] = []
        client = _client(lambda _r: None, seen.append)
        with self.assertLogs("noticehub.bot.test", level="INFO") as logs:
            asyncio.run(client.on_c2c_message_create(_c2c_message()))
        self.assertEqual(set(seen[0]), {"msg_id", "user_openid", "ts", "content"})
        self.assertEqual(seen[0]["user_openid"], "OPENID-1")
        self.assertIn("NOTICEHUB_PUSH_OPENIDS", "\n".join(logs.output))

    def test_c2c_callback_failure_is_swallowed(self) -> None:
        def boom(_record):
            raise RuntimeError("私聊回调炸了")

        client = _client(lambda _r: None, boom)
        with self.assertLogs("noticehub.bot.test", level="ERROR") as logs:
            asyncio.run(client.on_c2c_message_create(_c2c_message()))
        self.assertIn("处理私聊回调失败", "\n".join(logs.output))

    def test_openid_file_is_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = bot_module.BotRunner(types.SimpleNamespace(data_dir=Path(tmp)), lambda _r: None)
            runner._save_openid({"msg_id": "c-1", "user_openid": "OPENID-1", "ts": "2026-09-29T01:00:00", "content": "在吗"})
            runner._save_openid({"msg_id": "c-2", "user_openid": "OPENID-2", "ts": "2026-09-29T01:01:00", "content": "嗯"})
            lines = (Path(tmp) / "c2c_openids.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["user_openid"], "OPENID-1")
        self.assertIn("OPENID-2", lines[1])


class RunnerLifecycleTests(unittest.TestCase):
    def _runner(self, *, appid="123456", secret="s3cret", data_dir=None):
        settings = types.SimpleNamespace(appid=appid, secret=secret, sandbox=False, data_dir=data_dir)
        return bot_module.BotRunner(settings, lambda _r: None)

    def test_start_refuses_without_credentials(self) -> None:
        runner = self._runner(appid="", secret="")
        with self.assertLogs("noticehub.bot", level="ERROR") as logs:
            started = runner.start()
        self.assertFalse(started)
        self.assertEqual(runner.last_error, "缺少 AppID/AppSecret")
        self.assertIn("未配置 QQ_BOT_APPID/QQ_BOT_SECRET", "\n".join(logs.output))
        self.assertFalse(runner.is_alive)

    def test_start_spawns_daemon_thread_once(self) -> None:
        runner = self._runner()
        release = threading.Event()
        with mock.patch.object(bot_module.BotRunner, "_run", lambda self: release.wait(5)):
            self.assertTrue(runner.start())
            self.assertEqual(runner.thread.name, "qq-bot")
            self.assertTrue(runner.thread.daemon)
            self.assertFalse(runner.start(), "已在运行时不许起第二条线程")
            release.set()
            runner.thread.join(5)
        self.assertFalse(runner.is_alive)

    def test_stop_is_a_noop_without_thread(self) -> None:
        runner = self._runner()
        runner.stop()  # 不抛即可

    def test_ready_semantics_need_a_client(self) -> None:
        runner = self._runner()
        self.assertFalse(runner.is_ready)
        self.assertIsNone(runner.api)
        self.assertFalse(runner.wait_ready(0.01))


if __name__ == "__main__":
    unittest.main()
