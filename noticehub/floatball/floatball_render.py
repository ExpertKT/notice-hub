"""浮球自绘渲染层：只画图，不碰窗口、不碰网络。

球和面板都由 Pillow 画成 RGBA 图，宿主（floatball_host）负责逐帧贴到屏幕。
真透明靠 alpha 通道，边缘外 alpha=0。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFilter, ImageFont

BALL_SIZE = 72
PANEL_SIZE = (320, 440)
# 可视区固定显示几行任务卡，超出靠 scroll 滚动。
VISIBLE_ROWS = 6

# 配色沿用页面 --teal / --paper / --ink 一族。
BALL_COLOR = (92, 107, 68)        # #5C6B44
BALL_HIGHLIGHT = (128, 143, 103)  # 高光，比球体亮一档
PANEL_BG = (250, 247, 239)        # 纸色
PANEL_EDGE = (214, 208, 190)
INK = (58, 58, 40)
DIM = (120, 117, 98)
ACCENT = (92, 107, 68)
DANGER = (154, 63, 50)
GHOST = (200, 196, 180)
# 关闭按钮 × 的笔画色：底 (242,241,236) 上 ≥4.5:1，用户认不出 × 就等于面板关不掉。
X_STROKE = (58, 58, 52)
# 按钮文字：不浅于 #4A4A42（开发负责人拍板下限）；「完成」主操作再深一档 (42,42,36)。
BTN_INK = (74, 74, 66)
BTN_INK_DONE = (42, 42, 36)
ROW_BG = (255, 253, 248)
BADGE_BG = (196, 60, 44)
BADGE_RING = (255, 255, 255)

PANEL_RADIUS = 14
ROW_RADIUS = 10
SHADOW_BLUR = 12
SHADOW_ALPHA = 60
SHADOW_SPREAD = 10
SCROLLBAR_W = 3

FONT_TITLE = 15
FONT_ROW = 13
FONT_META = 10
FONT_BUTTON = 11
FONT_BADGE = 10

# 行高与间距：用户嫌过挤，56/8 是拍板值，别调回去。
ROW_H = 56
ROW_GAP = 8
BTN_H = 22
BTN_GAP = 4
# 按钮总宽 ≤132：钉住/已钉 40、稍后 40、完成 44。两字状态词，不需要加宽档。
BUTTON_WIDTHS = {"done": 44, "snooze": 40, "pin": 40, "pin_active": 40}


@dataclass(frozen=True)
class TaskRow:
    task_id: str
    title: str       # 已截断到一行
    source: str      # 群名/来源，可为空
    when: str        # 时间文本，可为空
    pinned: bool = False


@dataclass(frozen=True)
class PanelView:
    rows: tuple[TaskRow, ...]
    status: str = ""           # 底部状态行，如「已更新」「操作失败，任务仍保留：…」
    status_kind: str = ""      # "" | "success" | "error"
    busy: bool = False         # 正在提交操作：按钮置灰
    expanded: bool = True
    scroll: int = 0            # 列表区滚动像素偏移，>=0


# ---- 布局常量（面板内坐标；几何函数与绘制共用同一份，别各写一份）----
# 440 高要装下 6 行 56px + 5 个 8px 间距（376px）：行顶 36、行底 440-16-4=420。
PANEL_PAD = 14
TITLE_H = 26
STATUS_H = 16
ROW_GAP = 8
ROW_H = 56
BTN_H = 22
BTN_GAP = 4
# 按钮总宽 ≤132：钉住/已钉 40、稍后 40、完成 44，间距 4。
BUTTON_WIDTHS = {"done": 44, "snooze": 40, "pin": 40, "pin_active": 40}
CLOSE_SIZE = 22
CLOSE_MARGIN = 8

# 置顶按钮用状态词：「取消」在「稍后」旁边会被读成「取消这条任务」（开发负责人 2026-11-07）。
# 「钉住/已钉」+ 置顶态 ACCENT 色，宽度 40 不加宽。
BUTTON_LABELS = {"done": "完成", "snooze": "稍后", "pin": "钉住"}
PIN_ACTIVE_LABEL = "已钉"


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """中文字体：微软雅黑优先，黑体兜底，最后退到 Pillow 默认位图字体（不崩路径）。"""
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf"):
        path = os.path.join(r"C:\Windows\Fonts", name)
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _text(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, size: int,
          color: tuple[int, ...], bold: bool = False) -> None:
    if not text:
        return
    font = _font(size)
    if bold and size >= FONT_TITLE and os.path.exists(os.path.join(r"C:\Windows\Fonts", "msyhbd.ttc")):
        font = ImageFont.truetype(os.path.join(r"C:\Windows\Fonts", "msyhbd.ttc"), size)
    draw.text(xy, text, font=font, fill=color)


def _truncate(text: str, size: int, max_width: int) -> tuple[str, object]:
    """按像素宽度截断到 max_width 内，超出补「…」。中英文混排按 textbbox 量，不按字数。

    返回 (截断后文本, 所用字体)；空串原样返回。字体随文本带出，避免调用方再取一次。
    """
    if not text:
        return "", _font(size)
    font = _font(size)
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    if probe.textbbox((0, 0), text, font=font)[2] <= max_width:
        return text, font
    suffix = "…"
    suffix_w = probe.textbbox((0, 0), suffix, font=font)[2]
    lo, hi = 0, len(text)
    while lo < hi:  # 二分最长能放下的前缀
        mid = (lo + hi + 1) // 2
        w = probe.textbbox((0, 0), text[:mid] + suffix, font=font)[2]
        if w <= max_width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + suffix, font


def row_geometry(count: int) -> list[tuple[int, int, int, int]]:
    """每一行任务卡的矩形 (x, y, w, h)，面板内坐标。与绘制共用同一份数学。"""
    top = PANEL_PAD + TITLE_H + 8
    bottom = PANEL_SIZE[1] - STATUS_H - PANEL_PAD
    rows: list[tuple[int, int, int, int]] = []
    y = top
    for _ in range(max(0, count)):
        if y + ROW_H > bottom:
            break
        rows.append((PANEL_PAD, y, PANEL_SIZE[0] - PANEL_PAD * 2, ROW_H))
        y += ROW_H + ROW_GAP
    return rows


def visible_row_geometry() -> list[tuple[int, int, int, int]]:
    """可视区固定 VISIBLE_ROWS 行的矩形（面板内坐标），滚动条从行宽右侧扣掉 6px。

    440 高 = 标题 26 + 行区 376（6×56 + 5×8）+ 状态 16 + 留白：行顶 34、行底 410。
    """
    top = 34
    rows: list[tuple[int, int, int, int]] = []
    y = top
    for _ in range(VISIBLE_ROWS):
        rows.append((PANEL_PAD, y, PANEL_SIZE[0] - PANEL_PAD * 2 - 6, ROW_H))
        y += ROW_H + ROW_GAP
    return rows


def list_metrics(row_count: int) -> dict[str, int]:
    """滚动几何：可视高度、内容高度、最大滚动偏移。"""
    rows = visible_row_geometry()
    view_top = rows[0][1]
    view_h = rows[-1][1] + ROW_H - view_top
    content_h = row_count * ROW_H + max(0, row_count - 1) * ROW_GAP
    return {
        "top": view_top,
        "view_h": view_h,
        "content_h": content_h,
        "max_scroll": max(0, content_h - view_h),
    }


def button_geometry(row_index: int, pinned: bool = False) -> dict[str, tuple[int, int, int, int]]:
    """一行里三个按钮的矩形，面板内坐标。按钮靠右、同一行底边对齐。"""
    rows = visible_row_geometry()
    if row_index < 0 or row_index >= len(rows):
        return {}
    x, y, w, h = rows[row_index]
    right = x + w - 10
    buttons: dict[str, tuple[int, int, int, int]] = {}
    for name in ("done", "snooze", "pin"):
        width = BUTTON_WIDTHS[name]
        right -= width
        buttons[name] = (right, y + h - 10 - BTN_H, width, BTN_H)
        right -= BTN_GAP
    return buttons


def close_geometry() -> tuple[int, int, int, int]:
    """右上角关闭按钮，面板内坐标。"""
    return (PANEL_SIZE[0] - CLOSE_MARGIN - CLOSE_SIZE, CLOSE_MARGIN, CLOSE_SIZE, CLOSE_SIZE)


def refresh_geometry() -> tuple[int, int, int, int]:
    """关闭按钮左边的刷新按钮，面板内坐标（绘制与命中共用）。"""
    cx, cy, cw, ch = close_geometry()
    return (cx - 30, cy, 22, 22)


def hit_test(x: int, y: int, view: PanelView) -> tuple[str, str] | None:
    """返回 ("close","") / ("refresh","") / ("row", task_id) / ("done"|"snooze"|"pin", task_id) / None。

    y 先加上 scroll 换算回内容坐标再对可视区行判定；滚出可视区的行不能命中。
    """
    cx, cy, cw, ch = close_geometry()
    if cx <= x < cx + cw and cy <= y < cy + ch:
        return ("close", "")
    rx0, ry0, rw0, rh0 = refresh_geometry()
    if rx0 <= x < rx0 + rw0 and ry0 <= y < ry0 + rh0:
        return ("refresh", "")
    metrics = list_metrics(len(view.rows))
    scroll = max(0, min(int(view.scroll), metrics["max_scroll"]))
    rows = visible_row_geometry()
    top = rows[0][1]
    step = ROW_H + ROW_GAP
    for slot, (rx, ry, rw, rh) in enumerate(rows):
        # 内容第 (first+slot) 行画在本槽位：屏幕 y 范围 = 槽位范围。
        if not (rx <= x < rx + rw and ry <= y < ry + rh):
            continue
        index = scroll // step + slot
        if index >= len(view.rows):
            return None
        task = view.rows[index]
        for name, (bx, by, bw, bh) in button_geometry(slot, task.pinned).items():
            if bx <= x < bx + bw and by <= y < by + bh:
                return (name, task.task_id)
        return ("row", task.task_id)
    return None


# ---- 球 ----

import math  # noqa: E402  刷新箭头要用三角函数；放模块顶部亦可，这里紧邻使用处。

def render_ball(badge: int | None, phase: float = 0.0) -> Image.Image:
    """72x72 RGBA 圆球。phase 0..1 是按下回弹（0=静息）；badge 为待处理条数，None 不画角标。

    圆用 4x 超采样画再缩回，保证边缘 alpha 平滑；宿主靠 alpha 判 HTTRANSPARENT，
    圆外一圈必须是干净的 0。
    """
    phase = max(0.0, min(1.0, float(phase)))
    scale = 1.0 - 0.06 * phase
    center = BALL_SIZE / 2
    radius = (BALL_SIZE / 2 - 2) * scale

    ss = 4  # 超采样倍数
    big = Image.new("RGBA", (BALL_SIZE * ss, BALL_SIZE * ss), (0, 0, 0, 0))
    big_draw = ImageDraw.Draw(big)
    c = BALL_SIZE * ss / 2
    r = radius * ss
    big_draw.ellipse((c - r, c - r, c + r, c + r), fill=BALL_COLOR)
    # 极淡顶部内侧渐变（替代旧高光椭圆）。ImageDraw 是覆盖写，直接画会把球的 alpha
    # 也覆盖掉，所以画在独立 overlay 上再 alpha_composite 回来。
    overlay = Image.new("RGBA", big.size, (0, 0, 0, 0))
    ov_draw = ImageDraw.Draw(overlay)
    band_h = int(r * 0.9)
    for i in range(band_h):
        alpha = int(22 * (1 - i / band_h))
        if alpha <= 0:
            break
        dy = r - (i + 1)
        half = int((r * r - dy * dy) ** 0.5) - 2 * ss
        if half <= 0:
            continue
        yy = c - r + i
        ov_draw.line((c - half, yy, c + half, yy), fill=(255, 255, 255, alpha))
    big.alpha_composite(overlay)
    # 铃铛图标（旧版真机有、重写时丢了，2026-11-07 补回）：白色细线，约占球径 1/3，
    # 画在 big 上（白线画在 overlay 会被 alpha_composite 淡化）。4x 超采样下 stroke≈7。
    # 2026-11-07 调形：整体下移 2px 让提手露出角标；底檐收到每边只出 1px（20px 那版像「门」）。
    bell_w = 7
    white = (255, 255, 255)
    cx, cy = BALL_SIZE * ss / 2, BALL_SIZE * ss / 2
    bx0, bx1 = cx - 8 * ss, cx + 8 * ss          # 铃身两壁
    top_y = cy - 12 * ss                          # 拱顶（下移 2px）
    rim_y = cy + 10 * ss                          # 底檐
    big_draw.arc((bx0, top_y, bx1, top_y + 16 * ss), 180, 360, fill=white, width=bell_w)
    big_draw.line((bx0, cy - 4 * ss, bx0, rim_y), fill=white, width=bell_w)
    big_draw.line((bx1, cy - 4 * ss, bx1, rim_y), fill=white, width=bell_w)
    big_draw.line((bx0 - ss, rim_y, bx1 + ss, rim_y), fill=white, width=bell_w)  # 底檐每边出 1px
    big_draw.ellipse((cx - 2 * ss, rim_y - ss, cx + 2 * ss, rim_y + 3 * ss), outline=white, width=2 * ss)  # 铃舌
    big_draw.arc((cx - 4 * ss, top_y - 6 * ss, cx + 4 * ss, top_y + 2 * ss), 190, 350, fill=white, width=bell_w)  # 提手
    if badge is not None and badge > 0:
        text = "99+" if badge > 99 else str(badge)
        # 角标压铃铛右上：直径 18+描边 2 = 球径 28%（旧版 HTML .badge 同比），
        # 外沿 52+9+2=63 仍在 72 内；后画，压住铃铛。
        bx, by = BALL_SIZE * ss - 20 * ss, 20 * ss
        br = 9 * ss
        big_draw.ellipse((bx - br - 2 * ss, by - br - 2 * ss, bx + br + 2 * ss, by + br + 2 * ss), fill=BADGE_RING)
        big_draw.ellipse((bx - br, by - br, bx + br, by + br), fill=BADGE_BG)
        font = _font((FONT_BADGE - 1) * ss)
        left, top, right, bottom = big_draw.textbbox((0, 0), text, font=font)
        tw, th = right - left, bottom - top
        big_draw.text((bx - tw / 2 - left, by - th / 2 - top), text, font=font, fill=(255, 255, 255))
    image = big.resize((BALL_SIZE, BALL_SIZE), Image.LANCZOS)
    # LANCZOS 缩放会在圆外留 1~9 的 alpha 残值；宿主判透明要精确的 0，这里切掉。
    pixels = image.load()
    for yy in range(BALL_SIZE):
        for xx in range(BALL_SIZE):
            p = pixels[xx, yy]
            if p[3] < 10:
                pixels[xx, yy] = (0, 0, 0, 0)
    return image


# ---- 面板 ----

def _shadow_base() -> Image.Image:
    """面板 + 向外扩散的真 alpha 投影底图。"""
    spread = SHADOW_SPREAD
    width = PANEL_SIZE[0] + spread * 2
    height = PANEL_SIZE[1] + spread * 2
    mask = Image.new("L", (width, height), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle(
        (spread, spread, spread + PANEL_SIZE[0] - 1, spread + PANEL_SIZE[1] - 1),
        radius=PANEL_RADIUS,
        fill=SHADOW_ALPHA,
    )
    mask = mask.filter(ImageFilter.GaussianBlur(SHADOW_BLUR))
    base = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (width, height), (40, 40, 30, 0))
    shadow.putalpha(mask)
    base.alpha_composite(shadow)
    return base


def _status_color(kind: str) -> tuple[int, ...]:
    if kind == "success":
        return ACCENT
    if kind == "error":
        return DANGER
    return DIM


# ---- 过渡帧内容缓存 ----
# render_transition 的 t>0.6 段每帧都在重跑 render_panel + 逐像素抠投影区（~50ms/帧），
# 但内容几何已固定、每次画的是同一张图，只是整体透明度在变。同一次展开里 view 不变，
# 内容层只画一次，之后按 alpha 合成。键用 id(view)：缓存内容本身持有 view 引用，
# 拿不到时按 id 匹配失败就重画（_transition_cache 检查 cached[0] is not view）。
# 展开动画结束宿主换新 view（每次状态刷新都是新 PanelView）后旧条目自然失效，
# 最多存一条；这里不做 LRU，别为不存在的问题加结构。
_transition_cache: dict[int, tuple[PanelView, Image.Image]] = {}


def reset_transition_cache() -> None:
    """清掉内容层缓存。测试用它保证逐像素对比先走「画」再走「缓存」两条路径。"""
    _transition_cache.clear()



def render_panel(view: PanelView) -> Image.Image:
    """320x420 RGBA 面板（含向外扩散的投影区）。"""
    image = _shadow_base()
    pad = SHADOW_SPREAD
    draw = ImageDraw.Draw(image)
    x0, y0 = pad, pad
    x1, y1 = pad + PANEL_SIZE[0] - 1, pad + PANEL_SIZE[1] - 1
    draw.rounded_rectangle((x0, y0, x1, y1), radius=PANEL_RADIUS, fill=PANEL_BG, outline=PANEL_EDGE, width=1)

    ox, oy = pad, pad  # 面板内 -> 图上的偏移

    # 标题「待处理 · N 条」（N=总数，不是可见行数）+ 右上刷新弧 / ×
    count = len(view.rows)
    title = f"待处理 · {count} 条"
    _text(draw, (x0 + PANEL_PAD, y0 + 7), title, FONT_TITLE, INK, bold=True)
    # 刷新图标：U+21BB 在 msyh 里没字形会画成豆腐块，改手绘——3/4 圆弧 + 箭头短线。
    gx, gy, gw, gh = refresh_geometry()
    gcx, gcy = ox + gx + gw / 2, oy + gy + gh / 2
    gr = 7
    draw.arc((gcx - gr, gcy - gr, gcx + gr, gcy + gr), start=-60, end=170, fill=DIM, width=2)
    # 箭头：在弧的起点端（约 -60° 方向）画两根短线组成尖
    ang = math.radians(-60)
    tipx, tipy = gcx + gr * math.cos(ang), gcy + gr * math.sin(ang)
    draw.line((tipx - 4, tipy - 3, tipx, tipy), fill=DIM, width=2)
    draw.line((tipx - 1, tipy + 4, tipx, tipy), fill=DIM, width=2)

    # 右上角关闭按钮（圆形浅灰底 + 深色 ×，两条 2px 线，对比 ≥4.5:1）
    cx, cy, cw, ch = close_geometry()
    draw.ellipse((ox + cx, oy + cy, ox + cx + cw, oy + cy + ch), fill=(242, 241, 236))
    s = cw * 0.26  # 十字臂长
    mx, my = ox + cx + cw / 2, oy + cy + ch / 2
    for dx, dy in ((s, s), (s, -s)):
        draw.line((mx - dx, my - dy, mx + dx, my + dy), fill=X_STROKE, width=2)

    if not view.rows:
        _text(draw, (x0 + PANEL_PAD, y0 + 160), "现在没有要处理的事。", FONT_ROW, DIM)
        _text(draw, (x0 + PANEL_PAD, y0 + 182), "有新消息会变成待办出现在这里。", FONT_META, GHOST)
    else:
        metrics = list_metrics(count)
        rows = visible_row_geometry()
        first = max(0, min(int(view.scroll), metrics["max_scroll"])) // (ROW_H + ROW_GAP)
        for slot, (rx, ry, rw, rh) in enumerate(rows):
            index = first + slot
            if index >= count:
                break
            task = view.rows[index]
            draw.rounded_rectangle(
                (x0 + rx, y0 + ry, x0 + rx + rw - 1, y0 + ry + rh - 1),
                radius=ROW_RADIUS,
                fill=ROW_BG,
                outline=PANEL_EDGE,
                width=1,
            )
            # 标题（13px，按像素截断到按钮左侧 −8，绝不压按钮）+ 时间右对齐浅灰
            buttons = button_geometry(slot, task.pinned)
            btn_left = min(bx for bx, _, _, _ in buttons.values()) if buttons else rx + rw
            title, _ = _truncate(task.title, FONT_ROW, btn_left - 8 - (rx + 10))
            _text(draw, (x0 + rx + 10, y0 + ry + 8), title, FONT_ROW, INK)
            if task.when:
                when = task.when if len(task.when) <= 12 else task.when[:12]
                when_w = _font(FONT_META).getlength(when)
                when_x = rx + rw - 10 - when_w
                # 时间不许压标题：若与标题尾重叠就往右推，推不出去再截标题侧已保证不越界。
                title_end = rx + 10 + _font(FONT_ROW).getlength(title)
                when_x = max(when_x, title_end)
                when_x = min(when_x, rx + rw - 10 - when_w) if when_x + when_w <= rx + rw else title_end
                _text(draw, (x0 + when_x, y0 + ry + 10), when, FONT_META, GHOST)
            # 来源行（10px 浅灰）：★ 前缀 + 群名。来源行纵向（ry+28~ry+44）与按钮区
            # （ry+24~ry+46）重叠，横向必须和标题一样让出按钮区，否则群名压按钮。
            meta_prefix = "★ " if task.pinned else ""
            prefix_w = _font(FONT_META).getlength(meta_prefix)
            source, _ = _truncate(task.source or "", FONT_META, btn_left - 8 - (rx + 10) - prefix_w)
            _text(draw, (x0 + rx + 10, y0 + ry + 28), meta_prefix, FONT_META, DIM)
            _text(draw, (x0 + rx + 10 + prefix_w, y0 + ry + 28), source, FONT_META, DIM)
            # 三个按钮（pinned 行 pin 标签是「已钉」状态词，几何与命中测试同一份）
            for name, (bx, by, bw, bh) in buttons.items():
                is_pin = name == "pin"
                label = PIN_ACTIVE_LABEL if (is_pin and task.pinned) else BUTTON_LABELS[name]
                enabled = not view.busy
                fill = (233, 229, 216) if enabled else (238, 236, 228)
                color = BTN_INK if enabled else GHOST
                if name == "done" and enabled:
                    color = BTN_INK_DONE  # 主操作深一档，底不变，不大色块
                if is_pin and task.pinned and enabled:
                    color = ACCENT
                draw.rounded_rectangle(
                    (x0 + bx, y0 + by, x0 + bx + bw - 1, y0 + by + bh - 1),
                    radius=6,
                    fill=fill,
                )
                _text(draw, (x0 + bx + (bw - 2 * 11 * len(label)) // 2, y0 + by + 3), label, FONT_BUTTON, color)

        # 滚动条：内容超屏才出现，右侧 3px 细条，滑块长度按比例，浅灰。
        if metrics["max_scroll"] > 0:
            track_top = metrics["top"]
            track_h = metrics["view_h"]
            # x 也要加 x0：漏了会整体左移一个投影边距，滑块压进行卡右缘。
            track_x = x0 + PANEL_SIZE[0] - PANEL_PAD - SCROLLBAR_W
            draw.rounded_rectangle(
                (track_x, y0 + track_top, track_x + SCROLLBAR_W, y0 + track_top + track_h),
                radius=1,
                fill=(226, 222, 208),
            )
            ratio = track_h / metrics["content_h"]
            thumb_h = max(24, int(track_h * ratio))
            scroll = max(0, min(int(view.scroll), metrics["max_scroll"]))
            thumb_top = track_top + int((track_h - thumb_h) * scroll / metrics["max_scroll"])
            draw.rounded_rectangle(
                (track_x, y0 + thumb_top, track_x + SCROLLBAR_W, y0 + thumb_top + thumb_h),
                radius=1,
                fill=(180, 176, 160),
            )

    # 底部状态行
    if view.status:
        _text(draw, (x0 + PANEL_PAD, y0 + PANEL_SIZE[1] - STATUS_H - 5), view.status, FONT_META, _status_color(view.status_kind))

    return image


# ---- 过渡帧（球 → 面板，分层绘制，不缩放内容防糊字）----

def _ease_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1.0 - (1.0 - t) ** 3


def render_transition(t: float, view: PanelView) -> Image.Image:
    """展开过渡帧。t=0 是球，t=1 是完整面板。

    分层规则：几何 ease-out 从 72x72 长到面板全尺寸、锚右下角；卡片底/圆角/投影在
    t=0~0.55 淡入；内容（标题/行/按钮/滚动条）不缩放，t>0.6 才出现、用最终尺寸绘制、
    alpha 0.6~1.0 线性淡入；球在 t<0.35 时画在原位、缩到 0.92 后淡出。
    """
    t = max(0.0, min(1.0, float(t)))
    pad = SHADOW_SPREAD
    full_w = PANEL_SIZE[0] + pad * 2
    full_h = PANEL_SIZE[1] + pad * 2
    image = Image.new("RGBA", (full_w, full_h), (0, 0, 0, 0))
    if t <= 0.0:
        ball = render_ball(None)
        image.alpha_composite(ball, (full_w - BALL_SIZE - pad, full_h - BALL_SIZE - pad))
        return image

    grow = _ease_out(min(1.0, t / 0.85))
    w = int(BALL_SIZE + (PANEL_SIZE[0] - BALL_SIZE) * grow)
    h = int(BALL_SIZE + (PANEL_SIZE[1] - BALL_SIZE) * grow)
    # 锚右下角：面板内容区（含投影）右下对齐最终位置。
    card_x = full_w - pad - w
    card_y = full_h - pad - h

    # 卡片底淡入 t=0~0.55
    card_alpha = int(255 * max(0.0, min(1.0, t / 0.55)))
    if card_alpha > 0 and w > 4 and h > 4:
        card = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        card_draw = ImageDraw.Draw(card)
        radius = max(4, int(PANEL_RADIUS * grow))
        card_draw.rounded_rectangle(
            (0, 0, w - 1, h - 1), radius=radius,
            fill=PANEL_BG + (card_alpha,), outline=PANEL_EDGE + (card_alpha,), width=1,
        )
        # 投影：只画半透明黑底，blur 半径随尺寸缩
        shadow = Image.new("RGBA", (full_w, full_h), (0, 0, 0, 0))
        sh_draw = ImageDraw.Draw(shadow)
        sh_alpha = int(SHADOW_ALPHA * card_alpha / 255)
        sh_draw.rounded_rectangle(
            (card_x, card_y, card_x + w - 1, card_y + h - 1),
            radius=radius, fill=(40, 40, 30, sh_alpha),
        )
        blur = max(2, int(SHADOW_BLUR * grow))
        image.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(blur)))
        image.alpha_composite(card, (card_x, card_y))

    # 内容淡入 t>0.6，最终尺寸绘制 + 整体 alpha。
    # 内容几何在 t>0.6 段固定（w 已到全尺寸），每帧重跑 render_panel 的排版绘制再逐像素
    # 抠投影区要 50ms/帧——纯浪费：同一次展开里内容不变，画一次、抠一次，之后按 alpha 合成。
    content_alpha = int(255 * max(0.0, min(1.0, (t - 0.6) / 0.4)))
    if content_alpha > 0 and w >= PANEL_SIZE[0] * 0.98:
        cached = _transition_cache.get(id(view))
        if cached is None or cached[0] is not view:
            cached = None
        if cached is None:
            content = render_panel(view)
            # 去掉面板自己的投影底，只留内容（面板图上投影在四周 pad 区，直接裁掉会切掉边缘描边，
            # 所以用「纸色圆角矩形以内」的区域：先把投影区抹成透明再贴）。
            content_no_shadow = content.copy()
            px = content_no_shadow.load()
            for yy in range(content_no_shadow.height):
                for xx in range(content_no_shadow.width):
                    in_card = (pad <= xx < pad + PANEL_SIZE[0] and pad <= yy < pad + PANEL_SIZE[1])
                    if not in_card:
                        p = px[xx, yy]
                        px[xx, yy] = (p[0], p[1], p[2], 0)
            cached = (view, content_no_shadow)
            # 只保留最后一条：宿主每次刷新都新建 PanelView，不清会逐条强引用缓慢增长。
            _transition_cache.clear()
            _transition_cache[id(view)] = cached
        content_no_shadow = cached[1]
        if content_alpha < 255:
            # putalpha 是原地写，直接写缓存的图会把下一帧的 alpha 也改掉——先拷一份再调。
            content_no_shadow = content_no_shadow.copy()
            a = content_no_shadow.getchannel("A").point(lambda v: v * content_alpha // 255)
            content_no_shadow.putalpha(a)
        image.alpha_composite(content_no_shadow)

    # 球：t<0.35 画原位、缩到 0.92 后淡出
    if t < 0.35:
        ball = render_ball(None)
        scale = 1.0 - 0.08 * (t / 0.35)
        bw = int(BALL_SIZE * scale)
        ball = ball.resize((bw, bw), Image.LANCZOS)
        alpha = int(255 * (1.0 - t / 0.35))
        if alpha < 255:
            a = ball.getchannel("A").point(lambda v: v * alpha // 255)
            ball.putalpha(a)
        bx = full_w - pad - BALL_SIZE // 2 - bw // 2
        by = full_h - pad - BALL_SIZE // 2 - bw // 2
        image.alpha_composite(ball, (bx, by))
    return image


def build_preview_frames(badge: int, rows: tuple[TaskRow, ...]) -> Image.Image:
    """expanded-frames.png：render_transition 的 5 帧（t=0/0.25/0.5/0.75/1.0）横排，每帧注明 t。"""
    view = PanelView(rows=rows)
    ts = (0.0, 0.25, 0.5, 0.75, 1.0)
    frames = [render_transition(t, view) for t in ts]
    gap = 24
    label_h = 16
    width = sum(f.width for f in frames) + gap * (len(frames) - 1)
    height = max(f.height for f in frames) + label_h
    sheet = Image.new("RGBA", (width, height), (128, 128, 128, 255))
    draw = ImageDraw.Draw(sheet)
    x = 0
    for t, frame in zip(ts, frames):
        y = (height - label_h - frame.height) // 2
        sheet.alpha_composite(frame, (x, y))
        draw.text((x + frame.width // 2 - 20, height - label_h), f"t={t}", font=_font(11), fill=(20, 20, 20))
        x += frame.width + gap
    return sheet
