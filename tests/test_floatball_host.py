import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if os.name != "nt":
    raise unittest.SkipTest("Windows-only floating-ball host")
from noticehub.floatball_host import (
    CLOSED_SIZE,
    OPEN_SIZE,
    clamp_position,
    snap_position,
    process_matches_commandline,
    parse_title,
    region_radius,
    size_for_title,
    FloatBallHost,
    window_class,
    write_state,
)

class FloatBallHostTests(unittest.TestCase):
    def test_clamps_overflow(self):
        self.assertEqual(clamp_position(-5, 999, 100, 80, (0, 0, 800, 600)), (0, 520))
    def test_snaps_each_edge(self):
        work=(0,0,800,600)
        self.assertEqual(snap_position(2,200,100,80,work)[0], 8)
        self.assertEqual(snap_position(690,200,100,80,work)[0], 692)
        self.assertEqual(snap_position(300,2,100,80,work)[1], 8)
        self.assertEqual(snap_position(300,518,100,80,work)[1], 512)
    def test_state_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            host=FloatBallHost(Path(tmp)); write_state(host.state_path, 12, 34, True)
            self.assertEqual(json.loads(host.state_path.read_text(encoding='utf-8')), {'x':12,'y':34,'expanded':True})
            self.assertEqual(host._load_state()['x'],12)
    def test_title_layout_signal_ignores_loading_titles(self):
        self.assertEqual(size_for_title("nh-floatball:closed"), CLOSED_SIZE)
        self.assertEqual(size_for_title("nh-floatball:open"), OPEN_SIZE)
        self.assertIsNone(size_for_title("http://127.0.0.1:8765/floatball"))
        self.assertIsNone(size_for_title("nh-floatball:unknown"))

    def test_process_commandline_filter(self):
        profile=Path(r'F:\probe\floatball-profile')
        self.assertTrue(process_matches_commandline('msedge.exe --user-data-dir=F:\\probe\\floatball-profile', profile))
        self.assertFalse(process_matches_commandline('msedge.exe --user-data-dir=F:\\other', profile))

    def test_window_class_of_invalid_handle_is_empty(self):
        self.assertEqual(window_class(0), "")

    def test_region_radius(self):
        self.assertEqual(region_radius(False, 72), 36)
        self.assertEqual(region_radius(True, 600), 16)

    def test_parse_title_reads_viewport(self):
        self.assertEqual(parse_title("nh-floatball:closed:72x41"), ("closed", 72, 41))
        self.assertEqual(parse_title("nh-floatball:open"), ("open", 0, 0))
        self.assertIsNone(parse_title("127.0.0.1_/floatball"))

    def test_find_window_needs_a_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            host=FloatBallHost(Path(tmp))
            self.assertIsNone(host._find_window())

    def test_stop_clears_pid_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            host=FloatBallHost(Path(tmp))
            (Path(tmp)/'floatball.pid').write_text('999999', encoding='utf-8')
            host.stop()
            self.assertFalse((Path(tmp)/'floatball.pid').exists())

class StartGuardTests(unittest.TestCase):
    def _host(self, tmp):
        host = FloatBallHost(Path(tmp))
        host._edge_path = lambda: r"C:\Edge\msedge.exe"
        return host

    def test_start_passes_the_renderer_appcontainer_opt_out(self):
        # Python 3.14 起 Edge 时不带这个开关，渲染进程一个都起不来，窗口里只剩白框。
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host(tmp)
            popen = mock.Mock()
            popen.return_value.pid = 4321
            with mock.patch("noticehub.floatball_host.subprocess.Popen", popen), \
                    mock.patch.object(host, "_clear_stale"), \
                    mock.patch.object(host, "_find_window", return_value=99), \
                    mock.patch.object(host, "_style_window"), \
                    mock.patch.object(host, "_await_layout", return_value=True), \
                    mock.patch.object(host, "_place_initial"), \
                    mock.patch("noticehub.floatball_host.ensure_dpi_aware"), \
                    mock.patch("noticehub.floatball_host.threading.Thread"):
                host.start("http://127.0.0.1:8766/floatball?token=x")
            args = popen.call_args[0][0]
            self.assertIn("--disable-features=RendererAppContainer", args)
            self.assertTrue(host.visible)
            self.assertEqual(host.hwnd, 99)

    def test_start_closes_a_window_whose_page_never_rendered(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host(tmp)
            popen = mock.Mock()
            popen.return_value.pid = 4321
            killed = []
            with mock.patch("noticehub.floatball_host.subprocess.Popen", popen), \
                    mock.patch.object(host, "_clear_stale"), \
                    mock.patch.object(host, "_find_window", return_value=99), \
                    mock.patch.object(host, "_style_window"), \
                    mock.patch.object(host, "_await_layout", return_value=False), \
                    mock.patch("noticehub.floatball_host.ensure_dpi_aware"), \
                    mock.patch("noticehub.floatball_host._kill_tree", killed.append):
                with self.assertRaises(RuntimeError):
                    host.start("http://127.0.0.1:8766/floatball?token=x")
            self.assertEqual(killed, [4321])
            self.assertIsNone(host.hwnd)
            self.assertIsNone(host.process)
            self.assertFalse((Path(tmp) / "floatball.pid").exists())

    def test_manual_hide_is_not_undone_by_the_watchdog(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host(tmp)
            host.hwnd = 99
            host.visible = True
            host.hide()
            self.assertFalse(host.visible)
            self.assertFalse(host._should_auto_show())
            host.show()
            self.assertTrue(host.visible)
            host.hide(manual=False)
            self.assertTrue(host._should_auto_show())


class GeometryTests(unittest.TestCase):
    """裁剪区域必须跟着真实的客户区/页面区走，不能只按 viewport 反推。"""

    WINDOW = (2094, 636, 2586, 1432)          # 492x796
    CLIENT = (2102, 636, 2578, 1424)          # 476x788：左右各 8px 边框

    def _host(self, tmp, rect=None, client=None, viewport=(380, 600)):
        host = FloatBallHost(Path(tmp))
        host.hwnd = 1
        host.viewport = viewport
        host._rect = lambda: rect or self.WINDOW
        host._client_rect_screen = lambda: client or self.CLIENT
        host._dpi_scale = lambda: 1.25
        return host

    def test_region_box_uses_the_live_page_area(self):
        with tempfile.TemporaryDirectory() as tmp:
            # viewport 380x600 → 页面 475x750，贴在客户区右下角。
            host = self._host(tmp, client=(2102, 636, 2578, 1386))   # 476x750
            self.assertEqual(host._page_box(), (9, 0, 475, 750))
            self.assertEqual(host._region_box(), (394, 660, 90, 90))

    def test_edge_title_strip_is_clipped_off_the_panel(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Edge app 窗口的标题条画在客户区里面，只能靠 viewport 反推它有多高。
            host = self._host(tmp, viewport=(380, 571), client=(2102, 636, 2578, 1424))
            self.assertEqual(host._page_box(), (9, 74, 475, 714))
            self.assertEqual(host._region_box(), (394, 698, 90, 90))
        with tempfile.TemporaryDirectory() as tmp:
            # _fit_window 把页面撑到 380x600 之后：客户区 476x832，标题条 82px 不进裁剪区域。
            host = self._host(tmp, viewport=(380, 600), client=(2102, 636, 2578, 1468))
            host.expanded = True
            self.assertEqual(host._region_box(), (9, 82, 475, 750))

    def test_region_follows_the_window_when_edge_resizes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host(tmp, client=(2102, 636, 2578, 1386))
            narrow = host._region_box()[0]
            # Edge 首帧后把应用窗口拉宽 20px：客户区跟着变宽，裁剪区域必须一起右移。
            host._rect = lambda: (2094, 636, 2606, 1432)
            host._client_rect_screen = lambda: (2102, 636, 2598, 1386)
            self.assertEqual(host._region_box()[0], narrow + 20)

    def test_sync_geometry_reclips_and_keeps_the_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host(tmp)
            host._anchor = (100, 200)
            with mock.patch.object(host, "_apply_region") as apply_region, \
                    mock.patch.object(host, "_place_corner") as place_corner:
                host._sync_geometry()
                host._sync_geometry()
                self.assertEqual(apply_region.call_count, 1)
                self.assertEqual(place_corner.call_args[0], (100, 200))
                apply_region.assert_called_with(1)
                host._rect = lambda: (2094, 636, 2606, 1432)
                host._sync_geometry()
                self.assertEqual(apply_region.call_count, 2)
                self.assertEqual(place_corner.call_count, 2)

    def test_panel_grows_left_from_the_ball_corner(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host(tmp)
            with mock.patch.object(host, "_ensure_topmost"), \
                    mock.patch("noticehub.floatball_host.ctypes.windll.user32.SetWindowPos") as set_pos:
                host._place_card(2000, 1000)
                self.assertEqual(host._anchor, (2090, 1090))
                host.expanded = True
                host._place_corner(*host._anchor)
            # 展开后面板 475x750，右下角钉在 (2090,1090) → 左上 (1615,340)，减掉页面区偏移 (9,38)。
            self.assertEqual(set_pos.call_args_list[-1][0][2:4], (1615 - 9, 340 - 38))

    def test_badge_sits_at_the_ball_corner_and_only_when_collapsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host(tmp)
            left, top, width, height = host._badge_box()
            self.assertEqual((top, height), (0, 35))
            self.assertEqual(left + width, 90)
            self.assertEqual(left, 55)
            host.expanded = True
            self.assertIsNone(host._badge_box())


class TopmostTests(unittest.TestCase):
    def _host(self, tmp):
        host = FloatBallHost(Path(tmp))
        host._log = mock.Mock()
        return host

    def _user32(self, exstyle=0x08200080, set_pos_result=0):
        fake = mock.Mock()
        fake.GetWindowLongW.return_value = exstyle
        fake.SetWindowPos.return_value = set_pos_result
        return fake

    def test_failed_topmost_is_logged_instead_of_silent(self):
        # 置顶失败时球被别的窗口盖住，用户看到的是「浮球没出来」，必须留痕迹。
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host(tmp)
            fake = self._user32()
            with mock.patch("noticehub.floatball_host.ctypes.windll.user32", fake), \
                    mock.patch("noticehub.floatball_host.ctypes.get_last_error", return_value=5):
                host._ensure_topmost(7999908)
                message = host._log.call_args[0][0]
                self.assertIn("置顶失败", message)
                self.assertIn("err=5", message)
                self.assertIn("hwnd=7999908", message)

    def test_repeated_topmost_failure_is_rate_limited(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host(tmp)
            fake = self._user32()
            with mock.patch("noticehub.floatball_host.ctypes.windll.user32", fake), \
                    mock.patch("noticehub.floatball_host.ctypes.get_last_error", return_value=5):
                host._ensure_topmost(1)
                host._ensure_topmost(1)
            self.assertEqual(host._log.call_count, 1)
            self.assertEqual(fake.SetWindowPos.call_count, 2)

    def test_topmost_window_and_successful_call_stay_quiet(self):
        with tempfile.TemporaryDirectory() as tmp:
            already = self._host(tmp)
            fake = self._user32(exstyle=0x08200088)
            with mock.patch("noticehub.floatball_host.ctypes.windll.user32", fake):
                already._ensure_topmost(1)
            self.assertEqual(fake.SetWindowPos.call_count, 0)
            self.assertEqual(already._log.call_count, 0, "已经置顶的窗口不用再动，也不该刷日志")

        with tempfile.TemporaryDirectory() as tmp:
            fixed = self._host(tmp)
            fake = self._user32(set_pos_result=1)
            # 第一次读到的 exstyle 还没 topmost，置顶成功后就带上了。
            fake.GetWindowLongW.side_effect = [0x08200080, 0x08200088]
            with mock.patch("noticehub.floatball_host.ctypes.windll.user32", fake):
                fixed._ensure_topmost(1)
                fixed._ensure_topmost(1)  # 第二遍已经带了 topmost 样式，早退
            self.assertEqual(fake.SetWindowPos.call_count, 1)
            # 首次置顶成功记一条「浮球已置顶」：真机上「球看不见」时靠它区分
            # 「没置顶成功」还是「置顶了但没渲染」；只记一次，不每轮刷。
            self.assertEqual(fixed._log.call_count, 1)
            self.assertIn("浮球已置顶", fixed._log.call_args[0][0])


class FullscreenTests(unittest.TestCase):
    def _host(self, tmp):
        return FloatBallHost(Path(tmp))

    def test_maximized_window_is_not_treated_as_fullscreen(self):
        # 最大化窗口的矩形比显示器大一圈，按矩形判断会把球藏起来；用户看到的就是浮球不见了。
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host(tmp)
            host.hwnd = 777
            fake = mock.Mock()
            fake.GetForegroundWindow.return_value = 4242
            fake.IsIconic.return_value = 0
            fake.IsZoomed.return_value = 1
            with mock.patch("noticehub.floatball_host.ctypes.windll.user32", fake):
                self.assertFalse(host._foreground_fullscreen())
            self.assertEqual(fake.GetWindowRect.call_count, 0)

    def test_borderless_window_covering_the_monitor_is_fullscreen(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host(tmp)
            host.hwnd = 777
            fake = mock.Mock()
            fake.GetForegroundWindow.return_value = 4242
            fake.IsIconic.return_value = 0
            fake.IsZoomed.return_value = 0
            fake.GetWindowRect.side_effect = lambda hwnd, rect: _fill(rect, (0, 0, 2048, 1152))
            fake.MonitorFromWindow.return_value = 11
            fake.GetMonitorInfoW.side_effect = lambda mon, info: _fill_monitor(info, (0, 0, 2048, 1152))
            with mock.patch("noticehub.floatball_host.ctypes.windll.user32", fake):
                self.assertTrue(host._foreground_fullscreen())


def _fill(rect, box):
    left, top, right, bottom = box
    value = rect._obj
    value.left, value.top, value.right, value.bottom = left, top, right, bottom
    return 1


def _fill_monitor(info, box):
    left, top, right, bottom = box
    monitor = info._obj.rcMonitor
    monitor.left, monitor.top, monitor.right, monitor.bottom = left, top, right, bottom
    return 1


if __name__ == '__main__': unittest.main()
