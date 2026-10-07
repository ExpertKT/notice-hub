import datetime as dt
import unittest

from qq_digest import Message, extract_deadline


REFERENCE = dt.datetime(2026, 10, 7, 21, 0)


class RelativeDeadlineRegressionTests(unittest.TestCase):
    def deadline(self, text):
        return extract_deadline(Message(REFERENCE, "tester", text))

    def test_regressions(self):
        cases = {
            "10月10日课前提交第一次实践作业": dt.datetime(2026, 10, 10, 23, 59),
            "今晚23:59前填写腾讯文档": dt.datetime(2026, 10, 7, 23, 59),
            "下周周三前把实验报告交到学习委员那里": dt.datetime(2026, 10, 14, 23, 59),
            "今天的军训心得明早8点前发给班长": dt.datetime(2026, 10, 8, 8, 0),
            "哈哈哈这个梗图太搞笑了": None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.deadline(text), expected)

    def test_weekday_spellings(self):
        cases = {
            "下周三前交实验报告": dt.datetime(2026, 10, 14, 23, 59),
            "周三前交": dt.datetime(2026, 10, 7, 23, 59),
            "星期五前交": dt.datetime(2026, 10, 9, 23, 59),
            "本周五前交": dt.datetime(2026, 10, 9, 23, 59),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.deadline(text), expected)


if __name__ == "__main__":
    unittest.main()
