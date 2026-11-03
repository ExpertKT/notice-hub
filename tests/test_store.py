"""Store 契约回归。

每条用例都锁住一个对外行为：消息去重、投递冷却、待办状态机、纠错统计、老库迁移。
断言里的数值与参数是契约本身，只允许改写表达方式。
"""

from __future__ import annotations

import datetime as dt
import inspect
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, main as run_tests, mock  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from noticehub import store as store_module  # noqa: E402
from noticehub.infrastructure.sqlite import store as store_impl  # noqa: E402
from noticehub import summarizer  # noqa: E402
from noticehub.store import Store, effective_urgent  # noqa: E402
from noticehub.timeutil import iso, now_local  # noqa: E402

# 分叉前的老库：只有最初的 13 列，后续列全靠 init() 补。
LEGACY_COLUMNS = (
    "id INTEGER PRIMARY KEY AUTOINCREMENT",
    "task_key TEXT NOT NULL UNIQUE",
    "summary TEXT NOT NULL DEFAULT ''",
    "action TEXT NOT NULL DEFAULT ''",
    "category TEXT NOT NULL DEFAULT 'info'",
    "importance INTEGER NOT NULL DEFAULT 3",
    "deadline TEXT NOT NULL DEFAULT ''",
    "groups TEXT NOT NULL DEFAULT '[]'",
    "sender TEXT NOT NULL DEFAULT ''",
    "evidence TEXT NOT NULL DEFAULT ''",
    "status TEXT NOT NULL DEFAULT 'open'",
    "digest_id INTEGER NOT NULL DEFAULT 0",
    "created_at TEXT NOT NULL",
    "updated_at TEXT NOT NULL",
    "done_at TEXT NOT NULL DEFAULT ''",
)
LEGACY_DDL = "CREATE TABLE tasks (\n    " + ",\n    ".join(LEGACY_COLUMNS) + "\n);\n"
LEGACY_DDL += """
INSERT INTO tasks (task_key, summary, status, created_at, updated_at)
VALUES ('old', '旧任务', 'open', '2026-09-01 08:00:00', '2026-09-01 08:00:00');
"""


def group_rows() -> list[tuple[str, str, str, str]]:
    """(msg_id, 群号, 群名, 时间)：g1 最新一条没名字，次新的才有。"""
    return [
        ("a1", "g1", "g1", "2026-10-01 10:00:00"),
        ("a2", "g1", "真实群名", "2026-10-01 11:00:00"),
        ("a3", "g1", "g1", "2026-10-01 12:00:00"),
        ("b1", "g2", "g2", "2026-10-01 10:00:00"),
    ]


class StoreTest(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "test.sqlite3")

    # ------------------------------------------------------------- 夹具
    def insert_msg(self, msg_id, content="通知", group="g1", source_text="", **extra):
        fields = dict(
            msg_id=msg_id,
            content=content,
            group_id=group,
            sender_name="辅导员",
            source_text=source_text,
            group_name="学院群",
        )
        fields.update(extra)
        return self.store.insert_message(**fields)

    def insert_digest_record(self, **changed):
        base = {
            "window_start": "",
            "window_end": "",
            "kind": "window",
            "body": "正文",
            "payload": [],
            "message_count": 1,
            "llm_used": False,
        }
        base.update(changed)
        return self.store.insert_digest(**base)

    def claim(self, digest_id, **changed):
        base = dict(
            digest_id=digest_id,
            channel="qq-bot",
            target="openid-1",
            dedupe_key="digest:%d" % digest_id,
            max_attempts=3,
            retry_seconds=0,
        )
        base.update(changed)
        return self.store.claim_delivery(**base)

    def task(self, key, summary="任务", **changed):
        base = dict(task_key=key, summary=summary)
        base.update(changed)
        return self.store.upsert_task(**base)

    # ------------------------------------------------------------- 消息
    def test_insert_dedupes_by_msg_id(self):
        self.assertTrue(self.insert_msg("m1"))
        self.assertFalse(self.insert_msg("m1", content="重复"))
        self.assertEqual(1, self.store.counts()["messages"])
        self.assertEqual(1, self.store.counts()["unprocessed"])

    def test_inbox_items_include_message_source_and_event(self):
        key = "onebot:g1:attach:0123456789abcdef"
        self.store.insert_message(
            msg_id=key,
            group_id="g1",
            content="图片通知正文",
            source="onebot-attachment",
            event="GROUP_ATTACHMENT",
        )
        self.store.save_mark(key, "notice")
        row = self.store.inbox_items()[0]
        self.assertEqual("onebot-attachment", row["source"])
        self.assertEqual("GROUP_ATTACHMENT", row["event"])

    def test_delete_inbox_items_removes_marks_and_messages(self):
        self.insert_msg("m1")
        self.insert_msg("m2")
        self.store.save_mark("m1", "notice")
        self.store.save_mark("m2", "suspect")
        self.assertEqual(1, self.store.inbox_counts()["notice"])

        self.assertEqual(1, self.store.delete_inbox_items(["m1"]))

        # 两张表一起删：只删 marks 会让计数比列表多一条。
        self.assertEqual(["m2"], [row["msg_id"] for row in self.store.inbox_items()])
        self.assertEqual(1, self.store.counts()["messages"])
        self.assertEqual(0, self.store.inbox_counts()["notice"])
        self.assertEqual(1, self.store.inbox_counts()["suspect"])

    def test_delete_inbox_items_is_noop_for_empty_or_unknown_ids(self):
        self.insert_msg("m1")
        self.store.save_mark("m1", "notice")
        self.assertEqual(0, self.store.delete_inbox_items([]))
        self.assertEqual(0, self.store.delete_inbox_items(["没有这条"]))
        self.assertEqual(["m1"], [row["msg_id"] for row in self.store.inbox_items()])
        self.assertEqual(1, self.store.counts()["messages"])

    def test_unprocessed_and_mark_processed(self):
        self.insert_msg("m1")
        self.insert_msg("m2")
        self.assertEqual(["m1", "m2"], [row["msg_id"] for row in self.store.unprocessed_messages()])

        stamp = iso(now_local())
        digest_id = self.insert_digest_record(
            window_start=stamp, window_end=stamp, payload=[{"summary": "x"}], message_count=2
        )
        self.assertGreater(digest_id, 0)
        self.store.mark_processed(["m1", "m2"], digest_id)
        self.assertEqual([], self.store.unprocessed_messages())
        self.assertEqual(0, self.store.counts()["unprocessed"])

        saved = self.store.get_digest(digest_id)
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual([{"summary": "x"}], saved["items"])

    def test_jsonl_audit_written(self):
        self.insert_msg("m1")
        audit = self.root / "messages.jsonl"
        self.assertTrue(audit.is_file())
        self.assertIn("m1", audit.read_text(encoding="utf-8"))

    def test_meta_and_prune(self):
        old_stamp = iso(now_local() - dt.timedelta(days=40))
        self.store.insert_message(msg_id="old", group_id="g1", content="旧消息", received_at=old_stamp)
        self.store.mark_processed(["old"], None)
        self.store.meta_set("last_prune", "2026-09-28 00:00:00")
        self.assertEqual("2026-09-28 00:00:00", self.store.meta_get("last_prune"))

        self.assertEqual(1, self.store.prune(retention_days=30))
        self.assertEqual(0, self.store.counts()["messages"])

    def test_prune_drops_orphan_inbox_marks(self):
        """消息过期删掉后，判定行不能留成孤儿把收件箱数字撑大。"""
        stale = iso(now_local() - dt.timedelta(days=40))
        self.store.insert_message(msg_id="stale", group_id="g1", content="旧消息", received_at=stale)
        self.store.save_mark("stale", "notice", "过期", "llm")
        self.store.insert_message(
            msg_id="fresh", group_id="g1", content="新消息", received_at=iso(now_local())
        )
        self.store.save_mark("fresh", "suspect", "还在", "llm")
        self.assertEqual(1, self.store.inbox_counts()["notice"])

        self.assertEqual(1, self.store.prune(retention_days=30))

        counts = self.store.inbox_counts()
        self.assertEqual(0, counts["notice"], "过期消息的 marks 成了孤儿，计数必须归零")
        self.assertEqual(1, counts["suspect"], "还在库里的消息不能被误删")
        self.assertEqual(["fresh"], [row["msg_id"] for row in self.store.inbox_items()])

    # ------------------------------------------------------------- 投递
    def test_delivery_claim_dedupe_and_retry(self):
        digest_id = self.insert_digest_record()
        send, delivery_id = self.claim(digest_id)
        self.assertTrue(send)

        # 冷却窗口里同一条投递必须被挡住。
        send_again, same_id = self.claim(digest_id, retry_seconds=3600)
        self.assertFalse(send_again)
        self.assertEqual(delivery_id, same_id)

        self.store.mark_delivery(delivery_id or 0, ok=True)
        send_after_sent, _ = self.claim(digest_id)
        self.assertFalse(send_after_sent)

    def test_pending_deliveries_and_attempt_limit(self):
        digest_id = self.insert_digest_record(
            kind="urgent", window_end="2026-09-28 18:00", body="紧急正文"
        )
        _, delivery_id = self.claim(
            digest_id, channel="webhook", target="example.com#abcd", max_attempts=2
        )
        self.store.mark_delivery(delivery_id or 0, ok=False, error="boom")

        pending = self.store.pending_deliveries(max_attempts=2, retry_seconds=0)
        self.assertEqual(1, len(pending))
        self.assertEqual("紧急正文", pending[0]["body"])

        # 次数用满就不再出现在待重试里。
        self.store.increment_delivery_attempt(delivery_id or 0)
        self.store.mark_delivery(delivery_id or 0, ok=False, error="boom2")
        self.assertEqual([], self.store.pending_deliveries(max_attempts=2, retry_seconds=0))

    def test_mark_delivery_returns_affected_rows_and_warns_on_unknown_id(self):
        digest_id = self.insert_digest_record()
        _, delivery_id = self.claim(digest_id)

        self.assertEqual(1, self.store.mark_delivery(delivery_id or 0, ok=True))

        with self.assertLogs(store_impl.LOGGER, level="WARNING") as logs:
            self.assertEqual(0, self.store.mark_delivery(999999, ok=True))
        self.assertIn("投递状态更新未命中记录", "\n".join(logs.output))

    def test_delivery_stats_counts_statuses(self):
        digest_id = self.insert_digest_record(
            window_start=iso(now_local()), window_end=iso(now_local())
        )
        should_send, delivery_id = self.claim(
            digest_id, channel="webhook", target="t1", dedupe_key="k1"
        )
        self.assertTrue(should_send)
        self.store.mark_delivery(delivery_id or 0, ok=True)

        stats = self.store.delivery_stats()
        self.assertEqual(1, stats["sent"])
        self.assertEqual(0, stats["pending"])

    # ------------------------------------------------------------- 迁移
    def test_old_task_database_migrates_without_data_loss(self):
        path = self.root / "legacy.sqlite3"
        connection = sqlite3.connect(path)
        connection.executescript(LEGACY_DDL)
        connection.commit()
        connection.close()

        migrated = Store(path)
        task = migrated.list_tasks()[0]
        self.assertEqual("旧任务", task["summary"])
        self.assertEqual(0.0, task["confidence"])

        with migrated._connect() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)").fetchall()}
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
        self.assertIn("confirmed_at", columns)
        self.assertIn("last_reminded_at", columns)
        self.assertIn("task_events", tables)

    def test_task_urgent_override_migrates_idempotently(self):
        path = self.root / "upgrade.sqlite3"
        first = Store(path)
        task_id = first.upsert_task(task_key="existing", summary="已有", category="urgent")
        self.assertEqual(1, first.set_task_urgent(task_id, True)["urgent_override"])

        second = Store(path)
        third = Store(path)
        self.assertEqual(1, second.get_task(task_id)["urgent_override"])
        self.assertEqual("已有", third.get_task(task_id)["summary"])

    def test_task_pinned_is_persisted_listed_and_migrated(self):
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

    # ------------------------------------------------------------- 待办
    def test_task_keeps_structured_details_and_full_source_text(self):
        source = "原文完整内容：刚转专业同学需要注意，其他同学也要注意。" + "附件细节" * 300
        self.insert_msg("m-structured", "结构化摘要：两类同学都要处理", source_text=source)
        spec = dict(
            task_key="m-structured",
            summary="两类同学都要核查",
            audience="刚转专业同学；其他同学",
            condition="没有2026年体测成绩",
            action="查看并预约",
            category="action",
            groups=["测试群"],
        )
        spec["details"] = ["刚转专业同学先查看成绩", "其他同学也要查看成绩"]
        task_id = self.store.upsert_task(**spec)

        task = self.store.get_task(task_id)
        assert task is not None
        self.assertEqual("刚转专业同学；其他同学", task["audience"])
        self.assertEqual("没有2026年体测成绩", task["condition"])
        self.assertEqual(["刚转专业同学先查看成绩", "其他同学也要查看成绩"], task["details"])
        self.assertIn("原文完整内容", task["source_text"])

    def test_list_tasks_limit_none_returns_all_and_default_stays_capped(self):
        """统计与日历要全量；800 的默认截断只约束界面列表。"""
        default = inspect.signature(Store.list_tasks).parameters["limit"].default
        self.assertEqual(800, default)

        for index in range(3):
            self.task("limit-%d" % index, "任务%d" % index)
        self.assertEqual(2, len(self.store.list_tasks(limit=2)))
        self.assertEqual(3, len(self.store.list_tasks(limit=None)))
        self.assertEqual(3, len(self.store.list_tasks()))

    def test_task_actions_and_event_metrics(self):
        task_id = self.store.upsert_task(**dict(
            task_key="candidate-1",
            summary="可能要交报名表",
            status="candidate",
            category="action",
            groups=["班级群"],
            evidence="记得看一下报名表",
            confidence=0.61,
            source="qq_message",
            classification_reason="可能出现弱行动词",
        ))
        candidate = self.store.list_tasks(statuses=("candidate",))[0]
        self.assertEqual(task_id, candidate["id"])
        self.assertEqual(0.61, candidate["confidence"])

        for action in ("confirm", "done", "reopen", "dismiss"):
            self.assertTrue(self.store.apply_task_action(task_id, action))

        events = [item["event"] for item in self.store.list_task_events(task_id)]
        for expected in ("dismissed", "reopened", "done", "confirmed", "candidate_detected"):
            self.assertIn(expected, events)

        metrics = self.store.task_metrics(start="2020-01-01 00:00:00", end="2030-01-01 00:00:00")
        self.assertEqual(1, metrics["candidates"])
        self.assertEqual(1, metrics["confirmed"])
        self.assertEqual(1, metrics["dismissed"])
        self.assertEqual(1, self.store.task_stats()["dismissed"])

    def test_done_task_can_be_deleted_directly(self):
        """做完的任务可以真删，而且不写 dismissed 事件去污染忽略率。"""
        task_id = self.task("done-delete", "已完成的任务")
        self.assertTrue(self.store.apply_task_action(task_id, "done"))
        self.assertTrue(self.store.apply_task_action(task_id, "delete"))

        self.assertIsNone(self.store.get_task(task_id))
        self.assertEqual([], self.store.list_task_events(task_id))

    def test_dismiss_rules_are_unchanged(self):
        """dismiss 只收 candidate/open；done 不能被顺手改成「已忽略」。"""
        done_id = self.task("d-done", "已完成")
        self.store.apply_task_action(done_id, "done")
        self.assertFalse(self.store.apply_task_action(done_id, "dismiss"))
        self.assertEqual("done", self.store.get_task(done_id)["status"])

        open_id = self.task("d-open", "待办")
        candidate_id = self.task("d-candidate", "候选", status="candidate")
        self.assertTrue(self.store.apply_task_action(open_id, "dismiss"))
        self.assertTrue(self.store.apply_task_action(candidate_id, "dismiss"))

        # 重复忽略不再推进状态。
        self.assertFalse(self.store.apply_task_action(open_id, "dismiss"))
        expired_id = self.task("d-expired", "过期", status="expired")
        self.assertFalse(self.store.apply_task_action(expired_id, "dismiss"))

    def test_delete_only_accepts_finished_tasks(self):
        open_id = self.task("del-open", "还在做")
        candidate_id = self.task("del-candidate", "候选", status="candidate")
        # 没走完流程的不能真删，否则待办会凭空消失。
        self.assertFalse(self.store.apply_task_action(open_id, "delete"))
        self.assertFalse(self.store.apply_task_action(candidate_id, "delete"))
        self.assertIsNotNone(self.store.get_task(open_id))
        self.assertIsNotNone(self.store.get_task(candidate_id))

        dismissed_id = self.task("del-dismissed", "已忽略")
        self.store.apply_task_action(dismissed_id, "dismiss")
        self.assertTrue(self.store.apply_task_action(dismissed_id, "delete"))

        expired_id = self.task("del-expired", "已过期", status="expired")
        self.assertTrue(self.store.apply_task_action(expired_id, "delete"))
        self.assertFalse(self.store.apply_task_action(999999, "delete"))

    def test_clear_finished_tasks_only_removes_finished_and_their_events(self):
        done_id = self.task("cf-done", "已完成")
        self.store.apply_task_action(done_id, "done")
        dismissed_id = self.task("cf-dismissed", "已忽略")
        self.store.apply_task_action(dismissed_id, "dismiss")
        expired_id = self.task("cf-expired", "已过期", status="expired")
        open_id = self.task("cf-open", "还在做")
        candidate_id = self.task("cf-candidate", "候选", status="candidate")

        self.assertEqual(3, self.store.clear_finished_tasks())

        alive = {task["id"] for task in self.store.list_tasks(statuses=("open", "candidate"))}
        self.assertEqual({open_id, candidate_id}, alive)
        for task_id in (done_id, dismissed_id, expired_id):
            self.assertIsNone(self.store.get_task(task_id))
            self.assertEqual([], self.store.list_task_events(task_id), "事件要跟着任务一起删")
        self.assertEqual(0, self.store.clear_finished_tasks())

    def test_clear_tasks_returns_deleted_row_count(self):
        self.task("ct-1", "一")
        self.task("ct-2", "二")

        self.assertEqual(2, self.store.clear_tasks())
        self.assertEqual([], self.store.list_tasks())
        self.assertEqual(0, self.store.clear_tasks())

    def test_task_corrections_record_feedback_and_update_fields(self):
        urgent_id = self.task("urgent-1", "明早截止", category="urgent", importance=5)
        self.assertTrue(self.store.apply_task_correction(urgent_id, "not_urgent"))
        urgent = self.store.get_task(urgent_id)
        assert urgent is not None
        self.assertEqual("action", urgent["category"])
        self.assertEqual(3, urgent["importance"])

        duplicate_id = self.task("duplicate-1", "重复通知", category="info")
        self.assertTrue(self.store.apply_task_correction(duplicate_id, "duplicate"))
        duplicate = self.store.get_task(duplicate_id)
        assert duplicate is not None
        self.assertEqual("dismissed", duplicate["status"])

        corrected = next(item for item in self.store.list_task_events(urgent_id) if item["event"] == "corrected")
        self.assertIn("not_urgent", corrected["detail"])
        self.assertIn("previous", corrected["detail"])

    def test_correction_insights_summarise_group_mistakes(self):
        for index in range(3):
            task_id = self.task(
                "u%d" % index,
                "紧急通知 %d" % index,
                category="urgent",
                groups=["新生交流群"],
            )
            self.store.apply_task_correction(task_id, "not_urgent")

        stats = self.store.correction_stats(days=30)
        self.assertEqual(3, stats["total"])
        self.assertEqual(3, stats["by_type"]["not_urgent"])
        self.assertEqual(3, stats["by_group"]["新生交流群"]["not_urgent"])
        self.assertTrue(
            any("新生交流群" in text for text in self.store.correction_insights(days=30, min_group_hits=3))
        )

    # --------------------------------------------- 紧急覆盖 / 置顶 / 延后
    def test_task_urgent_override_true_is_persisted_and_listed(self):
        task_id = self.task("urgent-override", "普通任务", category="action")
        result = self.store.set_task_urgent(task_id, True)
        self.assertTrue(result["ok"])
        self.assertEqual(1, result["urgent_override"])

        listed = self.store.list_tasks()
        self.assertEqual(1, listed[0]["urgent_override"])
        self.assertTrue(effective_urgent(listed[0]))

    def test_task_urgent_override_none_follows_ai_category(self):
        task_id = self.task("urgent-follow", "AI紧急", category="urgent")
        self.assertEqual(0, self.store.set_task_urgent(task_id, False)["urgent_override"])

        restored = self.store.set_task_urgent(task_id, None)
        self.assertIsNone(restored["urgent_override"])
        self.assertTrue(effective_urgent(restored))

    def test_task_urgent_override_missing_task_returns_error(self):
        result = self.store.set_task_urgent(999999, True)
        self.assertFalse(result["ok"])
        self.assertIn("error", result)

    def test_task_pin_rejects_done_tasks_and_invalid_values(self):
        task_id = self.task("pin-done", "已完成", category="action")
        self.store.set_task_status(task_id, True)

        result = self.store.set_task_pinned(task_id, True)
        self.assertFalse(result["ok"])
        self.assertIn("置顶", result["error"])
        self.assertFalse(self.store.get_task(task_id)["pinned"])

        with self.assertRaises(ValueError):
            self.store.set_task_pinned(task_id, 1)
        self.assertFalse(self.store.set_task_pinned(999999, True)["ok"])

    def test_snooze_stores_until_and_duplicate_links_original(self):
        original_id = self.task("m1", "提交实验报告", category="action", groups=["学院群"])
        copy_id = self.task("m2", "提交实验报告", category="action", groups=["班级群"])
        until = iso(dt.datetime(2026, 10, 7, 7, 30))
        self.assertTrue(self.store.apply_task_action(original_id, "snooze", detail={"until": until}))

        snoozed = self.store.get_task(original_id)
        assert snoozed is not None
        self.assertEqual(until, snoozed["snooze_until"])

        self.assertTrue(self.store.apply_task_correction(copy_id, "duplicate"))
        copy = self.store.get_task(copy_id)
        assert copy is not None
        self.assertEqual("dismissed", copy["status"])
        self.assertEqual(original_id, copy["duplicate_of"])
        self.assertEqual("提交实验报告", copy["duplicate_summary"])
        self.assertEqual(["学院群", "班级群"], self.store.get_task(original_id)["groups"])

    def test_snooze_without_until_defaults_to_next_day(self):
        task_id = self.task("m1", "待办", category="action")
        self.assertTrue(self.store.apply_task_action(task_id, "snooze"))

        snoozed = self.store.get_task(task_id)
        assert snoozed is not None
        self.assertGreater(len(snoozed["snooze_until"]), 0)
        self.assertEqual("snoozed", self.store.list_task_events(task_id)[0]["event"])

    def test_category_importance_is_shared_by_both_entry_points(self):
        """分类到重要度只能有一份定义；两个入口必须算出同一个数。

        历史问题：两处各写一份，同一条任务 LLM 判 4，用户点一下「改成待办」就掉到 3。
        """
        expected = {"urgent": 5, "action": 4, "academic": 3, "info": 3}
        self.assertIs(summarizer.IMPORTANCE_BY_CATEGORY, store_module.IMPORTANCE_BY_CATEGORY)
        self.assertEqual(expected, summarizer.IMPORTANCE_BY_CATEGORY)

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
                self.assertEqual(category, task["category"])
                self.assertEqual(importance, task["importance"])

        # 回归点：info 手动改分类后是 3，不是历史上那个 1。
        info_id = self.task("shared-info-manual", "通知一条", category="info")
        self.assertTrue(self.store.apply_task_correction(info_id, "category", value="info"))
        info = self.store.get_task(info_id)
        assert info is not None
        self.assertEqual(3, info["importance"])

    # ------------------------------------------------------------- 超星同步状态
    def test_save_chaoxing_sync_state_reports_truncation(self):
        ids = [str(index) for index in range(1200)]
        with self.assertLogs(store_impl.LOGGER, level="WARNING") as logs:
            dropped = self.store.save_chaoxing_sync_state({"notice_ids": ids})
        self.assertEqual(200, dropped)
        self.assertIn("截掉 200 条", "\n".join(logs.output))
        self.assertEqual(ids[-1000:], self.store.chaoxing_sync_state()["notice_ids"])

    def test_save_chaoxing_sync_state_returns_zero_within_limit(self):
        self.assertEqual(0, self.store.save_chaoxing_sync_state({"notice_ids": ["a", "b"]}))
        self.assertEqual(["a", "b"], self.store.chaoxing_sync_state()["notice_ids"])

    # ------------------------------------------------------------- 群名
    def test_group_names_picks_latest_named_message_and_prefers_cache(self):
        rows = group_rows()
        with self.store._connect() as connection:
            connection.executemany(
                "INSERT INTO messages (msg_id, source, group_id, group_name, ts, received_at, content)"
                " VALUES (?, 'qqbot', ?, ?, ?, ?, '通知')",
                [(msg_id, group, name, ts, ts) for msg_id, group, name, ts in rows],
            )

        # 要的是「最新那条带名字的消息」而不是最新那条；名字等于群号的行不算名字。
        self.assertEqual({"g1": "真实群名"}, self.store.group_names())

        # group_names 表里 NapCat 报回来的名字更权威。
        self.assertEqual(2, self.store.remember_group_names({"g1": "缓存群名", "g9": "只在这一张表里"}))
        self.assertEqual({"g1": "缓存群名", "g9": "只在这一张表里"}, self.store.group_names())


class QuarantineTests(TestCase):
    """损坏库隔离：搬不干净就不能在原路径建新库，否则坏文件被覆盖、数据真没了。"""

    def test_partial_quarantine_raises_and_keeps_the_broken_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.sqlite3"
            path.write_bytes(b"not a database")
            quarantined = Store(path)
            before = path.read_bytes()
            with mock.patch.object(Store, "_integrity_problem", return_value="malformed"):
                with mock.patch.object(Path, "rename", side_effect=OSError("locked")):
                    with self.assertRaises(RuntimeError) as error:
                        quarantined._quarantine_broken_database()
            self.assertIn("隔离不完整", str(error.exception))
            self.assertEqual(before, path.read_bytes())


if __name__ == "__main__":
    run_tests()
