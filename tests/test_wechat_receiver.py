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
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

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

    def test_find_key_without_process_logs_and_returns_none(self):
        key = bytes(range(32))
        with tempfile.TemporaryDirectory() as tmp:
            storage = make_storage(tmp, message_dbs=[("message_0.db", b"\x00" * 4096)])
            with mock.patch.object(wr, "_read_shared", return_value=self._page_for_key(key)):
                with mock.patch.object(wr, "find_pids", side_effect=RuntimeError("none")):
                    with self.assertLogs(wr.LOGGER, level="WARNING") as logs:
                        self.assertIsNone(wr._find_key(storage))
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

    def test_find_key_no_candidates_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = make_storage(tmp, message_dbs=[("message_0.db", b"\x00" * 4096)])
            with mock.patch.object(wr, "_read_shared", return_value=bytes(4096)):
                with mock.patch.object(wr, "find_pids", return_value=[1]):
                    with mock.patch.object(wr, "scan_process", return_value=set()):
                        with self.assertLogs(wr.LOGGER, level="WARNING") as logs:
                            self.assertIsNone(wr._find_key(storage))
        self.assertIn("未找到可解开 message_0.db 的密钥", "\n".join(logs.output))

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
    """微信 4.0 数据目录发现：环境变量 / 注册表 / 用户文档 / 默认路径，以及失败文案。"""

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

    def test_hint_lists_tried_paths_and_env_knob(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(wr, "_documents_dir", return_value=None), \
                    mock.patch.object(wr, "_registry_storage_roots", return_value=[]), \
                    mock.patch.object(wr, "DEFAULT_STORAGE_ROOT", Path(tmp) / "missing"), \
                    mock.patch.object(wr, "legacy_client_running", return_value=False), \
                    mock.patch.dict(os.environ, {wr.STORAGE_DIR_ENV: ""}):
                hint = wr.storage_unavailable_hint()
        self.assertIn("已尝试", hint)
        self.assertIn(str(Path(tmp) / "missing"), hint)
        self.assertIn(wr.STORAGE_DIR_ENV, hint)
        self.assertNotIn("3.x", hint)

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


if __name__ == "__main__":
    unittest.main()
