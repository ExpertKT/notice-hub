"""S2：日历订阅的公网（Tailscale Funnel）项与局域网项的区分。

公网项是唯一「手机用流量也能订阅」的路径，必须排在前面且 hint 说清区别；
funnel 没开时不能凭空给一个假的公网地址。
"""

from __future__ import annotations

import sys
import unittest
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from noticehub import webapp  # noqa: E402

PUBLIC = "https://demo-machine.demo-tailnet.ts.net/calendar.ics"


class AdvertisedCalendarsTest(unittest.TestCase):
    def test_public_entry_comes_first_with_any_network_hint(self) -> None:
        with mock.patch("noticehub.webapp._tailscale_calendar_url", return_value=PUBLIC), mock.patch(
            "noticehub.webapp._lan_hosts", return_value=["192.168.8.10"]
        ):
            calendars = webapp._advertised_calendars(8766, "secret")

        self.assertEqual([item["kind"] for item in calendars], ["public", "lan"])
        public = calendars[0]
        self.assertEqual(public["url"], PUBLIC + "?token=secret")
        self.assertIn("流量", public["hint"])
        lan = calendars[1]
        self.assertEqual(lan["url"], "http://192.168.8.10:8766/calendar.ics?token=secret")
        self.assertIn("同一个网络", lan["hint"])
        self.assertNotEqual(public["hint"], lan["hint"])

    def test_no_public_entry_without_funnel(self) -> None:
        # funnel 没开就别给公网地址，否则用户拿着一个拉不到的订阅地址去加日历。
        with mock.patch("noticehub.webapp._tailscale_calendar_url", return_value=""), mock.patch(
            "noticehub.webapp._lan_hosts", return_value=["192.168.8.10"]
        ):
            calendars = webapp._advertised_calendars(8766, "secret")

        self.assertEqual([item["kind"] for item in calendars], ["lan"])


class SyncTestAllowlistTest(unittest.TestCase):
    """「测试订阅」按钮的 allowlist 必须放行公网项，否则公网地址永远 400。"""

    def _call(self, requested: str) -> tuple[int, dict]:
        handler = object.__new__(webapp._Handler)
        handler.server = SimpleNamespace(server_address=("127.0.0.1", 8766), token="secret")
        handler.path = "/api/sync/test?url=" + urllib.parse.quote(requested, safe="")
        seen: dict = {}
        handler._json = lambda status, payload: seen.update(status=status, payload=payload)
        with mock.patch("noticehub.webapp._tailscale_calendar_url", return_value=PUBLIC), mock.patch(
            "noticehub.webapp._lan_hosts", return_value=[]
        ), mock.patch(
            "noticehub.webapp.urllib.request.build_opener", side_effect=OSError("测试不发真请求")
        ):
            webapp._Handler._route_get_api_sync_test(handler, handler.path)
        return seen["status"], seen["payload"]

    def test_public_calendar_url_passes_sync_test_allowlist(self) -> None:
        # 502 = 过了 allowlist、只是没真去拉；400 才是被挡。
        status, payload = self._call(PUBLIC)
        self.assertEqual(status, 502, payload)
        self.assertIn("读取", payload["error"])

    def test_foreign_calendar_url_is_rejected(self) -> None:
        status, payload = self._call("https://evil.example.com/calendar.ics")
        self.assertEqual(status, 400, payload)


if __name__ == "__main__":
    unittest.main()
