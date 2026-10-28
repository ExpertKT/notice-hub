import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from qq_live_digest import hosting


def _mkdtemp(case: unittest.TestCase, prefix: str) -> Path:
    """临时目录用完即删：注册到用例 cleanup，失败也不留垃圾。"""
    path = Path(tempfile.mkdtemp(prefix=prefix))
    case.addCleanup(shutil.rmtree, path, ignore_errors=True)
    return path


_SPEC = importlib.util.spec_from_file_location("packaging_launcher", Path(__file__).parents[1] / "packaging" / "launcher.py")
launcher = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(launcher)


class LauncherImportCompatibilityTests(unittest.TestCase):
    def test_launcher_loads_without_optional_dpi_function(self):
        name = "qq_live_digest.floatball_host"
        fake_host = ModuleType(name)
        fake_host.snap_position = lambda *args: None
        fake_host.write_state = lambda *args: None
        source = Path(__file__).parents[1] / "packaging" / "launcher.py"
        spec = importlib.util.spec_from_file_location("packaging_launcher_without_dpi", source)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        with patch.dict(sys.modules, {name: fake_host}):
            spec.loader.exec_module(module)
            self.assertIsNone(module.ensure_dpi_aware)


class FakeProcess:
    def __init__(self, polls, pid=4321):
        self.polls = iter(polls)
        self.returncode = None
        self.pid = pid

    def poll(self):
        value = next(self.polls)
        if value is not None:
            self.returncode = value
        return value

    def terminate(self):
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode


class EnvTemplateTest(unittest.TestCase):
    """C-02 regression: a fresh install must bind 0.0.0.0 and get a non-empty token.

    webapp.py only generates/persists an access token when web_host is not a loopback
    address, so a 127.0.0.1 template meant every fresh install had no token at all and
    /api/app/qr.png always answered 400.
    """

    def test_template_binds_all_interfaces_and_generates_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_file = root / ".env"
            launcher._ensure_env_file(root, env_file)
            text = env_file.read_text(encoding="utf-8")
            values = dict(
                line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#")
            )
        self.assertEqual(values.get("QQ_DIGEST_WEB_HOST"), "0.0.0.0")
        self.assertTrue(values.get("QQ_DIGEST_WEB_TOKEN"))
        # Must match webapp.py: only a non-loopback host earns a token.
        self.assertNotIn(values.get("QQ_DIGEST_WEB_HOST"), {"127.0.0.1", "localhost", "::1"})

    def test_read_env_value_shares_config_parser_without_touching_environ(self):
        """W14: launcher 不再自带一套 .env 解析，改走 config.parse_env_file。"""
        key = "QQ_DIGEST_TEST_LAUNCHER_ONLY"
        self.assertNotIn(key, os.environ)
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "# comment\n"
                f"{key} = \"tok=with=equals\"\n"
                "\n"
                "QQ_DIGEST_WEB_HOST=0.0.0.0\n",
                encoding="utf-8",
            )
            self.assertEqual(launcher._read_env_value(env_file, key), "tok=with=equals")
            self.assertEqual(launcher._read_env_value(env_file, "QQ_DIGEST_WEB_HOST"), "0.0.0.0")
            self.assertEqual(launcher._read_env_value(env_file, "QQ_DIGEST_MISSING"), "")
            # 读 .env 不应产生副作用（旧手写版也不会写 os.environ）
            self.assertNotIn(key, os.environ)
            # 文件不存在 / 不是文件时返回空串，不抛异常
            self.assertEqual(launcher._read_env_value(Path(tmp) / "absent.env", key), "")
            self.assertEqual(launcher._read_env_value(Path(tmp), key), "")

    @unittest.skipUnless(sys.platform == "win32", "Windows launcher template")
    def test_template_matches_config_default_host(self):
        from qq_live_digest.config import Settings

        default_host = Settings().web_host
        template_host = launcher.ENV_TEMPLATE.split("QQ_DIGEST_WEB_HOST=", 1)[1].split("\n", 1)[0]
        self.assertEqual(template_host, default_host)


class DesktopShortcutTests(unittest.TestCase):
    def test_beta_icon_names_prefer_beta_assets(self):
        self.assertEqual(
            launcher._icon_names("beta"),
            ("icon-256-beta.png", "icon-beta.ico", "icon-256.png", "icon.ico"),
        )
        self.assertEqual(launcher._icon_names("stable"), ("icon-256.png", "icon.ico"))
        self.assertEqual(launcher._icon_names(None), ("icon-256.png", "icon.ico"))

    def test_shortcut_uses_beta_icon_when_present_and_stable_keeps_exe_icon(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / launcher.LAUNCHER_EXE
            target.write_bytes(b"exe")
            (root / "assets").mkdir()
            beta_icon = root / "assets" / "icon-beta.ico"
            beta_icon.write_bytes(b"ico")
            self.assertEqual(launcher._shortcut_icon_location(root, "beta"), str(beta_icon.resolve()))
            self.assertEqual(launcher._shortcut_icon_location(root, "stable"), str(target) + ",0")
            self.assertEqual(launcher._shortcut_icon_location(root, None), str(target) + ",0")

    def test_existing_shortcut_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / launcher.LAUNCHER_EXE
            target.write_bytes(b"exe")
            seen = {}

            def fake_run(args, **kwargs):
                seen["script"] = args[-1]
                return SimpleNamespace(returncode=0, stderr="")

            with patch.object(launcher.subprocess, "run", side_effect=fake_run):
                self.assertIsNone(launcher._create_desktop_shortcut(root))
            self.assertIn("if (Test-Path -LiteralPath $shortcut) { exit 0 }", seen["script"])


class SingleInstanceMutexTests(unittest.TestCase):
    """B-10: the single-instance mutex must be scoped per install directory.

    The old name was the global "Local\\notice-hub-launcher", so a running production
    tray made the beta launcher exit silently (exit 0) without starting beta.
    """

    def test_different_install_dirs_get_different_mutex_names(self):
        prod = launcher._instance_mutex_name(Path(r"F:\notice-hub"))
        beta = launcher._instance_mutex_name(Path(r"F:\notice-hub-beta"))
        self.assertNotEqual(prod, beta)

    def test_same_install_dir_is_stable(self):
        root = Path(r"F:\notice-hub")
        self.assertEqual(launcher._instance_mutex_name(root), launcher._instance_mutex_name(root))

    def test_name_is_case_insensitive_like_windows_paths(self):
        lower = launcher._instance_mutex_name(Path(r"F:\Notice-Hub"))
        upper = launcher._instance_mutex_name(Path(r"F:\NOTICE-HUB"))
        self.assertEqual(lower, upper)

    def test_name_has_no_backslashes_or_colons(self):
        # Windows object names cannot contain backslashes; that was the reason to hash.
        name = launcher._instance_mutex_name(Path(r"F:\notice-hub"))
        self.assertNotIn("\\", name.split("Local\\", 1)[1])
        self.assertNotIn(":", name)
        self.assertTrue(name.startswith("Local\\notice-hub-launcher-"))

    def test_relative_and_absolute_spellings_of_same_dir_agree(self):
        base = _mkdtemp(self, "notice-hub-mutex-")
        self.assertEqual(launcher._instance_mutex_name(base), launcher._instance_mutex_name(base.resolve()))

    @unittest.skipUnless(sys.platform == "win32", "Win32 named mutex")
    def test_acquire_passes_root_through_to_mutex_name(self):
        # Patch the real ctypes.windll.kernel32 attribute so the call is actually intercepted.
        import ctypes

        seen = {}

        class FakeKernel32:
            @staticmethod
            def CreateMutexW(_attrs, _owner, name):
                seen["name"] = name
                return object()

            @staticmethod
            def GetLastError():
                return 0

        root = Path(r"F:\notice-hub-beta")
        with patch.object(ctypes.windll, "kernel32", FakeKernel32()):
            self.assertTrue(launcher._acquire_single_instance(root))
        self.assertEqual(seen.get("name"), launcher._instance_mutex_name(root))


class LauncherOnedirPathTests(unittest.TestCase):
    def test_service_launch_uses_install_root_executable(self):
        root = _mkdtemp(self, "notice-hub-onedir-")
        env_file = root / ".env"
        with patch.object(launcher.subprocess, "Popen") as popen:
            launcher._launch_service(root, env_file, 8766)
        args = popen.call_args.args[0]
        self.assertEqual(Path(args[0]), root / "qq-live-digest.exe")
        self.assertEqual(popen.call_args.kwargs["cwd"], root)


class LauncherBrowserTests(unittest.TestCase):
    def test_open_browser_skips_when_suppressed(self):
        with patch.object(launcher, "BROWSER_SUPPRESSED", True), patch.object(launcher.webbrowser, "open") as open_browser:
            self.assertFalse(launcher._open_browser("http://127.0.0.1/"))
        open_browser.assert_not_called()

    def test_open_browser_opens_when_not_suppressed(self):
        with patch.object(launcher, "BROWSER_SUPPRESSED", False), patch.object(
            launcher.webbrowser, "open", return_value=True
        ) as open_browser:
            self.assertTrue(launcher._open_browser("http://127.0.0.1/"))
        open_browser.assert_called_once_with("http://127.0.0.1/")


class LauncherSilentFailureTests(unittest.TestCase):
    """B-5 (C-08): console=False + no tray means a failed start shows the user nothing."""

    def test_log_launcher_writes_readable_line(self):
        root = _mkdtemp(self, "notice-hub-log-")
        launcher._log_launcher(root, "健康检查失败：port=8790 exit=1")
        text = (root / "logs" / "launcher.log").read_text(encoding="utf-8")
        self.assertIn("健康检查失败：port=8790 exit=1", text)

    def test_log_launcher_survives_unwritable_root(self):
        # Must never raise: a logging failure must not mask the real error.
        launcher._log_launcher(Path(r"Z:\definitely\not\here"), "boom")


class FakeMenu:
    SEPARATOR = object()

    def __init__(self, *items):
        self.items = items

    def __call__(self, *items):
        return FakeMenu(*items)


class FakePystray:
    Menu = FakeMenu()

    class MenuItem:
        def __init__(self, title, action, **kwargs):
            self.title = title
            self.action = action
            self.enabled = kwargs.get("enabled")

        @property
        def text(self):
            return self.title(self) if callable(self.title) else self.title


class LauncherPortSafetyTests(unittest.TestCase):
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            payload = b'{"ok": true, "service": "qq-tasks"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            pass

    def start_health_server(self):
        server = HTTPServer(("127.0.0.1", 0), self.HealthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, server.server_address[1]

    def test_wait_health_rejects_external_200_when_process_already_exited(self):
        server, thread, port = self.start_health_server()
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait(timeout=5)
        try:
            self.assertFalse(launcher._wait_health(port, process, timeout=2))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_wait_health_rechecks_after_delay_when_process_exits(self):
        # 真实子进程的退出时刻不确定：全量跑时解释器启动会被拖慢，0.3 秒睡眠可能落在
        # _wait_health 的 0.6 秒重检窗口之外，返回 True。这里用可控假进程让「健康响应后
        # 进程退出」确定发生：第一次 poll 还活着，重检时已退出。
        server, thread, port = self.start_health_server()
        polls = []

        class DyingProcess:
            def poll(self):
                polls.append(None)
                return None if len(polls) == 1 else 0

        try:
            self.assertFalse(launcher._wait_health(port, DyingProcess(), timeout=2))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        # 必须问过第二次 poll：证明「响应后进程退出」的重检真的发生了
        self.assertGreaterEqual(len(polls), 2)

    def test_port_probe_detects_health_server_and_empty_port(self):
        server, thread, port = self.start_health_server()
        try:
            self.assertTrue(launcher._port_has_health_response(port))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        closed = HTTPServer(("127.0.0.1", 0), self.HealthHandler)
        closed_port = closed.server_address[1]
        closed.server_close()
        self.assertFalse(launcher._port_has_health_response(closed_port))

    def test_main_uses_next_port_without_editing_env_when_port_is_occupied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_file = root / ".env"
            env_file.write_text("QQ_DIGEST_WEB_PORT=18876\n", encoding="utf-8")
            before = env_file.read_bytes()
            fake_process = SimpleNamespace(poll=lambda: None, terminate=lambda: None, wait=lambda timeout=None: 0)
            with patch.object(launcher.sys, "executable", str(root / launcher.LAUNCHER_EXE)):
                with patch.object(launcher.Settings, "from_env", return_value=SimpleNamespace(web_port=18876)):
                    with patch.object(launcher, "_create_desktop_shortcut"):
                        # 后台的「检查更新」线程也会调 _notify_info：CI runner 能连到
                        # GitHub、真的查到更新，于是上面那句 notify.assert_not_called()
                        # 会在 runner 上红（本机查不到就不红）。这条测试要证明的是
                        # "端口回退这条路不弹通知"，所以必须把更新检查关掉。
                        with patch.object(launcher, "_update_check_enabled", return_value=False):
                            with patch.object(launcher, "_acquire_single_instance", return_value=True):
                                with patch.object(launcher, "_port_probe", side_effect=["occupied", "free"]):
                                    with patch.object(launcher, "_log_launcher") as log:
                                        with patch.object(launcher, "_notify_info") as notify:
                                            with patch.object(launcher, "_launch_service", return_value=fake_process) as launch:
                                                with patch.object(launcher, "_wait_health", return_value=True):
                                                    with patch.object(launcher.TrayHost, "run", return_value=True):
                                                        # 健康检查被 mock 成 True，main() 会走到健康检查通过后的
                                                        # webbrowser.open(url)。不拦它，跑这条测试就会在
                                                        # 跑测试的人屏幕上真弹一个浏览器窗口，指向一个服务
                                                        # 已经 terminate 掉的残留端口，URL 里还带着 token。
                                                        with patch.object(launcher, "_console_window_open", return_value=False), \
                                                                patch.object(launcher.webbrowser, "open") as opened:
                                                            self.assertEqual(launcher.main(), 0)
            self.assertEqual(opened.call_count, 1)
            # 路径必须两边都 resolve 再比：CI runner 的 TEMP 是 8.3 短名（RUNNER~1），
            # 而 main() 是从 sys.executable 推出的长名（runneradmin），直接比 Path 文本必红。
            args = launch.call_args.args
            self.assertEqual(Path(args[0]).resolve(), root.resolve())
            self.assertEqual(Path(args[1]).resolve(), env_file.resolve())
            self.assertEqual(args[2], 18877)
            notify.assert_not_called()
            self.assertTrue(any("端口 18876 被占用，本次改用 18877" in call.args[1] for call in log.call_args_list))
            self.assertEqual(env_file.read_bytes(), before)

    def test_port_probe_rejects_unrelated_health_json(self):
        class UnrelatedHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                payload = b'{"ok": true, "service": "other"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), UnrelatedHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertEqual(launcher._port_probe(server.server_address[1]), "occupied")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_port_probe_accepts_the_real_health_payload(self):
        """真实 /api/health 是 {"ok": True, "service": "qq-tasks", "channels": [...]}（webapp.py）。

        以前 _port_probe 用 `payload == {"ok": True, "service": "qq-tasks"}` 整字典比，
        多出来的 `channels` 让它永远不相等 → _wait_health 必然失败 → 启动器会把自己刚拉起来的
        服务 terminate 掉、并报"服务没能启动"。用户本地的实例就是这么被搞挂的。
        这条用**真实形状**兜住它，防止再退回整字典比。
        """
        class RealHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                payload = b'{"ok": true, "service": "qq-tasks", "channels": []}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), RealHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertEqual(launcher._port_probe(server.server_address[1]), "qq-tasks")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class LauncherWatchdogTests(unittest.TestCase):
    def make_host(self, process):
        root = _mkdtemp(self, "notice-hub-watchdog-")
        settings = type("Settings", (), {})()
        with patch.object(hosting, "hosting_status", return_value={"hosting_active": False}):
            host = launcher.TrayHost(root, root / ".env", settings, process, "http://127.0.0.1:18766")
        host._notify = lambda message: None
        return host

    def test_floatball_url_includes_existing_env_token(self):
        if launcher.FloatBallWindow is None:
            self.skipTest("浮球模块不在发布范围内")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_file = root / ".env"
            env_file.write_text("QQ_DIGEST_WEB_TOKEN=a/b token\n", encoding="utf-8")
            host = self.make_host(FakeProcess([None]))
            host.env_file = env_file
            with patch.object(launcher, "FLOATBALL_ENABLED", True), \
                    patch.object(host.floatball, "start") as start:
                host._start_floatball()
            start.assert_called_once_with(
                "http://127.0.0.1:18766/floatball?token=a%2Fb%20token"
            )

    def test_busy_flag_self_heals_after_timeout(self):
        """2026-10-24 现场：托管操作卡住把托盘「忙」永久冻住，连「重启并更新」都点不了。"""
        host = self.make_host(FakeProcess([None]))
        host.busy = True
        self.assertTrue(host.is_busy())
        with patch.object(
            launcher.time, "monotonic", return_value=host._busy_since + launcher.BUSY_TIMEOUT_SECONDS + 1
        ):
            self.assertFalse(host.is_busy())
        self.assertFalse(host.busy)
        log = (host.root / "logs" / "launcher.log").read_text(encoding="utf-8")
        self.assertIn("已恢复菜单", log)

    def test_start_returns_menu_when_hosting_start_never_returns(self):
        """W30: 托管层一次调用永不返回时，托盘不能永久灰着（现场要重启资源管理器才恢复）。"""
        host = self.make_host(FakeProcess([None]))
        notifications = []
        host._notify = notifications.append
        release = threading.Event()

        def stuck(_settings):
            release.wait(10)
            return {"ok": True}

        try:
            with patch.object(launcher, "BUSY_TIMEOUT_SECONDS", 0.05), patch.object(
                hosting, "start", side_effect=stuck
            ):
                host._start()
        finally:
            release.set()

        self.assertFalse(host.busy)
        self.assertTrue(any("超时" in message for message in notifications), notifications)
        log = (host.root / "logs" / "launcher.log").read_text(encoding="utf-8")
        self.assertIn("超时", log)

    def test_running_process_is_not_counted(self):
        host = self.make_host(FakeProcess([None]))
        with patch.object(launcher.threading, "Thread") as thread:
            self.assertTrue(host.watchdog_tick())
            thread.assert_not_called()
        self.assertEqual(host.restart_failures, 0)

    def test_exit_records_pid_and_schedules_five_second_restart(self):
        host = self.make_host(FakeProcess([1], pid=9876))
        delays = []

        class CapturedThread:
            def __init__(self, target, args=(), **kwargs):
                delays.append(args[0])

            def start(self):
                pass

        with patch.object(launcher.threading, "Thread", CapturedThread):
            self.assertFalse(host.watchdog_tick())
        self.assertEqual(delays, [5.0])
        self.assertEqual(host.restart_failures, 1)
        log = (host.root / "logs" / "launcher.log").read_text(encoding="utf-8")
        self.assertIn("pid=9876 code=1", log)

    def test_three_failed_restarts_use_backoff_then_require_manual(self):
        host = self.make_host(FakeProcess([1, 2, 3]))
        delays = []

        class CapturedThread:
            def __init__(self, target, args=(), **kwargs):
                delays.append(args[0])
                self.target = target
                self.args = args

            def start(self):
                host._restart_service = lambda automatic=False: False
                host._watchdog_stop.wait = lambda delay: False
                self.target(*self.args)

        with patch.object(launcher.threading, "Thread", CapturedThread):
            for _ in range(3):
                host.watchdog_tick()
        self.assertEqual(delays, [5.0, 15.0, 45.0])
        self.assertEqual(host.restart_failures, 3)
        self.assertTrue(host.manual_restart_required)

    def test_update_worker_does_not_notify_when_updater_reports_no_update(self):
        host = self.make_host(FakeProcess([None]))
        with patch.object(launcher, "_notify_info") as notify, patch.object(
            launcher, "_read_update_state", return_value={}
        ), patch.object(launcher, "_write_update_state"), patch(
            "qq_live_digest.updater.check", return_value={"ok": True, "has_update": False, "latest": {"tag": "v1.1.0-beta.6"}, "checked_at": "now", "error": "旧版本"}
        ):
            launcher._update_check_worker(host.root, host.root / "data")
        notify.assert_not_called()

    def test_update_state_keeps_fields_written_by_the_webapp(self):
        """回归：launcher 和网页服务都写 updates/state.json，谁后写都不许把对方的字段冲掉。

        以前的写法是「读全量 → 改全量 → 写回」，两边交替写必然互相覆盖
        （launcher 写完 last_check 后 webapp 一写，last_check 就没了）。
        """
        root = _mkdtemp(self, "notice-hub-update-state-")
        data_dir = root / "data"
        path = launcher._update_state_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 网页服务那边先写下自己的字段（模拟它的 read-modify-write）
        path.write_text(
            json.dumps({"last_download": {"tag": "v1.1.0-beta.8"}, "apply_pending": True}), encoding="utf-8"
        )

        launcher._write_update_state(
            data_dir,
            {
                "last_check": "2026-10-24 00:00:00",
                "last_result": {"ok": True, "error": "", "latest": "v1.1.0-beta.9", "has_update": True},
            },
        )
        # 网页服务再写一次自己的字段（它用的是旧的全量写法，会重读最新再改）
        web_state = json.loads(path.read_text(encoding="utf-8"))
        web_state["last_download"] = {"tag": "v1.1.0-beta.9"}
        path.write_text(json.dumps(web_state), encoding="utf-8")

        launcher._write_update_state(data_dir, {"notified_tag": "v1.1.0-beta.9"})
        final = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(final["notified_tag"], "v1.1.0-beta.9")
        self.assertEqual(final["last_check"], "2026-10-24 00:00:00")
        self.assertEqual(final["last_result"]["latest"], "v1.1.0-beta.9")
        self.assertEqual(final["last_download"], {"tag": "v1.1.0-beta.9"})
        self.assertTrue(final["apply_pending"])
        leftovers = [item.name for item in path.parent.iterdir() if item.name.endswith(".tmp")]
        self.assertEqual(leftovers, [], "临时文件没清干净：%r" % (leftovers,))

    def test_launcher_log_rotates_instead_of_growing_forever(self):
        root = _mkdtemp(self, "notice-hub-log-rotate-")
        log_file = root / "logs" / "launcher.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text("x" * (launcher.LOG_MAX_BYTES + 1), encoding="utf-8")

        launcher._log_launcher(root, "健康检查失败")
        self.assertTrue(log_file.exists())
        self.assertTrue(log_file.with_name("launcher.log.1").exists(), "旧日志该被挪成 .1")
        self.assertLess(log_file.stat().st_size, launcher.LOG_MAX_BYTES)
        self.assertIn("健康检查失败", log_file.read_text(encoding="utf-8"))

        # 老备份超过保留数量时最旧的要被删掉，不能无限堆文件
        log_file.with_name("launcher.log.2").write_text("old", encoding="utf-8")
        log_file.write_text("x" * (launcher.LOG_MAX_BYTES + 1), encoding="utf-8")
        launcher._log_launcher(root, "又一轮")
        self.assertTrue(log_file.with_name("launcher.log.2").exists())
        self.assertFalse(log_file.with_name("launcher.log.3").exists(), "最多保留 2 个备份")

    def test_menu_restart_label_updates_when_pending_changes(self):
        host = self.make_host(FakeProcess([None]))
        pending = [None]
        host._pending_update = lambda: pending[0]
        menu = host.build_menu(FakePystray)
        restart_item = next(
            item for item in menu.items
            if getattr(getattr(item, "action", None), "__self__", None) is host
            and getattr(getattr(item, "action", None), "__func__", None) is host.on_restart.__func__
        )
        self.assertEqual(restart_item.text, "重启服务")
        pending[0] = {"zip": "update.zip"}
        self.assertEqual(restart_item.text, "重启并更新")

    def test_menu_restart_item_disabled_while_busy(self):
        host = self.make_host(FakeProcess([None]))
        menu = host.build_menu(FakePystray)
        restart_item = next(
            item for item in menu.items
            if getattr(getattr(item, "action", None), "__self__", None) is host
            and getattr(getattr(item, "action", None), "__func__", None) is host.on_restart.__func__
        )
        self.assertTrue(restart_item.enabled(restart_item))
        host.busy = True
        self.assertFalse(restart_item.enabled(restart_item))
        host.busy = False
        self.assertTrue(restart_item.enabled(restart_item))

    def test_menu_contains_manual_restart(self):
        host = self.make_host(FakeProcess([None]))
        menu = host.build_menu(FakePystray)
        self.assertIn("重启服务", [item.text for item in menu.items if hasattr(item, "title")])

    def test_menu_contains_chaoxing_cookie_action_and_disables_while_busy(self):
        host = self.make_host(FakeProcess([None]))
        menu = host.build_menu(FakePystray)
        item = next(item for item in menu.items if item.text == "读取学习通 Cookie")
        self.assertTrue(item.enabled(item))
        host._cookie_read_busy = True
        self.assertFalse(item.enabled(item))
        host._cookie_read_busy = False
        host.busy = True
        self.assertFalse(item.enabled(item))

    def test_chaoxing_cookie_action_posts_token_and_notifies_success(self):
        host = self.make_host(FakeProcess([None]))
        host.env_file.write_text("QQ_DIGEST_WEB_TOKEN=secret-token\n", encoding="utf-8")
        response = type("Response", (), {"__enter__": lambda self: self, "__exit__": lambda self, *args: None, "read": lambda self: b'{"ok": true}'})()
        notifications = []
        with patch.object(launcher.urllib.request, "urlopen", return_value=response) as open_url, patch.object(
            host, "_notify", side_effect=notifications.append
        ), patch.object(launcher.threading, "Thread", lambda target, **kwargs: type("T", (), {"start": lambda self: target()})()):
            host.on_read_chaoxing_cookie()
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:18766/api/chaoxing/auto_cookie")
        self.assertEqual(request.get_header("X-token"), "secret-token")
        self.assertEqual(request.data, b"{}")
        self.assertEqual(notifications, ["已读取学习通 Cookie，并自动启用学习通"])
        self.assertFalse(host._cookie_read_busy)

    def test_menu_contains_clipboard_cookie_action_and_imports_text(self):
        host = self.make_host(FakeProcess([None]))
        menu = host.build_menu(FakePystray)
        item = next(item for item in menu.items if item.text == "从剪贴板导入学习通 Cookie")
        self.assertTrue(item.enabled(item))
        host.env_file.write_text("QQ_DIGEST_WEB_TOKEN=secret-token\n", encoding="utf-8")
        response = type("Response", (), {"__enter__": lambda self: self, "__exit__": lambda self, *args: None, "read": lambda self: b'{"ok": true}'})()
        notifications = []
        with patch.object(host, "_clipboard_text", return_value="  sid=test; uid=1  "), patch.object(launcher.urllib.request, "urlopen", return_value=response) as open_url, patch.object(host, "_notify", side_effect=notifications.append), patch.object(launcher.threading, "Thread", lambda target, **kwargs: type("T", (), {"start": lambda self: target()})()):
            host.on_import_clipboard_cookie()
        request = open_url.call_args.args[0]
        self.assertEqual(json.loads(request.data.decode("utf-8")), {"chaoxing_cookie": "sid=test; uid=1"})
        self.assertEqual(request.get_header("X-token"), "secret-token")
        self.assertEqual(notifications, ["已从剪贴板导入学习通 Cookie"])
        self.assertFalse(host._clipboard_cookie_busy)

    def test_clipboard_reader_does_not_use_tkinter_and_distinguishes_read_failure(self):
        import inspect
        self.assertNotIn("tkinter", inspect.getsource(launcher.TrayHost._clipboard_text))
        host = self.make_host(FakeProcess([None]))
        notifications = []
        with patch.object(host, "_clipboard_text", return_value=None), patch.object(host, "_notify", side_effect=notifications.append), patch.object(launcher.threading, "Thread", lambda target, **kwargs: type("T", (), {"start": lambda self: target()})()):
            host.on_import_clipboard_cookie()
        self.assertEqual(notifications, ["读取剪贴板失败，请稍后重试"])

    def test_clipboard_cookie_action_reports_empty_and_server_error(self):
        host = self.make_host(FakeProcess([None]))
        notifications = []
        with patch.object(host, "_clipboard_text", return_value=""), patch.object(host, "_notify", side_effect=notifications.append), patch.object(launcher.threading, "Thread", lambda target, **kwargs: type("T", (), {"start": lambda self: target()})()):
            host.on_import_clipboard_cookie()
        self.assertEqual(notifications, ["剪贴板里没有内容"])
        response = type("Response", (), {"__enter__": lambda self: self, "__exit__": lambda self, *args: None, "read": lambda self: '{"ok": false, "error": "Cookie 无效"}'.encode("utf-8")})()
        with patch.object(host, "_clipboard_text", return_value="sid=test"), patch.object(launcher.urllib.request, "urlopen", return_value=response), patch.object(host, "_notify", side_effect=notifications.append), patch.object(launcher.threading, "Thread", lambda target, **kwargs: type("T", (), {"start": lambda self: target()})()):
            host.on_import_clipboard_cookie()
        self.assertEqual(notifications[-1], "Cookie 无效")

    def test_chaoxing_cookie_action_reports_locked_browser(self):
        host = self.make_host(FakeProcess([None]))
        response = type("Response", (), {"__enter__": lambda self: self, "__exit__": lambda self, *args: None, "read": lambda self: b'{"ok": false, "category": "browser_locked"}'})()
        notifications = []
        with patch.object(launcher.urllib.request, "urlopen", return_value=response), patch.object(
            host, "_notify", side_effect=notifications.append
        ), patch.object(launcher.threading, "Thread", lambda target, **kwargs: type("T", (), {"start": lambda self: target()})()):
            host.on_read_chaoxing_cookie()
        self.assertEqual(notifications, ["浏览器还在运行：请完全退出 Edge/Chrome（含托盘图标和后台进程）后再点一次"])
        self.assertFalse(host._cookie_read_busy)


class LauncherFirewallTests(unittest.TestCase):
    """W31: 手机连不上时，启动器自己把防火墙规则补上，不让用户翻控制面板。

    用户原话：「弹一次 UAC 自动添加」「24 小时内不重复弹」「设置页手动点修复防火墙」。
    """

    def make_root(self):
        root = _mkdtemp(self, "notice-hub-firewall-")
        return root, root / "data"

    def probe(self, checked=True, allowed=False, port=8766):
        return {
            "checked": checked,
            "allowed": allowed,
            "port": port,
            "command": launcher._firewall_rule_command(port),
        }

    def test_missing_rule_prompts_once_per_24_hours(self):
        root, data = self.make_root()
        requests = []

        def fake_request(port, command):
            requests.append((port, command))
            return None

        with patch.object(launcher, "_firewall_allows", return_value=self.probe()), patch.object(
            launcher, "_request_firewall_rule", side_effect=fake_request
        ):
            first = launcher.ensure_firewall_rule(root, data, 8766, now=1_000_000.0)
            second = launcher.ensure_firewall_rule(root, data, 8766, now=1_000_000.0 + 3600)
            forced = launcher.ensure_firewall_rule(root, data, 8766, force=True, now=1_000_000.0 + 3600)
            later = launcher.ensure_firewall_rule(
                root, data, 8766, now=1_000_000.0 + 3600 + launcher.FIREWALL_RETRY_SECONDS + 1
            )

        self.assertTrue(first["prompted"])
        self.assertTrue(first["checked"])
        self.assertEqual(requests[0][0], 8766)
        self.assertIn("localport=8766", requests[0][1])
        self.assertFalse(second["prompted"], "24 小时内不许重复弹 UAC")
        self.assertIn("冷却中", second["error"])
        self.assertTrue(forced["prompted"], "设置页「修复防火墙」要能跳过冷却")
        self.assertTrue(later["prompted"], "过了冷却期要重新尝试")
        self.assertEqual(len(requests), 3)
        self.assertEqual(launcher._read_firewall_state(data)["port"], 8766)

    def test_rejected_prompt_also_counts_as_attempt(self):
        """用户点了「否」也是他的决定：时间照记，24 小时内别再烦他。"""
        root, data = self.make_root()
        calls = []
        with patch.object(launcher, "_firewall_allows", return_value=self.probe()), patch.object(
            launcher,
            "_request_firewall_rule",
            side_effect=lambda port, command: calls.append(port) or "用户取消或系统拒绝提权（代码 5）",
        ):
            first = launcher.ensure_firewall_rule(root, data, 8766, now=1_000_000.0)
            second = launcher.ensure_firewall_rule(root, data, 8766, now=1_000_000.0 + 60)

        self.assertFalse(first["prompted"])
        self.assertIn("取消", first["error"])
        self.assertEqual(launcher._read_firewall_state(data)["result"], "failed")
        self.assertFalse(second["prompted"])
        self.assertEqual(calls, [8766])

    def test_existing_rule_never_prompts_and_clears_state(self):
        root, data = self.make_root()
        data.mkdir()
        launcher._write_firewall_state(data, {"last_attempt": 1_000_000.0, "port": 8766, "result": "requested"})
        with patch.object(launcher, "_firewall_allows", return_value=self.probe(allowed=True)), patch.object(
            launcher, "_request_firewall_rule"
        ) as request:
            result = launcher.ensure_firewall_rule(root, data, 8766, now=1_000_000.0 + 1000)
        self.assertTrue(result["allowed"])
        request.assert_not_called()
        self.assertEqual(launcher._read_firewall_state(data), {})

    def test_unknown_probe_never_prompts(self):
        """netsh 查不到（非 Windows / 被策略禁掉）时不知道缺不缺，弹 UAC 只会骚扰。"""
        root, data = self.make_root()
        with patch.object(launcher, "_firewall_allows", return_value=self.probe(checked=False)), patch.object(
            launcher, "_request_firewall_rule"
        ) as request:
            result = launcher.ensure_firewall_rule(root, data, 8766, now=1_000_000.0)
        self.assertFalse(result["checked"])
        self.assertFalse(result["prompted"])
        request.assert_not_called()
        self.assertFalse(launcher._firewall_state_path(data).exists())

    def test_allows_parses_netsh_output_like_the_webapp(self):
        output = "\r\n".join(
            [
                "Rule Name:                            ChatGPT",
                "----------------------------------------------------------------------",
                "Enabled:                              Yes",
                "Direction:                            In",
                "Protocol:                             TCP",
                "LocalPort:                            8081-8112",
                "Action:                               Allow",
                "",
                "Rule Name:                            群务台 8766",
                "----------------------------------------------------------------------",
                "Enabled:                              Yes",
                "Direction:                            In",
                "Protocol:                             TCP",
                "LocalPort:                            8766,8767",
                "Action:                               Allow",
                "",
                "Rule Name:                            disabled-rule",
                "----------------------------------------------------------------------",
                "Enabled:                              No",
                "Direction:                            In",
                "LocalPort:                            8766",
                "Action:                               Allow",
                "",
            ]
        )
        # utf-8 而不是 gbk：decode_console_output 先试 utf-8；非中文 Windows 的
        # locale 是 cp1252，gbk 字节会被它「成功」解成乱码，规则名就对不上了。
        completed = SimpleNamespace(returncode=0, stdout=output.encode("utf-8"))
        # 探测入口按 sys.platform 判 Windows（不是 os.name），linux/macos job 上不 patch
        # 就根本不跑 netsh，checked 会是 False。
        # netsh 的子进程在 host_actions 里跑（launcher._firewall_allows 只是转发），
        # 所以 patch 的是 host_actions.subprocess；linux/macos job 上没有 netsh，
        # 不 patch 它就会 FileNotFoundError → 规则清单为空 → checked=False。
        with patch("qq_live_digest.host_actions.is_windows", return_value=True), \
                patch.object(launcher.host_actions.subprocess, "run", return_value=completed):
            result = launcher._firewall_allows(8766)
        self.assertTrue(result["checked"])
        self.assertTrue(result["allowed"], "端口列表里的 8766 也算放行")
        self.assertEqual(result["rule"], "群务台 8766")

    def _firewall_host(self, self_name: str = "notice-hub-firewall-request-"):
        root = _mkdtemp(self, self_name)
        settings = type("Settings", (), {})()
        with patch.object(hosting, "hosting_status", return_value={"hosting_active": False}):
            host = launcher.TrayHost(root, root / ".env", settings, FakeProcess([None]), "http://127.0.0.1:18766")
        host._notify = lambda message: None
        data = launcher._data_dir_for(root, settings)
        data.mkdir(parents=True, exist_ok=True)
        return host, data

    def test_watchdog_consumes_firewall_request_and_writes_result(self):
        """页面写 firewall.request → 托盘提权 → 结果写 firewall.result 并删请求。"""
        host, data = self._firewall_host()
        request = launcher._firewall_request_path(data)
        request.write_text(json.dumps({"port": 18766}), encoding="utf-8")

        with patch.object(launcher.host_actions, "run_elevated", return_value=("ok", "已请求管理员权限，确认后稍等几秒。")) as elevated, \
                patch.object(launcher.host_actions, "firewall_allows", return_value={"allowed": True, "checked": True}), \
                patch.object(launcher.time, "sleep", return_value=None), \
                patch.object(
                    launcher.threading,
                    "Thread",
                    lambda target, args=(), **kwargs: type("T", (), {"start": lambda self: target(*args)})(),
                ):
            host.watchdog_tick()

        elevated.assert_called_once()
        command = elevated.call_args[0][0]
        # 加规则 + 「把 netsh 规则清单导出到快照」必须在同一次提权里，否则会弹两次 UAC。
        # 旧规则（不限 profile）要先删再建，单条 add 覆盖不了它。
        self.assertTrue(command.startswith("cmd.exe /c " + launcher.host_actions.firewall_delete_command(18766)))
        self.assertIn(launcher.host_actions.firewall_allow_command(18766), command)
        self.assertIn("localport=18766", command)
        self.assertIn(launcher.host_actions.FIREWALL_SNAPSHOT_NAME, command)
        self.assertFalse(request.exists(), "请求文件要消费掉，否则每个 tick 都重跑")
        result = json.loads(launcher._firewall_result_path(data).read_text(encoding="utf-8"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["port"], 18766)
        self.assertIn("管理员权限", result["message"])
        self.assertTrue(result["at"])
        self.assertTrue(result["checked"])

    def test_watchdog_does_not_report_success_before_rule_is_live(self):
        """UAC 通过 ≠ 规则生效：真探不到就不能写 ok=true（消防墙绿得太早）。"""
        host, data = self._firewall_host("notice-hub-firewall-unconfirmed-")
        request = launcher._firewall_request_path(data)
        request.write_text(json.dumps({"port": 18766}), encoding="utf-8")

        with patch.object(launcher.host_actions, "run_elevated", return_value=("ok", "已请求管理员权限。")), \
                patch.object(launcher.host_actions, "firewall_allows", return_value={"allowed": False, "checked": True}), \
                patch.object(launcher.time, "sleep", return_value=None), \
                patch.object(
                    launcher.threading,
                    "Thread",
                    lambda target, args=(), **kwargs: type("T", (), {"start": lambda self: target(*args)})(),
                ):
            host.watchdog_tick()

        result = json.loads(launcher._firewall_result_path(data).read_text(encoding="utf-8"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "failed")
        self.assertIn("还没看到这条放行规则", result["message"])
        self.assertIn("netsh", result["message"])

    def test_watchdog_says_needs_admin_when_probe_cannot_read_rules(self):
        """探测读不到（checked=False）不能说成「规则没生效」，要提示需要管理员才能确认。"""
        host, data = self._firewall_host("notice-hub-firewall-unreadable-")
        request = launcher._firewall_request_path(data)
        request.write_text(json.dumps({"port": 18766}), encoding="utf-8")

        with patch.object(launcher.host_actions, "run_elevated", return_value=("ok", "已请求管理员权限。")), \
                patch.object(launcher.host_actions, "firewall_allows", return_value={"allowed": False, "checked": False}), \
                patch.object(launcher.time, "sleep", return_value=None), \
                patch.object(
                    launcher.threading,
                    "Thread",
                    lambda target, args=(), **kwargs: type("T", (), {"start": lambda self: target(*args)})(),
                ):
            host.watchdog_tick()

        result = json.loads(launcher._firewall_result_path(data).read_text(encoding="utf-8"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "failed")
        self.assertFalse(result["checked"])
        self.assertIn("需要管理员才能确认", result["message"])
        self.assertNotIn("已授予", result["message"])

    def test_watchdog_writes_failure_result_when_elevation_fails(self):
        host, data = self._firewall_host("notice-hub-firewall-failure-")
        request = launcher._firewall_request_path(data)
        request.write_text(json.dumps({"port": 18766}), encoding="utf-8")

        with patch.object(launcher.host_actions, "run_elevated", return_value=("cancelled", "你取消了管理员权限，可以再点一次。")), \
                patch.object(
                    launcher.threading,
                    "Thread",
                    lambda target, args=(), **kwargs: type("T", (), {"start": lambda self: target(*args)})(),
                ):
            host.watchdog_tick()

        result = json.loads(launcher._firewall_result_path(data).read_text(encoding="utf-8"))
        self.assertFalse(result["ok"])
        self.assertIn("取消", result["message"])
        self.assertFalse(request.exists())


class LauncherQuitTests(unittest.TestCase):
    """Regression: 托盘「退出」必须真的退得掉。

    用户 2026-10-11 撞上的：托盘图标黏着退不掉，只能去任务管理器强杀。根因是
    on_quit 的 shutdown() 一点异常都不兜：hosting_status()/hosting.stop() 只要抛，
    后面的 icon.stop() 就不执行，托盘、启动器、服务子进程全都留着。
    """

    class SyncThread:
        """把退出线程改成同步执行，测试里才好断言。"""

        def __init__(self, target, args=(), kwargs=None, daemon=None, name=None):
            self._target = target
            self._args = args

        def start(self):
            self._target(*self._args)

    class CapturedTimer:
        """记下硬兜底定时器，但不让它真的触发。"""

        def __init__(self, interval, function, args=(), kwargs=None):
            self.interval = interval

        def start(self):
            pass

    def make_host(self, process):
        root = _mkdtemp(self, "notice-hub-quit-")
        settings = type("Settings", (), {})()
        with patch.object(hosting, "hosting_status", return_value={"hosting_active": False}):
            host = launcher.TrayHost(root, root / ".env", settings, process, "http://127.0.0.1:18766")
        host._notify = lambda message: None
        return host

    def test_quit_stops_icon_kills_service_even_when_hosting_raises(self):
        process = FakeProcess([None, None])
        host = self.make_host(process)
        stopped = []

        class Icon:
            def stop(self):
                stopped.append(True)

        host.icon = Icon()
        exits = []
        with patch.object(launcher.threading, "Thread", self.SyncThread):
            with patch.object(launcher.threading, "Timer", self.CapturedTimer):
                with patch.object(launcher.os, "_exit", lambda code: exits.append(code)):
                    with patch.object(hosting, "hosting_status", side_effect=RuntimeError("boom")):
                        host.on_quit()
        self.assertEqual(stopped, [True])
        self.assertEqual(exits, [0])
        self.assertEqual(process.returncode, 0)
        log = (host.root / "logs" / "launcher.log").read_text(encoding="utf-8")
        self.assertIn("结束托管出错", log)

    def test_quit_hard_deadline_kills_service_and_exits(self):
        process = FakeProcess([None, None])
        host = self.make_host(process)
        exits = []
        with patch.object(launcher.os, "_exit", lambda code: exits.append(code)):
            host._force_quit()
        self.assertEqual(process.returncode, 0)
        self.assertEqual(exits, [0])


class LauncherQuitRequestTests(unittest.TestCase):
    """`--quit`：托盘点不动的自动化出口（也能让用户从命令行收工）。

    托盘没有窗口消息可投递，所以约定一个请求文件：运行中的看门狗每次 tick 查一次。
    没有它，「托盘退得干净吗」只能靠人工点托盘验证。
    """

    def make_host(self, process):
        root = _mkdtemp(self, "notice-hub-quitreq-")
        settings = type("Settings", (), {})()
        with patch.object(hosting, "hosting_status", return_value={"hosting_active": False}):
            host = launcher.TrayHost(root, root / ".env", settings, process, "http://127.0.0.1:18766")
        host._notify = lambda message: None
        return host

    def test_watchdog_tick_quits_when_request_file_appears(self):
        host = self.make_host(FakeProcess([None]))
        request = launcher._quit_request_path(host.root)
        request.write_text("1", encoding="utf-8")
        calls = []
        host.on_quit = lambda *args, **kwargs: calls.append(True)

        host.watchdog_tick()

        self.assertEqual(calls, [True])
        self.assertFalse(request.exists(), "请求文件必须被消费掉，不然下次启动会被立刻顶掉")

    def test_request_quit_reports_when_no_instance_is_running(self):
        root = _mkdtemp(self, "notice-hub-quitreq-")
        with patch.object(launcher, "_other_instance_running", return_value=False):
            self.assertEqual(launcher._request_quit(root), 1)
        self.assertFalse(launcher._quit_request_path(root).exists())

    def test_request_quit_writes_file_and_waits_for_exit(self):
        root = _mkdtemp(self, "notice-hub-quitreq-")
        seen = []

        def other_running(_root):
            seen.append(True)
            return len(seen) < 3  # 第三次探测时实例已经没了

        with patch.object(launcher, "_other_instance_running", side_effect=other_running):
            with patch.object(launcher.time, "sleep", lambda seconds: None):
                self.assertEqual(launcher._request_quit(root, wait_seconds=5), 0)
        content = launcher._quit_request_path(root).read_text(encoding="utf-8")
        self.assertTrue(content.isdigit(), "请求文件里要留下发起方的 pid，方便查日志")

    def test_request_quit_gives_up_after_deadline(self):
        root = _mkdtemp(self, "notice-hub-quitreq-")
        with patch.object(launcher, "_other_instance_running", return_value=True):
            with patch.object(launcher.time, "sleep", lambda seconds: None):
                self.assertEqual(launcher._request_quit(root, wait_seconds=0.0), 1)


if __name__ == "__main__":
    unittest.main()


class TempDirCleanupTests(unittest.TestCase):
    """W12: 测试临时目录不许留在仓库根目录。"""

    def test_mkdtemp_helper_removes_dir_on_cleanup(self):
        path = _mkdtemp(self, "notice-hub-selftest-")
        self.assertTrue(path.is_dir())
        self.doCleanups()
        self.assertFalse(path.exists())

    def test_mkdtemp_helper_leaves_repo_root_unchanged(self):
        root = Path.cwd()
        before = sorted(p.name for p in root.iterdir() if p.name.startswith("notice-hub-selftest-"))
        _mkdtemp(self, "notice-hub-selftest-")
        self.doCleanups()
        after = sorted(p.name for p in root.iterdir() if p.name.startswith("notice-hub-selftest-"))
        self.assertEqual(before, after)
