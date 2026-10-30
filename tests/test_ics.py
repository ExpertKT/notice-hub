"""issue #6：ICS 的 DTSTART 必须是上海时间，不能把机器本地时间直接贴 Asia/Shanghai 标签。

铁证：同一个 VEVENT 里 DTSTAMP 是换算过的 UTC，DTSTART 却是本地 naive 数值 + 上海标签。
在 UTC 的机器（Docker 默认）上，所有带时刻的日程会整体提前 8 小时。
"""

from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

from qq_live_digest import ics


TASKS = [
    {"id": "1", "deadline": "2026-10-29T15:00:00", "summary": "交实验报告", "status": "open"},
    {"id": "2", "deadline": "2026-10-30", "summary": "只有日期", "status": "open"},
]


def _line(text: str, prefix: str) -> str:
    for line in text.split("\r\n"):
        if line.startswith(prefix):
            return line
    raise AssertionError(f"没有 {prefix} 行：\n{text}")


class CstConversionTests(unittest.TestCase):
    def test_utc_machine_time_is_shifted_forward_by_eight_hours(self):
        # UTC 15:00 就是上海 23:00 同一天
        self.assertEqual(
            ics._to_cst_naive(dt.datetime(2026, 10, 29, 15, 0), local_tz=dt.timezone.utc),
            dt.datetime(2026, 10, 29, 23, 0),
        )

    def test_shanghai_machine_time_is_unchanged(self):
        shanghai = dt.timezone(dt.timedelta(hours=8))
        self.assertEqual(
            ics._to_cst_naive(dt.datetime(2026, 10, 29, 15, 0), local_tz=shanghai),
            dt.datetime(2026, 10, 29, 15, 0),
        )

    def test_aware_input_is_converted_not_reinterpreted(self):
        # 带时区的输入直接换算：UTC 07:00 → 上海 15:00，不会再加一次本地偏移
        aware = dt.datetime(2026, 10, 29, 7, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(ics._to_cst_naive(aware), dt.datetime(2026, 10, 29, 15, 0))


class RenderCalendarTimeTests(unittest.TestCase):
    def _render(self, *, local_tz: dt.tzinfo) -> str:
        with mock.patch.object(ics, "_local_tzinfo", return_value=local_tz):
            return ics.render_calendar(TASKS, now=dt.datetime(2026, 10, 29, 8, 0))

    def test_dtstart_matches_the_shanghai_label_on_a_utc_machine(self):
        """修好之前这里是 20261029T150000（本地值贴上海标签）；修好后必须是 230000。"""
        text = self._render(local_tz=dt.timezone.utc)
        self.assertIn("DTSTART;TZID=Asia/Shanghai:20261029T230000", text)
        self.assertNotIn("DTSTART;TZID=Asia/Shanghai:20261029T150000", text)

    def test_dtstart_is_unchanged_when_machine_is_already_shanghai(self):
        shanghai = dt.timezone(dt.timedelta(hours=8))
        text = self._render(local_tz=shanghai)
        self.assertIn("DTSTART;TZID=Asia/Shanghai:20261029T150000", text)

    def test_value_date_branch_has_no_timezone_shift(self):
        """纯日期没有时刻：VALUE=DATE 的数值必须与原来一模一样。"""
        text = self._render(local_tz=dt.timezone.utc)
        self.assertIn("DTSTART;VALUE=DATE:20261030", text)
        self.assertIn("DTEND;VALUE=DATE:20261031", text)
        self.assertNotIn("TZID=Asia/Shanghai:2026103", text)

    def test_dtstamp_stays_utc(self):
        """DTSTAMP 一直是 UTC（带 Z），修 DTSTART 不能把它带偏。"""
        text = self._render(local_tz=dt.timezone.utc)
        stamp = _line(text, "DTSTAMP:")
        self.assertTrue(stamp.endswith("Z"), stamp)
        # 与机器时区无关地验证：DTSTAMP 必须等于同一时刻换算出的 UTC（不做本地解释）
        self.assertEqual(stamp, "DTSTAMP:" + ics._utc_stamp(dt.datetime(2026, 10, 29, 8, 0)))
        self.assertNotIn("DTSTAMP;TZID", text, "DTSTAMP 不允许带 TZID，必须是 UTC")

    def test_calendar_timezone_declarations_stay_shanghai(self):
        text = self._render(local_tz=dt.timezone.utc)
        self.assertIn("X-WR-TIMEZONE:Asia/Shanghai", text)
        self.assertIn("TZID:Asia/Shanghai", text)
        self.assertIn("TZOFFSETTO:+0800", text)


if __name__ == "__main__":
    unittest.main()
