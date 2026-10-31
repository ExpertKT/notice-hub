"""临时目录卫生：测试产生的临时目录必须被清掉，且被 .gitignore 兜住。

背景：TEMP 指向仓库根时，mkdtemp 会在仓库里留下 notice-hub-*/noticehub-tests-*
一类的目录（曾积到 3800+ 个），既污染 git status 也拖慢扫描。
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 与 .gitignore 里的临时产物前缀保持一致
IGNORED_PREFIXES = (
    "notice-hub-watchdog-",
    "notice-hub-quitreq-",
    "notice-hub-quit-",
    "notice-hub-mutex-",
    "notice-hub-log-",
    "notice-hub-onedir-",
    "noticehub-tests-",
    "nh-inline-js-",
    "nh-theme-audit-",
)


def _matching_dirs() -> set[str]:
    return {
        entry.name
        for entry in ROOT.iterdir()
        if entry.is_dir() and entry.name.startswith(IGNORED_PREFIXES)
    }


class TempDirHygieneTests(unittest.TestCase):
    def test_gitignore_covers_temp_dir_prefixes(self):
        text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        missing = [p for p in IGNORED_PREFIXES if p not in text]
        self.assertEqual(missing, [], f".gitignore 缺少临时目录前缀：{missing}")

    def test_temp_dirs_are_cleaned_up(self):
        """在仓库根建一个临时目录，清掉后根目录不应多出任何匹配前缀的目录。"""
        before = _matching_dirs()
        path = Path(tempfile.mkdtemp(prefix="noticehub-tests-", dir=str(ROOT)))
        try:
            self.assertTrue(path.is_dir())
            self.assertIn(path.name, _matching_dirs())
        finally:
            shutil.rmtree(path, ignore_errors=True)

        self.assertFalse(path.exists())
        self.assertEqual(_matching_dirs() - before, set())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()