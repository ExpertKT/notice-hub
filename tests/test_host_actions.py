"""host_actions 的唯一入口契约：命令与旧实现逐字一致、非 Windows 不抛、审计按行写 JSON。"""

from __future__ import annotations

import ctypes
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from qq_live_digest import host_actions as ha


def _netsh_output(*blocks: str) -> bytes:
    # utf-8 而不是 mbcs：中文 Windows 的 mbcs 是 cp936，但非 Windows 上根本没有 mbcs
    # codec（LookupError），英文 Windows 的 mbcs 又是 cp1252（中文变 '?'）。UTF-8 是
    # decode_console_output 的第一个候选，三平台都能原样解回来。
    return ("\r\n\r\n".join(blocks)).encode("utf-8")


class FirewallCommandTests(unittest.TestCase):
    def test_allow_command_matches_webapp_character_for_character(self):
        from qq_live_digest import webapp

        for port in (8080, 3001, 65535):
            self.assertEqual(
                ha.firewall_allow_command(port),
                webapp._firewall_rule_command(port),
                f"端口 {port} 的命令与 webapp 不一致",
            )

    def test_allow_command_matches_launcher_format(self):
        # launcher 里是 '...name="群务台 %d" dir=in action=allow protocol=TCP localport=%d' % (port, port)
        for port in (8080, 443):
            expected = (
                'netsh advfirewall firewall add rule name="群务台 %d" dir=in action=allow '
                "protocol=TCP localport=%d" % (port, port)
            )
            self.assertEqual(ha.firewall_allow_command(port), expected)

    def test_rule_name(self):
        self.assertEqual(ha.firewall_rule_name(8080), "群务台 8080")
        self.assertEqual(ha.firewall_rule_name("443"), "群务台 443")


class PlatformTests(unittest.TestCase):
    def test_is_admin_false_off_windows(self):
        with mock.patch.object(ha.sys, "platform", "linux"):
            self.assertFalse(ha.is_windows())
            self.assertFalse(ha.is_admin())

    def test_run_elevated_reports_unsupported_off_windows(self):
        with mock.patch.object(ha.sys, "platform", "linux"), tempfile.TemporaryDirectory() as tmp:
            state, message = ha.run_elevated(["netsh", "advfirewall", "x"], reason="测试", log_dir=tmp)
            self.assertEqual(state, "unsupported")
            self.assertIn("Windows", message)
            lines = (Path(tmp) / ha.AUDIT_FILE_NAME).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["action"], "run_elevated")
        self.assertEqual(record["result"], "unsupported")


class AuditTests(unittest.TestCase):
    def test_writes_one_json_line_per_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            ha.audit_host_action("firewall_allow", {"port": 8080, "by": "webapp"}, "已请求", log_dir=tmp)
            ha.audit_host_action("firewall_allow", {"port": 8080, "by": "webapp"}, "取消", log_dir=tmp)
            lines = (Path(tmp) / ha.AUDIT_FILE_NAME).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(sorted(first), ["action", "at", "detail", "result"])
        self.assertEqual(first["action"], "firewall_allow")
        self.assertEqual(first["detail"], {"port": 8080, "by": "webapp"})
        self.assertEqual(first["result"], "已请求")

    def test_audit_failure_only_warns(self):
        with self.assertLogs(ha.LOGGER, level="WARNING"):
            ha.audit_host_action("x", {}, "y", log_dir=Path("\0bad"))


class FirewallProbeTests(unittest.TestCase):
    def setUp(self):
        ha.reset_firewall_cache()

    def _completed(self, payload: bytes, code: int = 0):
        return mock.Mock(returncode=code, stdout=payload)

    def test_allows_matching_enabled_inbound_rule(self):
        payload = _netsh_output(
            "Rule Name: notice-hub 8080\r\nEnabled: Yes\r\n"
            "Direction: In\r\nAction: Allow\r\n"
            "LocalPort: 8080",
            "Rule Name: other\r\nEnabled: Yes\r\n"
            "Direction: In\r\nAction: Allow\r\n"
            "LocalPort: 9999",
        )
        with mock.patch.object(ha.sys, "platform", "win32"), \
                mock.patch.object(ha.subprocess, "run", return_value=self._completed(payload)):
            result = ha.firewall_allows(8080)
        self.assertTrue(result["checked"])
        self.assertTrue(result["allowed"])
        self.assertEqual(result["port"], 8080)
        self.assertEqual(result["command"], ha.firewall_allow_command(8080))

    def test_unreadable_rules_report_checked_false(self):
        with mock.patch.object(ha.sys, "platform", "win32"), \
                mock.patch.object(ha.subprocess, "run", return_value=self._completed(b"", code=1)):
            result = ha.firewall_allows(8080)
        self.assertFalse(result["checked"])
        self.assertFalse(result["allowed"])

    def test_off_windows_never_runs_netsh(self):
        with mock.patch.object(ha.sys, "platform", "linux"), \
                mock.patch.object(ha.subprocess, "run") as runner:
            result = ha.firewall_allows(8080)
        runner.assert_not_called()
        self.assertFalse(result["checked"])

    def test_falls_back_to_elevated_snapshot_when_netsh_needs_admin(self):
        """本机非提权跑 netsh show 返回 1：要退回提权探测写下的快照，而不是判「未放行」。"""
        payload = _netsh_output(
            "Rule Name: 群务台 8080\r\nEnabled: Yes\r\n"
            "Direction: In\r\nAction: Allow\r\n"
            "LocalPort: 8080",
        )
        with tempfile.TemporaryDirectory() as tmp:
            ha.firewall_snapshot_path(tmp).write_bytes(payload)
            with mock.patch.object(ha.sys, "platform", "win32"), \
                    mock.patch.object(ha.subprocess, "run", return_value=self._completed(b"", code=1)):
                result = ha.firewall_allows(8080, tmp)
        self.assertTrue(result["checked"])
        self.assertTrue(result["allowed"])
        self.assertEqual(result["rule"], "群务台 8080")

    def test_snapshot_needs_a_data_dir_and_never_lies(self):
        with mock.patch.object(ha.sys, "platform", "win32"), \
                mock.patch.object(ha.subprocess, "run", return_value=self._completed(b"", code=1)):
            result = ha.firewall_allows(8080)
        self.assertFalse(result["checked"])
        self.assertFalse(result["allowed"])

    def test_snapshot_missing_or_garbage_reports_checked_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            ha.firewall_snapshot_path(tmp).write_bytes(b"\x00\xff garbage")
            with mock.patch.object(ha.sys, "platform", "win32"), \
                    mock.patch.object(ha.subprocess, "run", return_value=self._completed(b"", code=1)):
                result = ha.firewall_allows(8080, tmp)
        self.assertFalse(result["checked"])

    def test_allow_and_probe_command_is_one_elevation(self):
        """加规则 + 导出规则清单必须在同一条提权命令里，否则会弹两次 UAC。"""
        command = ha.firewall_allow_and_probe_command(8080, r"C:\data")
        self.assertTrue(command.startswith("cmd.exe /c " + ha.firewall_allow_command(8080)))
        self.assertIn("netsh advfirewall firewall show rule name=all dir=in", command)
        self.assertIn(str(ha.firewall_snapshot_path(r"C:\data")), command)


class ElevationTests(unittest.TestCase):
    def _fake_shell(self, code: int):
        fake = mock.Mock()
        fake.shell32.ShellExecuteW.return_value = code
        return fake

    def test_ok_state_only_means_requested(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ha.sys, "platform", "win32"), \
                mock.patch.object(ctypes, "windll", self._fake_shell(42), create=True):
            state, message = ha.run_elevated(
                ["netsh", "advfirewall", "firewall", "add", "rule"],
                reason="放行 8080",
                log_dir=tmp,
            )
            record = json.loads((Path(tmp) / ha.AUDIT_FILE_NAME).read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(state, "ok")
        self.assertIn("管理员权限", message)
        self.assertIn("netsh", record["detail"]["command"])
        self.assertEqual(record["detail"]["reason"], "放行 8080")
        self.assertEqual(record["result"], "ok")

    def test_cancelled_state_for_uac_refusal(self):
        for code in (1223, 5):
            with mock.patch.object(ha.sys, "platform", "win32"), \
                    mock.patch.object(ctypes, "windll", self._fake_shell(code), create=True):
                state, message = ha.run_elevated(["netsh"], reason="放行")
            self.assertEqual(state, "cancelled", f"返回码 {code} 应是取消")
            self.assertIn("取消", message)
            self.assertIn("再点一次", message)

    def test_failed_state_for_other_launch_errors(self):
        with mock.patch.object(ha.sys, "platform", "win32"), \
                mock.patch.object(ctypes, "windll", self._fake_shell(2), create=True):
            state, message = ha.run_elevated(["netsh"], reason="放行")
        self.assertEqual(state, "failed")
        self.assertIn("返回码 2", message)

    def test_accepts_full_command_string(self):
        """launcher 原来传的是整条 netsh 命令：exe 与参数要原样传，不重新拼引号。"""
        shell = self._fake_shell(42)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ha.sys, "platform", "win32"), \
                mock.patch.object(ctypes, "windll", shell, create=True):
            state, _message = ha.run_elevated(
                ha.firewall_allow_command(8080),
                reason="放行 8080",
                log_dir=tmp,
            )
        self.assertEqual(state, "ok")
        call = shell.shell32.ShellExecuteW.call_args[0]
        self.assertEqual(call[2], "netsh")
        self.assertTrue(call[3].startswith("advfirewall firewall add rule name="))
        self.assertIn("localport=8080", call[3])
        self.assertIn('name="群务台 8080"', call[3])


if __name__ == "__main__":
    unittest.main()
