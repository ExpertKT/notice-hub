"""`noticehub/floatball_win.py` 的用例：几何与命中测试是纯函数，不需要显示器；
Win32 相关只测不建窗口的部分（窗口一建出来屏幕上就会闪一下，测试里绝不许建）。

`FloatBallWindow.__init__` 在非 Windows 上直接拒绝构造，所以验窗口内部状态
（帧字节序、锚点漂移、回调兜底）的用例标了 WINDOWS_ONLY —— 这些行为由 Windows
上的 unittest job 覆盖，不是没人看着。
"""

import ctypes
import sys
import unittest
from ctypes import wintypes
from unittest import mock

from noticehub.floatball_win import (
    HTCLIENT,
    HTTRANSPARENT,
    MA_NOACTIVATE,
    WM_MOUSEACTIVATE,
    WM_MOUSEWHEEL,
    WM_NCHITTEST,
    FloatBallWindow,
    _unpack_lparam,
    alpha_hit,
    anchor_xy,
    wheel_notches,
)

WINDOWS_ONLY = unittest.skipUnless(sys.platform == "win32", "只有 Windows 有窗口")


def circle_alpha(size=72, radius=36):
    """画一个居中的实心圆（不透明），圆外全透明，返回 RGBA 逐行字节。"""
    center = size / 2.0
    data = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            dx = x + 0.5 - center
            dy = y + 0.5 - center
            if dx * dx + dy * dy <= radius * radius:
                offset = (y * size + x) * 4
                data[offset:offset + 4] = b"\x00\x00\x00\xff"
    return bytes(data)


class AlphaHitTests(unittest.TestCase):
    def setUp(self):
        self.size = 72
        self.alpha = circle_alpha(self.size, 36)

    def hit(self, x, y):
        return alpha_hit(self.alpha, self.size, self.size, x, y)

    def test_center_of_the_ball_is_hit(self):
        self.assertTrue(self.hit(36, 36))

    def test_four_corners_are_transparent(self):
        for x, y in ((0, 0), (71, 0), (0, 71), (71, 71)):
            self.assertFalse(self.hit(x, y), "角 (%d,%d) 应该是透明的" % (x, y))

    def test_ball_edge_is_hit(self):
        self.assertTrue(self.hit(36, 0))
        self.assertTrue(self.hit(36, 71))
        self.assertTrue(self.hit(0, 36))
        self.assertTrue(self.hit(71, 36))

    def test_outside_the_frame_is_not_a_hit(self):
        self.assertFalse(self.hit(-1, 10))
        self.assertFalse(self.hit(10, -1))
        self.assertFalse(self.hit(self.size, 10))
        self.assertFalse(self.hit(10, self.size))

    def test_empty_frame_is_not_a_hit(self):
        self.assertFalse(alpha_hit(b"", 0, 0, 0, 0))
        self.assertFalse(alpha_hit(b"", 72, 72, 36, 36))

    def test_fully_transparent_frame_is_not_a_hit(self):
        self.assertFalse(alpha_hit(bytes(72 * 72 * 4), 72, 72, 36, 36))

    def test_frame_smaller_than_expected_does_not_raise(self):
        # 帧数据比声明尺寸短时按不命中处理，不能抛 IndexError
        self.assertFalse(alpha_hit(bytes(8), 72, 72, 36, 36))


class AnchorTests(unittest.TestCase):
    def test_bottom_right_anchor_keeps_the_corner(self):
        position = anchor_xy((100, 100), (72, 72), (320, 420), "br")
        self.assertEqual(position, (100 + 72 - 320, 100 + 72 - 420))
        # 右下角不动
        self.assertEqual(position[0] + 320, 100 + 72)
        self.assertEqual(position[1] + 420, 100 + 72)

    def test_top_left_anchor_keeps_the_origin(self):
        self.assertEqual(anchor_xy((100, 100), (72, 72), (320, 420), "tl"), (100, 100))

    def test_default_anchor_is_bottom_right(self):
        self.assertEqual(anchor_xy((100, 100), (72, 72), (320, 420)), (-148, -248))

    def test_unknown_anchor_is_rejected(self):
        with self.assertRaises(ValueError):
            anchor_xy((100, 100), (72, 72), (320, 420), "center")


class MouseLParamTests(unittest.TestCase):
    def test_splits_positive_coordinates(self):
        self.assertEqual(_unpack_lparam((200 << 16) | 300), (300, 200))

    def test_splits_negative_coordinates(self):
        packed = ((-1 & 0xFFFF) << 16) | (10 & 0xFFFF)
        self.assertEqual(_unpack_lparam(packed), (10, -1))


@WINDOWS_ONLY
class FrameBytesTests(unittest.TestCase):
    """32bpp BI_RGB 的 DIB 字节序是 B,G,R,A。存成 R,G,B,A 会把红蓝互换
    （实机探针贴纯红，屏幕上出来是纯蓝），这里锁住导出的字节序。"""

    def test_pure_red_frame_is_stored_as_bgra(self):
        from PIL import Image

        window = FloatBallWindow(lambda x, y: None, lambda x, y: None)
        try:
            window.set_frame(Image.new("RGBA", (4, 4), (255, 0, 0, 255)), anchor="tl")
            data = window._pending[0]
            self.assertEqual(data[:4], b"\x00\x00\xff\xff", "纯红必须是 B=0 G=0 R=255 A=255")
        finally:
            window.close()

    def test_pure_blue_frame_is_stored_as_bgra(self):
        from PIL import Image

        window = FloatBallWindow(lambda x, y: None, lambda x, y: None)
        try:
            window.set_frame(Image.new("RGBA", (4, 4), (0, 0, 255, 255)), anchor="tl")
            self.assertEqual(window._pending[0][:4], b"\xff\x00\x00\xff")
        finally:
            window.close()

    def test_non_rgba_image_is_converted_before_export(self):
        from PIL import Image

        window = FloatBallWindow(lambda x, y: None, lambda x, y: None)
        try:
            window.set_frame(Image.new("RGB", (4, 4), (255, 0, 0)), anchor="tl")
            self.assertEqual(window._pending[0][:4], b"\x00\x00\xff\xff")
        finally:
            window.close()


@WINDOWS_ONLY
class AnchorDriftTests(unittest.TestCase):
    """宿主连推两帧时，第二帧必须拿第一帧算出的位置做基准；
    否则右下角逐帧平移（收起球 ↔ 展开面板的动画会飘）。"""

    def setUp(self):
        from PIL import Image

        self.window = FloatBallWindow(lambda x, y: None, lambda x, y: None)
        self.Image = Image
        self.window._position = (300, 300)
        self.window.set_frame(Image.new("RGBA", (72, 72), (0, 0, 0, 255)), anchor="br")

    def tearDown(self):
        self.window.close()

    def corner(self):
        x, y = self.window.position()
        width, height = self.window._frame_size
        return x + width, y + height

    def test_consecutive_frames_keep_the_bottom_right_corner(self):
        self.assertEqual(self.corner(), (372, 372))
        # 中间不 pump 消息循环：模拟 16ms 内连推两帧，_paint 还没来得及回写位置
        self.window.set_frame(self.Image.new("RGBA", (200, 260), (0, 0, 0, 255)), anchor="br")
        self.assertEqual(self.corner(), (372, 372), "第二帧的右下角不能漂")
        self.window.set_frame(self.Image.new("RGBA", (300, 420), (0, 0, 0, 255)), anchor="br")
        self.assertEqual(self.corner(), (372, 372), "第三帧的右下角不能漂")

    def test_growing_back_keeps_the_corner(self):
        self.window.set_frame(self.Image.new("RGBA", (300, 420), (0, 0, 0, 255)), anchor="br")
        self.window.set_frame(self.Image.new("RGBA", (72, 72), (0, 0, 0, 255)), anchor="br")
        self.assertEqual(self.corner(), (372, 372), "收回到球也不能漂")


@WINDOWS_ONLY
class SafeCallTests(unittest.TestCase):
    """回调抛异常不能把消息循环带走，但也不许静默吞掉。"""

    def test_callback_error_is_logged_and_swallowed(self):
        window = FloatBallWindow(lambda x, y: None, lambda x, y: None)
        try:
            def boom(*args):
                raise RuntimeError("回调炸了")

            with self.assertLogs("noticehub.floatball_win", level="ERROR") as logs:
                window._safe_call(boom, 1, 2)
            self.assertIn("浮球回调失败", "\n".join(logs.output))
            self.assertIn("回调炸了", "\n".join(logs.output))
        finally:
            window.close()

    def test_healthy_callback_still_runs(self):
        window = FloatBallWindow(lambda x, y: None, lambda x, y: None)
        seen = []
        try:
            window._safe_call(lambda a, b: seen.append((a, b)), 3, 4)
        finally:
            window.close()
        self.assertEqual(seen, [(3, 4)])


@WINDOWS_ONLY
class DragProcTests(unittest.TestCase):
    """拖动分支：注入假的 `_cursor_screen`，不依赖真光标。

    这台机器上 SetCursorPos 返回 0、mouse_event 也挪不动光标（会话不允许合成
    光标位移），所以实机探针驱动不了拖动；这里用注入坐标把窗口过程走一遍。
    """

    PRESS = (736, 436)

    def setUp(self):
        from PIL import Image

        from noticehub import floatball_win

        self.drag_ends = []
        self.clicks = []
        self.window = FloatBallWindow(
            on_click=lambda x, y: self.clicks.append((x, y)),
            on_drag_end=lambda x, y: self.drag_ends.append((x, y)),
        )
        self.window.set_frame(Image.new("RGBA", (72, 72), (0, 0, 0, 255)), anchor="tl")
        self.window._position = (700, 400)
        self.cursor = self.PRESS
        self.window._cursor_screen = lambda: self.cursor
        # 假句柄：只为让 _move_to 走完 SetWindowPos 与位置回写，不建真窗口。
        self.window._hwnd = 0x1234
        self.patches = [
            mock.patch.object(floatball_win.user32, "SetWindowPos"),
            mock.patch.object(floatball_win.user32, "SetCapture"),
            mock.patch.object(floatball_win.user32, "ReleaseCapture"),
        ]
        for item in self.patches:
            item.start()
        self.floatball_win = floatball_win

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.window._hwnd = 0
        self.window.close()

    def send(self, message, client_x, client_y, wparam=0):
        packed = ((client_y & 0xFFFF) << 16) | (client_x & 0xFFFF)
        return self.window._window_proc(0, message, wparam, packed)

    def test_drag_ends_with_one_callback_and_matches_the_cursor_delta(self):
        self.send(self.floatball_win.WM_LBUTTONDOWN, 36, 36, 0x0001)
        for dx, dy in ((12, 8), (30, 20), (48, 32)):
            self.cursor = (self.PRESS[0] + dx, self.PRESS[1] + dy)
            self.send(self.floatball_win.WM_MOUSEMOVE, 36 + dx, 36 + dy, 0x0001)
        self.send(self.floatball_win.WM_LBUTTONUP, 36 + 48, 36 + 32)

        self.assertEqual(self.drag_ends, [(self.PRESS[0] + 48, self.PRESS[1] + 32)],
                         "松手必须只回调一次 on_drag_end，坐标是光标位置")
        self.assertEqual(self.clicks, [], "拖动过程中不许触发 on_click")
        self.assertEqual(self.window.position(), (700 + 48, 400 + 32),
                         "窗口位移必须等于光标位移")

    def test_small_move_still_counts_as_a_click(self):
        self.send(self.floatball_win.WM_LBUTTONDOWN, 36, 36, 0x0001)
        self.cursor = (self.PRESS[0] + 2, self.PRESS[1] + 1)
        self.send(self.floatball_win.WM_MOUSEMOVE, 38, 37, 0x0001)
        self.send(self.floatball_win.WM_LBUTTONUP, 38, 37)

        self.assertEqual(self.drag_ends, [], "抖动小于阈值不算拖动")
        self.assertEqual(self.clicks, [(38, 37)], "应当按点击回调窗口内坐标")
        self.assertEqual(self.window.position(), (700, 400), "点击不许挪窗口")


@WINDOWS_ONLY
class WindowProcTests(unittest.TestCase):
    """直接调窗口过程，不建真窗口（建了会在桌面上闪一下）。"""

    def setUp(self):
        from PIL import Image

        self.window = FloatBallWindow(lambda x, y: None, lambda x, y: None)
        self.size = 72
        image = Image.new("RGBA", (self.size, self.size), (0, 0, 0, 0))
        center = self.size / 2.0
        pixels = image.load()
        for y in range(self.size):
            for x in range(self.size):
                dx = x + 0.5 - center
                dy = y + 0.5 - center
                if dx * dx + dy * dy <= 36 * 36:
                    pixels[x, y] = (0, 0, 0, 255)
        self.window.set_frame(image)

    def tearDown(self):
        self.window.close()

    def test_hit_test_over_the_ball_is_client(self):
        packed = (36 << 16) | 36
        self.assertEqual(self.window._window_proc(0, WM_NCHITTEST, 0, packed), HTCLIENT)

    def test_hit_test_over_transparent_corner_is_transparent(self):
        packed = (0 << 16) | 0
        self.assertEqual(self.window._window_proc(0, WM_NCHITTEST, 0, packed), HTTRANSPARENT)

    def test_clicking_does_not_activate_the_window(self):
        # 硬红线：不许把用户从全屏游戏里弹回桌面
        self.assertEqual(self.window._window_proc(0, WM_MOUSEACTIVATE, 0, 0), MA_NOACTIVATE)

    def test_constructing_and_setting_a_frame_does_not_create_a_window(self):
        self.assertEqual(self.window.hwnd, 0)
        self.assertFalse(self.window.visible)
        self.assertEqual(self.window.position(), (0, 0))


class WheelNotchTests(unittest.TestCase):
    """WM_MOUSEWHEEL 的 wParam 高 16 位是有符号增量，必须按档数交给宿主。"""

    def test_full_notch_up_and_down(self):
        self.assertEqual(wheel_notches(120 << 16), 1)
        self.assertEqual(wheel_notches(((-120) & 0xFFFF) << 16), -1)

    def test_multiple_notches(self):
        self.assertEqual(wheel_notches(360 << 16), 3)
        self.assertEqual(wheel_notches(((-240) & 0xFFFF) << 16), -2)

    def test_partial_notch_from_a_trackpad_still_moves_one_row(self):
        # 触控板发零头（比如 40）时整除会得到 0，滚不动。
        self.assertEqual(wheel_notches(40 << 16), 1)
        self.assertEqual(wheel_notches(((-40) & 0xFFFF) << 16), -1)

    def test_no_movement_is_zero(self):
        self.assertEqual(wheel_notches(0), 0)


@WINDOWS_ONLY
class WheelProcTests(unittest.TestCase):
    """直接调窗口过程，不建真窗口（建了会在桌面上闪一下）。"""

    def setUp(self):
        self.seen = []
        self.window = FloatBallWindow(
            lambda x, y: None, lambda x, y: None, on_wheel=self.seen.append
        )

    def tearDown(self):
        self.window.close()

    def wheel(self, delta):
        wparam = ((delta & 0xFFFF) << 16) | 0
        return self.window._window_proc(0, WM_MOUSEWHEEL, wparam, 0)

    def test_wheel_is_forwarded_to_the_callback(self):
        self.assertEqual(self.wheel(120), 0, "滚轮必须吃掉消息，不能落到 DefWindowProc")
        self.assertEqual(self.wheel(-120), 0)
        self.assertEqual(self.seen, [1, -1], "正数向上、负数向下，按有符号短整型取")

    def test_zero_delta_does_not_call_back(self):
        self.wheel(0)
        self.assertEqual(self.seen, [], "没有位移就不要惊动宿主")

    def test_window_without_a_wheel_callback_does_not_raise(self):
        plain = FloatBallWindow(lambda x, y: None, lambda x, y: None)
        try:
            wparam = (120 & 0xFFFF) << 16
            self.assertEqual(plain._window_proc(0, WM_MOUSEWHEEL, wparam, 0), 0)
        finally:
            plain.close()


@WINDOWS_ONLY
class CtypesSignatureTests(unittest.TestCase):
    """漏设 argtypes/restype 会让窗口静默失败，这里锁住几条关键签名。"""

    def test_update_layered_window_signature_is_declared(self):
        from noticehub import floatball_win

        self.assertEqual(len(floatball_win.user32.UpdateLayeredWindow.argtypes), 9)
        self.assertEqual(
            floatball_win.user32.UpdateLayeredWindow.restype, ctypes.wintypes.BOOL
        )

    def test_create_window_ex_signature_is_declared(self):
        from noticehub import floatball_win

        self.assertEqual(len(floatball_win.user32.CreateWindowExW.argtypes), 12)
        self.assertEqual(
            floatball_win.user32.CreateWindowExW.restype, ctypes.wintypes.HWND
        )

    def test_window_proc_type_is_a_real_callback(self):
        from noticehub import floatball_win

        self.assertTrue(callable(floatball_win.WNDPROC))


if __name__ == "__main__":
    unittest.main()
