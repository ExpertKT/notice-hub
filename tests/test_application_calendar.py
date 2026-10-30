"""日历排序和 primary 选取。不探测网卡，地址列表由测试直接传入。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from noticehub.application.calendar import (  # noqa: E402
    assemble_calendars,
    describe_calendar_subscription,
    host_priority,
    is_virtual_host,
    primary_calendar_url,
)


class CalendarOrderTest(unittest.TestCase):
    def test_public_wins_and_stays_first(self) -> None:
        calendars = assemble_calendars(
            ["192.168.8.10", "172.27.32.1"],
            "https://abc.ts.net/calendar.ics",
            port=8766,
            token="secret",
        )
        self.assertEqual(primary_calendar_url(calendars), "https://abc.ts.net/calendar.ics?token=secret")
        self.assertEqual(calendars[0]["kind"], "public")
        self.assertFalse(calendars[0]["virtual"])
        self.assertIn("流量", calendars[0]["hint"])

    def test_real_lan_beats_tailscale_and_virtual(self) -> None:
        hosts = ["100.78.12.25", "100.127.59.175", "172.27.32.1", "192.168.56.1", "10.4.5.6", "192.168.8.10"]
        calendars = assemble_calendars(hosts, "", port=8766, token="secret")
        self.assertEqual(
            primary_calendar_url(calendars),
            "http://192.168.8.10:8766/calendar.ics?token=secret",
        )
        self.assertEqual(
            [item["url"].split("//")[1].split(":")[0] for item in calendars],
            ["192.168.8.10", "10.4.5.6", "100.127.59.175", "100.78.12.25", "172.27.32.1", "192.168.56.1"],
        )
        self.assertTrue(is_virtual_host("172.27.32.1"))
        self.assertTrue(is_virtual_host("192.168.56.1"))
        self.assertFalse(is_virtual_host("192.168.8.10"))
        self.assertFalse(is_virtual_host("172.16.1.1"))
        self.assertEqual(calendars[-1]["virtual"], True)
        self.assertEqual(calendars[-1]["hint"], "虚拟机/容器网卡，手机连不到")

    def test_same_tier_sorts_by_address_text_and_ignores_input_order(self) -> None:
        hosts = ["192.168.8.10", "10.4.5.6", "192.168.8.9", "100.78.12.25"]
        forward = [item["url"] for item in assemble_calendars(hosts, port=8766, token="secret")]
        backward = [item["url"] for item in assemble_calendars(list(reversed(hosts)), port=8766, token="secret")]
        self.assertEqual(forward, backward)
        self.assertLess(host_priority("192.168.8.10"), host_priority("192.168.8.9"))
        self.assertLess(host_priority("100.127.0.1"), host_priority("100.128.0.1"))

    def test_only_virtual_falls_back_to_first(self) -> None:
        calendars = assemble_calendars(["172.27.32.1", "192.168.56.1"], port=8766, token="secret")
        primary = primary_calendar_url(calendars)
        self.assertEqual(primary, calendars[0]["url"])
        self.assertIn(":8766/calendar.ics", primary)

    def test_describe_stamps_events_and_lan_base(self) -> None:
        info = describe_calendar_subscription(
            ["10.0.0.8", "192.168.1.2"],
            "https://demo.ts.net/calendar.ics",
            port=8766,
            token="secret",
            events=3,
        )
        self.assertEqual(
            set(info),
            {"ok", "primary", "lan_base", "port", "events", "calendars"},
        )
        self.assertTrue(info["ok"])
        self.assertEqual(info["events"], 3)
        self.assertEqual(info["port"], 8766)
        self.assertEqual(info["primary"], "https://demo.ts.net/calendar.ics?token=secret")
        self.assertEqual(info["lan_base"], "http://192.168.1.2:8766")
        self.assertTrue(all(item["events"] == 3 for item in info["calendars"]))
        self.assertEqual({item["kind"] for item in info["calendars"]}, {"public", "lan"})

    def test_empty_probe_has_no_calendars(self) -> None:
        info = describe_calendar_subscription([], "", port=8766, token="secret", events=0)
        self.assertEqual(info["calendars"], [])
        self.assertEqual(info["primary"], "")
        self.assertEqual(info["lan_base"], "")
