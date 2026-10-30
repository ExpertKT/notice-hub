from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest.config import Settings  # noqa: E402
from qq_live_digest.event_extract import extract_events  # noqa: E402
from qq_live_digest.push import Pusher  # noqa: E402
from qq_live_digest.retry import LLMRequestError  # noqa: E402
from qq_live_digest.service import DigestService  # noqa: E402
from qq_live_digest.store import Store  # noqa: E402
from qq_live_digest.summarizer import Digest, build_digest  # noqa: E402
from qq_live_digest.timeutil import iso, now_local  # noqa: E402

# 日期必须跟着今天走：消息会真写进 store，而 prune() 用真实时钟按 30 天保留期删数据。
# 写死日期的话，一个月后这些测试数据会被裁剪掉，测试在某个整点「突然开始红」。
# 只固定 18:00 这个小时（有用例依赖时段行为），日期用今天。
NOW = now_local().replace(hour=18, minute=0, second=0, microsecond=0)


def day(days: int, hour: int, minute: int = 0) -> dt.datetime:
    """以今天为锚（时间固定 18:00）推算第 N 天的某个时刻。

    这些用例的截止/提醒时间都相对「当天」才有意义；写死 2026-09-xx 会落到
    prune() 的 30 天保留期之外（数据被裁掉），或者与真实时钟跨天后语义漂移。
    """
    return NOW.replace(hour=hour, minute=minute) + dt.timedelta(days=days)


class FakeBot:
    def __init__(self) -> None:
        self.api = None
        self.loop = None
        self.is_alive = False
        self.is_ready = False
        self.last_error = ""
        self.starts = 0
        self.stops = 0

    def start(self) -> bool:
        self.starts += 1
        self.is_alive = True
        return True

    def stop(self) -> None:
        self.stops += 1
        self.is_alive = False

    def wait_ready(self, timeout: float = 0) -> bool:
        return True


class RecordingPusher(Pusher):
    name = "webhook"
    tier = 0

    def __init__(self, *, fail_times: int = 0) -> None:
        super().__init__("test-target")
        self.fail_times = fail_times
        self.calls: list[str] = []

    def send(self, title: str, body: str, *, summary: str = "", html: str = "") -> None:
        self.calls.append(body)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("模拟推送失败")


def record(
    msg_id: str,
    content: str,
    *,
    minutes_ago: int,
    group: str = "g1",
    sender: str = "辅导员",
) -> dict:
    stamp = NOW - dt.timedelta(minutes=minutes_ago)
    return {
        "msg_id": msg_id,
        "source": "qqbot",
        "event": "GROUP_MESSAGE_CREATE",
        "group_id": group,
        "group_name": "学院通知群",
        "sender_id": "u1",
        "sender_name": sender,
        "ts": iso(stamp),
        "received_at": iso(stamp),
        "content": content,
    }


class ServiceTest(unittest.TestCase):
    def test_web_server_gets_safe_manual_scan_callback(self) -> None:
        self.settings.web_enabled = True
        web = mock.Mock()
        with mock.patch("qq_live_digest.service.TaskWebServer", return_value=web):
            self.service.start(start_bot=False)
            try:
                self.assertFalse(web.start_scan("dingtalk"))
                self.assertFalse(web.start_scan("wechat"))
                self.assertFalse(web.start_scan("unknown"))
            finally:
                self.service.stop()

    def test_start_scan_accepts_only_enabled_sources(self) -> None:
        self.settings.dingtalk_enabled = True
        self.settings.dingtalk_group_whitelist = ("dt-group",)
        self.settings.wechat_enabled = False
        self.service._dingtalk_reader = mock.Mock()
        self.assertFalse(self.service.start_scan("unknown"))
        self.assertFalse(self.service.start_scan("wechat"))
        self.assertTrue(self.service.start_scan("dingtalk"))
        self.service._dingtalk_reader.start_scan.assert_called_once_with()
        self.assertIn("scan_state", self.settings.dingtalk_status)
        self.assertIn("scan_state", self.settings.wechat_status)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "学院通知群"},
            window_minutes=10,
            min_score=3,
            delivery_retry_seconds=0,
        )
        self.store = Store(Path(self.tmp.name) / "service.sqlite3")
        self.pusher = RecordingPusher()
        self.bot = FakeBot()
        self.service = DigestService(
            self.settings,
            store=self.store,
            bot=self.bot,
            pushers=[self.pusher],
        )

    def test_rules_on_message_creates_task_from_matching_message(self) -> None:
        from qq_live_digest import rules as rules_module

        self.settings.lab_rules = True
        rules_module.save_rules(self.settings, [{
            "id": "hw",
            "match": {"keyword": ["作业"]},
            "action": {"action": "task", "payload": {"category": "urgent", "importance": 5}},
        }])
        record = {
            "msg_id": "m-rule-1", "source": "qqbot", "event": "GROUP_MESSAGE_CREATE",
            "group_id": "g1", "group_name": "学院通知群", "sender_id": "u1", "sender_name": "老师",
            "content": "作业周五截止", "received_at": NOW,
        }
        self.assertTrue(self.service.on_message(record))
        tasks = self.store.list_tasks(statuses=("candidate", "open"), limit=10)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["summary"], "作业周五截止")
        self.assertEqual(tasks[0]["category"], "urgent")
        self.assertEqual(tasks[0]["importance"], 5)
        self.assertEqual(tasks[0]["source"], "rule")

    def test_rules_do_not_create_task_when_message_does_not_match(self) -> None:
        from qq_live_digest import rules as rules_module

        self.settings.lab_rules = True
        rules_module.save_rules(self.settings, [{"id": "hw", "match": {"keyword": ["作业"]}, "action": "task"}])
        self.assertTrue(self.service.on_message({
            "msg_id": "m-rule-2", "source": "qqbot", "event": "GROUP_MESSAGE_CREATE",
            "group_id": "g1", "group_name": "学院通知群", "content": "哈哈哈", "received_at": NOW,
        }))
        self.assertEqual(self.store.list_tasks(statuses=("candidate", "open"), limit=10), [])

    def test_rules_are_inert_when_switch_is_off(self) -> None:
        from qq_live_digest import rules as rules_module

        # 规则文件存在，但开关是关的（默认）：一条都不该动。
        rules_module.save_rules(self.settings, [{"id": "hw", "match": {"keyword": ["作业"]}, "action": "task"}])
        self.assertFalse(self.settings.lab_enabled("rules"))
        self.service.on_message({
            "msg_id": "m-rule-3", "source": "qqbot", "event": "GROUP_MESSAGE_CREATE",
            "group_id": "g1", "group_name": "学院通知群", "content": "作业周五截止", "received_at": NOW,
        })
        self.assertEqual(self.store.list_tasks(statuses=("candidate", "open"), limit=10), [])

    def test_broken_rules_file_does_not_break_message_ingest(self) -> None:
        self.settings.lab_rules = True
        path = self.settings.data_dir / "rules.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ 这不是 JSON", encoding="utf-8")
        self.assertTrue(self.service.on_message({
            "msg_id": "m-rule-4", "source": "qqbot", "event": "GROUP_MESSAGE_CREATE",
            "group_id": "g1", "group_name": "学院通知群", "content": "作业周五截止", "received_at": NOW,
        }))
        self.assertEqual(self.store.list_tasks(statuses=("candidate", "open"), limit=10), [])

    def test_rule_hit_is_idempotent_for_same_message(self) -> None:
        from qq_live_digest import rules as rules_module

        self.settings.lab_rules = True
        rules_module.save_rules(self.settings, [{"id": "hw", "match": {"keyword": ["作业"]}, "action": "task"}])
        record = {
            "msg_id": "m-rule-5", "source": "qqbot", "event": "GROUP_MESSAGE_CREATE",
            "group_id": "g1", "group_name": "学院通知群", "content": "作业周五截止", "received_at": NOW,
        }
        self.service.on_message(record)
        self.service._apply_rules(record, "m-rule-5", "g1", "学院通知群")
        self.assertEqual(len(self.store.list_tasks(statuses=("candidate", "open"), limit=10)), 1)

    def test_task_url_uses_configured_public_base(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            web_base_url="https://qq-digest.tailnet.ts.net",
            web_token="secret",
        )
        store = Store(Path(self.tmp.name) / "public-url.sqlite3")
        service = DigestService(settings, store=store, bot=self.bot, pushers=[self.pusher])
        self.assertEqual(
            service._task_url(7),
            "https://qq-digest.tailnet.ts.net/?token=secret#task-7",
        )

    def test_window_flush_pushes_focus_items_once(self) -> None:
        self.assertTrue(self.service.on_message(record("m1", "【学院通知】关于2026年国庆节放假安排的通知", minutes_ago=11)))
        produced = self.service.tick(now=NOW)
        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0].kind, "window")
        self.assertEqual(len(self.pusher.calls), 1)
        self.assertIn("国庆节", self.pusher.calls[0])

        # 再次调度不会重复推送
        self.assertEqual(self.service.tick(now=NOW), [])
        self.assertEqual(len(self.pusher.calls), 1)
        self.assertEqual(self.store.counts()["unprocessed"], 0)

    def test_digest_writes_open_task(self) -> None:
        self.service.on_message(record("m1", "请各班班长今天18:00前提交材料", minutes_ago=11))
        produced = self.service.tick(now=NOW)
        self.assertEqual(len(produced), 1)
        tasks = self.store.list_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "open")
        self.assertIn("提交材料", tasks[0]["summary"])
        self.assertEqual(tasks[0]["groups"], ["学院通知群"])

    def test_event_time_fills_missing_llm_deadline(self) -> None:
        # LLM 只给了「要开会」，没给时间；event_extract 从原文补出 15:30 和地点。
        item = {
            "message": SimpleNamespace(text="明天下午3点半在A楼202开会", sender="李老师"),
            "category": "info",
            "action": "",
            "summary": "明天下午在A楼202开会",
            "details": [],
            "group_name": "学院通知群",
            "msg_id": "m-event-1",
        }
        digest = Digest(kind="window", window_start=NOW, window_end=NOW, items=[item], id=1)
        with mock.patch("qq_live_digest.event_extract.now_local", return_value=NOW):
            self.service._sync_tasks(digest)
        tasks = self.store.list_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["summary"], "明天下午在A楼202开会")
        self.assertTrue(tasks[0]["deadline"].startswith(day(1, 15, 30).isoformat(timespec="minutes")), tasks[0]["deadline"])
        self.assertTrue(any("A楼202" in detail for detail in tasks[0]["details"]), tasks[0]["details"])

    def test_llm_deadline_wins_and_event_only_fills_gaps(self) -> None:
        # LLM 已经给了 action/deadline：时间与摘要都以 LLM 为准，事件只补 condition 缺口。
        item = {
            "message": SimpleNamespace(text="明天下午3点半在A楼202开会", sender="李老师"),
            "category": "action",
            "action": "开会",
            "deadline_dt": day(2, 9, 0),
            "summary": "开会",
            "condition": "",
            "details": [],
            "group_name": "学院通知群",
            "msg_id": "m-event-3",
        }
        digest = Digest(kind="window", window_start=NOW, window_end=NOW, items=[item], id=1)
        with mock.patch("qq_live_digest.event_extract.now_local", return_value=NOW):
            self.service._sync_tasks(digest)
        task = self.store.list_tasks()[0]
        self.assertTrue(task["deadline"].startswith(day(2, 9, 0).isoformat(timespec="minutes")), task["deadline"])
        self.assertEqual(task["summary"], "开会")
        self.assertIn("A楼202", task["condition"])
        self.assertLessEqual(float(task["confidence"]), 0.80)

    def test_low_confidence_event_does_not_become_deadline(self) -> None:
        # 只有地点没有时间 → low；min_level=medium 要把它挡在待办之外。
        text = "材料放在A楼202"
        self.assertTrue(extract_events(text, now=NOW, min_level="low"))
        item = {
            "message": SimpleNamespace(text=text, sender="李老师"),
            "category": "info",
            "action": "",
            "summary": "材料放在A楼202",
            "details": [],
            "group_name": "学院通知群",
            "msg_id": "m-event-2",
        }
        digest = Digest(kind="window", window_start=NOW, window_end=NOW, items=[item], id=1)
        with mock.patch("qq_live_digest.event_extract.now_local", return_value=NOW):
            self.service._sync_tasks(digest)
        self.assertNotIn("deadline_dt", item)
        self.assertEqual(self.store.list_tasks(), [])

    def test_import_existing_tasks_fills_deadline_and_skips_expired(self) -> None:
        # 历史回填也要「先合并再判」：事件补出的时间能进待办，已经过期的照旧被挡。
        base = {"group": "g1", "sender": "李老师", "score": 4, "importance": 4, "created_at": iso(NOW)}
        self.store.insert_digest(
            kind="window",
            window_start=iso(NOW),
            window_end=iso(NOW),
            body="",
            message_count=2,
            llm_used=False,
            payload=[
                {**base, "text": "明天下午3点半在A楼202开会", "category": "info", "summary": "开会"},
                {**base, "text": "去年的会议纪要", "category": "action", "summary": "旧事项", "deadline": "2020-01-01T09:00:00"},
            ],
        )
        with mock.patch("qq_live_digest.service.now_local", return_value=NOW), mock.patch(
            "qq_live_digest.event_extract.now_local", return_value=NOW
        ):
            total = self.service.import_existing_tasks(days=7)

        self.assertEqual(total, 1)
        tasks = self.store.list_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["summary"], "开会")
        self.assertTrue(tasks[0]["deadline"].startswith(day(1, 15, 30).isoformat(timespec="minutes")), tasks[0]["deadline"])

    def test_location_detail_is_not_duplicated_when_merged_twice(self) -> None:
        # 回填会先合并一次、_sync_tasks 再合并一次：地点只能留一条。
        item = {
            "message": SimpleNamespace(text="明天下午3点半在A楼202开会", sender="李老师"),
            "category": "info",
            "action": "",
            "summary": "开会",
            "details": [],
            "group_name": "学院通知群",
            "msg_id": "m-event-4",
        }
        with mock.patch("qq_live_digest.event_extract.now_local", return_value=NOW):
            self.service._merge_extracted_event(item)
            self.service._merge_extracted_event(item)
        self.assertEqual(item["details"], ["地点：A楼202"])

    def test_multi_event_keeps_earliest_deadline_and_every_place(self) -> None:
        # 一条消息两个事件：时间取最早，地点都要留下（旧实现命中第一个就 return，B楼会丢）。
        item = {
            "message": SimpleNamespace(text="明天下午3点半在A楼202开会；后天上午9点在B楼301开会", sender="李老师"),
            "category": "info",
            "action": "",
            "summary": "开会",
            "details": [],
            "group_name": "学院通知群",
            "msg_id": "m-event-5",
        }
        digest = Digest(kind="window", window_start=NOW, window_end=NOW, items=[item], id=1)
        with mock.patch("qq_live_digest.event_extract.now_local", return_value=NOW):
            self.service._sync_tasks(digest)
        task = self.store.list_tasks()[0]
        self.assertTrue(task["deadline"].startswith(day(1, 15, 30).isoformat(timespec="minutes")), task["deadline"])
        self.assertEqual(task["details"], ["地点：A楼202", "地点：B楼301"])

    def test_medium_event_without_action_does_not_create_task(self) -> None:
        # 「今天下午3点放假，停电通知」：事件只到 medium（没地点没动词），纯信息不该变待办。
        item = {
            "message": SimpleNamespace(text="今天下午3点放假，停电通知", sender="李老师"),
            "category": "info",
            "action": "",
            "summary": "停电通知",
            "details": [],
            "group_name": "学院通知群",
            "msg_id": "m-event-6",
        }
        digest = Digest(kind="window", window_start=NOW, window_end=NOW, items=[item], id=1)
        with mock.patch("qq_live_digest.event_extract.now_local", return_value=NOW):
            self.service._sync_tasks(digest)
        # 时间确实是事件补出来的——旧门槛会因此放行，所以这条断言证明收紧生效而不是没抽到。
        self.assertIsInstance(item.get("deadline_dt"), dt.datetime)
        self.assertEqual(self.store.list_tasks(), [])

    def test_import_existing_tasks_prefers_future_event_over_expired_one(self) -> None:
        # 一条消息两个事件：9月20日那条已过期，明天那条还没到 → 用明天的时间建，别整条丢掉。
        base = {"group": "g1", "sender": "李老师", "score": 4, "importance": 4, "created_at": iso(NOW)}
        self.store.insert_digest(
            kind="window",
            window_start=iso(NOW),
            window_end=iso(NOW),
            body="",
            message_count=1,
            llm_used=False,
            payload=[
                {
                    **base,
                    "text": "2026年9月20日下午3点在A楼202开会；明天下午3点半在B楼301开会",
                    "category": "info",
                    "summary": "开会",
                }
            ],
        )
        with mock.patch("qq_live_digest.service.now_local", return_value=NOW), mock.patch(
            "qq_live_digest.event_extract.now_local", return_value=NOW
        ):
            total = self.service.import_existing_tasks(days=7)

        self.assertEqual(total, 1)
        task = self.store.list_tasks()[0]
        self.assertTrue(task["deadline"].startswith(day(1, 15, 30).isoformat(timespec="minutes")), task["deadline"])
        self.assertIn("地点：B楼301", task["details"])

    def test_task_sync_failure_does_not_block_push(self) -> None:
        # 消息在同步任务之前就标了 processed，同步炸了也必须把摘要推出去，
        # 否则用户既收不到通知、任务也没建。
        self.service.on_message(record("m1", "请各班班长今天18:00前提交材料", minutes_ago=11))
        with mock.patch.object(DigestService, "_sync_tasks", side_effect=RuntimeError("建任务炸了")):
            produced = self.service.tick(now=NOW)
        self.assertEqual(len(produced), 1)
        self.assertEqual(len(self.pusher.calls), 1)
        self.assertEqual(self.store.counts()["unprocessed"], 0)
        self.assertEqual(self.store.list_tasks(), [])

    def test_within_window_waits_then_flushes(self) -> None:
        self.service.on_message(record("m1", "【学院通知】放假安排", minutes_ago=1))
        self.assertEqual(self.service.tick(now=NOW), [])
        self.assertEqual(self.store.counts()["unprocessed"], 1)

        later = NOW + dt.timedelta(minutes=10)
        produced = self.service.tick(now=later)
        self.assertEqual(len(produced), 1)
        self.assertEqual(len(self.pusher.calls), 1)

    def test_immediate_group_flushes_without_waiting_window(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "测试群"},
            immediate_groups=("g1",),
            window_minutes=30,
            min_score=3,
            delivery_retry_seconds=0,
        )
        store = Store(Path(self.tmp.name) / "immediate.sqlite3")
        pusher = RecordingPusher()
        service = DigestService(settings, store=store, bot=FakeBot(), pushers=[pusher])
        service.on_message(record("m1", "【学院通知】关于2026年国庆节放假安排的通知", minutes_ago=1))
        produced = service.tick(now=NOW)
        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0].kind, "window")
        self.assertEqual(len(pusher.calls), 1)
        self.assertEqual(store.counts()["unprocessed"], 0)

    def test_chatter_window_is_silent(self) -> None:
        self.service.on_message(record("m1", "哈哈哈哈", minutes_ago=11, sender="同学A"))
        produced = self.service.tick(now=NOW)
        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0].kind, "silent")
        self.assertEqual(self.pusher.calls, [])
        self.assertEqual(self.store.counts()["unprocessed"], 0)
        self.assertEqual(self.store.counts()["digests"], 1)

    def test_urgent_pushed_immediately(self) -> None:
        self.service.on_message(record("m1", "请各班班长今天18:00前提交材料", minutes_ago=1))
        produced = self.service.tick(now=NOW)
        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0].kind, "urgent")
        self.assertEqual(len(self.pusher.calls), 1)
        self.assertIn("截止", self.pusher.calls[0])
        self.assertEqual(produced[0].items[0]["category"], "urgent")

    def test_quiet_group_chatter_is_silent_and_never_becomes_a_task(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "电气工程学院2026级新生交流群"},
            quiet_groups=("g1",),
            window_minutes=10,
            min_score=3,
            delivery_retry_seconds=0,
        )
        store = Store(Path(self.tmp.name) / "quiet-chatter.sqlite3")
        pusher = RecordingPusher()
        service = DigestService(settings, store=store, bot=FakeBot(), pushers=[pusher])
        service.on_message(record(
            "m1",
            "不会的吧，我今天问4栋那个快递站明天可以寄快递吗他还说可以的",
            minutes_ago=1,
            sender="26测仪",
        ))
        self.assertEqual(service.tick(now=NOW), [])
        produced = service.tick(now=NOW + dt.timedelta(minutes=11))
        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0].kind, "silent")
        self.assertEqual(pusher.calls, [])
        self.assertEqual(store.list_tasks(), [])

    def test_quiet_group_real_notice_goes_to_window_and_task(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "电气工程学院2026级新生交流群"},
            quiet_groups=("g1",),
            window_minutes=10,
            min_score=3,
            delivery_retry_seconds=0,
        )
        store = Store(Path(self.tmp.name) / "quiet-notice.sqlite3")
        pusher = RecordingPusher()
        service = DigestService(settings, store=store, bot=FakeBot(), pushers=[pusher])
        service.on_message(record(
            "m2",
            "请各班班长明天18:00前提交材料",
            minutes_ago=1,
            sender="辅导员",
        ))
        self.assertEqual(service.tick(now=NOW), [])
        produced = service.tick(now=NOW + dt.timedelta(minutes=11))
        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0].kind, "window")
        self.assertEqual(len(pusher.calls), 1)
        self.assertEqual(len(store.list_tasks()), 1)

    def test_catchup_total_failure_retries_after_five_minutes(self) -> None:
        self.service.settings.catchup_enabled = True
        def fake_backfill(
            settings, store, logger=None, *, now=None, force=False, stats=None, attachment_sink=None
        ):
            if stats is not None:
                stats.update({"groups": 1, "ok_groups": 0, "failed_groups": 1, "inserted": 0})
            return 0

        with mock.patch("qq_live_digest.service.backfill", side_effect=fake_backfill):
            self.assertEqual(self.service.run_catchup(now=NOW), 0)
            self.assertIsNone(self.service.last_catchup_at)
            self.assertEqual(self.service.last_catchup_attempt_at, NOW)

            self.service._maybe_catchup(NOW + dt.timedelta(minutes=1))
            self.assertIsNone(self.service._catchup_thread)

            self.service._maybe_catchup(NOW + dt.timedelta(minutes=6))
            self.assertIsNotNone(self.service._catchup_thread)
            self.service._catchup_thread.join(timeout=5)

    def test_catchup_backoff_doubles_and_caps(self) -> None:
        self.service.settings.catchup_enabled = True
        self.service._catchup_failures = 1
        self.service.last_catchup_attempt_at = NOW
        self.service._maybe_catchup(NOW + dt.timedelta(minutes=4))
        self.assertIsNone(self.service._catchup_thread)
        self.service._catchup_failures = 20
        self.service._maybe_catchup(NOW + dt.timedelta(minutes=29))
        self.assertIsNone(self.service._catchup_thread)

    def test_duplicate_and_whitelist_filter(self) -> None:
        payload = record("m1", "【学院通知】放假安排", minutes_ago=11)
        self.assertTrue(self.service.on_message(payload))
        self.assertFalse(self.service.on_message(payload))
        self.assertFalse(self.service.on_message(record("m2", "通知", minutes_ago=11, group="g2")))
        self.assertEqual(self.store.counts()["messages"], 1)

    def test_failed_push_is_retried_in_same_tick(self) -> None:
        self.pusher.fail_times = 1
        self.service.on_message(record("m1", "【学院通知】放假安排", minutes_ago=11))
        produced = self.service.tick(now=NOW)
        self.assertEqual(len(produced), 1)
        # retry_seconds=0：同一轮调度内失败重试一次并成功
        self.assertEqual(len(self.pusher.calls), 2)
        counts = self.store.counts()
        self.assertEqual(counts["deliveries_sent"], 1)
        self.assertEqual(counts["deliveries_failed"], 0)

    def test_failed_push_waits_for_cooldown(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            window_minutes=10,
            delivery_retry_seconds=3600,
        )
        store = Store(Path(self.tmp.name) / "cooldown.sqlite3")
        pusher = RecordingPusher(fail_times=1)
        service = DigestService(settings, store=store, bot=FakeBot(), pushers=[pusher])
        service.on_message(record("m1", "【学院通知】放假安排", minutes_ago=11))
        service.tick(now=NOW)
        self.assertEqual(len(pusher.calls), 1)
        service.tick(now=NOW + dt.timedelta(seconds=30))
        self.assertEqual(len(pusher.calls), 1)
        self.assertEqual(store.counts()["deliveries_failed"], 1)
        # 冷却结束后手动重试成功
        self.assertEqual(len(store.pending_deliveries(max_attempts=4, retry_seconds=0)), 1)
        outcomes = service.push_manager().retry_pending(retry_seconds=0)
        self.assertTrue(any(item.ok for item in outcomes))
        self.assertEqual(len(pusher.calls), 2)

    def test_status_reports_components(self) -> None:
        status = self.service.status()
        self.assertIn("counts", status)
        self.assertEqual(status["channels"], ["webhook:test-target"])

    def test_start_and_stop_with_injected_bot(self) -> None:
        self.service.start(start_bot=False)
        self.service.stop()
        status = self.service.status()
        self.assertEqual(status["started_at"], "")

    def test_deadline_reminders_fire_once_and_cover_tomorrow(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "学院通知群"},
            window_minutes=10,
            min_score=3,
            catchup_enabled=False,
            deadline_reminders_enabled=True,
            deadline_morning="07:30",
            deadline_evening="21:00",
        )
        store = Store(Path(self.tmp.name) / "reminders.sqlite3")
        pusher = RecordingPusher()
        service = DigestService(settings, store=store, bot=FakeBot(), pushers=[pusher])
        start = day(1, 6, 0)

        store.upsert_task(
            task_key="m1",
            summary="今天提交材料",
            category="action",
            deadline=iso(day(1, 18, 0)),
            groups=["学院通知群"],
        )
        store.upsert_task(
            task_key="m2",
            summary="明天提交报名表",
            category="action",
            deadline=iso(day(2, 12, 0)),
            groups=["学院通知群"],
        )

        service.tick(now=day(1, 7, 31))
        self.assertEqual(len(pusher.calls), 1)
        self.assertIn("今日截止", pusher.calls[0])
        self.assertIn(day(1, 18, 0).strftime("%Y-%m-%d %H:%M"), pusher.calls[0])
        self.assertNotIn(day(2, 0, 0).strftime("%Y-%m-%d"), pusher.calls[0])

        service.tick(now=day(1, 8, 0))
        self.assertEqual(len(pusher.calls), 1)

        service.tick(now=day(1, 21, 0))
        self.assertEqual(len(pusher.calls), 2)
        self.assertIn("截止提醒", pusher.calls[1])
        self.assertIn(day(2, 12, 0).strftime("%Y-%m-%d %H:%M"), pusher.calls[1])
        self.assertNotIn(day(1, 18, 0).strftime("%Y-%m-%d %H:%M"), pusher.calls[1])

    def test_missing_channels_warning_is_rate_limited(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            catchup_enabled=False,
            deadline_reminders_enabled=True,
            deadline_morning="07:30",
            deadline_evening="21:00",
        )
        store = Store(Path(self.tmp.name) / "reminders-no-channel.sqlite3")
        logger = mock.Mock()
        service = DigestService(settings, store=store, bot=FakeBot(), pushers=[], logger=logger)
        store.upsert_task(
            task_key="m1", summary="今天提交材料", category="action",
            deadline=iso(day(1, 18, 0)), groups=["g1"],
        )
        start = day(1, 7, 31)
        for minutes in (0, 1, 14, 28, 59):
            service._maybe_deadline_reminders(start + dt.timedelta(minutes=minutes))
        warnings = [call for call in logger.warning.call_args_list if "无可用推送通道" in str(call)]
        self.assertLessEqual(len(warnings), 2)

    def test_late_evening_start_skips_morning_reminder(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "学院通知群"},
            window_minutes=10,
            min_score=3,
            catchup_enabled=False,
            deadline_reminders_enabled=True,
            deadline_morning="07:30",
            deadline_evening="21:00",
        )
        store = Store(Path(self.tmp.name) / "reminders-late.sqlite3")
        pusher = RecordingPusher()
        service = DigestService(settings, store=store, bot=FakeBot(), pushers=[pusher])
        store.upsert_task(
            task_key="m1",
            summary="明天提交报名表",
            category="action",
            deadline=iso(day(2, 12, 0)),
            groups=["学院通知群"],
        )

        service.tick(now=day(1, 22, 0))
        self.assertEqual(len(pusher.calls), 1)
        self.assertIn("截止提醒", pusher.calls[0])
        self.assertNotIn("今日截止", pusher.calls[0])
        self.assertIn(day(2, 12, 0).strftime("%Y-%m-%d %H:%M"), pusher.calls[0])


    def test_ambiguous_action_becomes_candidate(self) -> None:
        self.service.on_message(record("m1", "记得明天可能要交报名表", minutes_ago=11, sender="同学"))
        self.service.tick(now=NOW)
        tasks = self.store.list_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "candidate")
        self.assertLess(tasks[0]["confidence"], 0.75)

    def test_candidate_confirmation_retries_and_is_daily_limited(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            delivery_retry_seconds=0,
            candidate_push_max_per_tick=1,
        )
        store = Store(Path(self.tmp.name) / "candidate-push.sqlite3")
        pusher = RecordingPusher(fail_times=1)
        service = DigestService(settings, store=store, bot=FakeBot(), pushers=[pusher])
        task_id = store.upsert_task(
            task_key="candidate-1",
            summary="可能要交报名表",
            category="action",
            evidence="记得看一下报名表",
            status="candidate",
            confidence=0.7,
        )

        service.tick(now=NOW)
        self.assertEqual(store.get_task(task_id)["status"], "candidate")
        self.assertEqual(store.get_task(task_id)["last_reminded_at"], "")

        service.tick(now=NOW + dt.timedelta(minutes=1))
        self.assertEqual(len(pusher.calls), 2)
        self.assertTrue(store.get_task(task_id)["last_reminded_at"])
        self.assertEqual(store.get_task(task_id)["status"], "candidate")

        service.tick(now=NOW + dt.timedelta(hours=2))
        self.assertEqual(len(pusher.calls), 2)

    def test_overdue_open_task_is_reminded_but_dismissed_is_not(self) -> None:
        clear = Settings(
            group_whitelist=("g1",),
            catchup_enabled=False,
            deadline_morning="07:30",
            deadline_evening="21:00",
        )
        store = Store(Path(self.tmp.name) / "overdue.sqlite3")
        pusher = RecordingPusher()
        service = DigestService(clear, store=store, bot=FakeBot(), pushers=[pusher])
        open_id = store.upsert_task(
            task_key="open-overdue",
            summary="昨天应交的材料",
            category="action",
            deadline=iso(day(0, 18, 0)),
        )
        dismissed_id = store.upsert_task(
            task_key="dismissed",
            summary="已忽略事项",
            category="action",
            deadline=iso(day(0, 19, 0)),
        )
        store.apply_task_action(dismissed_id, "dismiss")

        service.tick(now=NOW.replace(hour=8, minute=0))
        self.assertEqual(len(pusher.calls), 1)
        self.assertIn("昨天应交的材料", pusher.calls[0])
        self.assertNotIn("已忽略事项", pusher.calls[0])
        self.assertTrue(store.get_task(open_id)["last_reminded_at"])
        self.assertEqual(store.get_task(dismissed_id)["remind_count"], 0)

        # 同一任务在同一天再次调度不会重复提醒。
        service.tick(now=NOW.replace(hour=9, minute=0))
        self.assertEqual(len(pusher.calls), 1)


    def test_deadline_reminder_without_channel_keeps_retry_key(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            catchup_enabled=False,
            deadline_morning="07:30",
            deadline_evening="21:00",
        )
        store = Store(Path(self.tmp.name) / "no-channel.sqlite3")
        service = DigestService(settings, store=store, bot=FakeBot(), pushers=[])
        store.upsert_task(
            task_key="m1",
            summary="今天提交材料",
            category="action",
            deadline=iso(day(0, 18, 0)),
            groups=["学院通知群"],
        )
        service.tick(now=NOW.replace(hour=7, minute=31))
        self.assertEqual(store.meta_get("deadline_reminder:morning:%s" % NOW.strftime("%Y-%m-%d")), "")

        pusher = RecordingPusher()
        service._pushers = [pusher]
        service._push_manager = None
        service.tick(now=NOW.replace(hour=7, minute=32))
        self.assertEqual(len(pusher.calls), 1)
        self.assertTrue(store.meta_get("deadline_reminder:morning:%s" % NOW.strftime("%Y-%m-%d")))

    def test_retryable_llm_failure_defers_batch_then_recovers(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "学院通知群"},
            window_minutes=1,
            min_score=3,
            delivery_retry_seconds=0,
            dashscope_api_key="test-key",
            llm_max_retries=0,
            llm_retry_backoff=0.0,
            llm_defer_max_attempts=3,
        )
        store = Store(Path(self.tmp.name) / "llm-defer.sqlite3")
        pusher = RecordingPusher()
        service = DigestService(settings, store=store, bot=FakeBot(), pushers=[pusher])
        service.on_message(record("m1", "【学院通知】关于2026年国庆节放假安排的通知", minutes_ago=2))

        with mock.patch(
            "qq_digest.refine_with_dashscope",
            side_effect=LLMRequestError("LLM 请求失败: connection refused"),
        ):
            service.tick(now=NOW)
        self.assertEqual(len(pusher.calls), 0)
        self.assertEqual(store.counts()["unprocessed"], 1)
        self.assertEqual(store.meta_get("llm_defer:count"), "1")

        with mock.patch(
            "qq_digest.refine_with_dashscope",
            side_effect=lambda items, *args, **kwargs: items,
        ):
            service.tick(now=NOW + dt.timedelta(minutes=1))
        self.assertEqual(len(pusher.calls), 1)
        self.assertEqual(store.counts()["unprocessed"], 0)
        self.assertEqual(store.meta_get("llm_defer:count"), "0")

    def test_non_retryable_llm_failure_falls_back_without_blocking(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "学院通知群"},
            window_minutes=1,
            min_score=3,
            delivery_retry_seconds=0,
            dashscope_api_key="test-key",
            llm_max_retries=0,
            llm_retry_backoff=0.0,
        )
        store = Store(Path(self.tmp.name) / "llm-auth.sqlite3")
        pusher = RecordingPusher()
        service = DigestService(settings, store=store, bot=FakeBot(), pushers=[pusher])
        service.on_message(record("m1", "【学院通知】关于2026年国庆节放假安排的通知", minutes_ago=2))

        with mock.patch(
            "qq_digest.refine_with_dashscope",
            side_effect=LLMRequestError("LLM HTTP 401: bad key", status=401),
        ):
            service.tick(now=NOW)
        self.assertEqual(len(pusher.calls), 1)
        self.assertEqual(store.counts()["unprocessed"], 0)
        self.assertEqual(store.meta_get("llm_fallbacks_total"), "1")

    def test_quiet_hours_hold_batch_until_morning(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "学院通知群"},
            window_minutes=1,
            min_score=3,
            delivery_retry_seconds=0,
            quiet_hours="23:00-07:00",
        )
        store = Store(Path(self.tmp.name) / "quiet.sqlite3")
        pusher = RecordingPusher()
        service = DigestService(settings, store=store, bot=FakeBot(), pushers=[pusher])
        service.on_message(record("m1", "【学院通知】关于2026年国庆节放假安排的通知", minutes_ago=2))

        service.tick(now=NOW.replace(hour=23, minute=30))
        self.assertEqual(len(pusher.calls), 0)
        self.assertEqual(store.counts()["unprocessed"], 1)
        self.assertEqual(service.last_defer_reason, "quiet_hours")

        morning = (NOW + dt.timedelta(days=1)).replace(hour=7, minute=1)
        service.tick(now=morning)
        self.assertEqual(len(pusher.calls), 1)
        self.assertEqual(store.counts()["unprocessed"], 0)

    def test_daily_push_budget_holds_extra_batches(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "学院通知群"},
            window_minutes=1,
            min_score=3,
            delivery_retry_seconds=0,
            push_daily_budget=1,
        )
        store = Store(Path(self.tmp.name) / "budget.sqlite3")
        pusher = RecordingPusher()
        service = DigestService(settings, store=store, bot=FakeBot(), pushers=[pusher])
        service.on_message(record("m1", "【学院通知】关于2026年国庆节放假安排的通知", minutes_ago=11))
        service.tick(now=NOW)
        self.assertEqual(len(pusher.calls), 1)
        self.assertEqual(store.meta_get("push_budget:%s" % NOW.strftime("%Y-%m-%d")), "1")

        service.on_message(record("m2", "【学院通知】关于2026年国庆节后课程调整的通知", minutes_ago=11))
        service.tick(now=NOW + dt.timedelta(minutes=1))
        self.assertEqual(len(pusher.calls), 1)
        self.assertEqual(store.counts()["unprocessed"], 1)
        self.assertEqual(service.last_defer_reason, "daily_budget")

    def test_urgent_bypasses_quiet_hours(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "学院通知群"},
            window_minutes=30,
            min_score=3,
            delivery_retry_seconds=0,
            quiet_hours="23:00-07:00",
        )
        store = Store(Path(self.tmp.name) / "urgent-night.sqlite3")
        pusher = RecordingPusher()
        service = DigestService(settings, store=store, bot=FakeBot(), pushers=[pusher])
        service.on_message(record("m1", "请各班班长今天18:00前提交材料", minutes_ago=1))

        service.tick(now=NOW.replace(hour=23, minute=30))
        self.assertEqual(len(pusher.calls), 1)


class DingtalkReloadTest(unittest.TestCase):
    def test_start_requires_nonempty_whitelist(self) -> None:
        """启用钉钉 + 空白名单 ⇒ reader 不启动（与 reload 条件一致）"""
        for whitelist, should_start in (((), False), (("12345",), True)):
            with self.subTest(whitelist=whitelist), tempfile.TemporaryDirectory() as tmp:
                settings = Settings(
                    data_dir=Path(tmp),
                    official_bot_enabled=False,
                    attachments_enabled=False,
                    web_enabled=False,
                    dingtalk_enabled=True,
                    dingtalk_group_whitelist=whitelist,
                )
                service = DigestService(settings, store=Store(Path(tmp) / "dingtalk.sqlite3"))
                with mock.patch("qq_live_digest.service.DingTalkLocalReader") as reader_cls:
                    service.start(start_bot=False)
                    self.assertEqual(reader_cls.called, should_start)
                    if should_start:
                        reader_cls.return_value.start.assert_called_once()
                        self.assertIsNotNone(service._dingtalk_reader)
                    else:
                        self.assertIsNone(service._dingtalk_reader)
                service.stop()

if __name__ == "__main__":
    unittest.main()
