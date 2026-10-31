# -*- coding: utf-8 -*-
"""tests for noticehub.rules —— 规则引擎纯函数内核（W27）+ 规则集存取（beta.13 接线）。"""

from __future__ import annotations

import json
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from noticehub import rules
from noticehub.config import Settings


def _rule(**over):
    base = {
        "id": "r1",
        "enabled": True,
        "scope": "*",
        "match": {"keyword": ["通知"]},
        "action": {"action": "mark_important", "payload": {"reason": "test"}},
        "priority": 0,
    }
    base.update(over)
    return base


def _msg(**over):
    base = {
        "msg_id": "m1",
        "group_id": "g1",
        "sender": "u1",
        "content": "今晚通知：明天开会",
        "at_me": False,
    }
    base.update(over)
    return base


class RuleParsingTest(unittest.TestCase):
    def test_parse_and_hit_keyword(self):
        got = rules.match(_msg(), rules.parse_rules([_rule()]))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["rule_id"], "r1")
        self.assertEqual(got[0]["action"]["action"], "mark_important")

    def test_keyword_any_of_multiple(self):
        r = _rule(match={"keyword": ["停课", "放假"]})
        got = rules.match(_msg(content="明天放假一天"), rules.parse_rules([r]))
        self.assertEqual([h["rule_id"] for h in got], ["r1"])

    def test_string_action_wraps_payload(self):
        got = rules.match(_msg(), rules.parse_rules([_rule(action="pin")]))
        self.assertEqual(got[0]["action"], {"action": "pin", "payload": {}})

    def test_disabled_rule_never_hits(self):
        got = rules.match(_msg(), rules.parse_rules([_rule(enabled=False)]))
        self.assertEqual(got, [])

    def test_invalid_rules_skipped_not_raised(self):
        items = [
            "not a dict",
            {"match": {"keyword": ["x"]}},               # 缺 id
            _rule(id="bad-match", match="oops"),          # match 非 dict
            _rule(id="bad-scope", scope=123),             # scope 非法
            _rule(id="bad-priority", priority="high"),    # priority 非整数
            _rule(id="bad-regex", match={"regex": "([unclosed"}),  # 编译失败
            _rule(id="no-action", action={}),             # 无动作名
            _rule(id="ok"),                               # 合法的那条
        ]
        got = rules.match(_msg(), rules.parse_rules(items))
        self.assertEqual([h["rule_id"] for h in got], ["ok"])

    def test_empty_rule_list(self):
        self.assertEqual(rules.match(_msg(), rules.parse_rules([])), [])


class MatcherTest(unittest.TestCase):
    def test_regex_hit_and_case(self):
        r = _rule(match={"regex": r"\d{4}-\d{2}-\d{2}"})
        got = rules.match(_msg(content="截止 2026-11-01 前提交"), rules.parse_rules([r]))
        self.assertEqual([h["rule_id"] for h in got], ["r1"])

    def test_regex_miss_when_no_match(self):
        r = _rule(match={"regex": r"\d{4}-\d{2}-\d{2}"})
        got = rules.match(_msg(), rules.parse_rules([r]))
        self.assertEqual(got, [])

    def test_regex_and_keyword_are_and(self):
        r = _rule(match={"keyword": ["提交"], "regex": r"\d{4}-\d{2}-\d{2}"})
        self.assertEqual(rules.match(_msg(), rules.parse_rules([r])), [])
        got = rules.match(_msg(content="请在 2026-11-01 前提交表格"), rules.parse_rules([r]))
        self.assertEqual([h["rule_id"] for h in got], ["r1"])

    def test_at_me_requires_flag(self):
        r = _rule(match={"at_me": True})
        self.assertEqual(rules.match(_msg(at_me=False), rules.parse_rules([r])), [])
        got = rules.match(_msg(at_me=True), rules.parse_rules([r]))
        self.assertEqual([h["rule_id"] for h in got], ["r1"])

    def test_sender_filter(self):
        r = _rule(match={"sender": "boss"})
        self.assertEqual(rules.match(_msg(sender="u1"), rules.parse_rules([r])), [])
        got = rules.match(_msg(sender="boss"), rules.parse_rules([r]))
        self.assertEqual([h["rule_id"] for h in got], ["r1"])

    def test_scope_group_list(self):
        r = _rule(scope=["g2"])
        self.assertEqual(rules.match(_msg(group_id="g1"), rules.parse_rules([r])), [])
        got = rules.match(_msg(group_id="g2"), rules.parse_rules([r]))
        self.assertEqual([h["rule_id"] for h in got], ["r1"])

    def test_empty_match_is_rejected(self):
        # match:{} 等于匹配全部消息，属配置漏写，必须拒绝而不是放行。
        self.assertEqual(rules.parse_rules([_rule(match={})]), [])

    def test_misspelled_match_key_is_rejected(self):
        # 复数 regexes 是笔误：若静默忽略会退化成「匹配全部」。
        with self.assertLogs("noticehub.rules", level="WARNING"):
            got = rules.parse_rules([_rule(id="typo", match={"regexes": ["部署"]})])
        self.assertEqual(got, [])

    def test_unknown_key_among_legal_keys_is_rejected(self):
        got = rules.parse_rules([
            _rule(id="mix", match={"keyword": ["部署"], "source": ["qq"]}),
        ])
        self.assertEqual(got, [])

    def test_legal_match_keys_accepted(self):
        got = rules.parse_rules([
            _rule(id="k", match={"keyword": ["部署"]}),
            _rule(id="r", match={"regex": r"^\d{2}:\d{2}$"}),
            _rule(id="a", match={"at_me": True}),
            _rule(id="s", match={"sender": "boss"}),
        ])
        self.assertEqual([one.id for one in got], ["k", "r", "a", "s"])


class OrderingAndDedupeTest(unittest.TestCase):
    def test_priority_desc_order(self):
        rs = rules.parse_rules([
            _rule(id="low", priority=1),
            _rule(id="high", priority=9),
            _rule(id="mid", priority=5),
        ])
        got = rules.match(_msg(), rs)
        self.assertEqual([h["rule_id"] for h in got], ["high", "mid", "low"])

    def test_dedupe_key_template_and_once(self):
        rs = rules.parse_rules([
            _rule(id="a", priority=1, dedupe_key="{group_id}:daily"),
            _rule(id="b", priority=2, dedupe_key="{group_id}:daily"),
        ])
        got = rules.match(_msg(), rs)
        # b 优先级更高先返回；a 同 key 被去掉。
        self.assertEqual([h["rule_id"] for h in got], ["b"])
        self.assertEqual(got[0]["dedupe_key"], "g1:daily")

    def test_dedupe_key_template_vars(self):
        r = _rule(dedupe_key="{msg_id}:{group_id}:{missing_ok}")
        got = rules.match(_msg(), rules.parse_rules([r]))
        self.assertEqual(got[0]["dedupe_key"], "m1:g1:")

    def test_dedupe_differs_per_message(self):
        rs = rules.parse_rules([_rule(dedupe_key="{msg_id}")])
        one = rules.match(_msg(msg_id="m1"), rs)
        two = rules.match(_msg(msg_id="m2"), rs)
        self.assertEqual(one[0]["dedupe_key"], "m1")
        self.assertEqual(two[0]["dedupe_key"], "m2")

    def test_no_dedupe_key_returns_none(self):
        got = rules.match(_msg(), rules.parse_rules([_rule()]))
        self.assertIsNone(got[0]["dedupe_key"])


class RegexSafetyTest(unittest.TestCase):
    def test_body_truncated_before_matching(self):
        # 关键词落在 500 字符截断线之后：不得命中。
        far = "x" * 600 + "通知"
        got = rules.match(_msg(content=far), rules.parse_rules([_rule()]))
        self.assertEqual(got, [])
        # 同一关键词在截断线内：命中。
        near = "x" * 100 + "通知"
        got = rules.match(_msg(content=near), rules.parse_rules([_rule()]))
        self.assertEqual([h["rule_id"] for h in got], ["r1"])

    def test_dangerous_patterns_rejected_before_compile(self):
        dangerous = [r"(a+)+$", r"(a*)*$", r"(.*a){20}", r"a.{0,}b.{0,}", r"(.*)(.*)"]
        with mock.patch.object(rules.re, "compile", wraps=re.compile) as compile_mock:
            for index, pattern in enumerate(dangerous):
                with self.assertLogs("noticehub.rules", level="WARNING"):
                    got = rules.parse_rules([
                        _rule(id=f"evil{index}", match={"regex": pattern}),
                    ])
                self.assertEqual(got, [], f"{pattern!r} 应被静态拒绝")
            self.assertFalse(
                [c for c in compile_mock.call_args_list if c.args[0] in dangerous],
                "高危模式不应进入 re.compile",
            )

    def test_overlong_pattern_rejected(self):
        pattern = "a" * (rules.MAX_PATTERN_CHARS + 1)
        with self.assertLogs("noticehub.rules", level="WARNING"):
            self.assertEqual(rules.parse_rules([_rule(match={"regex": pattern})]), [])

    def test_normal_patterns_still_work(self):
        cases = [
            ("部署", "明天部署上线", True),
            (r"^收到$", "收到", True),
            (r"^收到$", "我收到了", False),
            (r"\d{2}:\d{2}", "会议 14:30 开始", True),
        ]
        for pattern, content, expected in cases:
            got = rules.match(
                _msg(content=content),
                rules.parse_rules([_rule(match={"regex": pattern})]),
            )
            self.assertEqual(bool(got), expected, f"{pattern!r} vs {content!r}")

    def test_truncation_bounds_slow_backtracking(self):
        # 高危模式已被静态拒绝；这里验证截断上限本身不再随正文增长而变慢。
        self.assertEqual(rules.MAX_TEXT_CHARS, 500)
        start = time.perf_counter()
        rules.match(_msg(content="a" * 20000 + "b"), rules.parse_rules([_rule()]))
        self.assertLess(time.perf_counter() - start, 1.0)

    def test_purity_no_io_no_state(self):
        # 连续两次调用结果一致，模块无全局可变状态；message 不被修改。
        rs = rules.parse_rules([_rule()])
        msg = _msg()
        snapshot = dict(msg)
        first = rules.match(msg, rs)
        second = rules.match(msg, rs)
        self.assertEqual(first, second)
        self.assertEqual(msg, snapshot)


class RuleStoreTest(unittest.TestCase):
    """load_rules / save_rules：只碰 <data_dir>/rules.json，fail-closed。"""

    def _settings(self, root: Path) -> Settings:
        return Settings(data_dir=root, log_dir=root / "logs")

    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            got = rules.load_rules(self._settings(Path(tmp)))
        self.assertEqual(got["rules"], [])
        self.assertEqual(got["count"], 0)

    def test_broken_json_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text("{not json", encoding="utf-8")
            got = rules.load_rules(self._settings(Path(tmp)))
        self.assertEqual(got["rules"], [])

    def test_broken_json_array_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "rules.json").write_text('["nope"]', encoding="utf-8")
            got = rules.load_rules(self._settings(Path(tmp)))
        self.assertEqual(got["rules"], [])

    def test_round_trip_preserves_fields(self):
        items = [
            _rule(id="a", match={"keyword": ["通知"]}, priority=5, dedupe_key="{group_id}:daily"),
            _rule(id="b", match={"regex": r"^\d{2}:\d{2}$", "at_me": True}, scope=["g1", "g2"]),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            saved = rules.save_rules(self._settings(root), items)
            self.assertTrue(saved["ok"])
            self.assertEqual(saved["count"], 2)
            got = rules.load_rules(self._settings(root))
        self.assertEqual(got["count"], 2)
        by_id = {one["id"]: one for one in got["rules"]}
        self.assertEqual(by_id["a"]["match"]["keyword"], ["通知"])
        self.assertEqual(by_id["a"]["priority"], 5)
        self.assertEqual(by_id["a"]["dedupe_key"], "{group_id}:daily")
        self.assertEqual(by_id["a"]["enabled"], True)
        # regex 从编译后的 pattern 还原成字符串，scope 列表原样回来。
        self.assertEqual(by_id["b"]["match"]["regex"], [r"^\d{2}:\d{2}$"])
        self.assertEqual(by_id["b"]["scope"], ["g1", "g2"])
        # at_me=True 才落到 match 里；False 是默认值，不占字段。
        self.assertEqual(by_id["b"]["match"]["at_me"], True)

    def test_invalid_rule_rejected_and_nothing_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "rules.json"
            saved = rules.save_rules(self._settings(root), [_rule(), _rule(match={})])
            self.assertFalse(saved["ok"])
            self.assertFalse(target.exists(), "非法规则必须一个字节都不写")

    def test_bad_shape_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            saved = rules.save_rules(self._settings(root), {"not": "a list"})
            self.assertFalse(saved["ok"])
            self.assertFalse((root / "rules.json").exists())

    def test_previous_file_survives_a_rejected_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertTrue(rules.save_rules(self._settings(root), [_rule()])["ok"])
            before = (root / "rules.json").read_text(encoding="utf-8")
            self.assertFalse(rules.save_rules(self._settings(root), [_rule(match={})])["ok"])
            self.assertEqual((root / "rules.json").read_text(encoding="utf-8"), before)

    def test_no_tmp_file_left_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rules.save_rules(self._settings(root), [_rule()])
            rules.save_rules(self._settings(root), [_rule(match={})])
            self.assertEqual([p.name for p in root.iterdir()], ["rules.json"])

    def test_loaded_rules_are_usable_by_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rules.save_rules(self._settings(root), [_rule(match={"keyword": ["通知"]})])
            loaded = rules.load_rules(self._settings(root))
            parsed = rules.parse_rules(loaded["rules"])
            self.assertEqual([h["rule_id"] for h in rules.match(_msg(), parsed)], ["r1"])

    def test_invalid_rule_reports_its_index_and_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = rules.save_rules(self._settings(Path(tmp)), [_rule(), _rule(match={})])
        self.assertFalse(saved["ok"])
        self.assertIn("第 2 条规则非法", saved["error"])
        self.assertIn("match 不能为空", saved["error"])

    def test_misspelled_key_reports_its_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = rules.save_rules(
                self._settings(Path(tmp)), [_rule(id="typo", match={"regexes": ["部署"]})]
            )
        self.assertFalse(saved["ok"])
        self.assertIn("第 1 条规则非法", saved["error"])
        self.assertIn("regexes", saved["error"])


class RuleTestRunTest(unittest.TestCase):
    """test_rules：拿当前规则集试跑一条消息，只读。"""

    def _settings(self, root: Path) -> Settings:
        return Settings(data_dir=root, log_dir=root / "logs")

    def test_no_rules_no_hits(self):
        with tempfile.TemporaryDirectory() as tmp:
            got = rules.test_rules(self._settings(Path(tmp)), {"content": "任意消息"})
        self.assertTrue(got["ok"])
        self.assertEqual(got["hits"], [])
        self.assertEqual(got["rule_count"], 0)

    def test_matching_rule_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rules.save_rules(self._settings(root), [_rule(match={"keyword": ["通知"]})])
            got = rules.test_rules(self._settings(root), {"content": "这是一条通知"})
        self.assertTrue(got["ok"])
        self.assertEqual(got["count"], 1)
        self.assertEqual(got["hits"][0]["rule_id"], "r1")

    def test_non_matching_message_has_no_hits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rules.save_rules(self._settings(root), [_rule(match={"keyword": ["通知"]})])
            got = rules.test_rules(self._settings(root), {"content": "闲聊"})
        self.assertEqual(got["count"], 0)

    def test_empty_content_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            got = rules.test_rules(self._settings(Path(tmp)), {"content": "   "})
        self.assertFalse(got["ok"])
        self.assertIn("缺少 content", got["error"])

    def test_non_dict_message_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            got = rules.test_rules(self._settings(Path(tmp)), "不是对象")
        self.assertFalse(got["ok"])
        self.assertIn("必须是对象", got["error"])

    def test_overlong_content_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            got = rules.test_rules(self._settings(Path(tmp)), {"content": "x" * 20000})
        self.assertFalse(got["ok"])
        self.assertIn("过长", got["error"])


if __name__ == "__main__":
    unittest.main()
