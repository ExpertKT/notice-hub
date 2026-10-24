# -*- coding: utf-8 -*-
"""event_extract 的最小测试：正向样本 + 负样本（时间/location/置信度）。"""

from __future__ import annotations

import datetime as dt
import unittest

from qq_live_digest.event_extract import extract_events

NOW = dt.datetime(2026, 10, 23, 9, 0, 0)  # 周五


class ExtractEventsTests(unittest.TestCase):
    def test_full_sentence(self):
        got = extract_events("明天下午三点半在A楼302开会", now=NOW)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["when_start"], "2026-10-24T15:30:00")
        self.assertIsNone(got[0]["when_end"])
        self.assertEqual(got[0]["where"], "A楼302")
        self.assertEqual(got[0]["what"], "在A楼302开会")
        self.assertEqual(got[0]["confidence"], "high")

    def test_weekday_morning(self):
        got = extract_events("下周一早上8点集合", now=NOW)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["when_start"], "2026-10-26T08:00:00")
        self.assertIsNone(got[0]["where"])

    def test_deadline_end_of_day(self):
        got = extract_events("本周五前交作业", now=NOW)
        self.assertEqual(got[0]["when_start"], "2026-10-23T23:59:00")

    def test_clock_range(self):
        got = extract_events("明天上午10点到11点半在会议室开会", now=NOW)
        self.assertEqual(got[0]["when_start"], "2026-10-24T10:00:00")
        self.assertEqual(got[0]["when_end"], "2026-10-24T11:30:00")

    def test_month_day(self):
        got = extract_events("11月3号下午2点在报告厅开答辩会", now=NOW)
        self.assertEqual(got[0]["when_start"], "2026-11-03T14:00:00")

    def test_month_day_rollforward(self):
        got = extract_events("1月5号上午9点体检", now=NOW)
        self.assertEqual(got[0]["when_start"], "2027-01-05T09:00:00")

    def test_clock_only_passed_goes_next_day(self):
        got = extract_events("早上8点集合", now=NOW)
        self.assertEqual(got[0]["when_start"], "2026-10-24T08:00:00")

    def test_place_only_low_confidence(self):
        got = extract_events("在302教室", now=NOW)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["confidence"], "low")
        self.assertIsNone(got[0]["when_start"])

    def test_min_level_filters_low(self):
        self.assertEqual(extract_events("在302教室", now=NOW, min_level="medium"), [])

    def test_chinese_clock_and_period(self):
        got = extract_events("明天晚上八点半在食堂聚餐", now=NOW)
        self.assertEqual(got[0]["when_start"], "2026-10-24T20:30:00")

    def test_negative_past(self):
        self.assertEqual(extract_events("上次开会说了这件事", now=NOW), [])

    def test_negative_question(self):
        self.assertEqual(extract_events("明天几点开会？", now=NOW), [])

    def test_negative_plain_text(self):
        self.assertEqual(extract_events("这个项目需要大家配合", now=NOW), [])

    def test_word_after_event_date_keeps_closest_time(self):
        got = extract_events("今天下午三点后到办公室找我", now=NOW)
        self.assertEqual(got[0]["when_start"], "2026-10-23T15:00:00")

    def test_end_only_clock(self):
        got = extract_events("明天到5点结束培训", now=NOW)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["when_start"], "2026-10-24T09:00:00")
        self.assertEqual(got[0]["when_end"], "2026-10-24T17:00:00")
        self.assertIsNone(got[0]["where"])

    def test_clock_range_with_end_marker(self):
        got = extract_events("明天9点到5点结束培训", now=NOW)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["when_start"], "2026-10-24T09:00:00")
        self.assertEqual(got[0]["when_end"], "2026-10-24T17:00:00")
        self.assertIsNone(got[0]["where"])

    def test_weekday_range_with_place(self):
        got = extract_events("下周三10点至12点在B楼201开会", now=NOW)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["when_start"], "2026-10-28T10:00:00")
        self.assertEqual(got[0]["when_end"], "2026-10-28T12:00:00")
        self.assertEqual(got[0]["where"], "B楼201")

    def test_afternoon_range_no_date(self):
        got = extract_events("下午两点到四点半审核", now=NOW)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["when_start"], "2026-10-23T14:00:00")
        self.assertEqual(got[0]["when_end"], "2026-10-23T16:30:00")
        self.assertIsNone(got[0]["where"])

    def test_no_store_side_effects(self):
        # 只验证模块未引用 store/summarizer（守边界用，正常应通过）。
        import qq_live_digest.event_extract as module
        with open(module.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("import store", source)
        self.assertNotIn("from .store", source)


if __name__ == "__main__":
    unittest.main()
