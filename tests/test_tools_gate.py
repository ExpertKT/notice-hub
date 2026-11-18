"""门禁脚本自己那几条容易坏的地方。

只锁两件事：写不进去要认得出来（之前仓库根建临时目录被拒就是这个判据），
以及换解释器只能换一次——换过去的那个如果还判「写不了」，来回 exec 会把
一次门禁跑成死循环。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
GATE_PY = ROOT / "tools" / "gate.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("nh_tools_gate", GATE_PY)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class GateInterpreterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gate = _load_gate()

    def test_writable_is_false_for_a_path_that_refuses_writes(self):
        """写不进去必须报 False，而不是抛异常或者乐观返回 True。"""
        self.assertTrue(self.gate._writable(Path(tempfile.mkdtemp())))
        blocked = Path(tempfile.mkdtemp()) / "probe"
        with mock.patch.object(Path, "write_text", side_effect=PermissionError(13, "拒绝访问")):
            self.assertFalse(self.gate._writable(blocked))

    def test_isolated_interpreter_gets_told_what_to_run(self):
        """写不了仓库时要打出该怎么起，而且不许自己 exec——被限的进程拉起的子进程一样受限。"""
        with mock.patch.object(self.gate, "_writable", return_value=False), \
                mock.patch.object(self.gate.subprocess, "call") as call, \
                mock.patch("sys.stderr") as err:
            self.assertIsNone(self.gate.reexec_if_isolated(["probe"]))
        call.assert_not_called()
        printed = "".join(part.args[0] for part in err.write.call_args_list)
        self.assertIn("--tier full", printed)
        self.assertIn("PYTHONPATH", printed)

    def test_no_reexec_when_the_repo_is_writable(self):
        """能写仓库就别唠叨，也不要白起进程、丢掉调用方设的环境。"""
        with mock.patch.object(self.gate, "_writable", return_value=True), \
                mock.patch.object(self.gate.subprocess, "call") as call, \
                mock.patch("sys.stderr") as err:
            self.assertIsNone(self.gate.reexec_if_isolated(["probe"]))
        call.assert_not_called()
        err.write.assert_not_called()


def _load_gate_check():
    spec = importlib.util.spec_from_file_location("nh_tools_gate_check",
                                                  ROOT / "tools" / "gate_check.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class GateCheckTest(unittest.TestCase):
    """推送闸只认「当前产品代码绿」：sh a 变了但 noticehub/ 树没变，仍然放行。"""

    def setUp(self):
        self.check = _load_gate_check()
        self.tmp = Path(tempfile.mkdtemp())
        self.record = self.tmp / "last-full.json"

    def _write(self, payload):
        import json
        self.record.write_text(json.dumps(payload), encoding="utf-8")

    def _run(self, head, tree, payload):
        self._write(payload)
        with mock.patch.object(self.check, "RECORD", self.record), \
                mock.patch.object(self.check, "head_sha", return_value=head), \
                mock.patch.object(self.check, "product_tree", return_value=tree), \
                mock.patch("sys.stdout"), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NH_GATE_BYPASS", None)
            return self.check.main()

    def test_same_sha_is_accepted(self):
        self.assertEqual(self._run("aaa", "t1", {"sha": "aaa", "product": "t1", "exit": 0}), 0)

    def test_docs_only_commit_still_counts(self):
        """只改了文档：HEAD 变了，noticehub/ 树没变 → 不用再跑 11 分钟全量。"""
        code = self._run("bbb", "t1", {"sha": "aaa", "product": "t1", "exit": 0})
        self.assertEqual(code, 0)

    def test_changed_product_blocks_the_push(self):
        code = self._run("bbb", "t2", {"sha": "aaa", "product": "t1", "exit": 0})
        self.assertEqual(code, 1)

    def test_red_run_blocks_the_push(self):
        code = self._run("aaa", "t1", {"sha": "aaa", "product": "t1", "exit": 1,
                                       "reds": {"x": {}}})
        self.assertEqual(code, 1)

    def test_missing_record_blocks_the_push(self):
        with mock.patch.object(self.check, "RECORD", self.tmp / "nope.json"), \
                mock.patch.object(self.check, "head_sha", return_value="aaa"), \
                mock.patch("sys.stdout"):
            self.assertEqual(self.check.main(), 1)


if __name__ == "__main__":
    unittest.main()
