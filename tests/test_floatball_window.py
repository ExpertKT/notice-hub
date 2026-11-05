import os
import unittest

if os.name != "nt":
    raise unittest.SkipTest("Windows-only floating-ball window")

from noticehub.floatball_window import (
    CLOSED,
    MARGIN,
    PANEL_W,
    FloatBallWindow,
    ball_box,
    sliver_box,
    task_rows,
    unread_total,
)


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


class _FakeRoot:
    """Stands in for tk.Tk: records what was scheduled, cancelled and how it was resized."""

    def __init__(self):
        self.scheduled = []
        self.cancelled = []
        self.geometry_value = ""

    def after(self, delay, callback):
        ident = "t%d" % len(self.scheduled)
        self.scheduled.append((delay, ident))
        return ident

    def after_cancel(self, ident):
        self.cancelled.append(ident)

    def geometry(self, value):
        self.geometry_value = value

    def winfo_x(self):
        return 0

    def winfo_y(self):
        return 0

    def quit(self):
        pass

    def destroy(self):
        pass


class _Event:
    def __init__(self, x_root, y_root):
        self.x_root = x_root
        self.y_root = y_root


def _bare_window():
    window = FloatBallWindow.__new__(FloatBallWindow)
    window.root = _FakeRoot()
    window._timers = []
    window._drag_origin = None
    window._drag_moved = False
    window._window_origin = (0, 0)
    window._factor = 1.0
    window.expanded = False
    return window


class LifecycleTests(unittest.TestCase):
    def test_show_cancels_the_previous_timer_chain_instead_of_stacking(self):
        window = _bare_window()
        window._later(1500, lambda: None)
        window._later(120, lambda: None)
        window._later(60000, lambda: None)
        self.assertEqual(len(window._timers), 3)

        window._cancel_timers()

        self.assertEqual(window._timers, [])
        self.assertEqual(window.root.cancelled, ["t0", "t1", "t2"])

        window._cancel_timers()
        self.assertEqual(window.root.cancelled, ["t0", "t1", "t2"], "第二次取消不该再对同一个 id 动手")

    def test_dragging_keeps_the_scaled_size(self):
        window = _bare_window()
        window._factor = 1.5
        window._drag_origin = (0, 0)
        window._window_origin = (100, 100)

        window._on_motion(_Event(40, 60))

        # 高 DPI 下宽高必须跟着 _factor 放，否则一拖动窗口就缩回逻辑尺寸
        self.assertEqual(window.root.geometry_value, "108x108+140+160")

    def test_dragging_an_expanded_panel_uses_the_panel_size(self):
        window = _bare_window()
        window.expanded = True
        window._drag_origin = (0, 0)
        window._window_origin = (10, 20)

        window._on_motion(_Event(30, 30))

        self.assertEqual(window.root.geometry_value, "%dx%d+40+50" % (PANEL_W, 520))

    def test_stop_marks_the_ball_hidden_so_the_next_tray_click_shows_it(self):
        """_do_stop 把 root 置 None 后必须同时把 visible 置 False，否则托盘那一项第一次点击没反应。"""
        import tempfile
        from pathlib import Path

        window = _bare_window()
        tmp = Path(tempfile.mkdtemp(prefix="fb-stop-"))
        window.data_dir = tmp
        window.state_path = tmp / "floatball-state.json"
        window.visible = True

        window._do_stop()

        self.assertFalse(window.visible)
        self.assertIsNone(window.root)


class CalibrationTests(unittest.TestCase):
    def test_calibration_survives_the_argtypes_floatball_host_installs(self):
        """floatball_host 会把 user32.GetWindowRect 的 argtypes 定成 POINTER(RECT)，user32 是共享句柄；
        这里传旧的 c_long*4 就会抛 ArgumentError（冒出去、_factor 停在 1.0），高 DPI 下球和面板全按错尺寸摆。"""
        import ctypes

        from noticehub import floatball_host  # noqa: F401 - 导入本身就会改 argtypes

        class DesktopRoot(_FakeRoot):
            def update_idletasks(self):
                pass

            def winfo_id(self):
                return ctypes.windll.user32.GetDesktopWindow()

        window = FloatBallWindow.__new__(FloatBallWindow)
        window.root = DesktopRoot()
        window._factor = 1.0

        window._calibrate()  # 抛出 ArgumentError 就是回归

        self.assertIsInstance(window._factor, float)
        self.assertGreater(window._factor, 0.0)
        self.assertNotEqual(window._factor, 1.0, "没读到真实窗口尺寸时不该停在 1.0")


if __name__ == "__main__":
    unittest.main()
