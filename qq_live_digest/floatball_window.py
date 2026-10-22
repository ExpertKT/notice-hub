"""Native floating ball window — tkinter + PIL, colour-key transparency, no browser shell."""
from __future__ import annotations

import ctypes
import json
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import winreg
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from PIL import Image, ImageDraw, ImageFont, ImageTk

from qq_live_digest.floatball_host import ensure_dpi_aware, snap_position, write_state

MAGIC = "#ff00fe"
CLOSED = 72
PANEL_W = 360
PANEL_H = 520
HEADER_H = 48
ROW_H = 84
MARGIN = 8
DOCK_CLEARANCE = 64  # 让出屏幕底部第三方 dock/任务栏的高度，别压在上面
STATE_NAME = "floatball-state.json"
POLL_SECONDS = 60
SS = 3

TEAL = (18, 105, 91)
TEAL_DARK = (10, 74, 64)
PAPER = (255, 255, 255)
PAPER_ALT = (242, 244, 246)
INK = (23, 39, 34)
MUTED = (92, 107, 100)
LINE = (207, 214, 211)
AMBER = (128, 84, 17)
AMBER_SOFT = (255, 243, 219)
RED = (165, 49, 45)
RED_SOFT = (255, 238, 237)

FONT_REG = r"C:\Windows\Fonts\msyh.ttc"
FONT_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"


def _debug(msg: str) -> None:
    try:
        with open(r"F:\notice-hub\data\floatball-debug.log", "a", encoding="utf-8") as stream:
            stream.write("%.3f %s\n" % (__import__("time").time(), msg))
    except OSError:
        pass



SLIVER_LONG = 64
SLIVER_THICK = 10


def sliver_box(edge: str, ball_xy: tuple[int, int], work: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Where the edge tab goes: flush to that edge, centred on the ball, clamped inside the work area."""
    width, height = (SLIVER_THICK, SLIVER_LONG) if edge in ("left", "right") else (SLIVER_LONG, SLIVER_THICK)
    cx = ball_xy[0] + CLOSED // 2
    cy = ball_xy[1] + CLOSED // 2
    if edge == "right":
        x = work[2] - width
        y = min(max(work[1], cy - height // 2), work[3] - height)
    elif edge == "left":
        x = work[0]
        y = min(max(work[1], cy - height // 2), work[3] - height)
    elif edge == "top":
        y = work[1]
        x = min(max(work[0], cx - width // 2), work[2] - width)
    else:
        y = work[3] - height
        x = min(max(work[0], cx - width // 2), work[2] - width)
    return x, y, width, height


def ball_box(edge: str, sliver_xy: tuple[int, int], work: tuple[int, int, int, int]) -> tuple[int, int]:
    """Inverse of sliver_box: come back out of the edge with the normal margin."""
    cx = sliver_xy[0] + SLIVER_THICK // 2 if edge in ("left", "right") else sliver_xy[0] + SLIVER_LONG // 2
    cy = sliver_xy[1] + SLIVER_LONG // 2 if edge in ("left", "right") else sliver_xy[1] + SLIVER_THICK // 2
    if edge == "right":
        x = work[2] - CLOSED - MARGIN
        y = min(max(work[1], cy - CLOSED // 2), work[3] - CLOSED)
    elif edge == "left":
        x = work[0] + MARGIN
        y = min(max(work[1], cy - CLOSED // 2), work[3] - CLOSED)
    elif edge == "top":
        y = work[1] + MARGIN
        x = min(max(work[0], cx - CLOSED // 2), work[2] - CLOSED)
    else:
        y = work[3] - CLOSED - MARGIN
        x = min(max(work[0], cx - CLOSED // 2), work[2] - CLOSED)
    return x, y


def _font(size: int, bold: bool = False) -> "ImageFont.FreeTypeFont":
    try:
        return ImageFont.truetype(FONT_BOLD if bold else FONT_REG, size * SS)
    except OSError:
        return ImageFont.load_default()


def task_rows(tasks: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in ("pinned", "today", "week", "later"):
        for item in tasks.get(key) or []:
            if not isinstance(item, dict):
                continue
            if item.get("done") or item.get("status") in ("done", "dismissed"):
                continue
            rows.append(item)
    return rows


def unread_total(tasks: dict[str, Any]) -> int:
    return len(task_rows(tasks)) + len(tasks.get("candidates") or [])


def _round_rect(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], radius: int, fill: tuple[int, int, int]) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def _finish(canvas: Image.Image, mask_source: Image.Image) -> Image.Image:
    """Colour-key transparency is binary, so harden the mask to avoid a pink halo."""
    mask = mask_source.split()[-1].resize(canvas.size, Image.Resampling.LANCZOS).point(lambda value: 255 if value > 64 else 0)
    out = Image.new("RGB", canvas.size, (255, 0, 254))
    out.paste(canvas, (0, 0), mask)
    return out


class FloatBallWindow:
    """Tk must own its thread, so every entry point marshals onto the UI thread."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.state_path = self.data_dir / STATE_NAME
        self.base_url = ""
        self.token = ""
        self.expanded = False
        self.mode = "ball"
        self.edge = "right"
        self._idle_since: float | None = None
        self._frames: list[Image.Image] = []
        self.edge = "right"
        self.along = 0
        self._left_ball_at = 0.0
        self._pointer_inside = False
        self._hover_key: int | None = None
        self._hover_row: int | None = None
        self._row_boxes: list[tuple[int, int, int, int]] = []
        self._hover_frame = 0
        self._animating = False
        self._breath_phase = 0
        self.rows: list[dict[str, Any]] = []
        self.candidates = 0
        self.message = ""
        self.hit_zones: list[tuple[tuple[int, int, int, int], Callable[[], None]]] = []
        self._drag_origin: tuple[int, int] | None = None
        self._drag_moved = False
        self._window_origin = (0, 0)
        self._photo: ImageTk.PhotoImage | None = None
        self.root: tk.Tk | None = None
        self.label: tk.Label | None = None
        self._queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self.visible = False

    # --- thread plumbing ----------------------------------------------
    def start(self, url: str) -> None:
        ensure_dpi_aware()
        base, _, query = str(url).partition("?")
        params = urllib.parse.parse_qs(query)
        self.base_url = base.split("/floatball")[0].rstrip("/")
        self.token = (params.get("token") or [""])[0]
        if self._thread and self._thread.is_alive():
            self.show()
            return
        self._thread = threading.Thread(target=self._boot, name="floatball-ui", daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("浮球窗口初始化超时")
        self.show()

    def _apply_toolwindow(self) -> None:
        """不进任务栏/Alt-Tab；overrideredirect 只挡边框，不挡任务栏条目。"""
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
        ex = user32.GetWindowLongW(hwnd, -20)
        user32.SetWindowLongW(hwnd, -20, ex | 0x00000080)

    def _boot(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.configure(bg=MAGIC)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", MAGIC)
        self.label = tk.Label(self.root, bd=0, bg=MAGIC, highlightthickness=0)
        self.label.place(x=0, y=0)
        self._bind()
        self._apply_toolwindow()
        self._ready.set()
        self.root.after(50, self._drain)
        self.root.mainloop()

    def _drain(self) -> None:
        while True:
            try:
                action = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                action()
            except Exception as exc:
                import traceback
                traceback.print_exc()
                print('floatball action failed:', exc)
        if self.root is not None:
            try:
                self.root.after(50, self._drain)
            except tk.TclError:
                pass

    def _call(self, action: Callable[[], None]) -> None:
        """Tk is not thread-safe: queue the call and let the UI thread run it."""
        self._queue.put(action)


    # --- rendering ----------------------------------------------------
    def _mask_ball(self) -> Image.Image:
        size = CLOSED * SS
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
        return mask

    def _render_ball(self) -> Image.Image:
        size = CLOSED * SS
        canvas = Image.new("RGBA", (size, size), TEAL + (255,))
        draw = ImageDraw.Draw(canvas)
        draw.ellipse((0, 0, size - 1, size - 1), fill=TEAL, outline=TEAL_DARK, width=SS)
        if self._breath_phase:
            draw.ellipse((12 * SS, 12 * SS, size - 12 * SS, size - 12 * SS), outline=(40, 130, 113), width=SS)
        total = len(self.rows) + self.candidates
        cx = size // 2
        bubble = (14 * SS, 14 * SS, 58 * SS, 54 * SS)
        draw.rounded_rectangle(bubble, radius=10 * SS, fill=(255, 255, 255))
        draw.polygon([(24 * SS, 50 * SS), (27 * SS, 58 * SS), (36 * SS, 52 * SS)], fill=(255, 255, 255))
        text = "99+" if total > 99 else str(total)
        draw.text((36 * SS, 34 * SS), text, font=_font(16 if total > 99 else 22, True), fill=TEAL, anchor="mm")
        mask = self._mask_ball()
        return _finish(canvas.convert("RGB").resize((CLOSED, CLOSED), Image.LANCZOS), mask)

    def _render_panel(self) -> Image.Image:
        width, height = PANEL_W * SS, PANEL_H * SS
        canvas = Image.new("RGB", (width, height), PAPER)
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=16 * SS, fill=PAPER, outline=LINE, width=SS)
        draw.text((16 * SS, HEADER_H * SS / 2), "置顶事项", font=_font(14, True), fill=INK, anchor="lm")
        total = len(self.rows) + self.candidates
        draw.text((94 * SS, HEADER_H * SS / 2), "共 %d 条" % total, font=_font(10), fill=MUTED, anchor="lm")
        draw.line((0, HEADER_H * SS, width, HEADER_H * SS), fill=LINE, width=SS)
        self.hit_zones = []
        self._row_boxes = []
        self._chip(draw, width - 78 * SS, 8 * SS, 30 * SS, 26 * SS, "\u5237", self.refresh)
        self._chip(draw, width - 42 * SS, 8 * SS, 30 * SS, 26 * SS, "\u00d7", self.collapse)

        y = HEADER_H + 10
        if not self.rows:
            draw.text((width / 2, (y + 40) * SS), self.message or "还没有置顶事项，去网页里点一下「置顶」",
                      font=_font(12), fill=MUTED, anchor="mm")
        visible_rows = max(0, (PANEL_H - 8 - y) // ROW_H)
        for index, task in enumerate(self.rows[:visible_rows]):
            self._row(draw, width, index, task, y)
            y += ROW_H
        remaining = total - len(self.rows[:visible_rows])
        if remaining > 0 and y + 28 <= PANEL_H:
            draw.text((width / 2, (y + 12) * SS), "还有 %d 条 · 去网页看" % remaining,
                      font=_font(10, True), fill=TEAL, anchor="mm")

        mask = Image.new("L", (width, height), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, height - 1), radius=16 * SS, fill=255)
        return _finish(canvas.resize((PANEL_W, PANEL_H), Image.LANCZOS), mask)

    def _row(self, draw: ImageDraw.ImageDraw, width: int, index: int, task: dict[str, Any], y: int) -> None:
        urgent = bool(task.get("effective_urgent") or task.get("urgent_override"))
        top, bottom = y * SS, (y + ROW_H - 6) * SS
        hovered = self._hover_row == index
        self._row_boxes.append((14, y + 2, width // SS - 14, y + ROW_H - 8))
        draw.rounded_rectangle((14 * SS, top, width - 14 * SS, bottom), radius=8 * SS,
                               fill=(238, 243, 241) if hovered else (PAPER_ALT if index % 2 else PAPER), outline=TEAL if hovered else LINE, width=SS)
        if urgent:
            draw.rounded_rectangle((16 * SS, top, 20 * SS, bottom), radius=2 * SS, fill=AMBER)
        summary = " ".join(str(task.get("summary") or task.get("details") or "未命名待办").split())
        draw.text((32 * SS, (y + 18) * SS), self._clip(summary, 18), font=_font(11, True), fill=INK, anchor="lm")
        meta_parts = [str(part) for part in (task.get("groups") or [])[:2] if part]
        if task.get("deadline_text"):
            meta_parts.append(str(task["deadline_text"]))
        if meta_parts:
            draw.text((32 * SS, (y + 40) * SS), self._clip(" · ".join(meta_parts), 30),
                      font=_font(9), fill=MUTED, anchor="lm")
        self._chip(draw, (width // SS - 206) * SS, (y + 52) * SS, 64 * SS, 28 * SS, "完成",
                   lambda t=task: self.act(t, "done"))
        self._chip(draw, (width // SS - 134) * SS, (y + 52) * SS, 64 * SS, 28 * SS, "稍后",
                   lambda t=task: self.act(t, "snooze"))
        self._chip(draw, (width // SS - 62) * SS, (y + 52) * SS, 48 * SS, 28 * SS, "忽略",
                   lambda t=task: self.act(t, "dismiss"))

    def _chip(self, draw: ImageDraw.ImageDraw, x: int, y: int, width: int, height: int,
              label: str, command: Callable[[], None], fill: tuple[int, int, int] = PAPER_ALT,
              ink: tuple[int, int, int] = INK) -> None:
        hovered = len(self.hit_zones) == self._hover_key
        draw.rounded_rectangle((x, y, x + width, y + height), radius=6 * SS,
                               fill=(214, 228, 224) if hovered else fill,
                               outline=TEAL if hovered else LINE, width=SS)
        draw.text((x + width / 2, y + height / 2), label, font=_font(10, True), fill=ink, anchor="mm")
        self.hit_zones.append(((x // SS, y // SS, (x + width) // SS, (y + height) // SS), command))

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"

    def _render_sliver(self) -> Image.Image:
        canvas = Image.new("RGB", (64 * SS, 10 * SS), TEAL)
        draw = ImageDraw.Draw(canvas)
        for x in (22, 32, 42):
            draw.ellipse(((x - 1) * SS, 4 * SS, (x + 1) * SS, 6 * SS), fill=PAPER)
        return canvas.resize((64, 10), Image.Resampling.LANCZOS)

    def _paint(self) -> None:
        if self.label is None:
            return
        if self.mode == "sliver":
            image = self._render_sliver()
            if self.edge in ("top", "bottom"):
                image = image.rotate(90, expand=True)
        elif self.expanded:
            image = self._render_panel()
        else:
            image = self._render_ball()
        self._photo = ImageTk.PhotoImage(image)
        self.label.configure(image=self._photo, width=image.width, height=image.height)
        self.label.place_configure(width=image.width, height=image.height)
        if self.mode == "ball" and not self.expanded:
            self.root.after(120, self._breath)

    def _breath(self) -> None:
        if self.root is None or self.expanded or self.mode != "ball" or self._animating:
            return
        self._breath_phase = (self._breath_phase + 1) % 50
        self._paint()

    def _on_hover(self, event: "tk.Event") -> None:
        point = (event.x, event.y)
        key = next((i for i, (box, _cmd) in enumerate(self.hit_zones)
                    if box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]), None)
        row = next((i for i, box in enumerate(self._row_boxes)
                    if box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]), None)
        if key == self._hover_key and row == self._hover_row:
            return
        self._hover_key = key
        self._hover_row = row
        self.label.configure(cursor="hand2" if (key is not None or row is not None) else "")
        self._paint()

    def _on_leave(self, _event: "tk.Event") -> None:
        if self._hover_key is not None or self._hover_row is not None:
            self._hover_key = None
            self._hover_row = None
            self.label.configure(cursor="")
            self._paint()

    # --- window plumbing ----------------------------------------------
    @staticmethod
    def _work_area() -> tuple[int, int, int, int]:
        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        rect = RECT()
        user32 = ctypes.windll.user32
        if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
        return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)

    def _calibrate(self) -> None:
        """Ask Win32 for 100px and learn the factor the host applies; avoids guessing the DPI mode."""
        try:
            hwnd = ctypes.windll.user32.GetParent(self._hwnd()) or self._hwnd()
            ctypes.windll.user32.SetWindowPos(hwnd, -1, 0, 0, 100, 100, 0x0010)
            rect = (ctypes.c_long * 4)()
            ctypes.windll.user32.GetWindowRect(hwnd, rect)
            actual = rect[2] - rect[0]
            self._factor = 100.0 / actual if actual > 10 else 1.0
        except (AttributeError, OSError):
            self._factor = 1.0

    def _dpi_scale(self) -> float:
        try:
            return max(1.0, float(self.root.winfo_fpixels("1i")) / 96.0)
        except (tk.TclError, ValueError):
            return 1.0

    def _hwnd(self) -> int:
        return int(self.root.winfo_id())

    def _calibrate(self) -> None:
        """Tk and Win32 disagree on DPI here; ask for a known size and learn the real factor."""
        try:
            self.root.geometry("200x200+120+120")
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self._hwnd()) or self._hwnd()
            rect = (ctypes.c_long * 4)()
            ctypes.windll.user32.GetWindowRect(hwnd, rect)
            actual = rect[2] - rect[0]
            self._factor = 200.0 / actual if actual > 20 else 1.0
        except (tk.TclError, AttributeError, OSError):
            self._factor = 1.0

    def _place(self, x: int, y: int, width: int, height: int) -> None:
        factor = getattr(self, "_factor", 1.0)
        self.root.geometry("%dx%d+%d+%d" % (
            int(round(width * factor)), int(round(height * factor)),
            int(round(x * factor)), int(round(y * factor)),
        ))
        self.root.update_idletasks()

    def _position(self) -> tuple[int, int]:
        return self.root.winfo_x(), self.root.winfo_y()

    def _load_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        try:
            x, y = self._position()
            write_state(self.state_path, x, y, self.expanded)
        except OSError:
            pass

    # --- state --------------------------------------------------------
    def _transition(self, start_xy, target_xy, start_size, target_size) -> None:
        """Geometry-only tween: content is already painted at final size, so only the frame moves."""
        self._animating = True
        frames, duration = 11, 180

        def reduced_motion() -> bool:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\\Accessibility\\Animation") as key:
                    message = int(winreg.QueryValueEx(key, "MessageDuration")[0])
                    client = int(winreg.QueryValueEx(key, "ClientAreaAnimation")[0])
                    return message == 0 or client == 0
            except (OSError, ValueError, TypeError):
                return False

        def finish() -> None:
            self._place(target_xy[0], target_xy[1], target_size[0], target_size[1])
            self._animating = False

        if reduced_motion():
            finish()
            return

        def step(frame: int) -> None:
            if frame >= frames:
                finish()
                return
            ratio = 1 - (1 - frame / frames) ** 3
            x = round(start_xy[0] + (target_xy[0] - start_xy[0]) * ratio)
            y = round(start_xy[1] + (target_xy[1] - start_xy[1]) * ratio)
            width = round(start_size[0] + (target_size[0] - start_size[0]) * ratio)
            height = round(start_size[1] + (target_size[1] - start_size[1]) * ratio)
            self._place(x, y, width, height)
            self.root.after(max(8, duration // frames), lambda: step(frame + 1))

        step(1)

    def collapse(self) -> None:
        if self._animating:
            self.root.after(200, self.collapse)
            return
        if not self.expanded:
            return
        start_x, start_y = self.root.winfo_x(), self.root.winfo_y()
        work = self._work_area()
        # 球始终回到面板的右下角，保证「展开再收起」位置不变（以前按左上角锚点会一路往上漂）。
        target_x = min(max(work[0] + MARGIN, start_x + PANEL_W - CLOSED), work[2] - CLOSED - MARGIN)
        target_y = min(max(work[1] + MARGIN, start_y + PANEL_H - CLOSED), work[3] - CLOSED - MARGIN)
        self.expanded = False
        self._transition((start_x, start_y), (target_x, target_y), (PANEL_W, PANEL_H), (CLOSED, CLOSED))
        self._save_state()

    def expand(self) -> None:
        _debug("expand requested expanded=%s" % self.expanded)
        if self._animating:
            self.root.after(200, self.expand)
            return
        if self.expanded or self.mode != "ball":
            return
        start_x, start_y = self.root.winfo_x(), self.root.winfo_y()
        work = self._work_area()
        # 面板从球的右下角向左上展开；与 collapse 互为逆运算，位置不会漂。
        target_x = min(max(work[0] + MARGIN, start_x + CLOSED - PANEL_W), work[2] - PANEL_W - MARGIN)
        target_y = min(max(work[1] + MARGIN, start_y + CLOSED - PANEL_H), work[3] - PANEL_H - MARGIN)
        self.expanded = True
        self._transition((start_x, start_y), (target_x, target_y), (CLOSED, CLOSED), (PANEL_W, PANEL_H))
        self._save_state()
        self.refresh()
        self._idle_since = None

    # --- morph animation (pre-rendered frames, colour-key transparency) ---
    MORPH_FRAMES = 14
    MORPH_MS = 45

    def _morph_mask(self, progress: float):
        import math
        from PIL import ImageFilter
        width, height = PANEL_W * SS, PANEL_H * SS
        mask = Image.new("L", (width, height), 0)
        draw = ImageDraw.Draw(mask)
        base = max(width, height) * (0.18 + 1.25 * progress)
        points = []
        for step in range(72):
            angle = 2 * math.pi * step / 72
            wobble = 1 + 0.12 * math.sin(3 * angle + progress * 6.0) + 0.06 * math.sin(5 * angle - progress * 4.0)
            radius = base * wobble
            points.append((width + radius * math.cos(angle), height + radius * math.sin(angle)))
        draw.polygon(points, fill=255)
        mask = mask.filter(ImageFilter.GaussianBlur(max(1.0, 14 * (1 - progress))))
        # colour-key transparency is binary: a blurred edge would blend with the key and show pink.
        mask = mask.point(lambda value: 255 if value > 128 else 0)
        return mask.resize((PANEL_W, PANEL_H), Image.LANCZOS).point(lambda value: 255 if value > 128 else 0)

    def _morph_frames(self) -> list[Image.Image]:
        if self._frames:
            return self._frames
        from PIL import ImageChops, ImageFilter
        panel = self._render_panel()
        ball = self._render_ball()
        ball_mask = Image.new("L", (CLOSED, CLOSED), 0)
        ImageDraw.Draw(ball_mask).ellipse((0, 0, CLOSED - 1, CLOSED - 1), fill=255)
        frames = []
        for index in range(self.MORPH_FRAMES):
            progress = index / (self.MORPH_FRAMES - 1)
            eased = 1 - (1 - progress) ** 3
            stage = Image.new("RGB", (PANEL_W, PANEL_H), (255, 0, 254))
            mask = self._morph_mask(eased)
            revealed = Image.new("RGB", (PANEL_W, PANEL_H), (255, 0, 254))
            revealed.paste(panel, (0, 0), mask)
            rim = ImageChops.subtract(mask.filter(ImageFilter.MaxFilter(3)), mask)
            revealed.paste(Image.new("RGB", (PANEL_W, PANEL_H), (206, 232, 224)), (0, 0), rim)
            stage.paste(revealed, (0, 0))

            # the bubble spins out and shrinks into the liquid; binary mask keeps edges clean.
            spin = ball.rotate(-70 * eased, resample=Image.BICUBIC, expand=True)
            spin_mask = ball_mask.rotate(-70 * eased, resample=Image.BICUBIC, expand=True)
            scale = (1.0 + 0.35 * eased) * max(0.0, 1 - max(0.0, eased - 0.45) / 0.55)
            if scale > 0.06:
                size = (max(1, int(spin.width * scale)), max(1, int(spin.height * scale)))
                spin_small = spin.resize(size, Image.LANCZOS)
                mask_small = spin_mask.resize(size, Image.LANCZOS).point(lambda value: 255 if value > 110 else 0)
                x = PANEL_W - size[0] // 2 - int(110 * eased)
                y = PANEL_H - size[1] // 2 - int(80 * eased)
                stage.paste(spin_small, (x, y), mask_small)
            frames.append(stage)
        self._frames = frames
        return frames

    def _play_morph(self, expanding: bool) -> None:
        if self._animating:
            return
        frames = self._morph_frames()
        if not frames:
            return
        sequence = frames if expanding else list(reversed(frames))
        work = self._work_area()
        if expanding:
            panel_x = min(max(work[0] + MARGIN, self.root.winfo_x() + CLOSED - PANEL_W), work[2] - PANEL_W - MARGIN)
            panel_y = min(max(work[1] + MARGIN, self.root.winfo_y() + CLOSED - PANEL_H), work[3] - PANEL_H - MARGIN)
        else:
            panel_x, panel_y = self.root.winfo_x(), self.root.winfo_y()
        self._place(panel_x, panel_y, PANEL_W, PANEL_H)
        self._animating = True

        def show(index: int) -> None:
            if index >= len(sequence):
                self._animating = False
                if expanding:
                    self._place(panel_x, panel_y, PANEL_W, PANEL_H)
                else:
                    work = self._work_area()
                    x = min(max(work[0] + MARGIN, panel_x + PANEL_W - CLOSED), work[2] - CLOSED - MARGIN)
                    y = min(max(work[1] + MARGIN, panel_y + PANEL_H - CLOSED), work[3] - CLOSED - MARGIN)
                    self._place(x, y, CLOSED, CLOSED)
                self._paint()
                return
            image = sequence[index]
            self._photo = ImageTk.PhotoImage(image)
            self.label.configure(image=self._photo, width=PANEL_W, height=PANEL_H)
            self.root.after(self.MORPH_MS, lambda: show(index + 1))

        show(0)

    # --- interaction ---------------------------------------------------
    def _bind(self) -> None:
        self.label.bind("<ButtonPress-1>", self._on_press)
        self.label.bind("<B1-Motion>", self._on_motion)
        self.label.bind("<ButtonRelease-1>", self._on_release)
        self.label.bind("<Motion>", self._on_hover)
        self.label.bind("<Leave>", self._on_leave)
        self.label.bind("<MouseWheel>", lambda _event: None)
        self.root.bind("<Escape>", lambda _event: self.collapse())
        self.root.bind("<FocusOut>", self._on_focus_out)

    def _on_focus_out(self, _event: "tk.Event") -> None:
        if self.expanded:
            self.collapse()

    def _on_press(self, event: "tk.Event") -> None:
        _debug("press %s,%s" % (event.x, event.y))
        self._drag_origin = (event.x_root, event.y_root)
        self._drag_moved = False
        self._window_origin = self._position()

    def _on_motion(self, event: "tk.Event") -> None:
        if self._drag_origin is None:
            return
        dx = event.x_root - self._drag_origin[0]
        dy = event.y_root - self._drag_origin[1]
        if not self._drag_moved and abs(dx) <= 3 and abs(dy) <= 3:
            return
        self._drag_moved = True
        width, height = (PANEL_W, PANEL_H) if self.expanded else (CLOSED, CLOSED)
        self.root.geometry("%dx%d+%d+%d" % (width, height, self._window_origin[0] + dx, self._window_origin[1] + dy))

    def _on_release(self, event: "tk.Event") -> None:
        _debug("release %s,%s moved=%s" % (event.x, event.y, self._drag_moved))
        if self._drag_origin is None:
            return
        moved = self._drag_moved
        self._drag_origin = None
        if moved:
            work = self._work_area()
            width, height = (PANEL_W, PANEL_H) if self.expanded else (CLOSED, CLOSED)
            if self.expanded:
                x = min(max(work[0] + MARGIN, self.root.winfo_x()), work[2] - width - MARGIN)
                y = min(max(work[1] + MARGIN, self.root.winfo_y()), work[3] - height - MARGIN)
            else:
                x, y = snap_position(self.root.winfo_x(), self.root.winfo_y(), width, height, work, MARGIN)
                gaps = {"left": x - work[0], "right": work[2] - (x + width),
                        "top": y - work[1], "bottom": work[3] - (y + height)}
                self.edge = min(gaps, key=gaps.get)
            self._place(x, y, width, height)
            self._save_state()
            return
        point = (event.x, event.y)
        for (x1, y1, x2, y2), command in self.hit_zones:
            if x1 <= point[0] <= x2 and y1 <= point[1] <= y2:
                command()
                return
        if not self.expanded:
            self.expand()

    # --- three-state machine (sliver <-> ball <-> panel) ----------------
    def _sliver_size(self) -> tuple[int, int]:
        return (10, 64) if self.edge in ("left", "right") else (64, 10)

    def _cursor_near_edge(self) -> bool:
        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
        point = POINT()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            return False
        work = self._work_area()
        if self.edge == "right":
            return point.x >= work[2] - 48
        if self.edge == "left":
            return point.x <= work[0] + 48
        if self.edge == "top":
            return point.y <= work[1] + 48
        return point.y >= work[3] - 48

    def _to_sliver(self) -> None:
        if self._animating:
            return
        if self.expanded or self.mode == "sliver":
            return
        self.mode = "sliver"
        self._paint()
        x, y = self._position()
        work = self._work_area()
        tx, ty, width, height = sliver_box(self.edge, (x, y), work)
        self._transition((x, y), (tx, ty), (CLOSED, CLOSED), (width, height))

    def _to_ball(self) -> None:
        if self._animating or self.mode != "sliver" or self.expanded:
            return
        self.mode = "ball"
        self._paint()
        x, y = self._position()
        work = self._work_area()
        tx, ty = ball_box(self.edge, (x, y), work)
        self._transition((x, y), (tx, ty), self._sliver_size(), (CLOSED, CLOSED))

    def _poll_cursor(self) -> None:
        try:
            if self.root is None:
                return
            near = self._cursor_near_edge()
            if near:
                self._idle_since = None
                if self.mode == "sliver" and not self._animating:
                    self._to_ball()
            elif self.mode == "ball" and not self.expanded and not self._animating:
                if self._idle_since is None:
                    self._idle_since = time.monotonic()
                elif time.monotonic() - self._idle_since > 1.2:
                    self._idle_since = None
                    self._to_sliver()
        except Exception:
            pass
        try:
            self.root.after(120, self._poll_cursor)
        except tk.TclError:
            pass

    # --- data -----------------------------------------------------------
    def _request(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None
        headers = {"X-Token": self.token}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers,
                                         method="POST" if data else "GET")
        with urllib.request.urlopen(request, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))

    def refresh(self) -> None:
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self) -> None:
        try:
            payload = self._request("/api/tasks?token=" + urllib.parse.quote(self.token, safe=""))
            # 浮球只显示「置顶」的事项：球是提醒，不是第二个待办中心。
            pinned = [item for item in (payload.get("pinned") or [])
                      if isinstance(item, dict) and not item.get("done")
                      and item.get("status") not in ("done", "dismissed")]
            self.rows = pinned
            self.candidates = 0
            self.message = ""
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.message = "读取失败：%s" % exc
        try:
            self.root.after(0, self._after_refresh)
        except (RuntimeError, tk.TclError):
            pass

    def _after_refresh(self) -> None:
        self._frames = []
        self._paint()

    def act(self, task: dict[str, Any], action: str) -> None:
        task_id = task.get("id")
        if not task_id:
            return

        def worker() -> None:
            try:
                self._request("/api/tasks/%s" % urllib.parse.quote(str(task_id), safe=""), {"action": action})
            except (urllib.error.URLError, OSError, ValueError):
                pass
            self._refresh_worker()

        threading.Thread(target=worker, daemon=True).start()

    # --- lifecycle -------------------------------------------------------
    def show(self) -> None:
        self._call(self._do_show)

    def _do_show(self) -> None:
        if self.root is None:
            return
        work = self._work_area()
        x, y = snap_position(work[2] - CLOSED - MARGIN, work[3] - CLOSED - DOCK_CLEARANCE, CLOSED, CLOSED, work, MARGIN)
        self.root.deiconify()
        self._apply_toolwindow()
        self._calibrate()
        self._place(x, y, CLOSED, CLOSED)
        self._paint()
        self._keep_topmost()
        self._poll()
        self._poll_cursor()
        self._watch_commands()
        self.visible = True

    def hide(self) -> None:
        self._call(self._do_hide)

    def _do_hide(self) -> None:
        if self.root is None:
            return
        self._save_state()
        self.root.withdraw()
        self.visible = False

    def _watch_commands(self) -> None:
        """ponytail: 仅验收用（自动化点击进不到窗口）；文件不存在时零成本。"""
        path = Path("F:/qq-notice-hub/_qa/floatball-cmd.txt")
        try:
            if path.exists():
                command = path.read_text(encoding="utf-8").strip().lower()
                path.unlink()
                if command == "expand":
                    if self.mode == "sliver":
                        self._to_ball()
                        self.root.after(260, self.expand)
                    else:
                        self.expand()
                elif command == "collapse":
                    self.collapse()
                elif command == "refresh":
                    self.refresh()
        except OSError:
            pass
        if self.root is not None:
            try:
                self.root.after(300, self._watch_commands)
            except tk.TclError:
                pass

    def _poll(self) -> None:
        self.refresh()
        self.root.after(POLL_SECONDS * 1000, self._poll)

    def _keep_topmost(self) -> None:
        if self._animating:
            self.root.after(400, self._keep_topmost)
            return
        try:
            hwnd = ctypes.windll.user32.GetParent(self._hwnd()) or self._hwnd()
            ctypes.windll.user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
        except (AttributeError, OSError):
            pass
        self.root.after(1500, self._keep_topmost)

    def stop(self) -> None:
        self._call(self._do_stop)

    def _do_stop(self) -> None:
        self._save_state()
        try:
            (self.data_dir / "floatball.pid").unlink()
        except OSError:
            pass
        try:
            if self.root is not None:
                self.root.quit()
                self.root.destroy()
        except tk.TclError:
            pass
        self.root = None
