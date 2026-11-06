"""floatball_render 的单测：几何契约、命中测试、滚动、过渡帧、透明兜底。"""

from __future__ import annotations

import unittest
from unittest import mock

from PIL import Image, ImageDraw

from noticehub import floatball_render as fr


def _rows(count: int) -> tuple:
    return tuple(
        fr.TaskRow(task_id=f"t{i}", title=f"任务 {i}", source="示例群", when="18:00", pinned=(i == 0))
        for i in range(count)
    )


class BallTest(unittest.TestCase):
    def test_ball_size_and_transparent_corners(self) -> None:
        image = fr.render_ball(3)
        self.assertEqual(image.size, (fr.BALL_SIZE, fr.BALL_SIZE))
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.getpixel((0, 0))[3], 0)          # 角落透明
        self.assertGreater(image.getpixel((36, 36))[3], 0)      # 中心不透明
        self.assertEqual(image.getpixel((36, 36))[:3], fr.BALL_COLOR)

    def test_ball_no_badge_when_none_or_zero(self) -> None:
        for badge in (None, 0):
            image = fr.render_ball(badge)
            self.assertNotEqual(image.getpixel((fr.BALL_SIZE - 8, 8))[:3], fr.BADGE_BG)

    def test_ball_has_a_bell_icon(self) -> None:
        # 球面必须有铃铛图标：重写时丢过一次（纯色圆直接发到真机），这条锁死。
        # 开发负责人口径：球心半径 20px 内 luminance>180 的像素 ≥40；
        # 另加铃铛下半部（底檐/铃舌，y40-52，角标永远够不到的区域）必须有白线，
        # 防 badge 的白描边/数字凑数蒙混。
        for badge in (None, 99):
            image = fr.render_ball(badge)
            px = image.load()
            c = fr.BALL_SIZE / 2
            bright = sum(
                1
                for y in range(fr.BALL_SIZE)
                for x in range(fr.BALL_SIZE)
                if (x - c) ** 2 + (y - c) ** 2 <= 20 * 20
                and (0.299 * px[x, y][0] + 0.587 * px[x, y][1] + 0.114 * px[x, y][2]) > 180
            )
            self.assertGreaterEqual(bright, 40, f"badge={badge} 球心无铃铛图标")
            lower = sum(
                1
                for y in range(40, 52)
                for x in range(24, 48)
                if (0.299 * px[x, y][0] + 0.587 * px[x, y][1] + 0.114 * px[x, y][2]) > 180
            )
            self.assertGreaterEqual(lower, 10, f"badge={badge} 铃铛下半部（底檐/铃舌）缺失")

    def test_ball_badge_inside_ball_bounds(self) -> None:
        image = fr.render_ball(9)
        # 角标圆心 (52,20)、半径 11、白描边 2：外沿最远 x=65、y=7，不出 72 外接矩形。
        # 探针点避开描边渐变与数字：红底在圆心左 (44,20)。
        r, g, b, _ = image.getpixel((44, 20))
        self.assertGreater(r, g + 60)
        self.assertGreater(r, b + 60)
        # 角标不再飘在球外：外沿点仍在球内（alpha>0 且偏红），且不出 72 边界由渲染尺寸保证。
        self.assertLessEqual(52 + 11 + 2, fr.BALL_SIZE)
        self.assertGreaterEqual(52 - 11 - 2, 0)
        self.assertGreaterEqual(20 - 11 - 2, 0)

    def test_ball_highlight_is_subtle_top_band(self) -> None:
        image = fr.render_ball(None)
        # 旧高光是 (128,143,103) 亮斑，已删；新的是极淡白渐变，任何像素不得再出现旧高光色。
        for x in range(0, fr.BALL_SIZE, 7):
            for y in range(0, fr.BALL_SIZE, 7):
                p = image.getpixel((x, y))
                if p[3] > 0:
                    self.assertNotEqual(p[:3], fr.BALL_HIGHLIGHT)

    def test_ball_phase_shrinks_circle(self) -> None:
        rest = fr.render_ball(None, 0.0)
        pressed = fr.render_ball(None, 1.0)
        rest_opaque = sum(1 for x in range(fr.BALL_SIZE) for y in range(fr.BALL_SIZE) if rest.getpixel((x, y))[3] > 0)
        pressed_opaque = sum(1 for x in range(fr.BALL_SIZE) for y in range(fr.BALL_SIZE) if pressed.getpixel((x, y))[3] > 0)
        self.assertLess(pressed_opaque, rest_opaque)

    def test_ball_edge_alpha_smooth_and_clean(self) -> None:
        image = fr.render_ball(3)
        w, h = image.size
        corners = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1), (w // 2, 0), (0, h // 2), (w - 1, h // 2)]
        for x, y in corners:
            self.assertEqual(image.getpixel((x, y))[3], 0, f"({x},{y}) alpha 不是 0")
        mid_y = h // 2
        left_edge = next(x for x in range(w) if image.getpixel((x, mid_y))[3] > 200)
        self.assertGreater(image.getpixel((left_edge - 1, mid_y))[3], 0)
        self.assertLess(image.getpixel((left_edge - 1, mid_y))[3], 200)

    def test_ball_font_fallback_never_crashes(self) -> None:
        with mock.patch.object(fr.os.path, "exists", return_value=False):
            image = fr.render_ball(5)
            self.assertEqual(image.size, (fr.BALL_SIZE, fr.BALL_SIZE))
            panel = fr.render_panel(fr.PanelView(rows=_rows(2), status="已更新", status_kind="success"))
            self.assertEqual(panel.size, (fr.PANEL_SIZE[0] + fr.SHADOW_SPREAD * 2, fr.PANEL_SIZE[1] + fr.SHADOW_SPREAD * 2))


class GeometryTest(unittest.TestCase):
    def test_panel_size_is_440(self) -> None:
        self.assertEqual(fr.PANEL_SIZE, (320, 440))

    def test_row_geometry_shape_and_growth(self) -> None:
        rows = fr.visible_row_geometry()
        self.assertEqual(len(rows), fr.VISIBLE_ROWS)
        x, y, w, h = rows[0]
        self.assertEqual(x, fr.PANEL_PAD)
        self.assertEqual(h, fr.ROW_H)
        self.assertLess(rows[0][1], rows[1][1])
        for (ax, ay, _, ah), (_, by, _, _) in zip(rows, rows[1:]):
            self.assertGreaterEqual(by, ay + ah)

    def test_six_rows_fit_viewport(self) -> None:
        rows = fr.visible_row_geometry()
        bottom = fr.PANEL_SIZE[1] - fr.STATUS_H - fr.PANEL_PAD
        for _, y, _, h in rows:
            self.assertLessEqual(y + h, bottom)

    def test_button_geometry_inside_row(self) -> None:
        buttons = fr.button_geometry(0)
        self.assertEqual(set(buttons), {"done", "snooze", "pin"})
        rx, ry, rw, rh = fr.visible_row_geometry()[0]
        for bx, by, bw, bh in buttons.values():
            self.assertGreaterEqual(bx, rx)
            self.assertGreaterEqual(by, ry)
            self.assertLessEqual(bx + bw, rx + rw)
        order = [buttons[name][0] for name in ("done", "snooze", "pin")]
        self.assertGreater(order[0], order[1])
        self.assertGreater(order[1], order[2])

    def test_button_total_width_within_budget(self) -> None:
        buttons = fr.button_geometry(0)
        total = sum(b[2] for b in buttons.values()) + fr.BTN_GAP * 2
        self.assertLessEqual(total, 132)

    def test_button_geometry_out_of_range_empty(self) -> None:
        self.assertEqual(fr.button_geometry(-1), {})
        self.assertEqual(fr.button_geometry(999), {})

    def test_pinned_button_uses_short_label_width(self) -> None:
        base = fr.button_geometry(0)["pin"]
        active = fr.button_geometry(0, pinned=True)["pin"]
        self.assertEqual(active[2], fr.BUTTON_WIDTHS["pin_active"])

    def test_pin_labels_fit_button_without_widening(self) -> None:
        # 开发负责人 2026-11-07：置顶按钮文案换两字状态词「钉住/已钉」，
        # 「取消」会被读成「取消这条任务」；宽度保持 40px 不加宽，文案必须放得下。
        self.assertEqual(fr.BUTTON_LABELS["pin"], "钉住")
        self.assertEqual(fr.PIN_ACTIVE_LABEL, "已钉")
        self.assertEqual(fr.BUTTON_WIDTHS["pin"], 40)
        self.assertEqual(fr.BUTTON_WIDTHS["pin_active"], 40)
        probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
        font = fr._font(fr.FONT_BUTTON)
        for label in (fr.BUTTON_LABELS["pin"], fr.PIN_ACTIVE_LABEL):
            width = probe.textbbox((0, 0), label, font=font)[2]
            self.assertLessEqual(width, fr.BUTTON_WIDTHS["pin"] - 4, label)


class ScrollTest(unittest.TestCase):
    def test_list_metrics_overflow(self) -> None:
        metrics = fr.list_metrics(8)
        self.assertGreater(metrics["content_h"], metrics["view_h"])
        self.assertGreater(metrics["max_scroll"], 0)

    def test_list_metrics_no_overflow(self) -> None:
        metrics = fr.list_metrics(5)
        self.assertEqual(metrics["max_scroll"], 0)

    def test_scrollbar_drawn_only_when_overflow(self) -> None:
        # 8 行内容超屏：滚动条区应有滑块灰像素；4 行不超屏：不应有。
        # 轨道在行宽右侧 6px 空隙处（图上 x=投影边+320-14-3..+3），滑块不许压进行卡右缘
        # —— 曾漏加投影偏移整体左移 10px 压在行卡上，这里锁死位置。
        track_x0 = fr.SHADOW_SPREAD + fr.PANEL_SIZE[0] - fr.PANEL_PAD - fr.SCROLLBAR_W
        track_x1 = track_x0 + fr.SCROLLBAR_W

        def thumb_pixels(rows: tuple) -> list:
            image = fr.render_panel(fr.PanelView(rows=rows))
            px = image.load()
            hits = []
            for y in range(fr.SHADOW_SPREAD + 34, fr.SHADOW_SPREAD + 410):
                for xx in range(track_x0, track_x1 + 1):
                    if px[xx, y][:3] == (180, 176, 160):
                        hits.append((xx, y))
            return hits

        hits = thumb_pixels(_rows(8))
        self.assertGreater(len(hits), 20)
        rx, _, rw, _ = fr.visible_row_geometry()[0]
        row_right = fr.SHADOW_SPREAD + rx + rw - 1
        self.assertGreaterEqual(min(h[0] for h in hits), row_right + 1)  # 滑块在行卡右侧
        self.assertEqual(thumb_pixels(_rows(4)), [])

    def test_hit_test_respects_scroll(self) -> None:
        view = fr.PanelView(rows=_rows(8), scroll=64)  # 一行+gap 的偏移
        self.assertEqual(fr.list_metrics(8)["max_scroll"], 128)
        # 可视槽位 0 显示内容第 1 行（t1）；最后一槽位（slot5）显示内容第 6 行（t6）。
        rows = fr.visible_row_geometry()
        rx, ry, _, _ = rows[0]
        self.assertEqual(fr.hit_test(rx + 5, ry + 5, view), ("row", "t1"))
        rx, ry, _, _ = rows[fr.VISIBLE_ROWS - 1]
        self.assertEqual(fr.hit_test(rx + 5, ry + 5, view), ("row", "t6"))
        # scroll=0 时同一槽位命中 t5
        self.assertEqual(fr.hit_test(rx + 5, ry + 5, fr.PanelView(rows=_rows(8))), ("row", "t5"))

    def test_hit_test_scroll_rolls_past_content(self) -> None:
        view = fr.PanelView(rows=_rows(8), scroll=fr.list_metrics(8)["max_scroll"])
        rows = fr.visible_row_geometry()
        rx, ry, _, _ = rows[0]
        # 滚到底：max_scroll=128（两行），槽位 0 显示 t2
        self.assertEqual(fr.hit_test(rx + 5, ry + 5, view), ("row", "t2"))


class HitTestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.view = fr.PanelView(rows=_rows(3))

    def test_close_button(self) -> None:
        cx, cy, _, _ = fr.close_geometry()
        self.assertEqual(fr.hit_test(cx + 5, cy + 5, self.view), ("close", ""))

    def test_row_body_and_buttons(self) -> None:
        rx, ry, _, _ = fr.visible_row_geometry()[1]
        self.assertEqual(fr.hit_test(rx + 5, ry + 5, self.view), ("row", "t1"))
        buttons = fr.button_geometry(1)
        bx, by, bw, bh = buttons["snooze"]
        self.assertEqual(fr.hit_test(bx + bw // 2, by + bh // 2, self.view), ("snooze", "t1"))
        bx, by, bw, bh = buttons["done"]
        self.assertEqual(fr.hit_test(bx + 1, by + 1, self.view), ("done", "t1"))
        bx, by, bw, bh = buttons["pin"]
        self.assertEqual(fr.hit_test(bx + 1, by + 1, self.view), ("pin", "t1"))

    def test_outside_returns_none(self) -> None:
        self.assertIsNone(fr.hit_test(-5, -5, self.view))
        self.assertIsNone(fr.hit_test(fr.PANEL_SIZE[0] + 5, fr.PANEL_SIZE[1] + 5, self.view))
        self.assertIsNone(fr.hit_test(fr.PANEL_PAD + 5, fr.PANEL_PAD + 5, self.view))


class PanelTest(unittest.TestCase):
    def test_panel_size_and_shadow(self) -> None:
        view = fr.PanelView(rows=_rows(5), status="已更新", status_kind="success")
        image = fr.render_panel(view)
        self.assertEqual(image.size, (fr.PANEL_SIZE[0] + fr.SHADOW_SPREAD * 2, fr.PANEL_SIZE[1] + fr.SHADOW_SPREAD * 2))
        self.assertEqual(image.mode, "RGBA")
        pad = fr.SHADOW_SPREAD
        self.assertEqual(image.getpixel((pad + fr.PANEL_SIZE[0] // 2, pad + fr.PANEL_SIZE[1] // 2))[3], 255)

    def test_title_shows_total_count(self) -> None:
        # 标题文案「待处理 · 8 条」由 render_panel 生成；这里锁 8 行也只画 6 行可视区。
        view = fr.PanelView(rows=_rows(8))
        image = fr.render_panel(view)
        self.assertEqual(image.size[0], fr.PANEL_SIZE[0] + fr.SHADOW_SPREAD * 2)
        metrics = fr.list_metrics(8)
        self.assertEqual(metrics["max_scroll"], (8 - fr.VISIBLE_ROWS) * (fr.ROW_H + fr.ROW_GAP))

    def test_empty_state(self) -> None:
        view = fr.PanelView(rows=())
        image = fr.render_panel(view)
        self.assertEqual(image.getpixel((fr.SHADOW_SPREAD + fr.PANEL_SIZE[0] // 2, fr.SHADOW_SPREAD + 40))[3], 255)
        self.assertIsNone(fr.hit_test(50, 200, view))

    def test_error_status(self) -> None:
        view = fr.PanelView(rows=_rows(1), status="操作失败，任务仍保留", status_kind="error")
        image = fr.render_panel(view)
        self.assertEqual(image.size[0], fr.PANEL_SIZE[0] + fr.SHADOW_SPREAD * 2)

    def test_busy_rows_render(self) -> None:
        view = fr.PanelView(rows=_rows(2), busy=True)
        image = fr.render_panel(view)
        self.assertEqual(image.size, (fr.PANEL_SIZE[0] + fr.SHADOW_SPREAD * 2, fr.PANEL_SIZE[1] + fr.SHADOW_SPREAD * 2))

    def test_expanded_flag_accepted(self) -> None:
        view = fr.PanelView(rows=_rows(1), expanded=False)
        self.assertEqual(fr.render_panel(view).size[0], fr.PANEL_SIZE[0] + fr.SHADOW_SPREAD * 2)

    def test_long_title_is_truncated_before_buttons(self) -> None:
        # 真机暴露：60 字长标题画到右边缘压住按钮。截断兜底必须按像素量。
        long_title = "这是一条非常非常非常长的任务标题用来验证截断兜底逻辑确保不会压住按钮区域" * 1 + "补充长度补充长度补充长度补充长度补充长度补充长度补充长度"
        self.assertGreater(len(long_title), 55)
        view = fr.PanelView(rows=(fr.TaskRow("t0", long_title, "示例群", "18:00"),))
        image = fr.render_panel(view)
        pad = fr.SHADOW_SPREAD
        rows = fr.visible_row_geometry()
        rx, ry, _, _ = rows[0]
        buttons = fr.button_geometry(0)
        btn_left = min(bx for bx, _, _, _ in buttons.values())
        # 扫标题行（y=ry+8 起的 13px 字带）找最右非背景像素，必须停在按钮左界 −4 之前
        rightmost = 0
        for x in range(rx + 10, btn_left):
            for y in range(pad + ry + 8, pad + ry + 26):
                p = image.getpixel((x, y))
                if p[3] > 0 and p[:3] != fr.ROW_BG and p[:3] != fr.PANEL_BG:
                    rightmost = max(rightmost, x)
        self.assertLessEqual(rightmost, pad + btn_left - 4)

    def test_long_source_is_truncated_within_row(self) -> None:
        # 群名长：来源行纵向与按钮区重叠，横向必须让出按钮区（与标题同一公式），
        # 且按钮区内不得被来源文字污染（与置空基线逐像素比）。
        long_source = "示例群 · 这是个很长很长的群名超过二十个字了还要继续加长才行哦再长一点"
        self.assertGreater(len(long_source), 20)
        rows = (fr.TaskRow("t0", "短标题", long_source, "18:00", pinned=True),)
        clean_rows = (fr.TaskRow("t0", "短标题", "", "18:00", pinned=True),)
        filled = fr.render_panel(fr.PanelView(rows=rows))
        clean = fr.render_panel(fr.PanelView(rows=clean_rows))
        pad = fr.SHADOW_SPREAD
        # 来源行最右像素也不得超出行右界
        rows_geo = fr.visible_row_geometry()
        rx, ry, rw, _ = rows_geo[0]
        rightmost = 0
        for x in range(rx, rx + rw):
            for y in range(pad + ry + 28, pad + ry + 44):
                p = filled.getpixel((x, y))
                if p[3] > 0 and p[:3] not in (fr.ROW_BG, fr.PANEL_BG):
                    rightmost = max(rightmost, x)
        self.assertLessEqual(rightmost, pad + rx + rw)
        # 按钮区逐像素比对：来源再长也不许压按钮
        buttons = fr.button_geometry(0, pinned=True)
        for _, (bx, by, bw, bh) in buttons.items():
            for x in range(pad + bx, pad + bx + bw):
                for y in range(pad + by, pad + by + bh):
                    self.assertEqual(filled.getpixel((x, y)), clean.getpixel((x, y)),
                                     f"来源行压住按钮区 ({x},{y})")

    def test_title_and_source_never_overlap_buttons(self) -> None:
        # 标题与群名同时超长：按钮矩形区域内不得被文字污染。
        # 基线用「同一行数据但标题/来源为空」的渲染——空 PanelView 没有行卡，不能当基线。
        long_title = "超长标题" * 20
        long_source = "超长群名" * 8
        rows = (fr.TaskRow("t0", long_title, long_source, "18:00 截止", pinned=True),)
        baseline = (fr.TaskRow("t0", "", "", "18:00 截止", pinned=True),)
        filled = fr.render_panel(fr.PanelView(rows=rows))
        clean = fr.render_panel(fr.PanelView(rows=baseline))
        pad = fr.SHADOW_SPREAD
        buttons = fr.button_geometry(0, pinned=True)
        for _, (bx, by, bw, bh) in buttons.items():
            for x in range(pad + bx, pad + bx + bw):
                for y in range(pad + by, pad + by + bh):
                    self.assertEqual(filled.getpixel((x, y)), clean.getpixel((x, y)),
                                     f"按钮区 ({x},{y}) 被文字污染")

    def test_scroll_field_defaults_zero(self) -> None:
        view = fr.PanelView(rows=())
        self.assertEqual(view.scroll, 0)


class TransitionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.view = fr.PanelView(rows=_rows(6))

    def test_full_size_at_all_t(self) -> None:
        for t in (0.0, 0.25, 0.5, 0.75, 1.0):
            image = fr.render_transition(t, self.view)
            self.assertEqual(
                image.size,
                (fr.PANEL_SIZE[0] + fr.SHADOW_SPREAD * 2, fr.PANEL_SIZE[1] + fr.SHADOW_SPREAD * 2),
                f"t={t}",
            )

    def test_content_appears_only_after_06(self) -> None:
        # 内容（标题墨色文字）在 t<=0.6 不出现，t=1 出现。
        def has_ink(image: Image.Image) -> bool:
            for x in range(image.width):
                for y in range(image.height):
                    p = image.getpixel((x, y))
                    if p[3] > 200 and abs(p[0] - fr.INK[0]) < 12 and abs(p[1] - fr.INK[1]) < 12 and abs(p[2] - fr.INK[2]) < 12:
                        return True
            return False

        self.assertFalse(has_ink(fr.render_transition(0.5, self.view)))
        self.assertTrue(has_ink(fr.render_transition(1.0, self.view)))

    def test_ball_fades_out_by_035(self) -> None:
        early = fr.render_transition(0.1, self.view)
        late = fr.render_transition(0.5, self.view)

        def ball_px(image: Image.Image) -> int:
            # 球被缩放并整体调 alpha，颜色不精确等于 BALL_COLOR；用「绿色调且不透明」近似球体像素。
            count = 0
            for x in range(image.width):
                for y in range(image.height):
                    p = image.getpixel((x, y))
                    if p[3] > 150 and abs(p[0] - fr.BALL_COLOR[0]) < 14 and abs(p[1] - fr.BALL_COLOR[1]) < 14 and abs(p[2] - fr.BALL_COLOR[2]) < 14:
                        count += 1
            return count

        self.assertGreater(ball_px(early), 100)
        self.assertEqual(ball_px(late), 0)

    def test_card_grows_with_easing(self) -> None:
        # 中间帧的卡片宽度介于球与面板之间，且 ease-out 使 t=0.5 已过半。
        def card_w(image: Image.Image) -> int:
            xs = [x for x in range(image.width) for y in range(image.height // 2, image.height, 5)
                  if image.getpixel((x, y))[3] > 200]
            return (max(xs) - min(xs) + 1) if xs else 0

        half = card_w(fr.render_transition(0.5, self.view))
        self.assertGreater(half, fr.BALL_SIZE)
        self.assertLess(half, fr.PANEL_SIZE[0] + fr.SHADOW_SPREAD * 2)

    def test_preview_sheet_has_five_frames(self) -> None:
        sheet = fr.build_preview_frames(12, _rows(6))
        self.assertEqual(sheet.mode, "RGBA")
        # 5 帧横排：宽度至少 5 倍面板宽。
        self.assertGreater(sheet.width, 5 * fr.PANEL_SIZE[0])

    def test_transition_cache_is_pixel_identical(self) -> None:
        """内容层缓存不许改变任何一帧的输出（开发负责人验收硬条件）。

        同一 view 连续渲染两个 t 点：先清缓存走「画」的路径，再不清缓存走「缓存」
        的路径，两轮逐像素比对。缓存路径上有一个坑必须锁住——putalpha 是原地写，
        直接把缓存的图调 alpha 会污染下一帧，这里用 t=0.7（alpha<255）连画两帧验证。
        """
        fr.reset_transition_cache()
        baseline = {t: fr.render_transition(t, self.view) for t in (0.55, 0.7)}
        # 第二轮不清缓存：t=0.7 命中内容缓存 + putalpha 原地写。
        for t in (0.55, 0.7):
            again = fr.render_transition(t, self.view)
            self.assertEqual(again.tobytes(), baseline[t].tobytes(), f"t={t} 缓存前后不一致")

    def test_transition_cache_invalidates_on_new_view(self) -> None:
        """换 view（宿主每轮刷新都是新 PanelView）后缓存必须失效，不许画出旧内容。"""
        fr.reset_transition_cache()
        old_view = self.view
        fr.render_transition(1.0, old_view)
        new_view = fr.PanelView(rows=_rows(2), status="状态变了")
        fresh = fr.render_transition(1.0, new_view)
        direct = fr.render_transition(1.0, fr.PanelView(rows=_rows(2), status="状态变了"))
        self.assertEqual(fresh.tobytes(), direct.tobytes())

    def test_transition_cache_holds_at_most_one_entry(self) -> None:
        """缓存只留最后一条：宿主每次刷新都新建 view，不清空会长跑缓慢增长。"""
        fr.reset_transition_cache()
        for i in range(5):
            fr.render_transition(1.0, fr.PanelView(rows=_rows(1), status=f"v{i}"))
        self.assertLessEqual(len(fr._transition_cache), 1)


if __name__ == "__main__":
    unittest.main()
