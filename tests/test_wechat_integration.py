"""微信（实验性）接线的集成测试：配置默认值、fail-closed 白名单、HTTP 形状与回调转发。

这里只碰 webapp/service/config 的接线面；wechat_receiver 本身的解析另有
tests/test_wechat_receiver.py 覆盖，本文件一律用 stub 顶替真机取钥。
"""

from __future__ import annotations

import json
import re
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

from noticehub.config import Settings
from noticehub.service import DigestService
from noticehub.store import Store
from noticehub.timeutil import now_local
from noticehub.webapp import TaskWebServer, wechat_group_label

GROUP = "1234@chatroom"
OTHER = "9876@chatroom"


def wechat_record(msg_id: str, group_id: str = GROUP) -> dict:
    return {
        "msg_id": msg_id,
        "source": "wechat",
        "event": "GROUP_MESSAGE_CREATE",
        "group_id": group_id,
        "group_name": "真实群名",
        "sender_id": "wxid_x",
        "ts": time.time(),
        "content": "明天交材料",
    }


class WechatSettingsTest(unittest.TestCase):
    def test_wechat_defaults_are_off(self) -> None:
        settings = Settings()
        self.assertFalse(settings.wechat_enabled)
        self.assertEqual(settings.wechat_group_whitelist, ())
        self.assertEqual(settings.wechat_group_aliases, {})
        self.assertEqual(settings.wechat_scan_interval_seconds, 300)

    def test_wechat_env_keys_parse(self) -> None:
        settings = Settings.from_env({
            "QQ_DIGEST_WECHAT_ENABLED": "1",
            "QQ_DIGEST_WECHAT_GROUP_WHITELIST": "g1, g2",
            "QQ_DIGEST_WECHAT_GROUP_ALIASES": "g1=项目群",
            "QQ_DIGEST_WECHAT_SCAN_INTERVAL_SECONDS": "600",
        })
        self.assertTrue(settings.wechat_enabled)
        self.assertEqual(settings.wechat_group_whitelist, ("g1", "g2"))
        self.assertEqual(settings.wechat_group_aliases, {"g1": "项目群"})
        self.assertEqual(settings.wechat_scan_interval_seconds, 600)

    def test_group_label_falls_back_alias_name_then_id_prefix(self) -> None:
        self.assertEqual(wechat_group_label(GROUP, {GROUP: "真名"}, {GROUP: "别名"}), "别名")
        self.assertEqual(wechat_group_label(GROUP, {GROUP: "真名"}, {}), "真名")
        self.assertEqual(wechat_group_label("1234567890@chatroom", {}, {}), "12345678")
        # 群名恰好等于群 ID 时不算有效名字，继续回落到前 8 位。
        self.assertEqual(wechat_group_label("1234567890@chatroom", {"1234567890@chatroom": "1234567890@chatroom"}), "12345678")
        self.assertEqual(wechat_group_label("", {}, {}), "")


class WechatFailClosedTest(unittest.TestCase):
    def _service(self, tmp: str, **overrides) -> DigestService:
        settings = Settings(data_dir=Path(tmp), **overrides)
        return DigestService(settings, store=Store(Path(tmp) / "wechat.sqlite3"))

    def test_disabled_rejects_wechat_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp, wechat_enabled=False, wechat_group_whitelist=(GROUP,))
            self.assertFalse(service.on_message(wechat_record("m1")))

    def test_empty_whitelist_rejects_even_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp, wechat_enabled=True, wechat_group_whitelist=())
            self.assertFalse(service.on_message(wechat_record("m2")))

    def test_group_outside_whitelist_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp, wechat_enabled=True, wechat_group_whitelist=(OTHER,))
            self.assertFalse(service.on_message(wechat_record("m3", group_id=GROUP)))

    def test_enabled_and_whitelisted_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp, wechat_enabled=True, wechat_group_whitelist=(GROUP,))
            self.assertTrue(service.on_message(wechat_record("m4")))
            self.assertEqual(service.store.counts()["unprocessed"], 1)

    def test_other_sources_unaffected_by_wechat_whitelist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp, group_whitelist=(GROUP,))
            record = wechat_record("m5")
            record["source"] = "qqbot"
            self.assertTrue(service.on_message(record))


class WechatReloadTest(unittest.TestCase):
    def test_start_requires_nonempty_whitelist(self) -> None:
        for whitelist, should_start in (((), False), ((GROUP,), True)):
            with self.subTest(whitelist=whitelist), tempfile.TemporaryDirectory() as tmp:
                settings = Settings(
                    data_dir=Path(tmp),
                    official_bot_enabled=False,
                    attachments_enabled=False,
                    web_enabled=False,
                    wechat_enabled=True,
                    wechat_group_whitelist=whitelist,
                )
                service = DigestService(settings, store=Store(Path(tmp) / "wechat.sqlite3"))
                with patch("noticehub.service.WeChatLocalReader") as reader_cls:
                    service.start(start_bot=False)
                    self.assertEqual(reader_cls.called, should_start)
                    if should_start:
                        reader_cls.return_value.start.assert_called_once()
                        self.assertIsNotNone(service._wechat_reader)
                    else:
                        self.assertIsNone(service._wechat_reader)
                service.stop()

    def test_reload_without_start_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = DigestService(Settings(wechat_enabled=True, data_dir=Path(tmp)))
            with patch("noticehub.service.WeChatLocalReader") as reader_cls:
                service.reload_wechat_reader()
            reader_cls.assert_not_called()

    def test_reload_stops_old_and_starts_new_reader(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                wechat_enabled=True,
                wechat_group_whitelist=(GROUP,),
                wechat_scan_interval_seconds=120,
            )
            service = DigestService(settings)
            service.started_at = now_local()
            old_reader = MagicMock()
            service._wechat_reader = old_reader
            with patch("noticehub.service.WeChatLocalReader") as reader_cls:
                new_reader = reader_cls.return_value
                service.reload_wechat_reader()
            old_reader.stop.assert_called_once()
            reader_cls.assert_called_once_with(
                (GROUP,), interval_seconds=120, on_message=service.on_message,
                logger=service.logger, status=settings.wechat_status,
            )
            new_reader.start.assert_called_once()

    def test_reload_disabled_stops_reader_without_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), wechat_enabled=False)
            service = DigestService(settings)
            service.started_at = now_local()
            old_reader = MagicMock()
            service._wechat_reader = old_reader
            with patch("noticehub.service.WeChatLocalReader") as reader_cls:
                service.reload_wechat_reader()
            old_reader.stop.assert_called_once()
            reader_cls.assert_not_called()
            self.assertIsNone(service._wechat_reader)


class WechatWebTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        folder = Path(self.tmp.name)
        (folder / ".env").write_text("QQ_DIGEST_WEB_TOKEN=test-token\r\n", encoding="utf-8")
        self.settings = Settings(
            web_host="127.0.0.1",
            web_port=0,
            web_token="test-token",
            data_dir=folder,
            env_file=folder / ".env",
            wechat_enabled=True,
            wechat_group_whitelist=(GROUP,),
            wechat_group_aliases={GROUP: "项目群别名"},
        )
        self.server = TaskWebServer(self.settings, Store(folder / "web.sqlite3"))
        self.assertTrue(self.server.start())
        self.addCleanup(self.server.stop)
        self.assertTrue(self.server.server is not None)
        self.base = f"http://127.0.0.1:{self.server.server.server_address[1]}"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _open(self, request: urllib.request.Request, attempts: int = 3):
        error: Exception | None = None
        for _ in range(attempts):
            try:
                return urllib.request.urlopen(request, timeout=5)
            except (ConnectionAbortedError, ConnectionResetError) as caught:
                error = caught
                time.sleep(0.2)
        raise error  # type: ignore[misc]

    def _get(self, path: str, token: str = "test-token") -> dict:
        request = urllib.request.Request(self.base + path, headers={"X-Token": token})
        with self._open(request) as response:
            return json.loads(response.read().decode("utf-8"))

    def _post(self, path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"X-Token": "test-token", "Content-Type": "application/json"},
            method="POST",
        )
        with self._open(request) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_groups_endpoint_shape_and_no_secret_leak(self) -> None:
        secret_hex = "ab" * 32  # 64 位 hex，形如真机数据库密钥
        names = {GROUP: "测控2602班群", OTHER: "另一个群"}
        with patch("noticehub.webapp.wechat_locate_storage", return_value=Path(self.tmp.name)), \
                patch("noticehub.webapp.wechat_group_names", return_value=names):
            with self.assertLogs("noticehub.webapp", level="INFO") as logs:
                body = self._get("/api/wechat/groups")
        self.assertTrue(body["ok"])
        rows = {row["id"]: row for row in body["groups"]}
        self.assertEqual(set(rows), {GROUP, OTHER})
        self.assertEqual(rows[GROUP]["name"], "测控2602班群")
        self.assertEqual(rows[GROUP]["alias"], "项目群别名")
        self.assertTrue(rows[GROUP]["selected"])
        self.assertEqual(rows[OTHER]["alias"], "")
        self.assertFalse(rows[OTHER]["selected"])
        blob = json.dumps(body, ensure_ascii=False) + "\n".join(record.getMessage() for record in logs.records)
        self.assertNotIn(secret_hex, blob)
        self.assertIsNone(re.search(r"[0-9a-fA-F]{32,}", blob))

    def test_groups_endpoint_requires_token(self) -> None:
        request = urllib.request.Request(self.base + "/api/wechat/groups", headers={"X-Token": "wrong"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._open(request)
        self.assertEqual(ctx.exception.code, 401)
        ctx.exception.close()

    def test_groups_endpoint_reports_missing_wechat(self) -> None:
        with patch("noticehub.webapp.wechat_locate_storage", return_value=None):
            body = self._get("/api/wechat/groups")
        self.assertFalse(body["ok"])
        self.assertEqual(body["groups"], [])

    def test_status_endpoint_reports_reader_and_last_read(self) -> None:
        with patch("noticehub.webapp.wechat_locate_storage", return_value=Path(self.tmp.name)), \
                patch("noticehub.webapp.wechat_group_names", return_value={GROUP: "测控2602班群"}):
            self._get("/api/wechat/groups")
            reader = MagicMock()
            reader.is_alive = True
            self.server.wechat_reader_provider = lambda: reader
            status = self._get("/api/wechat/status")
        self.assertTrue(status["enabled"])
        self.assertTrue(status["running"])
        self.assertEqual(status["groups"], 1)
        self.assertTrue(status["last_read_at"])
        self.assertEqual(status["state"], "已连接 1 个群")

    def test_settings_get_and_post_roundtrip_wechat(self) -> None:
        prefs = self._get("/api/settings")
        self.assertTrue(prefs["wechat_enabled"])
        self.assertEqual(prefs["wechat_group_whitelist"], [GROUP])
        self.assertEqual(prefs["wechat_group_aliases"], {GROUP: "项目群别名"})

        saved = self._post("/api/settings", {
            "wechat_enabled": True,
            "wechat_group_whitelist": [GROUP, OTHER],
            "wechat_group_aliases": {OTHER: "第二个群"},
            "wechat_scan_interval_seconds": 60,
        })
        self.assertTrue(saved["ok"])
        self.assertEqual(saved["wechat_group_whitelist"], [GROUP, OTHER])
        self.assertEqual(saved["wechat_group_aliases"], {OTHER: "第二个群"})
        self.assertEqual(saved["wechat_scan_interval_seconds"], 60)
        env_text = (Path(self.tmp.name) / ".env").read_text(encoding="utf-8")
        self.assertIn("NOTICEHUB_WECHAT_ENABLED=1", env_text)
        self.assertIn(f"NOTICEHUB_WECHAT_GROUP_WHITELIST={GROUP},{OTHER}", env_text)
        self.assertIn(f"NOTICEHUB_WECHAT_GROUP_ALIASES={OTHER}=第二个群", env_text)

    def test_settings_post_rejects_mixed_sections(self) -> None:
        request = urllib.request.Request(
            self.base + "/api/settings",
            data=json.dumps({"wechat_enabled": True, "dingtalk_enabled": True}).encode("utf-8"),
            headers={"X-Token": "test-token", "Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._open(request)
        self.assertEqual(ctx.exception.code, 400)
        body = json.loads(ctx.exception.read().decode("utf-8"))
        ctx.exception.close()
        self.assertFalse(body["ok"])
        self.assertIn("单独保存", body["error"])


class WechatForwardingTest(unittest.TestCase):
    def test_web_server_forwards_wechat_callbacks(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(web_host="127.0.0.1", web_port=0, web_token="test-token", data_dir=Path(folder))
            server = TaskWebServer(settings, Store(Path(folder) / "web.sqlite3"))
            self.assertTrue(server.start())
            self.addCleanup(server.stop)
            calls: list[str] = []
            server.reload_wechat = lambda: calls.append("wechat")
            server.wechat_reader_provider = lambda: "reader"
            self.assertTrue(callable(server.server.reload_wechat))
            self.assertTrue(callable(server.server.wechat_reader_provider))
            server.server.reload_wechat()
            self.assertEqual(server.server.wechat_reader_provider(), "reader")
            self.assertEqual(calls, ["wechat"])
            server.reload_wechat = None
            server.wechat_reader_provider = None
            server.server.reload_wechat()
            self.assertIsNone(server.server.wechat_reader_provider())


if __name__ == "__main__":
    unittest.main()
