"""公网入口状态机（qq_live_digest/tunnel.py）的行为测试。

只测「阶段怎么走、失败怎么落到用户能看懂的文案」，不碰真实 Tailscale、不联网、不提权。
"""

from __future__ import annotations

import unittest
from unittest import mock

from qq_live_digest import tunnel


class FunnelSetupStateMachineTest(unittest.TestCase):
    def _setup(self) -> tunnel.FunnelSetup:
        return tunnel.FunnelSetup(8766)

    def test_missing_tailscale_downloads_and_installs_then_enables(self) -> None:
        setup = self._setup()
        with (
            mock.patch("qq_live_digest.tunnel.tailscale_executable", side_effect=["", "tailscale"]),
            mock.patch("qq_live_digest.tunnel.download_installer", return_value=r"C:\tmp\ts.exe") as download,
            mock.patch("qq_live_digest.tunnel.run_installer") as install,
            mock.patch("qq_live_digest.tunnel.read_status", return_value={"BackendState": "Running"}),
            mock.patch("qq_live_digest.tunnel.enable_funnel", return_value={"ok": True, "url": "https://demo.ts.net:8443"}),
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
            mock.patch("qq_live_digest.tunnel.tailscale_executable", return_value=""),
            mock.patch("qq_live_digest.tunnel.download_installer", return_value="ts.exe"),
            mock.patch("qq_live_digest.tunnel.run_installer", side_effect=OSError("管理员授权被取消")),
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
            mock.patch("qq_live_digest.tunnel.tailscale_executable", return_value="tailscale"),
            mock.patch("qq_live_digest.tunnel.read_status", return_value={"BackendState": "NeedsLogin", "AuthURL": "https://login"}),
            mock.patch("qq_live_digest.tunnel.start_login", return_value=process) as login,
            mock.patch("qq_live_digest.tunnel.FunnelSetup._wait_login", return_value=True),
            mock.patch("qq_live_digest.tunnel.POLL_SECONDS", 0),
            mock.patch("qq_live_digest.tunnel.enable_funnel", return_value={"ok": True, "url": "https://demo.ts.net"}),
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
            mock.patch("qq_live_digest.tunnel.tailscale_executable", return_value="tailscale"),
            mock.patch("qq_live_digest.tunnel.read_status", return_value={"BackendState": "NeedsLogin"}),
            mock.patch("qq_live_digest.tunnel.start_login", return_value=process),
            mock.patch("qq_live_digest.tunnel.FunnelSetup._wait_login", side_effect=fail_login),
            mock.patch("qq_live_digest.tunnel.enable_funnel") as enable,
        ):
            setup.run()

        self.assertFalse(enable.called)
        self.assertEqual(setup.snapshot()["state"], "error")
        self.assertIn("登录", setup.snapshot()["message"])
        process.terminate.assert_called_once()

    def test_funnel_rejection_becomes_error(self) -> None:
        setup = self._setup()
        with (
            mock.patch("qq_live_digest.tunnel.tailscale_executable", return_value="tailscale"),
            mock.patch("qq_live_digest.tunnel.read_status", return_value={"BackendState": "Running"}),
            mock.patch("qq_live_digest.tunnel.enable_funnel", return_value={"ok": False, "error": "Tailscale 账户还没开启 Funnel"}),
        ):
            setup.run()

        self.assertEqual(setup.snapshot()["state"], "error")
        self.assertIn("Funnel", setup.snapshot()["message"])

    def test_start_is_idempotent_while_running_and_after_ready(self) -> None:
        setup = self._setup()
        with mock.patch("qq_live_digest.tunnel.FunnelSetup.run"):
            first = setup.start()
            thread = setup._thread
            second = setup.start()
        self.assertEqual(first["state"], "installing")
        self.assertEqual(second["state"], "installing")
        # 已经在跑就不再起第二个线程（页面连点「重试」不会叠出多份安装）。
        self.assertIs(setup._thread, thread)

    def test_enable_funnel_skips_commands_when_not_logged_in(self) -> None:
        with (
            mock.patch("qq_live_digest.tunnel.tailscale_executable", return_value="tailscale"),
            mock.patch("qq_live_digest.tunnel.status_hint", return_value="Tailscale 还没登录"),
            mock.patch("qq_live_digest.tunnel.subprocess.run") as run,
        ):
            result = tunnel.enable_funnel(8766)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "Tailscale 还没登录")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
