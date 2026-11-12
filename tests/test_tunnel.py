"""公网入口状态机（noticehub/tunnel.py）的行为测试。

只测「阶段怎么走、失败怎么落到用户能看懂的文案」，不碰真实 Tailscale、不联网、不提权。
"""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from noticehub import tunnel


class _PingHandler(BaseHTTPRequestHandler):
    """假的公网服务：按用例给定的 (状态码, body) 回 /api/ping。"""

    status = 200
    body = b'{"ok": true, "app": "notice-hub"}'
    delay = 0.0
    paths: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - 基类命名
        _PingHandler.paths.append(self.path)
        if self.delay:
            import time

            time.sleep(self.delay)
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        try:
            self.wfile.write(self.body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass  # 超时用例里客户端已经放弃这条连接，不是被测行为

    def log_message(self, *_args) -> None:
        pass


class ProbePublicTests(unittest.TestCase):
    """`probe_public` 必须真发一次请求：读本机配置证明不了公网可达。"""

    def _serve(self, *, status: int = 200, body: bytes = b'{"ok": true, "app": "notice-hub"}',
               delay: float = 0.0) -> str:
        _PingHandler.status = status
        _PingHandler.body = body
        _PingHandler.delay = delay
        _PingHandler.paths = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), _PingHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:%d" % server.server_address[1]

    def test_probe_reaches_the_liveness_endpoint(self) -> None:
        base = self._serve()
        ok, detail = tunnel.probe_public(base)
        self.assertTrue(ok, detail)
        self.assertEqual(_PingHandler.paths, ["/api/ping"], "必须打 /api/ping")

    def test_probe_fails_with_a_readable_reason(self) -> None:
        cases = {
            "状态码": self._serve(status=500, body=b"{}"),
            "内容不符": self._serve(body=b'{"ok": true, "app": "something-else"}'),
            "不是 JSON": self._serve(body=b"<html>hello</html>"),
        }
        for label, base in cases.items():
            with self.subTest(case=label):
                ok, detail = tunnel.probe_public(base)
                self.assertFalse(ok)
                self.assertTrue(detail, "失败必须说清原因")

    def test_probe_times_out_with_a_readable_reason(self) -> None:
        base = self._serve(delay=1.0)
        ok, detail = tunnel.probe_public(base, timeout=0.2)
        self.assertFalse(ok)
        self.assertIn("没有回应", detail)

    def test_probe_without_a_url_is_not_reachable(self) -> None:
        ok, detail = tunnel.probe_public("")
        self.assertFalse(ok)
        self.assertTrue(detail)


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

    def test_enable_funnel_probes_the_public_url_before_saying_ready(self) -> None:
        """规则写进去 ≠ 公网能通：必须真取过一次 /api/ping 才报成功。"""
        with (
            mock.patch("noticehub.tunnel.tailscale_executable", return_value="tailscale"),
            mock.patch("noticehub.tunnel.status_hint", return_value=""),
            mock.patch("noticehub.tunnel.subprocess.run", return_value=mock.Mock(stdout="")),
            mock.patch("noticehub.tunnel.public_app_url", return_value="https://demo.ts.net"),
            mock.patch("noticehub.tunnel.probe_public", return_value=(True, "公网访问正常")) as probe,
        ):
            result = tunnel.enable_funnel(8766)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["url"], "https://demo.ts.net")
        probe.assert_called_once_with("https://demo.ts.net")

    def test_enable_funnel_reports_error_when_probe_fails(self) -> None:
        with (
            mock.patch("noticehub.tunnel.tailscale_executable", return_value="tailscale"),
            mock.patch("noticehub.tunnel.status_hint", return_value=""),
            mock.patch("noticehub.tunnel.subprocess.run", return_value=mock.Mock(stdout="")),
            mock.patch("noticehub.tunnel.public_app_url", return_value="https://demo.ts.net"),
            mock.patch("noticehub.tunnel.probe_public", return_value=(False, "等了 15 秒没有回应")),
        ):
            result = tunnel.enable_funnel(8766)
        self.assertFalse(result["ok"], "探活失败不许报 ready")
        self.assertIn("规则已写入，但公网访问不通", result["error"])
        self.assertIn("等了 15 秒没有回应", result["error"])


class DisableFunnelTests(unittest.TestCase):
    """关公网：命令退出码不算数，回读配置里那条规则真没了才算关掉。"""

    def test_off_removes_the_rule_and_reports_ok(self) -> None:
        urls = ["https://demo.ts.net:8443", ""]
        with (
            mock.patch("noticehub.tunnel.tailscale_executable", return_value="tailscale"),
            mock.patch("noticehub.tunnel.public_app_url", side_effect=urls),
            mock.patch("noticehub.tunnel.subprocess.run", return_value=mock.Mock(stdout="")) as run,
            mock.patch.object(tunnel, "_SERVE_CACHE", {"tailscale": (0.0, {})}),
        ):
            result = tunnel.disable_funnel(8766)

        self.assertTrue(result["ok"], result)
        self.assertEqual(run.call_args[0][0][1:3], ["funnel", "--https=8443"])
        self.assertEqual(run.call_args[0][0][3], "off")
        self.assertNotIn("tailscale", tunnel._SERVE_CACHE, "关完必须清缓存，否则页面还读旧地址")

    def test_off_that_leaves_the_rule_is_not_ok(self) -> None:
        with (
            mock.patch("noticehub.tunnel.tailscale_executable", return_value="tailscale"),
            mock.patch("noticehub.tunnel.public_app_url", return_value="https://demo.ts.net:8443"),
            mock.patch("noticehub.tunnel.subprocess.run", return_value=mock.Mock(stdout="", stderr=b"")),
        ):
            result = tunnel.disable_funnel(8766)

        self.assertFalse(result["ok"], "规则还在就不能报成功")
        self.assertIn("规则", result["error"])

    def test_off_command_failure_is_reported(self) -> None:
        with (
            mock.patch("noticehub.tunnel.tailscale_executable", return_value="tailscale"),
            mock.patch("noticehub.tunnel.public_app_url", return_value="https://demo.ts.net:8443"),
            mock.patch("noticehub.tunnel.subprocess.run", side_effect=OSError("拒绝访问")),
        ):
            result = tunnel.disable_funnel(8766)

        self.assertFalse(result["ok"])
        self.assertIn("拒绝访问", result["error"])

    def test_off_uses_the_port_the_rule_actually_sits_on(self) -> None:
        # 开着 443 却按默认 8443 去 off，等于关了一条不存在的规则。
        with (
            mock.patch("noticehub.tunnel.tailscale_executable", return_value="tailscale"),
            mock.patch("noticehub.tunnel.public_app_url", side_effect=["https://demo.ts.net", ""]),
            mock.patch("noticehub.tunnel.subprocess.run", return_value=mock.Mock(stdout="")) as run,
        ):
            result = tunnel.disable_funnel(8766)

        self.assertTrue(result["ok"], result)
        self.assertEqual(run.call_args[0][0][2], "--https=443")


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
