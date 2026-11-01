"""DigestService 的调度回归：窗口/紧急/静默、待办同步、提醒、补采退避、推送闸门。

时间一律以「今天 18:00」为锚。消息会真写库，而 prune() 按真实时钟删 30 天前的数据，
写死日期会让这些用例在某个整点之后莫名其妙变红。
"""

from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from noticehub.config import Settings  # noqa: E402
from noticehub.event_extract import extract_events  # noqa: E402
from noticehub.push import Pusher  # noqa: E402
from noticehub.retry import LLMRequestError  # noqa: E402
from noticehub.service import DigestService  # noqa: E402
from noticehub.store import Store  # noqa: E402
from noticehub.summarizer import Digest, build_digest  # noqa: E402,F401
from noticehub.timeutil import iso, now_local  # noqa: E402

NOW = now_local().replace(hour=18, minute=0, second=0, microsecond=0)


def day(days: int, hour: int, minute: int = 0) -> dt.datetime:
    """相对今天（18:00 锚点）第 N 天的某个时刻。"""
    return NOW.replace(hour=hour, minute=minute) + dt.timedelta(days=days)


class FakeBot:
    """只记账的机器人替身：start/stop 各加一次计数。"""

    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0
        self.api = None
        self.loop = None
        self.is_alive = False
        self.is_ready = False
        self.last_error = ""

    def stop(self) -> None:
        self.stops += 1
        self.is_alive = False

    def start(self) -> bool:
        self.starts += 1
        self.is_alive = True
        return True

    def wait_ready(self, timeout: float = 0) -> bool:
        """替身永远就绪，不等。"""
        return True


class RecordingPusher(Pusher):
    """记录每次推送正文；fail_times 控制前几次故意失败。"""

    name = "webhook"
    tier = 0

    def __init__(self, *, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.calls: list[str] = []
        super().__init__("test-target")

    def send(self, title: str, body: str, *, summary: str = "", html: str = "") -> None:
        """记下正文；还剩失败次数就抛错，模拟通道不可用。"""
        self.calls.append(body)
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("推送失败（模拟）")


def record(msg_id: str, content: str, *, minutes_ago: int, group: str = "g1", sender: str = "辅导员") -> dict:
    stamp = NOW - dt.timedelta(minutes=minutes_ago)
    return dict(
        msg_id=msg_id,
        group_id=group,
        group_name="学院通知群",
        sender_id="u1",
        sender_name=sender,
        source="qqbot",
        event="GROUP_MESSAGE_CREATE",
        content=content,
        ts=iso(stamp),
        received_at=iso(stamp),
    )


def reminders_setup(path: Path, *, name: str):
    """截止提醒用例共用的一套装配：只测提醒，复盘与补采都关掉。"""
    settings = Settings(
        group_aliases={"g1": "学院通知群"},
        deadline_morning="07:30",
        deadline_evening="21:00",
        group_whitelist=("g1",),
        window_minutes=10,
        min_score=3,
        catchup_enabled=False,
        deadline_reminders_enabled=True,
        weekly_review_enabled=False,
    )
    store = Store(path / name)
    pusher = RecordingPusher()
    return settings, store, pusher, DigestService(settings, store=store, bot=FakeBot(), pushers=[pusher])


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.settings = self.default_settings()
        self.store = Store(self.path / "svc.sqlite3")
        self.pusher = RecordingPusher()
        self.bot = FakeBot()
        self.svc = DigestService(self.settings, store=self.store, bot=self.bot, pushers=[self.pusher])

    # ------------------------------------------------------------ 构造工具
    @staticmethod
    def default_settings(**overrides) -> Settings:
        params = {
            "group_whitelist": ("g1",),
            "group_aliases": {"g1": "学院通知群"},
            "window_minutes": 10,
            "min_score": 3,
            "delivery_retry_seconds": 0,
        }
        params.update(overrides)
        return Settings(**params)

    def new_service(self, name: str = "case", **overrides):
        """另起一套（settings、store、pusher、svc），互不干扰。"""
        settings = self.default_settings(**overrides)
        store = Store(self.path / f"{name}.sqlite3")
        pusher = RecordingPusher()
        svc = DigestService(settings, store=store, bot=FakeBot(), pushers=[pusher])
        return settings, store, pusher, svc

    # ---------------------------------------------------------------- 扫描
    def test_web_server_gets_safe_manual_scan_callback(self) -> None:
        self.settings.web_enabled = True
        web = mock.Mock()
        with mock.patch("noticehub.service.TaskWebServer", return_value=web):
            self.svc.start(start_bot=False)
            try:
                self.assertFalse(web.start_scan("dingtalk"))
                self.assertFalse(web.start_scan("wechat"))
                self.assertFalse(web.start_scan("unknown"))
            finally:
                self.svc.stop()

    def test_start_scan_accepts_only_enabled_sources(self) -> None:
        self.settings.dingtalk_enabled = True
        self.settings.dingtalk_group_whitelist = ("dt-group",)
        self.settings.wechat_enabled = False
        self.svc._dingtalk_reader = mock.Mock()

        self.assertFalse(self.svc.start_scan("unknown"))
        self.assertFalse(self.svc.start_scan("wechat"))
        self.assertTrue(self.svc.start_scan("dingtalk"))
        self.svc._dingtalk_reader.start_scan.assert_called_once_with()
        self.assertIn("scan_state", self.settings.dingtalk_status)
        self.assertIn("scan_state", self.settings.wechat_status)

    # ---------------------------------------------------------------- 规则
    def test_rules_on_message_creates_task_from_matching_message(self) -> None:
        from noticehub import rules as rules_module

        self.settings.lab_rules = True
        rules_module.save_rules(
            self.settings,
            [
                {
                    "id": "hw",
                    "match": {"keyword": ["作业"]},
                    "action": {"action": "task", "payload": {"category": "urgent", "importance": 5}},
                }
            ],
        )
        message = {
            "msg_id": "m-rule-1",
            "source": "qqbot",
            "event": "GROUP_MESSAGE_CREATE",
            "group_id": "g1",
            "group_name": "学院通知群",
            "sender_id": "u1",
            "sender_name": "老师",
            "content": "作业周五截止",
            "received_at": NOW,
        }
        self.assertTrue(self.svc.on_message(message))

        tasks = self.store.list_tasks(statuses=("candidate", "open"), limit=10)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["summary"], "作业周五截止")
        self.assertEqual(tasks[0]["category"], "urgent")
        self.assertEqual(tasks[0]["importance"], 5)
        self.assertEqual(tasks[0]["source"], "rule")

    def test_rules_do_not_create_task_when_message_does_not_match(self) -> None:
        from noticehub import rules as rules_module

        self.settings.lab_rules = True
        rules_module.save_rules(
            self.settings, [{"id": "hw", "match": {"keyword": ["作业"]}, "action": "task"}]
        )
        self.assertTrue(
            self.svc.on_message(
                {
                    "msg_id": "m-rule-2",
                    "source": "qqbot",
                    "event": "GROUP_MESSAGE_CREATE",
                    "group_id": "g1",
                    "group_name": "学院通知群",
                    "content": "哈哈哈",
                    "received_at": NOW,
                }
            )
        )
        self.assertEqual(self.store.list_tasks(statuses=("candidate", "open"), limit=10), [])

    def test_rules_are_inert_when_switch_is_off(self) -> None:
        from noticehub import rules as rules_module

        # 规则文件在，但总开关关着：一条都不许动。
        rules_module.save_rules(
            self.settings, [{"id": "hw", "match": {"keyword": ["作业"]}, "action": "task"}]
        )
        self.assertFalse(self.settings.lab_enabled("rules"))
        self.svc.on_message(
            {
                "msg_id": "m-rule-3",
                "source": "qqbot",
                "event": "GROUP_MESSAGE_CREATE",
                "group_id": "g1",
                "group_name": "学院通知群",
                "content": "作业周五截止",
                "received_at": NOW,
            }
        )
        self.assertEqual(self.store.list_tasks(statuses=("candidate", "open"), limit=10), [])

    def test_broken_rules_file_does_not_break_message_ingest(self) -> None:
        self.settings.lab_rules = True
        path = self.settings.data_dir / "rules.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ 这不是 JSON", encoding="utf-8")

        self.assertTrue(
            self.svc.on_message(
                {
                    "msg_id": "m-rule-4",
                    "source": "qqbot",
                    "event": "GROUP_MESSAGE_CREATE",
                    "group_id": "g1",
                    "group_name": "学院通知群",
                    "content": "作业周五截止",
                    "received_at": NOW,
                }
            )
        )
        self.assertEqual(self.store.list_tasks(statuses=("candidate", "open"), limit=10), [])

    def test_rule_hit_is_idempotent_for_same_message(self) -> None:
        from noticehub import rules as rules_module

        self.settings.lab_rules = True
        rules_module.save_rules(
            self.settings, [{"id": "hw", "match": {"keyword": ["作业"]}, "action": "task"}]
        )
        message = {
            "msg_id": "m-rule-5",
            "source": "qqbot",
            "event": "GROUP_MESSAGE_CREATE",
            "group_id": "g1",
            "group_name": "学院通知群",
            "content": "作业周五截止",
            "received_at": NOW,
        }
        self.svc.on_message(message)
        self.svc._apply_rules(message, "m-rule-5", "g1", "学院通知群")
        self.assertEqual(len(self.store.list_tasks(statuses=("candidate", "open"), limit=10)), 1)

    def test_task_url_uses_configured_public_base(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            web_base_url="https://qq-digest.tailnet.ts.net",
            web_token="secret",
        )
        store = Store(self.path / "public-url.sqlite3")
        svc = DigestService(settings, store=store, bot=self.bot, pushers=[self.pusher])
        self.assertEqual(
            svc._task_url(7), "https://qq-digest.tailnet.ts.net/?token=secret#task-7"
        )

    # ------------------------------------------------------------ 窗口与推送
    def test_window_flush_pushes_focus_items_once(self) -> None:
        self.assertTrue(
            self.svc.on_message(
                record("m1", "【学院通知】关于2026年国庆节放假安排的通知", minutes_ago=11)
            )
        )
        produced = self.svc.tick(now=NOW)
        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0].kind, "window")
        self.assertEqual(len(self.pusher.calls), 1)
        self.assertIn("国庆节", self.pusher.calls[0])

        # 第二轮不该再推一遍。
        self.assertEqual(self.svc.tick(now=NOW), [])
        self.assertEqual(len(self.pusher.calls), 1)
        self.assertEqual(self.store.counts()["unprocessed"], 0)

    def test_digest_writes_open_task(self) -> None:
        self.svc.on_message(record("m1", "请各班班长今天18:00前提交材料", minutes_ago=11))
        produced = self.svc.tick(now=NOW)
        self.assertEqual(len(produced), 1)

        tasks = self.store.list_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "open")
        self.assertIn("提交材料", tasks[0]["summary"])
        self.assertEqual(tasks[0]["groups"], ["学院通知群"])

    def test_task_sync_failure_does_not_block_push(self) -> None:
        # 消息在同步任务之前就已标记处理过：任务同步炸了也必须把摘要推出去。
        self.svc.on_message(record("m1", "请各班班长今天18:00前提交材料", minutes_ago=11))
        with mock.patch.object(DigestService, "_sync_tasks", side_effect=RuntimeError("建任务炸了")):
            produced = self.svc.tick(now=NOW)

        self.assertEqual(len(produced), 1)
        self.assertEqual(len(self.pusher.calls), 1)
        self.assertEqual(self.store.counts()["unprocessed"], 0)
        self.assertEqual(self.store.list_tasks(), [])

    def test_within_window_waits_then_flushes(self) -> None:
        self.svc.on_message(record("m1", "【学院通知】放假安排", minutes_ago=1))
        self.assertEqual(self.svc.tick(now=NOW), [])
        self.assertEqual(self.store.counts()["unprocessed"], 1)

        produced = self.svc.tick(now=NOW + dt.timedelta(minutes=10))
        self.assertEqual(len(produced), 1)
        self.assertEqual(len(self.pusher.calls), 1)

    def test_immediate_group_flushes_without_waiting_window(self) -> None:
        settings, store, pusher, svc = self.new_service(
            "immediate",
            group_aliases={"g1": "测试群"},
            immediate_groups=("g1",),
            window_minutes=30,
        )
        svc.on_message(record("m1", "【学院通知】关于2026年国庆节放假安排的通知", minutes_ago=1))
        produced = svc.tick(now=NOW)

        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0].kind, "window")
        self.assertEqual(len(pusher.calls), 1)
        self.assertEqual(store.counts()["unprocessed"], 0)

    def test_chatter_window_is_silent(self) -> None:
        self.svc.on_message(record("m1", "哈哈哈哈", minutes_ago=11, sender="同学A"))
        produced = self.svc.tick(now=NOW)

        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0].kind, "silent")
        self.assertEqual(self.pusher.calls, [])
        self.assertEqual(self.store.counts()["unprocessed"], 0)
        self.assertEqual(self.store.counts()["digests"], 1)

    def test_urgent_pushed_immediately(self) -> None:
        self.svc.on_message(record("m1", "请各班班长今天18:00前提交材料", minutes_ago=1))
        produced = self.svc.tick(now=NOW)

        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0].kind, "urgent")
        self.assertEqual(len(self.pusher.calls), 1)
        self.assertIn("截止", self.pusher.calls[0])
        self.assertEqual(produced[0].items[0]["category"], "urgent")

    def test_quiet_group_chatter_is_silent_and_never_becomes_a_task(self) -> None:
        settings, store, pusher, svc = self.new_service(
            "quiet-chatter", group_aliases={"g1": "电气工程学院2026级新生交流群"}, quiet_groups=("g1",)
        )
        svc.on_message(
            record(
                "m1",
                "不会的吧，我今天问4栋那个快递站明天可以寄快递吗他还说可以的",
                minutes_ago=1,
                sender="26测仪",
            )
        )
        self.assertEqual(svc.tick(now=NOW), [])
        produced = svc.tick(now=NOW + dt.timedelta(minutes=11))

        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0].kind, "silent")
        self.assertEqual(pusher.calls, [])
        self.assertEqual(store.list_tasks(), [])

    def test_quiet_group_real_notice_goes_to_window_and_task(self) -> None:
        settings, store, pusher, svc = self.new_service(
            "quiet-notice", group_aliases={"g1": "电气工程学院2026级新生交流群"}, quiet_groups=("g1",)
        )
        svc.on_message(record("m2", "请各班班长明天18:00前提交材料", minutes_ago=1))
        self.assertEqual(svc.tick(now=NOW), [])
        produced = svc.tick(now=NOW + dt.timedelta(minutes=11))

        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0].kind, "window")
        self.assertEqual(len(pusher.calls), 1)
        self.assertEqual(len(store.list_tasks()), 1)

    # ------------------------------------------------------------ 待办同步
    def test_event_time_fills_missing_llm_deadline(self) -> None:
        # 模型只说「要开会」没给时间；事件抽取从原文补出 15:30 与地点。
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
        with mock.patch("noticehub.event_extract.now_local", return_value=NOW):
            self.svc._sync_tasks(digest)

        tasks = self.store.list_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["summary"], "明天下午在A楼202开会")
        self.assertTrue(
            tasks[0]["deadline"].startswith(day(1, 15, 30).isoformat(timespec="minutes")),
            tasks[0]["deadline"],
        )
        self.assertTrue(any("A楼202" in detail for detail in tasks[0]["details"]), tasks[0]["details"])

    def test_llm_deadline_wins_and_event_only_fills_gaps(self) -> None:
        # 模型已经给了 action/deadline：时间与摘要以模型为准，事件只补 condition。
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
        with mock.patch("noticehub.event_extract.now_local", return_value=NOW):
            self.svc._sync_tasks(digest)

        task = self.store.list_tasks()[0]
        self.assertTrue(
            task["deadline"].startswith(day(2, 9, 0).isoformat(timespec="minutes")), task["deadline"]
        )
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
        with mock.patch("noticehub.event_extract.now_local", return_value=NOW):
            self.svc._sync_tasks(digest)

        self.assertNotIn("deadline_dt", item)
        self.assertEqual(self.store.list_tasks(), [])

    def test_location_detail_is_not_duplicated_when_merged_twice(self) -> None:
        # 回填合并一次、_sync_tasks 再合并一次：地点只能留一条。
        item = {
            "message": SimpleNamespace(text="明天下午3点半在A楼202开会", sender="李老师"),
            "category": "info",
            "action": "",
            "summary": "开会",
            "details": [],
            "group_name": "学院通知群",
            "msg_id": "m-event-4",
        }
        with mock.patch("noticehub.event_extract.now_local", return_value=NOW):
            self.svc._merge_extracted_event(item)
            self.svc._merge_extracted_event(item)
        self.assertEqual(item["details"], ["地点：A楼202"])

    def test_multi_event_keeps_earliest_deadline_and_every_place(self) -> None:
        # 一条消息两个事件：时间取最早，两个地点都要留下。
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
        with mock.patch("noticehub.event_extract.now_local", return_value=NOW):
            self.svc._sync_tasks(digest)

        task = self.store.list_tasks()[0]
        self.assertTrue(
            task["deadline"].startswith(day(1, 15, 30).isoformat(timespec="minutes")), task["deadline"]
        )
        self.assertEqual(task["details"], ["地点：A楼202", "地点：B楼301"])

    def test_medium_event_without_action_does_not_create_task(self) -> None:
        # 「今天下午3点放假，停电通知」只是信息：事件到 medium，却没有动词也没有地点。
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
        with mock.patch("noticehub.event_extract.now_local", return_value=NOW):
            self.svc._sync_tasks(digest)

        # 时间确实是事件补出来的，所以这条断言证明门槛收紧生效，而不是没抽到事件。
        self.assertIsInstance(item.get("deadline_dt"), dt.datetime)
        self.assertEqual(self.store.list_tasks(), [])

    def test_import_existing_tasks_fills_deadline_and_skips_expired(self) -> None:
        # 回填也要「先合并再判」：事件补出的时间能进待办，已过期的照旧被挡。
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
                {
                    **base,
                    "text": "去年的会议纪要",
                    "category": "action",
                    "summary": "旧事项",
                    "deadline": "2020-01-01T09:00:00",
                },
            ],
        )
        with mock.patch("noticehub.service.now_local", return_value=NOW), mock.patch(
            "noticehub.event_extract.now_local", return_value=NOW
        ):
            total = self.svc.import_existing_tasks(days=7)

        self.assertEqual(total, 1)
        tasks = self.store.list_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["summary"], "开会")
        self.assertTrue(
            tasks[0]["deadline"].startswith(day(1, 15, 30).isoformat(timespec="minutes")),
            tasks[0]["deadline"],
        )

    def test_import_existing_tasks_prefers_future_event_over_expired_one(self) -> None:
        # 一条消息两个事件：9 月 20 日那个已过期、明天那个还没到 → 用明天的时间建，别整条丢掉。
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
        with mock.patch("noticehub.service.now_local", return_value=NOW), mock.patch(
            "noticehub.event_extract.now_local", return_value=NOW
        ):
            total = self.svc.import_existing_tasks(days=7)

        self.assertEqual(total, 1)
        task = self.store.list_tasks()[0]
        self.assertTrue(
            task["deadline"].startswith(day(1, 15, 30).isoformat(timespec="minutes")), task["deadline"]
        )
        self.assertIn("地点：B楼301", task["details"])

    def test_ambiguous_action_becomes_candidate(self) -> None:
        self.svc.on_message(record("m1", "记得明天可能要交报名表", minutes_ago=11, sender="同学"))
        self.svc.tick(now=NOW)

        tasks = self.store.list_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "candidate")
        self.assertLess(tasks[0]["confidence"], 0.75)

    # ---------------------------------------------------------------- 推送闸门
    def test_duplicate_and_whitelist_filter(self) -> None:
        payload = record("m1", "【学院通知】放假安排", minutes_ago=11)
        self.assertTrue(self.svc.on_message(payload))
        self.assertFalse(self.svc.on_message(payload))
        self.assertFalse(self.svc.on_message(record("m2", "通知", minutes_ago=11, group="g2")))
        self.assertEqual(self.store.counts()["messages"], 1)

    def test_failed_push_is_retried_in_same_tick(self) -> None:
        self.pusher.fail_times = 1
        self.svc.on_message(record("m1", "【学院通知】放假安排", minutes_ago=11))
        produced = self.svc.tick(now=NOW)

        self.assertEqual(len(produced), 1)
        # delivery_retry_seconds=0：同一轮里失败一次就重试并成功。
        self.assertEqual(len(self.pusher.calls), 2)
        counts = self.store.counts()
        self.assertEqual(counts["deliveries_sent"], 1)
        self.assertEqual(counts["deliveries_failed"], 0)

    def test_failed_push_waits_for_cooldown(self) -> None:
        settings = Settings(window_minutes=10, group_whitelist=("g1",), delivery_retry_seconds=3600)
        store = Store(self.path / "cooldown.sqlite3")
        pusher = RecordingPusher(fail_times=1)
        svc = DigestService(settings, store=store, bot=FakeBot(), pushers=[pusher])

        svc.on_message(record("m1", "【学院通知】放假安排", minutes_ago=11))
        svc.tick(now=NOW)
        self.assertEqual(len(pusher.calls), 1)

        svc.tick(now=NOW + dt.timedelta(seconds=30))
        self.assertEqual(len(pusher.calls), 1)
        self.assertEqual(store.counts()["deliveries_failed"], 1)

        # 冷却结束后手动重试能成。
        self.assertEqual(len(store.pending_deliveries(max_attempts=4, retry_seconds=0)), 1)
        outcomes = svc.push_manager().retry_pending(retry_seconds=0)
        self.assertTrue(any(item.ok for item in outcomes))
        self.assertEqual(len(pusher.calls), 2)

    def test_status_reports_components(self) -> None:
        status = self.svc.status()
        self.assertIn("counts", status)
        self.assertEqual(status["channels"], ["webhook:test-target"])

    def test_start_and_stop_with_injected_bot(self) -> None:
        self.svc.start(start_bot=False)
        self.svc.stop()
        self.assertEqual(self.svc.status()["started_at"], "")

    def test_candidate_confirmation_retries_and_is_daily_limited(self) -> None:
        settings = Settings(
            group_whitelist=("g1",), delivery_retry_seconds=0, candidate_push_max_per_tick=1
        )
        store = Store(self.path / "candidate-push.sqlite3")
        pusher = RecordingPusher(fail_times=1)
        svc = DigestService(settings, store=store, bot=FakeBot(), pushers=[pusher])
        task_id = store.upsert_task(**dict(
            task_key="candidate-1",
            summary="可能要交报名表",
            category="action",
            evidence="记得看一下报名表",
            status="candidate",
            confidence=0.7,
        ))

        svc.tick(now=NOW)
        self.assertEqual(store.get_task(task_id)["status"], "candidate")
        self.assertEqual(store.get_task(task_id)["last_reminded_at"], "")

        svc.tick(now=NOW + dt.timedelta(minutes=1))
        self.assertEqual(len(pusher.calls), 2)
        self.assertTrue(store.get_task(task_id)["last_reminded_at"])
        self.assertEqual(store.get_task(task_id)["status"], "candidate")

        svc.tick(now=NOW + dt.timedelta(hours=2))
        self.assertEqual(len(pusher.calls), 2)

    def test_deadline_reminders_fire_once_and_cover_tomorrow(self) -> None:
        settings, store, pusher, svc = reminders_setup(self.path, name="reminders.sqlite3")

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

        svc.tick(now=day(1, 7, 31))
        self.assertEqual(len(pusher.calls), 1)
        self.assertIn("今日截止", pusher.calls[0])
        self.assertIn(day(1, 18, 0).strftime("%Y-%m-%d %H:%M"), pusher.calls[0])
        self.assertNotIn(day(2, 0, 0).strftime("%Y-%m-%d"), pusher.calls[0])

        svc.tick(now=day(1, 8, 0))
        self.assertEqual(len(pusher.calls), 1)

        svc.tick(now=day(1, 21, 0))
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
        store = Store(self.path / "reminders-no-channel.sqlite3")
        logger = mock.Mock()
        svc = DigestService(settings, store=store, bot=FakeBot(), pushers=[], logger=logger)
        store.upsert_task(
            task_key="m1",
            summary="今天提交材料",
            category="action",
            deadline=iso(day(1, 18, 0)),
            groups=["g1"],
        )

        start = day(1, 7, 31)
        for minutes in (0, 1, 14, 28, 59):
            svc._maybe_deadline_reminders(start + dt.timedelta(minutes=minutes))
        warnings = [call for call in logger.warning.call_args_list if "无可用推送通道" in str(call)]
        self.assertLessEqual(len(warnings), 2)

    def test_late_evening_start_skips_morning_reminder(self) -> None:
        settings, store, pusher, svc = reminders_setup(self.path, name="reminders-late.sqlite3")
        store.upsert_task(
            task_key="m1",
            summary="明天提交报名表",
            category="action",
            deadline=iso(day(2, 12, 0)),
            groups=["学院通知群"],
        )

        svc.tick(now=day(1, 22, 0))
        self.assertEqual(len(pusher.calls), 1)
        self.assertIn("截止提醒", pusher.calls[0])
        self.assertNotIn("今日截止", pusher.calls[0])
        self.assertIn(day(2, 12, 0).strftime("%Y-%m-%d %H:%M"), pusher.calls[0])

    def test_weekly_review_only_fires_when_enabled(self) -> None:
        """周日晚 20:30 之后只按开关决定推不推复盘——两个方向都锁住。

        这样以后谁把默认值改成开着，21:00/23:30 的截止提醒用例会立刻炸。
        """
        base = NOW.replace(hour=21, minute=0, second=0, microsecond=0)
        sunday = base + dt.timedelta(days=(6 - base.weekday()) % 7)
        self.assertEqual(sunday.weekday(), 6)
        self.assertGreaterEqual(sunday.time(), dt.time(20, 30))

        for enabled, expected in ((False, 0), (True, 1)):
            with self.subTest(weekly_review_enabled=enabled):
                settings = Settings(
                    group_whitelist=("g1",), window_minutes=10, weekly_review_enabled=enabled
                )
                store = Store(self.path / f"weekly-{enabled}.sqlite3")
                pusher = RecordingPusher()
                svc = DigestService(settings, store=store, bot=FakeBot(), pushers=[pusher])

                svc.tick(now=sunday)

                self.assertEqual(
                    len(pusher.calls),
                    expected,
                    "每周复盘开=%s 时推送条数不对：%r" % (enabled, pusher.calls),
                )
                if enabled:
                    self.assertIn("新增候选", pusher.calls[0])

    def test_overdue_open_task_is_reminded_but_dismissed_is_not(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            catchup_enabled=False,
            deadline_morning="07:30",
            deadline_evening="21:00",
        )
        store = Store(self.path / "overdue.sqlite3")
        pusher = RecordingPusher()
        svc = DigestService(settings, store=store, bot=FakeBot(), pushers=[pusher])
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

        svc.tick(now=NOW.replace(hour=8, minute=0))
        self.assertEqual(len(pusher.calls), 1)
        self.assertIn("昨天应交的材料", pusher.calls[0])
        self.assertNotIn("已忽略事项", pusher.calls[0])
        self.assertTrue(store.get_task(open_id)["last_reminded_at"])
        self.assertEqual(store.get_task(dismissed_id)["remind_count"], 0)

        # 同一天再调度一次不重复提醒。
        svc.tick(now=NOW.replace(hour=9, minute=0))
        self.assertEqual(len(pusher.calls), 1)

    def test_deadline_reminder_without_channel_keeps_retry_key(self) -> None:
        settings = Settings(group_whitelist=("g1",), catchup_enabled=False, deadline_morning="07:30", deadline_evening="21:00")
        store = Store(self.path / "no-channel.sqlite3")
        svc = DigestService(settings, store=store, bot=FakeBot(), pushers=[])
        store.upsert_task(
            task_key="m1",
            summary="今天提交材料",
            category="action",
            deadline=iso(day(0, 18, 0)),
            groups=["学院通知群"],
        )

        svc.tick(now=NOW.replace(hour=7, minute=31))
        self.assertEqual(
            store.meta_get("deadline_reminder:morning:%s" % NOW.strftime("%Y-%m-%d")), ""
        )

        # 通道补上以后，同一时段的提醒还能补发（说明上一轮没把 key 占掉）。
        pusher = RecordingPusher()
        svc._pushers = [pusher]
        svc._push_manager = None
        svc.tick(now=NOW.replace(hour=7, minute=32))
        self.assertEqual(len(pusher.calls), 1)
        self.assertTrue(store.meta_get("deadline_reminder:morning:%s" % NOW.strftime("%Y-%m-%d")))

    def test_retryable_llm_failure_defers_batch_then_recovers(self) -> None:
        settings, store, pusher, svc = self.new_service(
            "llm-defer",
            window_minutes=1,
            dashscope_api_key="test-key",
            llm_max_retries=0,
            llm_retry_backoff=0.0,
            llm_defer_max_attempts=3,
        )
        svc.on_message(record("m1", "【学院通知】关于2026年国庆节放假安排的通知", minutes_ago=2))

        with mock.patch(
            "qq_digest.refine_with_dashscope",
            side_effect=LLMRequestError("LLM 请求失败: connection refused"),
        ):
            svc.tick(now=NOW)
        self.assertEqual(len(pusher.calls), 0)
        self.assertEqual(store.counts()["unprocessed"], 1)
        self.assertEqual(store.meta_get("llm_defer:count"), "1")

        with mock.patch(
            "qq_digest.refine_with_dashscope", side_effect=lambda items, *args, **kwargs: items
        ):
            svc.tick(now=NOW + dt.timedelta(minutes=1))
        self.assertEqual(len(pusher.calls), 1)
        self.assertEqual(store.counts()["unprocessed"], 0)
        self.assertEqual(store.meta_get("llm_defer:count"), "0")

    def test_non_retryable_llm_failure_falls_back_without_blocking(self) -> None:
        settings, store, pusher, svc = self.new_service(
            "llm-auth",
            window_minutes=1,
            dashscope_api_key="test-key",
            llm_max_retries=0,
            llm_retry_backoff=0.0,
        )
        svc.on_message(record("m1", "【学院通知】关于2026年国庆节放假安排的通知", minutes_ago=2))

        with mock.patch(
            "qq_digest.refine_with_dashscope",
            side_effect=LLMRequestError("LLM HTTP 401: bad key", status=401),
        ):
            svc.tick(now=NOW)
        self.assertEqual(len(pusher.calls), 1)
        self.assertEqual(store.counts()["unprocessed"], 0)
        self.assertEqual(store.meta_get("llm_fallbacks_total"), "1")

    def test_quiet_hours_hold_batch_until_morning(self) -> None:
        settings, store, pusher, svc = self.new_service(
            "quiet",
            window_minutes=1,
            quiet_hours="23:00-07:00",
            # 23:30 在周日晚 20:30 之后，不关复盘会多一条推送把 len==0 打红。
            weekly_review_enabled=False,
        )
        svc.on_message(record("m1", "【学院通知】关于2026年国庆节放假安排的通知", minutes_ago=2))

        svc.tick(now=NOW.replace(hour=23, minute=30))
        self.assertEqual(len(pusher.calls), 0)
        self.assertEqual(store.counts()["unprocessed"], 1)
        self.assertEqual(svc.last_defer_reason, "quiet_hours")

        svc.tick(now=(NOW + dt.timedelta(days=1)).replace(hour=7, minute=1))
        self.assertEqual(len(pusher.calls), 1)
        self.assertEqual(store.counts()["unprocessed"], 0)

    def test_daily_push_budget_holds_extra_batches(self) -> None:
        settings, store, pusher, svc = self.new_service(
            "budget", window_minutes=1, push_daily_budget=1
        )
        svc.on_message(record("m1", "【学院通知】关于2026年国庆节放假安排的通知", minutes_ago=11))
        svc.tick(now=NOW)
        self.assertEqual(len(pusher.calls), 1)
        self.assertEqual(store.meta_get("push_budget:%s" % NOW.strftime("%Y-%m-%d")), "1")

        svc.on_message(record("m2", "【学院通知】关于2026年国庆节后课程调整的通知", minutes_ago=11))
        svc.tick(now=NOW + dt.timedelta(minutes=1))
        self.assertEqual(len(pusher.calls), 1)
        self.assertEqual(store.counts()["unprocessed"], 1)
        self.assertEqual(svc.last_defer_reason, "daily_budget")

    def test_urgent_bypasses_quiet_hours(self) -> None:
        settings, store, pusher, svc = self.new_service(
            "urgent-night",
            window_minutes=30,
            quiet_hours="23:00-07:00",
            weekly_review_enabled=False,
        )
        svc.on_message(record("m1", "请各班班长今天18:00前提交材料", minutes_ago=1))

        svc.tick(now=NOW.replace(hour=23, minute=30))
        self.assertEqual(len(pusher.calls), 1)

    # ---------------------------------------------------------------- 补采
    def _catchup_ready(self, stats: dict[str, int] | None = None):
        """引擎已登录 + backfill 假实现（不碰网络）。"""
        payload = {"groups": 1, "ok_groups": 1, "failed_groups": 0, "inserted": 0}
        payload.update(stats or {})

        def fake_backfill(
            settings, store, logger=None, *, client=None, now=None, force=False, stats=None,
            attachment_sink=None,
        ):
            if stats is not None:
                stats.update(payload)
            return int(payload.get("inserted", 0))

        return (
            mock.patch.object(self.svc, "_engine_ready", return_value=(True, "")),
            mock.patch("noticehub.service.backfill", side_effect=fake_backfill),
        )

    def test_catchup_total_failure_retries_after_one_minute(self) -> None:
        """首次失败 60 秒后重试：开机时引擎没登录不该被拖成半小时。"""
        self.svc.settings.catchup_enabled = True
        ready, patched = self._catchup_ready({"ok_groups": 0, "failed_groups": 1})
        with ready, patched:
            self.assertEqual(self.svc.run_catchup(now=NOW), 0)
            self.assertIsNone(self.svc.last_catchup_at)
            self.assertEqual(self.svc.last_catchup_attempt_at, NOW)
            self.assertEqual(self.svc.catchup_state, "failed")
            self.assertEqual(self.svc._catchup_failures, 1)

            self.svc._maybe_catchup(NOW + dt.timedelta(seconds=30))
            self.assertIsNone(self.svc._catchup_thread)

            self.svc._maybe_catchup(NOW + dt.timedelta(seconds=61))
            self.assertIsNotNone(self.svc._catchup_thread)
            self.svc._catchup_thread.join(timeout=5)

    def test_catchup_backoff_steps_and_cap(self) -> None:
        """第 1/2/3/4 次失败分别退避 60/120/300/600 秒，600 秒封顶。"""
        self.svc.settings.catchup_enabled = True
        scheduled: list[int] = []
        self.svc._schedule_catchup = lambda: scheduled.append(1)
        self.svc.catchup_state = "failed"
        self.svc.last_catchup_attempt_at = NOW

        for failures, expected in ((1, 60), (2, 120), (3, 300), (4, 600), (9, 600)):
            self.svc._catchup_failures = failures
            self.assertEqual(self.svc._catchup_retry_seconds(), expected)
            scheduled.clear()
            self.svc._maybe_catchup(NOW + dt.timedelta(seconds=expected - 1))
            self.assertEqual(scheduled, [], f"第 {failures} 次失败后不足 {expected} 秒不该重试")
            self.svc._maybe_catchup(NOW + dt.timedelta(seconds=expected + 1))
            self.assertEqual(scheduled, [1], f"第 {failures} 次失败后满 {expected} 秒该重试")

    def test_catchup_success_resets_failures_and_retry_window(self) -> None:
        """成功一次：失败计数归零、状态 ok、退回 60 秒口径。"""
        self.svc.settings.catchup_enabled = True
        self.svc.catchup_state = "failed"
        self.svc._catchup_failures = 5
        ready, patched = self._catchup_ready()
        with ready, patched:
            self.svc.run_catchup(now=NOW)

        self.assertEqual(self.svc._catchup_failures, 0)
        self.assertEqual(self.svc.catchup_state, "ok")
        self.assertEqual(self.svc.last_catchup_ok_at, NOW)
        self.assertEqual(self.svc.last_catchup_error, "")
        self.assertEqual(self.svc._catchup_retry_seconds(), 60)

    def test_catchup_waits_for_engine_without_counting_failures(self) -> None:
        """引擎没上线：不抓、不计失败、不动退避。"""
        self.svc.settings.catchup_enabled = True
        calls: list[dict] = []

        def fake_backfill(*args, **kwargs):
            calls.append(kwargs)
            return 0

        with mock.patch.object(
            self.svc, "_engine_ready", return_value=(False, "引擎还没上线")
        ), mock.patch("noticehub.service.backfill", side_effect=fake_backfill):
            for _ in range(3):
                self.assertEqual(self.svc.run_catchup(now=NOW), 0)
                self.assertEqual(self.svc.catchup_state, "waiting_engine")
                self.assertEqual(self.svc._catchup_failures, 0)
                self.assertIsNone(self.svc.last_catchup_at)

        self.assertEqual(calls, [], "引擎没上线就该一个群都不抓")
        self.assertIn("引擎还没上线", self.svc.last_catchup_error)

    def test_waiting_engine_polls_short_and_ignores_backoff(self) -> None:
        """等引擎按 20 秒短轮询，不受失败退避影响。"""
        self.svc.settings.catchup_enabled = True
        self.svc.catchup_state = "waiting_engine"
        self.svc.last_catchup_attempt_at = NOW
        scheduled: list[int] = []
        self.svc._schedule_catchup = lambda: scheduled.append(1)

        self.svc._maybe_catchup(NOW + dt.timedelta(seconds=19))
        self.assertEqual(scheduled, [])
        self.svc._maybe_catchup(NOW + dt.timedelta(seconds=21))
        self.assertEqual(scheduled, [1])

    def test_engine_arrival_triggers_catchup_and_resets_failures(self) -> None:
        """从「等引擎」变成可用：立刻补一次，失败计数清零。"""
        self.svc.settings.catchup_enabled = True
        self.svc.catchup_state = "waiting_engine"
        self.svc._catchup_failures = 4
        scheduled: list[int] = []
        self.svc._schedule_catchup = lambda: scheduled.append(1)

        self.svc._catchup_engine_arrived()
        self.assertEqual(self.svc._catchup_failures, 0)
        self.assertEqual(scheduled, [1])

    def test_catchup_state_is_exposed_in_status_payloads(self) -> None:
        self.svc.catchup_state = "waiting_engine"
        self.svc.last_catchup_attempt_at = NOW
        self.svc.last_catchup_error = "引擎还没上线"

        for payload in (self.svc.health_payload(), self.svc.status()):
            self.assertEqual(payload["catchup_state"], "waiting_engine")
            self.assertTrue(payload["catchup_waiting_engine"])
            self.assertEqual(payload["last_catchup_error"], "引擎还没上线")
            self.assertTrue(payload["catchup_next_retry_at"])
            self.assertIn("引擎", payload["catchup_hint"])


class DingtalkReloadTest(unittest.TestCase):
    def test_start_requires_nonempty_whitelist(self) -> None:
        """启用钉钉但白名单为空 → reader 不启动（与 reload 的条件保持一致）。"""
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
                svc = DigestService(settings, store=Store(Path(tmp) / "dingtalk.sqlite3"))
                with mock.patch("noticehub.service.DingTalkLocalReader") as reader_cls:
                    svc.start(start_bot=False)
                    self.assertEqual(reader_cls.called, should_start)
                    if should_start:
                        reader_cls.return_value.start.assert_called_once()
                        self.assertIsNotNone(svc._dingtalk_reader)
                    else:
                        self.assertIsNone(svc._dingtalk_reader)
                svc.stop()


if __name__ == "__main__":
    unittest.main()
