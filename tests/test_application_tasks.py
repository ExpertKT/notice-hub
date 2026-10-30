"""待办用例的参数检查。路由回归仍在 tests/test_webapp.py。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from noticehub.application.tasks import set_task_urgent, task_id_text  # noqa: E402
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
