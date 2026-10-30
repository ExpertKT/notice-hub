from __future__ import annotations

import sys
import sqlite3
import tempfile
import datetime as dt
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest.store import Store, effective_urgent  # noqa: E402
from qq_live_digest.timeutil import iso, now_local  # noqa: E402


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "test.sqlite3")

    def _insert(
        self, msg_id: str, content: str = "通知", group: str = "g1", source_text: str = ""
    ) -> bool:
        return self.store.insert_message(
            msg_id=msg_id,
            group_id=group,
            content=content,
            source_text=source_text,
            sender_name="辅导员",
            group_name="学院群",
        )

    def test_insert_dedupes_by_msg_id(self) -> None:
        self.assertTrue(self._insert("m1"))
        self.assertFalse(self._insert("m1", content="重复"))
        self.assertEqual(self.store.counts()["messages"], 1)
        self.assertEqual(self.store.counts()["unprocessed"], 1)

    def test_inbox_items_include_message_source_and_event(self) -> None:
        self.store.insert_message(
            msg_id="onebot:g1:attach:0123456789abcdef",
            group_id="g1",
            content="图片通知正文",
            source="onebot-attachment",
            event="GROUP_ATTACHMENT",
        )
        self.store.save_mark("onebot:g1:attach:0123456789abcdef", "notice")

        item = self.store.inbox_items()[0]
        self.assertEqual(item["source"], "onebot-attachment")
        self.assertEqual(item["event"], "GROUP_ATTACHMENT")

    def test_delete_inbox_items_removes_marks_and_messages(self) -> None:
        self._insert("m1")
        self._insert("m2")
        self.store.save_mark("m1", "notice")
        self.store.save_mark("m2", "suspect")
        self.assertEqual(self.store.inbox_counts()["notice"], 1)

        self.assertEqual(self.store.delete_inbox_items(["m1"]), 1)

        # 两张表都真删：列表里没有、消息总数少了、判定计数也不再算它。
        self.assertEqual([row["msg_id"] for row in self.store.inbox_items()], ["m2"])
        self.assertEqual(self.store.counts()["messages"], 1)
        self.assertEqual(self.store.inbox_counts()["notice"], 0)
        self.assertEqual(self.store.inbox_counts()["suspect"], 1)

    def test_delete_inbox_items_is_noop_for_empty_or_unknown_ids(self) -> None:
        self._insert("m1")
        self.store.save_mark("m1", "notice")
        self.assertEqual(self.store.delete_inbox_items([]), 0)
        self.assertEqual(self.store.delete_inbox_items(["没有这条"]), 0)
        # no-op 不能误删
        self.assertEqual([row["msg_id"] for row in self.store.inbox_items()], ["m1"])
        self.assertEqual(self.store.counts()["messages"], 1)

    def test_unprocessed_and_mark_processed(self) -> None:
        self._insert("m1")
        self._insert("m2")
        rows = self.store.unprocessed_messages()
        self.assertEqual([row["msg_id"] for row in rows], ["m1", "m2"])
        digest_id = self.store.insert_digest(
            kind="window",
            window_start=iso(now_local()),
            window_end=iso(now_local()),
            body="正文",
            payload=[{"summary": "x"}],
            message_count=2,
            llm_used=False,
        )
        self.assertGreater(digest_id, 0)
        self.store.mark_processed(["m1", "m2"], digest_id)
        self.assertEqual(self.store.unprocessed_messages(), [])
        self.assertEqual(self.store.counts()["unprocessed"], 0)
        digest = self.store.get_digest(digest_id)
        self.assertIsNotNone(digest)
        assert digest is not None
        self.assertEqual(digest["items"], [{"summary": "x"}])

    def test_delivery_claim_dedupe_and_retry(self) -> None:
        digest_id = self.store.insert_digest(
            kind="window",
            window_start="",
            window_end="",
            body="正文",
            payload=[],
            message_count=1,
            llm_used=False,
        )
        send, delivery_id = self.store.claim_delivery(
            digest_id=digest_id,
            channel="qq-bot",
            target="openid-1",
            dedupe_key=f"digest:{digest_id}",
            max_attempts=3,
            retry_seconds=0,
        )
        self.assertTrue(send)
        # 同一投递在冷却中不会重复发送
        send_again, same_id = self.store.claim_delivery(
            digest_id=digest_id,
            channel="qq-bot",
            target="openid-1",
            dedupe_key=f"digest:{digest_id}",
            max_attempts=3,
            retry_seconds=3600,
        )
        self.assertFalse(send_again)
        self.assertEqual(same_id, delivery_id)

        self.store.mark_delivery(delivery_id or 0, ok=True)
        send_after_sent, _ = self.store.claim_delivery(
            digest_id=digest_id,
            channel="qq-bot",
            target="openid-1",
            dedupe_key=f"digest:{digest_id}",
            max_attempts=3,
            retry_seconds=0,
        )
        self.assertFalse(send_after_sent)

    def test_pending_deliveries_and_attempt_limit(self) -> None:
        digest_id = self.store.insert_digest(
            kind="urgent",
            window_start="",
            window_end="2026-09-28 18:00",
            body="紧急正文",
            payload=[],
            message_count=1,
            llm_used=False,
        )
        _, delivery_id = self.store.claim_delivery(
            digest_id=digest_id,
            channel="webhook",
            target="example.com#abcd",
            dedupe_key=f"digest:{digest_id}",
            max_attempts=2,
            retry_seconds=0,
        )
        self.store.mark_delivery(delivery_id or 0, ok=False, error="boom")
        pending = self.store.pending_deliveries(max_attempts=2, retry_seconds=0)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["body"], "紧急正文")

        # attempts 达到上限后不再重试
        self.store.increment_delivery_attempt(delivery_id or 0)
        self.store.mark_delivery(delivery_id or 0, ok=False, error="boom2")
        self.assertEqual(self.store.pending_deliveries(max_attempts=2, retry_seconds=0), [])

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
        removed = self.store.prune(retention_days=30)
        self.assertEqual(removed, 1)
        self.assertEqual(self.store.counts()["messages"], 0)

    def test_prune_drops_orphan_inbox_marks(self) -> None:
        """messages 过期被删后，它的 inbox_marks 不能留着把收件箱计数撑起来。"""
        old = iso(now_local() - dt.timedelta(days=40))
        self.store.insert_message(msg_id="stale", group_id="g1", content="旧消息", received_at=old)
        self.store.save_mark("stale", "notice", "过期", "llm")
        self.store.insert_message(msg_id="fresh", group_id="g1", content="新消息", received_at=iso(now_local()))
        self.store.save_mark("fresh", "suspect", "还在", "llm")
        self.assertEqual(self.store.inbox_counts()["notice"], 1)

        self.assertEqual(self.store.prune(retention_days=30), 1)

        counts = self.store.inbox_counts()
        self.assertEqual(counts["notice"], 0, "过期消息的 marks 成了孤儿，计数必须归零")
        self.assertEqual(counts["suspect"], 1, "还在库里的消息不能被误删")
        self.assertEqual([row["msg_id"] for row in self.store.inbox_items()], ["fresh"])

    def test_jsonl_audit_written(self) -> None:
        self._insert("m1")
        path = Path(self.tmp.name) / "messages.jsonl"
        self.assertTrue(path.is_file())
        self.assertIn("m1", path.read_text(encoding="utf-8"))


    def test_old_task_database_migrates_without_data_loss(self) -> None:
        path = Path(self.tmp.name) / "old.sqlite3"
        connection = sqlite3.connect(path)
        connection.executescript(
            """
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
            INSERT INTO tasks
                (task_key, summary, status, created_at, updated_at)
            VALUES ('old', '旧任务', 'open', '2026-09-01 08:00:00', '2026-09-01 08:00:00');
            """
        )
        connection.commit()
        connection.close()

        migrated = Store(path)
        tasks = migrated.list_tasks()
        self.assertEqual(tasks[0]["summary"], "旧任务")
        self.assertEqual(tasks[0]["confidence"], 0.0)
        with migrated._connect() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)").fetchall()}
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn("confirmed_at", columns)
        self.assertIn("last_reminded_at", columns)
        self.assertIn("task_events", tables)

    def test_task_keeps_structured_details_and_full_source_text(self) -> None:
        self._insert(
            "m-structured",
            "结构化摘要：两类同学都要处理",
            source_text="原文完整内容：刚转专业同学需要注意，其他同学也要注意。" + "附件细节" * 300,
        )
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
        # 统计和日历导出必须看全量；默认 800 的截断只该管界面列表。
        import inspect

        default = inspect.signature(Store.list_tasks).parameters["limit"].default
        self.assertEqual(default, 800)
        for index in range(3):
            self.store.upsert_task(task_key=f"limit-{index}", summary=f"任务{index}")
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
        """已完成的任务可以直接真删：行与事件一起没了，不留 dismissed 事件污染忽略率。"""
        task_id = self.store.upsert_task(task_key="done-delete", summary="已完成的任务")
        self.assertTrue(self.store.apply_task_action(task_id, "done"))
        self.assertTrue(self.store.apply_task_action(task_id, "delete"))

        self.assertIsNone(self.store.get_task(task_id))
        self.assertEqual(self.store.list_task_events(task_id), [])

    def test_dismiss_rules_are_unchanged(self) -> None:
        """dismiss 只接受 candidate/open：done 不能借此变成「已忽略」。"""
        done_id = self.store.upsert_task(task_key="d-done", summary="已完成")
        self.store.apply_task_action(done_id, "done")
        self.assertFalse(self.store.apply_task_action(done_id, "dismiss"))
        self.assertEqual(self.store.get_task(done_id)["status"], "done")

        open_id = self.store.upsert_task(task_key="d-open", summary="待办")
        candidate_id = self.store.upsert_task(task_key="d-candidate", summary="候选", status="candidate")
        self.assertTrue(self.store.apply_task_action(open_id, "dismiss"))
        self.assertTrue(self.store.apply_task_action(candidate_id, "dismiss"))
        # 已经忽略/过期的再忽略不重复推进
        self.assertFalse(self.store.apply_task_action(open_id, "dismiss"))
        expired_id = self.store.upsert_task(task_key="d-expired", summary="过期", status="expired")
        self.assertFalse(self.store.apply_task_action(expired_id, "dismiss"))

    def test_delete_only_accepts_finished_tasks(self) -> None:
        open_id = self.store.upsert_task(task_key="del-open", summary="还在做")
        candidate_id = self.store.upsert_task(task_key="del-candidate", summary="候选", status="candidate")
        # open/candidate 不能真删：先让用户走「忽略」，否则待办会凭空消失。
        self.assertFalse(self.store.apply_task_action(open_id, "delete"))
        self.assertFalse(self.store.apply_task_action(candidate_id, "delete"))
        self.assertIsNotNone(self.store.get_task(open_id))
        self.assertIsNotNone(self.store.get_task(candidate_id))

        dismissed_id = self.store.upsert_task(task_key="del-dismissed", summary="已忽略")
        self.store.apply_task_action(dismissed_id, "dismiss")
        self.assertTrue(self.store.apply_task_action(dismissed_id, "delete"))
        expired_id = self.store.upsert_task(task_key="del-expired", summary="已过期", status="expired")
        self.assertTrue(self.store.apply_task_action(expired_id, "delete"))
        # 不存在的 id 返回 False，不抛异常
        self.assertFalse(self.store.apply_task_action(999999, "delete"))

    def test_clear_finished_tasks_only_removes_finished_and_their_events(self) -> None:
        done_id = self.store.upsert_task(task_key="cf-done", summary="已完成")
        self.store.apply_task_action(done_id, "done")
        dismissed_id = self.store.upsert_task(task_key="cf-dismissed", summary="已忽略")
        self.store.apply_task_action(dismissed_id, "dismiss")
        expired_id = self.store.upsert_task(task_key="cf-expired", summary="已过期", status="expired")
        open_id = self.store.upsert_task(task_key="cf-open", summary="还在做")
        candidate_id = self.store.upsert_task(task_key="cf-candidate", summary="候选", status="candidate")

        self.assertEqual(self.store.clear_finished_tasks(), 3)

        remaining = {task["id"] for task in self.store.list_tasks(statuses=("open", "candidate"))}
        self.assertEqual(remaining, {open_id, candidate_id})
        for task_id in (done_id, dismissed_id, expired_id):
            self.assertIsNone(self.store.get_task(task_id))
            self.assertEqual(self.store.list_task_events(task_id), [], "事件要跟着任务一起删")
        # 没有可删的时候返回 0，不能误伤
        self.assertEqual(self.store.clear_finished_tasks(), 0)

    def test_task_corrections_record_feedback_and_update_fields(self) -> None:
        urgent_id = self.store.upsert_task(
            task_key="urgent-1",
            summary="明早截止",
            category="urgent",
            importance=5,
        )
        self.assertTrue(self.store.apply_task_correction(urgent_id, "not_urgent"))
        urgent = self.store.get_task(urgent_id)
        assert urgent is not None
        self.assertEqual(urgent["category"], "action")
        self.assertEqual(urgent["importance"], 3)

        duplicate_id = self.store.upsert_task(
            task_key="duplicate-1",
            summary="重复通知",
            category="info",
        )
        self.assertTrue(self.store.apply_task_correction(duplicate_id, "duplicate"))
        duplicate = self.store.get_task(duplicate_id)
        assert duplicate is not None
        self.assertEqual(duplicate["status"], "dismissed")

        events = self.store.list_task_events(urgent_id)
        corrected = next(item for item in events if item["event"] == "corrected")
        self.assertIn("not_urgent", corrected["detail"])
        self.assertIn("previous", corrected["detail"])


    def test_task_urgent_override_true_is_persisted_and_listed(self) -> None:
        task_id = self.store.upsert_task(task_key="urgent-override", summary="普通任务", category="action")
        result = self.store.set_task_urgent(task_id, True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["urgent_override"], 1)
        listed = self.store.list_tasks()
        self.assertEqual(listed[0]["urgent_override"], 1)
        self.assertTrue(effective_urgent(listed[0]))

    def test_task_urgent_override_none_follows_ai_category(self) -> None:
        task_id = self.store.upsert_task(task_key="urgent-follow", summary="AI紧急", category="urgent")
        self.assertEqual(self.store.set_task_urgent(task_id, False)["urgent_override"], 0)
        restored = self.store.set_task_urgent(task_id, None)
        self.assertIsNone(restored["urgent_override"])
        self.assertTrue(effective_urgent(restored))

    def test_task_urgent_override_missing_task_returns_error(self) -> None:
        result = self.store.set_task_urgent(999999, True)
        self.assertFalse(result["ok"])
        self.assertIn("error", result)

    def test_task_urgent_override_migrates_idempotently(self) -> None:
        path = Path(self.tmp.name) / "migrate.sqlite3"
        first = Store(path)
        task_id = first.upsert_task(task_key="existing", summary="已有", category="urgent")
        self.assertEqual(first.set_task_urgent(task_id, True)["urgent_override"], 1)
        second = Store(path)
        third = Store(path)
        self.assertEqual(second.get_task(task_id)["urgent_override"], 1)
        self.assertEqual(third.get_task(task_id)["summary"], "已有")

    def test_task_pinned_is_persisted_listed_and_migrated(self) -> None:
        path = Path(self.tmp.name) / "pin.sqlite3"
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

    def test_task_pin_rejects_done_tasks_and_invalid_values(self) -> None:
        task_id = self.store.upsert_task(task_key="pin-done", summary="已完成", category="action")
        self.store.set_task_status(task_id, True)
        result = self.store.set_task_pinned(task_id, True)
        self.assertFalse(result["ok"])
        self.assertIn("置顶", result["error"])
        self.assertFalse(self.store.get_task(task_id)["pinned"])
        with self.assertRaises(ValueError):
            self.store.set_task_pinned(task_id, 1)
        self.assertFalse(self.store.set_task_pinned(999999, True)["ok"])

    def test_snooze_stores_until_and_duplicate_links_original(self) -> None:
        original_id = self.store.upsert_task(
            task_key="m1",
            summary="提交实验报告",
            category="action",
            groups=["学院群"],
        )
        copy_id = self.store.upsert_task(
            task_key="m2",
            summary="提交实验报告",
            category="action",
            groups=["班级群"],
        )
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
        task_id = self.store.upsert_task(task_key="m1", summary="待办", category="action")
        self.assertTrue(self.store.apply_task_action(task_id, "snooze"))
        snoozed = self.store.get_task(task_id)
        assert snoozed is not None
        self.assertGreater(len(snoozed["snooze_until"]), 0)
        events = self.store.list_task_events(task_id)
        self.assertEqual(events[0]["event"], "snoozed")

    def test_category_importance_is_shared_by_both_entry_points(self) -> None:
        """同一条任务的「分类 → 重要度」只能有一份定义，两个入口算出来必须一样。

        以前 summarizer 里 action=4、info=3，store 手动改分类里 action=3、info=1：
        同一条任务 LLM 判成 4，用户点一下「改成待办」就掉到 3（info 更狠，3→1）。
        """
        from qq_live_digest import store as store_module  # noqa: F401 - 别名只为可读
        from qq_live_digest import summarizer

        expected = {"urgent": 5, "action": 4, "academic": 3, "info": 3}
        # 唯一定义：两边拿到的是同一个对象，而不是各存一份相同的字面量。
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
                # 用户手动把分类再确认一次：重要度不许被重算成别的值
                self.assertTrue(self.store.apply_task_correction(task_id, "category", value=category))
                task = self.store.get_task(task_id)
                assert task is not None
                self.assertEqual(task["category"], category)
                self.assertEqual(task["importance"], importance)

        # 回归点：info 手动改分类后必须是 3，不是历史上的 1
        info_id = self.store.upsert_task(task_key="shared-info-manual", summary="通知一条", category="info")
        self.assertTrue(self.store.apply_task_correction(info_id, "category", value="info"))
        info = self.store.get_task(info_id)
        assert info is not None
        self.assertEqual(info["importance"], 3)

    def test_correction_insights_summarise_group_mistakes(self) -> None:
        for index in range(3):
            task_id = self.store.upsert_task(
                task_key=f"u{index}",
                summary=f"紧急通知 {index}",
                category="urgent",
                groups=["新生交流群"],
            )
            self.store.apply_task_correction(task_id, "not_urgent")
        stats = self.store.correction_stats(days=30)
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["by_type"]["not_urgent"], 3)
        self.assertEqual(stats["by_group"]["新生交流群"]["not_urgent"], 3)
        insights = self.store.correction_insights(days=30, min_group_hits=3)
        self.assertTrue(any("新生交流群" in text for text in insights))

    def test_delivery_stats_counts_statuses(self) -> None:
        digest_id = self.store.insert_digest(
            kind="window",
            window_start=iso(now_local()),
            window_end=iso(now_local()),
            body="正文",
            payload=[],
            message_count=1,
            llm_used=False,
        )
        should_send, delivery_id = self.store.claim_delivery(
            digest_id=digest_id,
            channel="webhook",
            target="t1",
            dedupe_key="k1",
            max_attempts=3,
            retry_seconds=0,
        )
        self.assertTrue(should_send)
        self.store.mark_delivery(delivery_id or 0, ok=True)
        stats = self.store.delivery_stats()
        self.assertEqual(stats["sent"], 1)
        self.assertEqual(stats["pending"], 0)

    def test_group_names_picks_latest_named_message_and_prefers_cache(self) -> None:
        rows = [
            ("a1", "g1", "g1", "2026-10-01 10:00:00"),        # 群名没取到，等于群号
            ("a2", "g1", "真实群名", "2026-10-01 11:00:00"),  # 带名字的那条
            ("a3", "g1", "g1", "2026-10-01 12:00:00"),        # 更新，但没名字
            ("b1", "g2", "g2", "2026-10-01 10:00:00"),        # 全程只有群号
        ]
        with self.store._connect() as connection:
            connection.executemany(
                "INSERT INTO messages (msg_id, source, group_id, group_name, ts, received_at, content)"
                " VALUES (?, 'qqbot', ?, ?, ?, ?, '通知')",
                [(msg_id, group, name, ts, ts) for msg_id, group, name, ts in rows],
            )
        # 取「最新那条带名字的消息」，不是最新那条消息；只有群号的行不算名字。
        self.assertEqual(self.store.group_names(), {"g1": "真实群名"})
        # group_names 表（NapCat 报回来的名字）优先。
        self.assertEqual(self.store.remember_group_names({"g1": "缓存群名", "g9": "只在这一张表里"}), 2)
        self.assertEqual(
            self.store.group_names(),
            {"g1": "缓存群名", "g9": "只在这一张表里"},
        )

if __name__ == "__main__":
    unittest.main()
