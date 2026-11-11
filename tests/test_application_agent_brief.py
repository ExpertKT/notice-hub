"""今况句子。不经过 HTTP。"""

from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from noticehub.application.agent_brief import agent_brief  # noqa: E402
from noticehub.domain.time import iso  # noqa: E402
from noticehub.store import Store  # noqa: E402

NOW = dt.datetime(2026, 11, 11, 9, 0, 0)


def _task(**overrides: object) -> dict:
    item = {
        "id": 1,
        "summary": "交实验报告",
        "deadline": "2026-11-11T18:00:00",
        "source": "qq_message",
        "status": "open",
        "urgent": None,
        "category": "info",
    }
    item.update(overrides)
    return item


class AgentBriefTest(unittest.TestCase):
    def test_today_and_unconfirmed_share_one_sentence(self) -> None:
        brief = agent_brief(
            [
                _task(id=1, summary="交实验报告"),
                _task(id=2, summary="社团报名", deadline="2026-11-11T12:00:00"),
                _task(id=3, summary="改课件", deadline="2026-11-12T09:00:00"),
                _task(id=4, status="candidate", summary="要不要报名", deadline="", source="wechat"),
            ],
            now=NOW,
        )
        self.assertEqual(brief["sentence"], "今天 2 件待办，1 条还要你确认")
        self.assertEqual(brief["action"], "confirm")
        self.assertEqual(brief["count"], 1)
        self.assertEqual([item["id"] for item in brief["items"]], [4])
        self.assertEqual(set(brief["items"][0]), {"id", "summary", "deadline", "source"})
        self.assertEqual(brief["items"][0]["source"], "wechat")
        self.assertEqual(brief["updated_at"], iso(NOW))

    def test_overdue_is_part_of_the_sentence(self) -> None:
        brief = agent_brief(
            [_task(id=7, deadline="2026-11-10T18:00:00", summary="补交")],
            now=NOW,
        )
        self.assertEqual(brief["sentence"], "今天 1 件待办，逾期 1 件")
        self.assertEqual(brief["action"], "open")
        self.assertEqual(brief["count"], 1)

    def test_open_tasks_without_a_due_date_today(self) -> None:
        brief = agent_brief(
            [_task(id=8, deadline="", summary="有空再看")],
            now=NOW,
        )
        self.assertEqual(brief["sentence"], "今天没有到期的事")
        self.assertEqual(brief["action"], "open")
        self.assertEqual(brief["count"], 0)
        self.assertEqual(brief["items"][0]["summary"], "有空再看")

    def test_finished_tasks_do_not_count(self) -> None:
        brief = agent_brief(
            [
                _task(id=1, status="done"),
                _task(id=2, status="dismissed"),
                _task(id=3, status="expired"),
            ],
            now=NOW,
        )
        self.assertEqual(brief["sentence"], "所有待办都清空了")
        self.assertEqual(brief["action"], "none")
        self.assertEqual(brief["count"], 0)
        self.assertEqual(brief["items"], [])

    def test_latest_digest_is_used_only_when_nothing_is_open(self) -> None:
        empty = agent_brief([], now=NOW, latest_digest={"item_count": 4})
        self.assertEqual(empty["sentence"], "刚整理了 4 条重点")
        self.assertEqual(empty["action"], "none")
        self.assertEqual(empty["count"], 0)
        busy = agent_brief(
            [_task(id=9, status="candidate", summary="确认一下", deadline="")],
            now=NOW,
            latest_digest={"item_count": 4},
        )
        self.assertEqual(busy["sentence"], "1 条还要你确认")

    def test_running_job_prefixes_the_same_sentence(self) -> None:
        tasks = [_task(id=5, status="candidate", summary="这场要不要去", deadline="")]
        history = agent_brief(tasks, now=NOW, job={"kind": "history", "running": True})
        self.assertEqual(history["sentence"], "正在回溯群消息。1 条还要你确认")
        self.assertEqual(history["action"], "confirm")
        classify = agent_brief(tasks, now=NOW, job={"kind": "classify", "running": False})
        self.assertEqual(classify["sentence"], "1 条还要你确认")
        idle = agent_brief([], now=NOW, job={"kind": "classify", "running": True})
        self.assertEqual(idle["sentence"], "正在提取通知。")
        self.assertEqual(idle["action"], "none")

    def test_items_stop_at_three_and_urgent_comes_first(self) -> None:
        brief = agent_brief(
            [
                _task(id=1, status="candidate", summary="甲", deadline="2026-11-11T10:00:00"),
                _task(id=2, status="candidate", summary="乙", deadline="2026-11-11T08:00:00", category="urgent"),
                _task(id=3, status="candidate", summary="丙", deadline="2026-11-11T09:00:00"),
                _task(id=4, status="candidate", summary="丁", deadline="2026-11-11T07:00:00"),
            ],
            now=NOW,
        )
        self.assertEqual(brief["count"], 4)
        self.assertEqual([item["summary"] for item in brief["items"]], ["乙", "丁", "丙"])

    def test_store_rows_use_the_same_sentence(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "brief.sqlite3")
        store.upsert_task(
            task_key="manual:aaaaaaaaaaaaaaaa",
            summary="交实验报告",
            deadline="2026-11-11T18:00:00",
            source="qq_message",
            status="open",
        )
        store.upsert_task(
            task_key="rule:1:m1",
            summary="要不要报名",
            source="wechat",
            status="candidate",
        )
        brief = agent_brief(store.list_tasks(), now=NOW)
        self.assertEqual(brief["sentence"], "今天 1 件待办，1 条还要你确认")
        self.assertEqual(brief["action"], "confirm")
        self.assertEqual(brief["items"][0]["source"], "wechat")


class AgentBriefRouteTest(unittest.TestCase):
    def test_route_table_has_no_agent_brief_yet(self) -> None:
        text = (PROJECT_ROOT / "noticehub" / "routes.py").read_text(encoding="utf-8")
        self.assertNotIn("/api/agent/brief", text)
