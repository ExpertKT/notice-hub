"""待回清单（replies）规则测试：四类命中、排除、幂等、ack、空表。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from noticehub.config import Settings
from noticehub.replies import (
    RULE_AT,
    RULE_CONFIRM,
    RULE_DEADLINE,
    RULE_QUESTION,
    ack,
    detect_reasons,
    list_pending,
    scan,
)
from noticehub.store import Store


class RepliesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "test.sqlite3")

    def _add(self, msg_id: str, content: str, *, group_id: str = "g1", sender_id: str = "u1",
             sender_name: str = "小明", source_text: str = "") -> None:
        self.store.insert_message(
            msg_id=msg_id,
            group_id=group_id,
            content=content,
            source_text=source_text,
            sender_id=sender_id,
            sender_name=sender_name,
        )

    def _scan(self, **kwargs):
        return scan(self.store, me_qq="10001", me_nickname="我", **kwargs)

    def test_four_rule_kinds_are_detected(self) -> None:
        self._add("m-at", "[CQ:at,qq=10001] 这个你看下")
        self._add("m-question", "会议室在哪？")
        self._add("m-deadline", "材料尽快发我")
        self._add("m-confirm", "收到请回复")

        outcome = self._scan()
        self.assertEqual(outcome["inserted"], 4)
        reasons = {row["msg_id"]: row["reason"] for row in list_pending(self.store)}
        self.assertIn(RULE_AT, reasons["m-at"])
        self.assertIn(RULE_QUESTION, reasons["m-question"])
        self.assertIn(RULE_DEADLINE, reasons["m-deadline"])
        self.assertIn(RULE_CONFIRM, reasons["m-confirm"])

    def test_question_words_without_mark(self) -> None:
        self.assertIn(RULE_QUESTION, detect_reasons("请问会议室在哪"))
        self.assertIn(RULE_QUESTION, detect_reasons("在吗"))
        self.assertIn(RULE_QUESTION, detect_reasons("这个怎么弄"))

    def test_nickname_mention_is_detected(self) -> None:
        self.assertIn(RULE_AT, detect_reasons("@我 帮我看下", me_nickname="我"))

    def test_self_sent_messages_are_excluded(self) -> None:
        self._add("m-self-id", "收到请回复", sender_id="10001")
        self._add("m-self-name", "这个问题怎么处理", sender_name="我")
        outcome = self._scan()
        self.assertEqual(outcome["matched"], 0)
        self.assertEqual(list_pending(self.store), [])

    def test_pure_image_and_emoji_are_excluded(self) -> None:
        self._add("m-img", "[CQ:image,file=abc.jpg]")
        self._add("m-emoji", "😂😂😂")
        outcome = self._scan()
        self.assertEqual(outcome["matched"], 0)

    def test_scan_is_idempotent(self) -> None:
        self._add("m-1", "明天见吗")
        first = self._scan()
        second = self._scan()
        self.assertEqual(first["inserted"], 1)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(len(list_pending(self.store)), 1)

    def test_ack_hides_entry_and_stays_hidden_after_rescan(self) -> None:
        self._add("m-1", "今晚能交吗")
        self._scan()
        self.assertTrue(ack(self.store, "m-1"))
        self.assertFalse(ack(self.store, "m-1"))
        self.assertEqual(list_pending(self.store), [])

        self._scan()
        self.assertEqual(list_pending(self.store), [])

    def test_empty_message_table(self) -> None:
        self.assertEqual(self._scan(), {"scanned": 0, "matched": 0, "inserted": 0})
        self.assertEqual(list_pending(self.store), [])

    def test_group_filter(self) -> None:
        self._add("m-1", "在吗", group_id="g1")
        self._add("m-2", "在吗", group_id="g2")
        self._scan()
        rows = list_pending(self.store, group_id="g2")
        self.assertEqual([row["msg_id"] for row in rows], ["m-2"])

    def test_no_identity_means_no_at_rule(self) -> None:
        self.assertEqual(detect_reasons("[CQ:at,qq=10001] 看下"), [])
        self.assertIn(RULE_CONFIRM, detect_reasons("[CQ:at,qq=10001] 确认一下"))

    def test_settings_reads_self_identity(self) -> None:
        settings = Settings.from_env(
            env={"QQ_DIGEST_SELF_QQ": "10001", "QQ_DIGEST_SELF_NICKNAME": "小王"},
            env_file=Path(self.tmp.name) / "missing.env",
        )
        self.assertEqual(settings.self_qq, "10001")
        self.assertEqual(settings.self_nickname, "小王")
        self.assertEqual(Settings().self_qq, "")


if __name__ == "__main__":
    unittest.main()