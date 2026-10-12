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
GBK_POWERSHELL = "C:\\程序文件\\NapCat\\NapCatWinBootMain.exe\r\n1234\r\n".encode("gbk")

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
        # Keep the fixture deterministic on runners whose locale is cp1252/UTF-8:
        # the production decoder must still reach its explicit cp936 fallback.
        with patch("qq_live_digest.win_text.locale.getpreferredencoding", return_value="utf-8"), patch(
            "qq_live_digest.win_text.sys.getfilesystemencoding", return_value="utf-8"
        ):
            decoded = decode_console_output(GBK_POWERSHELL)
        self.assertIn("程序文件", decoded)
        self.assertNotIn("�", decoded)

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
    def test_kill_matching_parses_gbk_powershell_output(self):
        killed = []
        with patch.object(subprocess, "check_output", return_value=GBK_POWERSHELL), patch.object(
            subprocess, "run", lambda *a, **k: killed.append(a)
        ):
            pids = na._kill_matching(r"C:\程序文件\NapCat")
        self.assertEqual(pids, [1234])
        self.assertEqual(len(killed), 1)


class HostingProcessTests(unittest.TestCase):
    def test_processes_parses_gbk_json(self):
        class Completed:
            stdout = GBK_HOSTING_JSON
            stderr = b""

        with patch.object(subprocess, "run", return_value=Completed()):
            rows = hosting._processes()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ProcessId"], 4242)


if __name__ == "__main__":
    unittest.main()
