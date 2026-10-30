import json
import os
import subprocess
import tempfile
import threading
import time
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
        self.quit_restore_qq = False
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

    def test_user_qq_pids_excludes_the_napcat_engine(self):
        """NapCat 的 QQ 和用户的日常 QQ 是同一个 QQ.exe，只有命令行能分开。

        这里算错就等于 quit_user_qq 把正在跑的 NapCat 一起杀掉，用户当场掉线。
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            real = root / "QQ" / "QQ.exe"
            real.parent.mkdir(parents=True)
            real.write_bytes(b"x")
            settings = FakeSettings(root)
            profile = (root / "napcat-profile").resolve()
            processes = [
                {"ProcessId": 11, "ExecutablePath": str(real), "CommandLine": '"%s"' % real},
                {
                    "ProcessId": 12,
                    "ExecutablePath": str(real),
                    "CommandLine": '"%s" --user-data-dir=%s' % (real, profile),
                },
                {"ProcessId": 13, "ExecutablePath": str(real), "CommandLine": None},
            ]
            with patch.object(ho, "user_qq_path", return_value=real), patch.object(ho, "_processes", return_value=processes):
                self.assertEqual(ho.user_qq_pids(settings), [11, 13])

    def test_user_qq_pids_still_excludes_napcat_without_settings(self):
        """拿不到 profile 目录时也要靠 --user-data-dir + 目录名认出来，宁可漏算不能误杀。"""
        with tempfile.TemporaryDirectory() as td:
            real = Path(td) / "QQ" / "QQ.exe"
            real.parent.mkdir(parents=True)
            real.write_bytes(b"x")
            processes = [
                {"ProcessId": 11, "ExecutablePath": str(real), "CommandLine": '"%s"' % real},
                {
                    "ProcessId": 12,
                    "ExecutablePath": str(real),
                    "CommandLine": '"%s" --user-data-dir=F:\\notice-hub\\data\\napcat-profile' % real,
                },
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

    @unittest.skipUnless(os.name == "nt", "Windows registry autostart")
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

    def test_quit_restore_qq_defaults_to_off(self):
        """用户 2026-10-12：托盘右键退出会自动开 QQ，希望它是设置项且默认关闭。"""
        with tempfile.TemporaryDirectory() as td:
            settings = FakeSettings(Path(td))
        self.assertFalse(settings.quit_restore_qq)
        self.assertFalse(ho.restore_qq_on_quit(settings))

    def test_prefs_reports_quit_restore_qq(self):
        with tempfile.TemporaryDirectory() as td:
            settings = FakeSettings(Path(td))
            self.assertFalse(ho.prefs(settings)["quit_restore_qq"])
            settings.quit_restore_qq = True
            self.assertTrue(ho.prefs(settings)["quit_restore_qq"])
            self.assertTrue(ho.restore_qq_on_quit(settings))

    def test_save_prefs_writes_quit_restore_qq(self):
        with tempfile.TemporaryDirectory() as td:
            settings = FakeSettings(Path(td))
            settings.env_file.write_text("QQ_DIGEST_WEB=1\n", encoding="utf-8")
            with patch.object(ho, "set_autostart"):
                result = ho.save_prefs(settings, {"quit_restore_qq": True})
            self.assertTrue(result["ok"])
            self.assertTrue(settings.quit_restore_qq)
            self.assertIn("QQ_DIGEST_QUIT_RESTORE_QQ=1", settings.env_file.read_text(encoding="utf-8"))
            with patch.object(ho, "set_autostart"):
                result = ho.save_prefs(settings, {"quit_restore_qq": False})
            self.assertTrue(result["ok"])
            self.assertFalse(settings.quit_restore_qq)
            self.assertIn("QQ_DIGEST_QUIT_RESTORE_QQ=0", settings.env_file.read_text(encoding="utf-8"))

    def test_hosting_status_uses_state_file(self):
        with tempfile.TemporaryDirectory() as td:
            settings = FakeSettings(Path(td))
            ho._write_state(settings, {"active": True, "uin": "123", "since": "2026-10-08T00:00:00"})
            with patch.object(ho, "user_qq_path", return_value=Path("D:/QQ/QQ.exe")), patch.object(
                ho, "user_qq_pids", return_value=[7]
            ), patch.object(ho, "_port_open", return_value=False):
                status = ho.hosting_status(settings)
        self.assertTrue(status["hosting_active"])
        self.assertEqual(status["hosting_uin"], "123")
        self.assertTrue(status["user_qq_running"])
        self.assertFalse(status["napcat_running"])
        self.assertFalse(status["napcat_online"])
        self.assertTrue(status["napcat_login_error"])

    def test_hosting_status_ignores_qq_webui_port(self):
        """6099 是日常 QQ 的 WebUI 端口：只开 6099 不算 NapCat 在运行。

        端口从 settings.napcat_api_url 推，默认 3001；这里让探测只对 6099 应答。
        """
        with tempfile.TemporaryDirectory() as td:
            settings = FakeSettings(Path(td))
            with patch.object(ho, "user_qq_path", return_value=Path("D:/QQ/QQ.exe")), patch.object(
                ho, "user_qq_pids", return_value=[7]
            ), patch.object(ho, "_port_open", side_effect=lambda port, **kw: port == 6099):
                status = ho.hosting_status(settings)
        self.assertTrue(status["user_qq_running"])
        self.assertFalse(status["napcat_running"])
        self.assertFalse(status["napcat_online"])

    def test_hosting_status_port_follows_the_configured_api_url(self):
        with tempfile.TemporaryDirectory() as td:
            settings = FakeSettings(Path(td))
            settings.napcat_api_url = "http://127.0.0.1:31001"
            seen = []

            def fake_port(port, **kw):
                seen.append(port)
                return False

            with patch.object(ho, "user_qq_path", return_value=Path("D:/QQ/QQ.exe")), patch.object(
                ho, "user_qq_pids", return_value=[]
            ), patch.object(ho, "_port_open", side_effect=fake_port):
                ho.hosting_status(settings)
        self.assertEqual(seen, [31001])

    def test_hosting_status_separates_running_from_logged_in(self):
        """引擎起来了但没登录，两个字段必须能同时表达出来。

        以前两个字段都探同一个端口，永远同真同假，「引擎已启动，未登录」不可达。
        """
        with tempfile.TemporaryDirectory() as td:
            settings = FakeSettings(Path(td))
            with patch.object(ho, "user_qq_path", return_value=None), patch.object(
                ho, "user_qq_pids", return_value=[]
            ), patch.object(ho, "_port_open", return_value=True), patch.object(
                ho, "_napcat_login_state", return_value=(False, "还没扫码")
            ):
                status = ho.hosting_status(settings)
        self.assertTrue(status["napcat_running"])
        self.assertFalse(status["napcat_online"])
        self.assertEqual(status["napcat_login_error"], "还没扫码")

    def test_hosting_status_reports_online_when_logged_in(self):
        with tempfile.TemporaryDirectory() as td:
            settings = FakeSettings(Path(td))
            with patch.object(ho, "user_qq_path", return_value=None), patch.object(
                ho, "user_qq_pids", return_value=[]
            ), patch.object(ho, "_port_open", return_value=True), patch.object(
                ho, "_napcat_login_state", return_value=(True, "")
            ):
                status = ho.hosting_status(settings)
        self.assertTrue(status["napcat_running"])
        self.assertTrue(status["napcat_online"])
        self.assertEqual(status["napcat_login_error"], "")

    def test_napcat_login_state_is_false_when_unreachable(self):
        """问不到登录态就说未登录，绝不假装在线。"""
        with tempfile.TemporaryDirectory() as td:
            settings = FakeSettings(Path(td))
            settings.napcat_api_url = "http://127.0.0.1:3001"
            with patch.object(ho, "_napcat_call", side_effect=OSError("refused")):
                online, error = ho._napcat_login_state(settings, 3001)
        self.assertFalse(online)
        self.assertTrue(error)

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
                with patch.object(ho, "_napcat_admin", return_value=module), patch.object(
                    ho, "_napcat_running", return_value=False
                ):
                    result = ho.start(settings, uin=given)
                self.assertTrue(result["ok"])
                self.assertEqual(captured["uin"], expected, "uin=%r 时传错了" % (given,))
                self.assertEqual(ho._read_state(settings).get("uin"), expected, "uin=%r 时状态写错了" % (given,))

    def test_start_reports_when_napcat_module_missing(self):
        with tempfile.TemporaryDirectory() as td:
            settings = FakeSettings(Path(td))
            settings.hosting_quit_qq = False
            with patch.object(ho, "_napcat_admin", return_value=None), patch.object(
                ho, "_napcat_running", return_value=False
            ):
                result = ho.start(settings)
        self.assertFalse(result["ok"])
        self.assertIn("NapCat", str(result["error"]))
        self.assertEqual([step["step"] for step in result["steps"]], ["start-napcat"])

    def test_start_skips_launch_when_napcat_already_running(self):
        """连点两次「开始托管」只该起一个 NapCat：已在跑就返回当前状态，不再 Popen、不退出 QQ。"""
        with tempfile.TemporaryDirectory() as td:
            settings = FakeSettings(Path(td))
            settings.hosting_quit_qq = True
            with patch.object(ho, "_napcat_running", return_value=True), patch.object(
                ho, "_napcat_admin"
            ) as admin, patch.object(ho, "quit_user_qq") as quit_qq:
                result = ho.start(settings, uin="12345")
                state = ho._read_state(settings)

        self.assertTrue(result["ok"])
        self.assertTrue(result["already_running"])
        self.assertEqual([step["step"] for step in result["steps"]], ["already-running"])
        quit_qq.assert_not_called()
        admin.assert_not_called()  # _napcat_running 已判定在跑，不该再摸管理模块
        self.assertTrue(state.get("active"))
        self.assertEqual(state.get("uin"), "12345")

    def test_concurrent_start_launches_only_once(self):
        """两个线程同时点「开始托管」：锁保证后进来的那个看到已在跑，只 Popen 一次。"""
        launched = []

        def fake_launch(settings, *, uin=None, profile_dir=None):
            launched.append(uin)
            time.sleep(0.05)
            running["value"] = True
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
        running = {"value": False}
        with tempfile.TemporaryDirectory() as td:
            settings = FakeSettings(Path(td))
            settings.hosting_quit_qq = False
            results: list[dict] = []
            with patch.object(ho, "_napcat_running", side_effect=lambda s: running["value"]), patch.object(
                ho, "_napcat_admin", return_value=module
            ):
                threads = [threading.Thread(target=lambda: results.append(ho.start(settings))) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)

        self.assertEqual(len(launched), 1, "只该启动一次 NapCat，实际 %d 次" % len(launched))
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result["ok"] for result in results))

    def test_stop_is_idempotent_when_nothing_left_to_do(self):
        """再点一次「结束托管」不该报错，也不该重复启动 QQ。"""
        module = type(
            "M",
            (),
            {"stop": staticmethod(lambda s: {"ok": True, "stopped": [], "already_stopped": True, "error": None})},
        )
        with tempfile.TemporaryDirectory() as td:
            settings = FakeSettings(Path(td))
            with patch.object(ho, "_napcat_admin", return_value=module), patch.object(
                ho, "start_user_qq", return_value={"ok": True, "pid": 0, "already_running": True, "error": None}
            ) as restore:
                first = ho.stop(settings)
                second = ho.stop(settings)
                state = ho._read_state(settings)

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(restore.call_count, 2)  # 只是问了两次，真正的重复启动由 start_user_qq 挡住
        self.assertFalse(state.get("active", True))

    def test_stop_stops_napcat_and_restores_qq(self):
        with tempfile.TemporaryDirectory() as td:
            settings = FakeSettings(Path(td))
            module = type("M", (), {"stop": staticmethod(lambda s: {"ok": True, "stopped": [1], "already_stopped": False, "error": None})})
            with patch.object(ho, "_napcat_admin", return_value=module), patch.object(
                ho, "_napcat_running", return_value=True
            ), patch.object(
                ho, "start_user_qq", return_value={"ok": True, "pid": 9, "already_running": False, "error": None}
            ) as restore:
                result = ho.stop(settings)
            self.assertTrue(result["ok"])
            # 「点结束托管」这条路径必须保留 CIM 复查：退出场景才跳过（verify=False）。
            restore.assert_called_once_with(settings, verify=True)
            self.assertFalse(ho._read_state(settings).get("active", True))

    def test_write_state_is_atomic_and_warns_when_blocked(self):
        """hosting.json 用临时文件 + os.replace；被占用时不能静默吞掉，要记 warning。"""
        with tempfile.TemporaryDirectory() as td:
            settings = FakeSettings(Path(td))
            ho._write_state(settings, {"active": True, "uin": "1", "since": "now"})
            path = ho._state_path(settings)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["uin"], "1")
            leftovers = [item.name for item in path.parent.iterdir() if item.name.endswith(".tmp")]
            self.assertEqual(leftovers, [], "临时文件没清干净：%r" % (leftovers,))

            with patch.object(ho.os, "replace", side_effect=PermissionError("occupied")), self.assertLogs(
                ho.LOGGER, level="WARNING"
            ) as logs:
                ho._write_state(settings, {"active": False})
            self.assertTrue(any("托管状态写入失败" in line for line in logs.output), logs.output)
            # 写失败时旧内容必须还在，不能写坏
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["uin"], "1")


@unittest.skipUnless(os.name == "nt", "Windows process enumeration")
class ProcessQueryCacheTests(unittest.TestCase):
    """进程名单的短 TTL 缓存：同一轮里的重复查询不重跑 CIM，要动手的路径必须现查。"""

    def setUp(self) -> None:
        self._saved = ho._process_cache
        ho._process_cache = None
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        ho._process_cache = self._saved

    @staticmethod
    def _completed(payload: str):
        class Completed:
            stdout = payload.encode("gbk")
            stderr = b""

        return Completed()

    def _patch_run(self, payload: str):
        return patch.object(subprocess, "run", return_value=self._completed(payload))

    def test_second_query_within_ttl_reuses_the_snapshot(self):
        payload = json.dumps([{"ProcessId": 7, "ExecutablePath": "C:\\QQ\\QQ.exe"}])
        with self._patch_run(payload) as run:
            first = ho._processes()
            second = ho._processes()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(first, second)
        self.assertEqual(first[0]["ProcessId"], 7)

    def test_expired_snapshot_queries_again(self):
        with self._patch_run(json.dumps([{"ProcessId": 7}])) as run:
            ho._processes()
            ho._process_cache = (time.monotonic() - ho.PROCESS_CACHE_TTL - 0.1, ho._process_cache[1])
            ho._processes()
        self.assertEqual(run.call_count, 2)

    def test_force_bypasses_the_cache(self):
        with self._patch_run(json.dumps([{"ProcessId": 7}])) as run:
            ho._processes()
            ho._processes(force=True)
        self.assertEqual(run.call_count, 2)

    def test_caller_cannot_poison_the_cached_snapshot(self):
        payload = json.dumps([{"ProcessId": 7}])
        with self._patch_run(payload):
            first = ho._processes()
        first[0]["ProcessId"] = 8
        with self._patch_run(payload) as run:
            second = ho._processes()
        self.assertEqual(run.call_count, 0)
        self.assertEqual(second[0]["ProcessId"], 7)

    def test_failed_query_is_not_cached(self):
        with patch.object(subprocess, "run", side_effect=OSError("boom")):
            self.assertEqual(ho._processes(), [])
        self.assertIsNone(ho._process_cache)

    def test_quit_user_qq_rechecks_with_a_fresh_query(self):
        """复查「QQ 退了没有」不能吃缓存，否则刚杀掉的进程会被读成还在跑、白等一轮。"""
        with patch.object(ho, "user_qq_pids", return_value=[11]) as pids, patch.object(ho, "_taskkill", return_value=True):
            ho.quit_user_qq(timeout=0.0)
        self.assertTrue(any(entry.kwargs.get("fresh") for entry in pids.call_args_list), pids.call_args_list)


if __name__ == "__main__":
    unittest.main()
