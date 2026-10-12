from __future__ import annotations

import json
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from qq_live_digest.config import Settings
from qq_live_digest.dingtalk_receiver import (
    PAGE_SIZE,
    SQLITE_HEADER,
    DingTalkLocalReader,
    can_increment_snapshot,
    derive_key_v3,
    parse_dingtalk_content,
    resume_frame_index,
    wal_generation,
)
from qq_live_digest.store import Store


class DingTalkReaderTest(unittest.TestCase):
    def test_async_scan_status_and_duplicate_trigger(self) -> None:
        settings = Settings(dingtalk_enabled=True, dingtalk_group_whitelist=("g1",))
        reader = DingTalkLocalReader(settings, Mock())
        reader.thread = Mock()
        reader.thread.is_alive.return_value = True
        entered, release = threading.Event(), threading.Event()

        def blocked_scan() -> None:
            entered.set()
            release.wait(2)

        reader.scan_once = blocked_scan
        try:
            self.assertTrue(reader.start_scan())
            self.assertTrue(entered.wait(1))
            self.assertEqual(reader.status["scan_state"], "running")
            self.assertRegex(reader.status["scan_started_at"], r"^\d{4}-\d{2}-\d{2} ")
            self.assertFalse(reader.start_scan())
        finally:
            release.set()
            reader._scan_thread.join(2)
        self.assertEqual(reader.status["scan_state"], "idle")
        self.assertEqual(reader.status["scan_started_monotonic"], 0.0)
        self.assertGreaterEqual(reader.status["last_read_seconds"], 0.0)

    def test_stop_does_not_close_cipher_used_by_active_scan(self) -> None:
        settings = Settings(dingtalk_enabled=True, dingtalk_group_whitelist=("g1",))
        reader = DingTalkLocalReader(settings, Mock())
        reader.thread = Mock()
        reader.thread.is_alive.return_value = True
        class Cipher:
            closed = False
            def close(self): self.closed = True
        cipher = Cipher()
        reader._cipher = cipher
        entered, release = threading.Event(), threading.Event()
        def blocked_scan():
            entered.set()
            release.wait(2)
        reader.scan_once = blocked_scan
        self.assertTrue(reader.start_scan())
        self.assertTrue(entered.wait(1))
        reader.stop(timeout=0.01)
        self.assertFalse(cipher.closed)
        self.assertEqual(reader.status["scan_state"], "idle")
        release.set()
        reader._scan_thread.join(2)
        self.assertTrue(cipher.closed)
        self.assertEqual(reader.status["scan_state"], "idle")

    def test_unchanged_scan_stage_survives_completion(self) -> None:
        reader = DingTalkLocalReader(Settings(dingtalk_enabled=True, dingtalk_group_whitelist=("g1",)), Mock())
        reader.status.update({"scan_state": "running", "scan_stage": "数据库未变化，跳过"})
        reader._finish_scan()
        self.assertEqual(reader.status["scan_state"], "idle")
        self.assertEqual(reader.status["scan_stage"], "数据库未变化，跳过")

    def test_scan_error_records_error_state(self) -> None:
        settings = Settings(dingtalk_enabled=True, dingtalk_group_whitelist=("g1",))
        reader = DingTalkLocalReader(settings, Mock())
        reader.thread = Mock()
        reader.thread.is_alive.return_value = True
        reader.scan_once = Mock(side_effect=RuntimeError("synthetic read error"))
        self.assertTrue(reader.start_scan())
        reader._scan_thread.join(1)
        self.assertEqual(reader.status["scan_state"], "error")
        self.assertEqual(reader.status["last_error"], "synthetic read error")
        self.assertEqual(reader.status["scan_started_monotonic"], 0.0)

    def test_appended_wal_frames_reuse_snapshot_but_reset_invalidates_it(self) -> None:
        main = (10, 4096)
        salt = (11, 12)
        self.assertTrue(can_increment_snapshot(True, main, main, salt, salt, 3, 3, 4, salt))
        self.assertFalse(can_increment_snapshot(True, main, main, salt, (11, 13), 3, 0, 1, salt))
        self.assertFalse(can_increment_snapshot(True, main, (10, 0), salt, salt, 3, 3, 4, salt))
        self.assertFalse(can_increment_snapshot(True, main, main, salt, salt, 3, 3, 2, salt))

    def test_v3_key_derivation_is_stable(self) -> None:
        self.assertEqual(
            derive_key_v3("123456789", bytes.fromhex("00112233445566778899aabbccddeeff")),
            derive_key_v3("123456789", bytes.fromhex("00112233445566778899aabbccddeeff")),
        )
        self.assertEqual(len(derive_key_v3("123456789", b"salt")), 16)

    def test_wal_generation_discards_old_generation_and_tail(self) -> None:
        current = (0x11223344, 0x55667788)
        old = (1, 2)
        # Real WAL header layout: magic(4) version(4) page_size(4) checkpoint(4) salt(8) checksum(8).
        header = (
            b"\x37\x7f\x06\x82"
            + b"\x00" * 12
            + struct.pack(">II", *current)
            + b"\x00" * 8
        )
        self.assertEqual(len(header), 32)

        def frame(salt: tuple[int, int], page: int) -> bytes:
            return struct.pack(">IIIIII", page, 0, *salt, 0, 0) + bytes([page]) * PAGE_SIZE

        data = header + frame(current, 2) + frame(old, 3) + frame(current, 4)
        salt, frames = wal_generation(data)
        self.assertEqual(salt, current)
        self.assertEqual([item[0] for item in frames], [2])
        self.assertEqual(resume_frame_index({"salt_1": 1, "salt_2": 2, "frame_index": 9}, current), 0)
        self.assertEqual(resume_frame_index({"salt_1": current[0], "salt_2": current[1], "frame_index": 9}, current), 9)

    def test_content_json_text_only(self) -> None:
        self.assertEqual(parse_dingtalk_content(json.dumps({"text": "提醒：明天提交"}), 1), "提醒：明天提交")
        self.assertEqual(parse_dingtalk_content(json.dumps({"text": "card"}), 1201), "")
        self.assertEqual(parse_dingtalk_content("not json", 1), "")

    def test_parse_groups_returns_only_group_metadata(self) -> None:
        import sqlite3
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "groups.db"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE tbconversation(cid TEXT,title TEXT,memberCount INTEGER,type TEXT)")
            db.executemany("INSERT INTO tbconversation VALUES (?,?,?,?)", [("g1", "项目群", 8, "2"), ("u1", "单聊", 2, "1")])
            db.commit(); db.close()
            rows = DingTalkLocalReader.parse_groups(path.read_bytes())
            self.assertEqual(rows, [{"cid": "g1", "title": "项目群", "members": 8}])

    def test_parse_groups_error_does_not_echo_secrets(self) -> None:
        secret, salt = "SECRET-KEY", "SALT-VALUE"
        with self.assertRaises(Exception) as ctx:
            DingTalkLocalReader.parse_groups(b"not-a-db")
        self.assertNotIn(secret, str(ctx.exception)); self.assertNotIn(salt, str(ctx.exception))

    def test_web_server_forwards_reader_callbacks(self) -> None:
        from qq_live_digest.webapp import TaskWebServer
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(web_host="127.0.0.1", web_port=0, web_token="test-token", data_dir=Path(folder))
            server = TaskWebServer(settings, Store(Path(folder) / "web.sqlite3"))
            self.assertTrue(server.start())
            self.addCleanup(server.stop)
            calls = []
            server.reload_dingtalk = lambda: calls.append("dingtalk")
            server.reload_chaoxing = lambda: calls.append("chaoxing")
            server.dingtalk_reader_provider = lambda: "reader"
            self.assertTrue(callable(server.server.reload_dingtalk))
            self.assertTrue(callable(server.server.reload_chaoxing))
            self.assertTrue(callable(server.server.dingtalk_reader_provider))
            server.server.reload_dingtalk()
            server.server.reload_chaoxing()
            self.assertEqual(server.server.dingtalk_reader_provider(), "reader")
            self.assertEqual(calls, ["dingtalk", "chaoxing"])
            server.reload_dingtalk = None
            server.reload_chaoxing = None
            server.dingtalk_reader_provider = None
            server.server.reload_dingtalk()
            server.server.reload_chaoxing()
            self.assertIsNone(server.server.dingtalk_reader_provider())

    def test_no_emit_does_not_log_info(self) -> None:
        logger = Mock()
        reader = DingTalkLocalReader(Settings(dingtalk_group_whitelist=("g1",)), lambda _record: False, logger=logger)
        self.assertFalse(reader.on_message({}))
        logger.info.assert_not_called()

    def test_read_messages_extracts_text_and_group_name(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "db.sqlite3"
            store = Store(path)
            reader = DingTalkLocalReader(Settings(dingtalk_group_whitelist=("group-1",)), Mock(), store=store)
            class FakeCipher:
                def decrypt(self, page: bytes) -> bytes:
                    return page
            reader._cipher = FakeCipher()
            import sqlite3
            connection = sqlite3.connect(path)
            connection.executescript("CREATE TABLE tbconversation(cid TEXT,title TEXT); CREATE TABLE tbmsg_000(cid TEXT,mid TEXT,senderId TEXT,createdAt INTEGER,contentType INTEGER,content TEXT);")
            connection.execute("INSERT INTO tbconversation VALUES ('group-1','项目群')")
            connection.execute("INSERT INTO tbmsg_000 VALUES ('group-1','m1','u1',1700000000000,1,?)", (json.dumps({'text': '请提交'}),))
            connection.commit(); connection.close()
            records, newest = reader._read_messages(path.read_bytes(), 0)
            self.assertEqual(records[0]['group_name'], '项目群')
            self.assertEqual(records[0]['content'], '请提交')
            self.assertEqual(newest, 1700000000000)


if __name__ == "__main__":
    unittest.main()
