"""群消息检索（qq_live_digest/search.py）的回归测试。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qq_live_digest import search as sr
from qq_live_digest.store import Store


class SearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "notice.db"
        self.store = Store(self.db)

    def add(self, msg_id: str, content: str, *, group_id: str = "g1", ts: str = "") -> None:
        self.store.insert_message(
            msg_id=msg_id,
            group_id=group_id,
            group_name="技术群" if group_id == "g1" else "闲聊群",
            sender_name="张三",
            content=content,
            ts=ts or "2026-10-24 10:00:00",
        )

    def test_indexes_existing_messages_and_catches_up_incrementally(self) -> None:
        """先有消息后建索引也要能搜到；之后新消息只补新增部分。"""
        self.add("m1", "群里有人问显卡价格，4090 现在多少钱")
        self.add("m2", "今天天气不错，出去走走", group_id="g2")

        first = sr.ensure_index(self.db)
        self.assertGreater(first, 0)

        hits = sr.search(self.db, "显卡价格")
        self.assertTrue(hits["ok"])
        self.assertEqual(hits["mode"], "fts")
        self.assertEqual(hits["total"], 1)
        self.assertEqual([hit["msg_id"] for hit in hits["hits"]], ["m1"])
        self.assertIn("显卡价格", hits["hits"][0]["snippet"])

        # 索引建好之后再来一条，增量追平要能搜到
        self.add("m3", "另一个群在讨论显卡价格走势", group_id="g2")
        sr.ensure_index(self.db)
        hits = sr.search(self.db, "显卡价格")
        self.assertEqual(hits["total"], 2)
        self.assertIn("m3", [hit["msg_id"] for hit in hits["hits"]])

    def test_two_character_chinese_query_falls_back_to_like(self) -> None:
        """trigram 命不中 2 个字的词，短查询必须退回 LIKE，不能假阴性。"""
        self.add("m1", "显卡又涨价了")
        result = sr.search(self.db, "显卡")
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "like")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["hits"][0]["msg_id"], "m1")

    def test_group_and_time_filters(self) -> None:
        self.add("m1", "显卡价格讨论", group_id="g1", ts="2026-10-20 09:00:00")
        self.add("m2", "显卡价格讨论", group_id="g2", ts="2026-10-22 09:00:00")
        self.add("m3", "显卡价格讨论", group_id="g1", ts="2026-10-24 09:00:00")

        by_group = sr.search(self.db, "显卡价格", group_id="g1")
        self.assertEqual(by_group["total"], 2)

        by_time = sr.search(
            self.db,
            "显卡价格",
            since="2026-10-21 00:00:00",
            until="2026-10-23 00:00:00",
        )
        self.assertEqual([hit["msg_id"] for hit in by_time["hits"]], ["m2"])

    def test_empty_and_overlong_query_return_empty_without_touching_db(self) -> None:
        self.add("m1", "显卡价格讨论")
        for query in ("", "   ", "\t\n"):
            result = sr.search(self.db, query)
            self.assertTrue(result["ok"])
            self.assertEqual(result["mode"], "none")
            self.assertEqual(result["hits"], [])

        # 超长查询不炸，也不该把库锁死
        long_query = "显卡价格" * 500
        result = sr.search(self.db, long_query)
        self.assertTrue(result["ok"])
        self.assertEqual(result["hits"], [])

    def test_query_with_fts_syntax_characters_is_treated_as_literal(self) -> None:
        """用户输入里的 " * OR NEAR( 不能被当 FTS5 语法，也不能抛异常。"""
        self.add("m1", '有人在群里发了一句 "显卡 OR 价格" 还有 NEAR( 符号')
        for query in ('"', "*", "OR", "NEAR(", '"显卡 OR 价格"'):
            result = sr.search(self.db, query)
            self.assertTrue(result["ok"], query)
            self.assertIsNone(result["error"], query)

        result = sr.search(self.db, '"显卡 OR 价格"')
        self.assertGreaterEqual(result["total"], 1)

    def test_search_creates_index_when_missing(self) -> None:
        """索引不存在时 search() 自己建，不需要调用方先 ensure_index。"""
        self.add("m1", "显卡价格讨论")
        with self.store._connect() as connection:
            connection.execute("DROP TABLE IF EXISTS messages_fts")
            connection.execute("DROP TABLE IF EXISTS search_state")

        result = sr.search(self.db, "显卡价格")
        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 1)

    def test_rebuild_drops_stale_index_rows(self) -> None:
        """重建索引后，已删消息不该再出现在结果里。"""
        self.add("m1", "显卡价格讨论")
        self.add("m2", "显卡价格讨论")
        self.assertEqual(sr.search(self.db, "显卡价格")["total"], 2)

        with self.store._connect() as connection:
            connection.execute("DELETE FROM messages WHERE msg_id = 'm2'")
        sr.ensure_index(self.db, rebuild=True)
        result = sr.search(self.db, "显卡价格")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["hits"][0]["msg_id"], "m1")

    def test_read_only_database_reports_error_instead_of_raising(self) -> None:
        """库文件只读时返回 ok=False + 原因，不能把异常抛给界面。"""
        self.add("m1", "显卡价格讨论")
        with patch.object(sr, "ensure_index", side_effect=sr.SearchError("消息索引不可用：attempt to write a readonly database")):
            result = sr.search(self.db, "显卡价格")
        self.assertFalse(result["ok"])
        self.assertIn("readonly", result["error"])
        self.assertEqual(result["hits"], [])

    def test_message_search_class_binds_path(self) -> None:
        self.add("m1", "显卡价格讨论")
        searcher = sr.MessageSearch(self.db)
        self.assertGreater(searcher.ensure_index(), 0)
        result = searcher.search("显卡价格", limit=10)
        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 1)

    def test_unopenable_database_raises_search_error(self) -> None:
        """库根本打不开（目录不存在）时也必须是 SearchError，不能漏裸 sqlite3 异常。"""
        missing = Path(self._tmp.name) / "no-such-dir" / "notice.db"
        with self.assertRaises(sr.SearchError):
            sr.ensure_index(missing)

    def test_does_not_hook_into_existing_flows(self) -> None:
        """默认关闭：没人调用时不建索引表。"""
        self.add("m1", "显卡价格讨论")
        with self.store._connect() as connection:
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                )
            }
        self.assertNotIn("messages_fts", names)


class TrigramAvailabilityTests(unittest.TestCase):
    def test_bundled_sqlite_has_trigram_tokenizer(self) -> None:
        """trigram 只在 SQLite 3.34+ 有；没有就别用 FTS 路径。"""
        self.assertGreaterEqual(sqlite3.sqlite_version_info, (3, 34, 0), sqlite3.sqlite_version)


if __name__ == "__main__":
    unittest.main()