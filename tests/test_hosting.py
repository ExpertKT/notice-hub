import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qq_live_digest import hosting as ho


class FakeSettings:
    def __init__(self, root: Path) -> None:
        self.data_dir = root
        self.env_file = root / ".env"
        self.hosting_quit_qq = True
        self.hosting_restore_qq = True
        self.hosting_auto_on_start = False
        self.autostart = False


class _FakeKey:
    def __enter__(self) -> "_FakeKey":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 2
    KEY_READ = 1
    REG_SZ = 1

    def __init__(self, values: dict | None = None) -> None:
        self.values = dict(values or {})

    def OpenKey(self, hive: object, path: str, reserved: int = 0, access: int = 0) -> _FakeKey:
        return _FakeKey()

    def SetValueEx(self, key: object, name: str, reserved: int, kind: int, value: str) -> None:
        self.values[name] = value

    def DeleteValue(self, key: object, name: str) -> None:
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]

    def QueryValueEx(self, key: object, name: str) -> tuple[str, int]:
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ


class HostingTests(unittest.TestCase):
    def test_exec_from_command_handles_quotes(self):
        self.assertEqual(ho._exec_from_command('"D:\\QQ\\Uninstall.exe"'), "D:\\QQ\\Uninstall.exe")
        self.assertEqual(ho._exec_from_command("D:\\QQ\\Uninstall.exe"), "D:\\QQ\\Uninstall.exe")
        self.assertEqual(ho._exec_from_command("D:\\QQ\\Uninstall.exe /S"), "D:\\QQ\\Uninstall.exe")
        self.assertEqual(ho._exec_from_command(""), "")

    def test_user_qq_path_rejects_napcat_shell(self):
        with tempfile.TemporaryDirectory() as td:
            shell = Path(td) / "NapCat.52230.Shell" / "QQ.exe"
            shell.parent.mkdir(parents=True)
            shell.write_bytes(b"x")
            with patch.object(ho, "_qq_exe_candidates", return_value=[shell]):
                self.assertIsNone(ho.user_qq_path())

    def test_user_qq_path_accepts_registered_qq(self):
        with tempfile.TemporaryDirectory() as td:
            real = Path(td) / "QQ" / "QQ.exe"
            real.parent.mkdir(parents=True)
            real.write_bytes(b"x")
            with patch.object(ho, "_qq_exe_candidates", return_value=[real]):
                self.assertEqual(ho.user_qq_path(), real)

    def test_user_qq_pids_requires_exact_path(self):
        with tempfile.TemporaryDirectory() as td:
            real = Path(td) / "QQ" / "QQ.exe"
            real.parent.mkdir(parents=True)
            real.write_bytes(b"x")
            processes = [
                {"ProcessId": 11, "ExecutablePath": str(real)},
                {"ProcessId": 12, "ExecutablePath": str(Path(td) / "onekey" / "QQ.exe")},
                {"ProcessId": 13, "ExecutablePath": None},
            ]
            with patch.object(ho, "user_qq_path", return_value=real), patch.object(ho, "_processes", return_value=processes):
                self.assertEqual(ho.user_qq_pids(), [11])

    def test_quit_user_qq_is_noop_when_not_running(self):
        with patch.object(ho, "user_qq_pids", return_value=[]), patch.object(ho, "_taskkill") as kill:
            result = ho.quit_user_qq()
        self.assertTrue(result["ok"])
        self.assertTrue(result["already_closed"])
        kill.assert_not_called()

    def test_quit_user_qq_is_graceful_first(self):
        with patch.object(ho, "user_qq_pids", side_effect=[[7], []]), patch.object(ho, "_taskkill") as kill, patch.object(
            ho.time, "sleep"
        ):
            result = ho.quit_user_qq(timeout=5.0)
        self.assertTrue(result["ok"])
        self.assertFalse(result["forced"])
        kill.assert_called_once_with(7, force=False)

    def test_quit_user_qq_forces_when_stubborn(self):
        with patch.object(ho, "user_qq_pids", return_value=[7]), patch.object(ho, "_taskkill") as kill, patch.object(
            ho.time, "sleep"
        ):
            result = ho.quit_user_qq(timeout=0.0)
        self.assertFalse(result["ok"])
        self.assertTrue(result["forced"])
        self.assertEqual(result["remaining"], [7])
        self.assertTrue(result["error"])
        self.assertEqual([call.kwargs.get("force") for call in kill.call_args_list], [False, True])

    def test_start_user_qq_skips_when_already_running(self):
        with patch.object(ho, "user_qq_path", return_value=Path("D:/QQ/QQ.exe")), patch.object(
            ho, "user_qq_pids", return_value=[7]
        ), patch.object(ho.subprocess, "Popen") as popen:
            result = ho.start_user_qq()
        self.assertTrue(result["ok"])
        self.assertTrue(result["already_running"])
        popen.assert_not_called()

    def test_start_user_qq_launches_detached(self):
        with tempfile.TemporaryDirectory() as td:
            real = Path(td) / "QQ.exe"
            real.write_bytes(b"x")
            with patch.object(ho, "user_qq_path", return_value=real), patch.object(
                ho, "user_qq_pids", return_value=[]
            ), patch.object(ho.subprocess, "Popen") as popen:
                popen.return_value.pid = 4242
                result = ho.start_user_qq()
        self.assertTrue(result["ok"])
        self.assertEqual(result["pid"], 4242)
        self.assertEqual(popen.call_args.args[0], [str(real)])

    def test_set_autostart_writes_and_removes_value(self):
        fake = FakeWinreg()
        with patch.object(ho, "_winreg", return_value=fake):
            written = ho.set_autostart(True, target=Path("C:/apps/QQ-Notice-Hub.exe"))
            self.assertTrue(written["ok"])
            self.assertEqual(fake.values[ho.AUTOSTART_NAME], '"C:\\apps\\QQ-Notice-Hub.exe"')
            self.assertTrue(ho.autostart_enabled())
            removed = ho.set_autostart(False)
            self.assertTrue(removed["ok"])
            self.assertNotIn(ho.AUTOSTART_NAME, fake.values)
            self.assertFalse(ho.autostart_enabled())

    def test_save_prefs_only_rewrites_its_own_lines(self):
        with tempfile.TemporaryDirectory() as td:
            settings = FakeSettings(Path(td))
            original = (
                "# 注释要原样保留\r\n"
                "QQ_DIGEST_WEB=1\r\n"
                "QQ_DIGEST_HOSTING_QUIT_QQ=1\r\n"
                "QQ_DIGEST_AUTOSTART=0\r\n"
                "QQ_DIGEST_WEB_TOKEN=tok"
            )
            settings.env_file.write_bytes(original.encode("utf-8"))
            with patch.object(ho, "set_autostart") as autostart:
                result = ho.save_prefs(settings, {"quit_qq": False, "autostart": True, "auto_on_start": True})
            self.assertTrue(result["ok"])
            self.assertFalse(settings.hosting_quit_qq)
            self.assertTrue(settings.autostart)
            autostart.assert_called_once_with(True)
            text = settings.env_file.read_bytes().decode("utf-8")
            self.assertIn("# 注释要原样保留", text)
            self.assertIn("QQ_DIGEST_HOSTING_QUIT_QQ=0", text)
            self.assertIn("QQ_DIGEST_AUTOSTART=1", text)
            self.assertIn("QQ_DIGEST_WEB_TOKEN=tok\r\n", text)  # 原有尾行（无换行）保持完整
            self.assertTrue(text.endswith("QQ_DIGEST_HOSTING_AUTO_ON_START=1\r\n"))  # 缺失键被追加到末尾
            self.assertIn("QQ_DIGEST_WEB=1", text)

    def test_save_prefs_rejects_unknown_keys(self):
        with tempfile.TemporaryDirectory() as td:
            settings = FakeSettings(Path(td))
            settings.env_file.write_text("A=1\n", encoding="utf-8")
            result = ho.save_prefs(settings, {"nope": True})
        self.assertFalse(result["ok"])

    def test_hosting_status_uses_state_file(self):
        with tempfile.TemporaryDirectory() as td:
            settings = FakeSettings(Path(td))
            ho._write_state(settings, {"active": True, "uin": "123", "since": "2026-10-08T00:00:00"})
            with patch.object(ho, "user_qq_path", return_value=Path("D:/QQ/QQ.exe")), patch.object(
                ho, "user_qq_pids", return_value=[7]
            ), patch.object(ho, "_port_open", side_effect=[True, False]):
                status = ho.hosting_status(settings)
        self.assertTrue(status["hosting_active"])
        self.assertEqual(status["hosting_uin"], "123")
        self.assertTrue(status["user_qq_running"])
        self.assertTrue(status["napcat_running"])
        self.assertFalse(status["napcat_online"])

    def test_start_normalises_empty_uin_to_none(self):
        """回归：uin 为空时绝不能把字符串 "None" 当成 QQ 号传给 NapCat。

        传了 -q 就等于「快速登录某个号」，传 "None" 会变成一次必然失败的快速登录，
        而空 uin 本来应该是扫码登录。
        """
        captured = {}

        def fake_launch(settings, *, uin=None, profile_dir=None):
            captured["uin"] = uin
            return {
                "ok": True,
                "already_running": False,
                "pid": 1,
                "command": [],
                "profile_dir": "",
                "boot": {},
                "error": None,
            }

        module = type("M", (), {"launch": staticmethod(fake_launch)})
        for given, expected in ((None, None), ("", None), ("   ", None), (" 12345 ", "12345")):
            with tempfile.TemporaryDirectory() as td:
                settings = FakeSettings(Path(td))
                settings.hosting_quit_qq = False
                with patch.object(ho, "_napcat_admin", return_value=module):
                    result = ho.start(settings, uin=given)
                self.assertTrue(result["ok"])
                self.assertEqual(captured["uin"], expected, "uin=%r 时传错了" % (given,))
                self.assertEqual(ho._read_state(settings).get("uin"), expected, "uin=%r 时状态写错了" % (given,))

    def test_start_reports_when_napcat_module_missing(self):
        with tempfile.TemporaryDirectory() as td:
            settings = FakeSettings(Path(td))
            settings.hosting_quit_qq = False
            with patch.object(ho, "_napcat_admin", return_value=None):
                result = ho.start(settings)
        self.assertFalse(result["ok"])
        self.assertIn("NapCat", str(result["error"]))
        self.assertEqual([step["step"] for step in result["steps"]], ["start-napcat"])

    def test_stop_stops_napcat_and_restores_qq(self):
        with tempfile.TemporaryDirectory() as td:
            settings = FakeSettings(Path(td))
            module = type("M", (), {"stop": staticmethod(lambda s: {"ok": True, "stopped": [1], "already_stopped": False, "error": None})})
            with patch.object(ho, "_napcat_admin", return_value=module), patch.object(
                ho, "start_user_qq", return_value={"ok": True, "pid": 9, "already_running": False, "error": None}
            ) as restore:
                result = ho.stop(settings)
            self.assertTrue(result["ok"])
            restore.assert_called_once_with()
            self.assertFalse(ho._read_state(settings).get("active", True))


if __name__ == "__main__":
    unittest.main()
