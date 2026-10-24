from __future__ import annotations

import importlib.machinery
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest import lab  # noqa: E402
from qq_live_digest.config import Settings  # noqa: E402


def _on(name: str) -> Settings:
    return Settings(**{f"lab_{name}": True})


def _inject_module(full_name: str, **attrs: object) -> None:
    """用假模块占住 sys.modules，验证门面正常转发（mock.patch.dict 保证还原）。

    必须带 ``__spec__``：find_spec 对 sys.modules 里没有 __spec__ 的模块
    会抛 ValueError，门面会把它误判成「模块尚未就绪」。
    """
    module = types.ModuleType(full_name)
    module.__spec__ = importlib.machinery.ModuleSpec(full_name, None)
    for key, value in attrs.items():
        setattr(module, key, value)
    patcher = mock.patch.dict(sys.modules, {full_name: module})
    patcher.start()
    return patcher


class LabAvailableTest(unittest.TestCase):
    def test_all_false_when_defaults(self) -> None:
        self.assertEqual(
            lab.available(Settings()),
            {"search": False, "replies": False, "events": False, "rules": False},
        )

    def test_switch_on_but_module_missing_is_false(self) -> None:
        # 不依赖外部文件状态（search.py 可能已落盘）：mock find_spec 为 None 模拟模块缺失。
        with mock.patch.object(
            lab.importlib.util, "find_spec", return_value=None
        ):
            available = lab.available(_on("search"))
        self.assertFalse(available["search"])
        self.assertFalse(available["replies"])
        self.assertFalse(available["events"])
        self.assertFalse(available["rules"])

    def test_real_event_extract_module_counts_as_ready(self) -> None:
        self.assertTrue(lab.available(_on("events"))["events"])

    def test_module_injected_becomes_available(self) -> None:
        with mock.patch.dict(sys.modules, {}):
            with mock.patch.object(
                lab.importlib.util,
                "find_spec",
                return_value=object(),
            ):
                available = lab.available(_on("search"))
        self.assertTrue(available["search"])


class LabEntryTest(unittest.TestCase):
    def test_switch_off_rejects_every_entry(self) -> None:
        settings = Settings()
        for call in (
            lambda: lab.search(settings, "作业"),
            lambda: lab.list_replies(settings),
            lambda: lab.ack_reply(settings, "m1"),
            lambda: lab.extract_events(settings, "明天开会"),
        ):
            result = call()
            self.assertEqual(result, {"ok": False, "error": "实验功能未开启"})

    def test_module_missing_degrades_gracefully(self) -> None:
        # 不依赖外部文件状态：mock find_spec 为 None 模拟模块缺失。
        with mock.patch.object(
            lab.importlib.util, "find_spec", return_value=None
        ):
            result = lab.search(_on("search"), "作业")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "该实验功能尚未就绪")

    def test_search_forwards_and_returns_result_verbatim(self) -> None:
        sentinel = [{"id": 1, "text": "作业通知"}]
        captured: dict[str, object] = {}

        def fake_search(query: str, **filters: object) -> list[dict[str, object]]:
            captured["query"] = query
            captured["filters"] = filters
            return sentinel

        with _inject_module("qq_live_digest.search", search=fake_search):
            result = lab.search(_on("search"), "作业", group="g1", days=7)
        self.assertEqual(result, {"ok": True, "result": sentinel})
        self.assertEqual(captured["query"], "作业")
        self.assertEqual(captured["filters"], {"group": "g1", "days": 7})

    def test_replies_entries_forward(self) -> None:
        with _inject_module(
            "qq_live_digest.replies",
            list_replies=lambda **filters: [("m1", "待确认")],
            ack_reply=lambda msg_id: f"acked:{msg_id}",
        ):
            listed = lab.list_replies(_on("replies"), group="g2")
            acked = lab.ack_reply(_on("replies"), "m1")
        self.assertEqual(listed, {"ok": True, "result": [("m1", "待确认")]})
        self.assertEqual(acked, {"ok": True, "result": "acked:m1"})

    def test_extract_events_forwards_to_event_extract(self) -> None:
        fake = lambda text, **kwargs: [{"what": text}]  # noqa: E731
        with _inject_module("qq_live_digest.event_extract", extract_events=fake):
            result = lab.extract_events(_on("events"), "明天开会", min_level="high")
        self.assertEqual(result, {"ok": True, "result": [{"what": "明天开会"}]})

    def test_underlying_exception_is_wrapped_not_raised(self) -> None:
        def boom(query: str, **kwargs: object) -> None:
            raise RuntimeError("底层炸了")

        with _inject_module("qq_live_digest.search", search=boom):
            result = lab.search(_on("search"), "x")
        self.assertFalse(result["ok"])
        self.assertIn("底层炸了", result["error"])

    def test_broken_module_import_is_wrapped(self) -> None:
        # 模块 find_spec 认为存在，但 import 时抛错（比如语法坏了）。
        with mock.patch.object(
            lab.importlib.util, "find_spec", return_value=object()
        ), mock.patch.object(
            lab.importlib,
            "import_module",
            side_effect=SyntaxError("坏模块"),
        ):
            result = lab.search(_on("search"), "x")
        self.assertFalse(result["ok"])
        self.assertIn("尚未就绪", result["error"])

    def test_unknown_name_is_rejected(self) -> None:
        result = lab.call(_on("search"), "nope")
        self.assertEqual(result, {"ok": False, "error": "未知实验功能"})
        # 真名但不存在的属性也走优雅降级而不是 AttributeError。
        with _inject_module("qq_live_digest.replies"):
            result = lab.call(_on("replies"), "replies")
        self.assertFalse(result["ok"])
        self.assertIn("尚未就绪", result["error"])


class LabPurityTest(unittest.TestCase):
    def test_no_io_no_global_state_in_source(self) -> None:
        source = Path(lab.__file__).read_text(encoding="utf-8")
        for banned in ("open(", "requests.", "urllib", "socket", "sqlite3"):
            self.assertNotIn(banned, source)
        self.assertNotIn("LAB_STATE", source)  # 没有模块级可变状态


if __name__ == "__main__":
    unittest.main()
