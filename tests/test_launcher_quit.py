"""退出流程（`_quit_steps`）的回归：顺序、单步失败要继续、托管收尾有总预算。

2026-10-31 的事故：托管活着时退出，`_quit_steps` 把「停 NapCat + 开回电脑版 QQ」
放在最前面，30 秒硬兜底被撑爆，用户点了退出等 40 秒还被强杀；而且服务子进程直到
硬兜底才被带走（服务日志还在写）。所以这里既要锁顺序，也要锁「一步炸了继续退」。
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_SPEC = importlib.util.spec_from_file_location(
    "packaging_launcher_quit", Path(__file__).parents[1] / "packaging" / "launcher.py"
)
launcher = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(launcher)

from noticehub import hosting  # noqa: E402


class QuitStepsOrderTests(unittest.TestCase):
    def _app(self, calls: list[str]) -> SimpleNamespace:
        app = object.__new__(launcher.TrayHost)
        app._log_watchdog = lambda message: calls.append("log:" + message)
        app._stop_watchdog = lambda: calls.append("watchdog")
        app._stop_service_child = lambda: calls.append("service")
        app.icon = SimpleNamespace(stop=lambda: calls.append("icon"))
        app.floatball = SimpleNamespace(stop=lambda: calls.append("floatball"))
        app.settings = SimpleNamespace()
        return app

    def test_quit_stops_service_and_icon_before_hosting_teardown(self):
        calls: list[str] = []
        app = self._app(calls)
        with patch.object(hosting, "hosting_status", return_value={"hosting_active": True}), \
                patch.object(hosting, "stop", side_effect=lambda *a, **k: calls.append("hosting")), \
                patch.object(hosting, "restore_qq_on_quit", return_value=False):
            launcher.TrayHost._quit_steps(app)
        self.assertEqual(calls, ["watchdog", "service", "icon", "floatball", "hosting"])

    def test_quit_skips_hosting_when_not_active(self):
        calls: list[str] = []
        app = self._app(calls)
        with patch.object(hosting, "hosting_status", return_value={"hosting_active": False}), \
                patch.object(hosting, "stop", side_effect=AssertionError("没托管不该停")), \
                patch.object(hosting, "restore_qq_on_quit", return_value=False):
            launcher.TrayHost._quit_steps(app)
        self.assertNotIn("hosting", calls)
        self.assertEqual(calls[:3], ["watchdog", "service", "icon"])

    def test_failed_step_does_not_block_the_rest(self):
        calls: list[str] = []
        app = self._app(calls)

        def boom() -> None:
            calls.append("icon-raise")
            raise RuntimeError("图标线程卡死")

        app.icon = SimpleNamespace(stop=boom)
        with patch.object(hosting, "hosting_status", return_value={"hosting_active": True}), \
                patch.object(hosting, "stop", side_effect=lambda *a, **k: calls.append("hosting")), \
                patch.object(hosting, "restore_qq_on_quit", return_value=False):
            launcher.TrayHost._quit_steps(app)
        # 图标炸了，浮窗与托管收尾照样要走到，并且留一条日志
        self.assertIn("floatball", calls)
        self.assertIn("hosting", calls)
        self.assertTrue(any(item.startswith("log:退出时关托盘图标出错") for item in calls), calls)

    def test_hosting_teardown_gets_exit_budget(self):
        """托管收尾必须带 quitting=True 和 5 秒预算，不许再用「点结束托管」那条慢路径。"""
        app = self._app([])
        seen: dict[str, object] = {}

        def fake_stop(settings, **kwargs):
            seen.update(kwargs)
            return {"ok": True}

        with patch.object(hosting, "hosting_status", return_value={"hosting_active": True}), \
                patch.object(hosting, "stop", side_effect=fake_stop), \
                patch.object(hosting, "restore_qq_on_quit", return_value=False):
            launcher.TrayHost._quit_steps(app)
        self.assertIs(seen.get("quitting"), True)
        self.assertEqual(seen.get("budget"), launcher.QUIT_TEARDOWN_BUDGET_SECONDS)
        self.assertLessEqual(launcher.QUIT_TEARDOWN_BUDGET_SECONDS, 5.0)


class HostingStopBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = SimpleNamespace(
            data_dir=Path(self.tmp.name),
            hosting_restore_qq=True,
        )

    def test_quitting_uses_fast_probe_and_skips_cim_verify(self):
        with patch.object(hosting, "_bridge", return_value={"ok": True}) as bridge, \
                patch.object(hosting, "start_user_qq", return_value={"ok": True}) as start, \
                patch.object(hosting, "_write_state"):
            hosting.stop(self.settings, quitting=True, budget=5.0)
        self.assertIs(bridge.call_args.kwargs.get("fast"), True)
        self.assertIs(start.call_args.kwargs.get("verify"), False)

    def test_normal_stop_keeps_accurate_probe(self):
        with patch.object(hosting, "_bridge", return_value={"ok": True}) as bridge, \
                patch.object(hosting, "start_user_qq", return_value={"ok": True}) as start, \
                patch.object(hosting, "_write_state"):
            hosting.stop(self.settings)
        self.assertEqual(bridge.call_args.kwargs, {})
        self.assertIs(start.call_args.kwargs.get("verify"), True)

    def test_budget_exhausted_skips_restore_with_warning(self):
        with patch.object(hosting, "_bridge", return_value={"ok": True}) as bridge, \
                patch.object(hosting, "start_user_qq") as start, \
                patch.object(hosting, "_write_state"), \
                self.assertLogs("noticehub.hosting", level="WARNING") as logs:
            result = hosting.stop(self.settings, quitting=True, budget=0.0)
        self.assertFalse(start.called, "预算用完就不许再开回 QQ")
        self.assertIs(bridge.call_args.kwargs.get("fast"), True)
        self.assertEqual(result["steps"][-1]["step"], "stop-napcat")
        self.assertTrue(any("跳过恢复电脑版 QQ" in line for line in logs.output), logs.output)


if __name__ == "__main__":
    unittest.main()
