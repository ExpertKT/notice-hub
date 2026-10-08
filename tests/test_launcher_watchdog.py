import importlib.util
import tempfile
import unittest
from pathlib import Path
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

    def test_menu_contains_manual_restart(self):
        host = self.make_host(FakeProcess([None]))
        menu = host.build_menu(FakePystray)
        self.assertIn("重启服务", [item.title for item in menu.items if hasattr(item, "title")])


if __name__ == "__main__":
    unittest.main()
