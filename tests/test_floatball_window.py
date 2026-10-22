import os
import unittest

if os.name != "nt":
    raise unittest.SkipTest("Windows-only floating-ball window")

from qq_live_digest.floatball_window import CLOSED, MARGIN, ball_box, sliver_box, task_rows, unread_total


class TaskRowsTests(unittest.TestCase):
    def test_flattens_groups_in_priority_order(self):
        payload = {
            "pinned": [{"id": "1", "summary": "置顶"}],
            "today": [{"id": "2", "summary": "今天"}],
            "week": [{"id": "3", "summary": "本周"}],
            "later": [{"id": "4", "summary": "以后"}],
            "done": [{"id": "5", "summary": "完成"}],
            "candidates": [{"msg_id": "m1"}],
        }
        self.assertEqual([row["id"] for row in task_rows(payload)], ["1", "2", "3", "4"])
        self.assertEqual(unread_total(payload), 5)

    def test_drops_done_and_dismissed(self):
        payload = {
            "today": [
                {"id": "1", "done": True},
                {"id": "2", "status": "dismissed"},
                {"id": "3", "status": "open"},
                "not-a-dict",
            ]
        }
        self.assertEqual([row["id"] for row in task_rows(payload)], ["3"])

    def test_empty_payload_is_empty(self):
        self.assertEqual(task_rows({}), [])
        self.assertEqual(unread_total({}), 0)


class EdgeStateTests(unittest.TestCase):
    WORK = (0, 0, 2560, 1440)

    def test_sliver_sits_flush_against_each_edge(self):
        ball = (2480, 1304)
        x, y, width, height = sliver_box("right", ball, self.WORK)
        self.assertEqual((width, height), (10, 64))
        self.assertEqual(x + width, self.WORK[2])
        self.assertEqual(y + height // 2, ball[1] + CLOSED // 2)

        x, y, width, height = sliver_box("bottom", ball, self.WORK)
        self.assertEqual((width, height), (64, 10))
        self.assertEqual(y + height, self.WORK[3])

    def test_ball_comes_back_with_margin(self):
        sliver = sliver_box("right", (2480, 1304), self.WORK)
        x, y = ball_box("right", (sliver[0], sliver[1]), self.WORK)
        self.assertEqual(x + CLOSED, self.WORK[2] - MARGIN)
        self.assertLessEqual(abs(y - (1304 + CLOSED // 2 - CLOSED // 2)), 8)

    def test_round_trip_keeps_the_same_edge(self):
        ball = (2480, 1304)
        sliver = sliver_box("right", ball, self.WORK)
        back = ball_box("right", (sliver[0], sliver[1]), self.WORK)
        again = sliver_box("right", back, self.WORK)
        self.assertEqual(sliver, again)


if __name__ == "__main__":
    unittest.main()
