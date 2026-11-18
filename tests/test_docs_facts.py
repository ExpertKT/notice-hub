"""赛事文档里的数字不靠手抄。

源文档写 ``{{facts.xxx}}``，``docs/contest/facts.json`` 由 ``tools/gen_facts.py`` 现算，
成品由 ``tools/render_docs.py`` 渲染。这几条用例锁住三件事：facts.json 跟当前代码一致、
成品跟「源文档 + facts」一致、文档里真的在用占位（有人把占位全删掉也能绿的话，这组用例没意义）。
"""

from __future__ import annotations

import copy
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTEST = ROOT / "docs" / "contest"
FACTS = CONTEST / "facts.json"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen_facts = _load("docs_gen_facts", ROOT / "tools" / "gen_facts.py")
render_docs = _load("docs_render_docs", ROOT / "tools" / "render_docs.py")


def _leaves(node, prefix: str = ""):
    """把嵌套事实摊成 ``{点号路径: 叶子}``；叶子是带 value 的那层。"""
    if isinstance(node, dict) and "value" in node:
        yield prefix, node
        return
    if isinstance(node, dict):
        for key, child in node.items():
            yield from _leaves(child, "%s.%s" % (prefix, key) if prefix else key)


def _values(node) -> dict:
    return {key: leaf["value"] for key, leaf in _leaves(node)}


class FactsJsonTest(unittest.TestCase):
    """facts.json 自己：每一条要么有值，要么显式 null 并写明为什么。"""

    def setUp(self):
        self.facts = json.loads(FACTS.read_text(encoding="utf-8"))

    def test_every_fact_has_a_value_or_a_reason(self):
        leaves = dict(_leaves(self.facts))
        self.assertTrue(leaves, "facts.json 里一条事实都没有")
        for key, leaf in leaves.items():
            with self.subTest(key=key):
                value = leaf["value"]
                if value is None:
                    self.assertTrue(leaf.get("why"), "%s 是 null，得写 why 说明为什么算不出来" % key)
                else:
                    self.assertNotIn(value, ("", [], {}), "%s 是空值，等于没算" % key)
                self.assertTrue(
                    leaf.get("source") or leaf.get("why"),
                    "%s 没写来源（文件:行号 或 命令）" % key,
                )

    def test_facts_match_current_code(self):
        """改了代码没重跑 gen_facts，这里红。"""
        fresh = _values(gen_facts.build_facts())
        saved = _values(self.facts)
        stale = sorted(key for key in set(fresh) | set(saved) if fresh.get(key) != saved.get(key))
        detail = ["%s：代码里是 %r，facts.json 里是 %r" % (k, fresh.get(k), saved.get(k)) for k in stale]
        self.assertEqual(stale, [], "facts.json 跟当前代码不一致，跑一次 tools/gen_facts.py：" + "；".join(detail))


class RenderedDocsTest(unittest.TestCase):
    """成品（docs/contest/rendered/）与「源文档 + facts」必须一致。"""

    def setUp(self):
        self.facts = render_docs.load_facts(FACTS)
        self.sources = render_docs.source_docs(CONTEST)
        self.rendered = render_docs.read_rendered(CONTEST)

    def test_check_passes(self):
        problems = render_docs.problems_for(self.sources, self.rendered, self.facts)
        self.assertEqual(problems, [], "render_docs --check 会红：\n" + "\n".join(problems))

    def test_docs_use_the_placeholders_that_matter(self):
        """最易漂的那几个数必须真的是占位，不能被写死回文档里。"""
        used: set[str] = set()
        for text in self.sources.values():
            used.update(render_docs.placeholders(text))
        required = {
            "test_defs",
            "code_lines.python",
            "code_lines.web_static",
            "routes",
            "poi_count",
            "trip_meters",
            "trip_minutes",
        }
        self.assertLessEqual(required, used, "这些占位在文档里已经没人用了：%s" % sorted(required - used))

    def test_a_wrong_number_in_facts_is_caught(self):
        """反证：把一条事实改错，--check 必须红（否则整组用例是空转）。"""
        wrong = copy.deepcopy(self.facts)
        wrong["trip_meters"]["value"] = wrong["trip_meters"]["value"] + 1
        problems = render_docs.problems_for(self.sources, self.rendered, wrong)
        self.assertTrue(problems, "facts 改了数，成品却没被判成不一致")
        self.assertTrue(any("trip_meters" in str(p) or "对不上" in str(p) for p in problems), problems)

    def test_a_stale_rendered_file_is_caught(self):
        """反证：成品里留着旧数字（有人改了文档没重渲染），必须红。"""
        name, text = next(
            (n, t) for n, t in self.rendered.items() if "{{facts." not in t
        )
        stale = dict(self.rendered)
        stale[name] = text.replace("米", "公里", 1) if "米" in text else text + "\n旧版残留\n"
        self.assertNotEqual(stale[name], text)
        problems = render_docs.problems_for(self.sources, stale, self.facts)
        self.assertTrue(problems, "成品被改坏了却没被判成不一致")
        self.assertIn(name, "\n".join(problems))

    def test_no_unresolved_placeholder_in_output(self):
        for name, text in self.rendered.items():
            with self.subTest(file=name):
                self.assertNotIn("{{facts.", text, "%s 里还有没替换的占位" % name)


if __name__ == "__main__":
    unittest.main()
