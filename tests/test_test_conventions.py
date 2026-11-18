"""测试约定静态闸自己的用例：抓得到违规，也别冤枉合规写法。

样例模板放 `tools/conventions_samples/`（放 `tests/` 里会把自己也扫成违规），
每个用例把它写成临时根下的 `tests/test_sample.py` 再用 `--root` 扫过去——不动仓库。
"""
from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tools import test_conventions

SAMPLES = Path(__file__).resolve().parent.parent / "tools" / "conventions_samples"


def _sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


class ConventionsGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="conventions-", dir=_sample_base())
        self.addCleanup(shutil.rmtree, Path(self.tmp), ignore_errors=True)

    def _root(self, name: str, sample: str) -> Path:
        root = Path(self.tmp) / name
        (root / "tests").mkdir(parents=True)
        (root / "tests" / "test_sample.py").write_text(_sample(sample), encoding="utf-8")
        return root

    def test_violating_sample_is_caught(self) -> None:
        root = self._root("violating", "violating.txt")
        found = test_conventions.collect(root)
        lines = sorted(hit["line"] for hit in found["rule_a"])
        # 8 行 Settings 没传 data_dir 又喂给按它建 store 的入口；13 行手拼仓库 data 路径。
        self.assertEqual(lines, [8, 13])
        self.assertTrue(all(hit["file"].name == "test_sample.py" for hit in found["rule_a"]))

        out = io.StringIO()
        with redirect_stdout(out):
            code = test_conventions.main(["--root", str(root)])
        self.assertEqual(code, 1)
        printed = out.getvalue()
        self.assertIn("tests/test_sample.py:13:", printed)
        self.assertIn("仓库 data", printed)
        # 规则 B 只警告：空断言被列出来，但不影响退出码。
        self.assertIn("规则 B：", printed)

    def test_compliant_sample_is_not_flagged(self) -> None:
        root = self._root("clean", "clean.txt")
        found = test_conventions.collect(root)
        self.assertEqual(found["rule_a"], [])

        out = io.StringIO()
        with redirect_stdout(out):
            code = test_conventions.main(["--root", str(root)])
        self.assertEqual(code, 0)
        self.assertNotIn("规则 A 命中", out.getvalue())


def _sample_base() -> str:
    """样例落 nh-tmp：这台机器的 Python 写不了用户 %TEMP%。"""
    base = os.environ.get("NH_GATE_TMP") or tempfile.gettempdir()
    Path(base).mkdir(parents=True, exist_ok=True)
    return base


if __name__ == "__main__":
    unittest.main()
