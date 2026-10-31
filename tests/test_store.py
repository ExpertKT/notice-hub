"""Store 的行为回归：消息去重、投递重试、待办生命周期、纠错统计与迁移。

这些断言是契约：参数与期望值都固定，别为了让实现好写而放宽。
"""

from __future__ import annotations

import datetime as dt
import inspect
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from noticehub import store as store_module  # noqa: E402
from noticehub import summarizer  # noqa: E402
from noticehub.store import Store, effective_urgent  # noqa: E402
from noticehub.timeutil import iso, now_local  # noqa: E402

# 升级前的老库长什么样：没有 confirmed_at / last_reminded_at / pinned 这些后来的列。
LEGACY_TASKS_DDL = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_key TEXT NOT NULL UNIQUE,
    summary TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'info',
    importance INTEGER NOT NULL DEFAULT 3,
    deadline TEXT NOT NULL DEFAULT '',
    groups TEXT NOT NULL DEFAULT '[]',
    sender TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    digest_id INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    done_at TEXT NOT NULL DEFAULT ''
);
INSERT INTO tasks (task_key, summary, status, created_at, updated_at)
VALUES ('old', '旧任务', 'open', '2026-09-01 08:00:00', '2026-09-01 08:00:00');
"""

GROUP_ROWS = (
    ("a1", "g1", "g1", "2026-10-01 10:00:00"),
    ("a2", "g1", "真实群名", "2026-10-01 11:00:00"),
    ("a3", "g1", "g1", "2026-10-01 12:00:00"),
    ("b1", "g2", "g2", "2026-10-01 10:00:00"),
)


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "test.sqlite3")

    # ------------------------------------------------------------ 构造工具
    def _message(self, msg_id: str, content: str = "通知", group: str = "g1", source_text: str = "", **extra):
        return self.store.insert_message(
            msg_id=msg_id,
            group_id=group,
            content=content,
            source_text=source_text,
            sender_name="辅导员",
            group_name="学院群",
            **extra,
        )

    def _digest(self, **overrides) -> int:
        params = {
            "kind": "window",
            "window_start": "",
            "window_end": "",
            "body": "正文",
            "payload": [],
            "message_count": 1,
            "llm_used": False,
        }
        params.update(overrides)
        return self.store.insert_digest(**params)

    def _deliver(self, digest_id: int, **overrides):
        params = {
            "digest_id": digest_id,
            "channel": "qq-bot",
            "target": "openid-1",
            "dedupe_key": "digest:%d" % digest_id,
            "max_attempts": 3,
            "retry_seconds": 0,
        }
        params.update(overrides)
        return self.store.claim_delivery(**params)

    def _task(self, key: str, summary: str = "任务", **overrides) -> int:
        params = {"task_key": key, "summary": summary}
        params.update(overrides)
        return self.store.upsert_task(**params)

    # ---------------------------------------------------------------- 消息
    def test_insert_dedupes_by_msg_id(self) -> None:
        self.assertTrue(self._message("m1"))
        self.assertFalse(self._message("m1", content="重复"))
        self.assertEqual(self.store.counts()["messages"], 1)
        self.assertEqual(self.store.counts()["unprocessed"], 1)

    def test_inbox_items_include_message_source_and_event(self) -> None:
        attachment_id = "onebot:g1:attach:0123456789abcdef"
        self.store.insert_message(
            msg_id=attachment_id,
            group_id="g1",
            content="图片通知正文",
            source="onebot-attachment",
            event="GROUP_ATTACHMENT",
        )
        self.store.save_mark(attachment_id, "notice")
        item = self.store.inbox_items()[0]
        self.assertEqual(item["source"], "onebot-attachment")
        self.assertEqual(item["event"], "GROUP_ATTACHMENT")

    def test_delete_inbox_items_removes_marks_and_messages(self) -> None:
        self._message("m1")
        self._message("m2")
        self.store.save_mark("m1", "notice")
        self.store.save_mark("m2", "suspect")
        self.assertEqual(self.store.inbox_counts()["notice"], 1)

        self.assertEqual(self.store.delete_inbox_items(["m1"]), 1)

        # 两张表必须一起删干净，否则计数会比列表多出一条。
        self.assertEqual([row["msg_id"] for row in self.store.inbox_items()], ["m2"])
        self.assertEqual(self.store.counts()["messages"], 1)
        self.assertEqual(self.store.inbox_counts()["notice"], 0)
        self.assertEqual(self.store.inbox_counts()["suspect"], 1)

    def test_delete_inbox_items_is_noop_for_empty_or_unknown_ids(self) -> None:
        self._message("m1")
        self.store.save_mark("m1", "notice")
        self.assertEqual(self.store.delete_inbox_items([]), 0)
        self.assertEqual(self.store.delete_inbox_items(["没有这条"]), 0)
        self.assertEqual([row["msg_id"] for row in self.store.inbox_items()], ["m1"])
        self.assertEqual(self.store.counts()["messages"], 1)

    def test_unprocessed_and_mark_processed(self) -> None:
        self._message("m1")
        self._message("m2")
        self.assertEqual([row["msg_id"] for row in self.store.unprocessed_messages()], ["m1", "m2"])

        stamp = iso(now_local())
        digest_id = self._digest(
            window_start=stamp, window_end=stamp, payload=[{"summary": "x"}], message_count=2
        )
        self.assertGreater(digest_id, 0)
        self.store.mark_processed(["m1", "m2"], digest_id)
        self.assertEqual(self.store.unprocessed_messages(), [])
        self.assertEqual(self.store.counts()["unprocessed"], 0)

        digest = self.store.get_digest(digest_id)
        self.assertIsNotNone(digest)
        assert digest is not None
        self.assertEqual(digest["items"], [{"summary": "x"}])

    def test_jsonl_audit_written(self) -> None:
        self._message("m1")
        path = self.root / "messages.jsonl"
        self.assertTrue(path.is_file())
        self.assertIn("m1", path.read_text(encoding="utf-8"))

    def test_meta_and_prune(self) -> None:
        self.store.insert_message(
            msg_id="old",
            group_id="g1",
            content="旧消息",
            received_at=iso(now_local() - dt.timedelta(days=40)),
        )
        self.store.mark_processed(["old"], None)
        self.store.meta_set("last_prune", "2026-09-28 00:00:00")
        self.assertEqual(self.store.meta_get("last_prune"), "2026-09-28 00:00:00")

        self.assertEqual(self.store.prune(retention_days=30), 1)
        self.assertEqual(self.store.counts()["messages"], 0)

    def test_prune_drops_orphan_inbox_marks(self) -> None:
        """消息过期删了，对应的判定不能留下来把收件箱数字撑大。"""
        old = iso(now_local() - dt.timedelta(days=40))
        self.store.insert_message(msg_id="stale", group_id="g1", content="旧消息", received_at=old)
        self.store.save_mark("stale", "notice", "过期", "llm")
        self.store.insert_message(
            msg_id="fresh", group_id="g1", content="新消息", received_at=iso(now_local())
        )
        self.store.save_mark("fresh", "suspect", "还在", "llm")
        self.assertEqual(self.store.inbox_counts()["notice"], 1)

        self.assertEqual(self.store.prune(retention_days=30), 1)

        counts = self.store.inbox_counts()
        self.assertEqual(counts["notice"], 0, "过期消息的 marks 成了孤儿，计数必须归零")
        self.assertEqual(counts["suspect"], 1, "还在库里的消息不能被误删")
        self.assertEqual([row["msg_id"] for row in self.store.inbox_items()], ["fresh"])

    # ---------------------------------------------------------------- 投递
    def test_delivery_claim_dedupe_and_retry(self) -> None:
        digest_id = self._digest()
        send, delivery_id = self._deliver(digest_id)
        self.assertTrue(send)

        # 冷却窗口内同一个投递不许再发。
        send_again, same_id = self._deliver(digest_id, retry_seconds=3600)
        self.assertFalse(send_again)
        self.assertEqual(same_id, delivery_id)

        self.store.mark_delivery(delivery_id or 0, ok=True)
        send_after_sent, _ = self._deliver(digest_id)
        self.assertFalse(send_after_sent)

    def test_pending_deliveries_and_attempt_limit(self) -> None:
        digest_id = self._digest(kind="urgent", window_end="2026-09-28 18:00", body="紧急正文")
        _, delivery_id = self._deliver(
            digest_id, channel="webhook", target="example.com#abcd", max_attempts=2
        )
        self.store.mark_delivery(delivery_id or 0, ok=False, error="boom")

        pending = self.store.pending_deliveries(max_attempts=2, retry_seconds=0)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["body"], "紧急正文")

        # 次数用满以后就该从待重试里消失。
        self.store.increment_delivery_attempt(delivery_id or 0)
        self.store.mark_delivery(delivery_id or 0, ok=False, error="boom2")
        self.assertEqual(self.store.pending_deliveries(max_attempts=2, retry_seconds=0), [])

    def test_delivery_stats_counts_statuses(self) -> None:
        digest_id = self._digest(window_start=iso(now_local()), window_end=iso(now_local()))
        should_send, delivery_id = self._deliver(
            digest_id, channel="webhook", target="t1", dedupe_key="k1"
        )
        self.assertTrue(should_send)
        self.store.mark_delivery(delivery_id or 0, ok=True)

        stats = self.store.delivery_stats()
        self.assertEqual(stats["sent"], 1)
        self.assertEqual(stats["pending"], 0)

    # ---------------------------------------------------------------- 迁移
    def test_old_task_database_migrates_without_data_loss(self) -> None:
        path = self.root / "old.sqlite3"
        connection = sqlite3.connect(path)
        connection.executescript(LEGACY_TASKS_DDL)
        connection.commit()
        connection.close()

        migrated = Store(path)
        task = migrated.list_tasks()[0]
        self.assertEqual(task["summary"], "旧任务")
        self.assertEqual(task["confidence"], 0.0)

        with migrated._connect() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)").fetchall()}
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
        self.assertIn("confirmed_at", columns)
        self.assertIn("last_reminded_at", columns)
        self.assertIn("task_events", tables)

    def test_task_urgent_override_migrates_idempotently(self) -> None:
        path = self.root / "migrate.sqlite3"
        first = Store(path)
        task_id = first.upsert_task(task_key="existing", summary="已有", category="urgent")
        self.assertEqual(first.set_task_urgent(task_id, True)["urgent_override"], 1)

        second = Store(path)
        third = Store(path)
        self.assertEqual(second.get_task(task_id)["urgent_override"], 1)
        self.assertEqual(third.get_task(task_id)["summary"], "已有")

    def test_task_pinned_is_persisted_listed_and_migrated(self) -> None:
        path = self.root / "pin.sqlite3"
        first = Store(path)
        task_id = first.upsert_task(task_key="pin-me", summary="要置顶", category="action")
        self.assertFalse(first.get_task(task_id)["pinned"])

        result = first.set_task_pinned(task_id, True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["pinned"])
        self.assertTrue(first.list_tasks()[0]["pinned"])

        reopened = Store(path)
        reopened_again = Store(path)
        self.assertTrue(reopened.get_task(task_id)["pinned"])
        self.assertTrue(reopened_again.get_task(task_id)["pinned"])
        self.assertFalse(reopened.set_task_pinned(task_id, False)["pinned"])
        self.assertFalse(reopened.get_task(task_id)["pinned"])

    # ---------------------------------------------------------------- 待办
    def test_task_keeps_structured_details_and_full_source_text(self) -> None:
        source = "原文完整内容：刚转专业同学需要注意，其他同学也要注意。" + "附件细节" * 300
        self._message("m-structured", "结构化摘要：两类同学都要处理", source_text=source)
        task_id = self.store.upsert_task(
            task_key="m-structured",
            summary="两类同学都要核查",
            audience="刚转专业同学；其他同学",
            condition="没有2026年体测成绩",
            details=["刚转专业同学先查看成绩", "其他同学也要查看成绩"],
            action="查看并预约",
            category="action",
            groups=["测试群"],
        )

        task = self.store.get_task(task_id)
        assert task is not None
        self.assertEqual(task["audience"], "刚转专业同学；其他同学")
        self.assertEqual(task["condition"], "没有2026年体测成绩")
        self.assertEqual(task["details"], ["刚转专业同学先查看成绩", "其他同学也要查看成绩"])
        self.assertIn("原文完整内容", task["source_text"])

    def test_list_tasks_limit_none_returns_all_and_default_stays_capped(self) -> None:
        """统计与日历要全量；800 的默认截断只约束界面列表。"""
        default = inspect.signature(Store.list_tasks).parameters["limit"].default
        self.assertEqual(default, 800)

        for index in range(3):
            self._task("limit-%d" % index, "任务%d" % index)
        self.assertEqual(len(self.store.list_tasks(limit=2)), 2)
        self.assertEqual(len(self.store.list_tasks(limit=None)), 3)
        self.assertEqual(len(self.store.list_tasks()), 3)

    def test_task_actions_and_event_metrics(self) -> None:
        task_id = self.store.upsert_task(
            task_key="candidate-1",
            summary="可能要交报名表",
            category="action",
            groups=["班级群"],
            evidence="记得看一下报名表",
            status="candidate",
            confidence=0.61,
            source="qq_message",
            classification_reason="可能出现弱行动词",
        )
        candidate = self.store.list_tasks(statuses=("candidate",))[0]
        self.assertEqual(candidate["id"], task_id)
        self.assertEqual(candidate["confidence"], 0.61)

        self.assertTrue(self.store.apply_task_action(task_id, "confirm"))
        self.assertTrue(self.store.apply_task_action(task_id, "done"))
        self.assertTrue(self.store.apply_task_action(task_id, "reopen"))
        self.assertTrue(self.store.apply_task_action(task_id, "dismiss"))

        events = [item["event"] for item in self.store.list_task_events(task_id)]
        for expected in ("dismissed", "reopened", "done", "confirmed", "candidate_detected"):
            self.assertIn(expected, events)

        metrics = self.store.task_metrics(start="2020-01-01 00:00:00", end="2030-01-01 00:00:00")
        self.assertEqual(metrics["candidates"], 1)
        self.assertEqual(metrics["confirmed"], 1)
        self.assertEqual(metrics["dismissed"], 1)
        self.assertEqual(self.store.task_stats()["dismissed"], 1)

    def test_done_task_can_be_deleted_directly(self) -> None:
        """做完的任务可以真删，且不写 dismissed 事件去污染忽略率。"""
        task_id = self._task("done-delete", "已完成的任务")
        self.assertTrue(self.store.apply_task_action(task_id, "done"))
        self.assertTrue(self.store.apply_task_action(task_id, "delete"))

        self.assertIsNone(self.store.get_task(task_id))
        self.assertEqual(self.store.list_task_events(task_id), [])

    def test_dismiss_rules_are_unchanged(self) -> None:
        """dismiss 只收 candidate/open；done 不能被顺手改成「已忽略」。"""
        done_id = self._task("d-done", "已完成")
        self.store.apply_task_action(done_id, "done")
        self.assertFalse(self.store.apply_task_action(done_id, "dismiss"))
        self.assertEqual(self.store.get_task(done_id)["status"], "done")

        open_id = self._task("d-open", "待办")
        candidate_id = self._task("d-candidate", "候选", status="candidate")
        self.assertTrue(self.store.apply_task_action(open_id, "dismiss"))
        self.assertTrue(self.store.apply_task_action(candidate_id, "dismiss"))

        # 重复忽略不再推进状态。
        self.assertFalse(self.store.apply_task_action(open_id, "dismiss"))
        expired_id = self._task("d-expired", "过期", status="expired")
        self.assertFalse(self.store.apply_task_action(expired_id, "dismiss"))

    def test_delete_only_accepts_finished_tasks(self) -> None:
        open_id = self._task("del-open", "还在做")
        candidate_id = self._task("del-candidate", "候选", status="candidate")
        # 没走完流程的不能真删，否则待办会凭空消失。
        self.assertFalse(self.store.apply_task_action(open_id, "delete"))
        self.assertFalse(self.store.apply_task_action(candidate_id, "delete"))
        self.assertIsNotNone(self.store.get_task(open_id))
        self.assertIsNotNone(self.store.get_task(candidate_id))

        dismissed_id = self._task("del-dismissed", "已忽略")
        self.store.apply_task_action(dismissed_id, "dismiss")
        self.assertTrue(self.store.apply_task_action(dismissed_id, "delete"))

        expired_id = self._task("del-expired", "已过期", status="expired")
        self.assertTrue(self.store.apply_task_action(expired_id, "delete"))
        self.assertFalse(self.store.apply_task_action(999999, "delete"))

    def test_clear_finished_tasks_only_removes_finished_and_their_events(self) -> None:
        done_id = self._task("cf-done", "已完成")
        self.store.apply_task_action(done_id, "done")
        dismissed_id = self._task("cf-dismissed", "已忽略")
        self.store.apply_task_action(dismissed_id, "dismiss")
        expired_id = self._task("cf-expired", "已过期", status="expired")
        open_id = self._task("cf-open", "还在做")
        candidate_id = self._task("cf-candidate", "候选", status="candidate")

        self.assertEqual(self.store.clear_finished_tasks(), 3)

        remaining = {task["id"] for task in self.store.list_tasks(statuses=("open", "candidate"))}
        self.assertEqual(remaining, {open_id, candidate_id})
        for task_id in (done_id, dismissed_id, expired_id):
            self.assertIsNone(self.store.get_task(task_id))
            self.assertEqual(self.store.list_task_events(task_id), [], "事件要跟着任务一起删")
        self.assertEqual(self.store.clear_finished_tasks(), 0)

    def test_task_corrections_record_feedback_and_update_fields(self) -> None:
        urgent_id = self._task("urgent-1", "明早截止", category="urgent", importance=5)
        self.assertTrue(self.store.apply_task_correction(urgent_id, "not_urgent"))
        urgent = self.store.get_task(urgent_id)
        assert urgent is not None
        self.assertEqual(urgent["category"], "action")
        self.assertEqual(urgent["importance"], 3)

        duplicate_id = self._task("duplicate-1", "重复通知", category="info")
        self.assertTrue(self.store.apply_task_correction(duplicate_id, "duplicate"))
        duplicate = self.store.get_task(duplicate_id)
        assert duplicate is not None
        self.assertEqual(duplicate["status"], "dismissed")

        corrected = next(item for item in self.store.list_task_events(urgent_id) if item["event"] == "corrected")
        self.assertIn("not_urgent", corrected["detail"])
        self.assertIn("previous", corrected["detail"])

    def test_correction_insights_summarise_group_mistakes(self) -> None:
        for index in range(3):
            task_id = self._task(
                "u%d" % index,
                "紧急通知 %d" % index,
                category="urgent",
                groups=["新生交流群"],
            )
            self.store.apply_task_correction(task_id, "not_urgent")

        stats = self.store.correction_stats(days=30)
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["by_type"]["not_urgent"], 3)
        self.assertEqual(stats["by_group"]["新生交流群"]["not_urgent"], 3)
        self.assertTrue(
            any("新生交流群" in text for text in self.store.correction_insights(days=30, min_group_hits=3))
        )

    # ------------------------------------------------- 紧急覆盖 / 置顶 / 延后
    def test_task_urgent_override_true_is_persisted_and_listed(self) -> None:
        task_id = self._task("urgent-override", "普通任务", category="action")
        result = self.store.set_task_urgent(task_id, True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["urgent_override"], 1)

        listed = self.store.list_tasks()
        self.assertEqual(listed[0]["urgent_override"], 1)
        self.assertTrue(effective_urgent(listed[0]))

    def test_task_urgent_override_none_follows_ai_category(self) -> None:
        task_id = self._task("urgent-follow", "AI紧急", category="urgent")
        self.assertEqual(self.store.set_task_urgent(task_id, False)["urgent_override"], 0)

        restored = self.store.set_task_urgent(task_id, None)
        self.assertIsNone(restored["urgent_override"])
        self.assertTrue(effective_urgent(restored))

    def test_task_urgent_override_missing_task_returns_error(self) -> None:
        result = self.store.set_task_urgent(999999, True)
        self.assertFalse(result["ok"])
        self.assertIn("error", result)

    def test_task_pin_rejects_done_tasks_and_invalid_values(self) -> None:
        task_id = self._task("pin-done", "已完成", category="action")
        self.store.set_task_status(task_id, True)

        result = self.store.set_task_pinned(task_id, True)
        self.assertFalse(result["ok"])
        self.assertIn("置顶", result["error"])
        self.assertFalse(self.store.get_task(task_id)["pinned"])

        with self.assertRaises(ValueError):
            self.store.set_task_pinned(task_id, 1)
        self.assertFalse(self.store.set_task_pinned(999999, True)["ok"])

    def test_snooze_stores_until_and_duplicate_links_original(self) -> None:
        original_id = self._task("m1", "提交实验报告", category="action", groups=["学院群"])
        copy_id = self._task("m2", "提交实验报告", category="action", groups=["班级群"])
        until = iso(dt.datetime(2026, 10, 7, 7, 30))
        self.assertTrue(self.store.apply_task_action(original_id, "snooze", detail={"until": until}))

        snoozed = self.store.get_task(original_id)
        assert snoozed is not None
        self.assertEqual(snoozed["snooze_until"], until)

        self.assertTrue(self.store.apply_task_correction(copy_id, "duplicate"))
        copy = self.store.get_task(copy_id)
        assert copy is not None
        self.assertEqual(copy["status"], "dismissed")
        self.assertEqual(copy["duplicate_of"], original_id)
        self.assertEqual(copy["duplicate_summary"], "提交实验报告")
        self.assertEqual(self.store.get_task(original_id)["groups"], ["学院群", "班级群"])

    def test_snooze_without_until_defaults_to_next_day(self) -> None:
        task_id = self._task("m1", "待办", category="action")
        self.assertTrue(self.store.apply_task_action(task_id, "snooze"))

        snoozed = self.store.get_task(task_id)
        assert snoozed is not None
        self.assertGreater(len(snoozed["snooze_until"]), 0)
        self.assertEqual(self.store.list_task_events(task_id)[0]["event"], "snoozed")

    def test_category_importance_is_shared_by_both_entry_points(self) -> None:
        """分类到重要度只能有一份定义，两个入口必须算出同一个数。

        历史问题是两处各写一份：同一条任务 LLM 判 4，用户点一下「改成待办」就掉到 3。
        """
        expected = {"urgent": 5, "action": 4, "academic": 3, "info": 3}
        self.assertIs(summarizer.IMPORTANCE_BY_CATEGORY, store_module.IMPORTANCE_BY_CATEGORY)
        self.assertEqual(summarizer.IMPORTANCE_BY_CATEGORY, expected)

        for category, importance in expected.items():
            with self.subTest(category=category):
                task_id = self.store.upsert_task(
                    task_key="shared-%s" % category,
                    summary="分类一致 %s" % category,
                    category=category,
                    importance=summarizer.IMPORTANCE_BY_CATEGORY[category],
                )
                self.assertTrue(self.store.apply_task_correction(task_id, "category", value=category))
                task = self.store.get_task(task_id)
                assert task is not None
                self.assertEqual(task["category"], category)
                self.assertEqual(task["importance"], importance)

        # 回归点：info 手动改分类后是 3，不是历史上那个 1。
        info_id = self._task("shared-info-manual", "通知一条", category="info")
        self.assertTrue(self.store.apply_task_correction(info_id, "category", value="info"))
        info = self.store.get_task(info_id)
        assert info is not None
        self.assertEqual(info["importance"], 3)

    # ---------------------------------------------------------------- 群名
    def test_group_names_picks_latest_named_message_and_prefers_cache(self) -> None:
        with self.store._connect() as connection:
            connection.executemany(
                "INSERT INTO messages (msg_id, source, group_id, group_name, ts, received_at, content)"
                " VALUES (?, 'qqbot', ?, ?, ?, ?, '通知')",
                [(msg_id, group, name, ts, ts) for msg_id, group, name, ts in GROUP_ROWS],
            )

        # 要的是「最新那条带名字的消息」，不是最新那条消息；名字等于群号的行不算名字。
        self.assertEqual(self.store.group_names(), {"g1": "真实群名"})

        # group_names 表里 NapCat 报回来的名字更权威。
        self.assertEqual(self.store.remember_group_names({"g1": "缓存群名", "g9": "只在这一张表里"}), 2)
        self.assertEqual(self.store.group_names(), {"g1": "缓存群名", "g9": "只在这一张表里"})


if __name__ == "__main__":
    unittest.main()
