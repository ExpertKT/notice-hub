import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from qq_live_digest import hosting


_SPEC = importlib.util.spec_from_file_location("packaging_launcher", Path(__file__).parents[1] / "packaging" / "launcher.py")
launcher = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(launcher)


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
        base = Path(tempfile.mkdtemp(prefix="notice-hub-mutex-"))
        self.assertEqual(launcher._instance_mutex_name(base), launcher._instance_mutex_name(base.resolve()))

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


class LauncherSilentFailureTests(unittest.TestCase):
    """B-5 (C-08): console=False + no tray means a failed start shows the user nothing."""

    def test_log_launcher_writes_readable_line(self):
        root = Path(tempfile.mkdtemp(prefix="notice-hub-log-"))
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
        server, thread, port = self.start_health_server()
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.3)"])
        try:
            self.assertFalse(launcher._wait_health(port, process, timeout=2))
        finally:
            process.wait(timeout=5)
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

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
                                                        with patch.object(launcher.webbrowser, "open") as opened:
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
        root = Path(tempfile.mkdtemp(prefix="notice-hub-watchdog-"))
        settings = type("Settings", (), {})()
        with patch.object(hosting, "hosting_status", return_value={"hosting_active": False}):
            host = launcher.TrayHost(root, root / ".env", settings, process, "http://127.0.0.1:18766")
        host._notify = lambda message: None
        return host

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
        root = Path(tempfile.mkdtemp(prefix="notice-hub-quit-"))
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
        root = Path(tempfile.mkdtemp(prefix="notice-hub-quitreq-"))
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
        root = Path(tempfile.mkdtemp(prefix="notice-hub-quitreq-"))
        with patch.object(launcher, "_other_instance_running", return_value=False):
            self.assertEqual(launcher._request_quit(root), 1)
        self.assertFalse(launcher._quit_request_path(root).exists())

    def test_request_quit_writes_file_and_waits_for_exit(self):
        root = Path(tempfile.mkdtemp(prefix="notice-hub-quitreq-"))
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
        root = Path(tempfile.mkdtemp(prefix="notice-hub-quitreq-"))
        with patch.object(launcher, "_other_instance_running", return_value=True):
            with patch.object(launcher.time, "sleep", lambda seconds: None):
                self.assertEqual(launcher._request_quit(root, wait_seconds=0.0), 1)


if __name__ == "__main__":
    unittest.main()
