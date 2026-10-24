"""防派生：同一件事只能有一份实现。

提权（ShellExecuteW / runas）、防火墙（netsh / 规则名）统一在
``qq_live_digest/host_actions.py``，别处只能 import 调用。这里用文本扫描守住这条线
（不用 AST：简单、报错能直接指到行）。注释与 docstring 里提到名字不算重复实现。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCAN_DIRS = ("qq_live_digest", "packaging")
_CENTRAL = _ROOT / "qq_live_digest" / "host_actions.py"


def _iter_code_lines(path: Path):
    """(行号, 去掉注释与 docstring 后的代码) —— docstring 里提到名字不算实现。"""
    in_doc = False
    text = path.read_text(encoding="utf-8", errors="replace")
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw
        if in_doc:
            for token in ('"""', "'''"):
                if token in line:
                    line = line.split(token, 1)[1]  # docstring 在这一行结束
                    in_doc = False
                    break
            else:
                continue
        for token in ('"""', "'''"):
            line = re.sub(f'{token}.*?{token}', "", line)  # 单行 docstring 整段去掉
            if token in line:  # 没闭合 → 多行 docstring 从这里开始
                line = line.split(token, 1)[0]
                in_doc = True
        line = line.split("#", 1)[0]
        yield lineno, line


def _py_files():
    for name in _SCAN_DIRS:
        base = _ROOT / name
        if base.is_dir():
            yield from sorted(base.rglob("*.py"))


def _hits(pattern: str):
    """[(相对路径, 行号, 行内容)]，注释与 docstring 已排除。"""
    rx = re.compile(pattern)
    found = []
    for path in _py_files():
        for lineno, line in _iter_code_lines(path):
            if rx.search(line):
                found.append((path.relative_to(_ROOT).as_posix(), lineno, line.strip()))
    return found


def _outside_central(pattern: str):
    return [hit for hit in _hits(pattern) if hit[0] != "qq_live_digest/host_actions.py"]


def _format(kind: str, hits) -> str:
    return "\n".join(
        f"{where}:{lineno} → 这里又写了一份 {kind}，应该调 host_actions：{text}"
        for where, lineno, text in hits
    )


class NoDuplicatesTests(unittest.TestCase):
    def test_shell_execute_w_only_in_host_actions(self):
        hits = _outside_central(r"\bShellExecuteW\b")
        self.assertEqual([], hits, _format("ShellExecuteW 提权", hits))

    def test_netsh_literal_only_in_host_actions(self):
        hits = _outside_central(r"""["']netsh["']""")
        self.assertEqual([], hits, _format("netsh 命令", hits))

    def test_firewall_rule_name_defined_once(self):
        hits = _hits(r"^\s*def firewall_rule_name\b")
        self.assertEqual(
            ["qq_live_digest/host_actions.py"],
            [hit[0] for hit in hits],
            _format("防火墙规则名 firewall_rule_name", hits),
        )
        # 规则名模板只允许定义一次，别处拼字符串就是派生。
        template = _hits(r"RULE_NAME_TEMPLATE\s*=")
        self.assertEqual(
            ["qq_live_digest/host_actions.py"],
            [hit[0] for hit in template],
            _format("防火墙规则名模板 RULE_NAME_TEMPLATE", template),
        )

    def test_run_elevated_defined_once(self):
        hits = _hits(r"^\s*def run_elevated\b")
        self.assertEqual(
            ["qq_live_digest/host_actions.py"],
            [hit[0] for hit in hits],
            _format("提权入口 run_elevated", hits),
        )

    def test_scanner_actually_catches_duplicates(self):
        """负控：扫描器真的能看见重复，别让它空转变绿。"""
        hits = _hits(r"RULE_NAME_TEMPLATE\s*=")
        self.assertTrue(hits, "扫描器连 host_actions 里的模板都没找到，规则形同虚设")
        self.assertTrue(
            any("ShellExecuteW" in text for _, _, text in _hits(r"\bShellExecuteW\b")),
            "扫描器扫不到 host_actions 里的 ShellExecuteW，说明去注释逻辑把代码也删了",
        )


if __name__ == "__main__":
    unittest.main()
