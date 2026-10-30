"""B-9 regression: Windows console output must never raise UnicodeDecodeError.

On a Chinese Windows, `netstat` / `powershell.exe` write GBK (cp936). Calling them
with `subprocess(..., text=True)` and no explicit encoding makes Python decode with
whatever the current encoding is, so the moment UTF-8 mode is on (`PYTHONUTF8=1`,
`-X utf8`) the same call raises:

    UnicodeDecodeError: 'utf-8' codec can't decode byte 0xbb in position 2

and the user sees `NapCat 安装失败：'utf-8' codec can't decode byte ...`.

These tests deliberately do NOT depend on the ambient PYTHONUTF8 setting: they feed
real GBK bytes through the patched call sites and assert we get text back.
"""

from __future__ import annotations

import os
import subprocess
import unittest
from unittest.mock import patch

from qq_live_digest import napcat_admin as na
from qq_live_digest import hosting
from qq_live_digest.win_text import decode_console_output

# A netstat line whose state/host columns carry Chinese-locale bytes (cp936).
GBK_NETSTAT = (
    "  TCP    0.0.0.0:6099    0.0.0.0:0    LISTENING    10\n"
    "  TCP    [::]:6099       [::]:0       LISTENING    10\n"
).encode("gbk")

# PowerShell echoing a Chinese install path back, as _kill_matching's query does.
GBK_POWERSHELL = b"C:\\" + "程序文件".encode("gbk") + b"\\NapCat\\NapCatWinBootMain.exe\r\n1234\r\n"

# _matching_pids 现在先问 tasklist「启动器在不在」，在跑才去拉命令行全表（CIM JSON）。
# 这份 JSON 里带中文安装目录，按 GBK 输出：解析走 decode_console_output，正是原来的覆盖点。
GBK_CIM_JSON = (
    '[{"Name":"NapCatWinBootMain.exe","ProcessId":1234,'
    '"CommandLine":"C:\\\\程序文件\\\\NapCat\\\\NapCatWinBootMain.exe --path C:\\\\程序文件\\\\NapCat"},'
    '{"Name":"NapCatWinBootMain.exe","ProcessId":5678,'
    '"CommandLine":"C:\\\\别的目录\\\\NapCat\\\\NapCatWinBootMain.exe"}]'
).encode("gbk")

# hosting._processes parses ConvertTo-Json output holding a Chinese QQ.exe path.
GBK_HOSTING_JSON = (
    '[{"ProcessId":4242,"ExecutablePath":"D:\\\\QQ\\\\QQ.exe","ParentProcessId":1}]'
).encode("gbk")


class DecodeConsoleOutputTests(unittest.TestCase):
    def test_decodes_gbk_bytes(self):
        self.assertIn("6099", decode_console_output(GBK_NETSTAT.decode("gbk").encode("gbk")))

    def test_prefers_utf8_when_valid(self):
        self.assertEqual(decode_console_output("café".encode("utf-8")), "café")

    def test_keeps_chinese_characters_intact(self):
        # Must not be errors="ignore"/"replace": the Chinese path has to survive.
        # 这里模拟"中文 Windows"：ambient 首选编码报 cp936，于是候选表是
        # ["utf-8", "cp936", …] —— UTF-8 解不开这些 GBK 字节后就会走到 cp936，和真机一致。
        # 不能只把 ambient patch 成 "utf-8"：那样候选表里下一个能解的会是硬编码的 "mbcs"
        # （英文 runner 上是 cp1252，任何单字节都能"成功"解码），GBK 永远轮不到，
        # 断言在英文机器上必然红 —— CI 上就是这么红的。
        with patch("qq_live_digest.win_text.locale.getpreferredencoding", return_value="cp936"), patch(
            "qq_live_digest.win_text.sys.getfilesystemencoding", return_value="cp936"
        ):
            decoded = decode_console_output(GBK_POWERSHELL)
        self.assertIn("程序文件", decoded)
        self.assertNotIn("\ufffd", decoded)

    def test_never_raises_on_arbitrary_bytes(self):
        # Mixed / invalid bytes: we still get a string back instead of an exception.
        self.assertIsInstance(decode_console_output(b"\xff\xfe\x00\x81\xbb\xb4"), str)

    def test_none_and_str_pass_through(self):
        self.assertEqual(decode_console_output(None), "")
        self.assertEqual(decode_console_output("already text"), "already text")


class PortsSubprocessTests(unittest.TestCase):
    """na._ports() must survive GBK netstat output even with UTF-8 mode on."""

    def test_ports_parses_gbk_netstat_output(self):
        with patch.object(subprocess, "check_output", return_value=GBK_NETSTAT):
            self.assertIn(6099, na._ports())

    def test_ports_does_not_request_text_decoding(self):
        # Guard against someone re-adding text=True, which reintroduces the bug.
        seen = {}

        def fake_check_output(cmd, **kwargs):
            seen.update(kwargs)
            return GBK_NETSTAT

        with patch.object(subprocess, "check_output", fake_check_output):
            na._ports()
        self.assertNotIn("text", seen)
        self.assertNotIn("encoding", seen)


class KillMatchingTests(unittest.TestCase):
    def test_kill_matching_parses_gbk_command_line_and_kills_only_match(self):
        """按命令行里的中文目录匹配：GBK 要能解析，只杀命中的，不误杀。"""
        killed = []
        na.clear_probe_cache()  # 进程表有 TTL 缓存，用例之间要隔离
        self.addCleanup(na.clear_probe_cache)
        rows = [
            {"name": "NapCatWinBootMain.exe", "pid": "1234"},
            {"name": "NapCatWinBootMain.exe", "pid": "5678"},
        ]
        with patch.object(na, "_tasklist_rows", return_value=rows), patch.object(
            subprocess, "check_output", return_value=GBK_CIM_JSON
        ), patch.object(subprocess, "run", lambda *a, **k: killed.append(a)):
            pids = na._kill_matching(r"C:\程序文件\NapCat")
        self.assertEqual(pids, [1234])  # 只有命令行含这个中文目录的那个
        self.assertEqual(len(killed), 1)  # 只杀一次：5678 在别的目录，不许误杀
        self.assertIn("1234", killed[0][0])  # 传下去的是匹配到的 pid，不是别的


class HostingProcessTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows process query")
    def test_processes_parses_gbk_json(self):
        class Completed:
            stdout = GBK_HOSTING_JSON
            stderr = b""

        saved = hosting._process_cache
        hosting._process_cache = None
        self.addCleanup(setattr, hosting, "_process_cache", saved)
        # force=True：这条测的是解析，不是缓存（缓存另有 tests/test_hosting.py 的进程缓存用例）。
        with patch.object(subprocess, "run", return_value=Completed()):
            rows = hosting._processes(force=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ProcessId"], 4242)


if __name__ == "__main__":
    unittest.main()
