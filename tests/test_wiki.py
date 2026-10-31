"""wiki 是长期文档入口：页要在、链接要通、当前版本必须能在更新日志里找到。

这三条都不靠人自觉：少一页、链到不存在的文件、发版忘了写更新日志，测试直接红。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from noticehub.version import APP_VERSION

WIKI = Path(__file__).resolve().parent.parent / "docs" / "wiki"

REQUIRED_PAGES = (
    "README.md",
    "install.md",
    "use.md",
    "config.md",
    "architecture.md",
    "changelog.md",
    "faq.md",
)

# 正文里指向同目录其它 wiki 页的链接：`[文字](install.md)`、`[文字](install.md#锚点)`
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


class WikiPagesTest(unittest.TestCase):
    def test_required_pages_exist(self):
        self.assertTrue(WIKI.is_dir(), "docs/wiki 目录不存在")
        missing = [name for name in REQUIRED_PAGES if not (WIKI / name).is_file()]
        self.assertEqual(missing, [], "wiki 缺页：%s" % (missing,))

    def test_relative_links_resolve(self):
        broken: list[str] = []
        for page in sorted(WIKI.glob("*.md")):
            text = page.read_text(encoding="utf-8")
            for target in LINK.findall(text):
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                path = target.split("#", 1)[0]
                if not path.endswith(".md"):
                    continue
                if not (WIKI / path).is_file():
                    broken.append("%s -> %s" % (page.name, target))
        self.assertEqual(broken, [], "wiki 里有死链：%s" % (broken,))

    def test_changelog_has_unreleased_section(self):
        text = (WIKI / "changelog.md").read_text(encoding="utf-8")
        self.assertIn("未发布", text, "changelog 顶部要有「未发布」小节，日常改动写在这一节")

    def test_changelog_covers_current_version(self):
        text = (WIKI / "changelog.md").read_text(encoding="utf-8")
        self.assertIn(
            APP_VERSION,
            text,
            "更新日志里找不到当前版本 %s：发版时必须把它从「未发布」整理成一节" % APP_VERSION,
        )


if __name__ == "__main__":
    unittest.main()
