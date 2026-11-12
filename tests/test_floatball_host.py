"""浮球宿主：窗口、动画、状态和面板数据都要在不建真窗口的前提下测到。"""

import datetime as dt
import http.server
import json
import logging
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

if os.name != "nt":
    raise unittest.SkipTest("Windows-only floating-ball host")
from noticehub import floatball_host as host_module
from noticehub.floatball_host import (
    ANIMATION_STEPS,
    CLOSED_SIZE,
    CORNER_GAP,
    OPEN_SIZE,
    FloatBallHost,
    clamp_position,
    ease_out_cubic,
    parse_title,
    process_matches_commandline,
    region_radius,
    size_for_title,
    snap_position,
    window_class,
    write_state,
)

PANEL_FRAME = (340, 460)


def _image(size):
    from PIL import Image

    return Image.new("RGBA", (int(size[0]), int(size[1])))


class _FakeWindow:
    """假窗口：只记录调用，不碰真 Win32，用例不会闪窗。"""

    def __init__(self, on_click=None, on_drag_end=None, hwnd=4321):
        self.hwnd = hwnd
        self.calls = []
        self._position = (0, 0)
        self.closed = False

    def set_frame(self, image, anchor="br"):
        self.calls.append(("set_frame", tuple(image.size), anchor))

    def show(self, x=None, y=None):
        if x is not None and y is not None:
            self._position = (int(x), int(y))
        self.calls.append(("show", x, y))

    def hide(self):
        self.calls.append(("hide",))

    def position(self):
        return self._position

    def close(self):
        self.closed = True


@dataclass(frozen=True)
class _Row:
    task_id: str
    title: str
    source: str
    when: str
    pinned: bool = False


@dataclass(frozen=True)
class _View:
    rows: tuple = ()
    status: str = ""
    status_kind: str = ""
    busy: bool = False
    expanded: bool = True
    scroll: int = 0


def _fake_render(progress=None):
    """假渲染层：尺寸与真实现一致（面板含 10px 投影），并记录过渡进度。"""
    module = types.SimpleNamespace()
    module.SHADOW_SPREAD = 10
    module.PANEL_SIZE = (320, 440)
    module.ROW_H = 56
    module.ROW_GAP = 8
    module.TaskRow = _Row
    module.PanelView = _View
    module.render_ball = lambda badge, phase=0.0: _image(CLOSED_SIZE)
    module.render_panel = lambda view: _image(PANEL_FRAME)

    def list_metrics(row_count):
        # 与真实现同一份算术：6 行可视区 = 6*56 + 5*8 = 376
        view_h = 6 * 56 + 5 * 8
        content_h = row_count * 56 + max(0, row_count - 1) * 8
        return {"top": 34, "view_h": view_h, "content_h": content_h,
                "max_scroll": max(0, content_h - view_h)}

    module.list_metrics = list_metrics

    def render_transition(t, view):
        if progress is not None:
            progress.append(float(t))
        return _image(PANEL_FRAME)

    module.render_transition = render_transition
    module.hit_test = lambda x, y, view: None
    return module


class FloatBallHostTests(unittest.TestCase):
    def test_clamps_overflow(self):
        self.assertEqual(clamp_position(-5, 999, 100, 80, (0, 0, 800, 600)), (0, 520))

    def test_snaps_each_edge(self):
        work = (0, 0, 800, 600)
        self.assertEqual(snap_position(2, 200, 100, 80, work)[0], 8)
        self.assertEqual(snap_position(690, 200, 100, 80, work)[0], 692)
        self.assertEqual(snap_position(300, 2, 100, 80, work)[1], 8)
        self.assertEqual(snap_position(300, 518, 100, 80, work)[1], 512)

    def test_state_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = FloatBallHost(Path(tmp))
            write_state(host.state_path, 12, 34, True)
            self.assertEqual(
                json.loads(host.state_path.read_text(encoding="utf-8")),
                {"x": 12, "y": 34, "expanded": True},
            )
            self.assertEqual(host._load_state()["x"], 12)

    def test_title_layout_signal_ignores_loading_titles(self):
        self.assertEqual(size_for_title("nh-floatball:closed"), CLOSED_SIZE)
        self.assertEqual(size_for_title("nh-floatball:open"), OPEN_SIZE)
        self.assertIsNone(size_for_title("http://127.0.0.1:8765/floatball"))
        self.assertIsNone(size_for_title("nh-floatball:unknown"))

    def test_process_commandline_filter(self):
        profile = Path(r"F:\probe\floatball-profile")
        self.assertTrue(
            process_matches_commandline(
                "msedge.exe --user-data-dir=F:\\probe\\floatball-profile", profile
            )
        )
        self.assertFalse(
            process_matches_commandline("msedge.exe --user-data-dir=F:\\other", profile)
        )

    def test_window_class_of_invalid_handle_is_empty(self):
        self.assertEqual(window_class(0), "")

    def test_region_radius(self):
        self.assertEqual(region_radius(False, 72), 36)
        self.assertEqual(region_radius(True, 600), 16)

    def test_parse_title_reads_viewport(self):
        self.assertEqual(parse_title("nh-floatball:closed:72x41"), ("closed", 72, 41))
        self.assertEqual(parse_title("nh-floatball:open"), ("open", 0, 0))
        self.assertIsNone(parse_title("127.0.0.1_/floatball"))

    def test_stop_clears_pid_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = FloatBallHost(Path(tmp))
            (Path(tmp) / "floatball.pid").write_text("999999", encoding="utf-8")
            host.stop()
            self.assertFalse((Path(tmp) / "floatball.pid").exists())

    def test_ease_out_cubic_starts_fast_and_lands_exactly_on_one(self):
        self.assertEqual(ease_out_cubic(0), 0.0)
        self.assertEqual(ease_out_cubic(1), 1.0)
        self.assertGreater(ease_out_cubic(0.5), 0.5)
        self.assertLess(ease_out_cubic(0.25), ease_out_cubic(0.5))


class StartTests(unittest.TestCase):
    def _start(self, host, window, render, url="http://127.0.0.1:8766/floatball?token=x"):
        with mock.patch.object(host, "_window_class", return_value=lambda *args, **kwargs: window), \
                mock.patch.object(host, "_render_module", return_value=render), \
                mock.patch("noticehub.floatball_host._work_area", return_value=(0, 0, 800, 600)), \
                mock.patch("noticehub.floatball_host.ensure_dpi_aware"), \
                mock.patch.object(host, "_refresh_async"), \
                mock.patch("noticehub.floatball_host.threading.Thread"):
            host.start(url)

    def test_start_puts_the_ball_at_the_work_area_corner(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = FloatBallHost(Path(tmp))
            window = _FakeWindow()
            self._start(host, window, _fake_render())
            self.assertTrue(host.visible)
            self.assertEqual(host.hwnd, 4321)
            self.assertEqual(
                window.position(),
                (800 - CLOSED_SIZE[0] - CORNER_GAP, 600 - CLOSED_SIZE[1] - CORNER_GAP),
            )

    def test_start_frames_before_showing_so_the_corner_does_not_jump(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = FloatBallHost(Path(tmp))
            window = _FakeWindow()
            self._start(host, window, _fake_render())
            self.assertEqual([call[0] for call in window.calls], ["set_frame", "show"])
            self.assertEqual(window.calls[0][1], CLOSED_SIZE)

    def test_start_clamps_a_saved_position_back_into_the_work_area(self):
        # 换了显示器/分辨率后旧坐标会落到屏幕外，浮球就「不见了」。
        with tempfile.TemporaryDirectory() as tmp:
            host = FloatBallHost(Path(tmp))
            write_state(host.state_path, 9000, 9000, False)
            window = _FakeWindow()
            self._start(host, window, _fake_render())
            self.assertEqual(
                window.position(),
                (800 - CLOSED_SIZE[0], 600 - CLOSED_SIZE[1]),
            )

    def test_start_restores_an_expanded_panel_with_the_panel_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = FloatBallHost(Path(tmp))
            write_state(host.state_path, 400, 100, True)
            window = _FakeWindow()
            self._start(host, window, _fake_render())
            self.assertTrue(host.expanded)
            self.assertEqual(window.calls[0][1], PANEL_FRAME)
            # 存过的位置照用，只按面板尺寸 clamp 回工作区（400/100 本来就在界内）。
            self.assertEqual(window.position(), (400, 100))

    def test_start_refuses_without_the_window_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = FloatBallHost(Path(tmp))
            with mock.patch.object(host, "_window_class", return_value=None):
                with self.assertRaises(RuntimeError) as caught:
                    host.start("http://127.0.0.1:8766/floatball?token=x")
            self.assertIn("缺少浮球窗口模块", str(caught.exception))
            self.assertFalse(host.visible)

    def test_start_refuses_a_window_without_a_handle(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = FloatBallHost(Path(tmp))
            window = _FakeWindow(hwnd=None)
            with self.assertRaises(RuntimeError) as caught:
                self._start(host, window, _fake_render())
            self.assertIn("没有拿到窗口句柄", str(caught.exception))
            self.assertIsNone(host._win)
            self.assertIsNone(host.hwnd)
            self.assertFalse(host.visible)

    def test_start_waits_for_the_lazily_created_window(self):
        # FloatBallWindow 是懒建窗口的：hwnd 在 show() 之前恒为 0。
        class LazyWindow(_FakeWindow):
            def __init__(self, on_click=None, on_drag_end=None):
                super().__init__(on_click, on_drag_end, hwnd=0)

            def show(self, x=None, y=None):
                super().show(x, y)
                if not self.hwnd:
                    self.hwnd = 4321

        with tempfile.TemporaryDirectory() as tmp:
            host = FloatBallHost(Path(tmp))
            window = LazyWindow()
            self._start(host, window, _fake_render())
            self.assertTrue(host.visible)
            self.assertEqual(host.hwnd, 4321)
            shows = [call for call in window.calls if call[0] == "show"]
            self.assertEqual(len(shows), 2)
            self.assertEqual(shows[0][1:], (0, 0), "先起线程，不能直接定位")
            self.assertEqual(
                shows[1][1:],
                (800 - CLOSED_SIZE[0] - CORNER_GAP, 600 - CLOSED_SIZE[1] - CORNER_GAP),
            )


class AnimationTests(unittest.TestCase):
    def _host(self, tmp, progress=None):
        host = FloatBallHost(Path(tmp))
        window = _FakeWindow()
        host._win = window
        host.hwnd = window.hwnd
        host._render_module = lambda: _fake_render(progress)
        return host, window

    def test_expand_pushes_eased_transition_frames_to_the_ball_corner(self):
        progress = []
        with tempfile.TemporaryDirectory() as tmp:
            host, window = self._host(tmp, progress)
            with mock.patch("noticehub.floatball_host.time.sleep"):
                host._run_animation(True)
            self.assertTrue(host.expanded)
            self.assertEqual(len(progress), ANIMATION_STEPS)
            self.assertAlmostEqual(progress[0], ease_out_cubic(1 / ANIMATION_STEPS), places=6)
            self.assertEqual(progress[-1], 1.0)
            self.assertEqual(progress, sorted(progress), "展开进度必须单调递增")
            anchors = [call[2] for call in window.calls if call[0] == "set_frame"]
            self.assertEqual(set(anchors), {"br"}, "每一帧都要锚在右下角，球那一角不能跳")

    def test_collapse_runs_the_same_frames_backwards(self):
        progress = []
        with tempfile.TemporaryDirectory() as tmp:
            host, window = self._host(tmp, progress)
            host.expanded = True
            with mock.patch("noticehub.floatball_host.time.sleep"):
                host._run_animation(False)
            self.assertFalse(host.expanded)
            self.assertEqual(len(progress), ANIMATION_STEPS)
            self.assertAlmostEqual(
                progress[0], 1.0 - ease_out_cubic(1 / ANIMATION_STEPS), places=6
            )
            self.assertEqual(progress[-1], 0.0)
            self.assertEqual(progress, sorted(progress, reverse=True))

    def test_second_expand_is_ignored_while_the_first_one_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, window = self._host(tmp)
            host._animating = True
            with mock.patch("noticehub.floatball_host.threading.Thread") as thread, \
                    mock.patch.object(host, "_refresh_async"):
                host.expand()
            thread.assert_not_called()
            self.assertEqual(window.calls, [], "动画进行中不能再插一帧")

    def test_animation_clears_the_guard_when_it_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, _ = self._host(tmp)
            with mock.patch("noticehub.floatball_host.time.sleep"):
                host._run_animation(True)
            self.assertFalse(host._animating)


class PanelDataTests(unittest.TestCase):
    def _host(self, tmp):
        host = FloatBallHost(Path(tmp))
        window = _FakeWindow()
        host._win = window
        host.hwnd = window.hwnd
        host._render_module = lambda: _fake_render()
        return host, window

    def test_api_failure_shows_the_error_in_the_status_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, window = self._host(tmp)
            host.expanded = True
            with mock.patch.object(host, "_api_get", side_effect=RuntimeError("连接被拒绝")):
                host._refresh_panel()
            self.assertEqual(host._status_kind, "error")
            self.assertIn("读取待办失败", host._status)
            self.assertIn("连接被拒绝", host._status)
            self.assertEqual(window.calls[-1][0], "set_frame", "失败也要重绘，否则用户看不到错误")

    def test_rows_come_from_the_task_board_buckets_in_order(self):
        payload = {
            "candidates": [
                {
                    "id": "a",
                    "summary": "交周报",
                    "source": "群公告",
                    "deadline_text": "今天 18:00",
                }
            ],
            "today": [{"id": "b", "summary": "交电费", "source": "私聊", "pinned": True}],
            "done": [{"id": "c", "summary": "已经做完的"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            host, _ = self._host(tmp)
            with mock.patch.object(host, "_api_get", return_value=payload):
                host._refresh_panel()
            self.assertEqual([row.task_id for row in host._rows], ["a", "b"])
            self.assertEqual(host._rows[0].title, "交周报")
            self.assertEqual(host._rows[0].when, "今天 18:00")
            self.assertTrue(host._rows[1].pinned)
            self.assertEqual(host._status, "")
            self.assertEqual(host._status_kind, "")

    def test_broken_payload_is_reported_instead_of_blank_panel(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, _ = self._host(tmp)
            with mock.patch.object(host, "_api_get", return_value=["不是对象"]):
                with mock.patch.object(host, "_rows_from_board", side_effect=AttributeError("没这个桶")):
                    host._refresh_panel()
            self.assertIn("待办数据看不懂", host._status)
            self.assertEqual(host._status_kind, "error")

    # 真实 payload（实抓 /api/tasks）：summary 是 30~60 字原文，action 才是能放下的短句。
    REAL_ITEM = {
        "id": 23,
        "summary": "今天下午6-7节13:30-14:55在立功楼7阶上生涯规划课，选课同学请提前到场",
        "action": "13:30前到立功楼7阶上课",
        "deadline": "2026-10-09T13:30:00",
        "groups": ["计算机26级专业群"],
        "sender": "龙老师",
        "source": "qq_message",
        "pinned": False,
    }

    def _rows_for(self, item):
        with tempfile.TemporaryDirectory() as tmp:
            host, _ = self._host(tmp)
            with mock.patch.object(host, "_api_get", return_value={"today": [item]}):
                host._refresh_panel()
            return list(host._rows)

    def test_panel_title_prefers_action_over_summary(self):
        rows = self._rows_for(self.REAL_ITEM)
        self.assertEqual(rows[0].title, "13:30前到立功楼7阶上课")

    def test_panel_source_uses_group_name(self):
        rows = self._rows_for(self.REAL_ITEM)
        self.assertEqual(rows[0].source, "计算机26级专业群")

    def test_panel_time_formats_deadline(self):
        from noticehub.floatball_host import format_deadline

        now = dt.datetime(2026, 10, 9, 9, 0)
        self.assertEqual(format_deadline("2026-10-09T13:30:00", now), "今天 13:30")
        self.assertEqual(format_deadline("2026-10-20T13:30:00", now), "10-20 13:30")
        self.assertEqual(format_deadline("2026-10-01T13:30:00", now), "已过期")
        self.assertEqual(format_deadline("", now), "")
        self.assertEqual(format_deadline("不是时间", now), "")
        rows = self._rows_for(self.REAL_ITEM)
        self.assertNotIn("T", rows[0].when, "ISO 串不许直接上屏")

    def test_panel_survives_missing_groups_and_deadline(self):
        rows = self._rows_for({"id": 9, "summary": "交周报", "action": ""})
        self.assertEqual(rows[0].title, "交周报", "action 为空时回退 summary")
        self.assertEqual(rows[0].source, "")
        self.assertEqual(rows[0].when, "")


class LoadFailureTests(unittest.TestCase):
    """取数失败不能画成「0 条」：那跟真有 0 条长得一模一样，用户以为没事。"""

    def _host(self, tmp, rows=()):
        host = FloatBallHost(Path(tmp))
        window = _FakeWindow()
        host._win = window
        host.hwnd = window.hwnd
        host._render_module = lambda: _fake_render()
        host._rows = list(rows)
        return host, window

    def _badge_pixel(self, image):
        from noticehub import floatball_render as render

        # 角标圆心在 (size-20, 20)，半径 9；偏左 5px 落在红底上，避开中间那道白色「!」。
        return image.getpixel((CLOSED_SIZE[0] - 25, 20)), render.BADGE_BG

    def test_failed_load_draws_error_marker_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, window = self._host(tmp)
            host.expanded = False
            with mock.patch.object(host, "_api_get", side_effect=RuntimeError("连接被拒绝")):
                host._refresh_panel()
            self.assertTrue(host._load_failed)
            self.assertEqual(window.calls[-1][0], "set_frame")
            frame = host._ball_frame(0)
            pixel, badge_bg = self._badge_pixel(frame)
            self.assertEqual(pixel[:3], badge_bg, "失败角标必须是红底「!」，不是数字 0")
            self.assertEqual(pixel[3], 255, "角标要实心，不能是透明的")
            # 参照：正常 0 条时那个位置什么都不画（透明），所以肉眼能分出两种状态。
            host._load_failed = False
            self.assertEqual(host._ball_frame(0).getpixel((CLOSED_SIZE[0] - 25, 20))[3], 0)

    def test_periodic_refresh_recovers_after_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, _ = self._host(tmp)
            with mock.patch.object(host, "_api_get", side_effect=RuntimeError("连接被拒绝")):
                host._refresh_panel()
            self.assertTrue(host._load_failed)

            with mock.patch.object(
                host, "_api_get", return_value={"today": [{"id": 1, "action": "交周报"}]}
            ):
                host._refresh_panel()
            self.assertFalse(host._load_failed, "刷新成功必须清掉失败态")
            self.assertEqual(len(host._rows), 1)
            self.assertEqual(
                host._ball_frame(1).getpixel((CLOSED_SIZE[0] - 25, 20))[3],
                0,
                "恢复后角标回到真实条数，不再画「!」",
            )

    def test_true_zero_rows_draws_no_error_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, _ = self._host(tmp)
            with mock.patch.object(host, "_api_get", return_value={"today": []}):
                host._refresh_panel()
            self.assertFalse(host._load_failed)
            self.assertEqual(host._rows, [])
            self.assertEqual(host._ball_frame(0).getpixel((CLOSED_SIZE[0] - 25, 20))[3], 0,
                             "真 0 条不许画「!」")

    def test_failed_panel_says_load_failed_instead_of_zero(self):
        # 用真渲染层：这段文案是叠在真面板上的，假渲染层画不出字。
        from noticehub import floatball_render as render

        with tempfile.TemporaryDirectory() as tmp:
            host, _ = self._host(tmp)
            host._render_module = lambda: render
            host.expanded = True
            with mock.patch.object(host, "_api_get", side_effect=RuntimeError("连接被拒绝")):
                host._refresh_panel()
            self.assertTrue(host._load_failed)
            frame = host._panel_frame(host._panel).convert("RGBA")

            def red_pixels(image):
                return sum(
                    1
                    for y in range(0, 60)
                    for x in range(0, image.size[0])
                    if image.getpixel((x, y))[:3] == render.DANGER
                )

            self.assertGreater(red_pixels(frame), 20, "标题区必须写「待办加载失败」，不能只说 0 条")
            host._load_failed = False
            host._status = ""
            host._status_kind = ""
            self.assertEqual(red_pixels(host._panel_frame(host._panel)), 0, "正常态标题区不该有红字")

    def test_watch_refreshes_every_30s(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, _ = self._host(tmp)
            host.hwnd = None  # 不触发自动显示，只测刷新节奏
            clock = [0.0]
            host._stop_event.wait = mock.Mock(side_effect=lambda _t: False)
            calls = []

            def refresh():
                calls.append(time.monotonic())
                if len(calls) >= 2:
                    host._stop_event.wait = mock.Mock(return_value=True)

            with mock.patch.object(host, "_refresh_async", side_effect=refresh), \
                    mock.patch("noticehub.floatball_host.time.monotonic", side_effect=lambda: clock[0]), \
                    mock.patch.object(host, "_foreground_fullscreen", return_value=False):
                # 每轮循环把时钟往前推 20 秒：0 -> 20(不刷) -> 40(刷) -> 60(刷)
                def tick(_seconds):
                    clock[0] += 20
                    return False

                host._stop_event.wait.side_effect = tick
                host._watch()

            self.assertGreaterEqual(len(calls), 2, "30 秒内必须自己重试，不能等用户重启")

    def test_start_is_not_reentrant(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = FloatBallHost(Path(tmp))
            built = []
            inside = threading.Event()
            release = threading.Event()

            def factory(*_args, **_kwargs):
                built.append(1)
                inside.set()
                release.wait(3)
                return _FakeWindow()

            # 线程必须在 patch 之外建：patch 把 threading.Thread 换成了替身，替身 .start() 不真跑。
            first = threading.Thread(target=host.start, args=("http://x/?token=t",), daemon=True)
            with mock.patch.object(host, "_window_class", return_value=factory), \
                    mock.patch.object(host, "_render_module", return_value=_fake_render()), \
                    mock.patch("noticehub.floatball_host._work_area", return_value=(0, 0, 800, 600)), \
                    mock.patch("noticehub.floatball_host.ensure_dpi_aware"), \
                    mock.patch.object(host, "_refresh_async"), \
                    mock.patch("noticehub.floatball_host.threading.Thread"):
                first.start()
                self.assertTrue(inside.wait(3), "第一次 start 没进到建窗口")
                host.start("http://x/?token=t")  # 并发第二次：必须直接返回
                release.set()
                first.join(3)

            self.assertEqual(len(built), 1, "并发 start 只能建一个窗口")

    def test_stop_joins_the_watcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = FloatBallHost(Path(tmp))
            watcher = threading.Thread(target=lambda: host._stop_event.wait(5), daemon=True)
            host._watcher = watcher
            watcher.start()
            host.stop()
            self.assertFalse(watcher.is_alive(), "stop() 必须等 watcher 退出，否则它会被下一次 start 复活")
            self.assertIsNone(host._watcher)


class WheelTests(unittest.TestCase):
    """滚轮：一档一行，到顶到底不越界，收起后回到顶部。"""

    def _host(self, tmp, rows=18):
        host = FloatBallHost(Path(tmp))
        window = _FakeWindow()
        host._win = window
        host.hwnd = window.hwnd
        host._render_module = lambda: _fake_render()
        host._rows = [_Row("%d" % i, "任务 %d" % i, "", "") for i in range(rows)]
        host.expanded = True
        return host, window

    def test_wheel_scrolls_the_panel(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, window = self._host(tmp)
            limit = host._max_scroll()
            self.assertGreater(limit, 0, "18 条任务必须能滚")
            window.calls.clear()
            host._on_wheel(-1)
            self.assertEqual(window.calls, [("set_frame", PANEL_FRAME, "br")],
                             "滚动值变了必须马上重画一帧")
            for _ in range(30):  # 多滚几档，到底就停住
                host._on_wheel(-1)
            self.assertEqual(host._scroll, limit, "滚到底正好停在 max_scroll，不越界")

    def test_wheel_does_not_overflow_when_at_top(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, _ = self._host(tmp)
            self.assertEqual(host._scroll, 0)
            for _ in range(3):
                host._on_wheel(1)  # 向上滚 = 往回
            self.assertEqual(host._scroll, 0, "已经在顶部时向上滚不能变负")

    def test_collapse_resets_scroll(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, _ = self._host(tmp)
            host._on_wheel(-2)
            self.assertGreater(host._scroll, 0)
            with mock.patch("noticehub.floatball_host.time.sleep"):
                host._run_animation(False)
            self.assertEqual(host._scroll, 0, "收起后下次展开必须从第一条开始")
            self.assertFalse(host.expanded)

    def test_wheel_ignored_while_collapsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, window = self._host(tmp)
            host.expanded = False
            window.calls.clear()
            host._on_wheel(-1)
            self.assertEqual(host._scroll, 0)
            self.assertEqual(window.calls, [], "球没展开时滚轮不该重画任何东西")

    def test_wheel_at_the_bottom_does_not_repaint(self):
        # 到底后再滚不能每档重画一张 340×460，滚轮会被拖成幻灯片。
        with tempfile.TemporaryDirectory() as tmp:
            host, window = self._host(tmp)
            for _ in range(30):
                host._on_wheel(-1)
            window.calls.clear()
            host._on_wheel(-1)
            self.assertEqual(window.calls, [], "滚动值没变就别重画")


class SilentFallbackTests(unittest.TestCase):
    """兜底不许静默：打包后缺模块的表现是「什么都不发生」，日志里必须留痕且不刷屏。"""

    def setUp(self):
        host_module._RENDER.clear()
        host_module._warned.clear()
        self.addCleanup(host_module._RENDER.clear)
        self.addCleanup(host_module._warned.clear)

    def test_broken_render_module_is_logged_once(self):
        # 让 `from . import floatball_render` 真的抛：sys.modules 放 None，同时摘掉包属性，
        # 否则 from-import 直接命中已加载的属性、根本不走导入。
        import noticehub

        with mock.patch.dict(sys.modules, {"noticehub.floatball_render": None}):
            saved = noticehub.__dict__.pop("floatball_render", None)
            try:
                with self.assertLogs("noticehub.floatball_host", level="ERROR") as logs:
                    self.assertIsNone(host_module._render_module(), "导入失败要降级成兜底")
            finally:
                if saved is not None:
                    noticehub.floatball_render = saved
        self.assertIn("渲染模块", logs.output[0])
        with self.assertNoLogs("noticehub.floatball_host", level="ERROR"):
            self.assertIsNone(host_module._render_module(), "缓存住了就别重复记")

    def test_broken_scroll_geometry_warns_once(self):
        host = FloatBallHost(Path(tempfile.gettempdir()) / "nh-floatball-silent")
        broken = _fake_render()
        del broken.ROW_H
        del broken.ROW_GAP

        def boom(_count):
            raise RuntimeError("几何算不出来")

        broken.list_metrics = boom
        host._render_module = lambda: broken

        with self.assertLogs("noticehub.floatball_host", level="ERROR") as logs:
            self.assertEqual(host._scroll_step(), 64, "取不到行高就回退一行的兜底值")
            self.assertEqual(host._max_scroll(), 0, "取不到上限就当不能滚")
        self.assertEqual(len(logs.output), 2, "两处兜底各记一条")
        self.assertTrue(any("滚动步长" in line for line in logs.output))
        self.assertTrue(any("滚动上限" in line for line in logs.output))

        with self.assertNoLogs("noticehub.floatball_host", level="ERROR"):
            host._scroll_step()
            host._max_scroll()


def _mock_handler(records, payload):
    """真 socket 的假接口：每次请求记下方法、路径、body 和 X-Token 供断言。"""

    class Handler(http.server.BaseHTTPRequestHandler):
        def _reply(self, body):
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            path = self.path.split("?")[0]
            records.append(
                {"method": "GET", "path": path, "body": "", "token": self.headers.get("X-Token")}
            )
            self._reply(payload if path == "/api/tasks" else {"items": []})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            records.append(
                {
                    "method": "POST",
                    "path": self.path,
                    "body": raw,
                    "token": self.headers.get("X-Token"),
                }
            )
            self._reply({"ok": True})

        def log_message(self, *args):
            pass

    return Handler


class ActionRequestTests(unittest.TestCase):
    """四个动作按钮：请求到底发没发、路径和 body 对不对、X-Token 带没带。

    这里不 mock urllib，起一个真 socket 的 server，让宿主真发 HTTP。
    """

    FIXTURE = {
        "candidates": [
            {
                "id": 1,
                "summary": "原文很长很长的一句话，正常不会上屏",
                "action": "交周报",
                "deadline": "2026-10-09T18:00:00",
                "groups": ["计算机26级专业群"],
                "sender": "龙老师",
                "pinned": False,
            }
        ],
        "today": [
            {
                "id": 2,
                "summary": "交电费",
                "action": "",
                "deadline": "",
                "groups": [],
                "sender": "",
                "pinned": False,
            }
        ],
        "week": [
            {
                "id": 3,
                "summary": "周三党史学习",
                "action": "看完第三讲",
                "deadline": "",
                "groups": ["周三党史-计算机12"],
                "sender": "",
                "pinned": False,
            }
        ],
    }

    def setUp(self):
        self.records = []
        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _mock_handler(self.records, self.FIXTURE)
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.host = FloatBallHost(Path(self.tmp.name))
        window = _FakeWindow()
        with mock.patch.object(
            self.host, "_window_class", return_value=lambda *args, **kwargs: window
        ), mock.patch("noticehub.floatball_host._work_area", return_value=(0, 0, 800, 600)), \
                mock.patch("noticehub.floatball_host.ensure_dpi_aware"):
            self.host.start("http://127.0.0.1:%d/?token=T" % self.port)
        # 看门狗会去问前台窗口是不是全屏，用例里没必要真问
        self.host._stop_event.set()
        self.window = window
        self.assertTrue(
            self._wait(lambda: len(self.host._rows) == 3), "面板数据没从 mock 接口加载出来"
        )
        self.host.expanded = True  # 直接置标志，别跑动画（动画会 sleep 掉近 1 秒）
        self.host._refresh_panel()

    def tearDown(self):
        self.host._stop_event.set()
        self.host.stop()
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _wait(self, predicate, timeout=6.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    def _posts(self):
        return [item for item in self.records if item["method"] == "POST"]

    def _click_button(self, row_index, name, pinned=False):
        from noticehub import floatball_render as render

        pad = render.SHADOW_SPREAD
        x, y, width, height = render.button_geometry(row_index, pinned)[name]
        self.host._on_click(x + pad + width // 2, y + pad + height // 2)

    def test_action_buttons_hit_the_right_endpoints(self):
        self._click_button(0, "done")
        self.assertTrue(self._wait(lambda: len(self._posts()) >= 1), "点了「完成」一个请求都没发出去")
        done = self._posts()[-1]
        self.assertEqual(done["path"], "/api/tasks/1")
        self.assertEqual(json.loads(done["body"]), {"action": "done"})
        self.assertEqual(done["token"], "T", "X-Token 必须带上，否则真机会被 403")

        self._click_button(1, "snooze")
        self.assertTrue(self._wait(lambda: len(self._posts()) >= 2), "点了「稍后」没发请求")
        snooze = self._posts()[-1]
        self.assertEqual(snooze["path"], "/api/tasks/2", "第二行必须是 2 号任务")
        self.assertEqual(json.loads(snooze["body"]), {"action": "snooze"})
        self.assertEqual(snooze["token"], "T")

        self._click_button(0, "pin")
        self.assertTrue(self._wait(lambda: len(self._posts()) >= 3), "点了「置顶」没发请求")
        pin = self._posts()[-1]
        self.assertEqual(pin["path"], "/api/tasks/pin")
        self.assertEqual(json.loads(pin["body"]), {"task_id": "1", "pinned": True})
        self.assertEqual(pin["token"], "T")

    def test_every_request_carries_the_token(self):
        self.assertTrue(self.records, "启动后应该已经拉过一次任务列表")
        for item in self.records:
            self.assertEqual(item["token"], "T", "每个请求都要带 X-Token：%s" % item["path"])

    def test_close_button_sends_nothing_and_collapses(self):
        from noticehub import floatball_render as render

        x, y, width, height = render.close_geometry()
        pad = render.SHADOW_SPREAD
        before = len(self._posts())
        self.host._on_click(x + pad + width // 2, y + pad + height // 2)
        self.assertTrue(self._wait(lambda: not self.host.expanded, timeout=5.0), "点关闭没收起")
        time.sleep(0.1)
        self.assertEqual(len(self._posts()), before, "关闭按钮不许再发任何请求")


class ClickTests(unittest.TestCase):
    def _host(self, tmp):
        host = FloatBallHost(Path(tmp))
        window = _FakeWindow()
        host._win = window
        host.hwnd = window.hwnd
        host._render_module = lambda: _fake_render()
        return host, window

    def test_click_on_the_collapsed_ball_expands_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, _ = self._host(tmp)
            host.expanded = False
            with mock.patch.object(host, "expand") as expand, \
                    mock.patch.object(host, "_refresh_async"):
                host._on_click(36, 36)
            expand.assert_called_once()

    def test_drag_end_clamps_the_ball_and_saves_the_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, window = self._host(tmp)
            window._position = (9000, 9000)
            with mock.patch("noticehub.floatball_host._work_area", return_value=(0, 0, 800, 600)):
                host._on_drag_end(9000, 9000)
            self.assertEqual(window.position(), (800 - CLOSED_SIZE[0], 600 - CLOSED_SIZE[1]))
            self.assertEqual(host._anchor, (800, 600))
            self.assertEqual(
                json.loads(host.state_path.read_text(encoding="utf-8"))["x"],
                800 - CLOSED_SIZE[0],
            )


class WatchdogTests(unittest.TestCase):
    def test_manual_hide_is_not_undone_by_the_watchdog(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = FloatBallHost(Path(tmp))
            host._win = _FakeWindow(hwnd=99)
            host.hwnd = 99
            host.visible = True
            host.hide()
            self.assertFalse(host.visible)
            self.assertFalse(host._should_auto_show())
            host.show()
            self.assertTrue(host.visible)
            host.hide(manual=False)
            self.assertTrue(host._should_auto_show())


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
            fake.GetMonitorInfoW.side_effect = lambda mon, info: _fill_monitor(
                info, (0, 0, 2048, 1152)
            )
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


if __name__ == "__main__":
    unittest.main()
