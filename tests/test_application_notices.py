"""「最近推送」列表的数据层：正文上限、空条目、window_end、群名替换。

这里是接口契约，页面（前端2）按同一形状渲染；改形状前先改契约。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve()
for _root in (_HERE.parent.parent,):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

from noticehub.application.notices import BODY_LIMIT, list_notices  # noqa: E402


class FakeStore:
    """只实现 list_notices 用到的两个方法：最近摘要与群名。"""

    def __init__(self, digests: list[dict], names: dict | None = None) -> None:
        self._digests = digests
        self._names = dict(names or {})

    def recent_digests(self, *, limit: int = 5) -> list[dict]:
        return list(self._digests)[: max(1, limit)]

    def group_names(self) -> dict:
        return dict(self._names)


def row(digest_id: int, body: str, **overrides) -> dict:
    base = {
        "id": digest_id,
        "kind": "window",
        "created_at": "2026-11-05 07:41:03",
        "window_end": "2026-11-05 07:41",
        "item_count": 3,
        "body": body,
    }
    base.update(overrides)
    return base


class ListNoticesTest(unittest.TestCase):
    def test_body_is_not_hard_cut_at_600(self) -> None:
        """旧实现硬切 600 字：第 5 条断在半句、后面几条直接看不到。"""
        body = "\n".join("第 %02d 行 请各班今天18:00前提交材料" % index for index in range(80))
        self.assertGreater(len(body), 600)
        self.assertLess(len(body), BODY_LIMIT)

        items = list_notices(FakeStore([row(1, body)]))["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["body"], body)
        self.assertNotIn("以下省略", items[0]["body"])

    def test_body_over_limit_ends_with_an_omission_note_on_a_whole_line(self) -> None:
        lines = ["第 %03d 行 请各班今天18:00前提交材料" % index for index in range(400)]
        items = list_notices(FakeStore([row(1, "\n".join(lines))]))["items"]
        body = items[0]["body"]

        self.assertTrue(body.endswith("\n…（以下省略）"))
        self.assertLessEqual(len(body), BODY_LIMIT)
        kept = body[: -len("\n…（以下省略）")].splitlines()
        self.assertEqual(kept[-1], lines[len(kept) - 1])
        self.assertNotIn(lines[len(kept)], body)

    def test_empty_body_is_not_returned(self) -> None:
        store = FakeStore([row(1, ""), row(2, "   \n  "), row(3, "【QQ群通知汇总】2026-11-05 07:41")])
        items = list_notices(store)["items"]
        self.assertEqual([item["id"] for item in items], [3])

    def test_window_end_and_shape_are_exposed(self) -> None:
        items = list_notices(FakeStore([row(7, "正文", item_count=4)]))["items"]
        self.assertEqual(
            set(items[0]),
            {"id", "kind", "created_at", "window_end", "summary", "body"},
        )
        self.assertEqual(items[0]["window_end"], "2026-11-05 07:41")
        self.assertEqual(items[0]["summary"], "4 条重点")

    def test_group_ids_in_the_body_are_replaced_with_names(self) -> None:
        body = (
            "【QQ群通知汇总】2026-11-05 07:41\n"
            "791855425、1051550372 · 12 条消息 · 2 条重点\n\n"
            "1. [通知] 请各班今天18:00前提交材料\n"
            "   计算机261通知群 · 辅导员\n"
            "   7918554253 不是群号，别动它"
        )
        names = {"791855425": "计算机261通知群", "1051550372": "计算机26级专业群"}
        body_text = list_notices(FakeStore([row(1, body)], names))["items"][0]["body"]

        self.assertIn("计算机261通知群、计算机26级专业群 · 12 条消息", body_text)
        self.assertIn("7918554253", body_text)
        self.assertNotIn("791855425、", body_text)


class OldBodyCleanupTest(unittest.TestCase):
    """旧摘要（群里已经发出去的那些）留在库里的残号与重复「需做」，读的时候补干净。"""

    NAMES = {"791855425": "计算机261通知群", "1051550372": "计算机26级专业群"}

    def _body(self, header: str, rest: str = "") -> str:
        text = "【QQ群通知汇总】2026-11-05 07:41\n%s" % header
        return text + ("\n\n" + rest if rest else "")

    def test_cut_id_is_restored_from_a_unique_prefix(self) -> None:
        body = self._body("计算机26级专业群、79185542 · 63 条消息 · 7 条重点")
        text = list_notices(FakeStore([row(1, body)], self.NAMES))["items"][0]["body"]
        self.assertIn("计算机26级专业群、计算机261通知群 · 63 条消息", text)
        self.assertNotIn("79185542", text)

    def test_cut_id_with_ellipsis_restores_the_name_without_the_ellipsis(self) -> None:
        body = self._body("计算机26级专业群、7918554… · 17 条消息 · 7 条重点")
        text = list_notices(FakeStore([row(1, body)], self.NAMES))["items"][0]["body"]
        self.assertIn("计算机261通知群 · 17 条消息", text)
        self.assertNotIn("计算机261通知群…", text)

    def test_ambiguous_prefix_is_left_alone(self) -> None:
        names = {"791855425": "甲群", "791855426": "乙群"}
        body = self._body("79185542 · 63 条消息 · 7 条重点")
        text = list_notices(FakeStore([row(1, body)], names))["items"][0]["body"]
        self.assertIn("79185542 · 63 条消息", text)

    def test_only_the_first_two_lines_are_restored(self) -> None:
        body = self._body(
            "计算机26级专业群 · 63 条消息 · 7 条重点",
            "1. [通知] 请各班今天18:00前提交材料\n   79185542 是旧正文里的样张，不该动",
        )
        text = list_notices(FakeStore([row(1, body)], self.NAMES))["items"][0]["body"]
        self.assertIn("79185542 是旧正文里的样张", text)

    def test_repeated_needs_action_line_is_dropped(self) -> None:
        body = self._body(
            "计算机26级专业群 · 63 条消息 · 7 条重点",
            "1. [待办] @all 由于期中考试全统考，网上平台需要…\n"
            "   截止：11-06 18:00\n"
            "   需做：@all 由于期中考试全统考，网上平台需要…",
        )
        text = list_notices(FakeStore([row(1, body)], self.NAMES))["items"][0]["body"]
        self.assertNotIn("需做：", text)
        self.assertIn("1. [待办] @all 由于期中考试全统考", text)

    def test_repeated_action_with_the_source_suffix_is_dropped(self) -> None:
        """真实旧格式：标题截断更长，需做 = 截断标题 + 「· 群号 · 发送人」。"""
        body = self._body(
            "计算机26级专业群 · 63 条消息 · 7 条重点",
            "2. [待办] @all 由于期中考试全院通考，网上平台需要大家进入另外一个班级，"
            "请今明两天操作一下，方法如下： 加入【大数据实践教学…\n"
            "   需做：@all 由于期中考试全院通考，网上平台需要大家进入另外一… · 422482886 · 娟",
        )
        text = list_notices(FakeStore([row(1, body)], self.NAMES))["items"][0]["body"]
        self.assertNotIn("需做：", text)
        self.assertIn("2. [待办] @all 由于期中考试全院通考", text)

    def test_repeated_action_glued_after_the_deadline_is_removed(self) -> None:
        """旧格式还有「截止：… · 需做：… · 群 · 人」挪在同一行里的写法。"""
        body = self._body(
            "计算机26级专业群 · 63 条消息 · 7 条重点",
            "6. [通知] @all 通知：生涯教育与就业指导，本周五下午6-7节课在立德楼2阶上课\n"
            "   截止：2026-11-06 23:59 · 需做：@all 通知：生涯教育与就业指导，本周五下午6-7节课在… · 1051550372 · 龙老师",
        )
        text = list_notices(FakeStore([row(1, body)], self.NAMES))["items"][0]["body"]
        self.assertNotIn("需做：", text)
        self.assertIn("截止：2026-11-06 23:59 · 计算机26级专业群 · 龙老师", text)

    def test_different_action_glued_after_the_deadline_is_kept(self) -> None:
        body = self._body(
            "计算机26级专业群 · 63 条消息 · 7 条重点",
            "1. [紧急] 龙老师提醒今晚分享会需参加并签到，未到者速去\n"
            "   截止：2026-11-04 18:30 · 需做：尽快前往分享会现场并签到 · 1051550372 · 龙老师",
        )
        text = list_notices(FakeStore([row(1, body)], self.NAMES))["items"][0]["body"]
        self.assertIn("需做：尽快前往分享会现场并签到", text)

    def test_different_needs_action_line_is_kept(self) -> None:
        body = self._body(
            "计算机26级专业群 · 63 条消息 · 7 条重点",
            "1. [活动] 学院本周举办分享会\n   需做：尽快前往分享会现场并签到",
        )
        text = list_notices(FakeStore([row(1, body)], self.NAMES))["items"][0]["body"]
        self.assertIn("需做：尽快前往分享会现场并签到", text)

    def test_needs_action_without_a_title_is_kept(self) -> None:
        body = self._body("计算机26级专业群 · 63 条消息 · 7 条重点", "需做：请各班尽快交材料")
        text = list_notices(FakeStore([row(1, body)], self.NAMES))["items"][0]["body"]
        self.assertIn("需做：请各班尽快交材料", text)


class RealStoreTest(unittest.TestCase):
    """库里的群名表真能接上；用临时库，绝不碰用户 data/。"""

    def test_group_names_come_from_the_real_store(self) -> None:
        import tempfile

        from noticehub.store import Store

        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "digest.sqlite3")
            store.insert_digest(
                kind="window",
                window_start="2026-11-05T07:35:00",
                window_end="2026-11-05T07:41:00",
                body="791855425 · 63 条消息 · 7 条重点",
                payload=[{"summary": "示例条目"}],
                message_count=63,
                llm_used=False,
            )
            self.assertEqual(store.remember_group_names({"791855425": "计算机261通知群"}), 1)

            item = list_notices(store)["items"][0]
            self.assertIn("计算机261通知群 · 63 条消息", item["body"])
            self.assertEqual(item["window_end"], "2026-11-05T07:41:00")
            self.assertEqual(item["summary"], "1 条重点")


if __name__ == "__main__":
    unittest.main()
