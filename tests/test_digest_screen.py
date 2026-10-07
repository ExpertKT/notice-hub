import datetime as dt
import unittest
from qq_digest import Message, analyze_message, extract_deadline

class DigestScreenTests(unittest.TestCase):
    def msg(self, text):
        return Message(dt.datetime(2026, 10, 7, 9), '教务老师', text, 'test')
    def test_attachment_filename_is_not_notice(self):
        result = analyze_message(self.msg('[文件]截屏2026-10-07 16.33.44.png'))
        self.assertLess(result['score'], 0)
        self.assertIsNone(result['deadline'])
    def test_image_placeholder_is_not_notice(self):
        result = analyze_message(self.msg('[图片]'))
        self.assertLess(result['score'], 0)
        self.assertIsNone(result['deadline'])
    def test_real_notice_keeps_deadline(self):
        result = analyze_message(self.msg('同学们，下次课（10月10日）需要提交第一次课内实践作业，请全体成员完成'))
        self.assertGreater(result['score'], 0)
        self.assertIsNotNone(result['deadline'])
        self.assertEqual(result['deadline'].date(), dt.date(2026, 10, 10))
    def test_attachment_plus_text_keeps_deadline(self):
        result = analyze_message(self.msg('[文件]作业要求.pdf 请周五24点前提交'))
        self.assertGreater(result['score'], 0)
        self.assertIsNotNone(result['deadline'])

if __name__ == '__main__':
    unittest.main()
