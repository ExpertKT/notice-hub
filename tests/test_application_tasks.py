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

import re

from noticehub.application.tasks import (  # noqa: E402
    apply_task_action,
    correct_task,
    create_task,
    list_task_board,
    set_task_pinned,
    set_task_urgent,
    task_id_text,
    task_stats,
)
from noticehub.config import Settings  # noqa: E402
from noticehub.timeutil import iso  # noqa: E402
from noticehub.store import Store  # noqa: E402


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


class CreateTaskTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "tasks.sqlite3")

    def test_folds_summary_and_normalizes_deadline(self) -> None:
        created = create_task(self.store, "  给导师  发周报  ", "2026-09-30T18:00")
        self.assertEqual(
            set(created),
            {"ok", "task_id", "summary", "deadline"},
        )
        self.assertTrue(created["ok"])
        self.assertEqual(created["summary"], "给导师 发周报")
        self.assertEqual(created["deadline"], "2026-09-30T18:00:00")
        saved = self.store.get_task(created["task_id"])
        self.assertEqual(saved["source"], "manual")
        self.assertEqual(saved["confidence"], 1.0)
        self.assertRegex(saved["task_key"], r"^manual:[0-9a-f]{16}$")
        self.assertTrue(re.search(r"页面手动新建", str(self.store.list_task_events(created["task_id"]))))

    def test_blank_deadline_stays_empty(self) -> None:
        created = create_task(self.store, "整理桌面")
        self.assertEqual(created["deadline"], "")

    def test_validation_messages(self) -> None:
        cases = (
            (12, "", "summary 必须是文本"),
            ("   ", "", "请填写待办内容"),
            ("x" * 201, "", "待办内容请控制在 200 字以内"),
            ("正常内容", 123, "deadline 必须是文本"),
            ("正常内容", "下周三", "截止时间格式不对，请重新选择"),
        )
        for summary, deadline, message in cases:
            with self.assertRaises(ValueError) as caught:
                create_task(self.store, summary, deadline)
            self.assertEqual(str(caught.exception), message)
        self.assertEqual(self.store.list_tasks(), [])


class ApplyTaskActionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "tasks.sqlite3")
        self.task_id = self.store.upsert_task(task_key="a1", summary="交报告", category="action")
        self.when = dt.datetime(2026, 9, 30, 9, 0)

    def test_missing_action_maps_done_flag(self) -> None:
        done = apply_task_action(self.store, self.task_id, "", done=True)
        self.assertEqual(done["action"], "done")
        self.assertTrue(done["ok"])
        self.assertTrue(done["done"])
        self.assertEqual(done["status"], "done")
        reopened = apply_task_action(self.store, self.task_id, "", done=False)
        self.assertEqual(reopened["action"], "reopen")
        self.assertEqual(reopened["status"], "open")
        self.assertFalse(reopened["done"])

    def test_unknown_action_raises_the_http_text(self) -> None:
        with self.assertRaises(ValueError) as caught:
            apply_task_action(self.store, self.task_id, "nope")
        self.assertEqual(str(caught.exception), "unsupported action")

    def test_snooze_uses_next_morning_when_until_is_empty(self) -> None:
        result = apply_task_action(
            self.store,
            self.task_id,
            "snooze",
            settings=Settings(),
            now=lambda: self.when,
        )
        self.assertTrue(result["ok"])
        task = self.store.get_task(self.task_id)
        self.assertEqual(task["snooze_until"], "2026-10-01T07:30:00")

    def test_delete_finished_task_and_refuse_open_one(self) -> None:
        self.store.apply_task_action(self.task_id, "done")
        removed = apply_task_action(self.store, self.task_id, "delete")
        self.assertTrue(removed["ok"])
        self.assertEqual(removed["status"], "")
        self.assertIsNone(self.store.get_task(self.task_id))
        other = self.store.upsert_task(task_key="a2", summary="还开着", category="action")
        refused = apply_task_action(self.store, other, "delete")
        self.assertFalse(refused["ok"])
        self.assertIsNotNone(self.store.get_task(other))


class CorrectTaskTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "tasks.sqlite3")

    def test_not_urgent_drops_category_and_caps_importance(self) -> None:
        task_id = self.store.upsert_task(
            task_key="c1", summary="明早截止", category="urgent", importance=5
        )
        result = correct_task(self.store, task_id, "not_urgent")
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "correct")
        self.assertEqual(result["status"], "open")
        saved = self.store.get_task(task_id)
        self.assertEqual(saved["category"], "action")
        self.assertLessEqual(saved["importance"], 3)

    def test_category_uses_the_single_importance_map(self) -> None:
        task_id = self.store.upsert_task(task_key="c2", summary="交表", category="info", importance=1)
        result = correct_task(self.store, task_id, "category", "academic")
        self.assertEqual(result["status"], "open")
        saved = self.store.get_task(task_id)
        self.assertEqual(saved["category"], "academic")
        self.assertEqual(saved["importance"], 3)

    def test_not_notice_dismisses(self) -> None:
        task_id = self.store.upsert_task(task_key="c3", summary="闲聊", category="info")
        result = correct_task(self.store, task_id, "not_notice")
        self.assertEqual(result["status"], "dismissed")
        self.assertFalse(result["done"])

    def test_bad_category_and_missing_task(self) -> None:
        task_id = self.store.upsert_task(task_key="c4", summary="交表", category="info")
        with self.assertRaises(ValueError) as caught:
            correct_task(self.store, task_id, "category", "gossip")
        self.assertEqual(str(caught.exception), "无效的任务分类")
        missing = correct_task(self.store, 999999999, "not_task")
        self.assertEqual(missing["ok"], False)
        self.assertEqual(missing["status"], "")


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
