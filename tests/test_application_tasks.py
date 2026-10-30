"""待办用例的参数检查。路由回归仍在 tests/test_webapp.py。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import datetime as dt

from noticehub.application.tasks import (  # noqa: E402
    list_task_board,
    set_task_pinned,
    set_task_urgent,
    task_id_text,
    task_stats,
)
from qq_live_digest.config import Settings  # noqa: E402
from qq_live_digest.timeutil import iso  # noqa: E402
from qq_live_digest.store import Store  # noqa: E402


class TaskIdTextTest(unittest.TestCase):
    def test_accepts_ascii_digits_in_range(self) -> None:
        self.assertEqual(task_id_text("1"), 1)
        self.assertEqual(task_id_text("18"), 18)
        self.assertEqual(task_id_text("9" * 18), int("9" * 18))

    def test_rejects_non_text_and_out_of_range(self) -> None:
        for value in (None, 12, "abc", "9" * 19, "0", "", " 1", "1 ", "+1"):
            with self.assertRaises(ValueError) as caught:
                task_id_text(value)
            self.assertEqual(str(caught.exception), "task_id 必须是正整数文本")


class SetTaskUrgentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "tasks.sqlite3")
        self.task_id = self.store.upsert_task(task_key="u1", summary="交报告", category="action")

    def test_sets_and_clears_override(self) -> None:
        saved = set_task_urgent(self.store, str(self.task_id), True)
        self.assertTrue(saved["ok"])
        self.assertEqual(saved["urgent_override"], 1)
        cleared = set_task_urgent(self.store, str(self.task_id), None)
        self.assertTrue(cleared["ok"])
        self.assertIsNone(cleared["urgent_override"])

    def test_missing_task_is_not_an_exception(self) -> None:
        result = set_task_urgent(self.store, "999999999", True)
        self.assertEqual(result, {"ok": False, "error": "任务不存在"})

    def test_bad_urgent_value_raises(self) -> None:
        with self.assertRaises(ValueError) as caught:
            set_task_urgent(self.store, str(self.task_id), 1)  # type: ignore[arg-type]
        self.assertEqual(str(caught.exception), "urgent 必须是 true、false 或 null")


class SetTaskPinnedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "tasks.sqlite3")
        self.task_id = self.store.upsert_task(task_key="p1", summary="交报告", category="action")

    def test_toggles_pin(self) -> None:
        pinned = set_task_pinned(self.store, str(self.task_id), True)
        self.assertTrue(pinned["ok"])
        self.assertTrue(pinned["pinned"])
        cleared = set_task_pinned(self.store, str(self.task_id), False)
        self.assertTrue(cleared["ok"])
        self.assertFalse(cleared["pinned"])

    def test_done_task_keeps_store_error(self) -> None:
        self.store.set_task_status(self.task_id, True)
        result = set_task_pinned(self.store, str(self.task_id), True)
        self.assertEqual(result, {"ok": False, "error": "已完成的任务不能置顶"})

    def test_missing_task_is_not_an_exception(self) -> None:
        result = set_task_pinned(self.store, "999999999", True)
        self.assertEqual(result, {"ok": False, "error": "任务不存在"})


class ListTaskBoardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "tasks.sqlite3")
        self.now = dt.datetime(2026, 9, 30, 9, 0)
        self.settings = Settings(
            group_aliases={"g1": "学院通知群"},
            wechat_group_aliases={"wx1": "微信班群"},
        )

    def test_board_keys_and_group_labels(self) -> None:
        self.store.upsert_task(
            task_key="today",
            summary="今天交",
            category="action",
            deadline=iso(self.now + dt.timedelta(hours=2)),
            groups=["g1"],
            source="qq_message",
        )
        self.store.upsert_task(
            task_key="wx",
            summary="微信里的",
            category="info",
            groups=["wx1"],
            source="wechat",
        )
        board = list_task_board(self.store, self.settings, now=self.now)
        for key in (
            "headline", "subline", "progress", "stats",
            "candidates", "today", "week", "later", "done", "pinned",
        ):
            self.assertIn(key, board)
        self.assertEqual(board["today"][0]["groups"], ["学院通知群"])
        self.assertEqual(board["later"][0]["groups"], ["微信班群"])
        self.assertIn("今天", board["headline"])
        self.assertEqual(
            set(board["stats"]),
            {"total", "candidate", "open", "done", "dismissed", "expired", "overdue"},
        )
        self.assertEqual(board["stats"], task_stats(self.store))
