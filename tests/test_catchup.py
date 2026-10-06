from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest.catchup import backfill, history_to_record  # noqa: E402
from qq_live_digest.config import Settings  # noqa: E402
from qq_live_digest.store import Store  # noqa: E402

NOW = dt.datetime(2026, 9, 29, 1, 0, 0)


def history_message(
    msg_id: str,
    *,
    group: str = "g1",
    user: str = "u1",
    minutes_ago: int = 30,
    text: str = "请提交材料",
) -> dict:
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": group,
        "user_id": user,
        "self_id": "999",
        "message_id": msg_id,
        "time": int((NOW - dt.timedelta(minutes=minutes_ago)).timestamp()),
        "message": [{"type": "text", "data": {"text": text}}],
        "sender": {"card": "辅导员", "nickname": "老师"},
    }


class FakeNapCat:
    def __init__(self, messages: list[dict]) -> None:
        self.messages = messages
        self.calls: list[tuple[str, int]] = []

    def login_info(self) -> dict:
        return {"user_id": 999, "nickname": "bot"}

    def group_history(self, group_id: str, count: int) -> list[dict]:
        self.calls.append((group_id, count))
        return [item for item in self.messages if str(item.get("group_id")) == group_id]


class FailingNapCat:
    def login_info(self) -> dict:
        return {"user_id": 999, "nickname": "bot"}

    def group_history(self, group_id: str, count: int) -> list[dict]:
        raise RuntimeError("napcat unavailable")


class CatchupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "学院通知群"},
            catchup_enabled=True,
            catchup_hours=24,
            catchup_count=50,
            napcat_api_token="token",
        )
        self.store = Store(Path(self.tmp.name) / "catchup.sqlite3")

    def test_backfill_inserts_recent_messages_and_dedupes(self) -> None:
        messages = [
            history_message("m1"),
            history_message("m2", user="999"),          # 自己发的，跳过
            history_message("m3", minutes_ago=60 * 48),  # 超过回溯窗口，跳过
            history_message("m4", group="g2"),           # 白名单外，跳过
            history_message("m5", minutes_ago=90),
        ]
        client = FakeNapCat(messages)
        inserted = backfill(self.settings, self.store, client=client, now=NOW, force=True)
        self.assertEqual(inserted, 2)
        counts = self.store.counts()
        self.assertEqual(counts["messages"], 2)
        self.assertEqual(counts["unprocessed"], 2)

        # 同一批消息再次补采不会重复入库。
        self.assertEqual(backfill(self.settings, self.store, client=client, now=NOW, force=True), 0)
        self.assertEqual(self.store.counts()["messages"], 2)

    def test_backfill_reports_total_group_failure(self) -> None:
        stats: dict[str, int] = {}
        inserted = backfill(
            self.settings,
            self.store,
            client=FailingNapCat(),
            now=NOW,
            force=True,
            stats=stats,
        )
        self.assertEqual(inserted, 0)
        self.assertEqual(stats["groups"], 1)
        self.assertEqual(stats["ok_groups"], 0)
        self.assertEqual(stats["failed_groups"], 1)

    def test_history_record_keeps_segment_labels_and_origin_time(self) -> None:
        message = history_message("m6")
        message["message"] = [{"type": "image", "data": {"file": "a.png"}}]
        record = history_to_record(message, self.settings)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["content"], "[图片]")
        self.assertEqual(record["group_name"], "学院通知群")
        self.assertEqual(record["sender_name"], "辅导员")


if __name__ == "__main__":
    unittest.main()
