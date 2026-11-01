from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from noticehub.catchup import available_floor, backfill, backfill_range, history_to_record  # noqa: E402
from noticehub.config import Settings  # noqa: E402
from noticehub.store import Store  # noqa: E402

# 固定「现在」，所有消息时间都用相对分钟数算，避免用例受真实时钟影响。
NOW = dt.datetime(2026, 9, 29, 1, 0, 0)


def history_message(msg_id: str, **overrides) -> dict:
    """造一条 NapCat 历史消息；只有需要区分的字段由调用方覆盖。"""
    base = {
        "post_type": "message",
        "message_type": "group",
        "group_id": "g1",
        "user_id": "u1",
        "self_id": "999",
        "message_id": msg_id,
        "time": int((NOW - dt.timedelta(minutes=30)).timestamp()),
        "message": [{"type": "text", "data": {"text": "请提交材料"}}],
        "sender": {"card": "辅导员", "nickname": "老师"},
    }
    if "minutes_ago" in overrides:
        minutes = overrides.pop("minutes_ago")
        base["time"] = int((NOW - dt.timedelta(minutes=minutes)).timestamp())
    if "group" in overrides:
        base["group_id"] = overrides.pop("group")
    if "user" in overrides:
        base["user_id"] = overrides.pop("user")
    if "text" in overrides:
        text = overrides.pop("text")
        base["message"] = [{"type": "text", "data": {"text": text}}]
    base.update(overrides)
    return base


class FakeNapCat:
    """一次性返回全部历史，记录每个群被要过几次。"""

    LOGIN = {"user_id": 999, "nickname": "bot"}

    def __init__(self, messages: list[dict]) -> None:
        self._records = list(messages)
        self.calls: list[tuple[str, int]] = []

    def login_info(self) -> dict:
        return dict(self.LOGIN)

    def group_history(self, group_id: str, count: int) -> list[dict]:
        self.calls.append((group_id, count))
        wanted = str(group_id)
        return [record for record in self._records if str(record.get("group_id")) == wanted]


class FailingNapCat:
    """拉历史必失败，用来验整组失败的统计口径。"""

    def login_info(self) -> dict:
        return {"user_id": 999, "nickname": "bot"}

    def group_history(self, group_id: str, count: int) -> list[dict]:
        raise RuntimeError("napcat unavailable")


class PagedNapCat:
    """按页返回，页尾返回空列表模拟「没有更早的消息了」。"""

    def __init__(self, pages: list[list[dict]]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, int, int | None]] = []

    def group_history(self, group_id: str, count: int, message_seq: int | None = None) -> list[dict]:
        self.calls.append((group_id, count, message_seq))
        return self.pages[min(len(self.calls) - 1, len(self.pages) - 1)]


def make_settings(**overrides) -> Settings:
    """补采用例共用的配置；只有要改的字段由用例覆盖。"""
    base = {
        "group_whitelist": ("g1",),
        "group_aliases": {"g1": "学院通知群"},
        "catchup_enabled": True,
        "catchup_hours": 24,
        "catchup_count": 50,
        "napcat_api_token": "token",
    }
    base.update(overrides)
    return Settings(**base)


class CatchupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = make_settings()
        self.store = Store(Path(self.tmp.name) / "catchup.sqlite3")

    def backfill_with(self, client, **overrides) -> int:
        """默认按「强制全量采一次」调用；用例只给 client，个别参数按需覆盖。"""
        return backfill(self.settings, self.store, client=client, now=NOW, force=True, **overrides)

    def test_backfill_inserts_recent_messages_and_dedupes(self) -> None:
        # 一批里混四种该跳过的情况：自己发的、超窗口、白名单外，只留两条真该入库的。
        client = FakeNapCat([
            history_message("m1"),
            history_message("m2", user="999"),
            history_message("m3", minutes_ago=60 * 48),
            history_message("m4", group="g2"),
            history_message("m5", minutes_ago=90),
        ])
        inserted = self.backfill_with(client)
        self.assertEqual(inserted, 2)
        counts = self.store.counts()
        self.assertEqual(counts["messages"], 2)
        self.assertEqual(counts["unprocessed"], 2)

        # 紧接着再采一次，同一条消息不该进第二行。
        self.assertEqual(self.backfill_with(client), 0)
        self.assertEqual(self.store.counts()["messages"], 2)

    def test_backfill_reports_total_group_failure(self) -> None:
        stats: dict[str, int] = {}
        inserted = self.backfill_with(FailingNapCat(), stats=stats)
        self.assertEqual(inserted, 0)
        self.assertEqual(stats["groups"], 1)
        self.assertEqual(stats["ok_groups"], 0)
        self.assertEqual(stats["failed_groups"], 1)

    def test_backfill_range_pages_and_filters_explicit_group(self) -> None:
        page1 = [
            history_message("new", group="other", minutes_ago=30, message_seq=300),
            history_message("mid", group="other", minutes_ago=90, message_seq=200),
        ]
        page2 = [history_message("old", group="other", minutes_ago=180, message_seq=100)]
        client = PagedNapCat([page1, page2, []])
        result = backfill_range(
            self.settings, self.store, groups=["other"],
            since=NOW - dt.timedelta(minutes=120), until=NOW, client=client,
        )
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(result["groups"][0]["scanned"], 3)
        # 第二页必须带上第一页最小的 seq 才能继续往前翻。
        self.assertEqual(client.calls[1][2], 200)

    def test_available_floor_stops_at_max_pages_and_reports_oldest(self) -> None:
        pages = [
            [history_message("a", minutes_ago=30, message_seq=20)],
            [history_message("b", minutes_ago=60, message_seq=10)],
        ]
        result = available_floor(self.settings, "g1", client=PagedNapCat(pages), page=1, max_pages=2)
        self.assertEqual(result["pages"], 2)
        self.assertEqual(result["floor_message_seq"], 10)

    def test_backfill_range_cancel_preserves_inserted(self) -> None:
        message = history_message("cancel-me", message_seq=1)
        checks = iter((False, True))
        result = backfill_range(
            self.settings, self.store, client=PagedNapCat([[message]]),
            cancel=lambda: next(checks, True),
        )
        # 取消只是不再继续翻页，已经采进来的那条必须留着。
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(self.store.counts()["messages"], 1)

    def test_history_record_keeps_segment_labels_and_origin_time(self) -> None:
        message = history_message("m6", message=[{"type": "image", "data": {"file": "a.png"}}])
        record = history_to_record(message, self.settings)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["content"], "[图片]")
        self.assertEqual(record["group_name"], "学院通知群")
        self.assertEqual(record["sender_name"], "辅导员")
