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

from noticehub import hosting, updater


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
        name = "noticehub.floatball_host"
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
        from noticehub.config import Settings

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
        self.assertEqual(Path(args[0]), root / "noticehub.exe")
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


class LauncherSingleInstanceTests(unittest.TestCase):
    """已经开着一份时不该再开标签：双击、开机自启、自动更新后的重启都会走到这里。"""

    def _run_with_existing_instance(self, console_open: bool):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("QQ_DIGEST_WEB_PORT=18876\n", encoding="utf-8")
            with patch.object(launcher.sys, "executable", str(root / launcher.LAUNCHER_EXE)), patch.object(
                launcher, "_create_desktop_shortcut"
            ), patch.object(launcher, "_acquire_single_instance", return_value=False), patch.object(
                launcher, "_console_window_open", return_value=console_open
            ), patch.object(launcher, "BROWSER_SUPPRESSED", False), patch.object(
                launcher, "_log_launcher"
            ) as log, patch.object(
                launcher.webbrowser, "open"
            ) as open_browser:
                code = launcher.main()
        return code, open_browser, log

    def test_console_already_open_does_not_spawn_another_tab(self):
        code, open_browser, log = self._run_with_existing_instance(console_open=True)
        self.assertEqual(code, 0)
        open_browser.assert_not_called()
        self.assertTrue(
            any(
                "已有实例在运行" in call.args[1] and "控制台已打开" in call.args[1]
                for call in log.call_args_list
            )
        )

    def test_missing_console_opens_the_console_once(self):
        code, open_browser, log = self._run_with_existing_instance(console_open=False)
        self.assertEqual(code, 0)
        self.assertEqual(open_browser.call_count, 1)
        self.assertTrue(open_browser.call_args.args[0].startswith("http://127.0.0.1:18876/"))
        self.assertTrue(
            any("未检测到控制台窗口" in call.args[1] for call in log.call_args_list)
        )


class LauncherStalePendingTests(unittest.TestCase):
    """装完了的待装记录要自己消失：否则托盘永远显示「重启并更新」，点一次又装一遍。"""

    def _pending_file(self, root: Path, version: str) -> Path:
        updates = root / "data" / "updates"
        updates.mkdir(parents=True, exist_ok=True)
        path = updates / "pending.json"
        record = {
            "zip": str(root / "pkg.zip"),
            "version": version,
            "at": "2026-11-01T06:00:00+00:00",
            "sha256": "0" * 64,
        }
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def test_pending_for_current_version_is_cleared(self):
        root = _mkdtemp(self, "nh-pending-")
        pending = self._pending_file(root, "1.1.0-beta.16")
        with patch.object(launcher, "_data_dir_for", return_value=root / "data"), patch.object(
            updater, "current_version", return_value="1.1.0-beta.16"
        ):
            launcher._drop_installed_pending(root, SimpleNamespace())
        self.assertFalse(pending.exists())
        log = (root / "logs" / "launcher.log").read_text(encoding="utf-8")
        self.assertIn("1.1.0-beta.16 不比当前版本 1.1.0-beta.16 新，已清理", log)

    def test_pending_older_than_the_running_version_is_cleared(self):
        """手动覆装过之后，残留的旧待装记录也要清：否则页面一直喊「更新包已就绪」。"""
        root = _mkdtemp(self, "nh-pending-")
        pending = self._pending_file(root, "1.1.0-beta.16")
        with patch.object(launcher, "_data_dir_for", return_value=root / "data"), patch.object(
            updater, "current_version", return_value="1.1.0-beta.17"
        ):
            launcher._drop_installed_pending(root, SimpleNamespace())
        self.assertFalse(pending.exists())
        log = (root / "logs" / "launcher.log").read_text(encoding="utf-8")
        self.assertIn("1.1.0-beta.16 不比当前版本 1.1.0-beta.17 新，已清理", log)

    def test_pending_for_another_version_is_kept(self):
        root = _mkdtemp(self, "nh-pending-")
        pending = self._pending_file(root, "1.1.0-beta.17")
        with patch.object(launcher, "_data_dir_for", return_value=root / "data"), patch.object(
            updater, "current_version", return_value="1.1.0-beta.16"
        ):
            launcher._drop_installed_pending(root, SimpleNamespace())
        self.assertTrue(pending.exists())


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
            self.visible = kwargs.get("visible", True)

        @property
        def text(self):
            return self.title(self) if callable(self.title) else self.title

        @property
        def shown(self):
            """pystray 在摆菜单时求值 visible；callable 就调它，和真库一致。"""
            visible = self.visible
            return bool(visible(self)) if callable(visible) else bool(visible)


class LauncherMenuTests(unittest.TestCase):
    """托盘菜单只摆真能用的项：浮球开关关着时不出现，重复动作的「设置」已删。"""

    def make_host(self, floatball_enabled: bool = False):
        root = _mkdtemp(self, "notice-hub-menu-")
        settings = SimpleNamespace(data_dir=str(root / "data"), floatball_enabled=floatball_enabled)
        with patch.object(hosting, "hosting_status", return_value={"hosting_active": False}):
            return launcher.TrayHost(root, root / ".env", settings, FakeProcess([None]), "http://127.0.0.1:18766")

    def menu_titles(self, host):
        menu = host.build_menu(FakePystray)
        return [item.text for item in menu.items if isinstance(item, FakePystray.MenuItem) and item.shown]

    def test_floatball_item_hidden_when_feature_disabled(self):
        host = self.make_host(floatball_enabled=False)
        titles = self.menu_titles(host)
        self.assertNotIn("显示/隐藏浮窗", titles, "浮球开关关着时这一项点了没反应，不该摆出来")
        self.assertIn("打开控制台", titles)
        self.assertNotIn("设置", titles, "「设置」和「打开控制台」是同一个动作，名不副实")

    def test_floatball_item_shown_when_feature_enabled(self):
        host = self.make_host(floatball_enabled=True)
        titles = self.menu_titles(host)
        self.assertIn("显示/隐藏浮窗", titles)

    def test_floatball_item_follows_the_setting_after_it_changes(self):
        """设置页勾上之后菜单项要自己出现，不用重启托盘。"""
        host = self.make_host(floatball_enabled=False)
        self.assertNotIn("显示/隐藏浮窗", self.menu_titles(host))
        host.settings.floatball_enabled = True
        self.assertIn("显示/隐藏浮窗", self.menu_titles(host))

    def test_launcher_does_not_create_a_floatball_host_while_disabled(self):
        """开关关着就别在机器上预建一份 Edge 宿主（构造时不碰磁盘也该是 None）。"""
        host = self.make_host(floatball_enabled=False)
        self.assertIsNone(host.floatball)

    def test_toggling_floatball_without_the_module_does_not_raise(self):
        """浮球模块没打进包时（导入退成 None），点托盘这一项只记日志，不该抛异常。"""
        host = self.make_host(floatball_enabled=True)
        with patch.object(launcher, "FloatBallHost", None), \
                patch.object(host, "_log_watchdog") as logged:
            host._toggle_floatball()  # 不抛就算过
        self.assertEqual(logged.call_count, 1)
        self.assertIn("浮窗模块不可用", str(logged.call_args))

    @unittest.skipUnless(sys.platform == "win32", "浮球宿主只在 Windows 上创建")
    def test_toggling_floatball_does_nothing_after_the_switch_is_turned_off(self):
        """菜单是建单时定型的，关掉开关后旧菜单项可能还挂着；点进来不许把球又起出来。"""
        host = self.make_host(floatball_enabled=False)
        with patch.object(launcher.threading, "Thread") as thread:
            host._toggle_floatball()
        thread.assert_not_called()
        self.assertIsNone(host.floatball)

    @unittest.skipUnless(sys.platform == "win32", "浮球宿主只在 Windows 上创建")
    def test_toggling_floatball_shows_the_existing_edge_window_instead_of_starting_another(self):
        """Edge 窗口还在（hwnd 非空）时，托盘点一下只该显示它，不该再起第二个窗口。"""
        host = self.make_host(floatball_enabled=True)
        shown = []
        ball = SimpleNamespace(hwnd=4321, visible=False, show=lambda: shown.append(1))
        host.floatball = ball
        with patch.object(launcher.threading, "Thread") as thread:
            host._toggle_floatball()
        self.assertEqual(shown, [1])
        thread.assert_not_called()


class FloatballSettingSyncTests(unittest.TestCase):
    """设置页勾/取消之后，看门狗 2 秒一轮把浮球起停跟到位（不重启、不动网页）。"""

    def make_host(self, floatball_enabled: bool = False):
        root = _mkdtemp(self, "notice-hub-floatball-sync-")
        settings = SimpleNamespace(data_dir=str(root / "data"), floatball_enabled=floatball_enabled)
        with patch.object(hosting, "hosting_status", return_value={"hosting_active": False}):
            host = launcher.TrayHost(root, root / ".env", settings, FakeProcess([None]), "http://127.0.0.1:18766")
        return host, root / ".env"

    def write_env(self, path, value, stamp):
        path.write_text("NOTICEHUB_FLOATBALL_ENABLED=%s\n" % value, encoding="utf-8")
        os.utime(path, (stamp, stamp))

    def test_unchanged_setting_never_touches_the_floatball(self):
        host, env = self.make_host(floatball_enabled=False)
        self.write_env(env, "0", 1_700_000_000)
        with patch.object(host, "_start_floatball") as started, patch.object(host, "_stop_floatball") as stopped:
            host._sync_floatball_setting()
            host._sync_floatball_setting()  # 第二轮 mtime 没变，连 .env 都不用再读
        started.assert_not_called()
        stopped.assert_not_called()
        self.assertFalse(host.settings.floatball_enabled)

    def test_env_turning_on_starts_the_floatball(self):
        host, env = self.make_host(floatball_enabled=False)
        self.write_env(env, "0", 1_700_000_000)
        host._sync_floatball_setting()
        self.write_env(env, "1", 1_700_000_100)
        with patch.object(host, "_start_floatball") as started, patch.object(launcher.threading, "Thread") as thread:
            host._sync_floatball_setting()
        # 起 Edge 要等窗口，必须甩到后台线程；直接同步起会把看门狗卡住。
        thread.assert_called_once()
        self.assertIs(thread.call_args.kwargs["target"], started)
        self.assertTrue(host.settings.floatball_enabled)

    def test_env_turning_off_stops_and_forgets_the_floatball(self):
        host, env = self.make_host(floatball_enabled=False)
        self.write_env(env, "1", 1_700_000_000)
        with patch.object(host, "_start_floatball"), patch.object(launcher.threading, "Thread"):
            host._sync_floatball_setting()
        self.assertTrue(host.settings.floatball_enabled)
        stops = []
        host.floatball = SimpleNamespace(stop=lambda: stops.append(1))
        self.write_env(env, "0", 1_700_000_200)
        with patch.object(host, "_start_floatball") as started:
            host._sync_floatball_setting()
        self.assertEqual(stops, [1], "取消勾选要把 Edge 浮球真的关掉")
        self.assertIsNone(host.floatball, "关掉之后实例要清空，重新勾选才能新建一份")
        started.assert_not_called()
        self.assertFalse(host.settings.floatball_enabled)

    def test_bootstrap_does_not_start_the_floatball_when_disabled(self):
        host, env = self.make_host(floatball_enabled=False)
        self.write_env(env, "0", 1_700_000_000)
        with patch.object(host, "_start_floatball") as started:
            host._bootstrap_floatball()
        started.assert_not_called()
        self.assertIsNone(host.floatball)

    def test_bootstrap_starts_the_floatball_when_the_env_says_on(self):
        """升级前就把开关打开的机器：启动时球要自己出来，不能等用户去点托盘。"""
        host, env = self.make_host(floatball_enabled=True)
        self.write_env(env, "1", 1_700_000_000)
        with patch.object(host, "_start_floatball") as started:
            host._bootstrap_floatball()
        started.assert_called_once_with()

    def test_stop_failure_is_logged_and_does_not_break_the_watchdog(self):
        host, env = self.make_host(floatball_enabled=True)

        def boom():
            raise RuntimeError("Edge 已退出")

        host.floatball = SimpleNamespace(stop=boom)
        self.write_env(env, "0", 1_700_000_300)
        with patch.object(host, "_log_watchdog") as logged:
            host._sync_floatball_setting()  # 不抛就算过
        self.assertIn("关闭浮窗失败", str(logged.call_args))
        # 引用必须留着：丢了引用下一轮就再没东西能关掉这只球（托盘项已随开关隐藏）。
        self.assertIsNotNone(host.floatball)
        # 下一轮看门狗接着收，收掉了才摘引用。
        stopped = []
        host.floatball = SimpleNamespace(stop=lambda: stopped.append(1))
        host._sync_floatball_setting()
        self.assertEqual(stopped, [1])
        self.assertIsNone(host.floatball)

    def test_old_prefix_env_key_still_turns_the_floatball_on(self):
        """有人照旧前缀手写 .env：启动时 settings 读成 True，同步这一侧也必须认，否则会被翻回关闭。"""
        host, _env = self.make_host(floatball_enabled=True)
        env = host.env_file
        env.write_text("QQ_DIGEST_FLOATBALL_ENABLED=1\n", encoding="utf-8")
        os.utime(env, (1_700_000_700, 1_700_000_700))
        started, stopped = [], []
        with patch.object(host, "_start_floatball", lambda: started.append(1)), \
             patch.object(host, "_stop_floatball", lambda: stopped.append(1)):
            host._sync_floatball_setting()
        # 读不到新前缀时若不回退读旧前缀，这里会被翻成 False，启动对账就不起球、网页还显示开。
        self.assertTrue(host.settings.floatball_enabled)
        self.assertEqual(stopped, [])
        self.assertEqual(started, [], "状态没变，不该在这里再起一次（启动对账负责起）")

    def test_concurrent_starts_do_not_open_two_browsers(self):
        """托盘点击与开关同步可能同时进来，只许一次启动在飞。"""
        host, _env = self.make_host(floatball_enabled=True)
        host.url = "http://127.0.0.1:9/"
        created = []
        host._floatball_starting.acquire()
        try:
            with patch.object(launcher, "FloatBallHost", lambda *_a, **_k: created.append(1)):
                host._start_floatball()
            self.assertEqual(created, [], "已有一次在飞就别再建第二个")
        finally:
            host._floatball_starting.release()
        with patch.object(host, "_log_watchdog"):
            with patch.object(launcher, "FloatBallHost", lambda *_a, **_k: created.append(1)):
                host._start_floatball()
        self.assertEqual(len(created), 1)

    def test_starting_floatball_while_the_setting_turns_off_stops_it_again(self):
        """起球要等窗口（最长 15 秒），这中间关掉开关就必须立刻收掉，否则界面上没有入口。"""
        host, env = self.make_host(floatball_enabled=True)
        host.url = "http://127.0.0.1:9/"
        started, stopped = [], []
        host.floatball = SimpleNamespace(
            start=lambda url: (started.append(url), setattr(host.settings, "floatball_enabled", False)),
            stop=lambda: stopped.append(1),
        )
        host._start_floatball()
        self.assertEqual(len(started), 1, "该起的时候还是要起")
        self.assertEqual(stopped, [1], "开关中途关掉就得立刻收掉")
        self.assertIsNone(host.floatball)

    def test_env_change_rebuilds_the_tray_menu(self):
        """pystray 的 visible 只在建单时求值，开关变了必须重建菜单，否则项一直挂着旧状态。"""
        host, env = self.make_host(floatball_enabled=False)
        host._pystray = FakePystray
        host.icon = SimpleNamespace(menu=None)
        self.write_env(env, "0", 1_700_000_400)
        host._sync_floatball_setting()
        self.assertIsNone(host.icon.menu, "没变就不该重建")
        self.write_env(env, "1", 1_700_000_500)
        with patch.object(host, "_start_floatball"), patch.object(launcher.threading, "Thread"):
            host._sync_floatball_setting()
        self.assertIsNotNone(host.icon.menu)
        titles = [item.text for item in host.icon.menu.items
                  if isinstance(item, FakePystray.MenuItem) and item.shown]
        self.assertIn("显示/隐藏浮窗", titles)



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
        if launcher.FloatBallHost is None:
            self.skipTest("浮球模块不在发布范围内")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_file = root / ".env"
            env_file.write_text("QQ_DIGEST_WEB_TOKEN=a/b token\n", encoding="utf-8")
            host = self.make_host(FakeProcess([None]))
            host.env_file = env_file
            host.settings.floatball_enabled = True
            # __init__ 不再预建宿主（默认关闭），这里补一个假的，只为断言拼出来的 URL。
            host.floatball = SimpleNamespace(start=lambda *_args, **_kwargs: None)
            with patch.object(host.floatball, "start") as start, \
                    patch.object(launcher, "FloatBallHost", lambda *_args, **_kwargs: host.floatball):
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
            "noticehub.updater.check", return_value={"ok": True, "has_update": False, "latest": {"tag": "v1.1.0-beta.6"}, "checked_at": "now", "error": "旧版本"}
        ):
            launcher._update_check_worker(host.root, host.root / "data")
        notify.assert_not_called()

    def test_update_notice_tells_user_an_agent_can_update_for_them(self):
        """弹框正文要说明「更新失败可以交给 agent」，别让用户自己折腾。"""
        host = self.make_host(FakeProcess([None]))
        shown = []
        with patch.object(launcher, "_notify_info", lambda root, message, title="": shown.append(message)), patch.object(
            launcher, "_read_update_state", return_value={}
        ), patch.object(launcher, "_write_update_state"), patch(
            "noticehub.updater.check",
            return_value={
                "ok": True,
                "has_update": True,
                "latest": {"tag": "v1.1.0-beta.9", "name": "群务台 1.1.0-beta.9"},
                "checked_at": "now",
                "error": "",
            },
        ):
            launcher._update_check_worker(host.root, host.root / "data")
        self.assertEqual(len(shown), 1, shown)
        self.assertIn("v1.1.0-beta.9", shown[0])
        self.assertIn("要是不想折腾，把这一屏交给 agent，让它替你更新。", shown[0])

    def _sync_thread(self):
        """把 launcher 里起的线程改成同步执行（真线程 + os._exit 会打死测试进程）。"""

        class ImmediateThread:
            def __init__(self, target=None, args=(), kwargs=None, daemon=None):
                self.target = target
                self.args = args
                self.kwargs = kwargs or {}

            def start(self):
                self.target(*self.args, **self.kwargs)

        return ImmediateThread

    def test_restart_with_pending_update_leaves_pending_record_to_the_apply_script(self):
        """托盘点「重启并更新」只负责把脚本拉起来，删 pending.json 是脚本解压成功后的事。

        脚本还没跑就在启动器里清掉，一旦脚本失败（哈希不符 / 文件被占用 / 解压异常），
        用户就失去重试入口，只能重下整个包。
        """
        host = self.make_host(FakeProcess([None]))
        quit_done = []
        host._pending_update = lambda: {"zip": str(host.root / "pkg.zip")}
        host._stop_watchdog = lambda: None
        host._start_watchdog = lambda: None
        host._quit_steps = lambda: quit_done.append(True)

        with patch.object(launcher.threading, "Thread", self._sync_thread()), patch.object(
            launcher.os, "_exit"
        ) as exit_mock, patch(
            "noticehub.updater.apply_async", return_value={"ok": True, "error": "", "script": "apply.ps1"}
        ), patch("noticehub.updater.clear_pending") as clear_pending:
            host.on_restart()

        clear_pending.assert_not_called()
        self.assertEqual(quit_done, [True])
        exit_mock.assert_called_once_with(0)

    def test_restart_does_not_clear_pending_even_when_apply_async_fails(self):
        """脚本都没启动成功时更不能清——那是用户唯一的重试入口。"""
        host = self.make_host(FakeProcess([None]))
        notices = []
        host._notify = notices.append
        host._pending_update = lambda: {"zip": str(host.root / "pkg.zip")}
        host._stop_watchdog = lambda: None
        host._start_watchdog = lambda: None
        host._quit_steps = lambda: None

        with patch.object(launcher.threading, "Thread", self._sync_thread()), patch.object(
            launcher.os, "_exit"
        ) as exit_mock, patch(
            "noticehub.updater.apply_async", return_value={"ok": False, "error": "更新包不存在"}
        ), patch("noticehub.updater.clear_pending") as clear_pending:
            host.on_restart()

        clear_pending.assert_not_called()
        exit_mock.assert_not_called()
        self.assertTrue(any("更新包不存在" in message for message in notices), notices)

    def test_restart_hands_the_pending_sha256_to_the_apply_script(self):
        """apply_async 没有可信哈希会直接拒绝安装；托盘这条路必须把 pending 里的哈希传下去。

        漏传过一次：网页「一键更新」能装，托盘点「重启并更新」每次都失败并提示「请重新下载」，
        而重新下载根本解决不了——下载是对的，是启动时没把哈希交出去。
        """
        host = self.make_host(FakeProcess([None]))
        host._pending_update = lambda: {
            "zip": str(host.root / "pkg.zip"),
            "sha256": "a" * 64,
        }
        host._stop_watchdog = lambda: None
        host._start_watchdog = lambda: None
        host._quit_steps = lambda: None

        with patch.object(launcher.threading, "Thread", self._sync_thread()), patch.object(
            launcher.os, "_exit"
        ), patch(
            "noticehub.updater.apply_async", return_value={"ok": True, "error": "", "script": "apply.ps1"}
        ) as apply_mock:
            host.on_restart()

        self.assertEqual(apply_mock.call_args.kwargs.get("expected_sha256"), "a" * 64)
        self.assertEqual(apply_mock.call_args.kwargs.get("web_port"), 8766)

    def test_update_worker_records_a_failed_check_instead_of_staying_silent(self):
        """检查更新抛异常也要落 state.json：否则页面永远显示「还没有检查过更新」。"""
        host = self.make_host(FakeProcess([None]))
        data_dir = host.root / "data"

        with patch("noticehub.updater.check", side_effect=RuntimeError("网络炸了")):
            launcher._update_check_worker(host.root, data_dir)

        state = launcher._read_update_state(data_dir)
        self.assertFalse(state["last_result"]["ok"])
        self.assertIn("网络炸了", state["last_result"]["error"])
        self.assertFalse(state["last_result"]["has_update"])
        self.assertTrue(state["last_check"])
        log = (host.root / "logs" / "launcher.log").read_text(encoding="utf-8")
        self.assertIn("后台检查更新失败", log)

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

    def test_two_quick_restarts_only_launch_one_and_leave_busy_releasable(self):
        """P3-19：连点两次「重启服务」只能起一条线程；检查与置位必须一次做完。"""
        host = self.make_host(FakeProcess([None]))
        started = []

        class HeldThread:
            def __init__(self, target=None, args=(), kwargs=None, daemon=None):
                pass

            def start(self):
                # 故意不跑 target，模拟「线程体还没把 busy 置起来」的窗口。
                started.append(True)

        with patch.object(launcher.threading, "Thread", HeldThread):
            host.on_restart()
            host.on_restart()

        self.assertEqual(len(started), 1, started)
        self.assertTrue(host.is_busy())
        # 不能留死局：真正跑完复位后，菜单还能再点一次。
        host.busy = False
        with patch.object(launcher.threading, "Thread", HeldThread):
            host.on_restart()
        self.assertEqual(len(started), 2, started)

    def test_restart_reuses_the_fallback_port_instead_of_dropping_it(self):
        """P2-10：首启因端口占用换过端口，重启必须带上同一个端口。"""
        host = self.make_host(FakeProcess([0]))
        host.port = 18877
        calls = []

        def fake_launch(root, env_file, port=None):
            calls.append(port)
            return FakeProcess([None])

        with patch.object(launcher, "_port_probe", return_value="free"), patch.object(
            launcher, "_launch_service", side_effect=fake_launch
        ):
            ok = host._restart_service()

        self.assertTrue(ok)
        self.assertEqual(calls, [18877])

    def test_restart_falls_back_and_rewrites_url_when_remembered_port_is_taken(self):
        """P2-10：记住的端口后来也被占，重启走回退并同步 url/port，别打开死页。"""
        host = self.make_host(FakeProcess([0]))
        host.env_file.write_text("QQ_DIGEST_WEB_TOKEN=tok\n", encoding="utf-8")
        host.port = 18877
        calls = []

        def fake_launch(root, env_file, port=None):
            calls.append(port)
            return FakeProcess([None])

        with patch.object(launcher, "_port_probe", return_value="occupied"), patch.object(
            launcher, "_find_available_port", return_value=18878
        ) as find, patch.object(launcher, "_launch_service", side_effect=fake_launch):
            ok = host._restart_service()

        self.assertTrue(ok)
        self.assertEqual(calls, [18878])
        find.assert_called_once_with(18877)
        self.assertEqual(host.port, 18878)
        self.assertIn("18878", host.url)


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
        with patch("noticehub.host_actions.is_windows", return_value=True), \
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
