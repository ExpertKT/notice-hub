"""qq_live_digest/wechat_receiver.py 测试。

全部合成数据：用明文 SQLite 直接构造「解密后」的库字节，用 mock 打桩内存扫描与密钥
发现（**绝不真扫 767MB 内存 / 不碰真机微信目录**）。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import struct
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from qq_live_digest import ttl_cache
from qq_live_digest import wechat_receiver as wr

GROUP = "1234@chatroom"
GROUP_OTHER = "9999@chatroom"


def _sqlite_bytes(build) -> bytes:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    connection = sqlite3.connect(path)
    try:
        build(connection)
        connection.commit()
    finally:
        connection.close()
    try:
        return Path(path).read_bytes()
    finally:
        os.unlink(path)


def build_message_db(group_id, rows, senders=None) -> bytes:
    table = wr.message_table_name(group_id)

    def build(connection):
        connection.execute(
            f'CREATE TABLE "{table}" (local_id INTEGER, local_type INTEGER, '
            "real_sender_id INTEGER, create_time INTEGER, message_content BLOB)"
        )
        connection.executemany(
            f'INSERT INTO "{table}" VALUES (?, ?, ?, ?, ?)', rows
        )
        if senders is not None:
            connection.execute("CREATE TABLE Name2Id (user_name TEXT)")
            connection.executemany(
                "INSERT INTO Name2Id (rowid, user_name) VALUES (?, ?)", senders
            )

    return _sqlite_bytes(build)


def build_contact_db(rows) -> bytes:
    def build(connection):
        connection.execute(
            "CREATE TABLE contact (id INTEGER, username TEXT, nick_name TEXT, "
            "remark TEXT, alias TEXT)"
        )
        connection.executemany(
            "INSERT INTO contact (username, nick_name) VALUES (?, ?)", rows
        )

    return _sqlite_bytes(build)


def make_storage(tmpdir, message_dbs=None, contact_db=None) -> Path:
    """造一个假的 db_storage 目录树。"""
    storage = Path(tmpdir) / "db_storage"
    (storage / "message").mkdir(parents=True)
    for name, blob in message_dbs or []:
        (storage / "message" / name).write_bytes(blob)
    if contact_db is not None:
        (storage / "contact").mkdir()
        (storage / "contact" / "contact.db").write_bytes(contact_db)
    return storage


class RecordShapeTests(unittest.TestCase):
    def test_record_fields_and_text_filter(self):
        blob = build_message_db(
            GROUP,
            [
                (1, 1, 2, 1791626082, "hello 群"),
                (2, 3, 2, 1791626083, b"\x89PNG\r\n"),   # 图片，非文本
                (3, 1, 2, 1791626084, ""),               # 空文本
                (4, 1, 2, 1791626085, b"\xff\xfe\x00"),  # 非法 UTF-8
            ],
            senders=[(2, "wxid_bob")],
        )
        records = wr.read_messages_from_db(blob, {GROUP}, {GROUP: "测试群"})
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(
            set(record),
            {"msg_id", "source", "event", "group_id", "group_name",
             "sender_id", "ts", "content"},
        )
        self.assertEqual(record["msg_id"], f"wechat:{GROUP}:1")
        self.assertEqual(record["source"], "wechat")
        self.assertEqual(record["group_id"], GROUP)
        self.assertEqual(record["group_name"], "测试群")
        self.assertEqual(record["sender_id"], "wxid_bob")
        self.assertEqual(record["ts"], 1791626082)
        self.assertEqual(record["content"], "hello 群")

    def test_sender_falls_back_to_raw_id_without_name2id(self):
        blob = build_message_db(GROUP, [(1, 1, 7, 100, "hi")])
        records = wr.read_messages_from_db(blob, {GROUP}, {})
        self.assertEqual(records[0]["sender_id"], "7")
        self.assertIsNone(records[0]["group_name"])

    def test_since_ts_filters_old_rows(self):
        blob = build_message_db(
            GROUP, [(1, 1, 1, 100, "old"), (2, 1, 1, 200, "new")]
        )
        records = wr.read_messages_from_db(blob, {GROUP}, {}, since_ts=150)
        self.assertEqual([r["content"] for r in records], ["new"])

    def test_rowid_cursor_reads_only_appended_rows_even_with_old_timestamps(self):
        blob = build_message_db(
            GROUP, [(1, 1, 1, 100, "old"), (2, 1, 1, 101, "appended")]
        )
        rowid_updates = {}
        records = wr.read_messages_from_db(
            blob, {GROUP}, {}, since_ts=1000, since_rowids={GROUP: 1},
            rowid_updates=rowid_updates,
        )
        self.assertEqual([record["content"] for record in records], ["appended"])
        self.assertEqual(rowid_updates[GROUP], 2)

    def test_rowid_unavailable_falls_back_to_full_scan_and_reports_reason(self):
        table = wr.message_table_name(GROUP)

        def build(connection):
            connection.execute(
                f'CREATE TABLE "{table}" (local_id INTEGER PRIMARY KEY, '
                "local_type INTEGER, real_sender_id INTEGER, create_time INTEGER, "
                "message_content TEXT) WITHOUT ROWID"
            )
            connection.execute(f'INSERT INTO "{table}" VALUES (1, 1, 1, 100, "old")')
            connection.execute(f'INSERT INTO "{table}" VALUES (2, 1, 1, 200, "recent")')

        rowid_updates = {}
        stages = []
        records = wr.read_messages_from_db(
            _sqlite_bytes(build), {GROUP}, {}, since_ts=150,
            since_rowids={GROUP: 88}, rowid_updates=rowid_updates, progress=stages.append,
        )
        self.assertEqual([record["content"] for record in records], ["old", "recent"])
        self.assertEqual(rowid_updates[GROUP], 0)
        self.assertIn("无法按 rowid 增量定位", stages[0])

    def test_message_table_name_matches_md5(self):
        expected = "Msg_" + hashlib.md5(GROUP.encode()).hexdigest()
        self.assertEqual(wr.message_table_name(GROUP), expected)


class WhitelistTests(unittest.TestCase):
    def test_group_not_in_whitelist_is_dropped(self):
        blob = build_message_db(GROUP, [(1, 1, 1, 100, "secret")])
        self.assertEqual(
            wr.read_messages_from_db(blob, {GROUP_OTHER}, {GROUP_OTHER: "别的群"}), []
        )

    def test_empty_whitelist_produces_nothing(self):
        blob = build_message_db(GROUP, [(1, 1, 1, 100, "secret")])
        self.assertEqual(wr.read_messages_from_db(blob, set(), {}), [])
        self.assertEqual(wr.read_messages_from_db(blob, None, {}), [])

    def test_fetch_messages_empty_whitelist_short_circuits(self):
        with mock.patch.object(wr, "locate_storage") as locate:
            self.assertEqual(wr.fetch_messages("ignored", set()), [])
        locate.assert_not_called()


class ContactNameTests(unittest.TestCase):
    def test_parse_contact_names_filters_empty_and_non_groups(self):
        blob = build_contact_db([
            ("g1@chatroom", "群一"),
            ("g2@chatroom", None),
            ("g3@chatroom", "   "),
            ("g4@chatroom", "群四"),
            ("wxid_alice", "张三"),
        ])
        self.assertEqual(
            wr.parse_contact_names(blob),
            {"g1@chatroom": "群一", "g4@chatroom": "群四"},
        )

    def test_parse_contact_names_survives_plain_garbage(self):
        self.assertEqual(wr.parse_contact_names(b"not a sqlite db"), {})

    def test_group_names_returns_mapping_with_injected_key(self):
        contact = build_contact_db([("g1@chatroom", "群一")])
        with tempfile.TemporaryDirectory() as tmp:
            storage = make_storage(tmp, contact_db=contact)
            with mock.patch.object(wr, "resolve_key", return_value=b"k" * 32):
                with mock.patch.object(wr, "decrypt_database", return_value=contact):
                    names = wr.group_names(storage, key=b"k" * 32)
        self.assertEqual(names, {"g1@chatroom": "群一"})

    def test_group_names_missing_storage_logs_and_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertLogs(wr.LOGGER, level="WARNING") as logs:
                names = wr.group_names(Path(tmp) / "nope")
        self.assertEqual(names, {})
        self.assertIn("db_storage", "\n".join(logs.output))

    def test_group_names_without_key_logs_and_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = make_storage(tmp, contact_db=build_contact_db([]))
            with mock.patch.object(wr, "resolve_key", return_value=None):
                with self.assertLogs(wr.LOGGER, level="WARNING") as logs:
                    names = wr.group_names(storage)
        self.assertEqual(names, {})
        self.assertIn("未取得密钥", "\n".join(logs.output))

    def test_group_names_decrypt_failure_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = make_storage(tmp, contact_db=b"\x00" * 32)
            with mock.patch.object(wr, "resolve_key", return_value=b"k" * 32):
                with mock.patch.object(
                    wr, "decrypt_database", side_effect=ValueError("boom")
                ):
                    with self.assertLogs(wr.LOGGER, level="WARNING") as logs:
                        names = wr.group_names(storage, key=b"k" * 32)
        self.assertEqual(names, {})
        self.assertIn("contact.db", "\n".join(logs.output))


@unittest.skipUnless(os.name == "nt", "Windows process-memory key discovery")
class KeyDiscoveryTests(unittest.TestCase):
    @staticmethod
    def _page_for_key(key: bytes, salt: bytes = b"\x11" * 16) -> bytes:
        page = bytearray(4096)
        page[:16] = salt
        for index in range(16, 4096 - 80 + 16):
            page[index] = (index * 7) & 0xFF
        mac_salt = bytes(b ^ 0x3A for b in salt)
        mac_key = hashlib.pbkdf2_hmac("sha512", key, mac_salt, 2, 32)
        page[4096 - 64:4096] = hmac.new(
            mac_key, bytes(page[16:4096 - 80 + 16]) + struct.pack("<I", 1),
            hashlib.sha512,
        ).digest()
        return bytes(page)

    def test_check_page_key_accepts_matching_key(self):
        key = bytes(range(32))
        self.assertTrue(wr._check_page_key(self._page_for_key(key), key))
        self.assertFalse(wr._check_page_key(self._page_for_key(key), b"\x00" * 32))

    def test_page_key_valid_requires_sqlite_header_before_hmac(self):
        page = bytes(4096)
        key = bytes(range(32))
        with mock.patch.object(wr, "decrypt_page", return_value=wr.SQLITE_HEADER + bytes(4080)):
            with mock.patch.object(wr, "_check_page_key", return_value=True) as check:
                self.assertTrue(wr._page_key_valid(page, key))
                check.assert_called_once_with(page, key)
        with mock.patch.object(wr, "decrypt_page", return_value=b"not sqlite" + bytes(4080)):
            with mock.patch.object(wr, "_check_page_key") as check:
                self.assertFalse(wr._page_key_valid(page, key))
                check.assert_not_called()

    def test_candidate_keys_decodes_xor_masked_literal(self):
        key = bytes(range(32))
        literal = b"x'" + key.hex().encode() + b"'"
        mask = wr._CONFIG_XOR_MASK
        masked = bytes(b ^ mask[i % len(mask)] for i, b in enumerate(literal))
        self.assertIn(key, wr._candidate_keys(masked))

    def test_find_key_without_process_raises_not_logged_in(self):
        """没找到 Weixin.exe 要说清「微信没登录」，不能只回 None 让上层瞎猜。"""
        key = bytes(range(32))
        with tempfile.TemporaryDirectory() as tmp:
            storage = make_storage(tmp, message_dbs=[("message_0.db", b"\x00" * 4096)])
            with mock.patch.object(wr, "_read_shared", return_value=self._page_for_key(key)):
                with mock.patch.object(wr, "find_pids", side_effect=RuntimeError("none")):
                    with self.assertLogs(wr.LOGGER, level="WARNING") as logs:
                        with self.assertRaises(wr.WechatScanError) as raised:
                            wr._find_key(storage)
        self.assertEqual(raised.exception.category, "not_logged_in")
        self.assertIn("未找到 Weixin.exe 进程", "\n".join(logs.output))

    def test_find_key_scans_memory_and_matches(self):
        key = bytes(range(32))
        page = self._page_for_key(key)
        with tempfile.TemporaryDirectory() as tmp:
            storage = make_storage(tmp, message_dbs=[("message_0.db", b"\x00" * 4096)])
            with mock.patch.object(wr, "_read_shared", return_value=page):
                with mock.patch.object(wr, "find_pids", return_value=[4242]):
                    with mock.patch.object(wr, "scan_process", return_value={key}):
                        self.assertEqual(wr._find_key(storage), key)

    def test_find_key_without_message_db_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = make_storage(tmp)
            with self.assertLogs(wr.LOGGER, level="ERROR") as logs:
                self.assertIsNone(wr._find_key(storage))
        self.assertIn("未找到 message/message_*.db", "\n".join(logs.output))

    def test_find_key_no_candidates_logs_and_classifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = make_storage(tmp, message_dbs=[("message_0.db", b"\x00" * 4096)])
            with mock.patch.object(wr, "_read_shared", return_value=bytes(4096)):
                with mock.patch.object(wr, "find_pids", return_value=[1]):
                    with mock.patch.object(wr, "scan_process", return_value=set()):
                        with self.assertLogs(wr.LOGGER, level="WARNING") as logs:
                            with self.assertRaises(wr.WechatScanError) as raised:
                                wr._find_key(storage)
        self.assertEqual(raised.exception.category, "no_candidates")
        self.assertIn("未找到可解开 message_0.db 的密钥", "\n".join(logs.output))

    def test_find_key_unmatched_candidates_says_version_maybe_unsupported(self):
        """issue #5 的核心：20 个候选都解不开要说「版本可能没适配」，不是让用户干等。"""
        with tempfile.TemporaryDirectory() as tmp:
            storage = make_storage(tmp, message_dbs=[("message_0.db", b"\x00" * 4096)])
            with mock.patch.object(wr, "_read_shared", return_value=bytes(4096)):
                with mock.patch.object(wr, "find_pids", return_value=[1]):
                    with mock.patch.object(wr, "scan_process", return_value={bytes(range(32))}):
                        with self.assertRaises(wr.WechatScanError) as raised:
                            wr._find_key(storage)
        self.assertEqual(raised.exception.category, "candidates_unmatched")
        self.assertIn("可能改了密钥存放方式", str(raised.exception))
        self.assertEqual(wr.last_key_failure().get("category"), "candidates_unmatched")

    def test_find_key_targets_each_database_and_caches_separately(self):
        key_message = bytes(range(32))
        key_contact = bytes(range(32, 64))
        page_message = self._page_for_key(key_message, b"m" * 16)
        page_contact = self._page_for_key(key_contact, b"c" * 16)
        with tempfile.TemporaryDirectory() as tmp:
            storage = make_storage(tmp, message_dbs=[("message_0.db", page_message)])
            contact = storage / "contact" / "contact.db"
            contact.parent.mkdir()
            contact.write_bytes(page_contact)
            pages = {storage / "message" / "message_0.db": page_message, contact: page_contact}
            with mock.patch.object(wr, "_read_shared", side_effect=lambda p: pages[Path(p)]):
                with mock.patch.object(wr, "find_pids", return_value=[1]):
                    with mock.patch.object(wr, "scan_process", return_value={key_message, key_contact}):
                        with mock.patch.object(wr, "_page_key_valid", side_effect=lambda p, k: wr._check_page_key(p, k)):
                            wr._KEY_CACHE.clear()
                            self.assertEqual(wr.resolve_key(storage, target_db="message/message_0.db"), key_message)
                            self.assertEqual(wr.resolve_key(storage, target_db=contact), key_contact)

    def test_resolve_key_reuses_valid_cache(self):
        key = bytes(range(32))
        page = self._page_for_key(key)
        with tempfile.TemporaryDirectory() as tmp:
            storage = make_storage(tmp, message_dbs=[("message_0.db", page)])
            with mock.patch.object(wr, "_read_shared", return_value=page):
                with mock.patch.object(wr, "_find_key", return_value=key) as find:
                    with mock.patch.object(wr, "_page_key_valid", side_effect=lambda p, k: wr._check_page_key(p, k)):
                        wr._KEY_CACHE.clear()
                        self.assertEqual(wr.resolve_key(storage), key)
                        self.assertEqual(wr.resolve_key(storage), key)
                    find.assert_called_once()

    def test_explicit_invalid_key_is_rejected(self):
        page = self._page_for_key(bytes(range(32)))
        with tempfile.TemporaryDirectory() as tmp:
            storage = make_storage(tmp, contact_db=page)
            with mock.patch.object(wr, "_page_key_valid", return_value=False):
                self.assertIsNone(wr.resolve_key(storage, b"x" * 32, target_db="contact/contact.db"))
                with mock.patch.object(wr, "decrypt_database") as decrypt:
                    self.assertEqual(wr.group_names(storage, key=b"x" * 32), {})
                decrypt.assert_not_called()

    def test_group_names_without_contact_key_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = make_storage(tmp, contact_db=bytes(4096))
            with mock.patch.object(wr, "resolve_key", return_value=None):
                self.assertEqual(wr.group_names(storage), {})


class FetchMessagesTests(unittest.TestCase):
    def test_fetch_messages_without_key_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = make_storage(tmp, message_dbs=[("message_0.db", b"\x00" * 4096)])
            with mock.patch.object(wr, "resolve_key", return_value=None):
                self.assertEqual(wr.fetch_messages(storage, {GROUP}), [])

    def test_fetch_messages_missing_storage_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertLogs(wr.LOGGER, level="WARNING") as logs:
                result = wr.fetch_messages(Path(tmp) / "gone", {GROUP})
        self.assertEqual(result, [])
        self.assertIn("db_storage", "\n".join(logs.output))

    def test_fetch_messages_end_to_end_with_patched_decrypt(self):
        message = build_message_db(GROUP, [(5, 1, 1, 1700000000, "群消息")])
        contact = build_contact_db([(GROUP, "真群名")])
        with tempfile.TemporaryDirectory() as tmp:
            storage = make_storage(
                tmp,
                message_dbs=[("message_0.db", b"\x00" * 4096)],
                contact_db=contact,
            )

            def fake_decrypt(path, wal=None, key=None, progress=None):
                return contact if Path(path).name == "contact.db" else message

            with mock.patch.object(wr, "decrypt_database", side_effect=fake_decrypt):
                with mock.patch.object(wr, "resolve_key", return_value=b"k" * 32):
                    records = wr.fetch_messages(storage, {GROUP}, key=b"k" * 32)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["msg_id"], f"wechat:{GROUP}:5")
        self.assertEqual(records[0]["group_name"], "真群名")
        self.assertEqual(records[0]["content"], "群消息")


class WalAndPageTests(unittest.TestCase):
    def test_wal_frames_parses_current_salt_only(self):
        header = struct.pack(">6I", 0x377F0682, 3007000, 4096, 0, 111, 222) + bytes(8)
        good = struct.pack(">4I", 2, 0, 111, 222) + bytes(8) + b"\x0a" * 4096
        stale = struct.pack(">4I", 3, 0, 9, 9) + bytes(8) + b"\x0b" * 4096
        frames = wr._wal_frames(header + good + stale)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0][0], 2)
        self.assertEqual(frames[0][2], b"\x0a" * 4096)

    def test_wal_frames_rejects_non_wal(self):
        self.assertEqual(wr._wal_frames(b"nope"), [])

    def test_decrypt_page_layout_with_identity_engine(self):
        class IdentityEngine:
            def decrypt(self, data):
                return data

        page = bytes(range(0, 256)) * 16  # 4096 字节
        plain = wr.decrypt_page(IdentityEngine(), page, 1)
        self.assertEqual(len(plain), 4096)
        self.assertTrue(plain.startswith(wr.SQLITE_HEADER))
        second = wr.decrypt_page(IdentityEngine(), page, 2)
        self.assertEqual(len(second), 4096)
        self.assertFalse(second.startswith(wr.SQLITE_HEADER))

    def test_parse_wechat_text(self):
        self.assertEqual(wr.parse_wechat_text("  你好  "), "你好")
        self.assertEqual(wr.parse_wechat_text("文本\x01残留"), "文本")
        self.assertEqual(wr.parse_wechat_text(b"\x28\xb5\x2f\xfd..."), "")
        self.assertEqual(wr.parse_wechat_text(b"\xff\xfe"), "")
        self.assertEqual(wr.parse_wechat_text(None), "")


class ReaderLifecycleTests(unittest.TestCase):
    def test_scan_once_missing_storage_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            reader = wr.WeChatLocalReader({GROUP}, storage_dir=Path(tmp) / "gone")
            with self.assertRaisesRegex(RuntimeError, "未找到微信本地数据库目录"):
                reader.scan_once()

    def test_scan_once_emits_records(self):
        message = build_message_db(
            GROUP, [(1, 1, 1, 1700000000, "第一条"), (2, 1, 1, 1700000001, "第二条")]
        )
        contact = build_contact_db([(GROUP, "真群名")])
        received = []
        with tempfile.TemporaryDirectory() as tmp:
            storage = make_storage(
                tmp, message_dbs=[("message_0.db", b"\x00" * 4096)], contact_db=contact
            )

            def fake_decrypt(path, wal=None, key=None, progress=None):
                return contact if Path(path).name == "contact.db" else message

            reader = wr.WeChatLocalReader(
                {GROUP}, interval_seconds=5, on_message=received.append,
                storage_dir=storage,
            )
            reader._since = 0  # 让合成库里的历史消息进入本次扫描
            with mock.patch.object(wr, "resolve_key", return_value=b"k" * 32):
                with mock.patch.object(wr, "decrypt_database", side_effect=fake_decrypt):
                    count = reader.scan_once()
        self.assertEqual(count, 2)
        self.assertEqual([r["content"] for r in received], ["第一条", "第二条"])
        self.assertEqual(reader._since, 1700000001)

    def test_scan_once_swallows_callback_error(self):
        def boom(_record):
            raise RuntimeError("callback down")

        with tempfile.TemporaryDirectory() as tmp:
            storage = make_storage(
                tmp, message_dbs=[("message_0.db", b"\x00" * 4096)]
            )
            reader = wr.WeChatLocalReader({GROUP}, on_message=boom, storage_dir=storage)
            with mock.patch.object(wr, "resolve_key", return_value=b"k" * 32):
                with mock.patch.object(
                    wr, "read_messages_from_db",
                    return_value=[{"ts": 1, "msg_id": "wechat:x:1"}],
                ):
                    with mock.patch.object(wr, "decrypt_database", return_value=b""):
                        with self.assertRaisesRegex(RuntimeError, "交付失败"):
                            reader.scan_once()

    def test_async_scan_status_and_duplicate_trigger(self):
        reader = wr.WeChatLocalReader({GROUP})
        reader._thread = mock.Mock()
        reader._thread.is_alive.return_value = True
        entered, release = threading.Event(), threading.Event()

        def blocked_scan():
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

    def test_scan_once_skips_key_and_decrypt_when_db_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = make_storage(tmp, message_dbs=[("message_0.db", b"x" * 4096)])
            reader = wr.WeChatLocalReader({GROUP}, storage_dir=storage)
            reader._key = b"k" * 32
            reader.status["scan_state"] = "running"
            with mock.patch.object(wr, "_safe_group_names", return_value={}), \
                 mock.patch.object(wr, "fetch_messages", return_value=[]) as fetch:
                self.assertEqual(reader.scan_once(), 0)
                self.assertEqual(reader.scan_once(), 0)
            fetch.assert_called_once()
            reader._finish_scan()
            self.assertEqual(reader.status["scan_stage"], "数据库未变化，跳过")

    def test_file_signature_includes_wal_and_shm(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = make_storage(tmp, message_dbs=[("message_0.db", b"x" * 4096)])
            reader = wr.WeChatLocalReader({GROUP}, storage_dir=storage)
            initial = reader._file_signatures(storage)
            Path(str(storage / "message" / "message_0.db") + "-shm").write_bytes(b"shm")
            self.assertNotEqual(initial, reader._file_signatures(storage))

    def test_stop_does_not_let_active_scan_overwrite_status(self):
        reader = wr.WeChatLocalReader({GROUP})
        reader._thread = mock.Mock()
        reader._thread.is_alive.return_value = True
        entered, release = threading.Event(), threading.Event()
        def blocked_scan():
            entered.set()
            release.wait(2)
        reader.scan_once = blocked_scan
        self.assertTrue(reader.start_scan())
        self.assertTrue(entered.wait(1))
        reader.stop(timeout=0.01)
        self.assertEqual(reader.status["scan_state"], "idle")
        release.set()
        reader._scan_thread.join(2)
        self.assertEqual(reader.status["scan_state"], "idle")

    def test_scan_error_records_error_state(self):
        reader = wr.WeChatLocalReader({GROUP})
        reader._thread = mock.Mock()
        reader._thread.is_alive.return_value = True
        reader.scan_once = mock.Mock(side_effect=RuntimeError("synthetic read error"))
        self.assertTrue(reader.start_scan())
        reader._scan_thread.join(1)
        self.assertEqual(reader.status["scan_state"], "error")
        self.assertEqual(reader.status["last_error"], "synthetic read error")
        self.assertEqual(reader.status["scan_started_monotonic"], 0.0)

    def _scan_error_reader(self, exc: BaseException) -> "wr.WeChatLocalReader":
        reader = wr.WeChatLocalReader({GROUP})
        reader._thread = mock.Mock()
        reader._thread.is_alive.return_value = True
        reader.scan_once = mock.Mock(side_effect=exc)
        self.assertTrue(reader.start_scan())
        reader._scan_thread.join(2)
        return reader

    def test_scan_error_category_not_logged_in(self):
        reader = self._scan_error_reader(RuntimeError("未找到微信本地数据库目录\n找不到数据"))
        self.assertEqual(reader.status["error_category"], "not_logged_in")
        self.assertIn("登录微信", wr.scan_error_hint(reader.status["error_category"]))

    def test_scan_error_category_no_candidates(self):
        reader = self._scan_error_reader(RuntimeError("未能从微信本地进程取得数据库密钥"))
        self.assertEqual(reader.status["error_category"], "no_candidates")
        self.assertIn("3–6 分钟", wr.scan_error_hint(reader.status["error_category"]))

    def test_scan_error_category_candidates_unmatched(self):
        reader = self._scan_error_reader(wr.WechatScanError("扫到 20 个候选密钥，但都解不开 contact.db", "candidates_unmatched"))
        self.assertEqual(reader.status["error_category"], "candidates_unmatched")
        self.assertIn("可能未适配", wr.scan_error_hint(reader.status["error_category"]))

    def test_scan_error_category_version_unsupported(self):
        reader = self._scan_error_reader(RuntimeError("检测到的是 WeChat.exe（微信 3.x），本功能只支持微信 4.0（Weixin.exe）："))
        self.assertEqual(reader.status["error_category"], "version_unsupported")
        self.assertIn("升级微信", wr.scan_error_hint(reader.status["error_category"]))

    def test_scan_error_category_other(self):
        reader = self._scan_error_reader(RuntimeError("synthetic read error"))
        self.assertEqual(reader.status["error_category"], "other")
        self.assertIn("日志原文", wr.scan_error_hint(reader.status["error_category"]))

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_start_stop_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            reader = wr.WeChatLocalReader({GROUP}, interval_seconds=5, storage_dir=tmp)
            self.assertFalse(reader.is_alive)
            self.assertTrue(reader.start())
            self.assertTrue(reader.start())  # 幂等
            self.assertTrue(reader.is_alive)
            reader.stop()
            self.assertFalse(reader.is_alive)

    def test_start_returns_false_off_windows(self):
        with mock.patch.object(wr.os, "name", "posix"):
            reader = wr.WeChatLocalReader({GROUP})
            with self.assertLogs(wr.LOGGER, level="WARNING") as logs:
                self.assertFalse(reader.start())
        self.assertIn("仅支持 Windows", "\n".join(logs.output))
        self.assertFalse(reader.is_alive)


class StorageDiscoveryTests(unittest.TestCase):
    """微信 4.0 数据目录发现：环境变量 / 进程 / 注册表 / 用户文档 / 默认路径，以及失败文案。"""

    def setUp(self):
        # 缓存用例要把三个包装函数换回真身：patch 之后原件就拿不到了，先存一份。
        self._real_config_roots = wr._wechat_config_roots
        self._real_process_dirs = wr._process_install_dirs
        self._real_drive_roots = wr._drive_storage_roots
        # 真实机器上跑着的微信会污染发现结果：这里统一关掉进程发现，需要的用例单独打开。
        patcher = mock.patch.object(wr, "_process_install_dirs", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)
        # 盘符扫描会捞到本机真实的 xwechat_files（D 盘就有），发现类用例必须自己说了算。
        drive = mock.patch.object(wr, "_drive_storage_roots", return_value=[])
        drive.start()
        self.addCleanup(drive.stop)
        # 本机 %APPDATA%\Tencent\xwechat\config 里真有存储位置 ini，同理要关掉。
        config = mock.patch.object(wr, "_wechat_config_roots", return_value=[])
        config.start()
        self.addCleanup(config.stop)

    def _storage(self, root: Path, account: str = "wxid_demo") -> Path:
        storage = root / account / "db_storage"
        storage.mkdir(parents=True)
        return storage

    def test_discovers_standard_documents_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp) / "Documents"
            expected = self._storage(docs / "xwechat_files")
            with mock.patch.object(wr, "_documents_dir", return_value=docs), \
                    mock.patch.object(wr, "_registry_storage_roots", return_value=[]), \
                    mock.patch.object(wr, "DEFAULT_STORAGE_ROOT", Path(tmp) / "missing"), \
                    mock.patch.dict(os.environ, {wr.STORAGE_DIR_ENV: ""}):
                self.assertEqual(wr.locate_storage(), expected)

    def test_registry_root_is_used_when_env_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            expected = self._storage(Path(tmp) / "custom")
            with mock.patch.object(wr, "_documents_dir", return_value=None), \
                    mock.patch.object(wr, "_registry_storage_roots", return_value=[Path(tmp) / "custom"]), \
                    mock.patch.object(wr, "DEFAULT_STORAGE_ROOT", Path(tmp) / "missing"), \
                    mock.patch.dict(os.environ, {wr.STORAGE_DIR_ENV: ""}):
                self.assertEqual(wr.locate_storage(), expected)

    def test_env_override_wins_over_other_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            expected = self._storage(Path(tmp) / "env_root")
            other = self._storage(Path(tmp) / "docs_root")
            with mock.patch.object(wr, "_documents_dir", return_value=Path(tmp) / "docs_root"), \
                    mock.patch.object(wr, "_registry_storage_roots", return_value=[]), \
                    mock.patch.object(wr, "DEFAULT_STORAGE_ROOT", Path(tmp) / "missing"), \
                    mock.patch.dict(os.environ, {wr.STORAGE_DIR_ENV: str(Path(tmp) / "env_root")}):
                self.assertEqual(wr.locate_storage(), expected)
            self.assertTrue(other.is_dir())

    def test_env_override_may_point_straight_at_db_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            expected = self._storage(Path(tmp) / "root")
            with mock.patch.dict(os.environ, {wr.STORAGE_DIR_ENV: str(expected)}):
                self.assertEqual(wr.locate_storage(), expected)

    def test_explicit_directory_still_taken_as_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(wr.locate_storage(tmp), Path(tmp))
            self.assertIsNone(wr.locate_storage(str(Path(tmp) / "nope")))

    def test_missing_storage_logs_tried_paths_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(wr, "_documents_dir", return_value=None), \
                    mock.patch.object(wr, "_registry_storage_roots", return_value=[]), \
                    mock.patch.object(wr, "DEFAULT_STORAGE_ROOT", Path(tmp) / "missing"), \
                    mock.patch.object(wr, "_PROBED_LOG_KEY", ()), \
                    mock.patch.dict(os.environ, {wr.STORAGE_DIR_ENV: ""}), \
                    self.assertLogs(wr.LOGGER, level="WARNING") as logs:
                self.assertIsNone(wr.locate_storage())
                self.assertIsNone(wr.locate_storage())
        self.assertEqual(len(logs.records), 1)
        self.assertIn(str(Path(tmp) / "missing"), logs.records[0].getMessage())
        self.assertIn(wr.STORAGE_DIR_ENV, logs.records[0].getMessage())

    def test_hint_is_short_and_points_at_env_knob(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(wr, "_documents_dir", return_value=None), \
                    mock.patch.object(wr, "_registry_storage_roots", return_value=[]), \
                    mock.patch.object(wr, "DEFAULT_STORAGE_ROOT", Path(tmp) / "missing"), \
                    mock.patch.object(wr, "legacy_client_running", return_value=False), \
                    mock.patch.dict(os.environ, {wr.STORAGE_DIR_ENV: ""}):
                hint = wr.storage_unavailable_hint()
        self.assertIn("没找到微信数据目录", hint)
        self.assertIn(wr.STORAGE_DIR_ENV, hint)
        # 试过的路径只进日志，不再铺在界面上。
        self.assertNotIn("已尝试", hint)
        self.assertNotIn(str(Path(tmp) / "missing"), hint)
        self.assertNotIn("3.x", hint)

    def test_explicit_dir_resolves_down_to_db_storage(self):
        """用户在设置页填「文件管理」里的父目录时，必须解析到真正的 db_storage。

        否则状态显示「找到了」，但 `_message_dbs()` 找的是 <storage>/message，一条都扫不到。
        """
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "Wechat files"
            storage = parent / "xwechat_files" / "wxid_oowfq34fsw9u22_bf48" / "db_storage"
            (storage / "message").mkdir(parents=True)
            self.assertEqual(wr.locate_storage(str(parent)), storage)
            # 中间几层入口都要落到同一个 db_storage
            self.assertEqual(wr.locate_storage(str(parent / "xwechat_files")), storage)
            self.assertEqual(wr.locate_storage(str(storage.parent)), storage)
            # 已经直接指到 db_storage：原样返回
            self.assertEqual(wr.locate_storage(str(storage)), storage)

    def test_explicit_dir_without_db_storage_still_returns_itself(self):
        """老布局/自定义目录兼容：目录在但解析不到 db_storage 时，仍返回该目录本身。"""
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "WeChat Files"
            (legacy / "message").mkdir(parents=True)
            self.assertEqual(wr.locate_storage(str(legacy)), legacy)

    def test_explicit_missing_dir_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(wr.locate_storage(str(Path(tmp) / "nowhere")))

    def test_clear_storage_cache_forces_three_producers_to_run_again(self):
        """改完微信存储位置点一下按钮，三份探测缓存必须立刻重算（不等 TTL）。

        setUp 把三个包装函数换成了 ``return_value=[]``，所以这里先换回真身；计数打在**内层
        producer** 上——数包装函数被调用几次证明不了缓存被清（包装每次都进），数内层才证明
        「清完缓存后真的重新探测了一遍」。
        """
        counts = {"config": 0, "process": 0, "drive": 0}

        def producer(name, value):
            def _run(*args, **kwargs):
                counts[name] += 1
                return value
            return _run

        with tempfile.TemporaryDirectory() as tmp:
            some_dir = Path(tmp)
            # 三个 producer 都返回非空结果（空结果本来就不缓存，测不出差别）。
            # 两个包装函数在非 nt 上会早退，用假的 os 模块让它们走到缓存；producer 都打了桩，
            # 不会真扫盘、也不会真起 PowerShell。
            with mock.patch.object(wr, "_wechat_config_roots", self._real_config_roots), \
                    mock.patch.object(wr, "_process_install_dirs", self._real_process_dirs), \
                    mock.patch.object(wr, "_drive_storage_roots", self._real_drive_roots), \
                    mock.patch.object(wr, "os", types.SimpleNamespace(name="nt")), \
                    mock.patch.object(wr, "_scan_wechat_config_roots", side_effect=producer("config", [some_dir])), \
                    mock.patch.object(wr, "_query_wechat_install_dirs", side_effect=producer("process", [some_dir])), \
                    mock.patch.object(wr, "_scan_drive_roots", side_effect=producer("drive", [some_dir])), \
                    mock.patch.object(wr, "_default_config_dirs", return_value=[]):
                wr.clear_storage_cache()
                wr._wechat_config_roots()
                wr._process_install_dirs()
                wr._drive_storage_roots()
                first = dict(counts)
                self.assertEqual(first, {"config": 1, "process": 1, "drive": 1})
                # 缓存期内再调：producer 不该再跑
                wr._wechat_config_roots()
                wr._process_install_dirs()
                wr._drive_storage_roots()
                self.assertEqual(counts, first, "TTL 内应当命中缓存")
                wr.clear_storage_cache()
                wr._wechat_config_roots()
                wr._process_install_dirs()
                wr._drive_storage_roots()
                self.assertEqual(counts, {"config": 2, "process": 2, "drive": 2},
                                 "清缓存后三份探测都必须重新跑一次")

    def test_diagnosis_names_the_missing_configured_dir(self):
        missing = str(Path(tempfile.gettempdir()) / "definitely-missing-wechat-dir")
        with mock.patch.dict(os.environ, {wr.STORAGE_DIR_ENV: missing}):
            text = wr._storage_diagnosis()
        self.assertIn(missing, text)
        self.assertIn("不存在", text)

    def test_diagnosis_flags_configured_dir_without_db_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "Wechat files"
            empty.mkdir()
            with mock.patch.dict(os.environ, {wr.STORAGE_DIR_ENV: str(empty)}):
                text = wr._storage_diagnosis()
        self.assertIn(str(empty), text)
        self.assertIn("db_storage", text)

    def test_diagnosis_names_ini_path_without_db_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            ini_root = Path(tmp) / "Wechat files"
            ini_root.mkdir()
            with mock.patch.dict(os.environ, {wr.STORAGE_DIR_ENV: ""}), \
                    mock.patch.object(wr, "_wechat_config_roots", return_value=[ini_root]):
                text = wr._storage_diagnosis()
        self.assertIn(str(ini_root), text)
        self.assertIn("微信设置里记的是", text)

    def test_hint_points_at_settings_page_and_needs_no_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(wr, "_documents_dir", return_value=None), \
                    mock.patch.object(wr, "_registry_storage_roots", return_value=[]), \
                    mock.patch.object(wr, "DEFAULT_STORAGE_ROOT", Path(tmp) / "missing"), \
                    mock.patch.object(wr, "legacy_client_running", return_value=False), \
                    mock.patch.dict(os.environ, {wr.STORAGE_DIR_ENV: ""}):
                hint = wr.storage_unavailable_hint()
        self.assertIn("微信数据目录", hint)
        self.assertIn("立即生效", hint)
        self.assertIn("不用重启", hint)
        self.assertNotIn("已尝试", hint)

    def test_process_discovery_runs_before_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp) / "Documents"
            from_process = self._storage(Path(tmp) / "from_process")
            from_registry = self._storage(Path(tmp) / "from_registry")
            with mock.patch.object(wr, "_process_storage_roots", return_value=[Path(tmp) / "from_process"]), \
                    mock.patch.object(wr, "_documents_dir", return_value=docs), \
                    mock.patch.object(wr, "_registry_storage_roots", return_value=[Path(tmp) / "from_registry"]), \
                    mock.patch.object(wr, "DEFAULT_STORAGE_ROOT", Path(tmp) / "missing"), \
                    mock.patch.dict(os.environ, {wr.STORAGE_DIR_ENV: ""}):
                self.assertEqual(wr.locate_storage(), from_process)
            self.assertTrue(from_registry.is_dir())

    def test_process_roots_cover_install_siblings_and_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = Path(tmp) / "Tencent" / "Weixin"
            docs = Path(tmp) / "Documents"
            with mock.patch.object(wr, "_process_install_dirs", return_value=[install]), \
                    mock.patch.object(wr, "_documents_dir", return_value=docs):
                roots = wr._process_storage_roots()
        texts = [str(root) for root in roots]
        self.assertIn(str(install), texts)
        self.assertIn(str(install.parent), texts)
        self.assertIn(str(install / "xwechat_files"), texts)
        self.assertIn(str(install.parent / "WeChat Files"), texts)
        self.assertIn(str(docs / "xwechat_files"), texts)

    def test_default_root_survives_discovery_source_crash(self):
        """发现层抛异常时，默认盘候选仍要留在列表里（最后一个兜底不能被拖丢）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "default_root"
            expected = self._storage(root)
            with mock.patch.object(wr, "_process_storage_roots", side_effect=RuntimeError("进程层坏了")), \
                    mock.patch.object(wr, "_registry_storage_roots", side_effect=RuntimeError("注册表坏了")), \
                    mock.patch.object(wr, "_documents_dir", return_value=None), \
                    mock.patch.object(wr, "DEFAULT_STORAGE_ROOT", root), \
                    mock.patch.dict(os.environ, {wr.STORAGE_DIR_ENV: ""}), \
                    self.assertLogs(wr.LOGGER, level="WARNING"):
                self.assertEqual(wr.locate_storage(), expected)

    @unittest.skipUnless(os.name == "nt", "Windows 注册表语义（%USERPROFILE% 展开、winreg）")
    def test_registry_expands_environment_placeholders(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_winreg = mock.MagicMock()
            fake_winreg.HKEY_CURRENT_USER = object()
            key = mock.MagicMock()
            fake_winreg.OpenKey.return_value.__enter__.return_value = key
            fake_winreg.QueryInfoKey.return_value = (0, 1, 0)
            fake_winreg.EnumValue.return_value = ("FileSavePath", r"%USERPROFILE%\Documents\xwechat_files", 1)
            with mock.patch.object(wr.os, "name", "nt"), \
                    mock.patch.dict(sys.modules, {"winreg": fake_winreg}), \
                    mock.patch.dict(os.environ, {"USERPROFILE": str(Path(tmp) / "demo")}):
                roots = wr._registry_storage_roots()
        self.assertIn(Path(tmp) / "demo" / "Documents" / "xwechat_files", roots)

    def test_falls_back_to_original_candidates_when_discovery_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp) / "Documents"
            expected = self._storage(docs / "xwechat_files")
            with mock.patch.object(wr, "_process_storage_roots", return_value=[]), \
                    mock.patch.object(wr, "_documents_dir", return_value=docs), \
                    mock.patch.object(wr, "_registry_storage_roots", return_value=[]), \
                    mock.patch.object(wr, "DEFAULT_STORAGE_ROOT", Path(tmp) / "missing"), \
                    mock.patch.dict(os.environ, {wr.STORAGE_DIR_ENV: ""}):
                self.assertEqual(wr.locate_storage(), expected)

    def test_hint_points_out_legacy_wechat(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(wr, "_documents_dir", return_value=None), \
                    mock.patch.object(wr, "_registry_storage_roots", return_value=[]), \
                    mock.patch.object(wr, "DEFAULT_STORAGE_ROOT", Path(tmp) / "missing"), \
                    mock.patch.object(wr, "legacy_client_running", return_value=True), \
                    mock.patch.dict(os.environ, {wr.STORAGE_DIR_ENV: ""}):
                hint = wr.storage_unavailable_hint()
        self.assertIn("微信 3.x", hint)
        self.assertIn("微信 4.0", hint)


class ProcessInstallDirTests(unittest.TestCase):
    """Weixin.exe / WeChat.exe 的 ExecutablePath 解析：成功拿目录，PowerShell 失败静默返回空。"""

    def setUp(self):
        ttl_cache.clear(wr.PROCESS_CACHE_KEY)
        self.addCleanup(ttl_cache.clear, wr.PROCESS_CACHE_KEY)

    @unittest.skipUnless(os.name == "nt", "Windows 盘符路径语义：C:\\... 在 posix 上不是绝对路径")
    def test_reads_executable_path(self):
        exe = r"C:\Program Files\Tencent\Weixin\Weixin.exe"
        ok = mock.Mock(returncode=0, stdout=exe + "\r\n", stderr="")
        with mock.patch.object(wr.os, "name", "nt"), \
                mock.patch.object(wr.subprocess, "run", return_value=ok):
            self.assertEqual(wr._process_install_dirs(), [Path(r"C:\Program Files\Tencent\Weixin")])

    def test_survives_powershell_failure(self):
        failure = mock.Mock(returncode=1, stdout="", stderr="denied")
        with mock.patch.object(wr.os, "name", "nt"), \
                mock.patch.object(wr.subprocess, "run", return_value=failure):
            self.assertEqual(wr._process_install_dirs(), [])
        ttl_cache.clear(wr.PROCESS_CACHE_KEY)
        with mock.patch.object(wr.os, "name", "nt"), \
                mock.patch.object(wr.subprocess, "run", side_effect=OSError("powershell 不在")):
            self.assertEqual(wr._process_install_dirs(), [])
        ttl_cache.clear(wr.PROCESS_CACHE_KEY)
        with mock.patch.object(wr.os, "name", "nt"), \
                mock.patch.object(wr.subprocess, "run", side_effect=wr.subprocess.TimeoutExpired("powershell", 20)):
            self.assertEqual(wr._process_install_dirs(), [])

    def test_output_decode_error_does_not_bubble(self):
        """PowerShell 输出解不了码时是 ValueError（UnicodeDecodeError），不能冒到 locate_storage。"""
        with mock.patch.object(wr.os, "name", "nt"), \
                mock.patch.object(wr.subprocess, "run", side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")):
            self.assertEqual(wr._process_install_dirs(), [])
        ttl_cache.clear(wr.PROCESS_CACHE_KEY)
        with mock.patch.object(wr.os, "name", "nt"), \
                mock.patch.object(wr.subprocess, "run", side_effect=ValueError("boom")):
            self.assertEqual(wr._process_install_dirs(), [])

    def test_relative_executable_path_is_skipped(self):
        ok = mock.Mock(returncode=0, stdout="Weixin.exe\r\n..\\WeChat.exe\r\n", stderr="")
        with mock.patch.object(wr.os, "name", "nt"), \
                mock.patch.object(wr.subprocess, "run", return_value=ok):
            self.assertEqual(wr._process_install_dirs(), [])

    def test_failure_is_not_cached(self):
        """失败的空结果不缓存：用户点「重新检测」后要能立刻重跑（cache_empty=False）。"""
        failure = mock.Mock(returncode=1, stdout="", stderr="")
        with mock.patch.object(wr.os, "name", "nt"), \
                mock.patch.object(wr.subprocess, "run", return_value=failure) as runner:
            self.assertEqual(wr._process_install_dirs(), [])
            self.assertEqual(wr._process_install_dirs(), [])
        self.assertEqual(runner.call_count, 2)

    @unittest.skipUnless(os.name == "nt", "Windows 盘符路径语义：C:\\... 在 posix 上不是绝对路径")
    def test_success_is_cached(self):
        exe = r"C:\Program Files\Tencent\Weixin\Weixin.exe"
        ok = mock.Mock(returncode=0, stdout=exe + "\r\n", stderr="")
        with mock.patch.object(wr.os, "name", "nt"), \
                mock.patch.object(wr.subprocess, "run", return_value=ok) as runner:
            first = wr._process_install_dirs()
            second = wr._process_install_dirs()
        self.assertEqual(first, second)
        self.assertEqual(runner.call_count, 1)


class ProcessDetectionTests(unittest.TestCase):
    def test_find_pids_returns_weixin_pids(self):
        with mock.patch.object(wr.os, "name", "nt"), \
                mock.patch.object(wr, "_tasklist_pids", side_effect=lambda image: [4321] if image == "Weixin.exe" else []):
            self.assertEqual(wr.find_pids(), [4321])

    def test_find_pids_explains_legacy_client(self):
        with mock.patch.object(wr.os, "name", "nt"), \
                mock.patch.object(wr, "_tasklist_pids", side_effect=lambda image: [99] if image == "WeChat.exe" else []):
            with self.assertRaises(RuntimeError) as ctx:
                wr.find_pids()
        self.assertIn("微信 3.x", str(ctx.exception))
        self.assertIn("微信 4.0", str(ctx.exception))

    def test_find_pids_explains_missing_process(self):
        with mock.patch.object(wr.os, "name", "nt"), \
                mock.patch.object(wr, "_tasklist_pids", return_value=[]):
            with self.assertRaises(RuntimeError) as ctx:
                wr.find_pids()
        self.assertIn("Weixin.exe", str(ctx.exception))


class ManualKeyTests(unittest.TestCase):
    """issue #5 的 a 层：.env 手填 raw key（自动提取解不开时的唯一出路）。"""

    def setUp(self):
        wr._KEY_CACHE.clear()
        wr._record_key_failure("", "")
        self.addCleanup(wr._KEY_CACHE.clear)
        self.addCleanup(wr._record_key_failure, "", "")

    def test_parse_accepts_upper_case_and_whitespace(self):
        raw = bytes(range(32))
        self.assertEqual(wr.parse_manual_key("  %s\n" % raw.hex().upper()), raw)
        self.assertIsNone(wr.parse_manual_key(""))
        self.assertIsNone(wr.parse_manual_key(None))

    def test_parse_rejects_wrong_length_with_actionable_message(self):
        with self.assertRaises(ValueError) as ctx:
            wr.parse_manual_key("abcd1234")
        self.assertIn("QQ_DIGEST_WECHAT_KEY", str(ctx.exception))
        self.assertIn("64 位十六进制", str(ctx.exception))
        self.assertNotIn("abcd1234", str(ctx.exception))  # 报错里别回显密钥

    def test_resolve_key_uses_env_key_without_scanning_memory(self):
        key = bytes(range(32))
        with tempfile.TemporaryDirectory() as tmp:
            storage = make_storage(tmp, message_dbs=[("message_0.db", b"\x00" * 4096)])
            with mock.patch.object(wr, "_key_still_valid", return_value=True) as valid, \
                    mock.patch.object(wr, "_find_key") as find, \
                    mock.patch.dict(os.environ, {"QQ_DIGEST_WECHAT_KEY": key.hex()}):
                self.assertEqual(wr.resolve_key(storage), key)
        valid.assert_called_once()
        find.assert_not_called()

    def test_resolve_key_rejects_manual_key_that_does_not_fit(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = make_storage(tmp, message_dbs=[("message_0.db", b"\x00" * 4096)])
            with mock.patch.object(wr, "_key_still_valid", return_value=False), \
                    mock.patch.object(wr, "_find_key") as find, \
                    mock.patch.dict(os.environ, {"QQ_DIGEST_WECHAT_KEY": bytes(range(32)).hex()}):
                with self.assertRaises(ValueError) as ctx:
                    wr.resolve_key(storage)
        self.assertIn("QQ_DIGEST_WECHAT_KEY", str(ctx.exception))
        self.assertEqual(wr.last_key_failure().get("category"), "bad_manual_key")
        find.assert_not_called()

    def test_resolve_key_without_target_db_reports_storage_problem_not_key_error(self):
        """目标库不存在时（目录没配置/微信没登录），不能把手填 key 说成解不开（自查发现）。"""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp) / "nowhere"
            with mock.patch.object(wr, "_key_still_valid", return_value=False), \
                    mock.patch.object(wr, "_find_key") as find, \
                    mock.patch.dict(os.environ, {"QQ_DIGEST_WECHAT_KEY": bytes(range(32)).hex()}):
                with self.assertRaises(wr.WechatScanError) as ctx:
                    wr.resolve_key(storage)
        self.assertIn("微信数据目录", str(ctx.exception))
        self.assertEqual(wr.last_key_failure().get("category"), "no_database")
        self.assertIn("数据目录", wr.scan_error_hint("no_database"))
        find.assert_not_called()


class DriveRootDiscoveryTests(unittest.TestCase):
    """issue #5 的 c 层：任意盘符下的 xwechat_files 也要能自动找到。"""

    def setUp(self):
        config = mock.patch.object(wr, "_wechat_config_roots", return_value=[])
        config.start()
        self.addCleanup(config.stop)

    def test_drive_roots_are_probed_for_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            expected = Path(tmp) / "xwechat_files" / "wxid_demo" / "db_storage"
            expected.mkdir(parents=True)
            with mock.patch.object(wr, "_drive_storage_roots", return_value=[Path(tmp) / "xwechat_files"]), \
                    mock.patch.object(wr, "_process_install_dirs", return_value=[]), \
                    mock.patch.object(wr, "_registry_storage_roots", return_value=[]), \
                    mock.patch.object(wr, "_documents_dir", return_value=None), \
                    mock.patch.object(wr, "DEFAULT_STORAGE_ROOT", Path(tmp) / "missing"), \
                    mock.patch.dict(os.environ, {wr.STORAGE_DIR_ENV: ""}):
                self.assertEqual(wr.locate_storage(), expected)

    def test_drive_roots_are_probed_before_default_root(self):
        drive = Path("Z:/xwechat_files")
        with mock.patch.object(wr, "_drive_storage_roots", return_value=[drive]), \
                mock.patch.object(wr, "_process_install_dirs", return_value=[]), \
                mock.patch.object(wr, "_registry_storage_roots", return_value=[]), \
                mock.patch.object(wr, "_documents_dir", return_value=None), \
                mock.patch.dict(os.environ, {wr.STORAGE_DIR_ENV: ""}):
            roots = wr._storage_root_candidates()
        self.assertIn(drive, roots)
        self.assertIn(wr.DEFAULT_STORAGE_ROOT, roots)
        self.assertLess(roots.index(drive), roots.index(wr.DEFAULT_STORAGE_ROOT))


class WechatConfigRootTests(unittest.TestCase):
    """微信 4.x 的 storage ini：用户把位置改到自定义名/中文目录后，靠它才找得到。"""

    def setUp(self):
        ttl_cache.clear(wr._CONFIG_ROOTS_CACHE_KEY)

    def tearDown(self):
        ttl_cache.clear(wr._CONFIG_ROOTS_CACHE_KEY)

    def _config_dir(self, root: Path) -> Path:
        config = root / "config"
        config.mkdir(parents=True, exist_ok=True)
        return config

    @staticmethod
    def _write_ini(config: Path, name: str, text: str, encoding: str = "utf-8") -> None:
        (config / name).write_bytes(text.encode(encoding))

    def test_ini_pointing_at_custom_dir_is_discovered(self):
        """ini 指向名字里没有 wechat、也不在盘根的自定义目录时，仍要能定位 db_storage。"""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            custom = base / "我的微信资料"
            storage = custom / "xwechat_files" / "wxid_demo" / "db_storage"
            storage.mkdir(parents=True)
            config = self._config_dir(base / "cfg")
            self._write_ini(config, "abc.ini", str(custom))

            with mock.patch.object(wr, "_default_config_dirs", return_value=[config]), \
                    mock.patch.object(wr, "_process_install_dirs", return_value=[]), \
                    mock.patch.object(wr, "_registry_storage_roots", return_value=[]), \
                    mock.patch.object(wr, "_documents_dir", return_value=None), \
                    mock.patch.object(wr, "_drive_storage_roots", return_value=[]), \
                    mock.patch.object(wr, "DEFAULT_STORAGE_ROOT", base / "missing"), \
                    mock.patch.dict(os.environ, {wr.STORAGE_DIR_ENV: ""}):
                self.assertEqual(wr.locate_storage(), storage)

    def test_ini_comes_after_env_but_before_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            custom = base / "custom_store"
            custom.mkdir()
            config = self._config_dir(base / "cfg")
            self._write_ini(config, "a.ini", str(custom))
            env_root = base / "env_root"
            env_root.mkdir()
            registry = base / "from_registry"
            registry.mkdir()

            with mock.patch.object(wr, "_default_config_dirs", return_value=[config]), \
                    mock.patch.object(wr, "_process_storage_roots", return_value=[]), \
                    mock.patch.object(wr, "_registry_storage_roots", return_value=[registry]), \
                    mock.patch.object(wr, "_documents_dir", return_value=None), \
                    mock.patch.object(wr, "_drive_storage_roots", return_value=[]), \
                    mock.patch.dict(os.environ, {wr.STORAGE_DIR_ENV: str(env_root)}):
                roots = wr._storage_root_candidates()

        self.assertEqual(roots[0], env_root)
        self.assertLess(roots.index(env_root), roots.index(custom))
        self.assertLess(roots.index(custom), roots.index(registry), "ini 是微信自己写的权威值，排在注册表前")

    def test_garbage_relative_and_missing_entries_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = self._config_dir(base)
            self._write_ini(config, "junk.ini", "这不是路径\n第二行")
            self._write_ini(config, "relative.ini", "wechat_files")
            self._write_ini(config, "missing.ini", str(base / "does_not_exist"))
            (config / "empty.ini").write_bytes(b"")

            self.assertEqual(wr._wechat_config_roots(config_dirs=[config]), [])

    def test_unreadable_config_dir_does_not_raise(self):
        """配置目录读不动只能 warning：发现层其它候选必须照常工作。"""
        fake = mock.MagicMock()
        fake.is_dir.return_value = True
        fake.iterdir.side_effect = OSError("拒绝访问")
        self.assertEqual(wr._wechat_config_roots(config_dirs=[fake]), [])

        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp) / "Documents"
            expected = docs / "xwechat_files" / "wxid_demo" / "db_storage"
            expected.mkdir(parents=True)
            with mock.patch.object(wr, "_default_config_dirs", return_value=[fake]), \
                    mock.patch.object(wr, "_process_install_dirs", return_value=[]), \
                    mock.patch.object(wr, "_registry_storage_roots", return_value=[]), \
                    mock.patch.object(wr, "_documents_dir", return_value=docs), \
                    mock.patch.object(wr, "_drive_storage_roots", return_value=[]), \
                    mock.patch.object(wr, "DEFAULT_STORAGE_ROOT", Path(tmp) / "missing"), \
                    mock.patch.dict(os.environ, {wr.STORAGE_DIR_ENV: ""}), \
                    self.assertLogs(wr.LOGGER, level="WARNING") as logs:
                self.assertEqual(wr.locate_storage(), expected)
        self.assertTrue(any("读取微信设置目录失败" in line for line in logs.output))

    def test_gbk_encoded_chinese_path_is_decoded(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            chinese = base / "微信文件目录"
            chinese.mkdir()
            config = self._config_dir(base / "cfg")
            self._write_ini(config, "gbk.ini", str(chinese), encoding="gbk")

            self.assertEqual(wr._wechat_config_roots(config_dirs=[config]), [chinese])

    def test_trailing_newline_and_quotes_are_stripped(self):
        """微信写的 ini 可能带换行/引号：去掉后仍要当成有效路径。"""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            target = base / "Wechat files"
            target.mkdir()
            config = self._config_dir(base / "cfg")
            self._write_ini(config, "quoted.ini", '"%s"\r\n' % target)

            self.assertEqual(wr._wechat_config_roots(config_dirs=[config]), [target])


class StorageBusyTests(unittest.TestCase):
    """库被别的进程占住时，必须报「被占用」，绝不能报成「密钥不对」。"""

    KEY_HEX = "ab" * 32

    def _storage(self, tmp):
        # 内容不重要：读路径要么被打桩、要么被独占句柄挡住
        return make_storage(tmp, message_dbs=[("message_0.db", b"\x00" * 4096)])

    def test_permission_error_is_reported_as_busy_not_bad_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = self._storage(tmp)
            with mock.patch.dict(os.environ, {"QQ_DIGEST_WECHAT_KEY": self.KEY_HEX}, clear=False), mock.patch.object(
                wr, "_read_shared", side_effect=PermissionError(13, "另一个程序正在使用此文件")
            ):
                with self.assertRaises(wr.WechatScanError) as caught:
                    wr.resolve_key(storage)
        self.assertEqual(caught.exception.category, "storage_busy")
        self.assertNotEqual(caught.exception.category, "bad_manual_key")
        self.assertIn("被占用", str(caught.exception))
        self.assertIn("被占用", wr.scan_error_hint("storage_busy"))
        self.assertIn("退出微信", wr.scan_error_hint("storage_busy"))

    def test_oserror_errno_32_is_classified_as_storage_busy(self):
        self.assertEqual(wr.classify_scan_error(OSError(32, "另一个程序正在使用此文件")), "storage_busy")
        self.assertEqual(wr.classify_scan_error(PermissionError(13, "denied")), "storage_busy")
        self.assertEqual(wr.classify_scan_error(OSError(5, "I/O error")), "storage_busy")
        # 真正的密钥报错仍然归到 bad_manual_key，别把两类混在一起
        self.assertEqual(
            wr.classify_scan_error(ValueError("QQ_DIGEST_WECHAT_KEY 填的密钥解不开当前微信数据库")),
            "bad_manual_key",
        )

    def test_busy_hint_is_used_when_storage_found_but_locked(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = self._storage(tmp)
            with mock.patch.object(wr, "_storage_root_candidates", return_value=[storage]), mock.patch.object(
                wr, "_read_shared", side_effect=PermissionError(13, "另一个程序正在使用此文件")
            ):
                self.assertIn("被占用", wr.storage_unavailable_hint())

    @unittest.skipUnless(os.name == "nt", "独占句柄只在 Windows 上能造")
    def test_exclusive_handle_is_reported_as_busy(self):
        import ctypes

        with tempfile.TemporaryDirectory() as tmp:
            storage = self._storage(tmp)
            db = storage / "message" / "message_0.db"
            handle = ctypes.windll.kernel32.CreateFileW(
                str(db), 0x80000000, 0, None, 3, 0x80, None  # GENERIC_READ, share=0, OPEN_EXISTING
            )
            self.assertNotEqual(handle, ctypes.c_void_p(-1).value, "造独占句柄失败")
            try:
                with mock.patch.dict(os.environ, {"QQ_DIGEST_WECHAT_KEY": self.KEY_HEX}, clear=False):
                    with self.assertRaises(wr.WechatScanError) as caught:
                        wr.resolve_key(storage)
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        self.assertEqual(caught.exception.category, "storage_busy")
        self.assertIn("被占用", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
