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

from noticehub.platform import lab# noqa: E402
from noticehub.platform.config import Settings  # noqa: E402

LAB_NAMES = ("search", "events", "rules")


def _settings() -> Settings:
    return Settings()


def _inject_module(full_name: str, **attrs: object) -> None:
    """用假模块占住 sys.modules，验证门面正常转发（mock.patch.dict 保证还原）。

    必须带 ``__spec__``：find_spec 对 sys.modules 里没有 __spec__ 的模块
    会抛 ValueError，门面会把它误判成「模块尚未就绪」。
    """
    module = types.ModuleType(full_name)
    module.__spec__ = importlib.machinery.ModuleSpec(full_name, None)
    for key, value in attrs.items():
        setattr(module, key, value)
    # 不在这里 start()：调用方一律 ``with _inject_module(...)`` 使用，
    # with 的 __enter__ 会再 start 一次，二次 start 会把假模块当成「原始值」
    # 快照，stop() 恢复出来的就是假模块而不是真模块（全量跑时污染 sys.modules）。
    return mock.patch.dict(sys.modules, {full_name: module})


class LabAvailableTest(unittest.TestCase):
    def test_available_covers_the_three_features(self) -> None:
        # 1.1.0 起常开：available 只看模块在不在，待回清单整条链路已删。
        available = lab.available(_settings())
        self.assertEqual(set(available), set(LAB_NAMES))
        self.assertNotIn("replies", available)

    def test_module_missing_is_false(self) -> None:
        # 不依赖外部文件状态：mock find_spec 为 None 模拟模块缺失。
        with mock.patch.object(lab.importlib.util, "find_spec", return_value=None):
            available = lab.available(_settings())
        self.assertEqual(available, {"search": False, "events": False, "rules": False})

    def test_real_modules_count_as_ready(self) -> None:
        available = lab.available(_settings())
        self.assertTrue(available["events"])
        self.assertTrue(available["rules"])

    def test_module_injected_becomes_available(self) -> None:
        with mock.patch.object(lab.importlib.util, "find_spec", return_value=object()):
            available = lab.available(_settings())
        self.assertEqual(available, {"search": True, "events": True, "rules": True})


class LabEntryTest(unittest.TestCase):
    def test_rules_switch_off_is_rejected(self) -> None:
        # 只剩自动整理规则一个开关：关着时三个入口都拒绝，文案对齐「已关闭」。
        settings = Settings(lab_rules=False)
        for call in (
            lambda: lab.list_rules(settings),
            lambda: lab.save_rules(settings, []),
            lambda: lab.test_rules(settings, {"content": "x"}),
        ):
            self.assertEqual(call(), {"ok": False, "error": "自动整理规则已关闭"})

    def test_other_features_ignore_the_rules_switch(self) -> None:
        # 检索与抽日程常开：rules 关着也不影响它们。
        with _inject_module("noticehub.web.search", search=lambda *a, **k: ["hit"]):
            result = lab.search(Settings(lab_rules=False), "库/digest.sqlite3", "作业")
        self.assertEqual(result, {"ok": True, "result": ["hit"]})

    def test_module_missing_degrades_gracefully(self) -> None:
        # 不依赖外部文件状态：mock find_spec 为 None 模拟模块缺失。
        with mock.patch.object(lab.importlib.util, "find_spec", return_value=None):
            result = lab.search(_settings(), "库/digest.sqlite3", "作业")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "该实验功能尚未就绪")

    def test_search_forwards_db_path_query_and_returns_result_verbatim(self) -> None:
        sentinel = [{"id": 1, "text": "作业通知"}]
        captured: dict[str, object] = {}

        def fake_search(
            db_path: object, query: str, **filters: object
        ) -> list[dict[str, object]]:
            captured["db"] = db_path
            captured["query"] = query
            captured["filters"] = filters
            return sentinel

        with _inject_module("noticehub.web.search", search=fake_search):
            result = lab.search(
                _settings(), "库/digest.sqlite3", "作业", group="g1", days=7
            )
        self.assertEqual(result, {"ok": True, "result": sentinel})
        self.assertEqual(captured["query"], "作业")
        self.assertEqual(captured["filters"], {"group": "g1", "days": 7})
        # 消息库路径必须由调用方交进来：门面自己拼路径就会把「未就绪」掩盖成永远 503。
        self.assertEqual(captured["db"], "库/digest.sqlite3")

    def test_extract_events_forwards_to_event_extract(self) -> None:
        fake = lambda text, **kwargs: [{"what": text}]  # noqa: E731
        with _inject_module("noticehub.ingest.event_extract", extract_events=fake):
            result = lab.extract_events(_settings(), "明天开会", min_level="high")
        self.assertEqual(result, {"ok": True, "result": [{"what": "明天开会"}]})

    def test_list_rules_forwards_settings_and_returns_result(self) -> None:
        sentinel = {"rules": [{"id": "a"}], "count": 1, "path": "/tmp/rules.json"}
        captured: dict[str, object] = {}

        def fake_load(settings: object) -> dict:
            captured["settings"] = settings
            return sentinel

        settings = _settings()
        with _inject_module("noticehub.ingest.rules", load_rules=fake_load):
            result = lab.list_rules(settings)
        self.assertEqual(result, {"ok": True, "result": sentinel})
        self.assertIs(captured["settings"], settings)

    def test_save_rules_forwards_items_and_returns_result(self) -> None:
        sentinel = {"ok": True, "count": 2, "path": "/tmp/rules.json"}
        captured: dict[str, object] = {}

        def fake_save(settings: object, items: object) -> dict:
            captured["settings"] = settings
            captured["items"] = items
            return sentinel

        items = [{"id": "a"}, {"id": "b"}]
        with _inject_module("noticehub.ingest.rules", save_rules=fake_save):
            result = lab.save_rules(_settings(), items)
        self.assertEqual(result, {"ok": True, "result": sentinel})
        self.assertEqual(captured["items"], items)

    def test_real_rules_module_is_ready(self) -> None:
        # rules.py 已定稿：真模块在盘上，available() 必须为真。
        self.assertTrue(lab.available(_settings())["rules"])

    def test_test_rules_forwards_message_and_returns_result(self) -> None:
        sentinel = {"ok": True, "hits": [], "count": 0, "rule_count": 0}
        captured: dict[str, object] = {}

        def fake_test(settings: object, message: object) -> dict:
            captured["message"] = message
            return sentinel

        message = {"content": "明天开会"}
        with _inject_module("noticehub.ingest.rules", test_rules=fake_test):
            result = lab.test_rules(_settings(), message)
        self.assertEqual(result, {"ok": True, "result": sentinel})
        self.assertEqual(captured["message"], message)

    def test_underlying_exception_is_wrapped_not_raised(self) -> None:
        def boom(db_path: object, query: str, **kwargs: object) -> None:
            raise RuntimeError("底层炸了")

        with _inject_module("noticehub.web.search", search=boom):
            result = lab.search(_settings(), "库/digest.sqlite3", "x")
        self.assertFalse(result["ok"])
        self.assertIn("底层炸了", result["error"])
        # 模块和函数都在，失败就是这次调用真出错：别把原因抹成「尚未就绪」。
        self.assertIn("RuntimeError", result["error"])
        self.assertNotIn("尚未就绪", result["error"])

    def test_broken_module_import_is_wrapped(self) -> None:
        # 模块 find_spec 认为存在，但 import 时抛错（比如语法坏了）。
        with mock.patch.object(
            lab.importlib.util, "find_spec", return_value=object()
        ), mock.patch.object(
            lab.importlib,
            "import_module",
            side_effect=SyntaxError("坏模块"),
        ):
            result = lab.search(_settings(), "库/digest.sqlite3", "x")
        self.assertFalse(result["ok"])
        self.assertIn("尚未就绪", result["error"])

    def test_unknown_name_is_rejected(self) -> None:
        result = lab.call(_settings(), "nope")
        self.assertEqual(result, {"ok": False, "error": "未知实验功能"})

    def test_missing_attribute_degrades_gracefully(self) -> None:
        # 真名但模块里没有这个函数也走优雅降级而不是 AttributeError。
        with _inject_module("noticehub.ingest.event_extract"):
            result = lab.call(_settings(), "events")
        self.assertFalse(result["ok"])
        self.assertIn("尚未就绪", result["error"])
        self.assertIn("模块里没有 events", result["error"])


class LabPurityTest(unittest.TestCase):
    def test_no_io_no_global_state_in_source(self) -> None:
        source = Path(lab.__file__).read_text(encoding="utf-8")
        for banned in ("open(", "requests.", "urllib", "socket", "sqlite3"):
            self.assertNotIn(banned, source)
        self.assertNotIn("LAB_STATE", source)  # 没有模块级可变状态


if __name__ == "__main__":
    unittest.main()
