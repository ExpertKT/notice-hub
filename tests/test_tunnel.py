"""公网入口状态机（noticehub/tunnel.py）的行为测试。

只测「阶段怎么走、失败怎么落到用户能看懂的文案」，不碰真实 Tailscale、不联网、不提权。
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from noticehub import tunnel


class FunnelSetupStateMachineTest(unittest.TestCase):
    def _setup(self) -> tunnel.FunnelSetup:
        return tunnel.FunnelSetup(8766)

    def test_missing_tailscale_downloads_and_installs_then_enables(self) -> None:
        setup = self._setup()
        with (
            mock.patch("noticehub.tunnel.tailscale_executable", side_effect=["", "tailscale"]),
            mock.patch("noticehub.tunnel.download_installer", return_value=r"C:\tmp\ts.exe") as download,
            mock.patch("noticehub.tunnel.run_installer") as install,
            mock.patch("noticehub.tunnel.read_status", return_value={"BackendState": "Running"}),
            mock.patch("noticehub.tunnel.enable_funnel", return_value={"ok": True, "url": "https://demo.ts.net:8443"}),
        ):
            setup.run()

        download.assert_called_once()
        install.assert_called_once_with(r"C:\tmp\ts.exe")
        self.assertEqual(setup.snapshot()["state"], "ready")
        self.assertEqual(setup.snapshot()["url"], "https://demo.ts.net:8443")
        self.assertFalse(setup.snapshot()["retryable"])

    def test_installer_cancel_is_reported_with_retry_hint(self) -> None:
        setup = self._setup()
        with (
            mock.patch("noticehub.tunnel.tailscale_executable", return_value=""),
            mock.patch("noticehub.tunnel.download_installer", return_value="ts.exe"),
            mock.patch("noticehub.tunnel.run_installer", side_effect=OSError("管理员授权被取消")),
        ):
            setup.run()

        snapshot = setup.snapshot()
        self.assertEqual(snapshot["state"], "error")
        self.assertIn("管理员授权被取消", snapshot["message"])
        self.assertIn("重试", snapshot["message"])
        self.assertTrue(snapshot["retryable"])

    def test_not_logged_in_waits_for_login_then_enables(self) -> None:
        setup = self._setup()
        process = mock.Mock()
        process.poll.return_value = 1
        with (
            mock.patch("noticehub.tunnel.tailscale_executable", return_value="tailscale"),
            mock.patch("noticehub.tunnel.read_status", return_value={"BackendState": "NeedsLogin", "AuthURL": "https://login"}),
            mock.patch("noticehub.tunnel.start_login", return_value=process) as login,
            mock.patch("noticehub.tunnel.FunnelSetup._wait_login", return_value=True),
            mock.patch("noticehub.tunnel.POLL_SECONDS", 0),
            mock.patch("noticehub.tunnel.enable_funnel", return_value={"ok": True, "url": "https://demo.ts.net"}),
        ):
            setup.run()

        login.assert_called_once_with("tailscale")
        self.assertEqual(setup.snapshot()["state"], "ready")

    def test_login_timeout_keeps_waiting_login_message_and_stops(self) -> None:
        setup = self._setup()
        process = mock.Mock()
        process.poll.return_value = None

        def fail_login(executable: str) -> bool:
            setup._fail("等待 Tailscale 登录超时（10 分钟）。请在电脑上打开 Tailscale 登录，再点「重试」。")
            return False

        with (
            mock.patch("noticehub.tunnel.tailscale_executable", return_value="tailscale"),
            mock.patch("noticehub.tunnel.read_status", return_value={"BackendState": "NeedsLogin"}),
            mock.patch("noticehub.tunnel.start_login", return_value=process),
            mock.patch("noticehub.tunnel.FunnelSetup._wait_login", side_effect=fail_login),
            mock.patch("noticehub.tunnel.enable_funnel") as enable,
        ):
            setup.run()

        self.assertFalse(enable.called)
        self.assertEqual(setup.snapshot()["state"], "error")
        self.assertIn("登录", setup.snapshot()["message"])
        process.terminate.assert_called_once()

    def test_funnel_rejection_becomes_error(self) -> None:
        setup = self._setup()
        with (
            mock.patch("noticehub.tunnel.tailscale_executable", return_value="tailscale"),
            mock.patch("noticehub.tunnel.read_status", return_value={"BackendState": "Running"}),
            mock.patch("noticehub.tunnel.enable_funnel", return_value={"ok": False, "error": "Tailscale 账户还没开启 Funnel"}),
        ):
            setup.run()

        self.assertEqual(setup.snapshot()["state"], "error")
        self.assertIn("Funnel", setup.snapshot()["message"])

    def test_start_is_idempotent_while_running_and_after_ready(self) -> None:
        setup = self._setup()
        with mock.patch("noticehub.tunnel.FunnelSetup.run"):
            first = setup.start()
            thread = setup._thread
            second = setup.start()
        self.assertEqual(first["state"], "installing")
        self.assertEqual(second["state"], "installing")
        # 已经在跑就不再起第二个线程（页面连点「重试」不会叠出多份安装）。
        self.assertIs(setup._thread, thread)

    def test_enable_funnel_skips_commands_when_not_logged_in(self) -> None:
        with (
            mock.patch("noticehub.tunnel.tailscale_executable", return_value="tailscale"),
            mock.patch("noticehub.tunnel.status_hint", return_value="Tailscale 还没登录"),
            mock.patch("noticehub.tunnel.subprocess.run") as run,
        ):
            result = tunnel.enable_funnel(8766)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "Tailscale 还没登录")
        run.assert_not_called()


class PublicCalendarUrlTests(unittest.TestCase):
    """S2：手机日历走公网订阅，不再要求和电脑同一个 WiFi。

    `enable_funnel` 开的是整站 "/" 规则，`/calendar.ics` 本来就在公网可达；
    以前只认「专门把 /xxx.ics 反代出来」的规则，于是公网地址永远查不到。
    """

    def _serve(self, handlers: dict, *, target: str = "demo.ts.net:8443", funnel: bool = True) -> str:
        config = {"Web": {target: {"Handlers": handlers}}}
        if funnel:
            config["AllowFunnel"] = {target: True}
        return json.dumps(config)

    def _call(self, serve: str, port: int = 8766) -> str:
        tunnel._SERVE_CACHE.clear()  # 每条用例换一份 serve 配置，别吃上一条的缓存
        with (
            mock.patch("noticehub.tunnel.tailscale_executable", return_value="tailscale"),
            mock.patch("noticehub.tunnel.subprocess.run", return_value=mock.Mock(stdout=serve)),
        ):
            return tunnel.public_calendar_url(port)

    def test_whole_site_funnel_route_covers_calendar_ics(self) -> None:
        serve = self._serve({"/": {"Proxy": "http://127.0.0.1:8766"}})
        self.assertEqual(self._call(serve), "https://demo.ts.net:8443/calendar.ics")

    def test_dedicated_ics_route_is_still_honoured(self) -> None:
        serve = self._serve({"/notice.ics": {"Proxy": "http://127.0.0.1:8766/calendar.ics"}}, target="demo.ts.net:443")
        self.assertEqual(self._call(serve), "https://demo.ts.net/notice.ics")

    def test_route_for_another_port_or_without_funnel_is_not_public(self) -> None:
        other_port = self._serve({"/": {"Proxy": "http://127.0.0.1:9999"}})
        self.assertEqual(self._call(other_port), "")
        not_funnel = self._serve({"/": {"Proxy": "http://127.0.0.1:8766"}}, funnel=False)
        self.assertEqual(self._call(not_funnel), "")


if __name__ == "__main__":
    unittest.main()
