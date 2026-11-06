"""浮球宿主：窗口层负责逐像素贴图，这里管状态、动画和面板数据。"""

from __future__ import annotations

import ctypes
import datetime as dt
import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

STATE_NAME = "floatball-state.json"
PID_NAME = "floatball.pid"
CLOSED_SIZE = (72, 72)
OPEN_SIZE = (380, 600)
PANEL_FALLBACK_SIZE = (340, 460)
TITLE_OPEN = "nh-floatball:open"
TITLE_CLOSED = "nh-floatball:closed"
TITLE_SIZES = {TITLE_OPEN: OPEN_SIZE, TITLE_CLOSED: CLOSED_SIZE}
TITLE_PATTERN = re.compile(r"^nh-floatball:(open|closed)(?::(\d+)x(\d+))?$")
WINDOW_CLASS = "Chrome_WidgetWin_1"

GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x8
HWND_TOPMOST = -1
SWP_NOSIZE = 0x1
SWP_NOMOVE = 0x2
SWP_NOACTIVATE = 0x10
MONITOR_DEFAULTTONEAREST = 2

CORNER_GAP = 8
ANIMATION_STEPS = 12
ANIMATION_SECONDS = 0.18
COLLAPSE_SECONDS = 0.14
WATCH_INTERVAL = 0.2
# 收起态也要周期取数，否则启动那一次失败后角标永远停在错的状态。
REFRESH_INTERVAL = 30.0
API_TIMEOUT = 5.0
TOPMOST_RETRY_SECONDS = 30.0
# 渲染层拿不到时才用的兜底：一行 56 + 间距 8。
ROW_FALLBACK_STEP = 64


def ease_out_cubic(t: float) -> float:
    """展开/收起的缓动曲线；渲染层只认 0..1 的进度。"""
    t = min(1.0, max(0.0, float(t)))
    return 1.0 - (1.0 - t) ** 3


def format_deadline(value: Any, now: dt.datetime | None = None) -> str:
    """把接口给的 ISO 时间写成面板里那点宽度放得下的短句。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        stamp = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone().replace(tzinfo=None)
        current = now or dt.datetime.now()
        if stamp.date() == current.date():
            return "今天 %02d:%02d" % (stamp.hour, stamp.minute)
        if stamp < current:
            return "已过期"
        return "%02d-%02d %02d:%02d" % (stamp.month, stamp.day, stamp.hour, stamp.minute)
    except (TypeError, ValueError):
        return ""


def clamp_position(
    x: int, y: int, width: int, height: int, work: tuple[int, int, int, int]
) -> tuple[int, int]:
    left, top, right, bottom = work
    return (
        max(left, min(int(x), right - width)),
        max(top, min(int(y), bottom - height)),
    )


def snap_position(
    x: int,
    y: int,
    width: int,
    height: int,
    work: tuple[int, int, int, int],
    margin: int = 8,
) -> tuple[int, int]:
    x, y = clamp_position(x, y, width, height, work)
    left, top, right, bottom = work
    candidates = [
        (abs(x - left), (left + margin, y)),
        (abs(right - width - x), (right - width - margin, y)),
        (abs(y - top), (x, top + margin)),
        (abs(bottom - height - y), (x, bottom - height - margin)),
    ]
    return min(candidates, key=lambda item: item[0])[1]


def process_matches_commandline(command_line: str, profile: Path) -> bool:
    """旧 Edge 宿主留下的判断：命令行里是否带着这个 profile。"""
    text = str(command_line or "").lower()
    return str(profile.resolve()).lower() in text and "msedge" in text


def parse_title(title: str) -> tuple[str, int, int] | None:
    match = TITLE_PATTERN.match(title or "")
    if not match:
        return None
    return (match.group(1), int(match.group(2) or 0), int(match.group(3) or 0))


def size_for_title(title: str) -> tuple[int, int] | None:
    if not str(title or "").startswith("nh-floatball:"):
        return None
    return TITLE_SIZES.get(str(title))


def region_radius(expanded: bool, height: int) -> int:
    return int(height) // 2 if not expanded else 16


_DPI_READY = False


def ensure_dpi_aware() -> None:
    """整个进程只做一次；浮球与主窗口必须按同一套物理像素坐标走。"""
    global _DPI_READY
    if _DPI_READY or os.name != "nt":
        return
    _DPI_READY = True
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def window_class(hwnd: int) -> str:
    if not hwnd:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(hwnd, buffer, 256)
        return buffer.value
    except (AttributeError, OSError):
        return ""


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


_U32_READY = False


def user32() -> Any:
    global _U32_READY
    handle = ctypes.windll.user32
    if _U32_READY:
        return handle
    _U32_READY = True
    handle.SetWindowPos.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_uint,
    ]
    handle.SetWindowPos.restype = ctypes.c_bool
    handle.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(RECT)]
    handle.GetWindowRect.restype = ctypes.c_bool
    handle.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    handle.GetWindowLongW.restype = ctypes.c_long
    handle.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    handle.ShowWindow.restype = ctypes.c_bool
    handle.GetForegroundWindow.argtypes = []
    handle.GetForegroundWindow.restype = ctypes.c_void_p
    handle.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    handle.MonitorFromWindow.restype = ctypes.c_void_p
    handle.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    handle.GetMonitorInfoW.restype = ctypes.c_bool
    handle.IsIconic.argtypes = [ctypes.c_void_p]
    handle.IsIconic.restype = ctypes.c_bool
    handle.IsZoomed.argtypes = [ctypes.c_void_p]
    handle.IsZoomed.restype = ctypes.c_bool
    handle.GetSystemMetrics.argtypes = [ctypes.c_int]
    handle.GetSystemMetrics.restype = ctypes.c_int
    return handle


def _work_area(hwnd: int) -> tuple[int, int, int, int]:
    """显示器工作区（去掉任务栏）；拿不到就退回整屏再让出 48px。"""

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_ulong),
            ("rcMonitor", RECT),
            ("rcWork", RECT),
            ("dwFlags", ctypes.c_ulong),
        ]

    handle = user32()
    try:
        monitor = handle.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if monitor and handle.GetMonitorInfoW(monitor, ctypes.byref(info)):
            work = info.rcWork
            return (work.left, work.top, work.right, work.bottom)
    except (AttributeError, OSError):
        pass
    try:
        width = handle.GetSystemMetrics(0)
        height = handle.GetSystemMetrics(1) - 48
    except (AttributeError, OSError):
        width, height = 1280, 720
    return (0, 0, width, height)


def write_state(path: Path, x: int, y: int, expanded: bool) -> None:
    """先写临时文件再 replace：断电或两个进程同时写都不会留半个 json。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"x": int(x), "y": int(y), "expanded": bool(expanded)}
    temporary = Path(str(path) + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


# 渲染层由前端维护；缺模块时用纯色兜底，宿主本身必须能 import。
# 兜底不许静默：打包后缺模块的表现是「什么都不发生」，日志里必须留下痕迹。
_LOG = logging.getLogger(__name__)
_warned: set[str] = set()


def _warn_once(key: str, message: str, error: BaseException) -> None:
    """同一处兜底只记一条，避免每帧刷屏。"""
    if key in _warned:
        return
    _warned.add(key)
    _LOG.error("%s：%s", message, error)


_RENDER_LOCK = threading.Lock()
_RENDER: dict[str, Any] = {}


def _render_module() -> Any:
    with _RENDER_LOCK:
        if "module" not in _RENDER:
            try:
                from . import floatball_render

                _RENDER["module"] = floatball_render
            except Exception:
                _RENDER["module"] = None
                _LOG.exception("浮球渲染模块导入失败")
        return _RENDER["module"]


def _window_class() -> Any:
    with _RENDER_LOCK:
        if "window" not in _RENDER:
            try:
                from .floatball_win import FloatBallWindow

                _RENDER["window"] = FloatBallWindow
            except Exception:
                _RENDER["window"] = None
                _LOG.exception("浮球窗口模块导入失败")
        return _RENDER["window"]


@dataclass(frozen=True)
class _FallbackRow:
    task_id: str
    title: str
    source: str
    when: str
    pinned: bool = False


@dataclass(frozen=True)
class _FallbackPanel:
    rows: tuple[Any, ...] = ()
    status: str = ""
    status_kind: str = ""
    busy: bool = False
    expanded: bool = True
    scroll: int = 0


class FloatBallHost:
    """把渲染帧贴到屏幕，并处理点击、拖动与面板数据。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.state_path = self.data_dir / STATE_NAME
        self.url = ""
        self.hwnd: int | None = None
        self.visible = False
        self.expanded = False
        self.width, self.height = CLOSED_SIZE
        self.viewport: tuple[int, int] | None = None
        self._win: Any = None
        self._stop_event = threading.Event()
        self._watcher: threading.Thread | None = None
        self._manual_hidden = False
        self._anchor: tuple[int, int] | None = None
        self._frame_size: tuple[int, int] = CLOSED_SIZE
        self._topmost_warned = 0.0
        self._topmost_ok_logged = False
        self._token = ""
        self._api_base = ""
        self._rows: list[Any] = []
        self._status = ""
        self._status_kind = ""
        self._load_failed = False
        self._next_refresh = 0.0
        self._scroll = 0
        self._panel: Any = None
        self._anim_lock = threading.Lock()
        self._animating = False
        self._starting = threading.Lock()
        self._dpi_scale = None  # 旧宿主的钩子，自绘窗口按物理像素走，不再缩放

    # ---- 依赖注入点（测试替换这三个，不建真窗口）----
    def _render_module(self) -> Any:
        return _render_module()

    def _window_class(self) -> Any:
        return _window_class()

    def _ball_frame(self, badge: int) -> Any:
        module = self._render_module()
        image = None
        if module is not None:
            try:
                image = module.render_ball(badge)
            except Exception as exc:  # 渲染出错不能把整个浮球带走
                self._log("画浮球失败：%s" % exc)
        if image is None:
            image = self._fallback_frame(CLOSED_SIZE, badge)
        return self._mark_failed(image) if self._load_failed else image

    def _mark_failed(self, image: Any) -> Any:
        """取数失败时角标画「!」：画成 0 条就跟真的没有待办分不出来了。"""
        try:
            from PIL import ImageDraw

            module = self._render_module()
            size = int(getattr(module, "BALL_SIZE", CLOSED_SIZE[0]) or CLOSED_SIZE[0])
            inner = getattr(module, "BADGE_BG", (196, 60, 44))
            ring = getattr(module, "BADGE_RING", (255, 255, 255))
            marked = image.copy()
            draw = ImageDraw.Draw(marked)
        except Exception as exc:
            self._log("画错误角标失败：%s" % exc)
            return image
        bx, by, br = size - 20, 20, 9
        draw.ellipse((bx - br - 2, by - br - 2, bx + br + 2, by + br + 2), fill=ring)
        draw.ellipse((bx - br, by - br, bx + br, by + br), fill=inner)
        # 「!」手画：竖条 + 点，不依赖字体里有没有这个字形。
        draw.rectangle((bx - 1, by - 6, bx + 1, by + 1), fill=ring)
        draw.rectangle((bx - 1, by + 3, bx + 1, by + 5), fill=ring)
        return marked

    def _panel_frame(self, view: Any) -> Any:
        module = self._render_module()
        image = None
        if module is not None:
            try:
                image = module.render_panel(view)
            except Exception as exc:
                self._log("画面板失败：%s" % exc)
        if image is None:
            image = self._fallback_frame(self._panel_frame_size(), 0)
        return self._mark_panel_failed(image) if self._load_failed else image

    def _mark_panel_failed(self, image: Any) -> Any:
        """展开态标题区写「待办加载失败」：否则失败被画成「待处理 · 0 条」，看着像真没待办。"""
        marked = image
        try:
            from PIL import ImageDraw

            module = self._render_module()
            text = getattr(module, "_text", None)
            pad = self._shadow()
            bg = getattr(module, "PANEL_BG", (250, 247, 239))
            danger = getattr(module, "DANGER", (154, 63, 50))
            left = pad + 14
            marked = image.copy()
            draw = ImageDraw.Draw(marked)
            # 只盖标题那半截，右边留给刷新/关闭两个图标。
            draw.rectangle((left, pad + 4, pad + 150, pad + 28), fill=bg)
            if callable(text):
                text(draw, (left, pad + 6), "待办加载失败", 15, danger, True)
            if not self._rows:
                # 空列表那句「现在没有要处理的事」同样是错的：它不是没有，是没读到。
                draw.rectangle((left, pad + 152, image.size[0] - 24, pad + 208), fill=bg)
                if callable(text):
                    text(draw, (left, pad + 158), "网络恢复后会自动重试。", 13, danger)
        except Exception as exc:
            self._log("画面板错误提示失败：%s" % exc)
            return image
        return marked

    def _transition_frame(self, progress: float, view: Any) -> Any:
        module = self._render_module()
        if module is not None:
            try:
                return module.render_transition(progress, view)
            except Exception as exc:
                self._log("画过渡帧失败：%s" % exc)
        if progress >= 0.5:
            return self._panel_frame(view)
        return self._ball_frame(len(self._rows))

    def _shadow(self) -> int:
        module = self._render_module()
        return int(getattr(module, "SHADOW_SPREAD", 10) or 10) if module else 10

    def _panel_frame_size(self) -> tuple[int, int]:
        module = self._render_module()
        try:
            width, height = module.PANEL_SIZE
            pad = int(module.SHADOW_SPREAD)
            return (int(width) + pad * 2, int(height) + pad * 2)
        except Exception:
            return PANEL_FALLBACK_SIZE

    def _fallback_frame(self, size: tuple[int, int], badge: int) -> Any:
        try:
            from PIL import Image, ImageDraw
        except Exception as exc:  # 没装 Pillow 时给一句能读懂的话
            raise RuntimeError("缺少 Pillow，画不了浮球：%s" % exc) from exc
        image = Image.new("RGBA", (int(size[0]), int(size[1])), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        if size == CLOSED_SIZE:
            draw.ellipse((2, 2, size[0] - 3, size[1] - 3), fill=(92, 107, 68, 255))
            if badge:
                text = "99+" if badge > 99 else str(badge)
                draw.text((size[0] - 24, 3), text, fill=(255, 255, 255, 255))
        else:
            draw.rectangle((0, 0, size[0] - 1, size[1] - 1), fill=(250, 247, 239, 255))
            draw.text((14, 14), "待处理 %d 条" % badge, fill=(58, 58, 40, 255))
        return image

    # ---- 状态文件 ----
    def _load_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_state(self) -> None:
        if self._win is None:
            return
        try:
            position = self._win.position()
        except Exception as exc:
            self._log("读浮球位置失败：%s" % exc)
            return
        try:
            write_state(self.state_path, position[0], position[1], self.expanded)
        except OSError as exc:
            self._log("保存浮球位置失败：%s" % exc)

    def _log(self, message: str) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with open(self.data_dir / "floatball.log", "a", encoding="utf-8") as handle:
                handle.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
        except OSError:
            pass

    # ---- 置顶 / 全屏 ----
    def _ensure_topmost(self, hwnd: int) -> None:
        handle = user32()
        exstyle = handle.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if exstyle & WS_EX_TOPMOST:
            return
        result = handle.SetWindowPos(
            hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
        )
        if result:
            if not self._topmost_ok_logged:
                self._topmost_ok_logged = True
                self._log("浮球已置顶 hwnd=%s" % hwnd)
            return
        now = time.monotonic()
        if now - self._topmost_warned < TOPMOST_RETRY_SECONDS:
            return
        self._topmost_warned = now
        self._log(
            "置顶失败 hwnd=%s err=%s exstyle=0x%08x"
            % (hwnd, ctypes.get_last_error(), exstyle & 0xFFFFFFFF)
        )

    def _foreground_fullscreen(self) -> bool:
        handle = user32()
        foreground = handle.GetForegroundWindow()
        if not foreground or foreground == self.hwnd:
            return False
        if handle.IsIconic(foreground):
            return False
        # 最大化窗口的矩形比显示器大一圈，不能当全屏，否则用户一最大化球就消失
        if handle.IsZoomed(foreground):
            return False
        rect = RECT()
        if not handle.GetWindowRect(foreground, ctypes.byref(rect)):
            return False
        monitor = handle.MonitorFromWindow(foreground, MONITOR_DEFAULTTONEAREST)

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_ulong),
                ("rcMonitor", RECT),
                ("rcWork", RECT),
                ("dwFlags", ctypes.c_ulong),
            ]

        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not monitor or not handle.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return False
        screen = info.rcMonitor
        return (
            abs(rect.left - screen.left) <= 2
            and abs(rect.top - screen.top) <= 2
            and abs(rect.right - screen.right) <= 2
            and abs(rect.bottom - screen.bottom) <= 2
        )

    # ---- 生命周期 ----
    def start(self, url: str) -> None:
        if os.name != "nt":
            raise RuntimeError("浮球宿主仅支持 Windows")
        # launcher 两处（托盘切换与设置同步）可能同时调进来：不掐住就会建出两个窗口，
        # 旧 watcher 也会被 clear() 复活。
        if not self._starting.acquire(blocking=False):
            return
        try:
            self._start_locked(url)
        finally:
            self._starting.release()

    def _start_locked(self, url: str) -> None:
        if self._win is not None:
            return
        window_class_ = self._window_class()
        if window_class_ is None:
            raise RuntimeError("缺少浮球窗口模块 noticehub/floatball_win.py，浮球起不来")
        self.url = url
        self._token, self._api_base = _parse_url(url)
        ensure_dpi_aware()
        window = window_class_(self._on_click, self._on_drag_end, on_wheel=self._on_wheel)
        self._win = window
        self.hwnd = getattr(window, "hwnd", None)
        if not self.hwnd:
            # 窗口是懒建的：先让它把消息线程起来（此时 hwnd 还是 0，不会显示），再取句柄
            window.show(0, 0)
            self.hwnd = getattr(window, "hwnd", None)
        if not self.hwnd:
            self._win = None
            raise RuntimeError("浮球窗口创建失败（没有拿到窗口句柄）")
        state = self._load_state()
        expanded = bool(state.get("expanded"))
        size = self._panel_frame_size() if expanded else CLOSED_SIZE
        self._frame_size = size
        work = _work_area(self.hwnd)
        x, y = state.get("x"), state.get("y")
        if not isinstance(x, int) or not isinstance(y, int):
            x, y = work[2] - size[0] - CORNER_GAP, work[3] - size[1] - CORNER_GAP
        x, y = clamp_position(x, y, size[0], size[1], work)
        self.expanded = expanded
        if expanded:
            self._refresh_view()
            window.set_frame(self._panel_frame(self._panel), anchor="br")
        else:
            window.set_frame(self._ball_frame(0), anchor="br")
        window.show(x, y)
        self._anchor = (x + size[0], y + size[1])
        self.visible = True
        self._manual_hidden = False
        self._stop_event.clear()
        self._watcher = threading.Thread(target=self._watch, daemon=True, name="floatball-watch")
        self._watcher.start()
        self._refresh_async()

    def stop(self) -> None:
        self._log("stop() 主动关闭浮球")
        self._stop_event.set()
        watcher, self._watcher = self._watcher, None
        if watcher is not None and watcher is not threading.current_thread():
            # 不 join 的话旧 watcher 还在跑一轮，随后 start() 的 clear() 让它复活。
            watcher.join(timeout=2)
        self._save_state()
        window = self._win
        self._win = None
        self.visible = False
        self._manual_hidden = False
        self.hwnd = None
        if window is not None:
            try:
                window.close()
            except Exception as exc:
                self._log("关闭浮球窗口失败：%s" % exc)
        try:
            (self.data_dir / PID_NAME).unlink()
        except OSError:
            pass

    def _watch(self) -> None:
        self._next_refresh = time.monotonic() + REFRESH_INTERVAL
        while not self._stop_event.wait(WATCH_INTERVAL):
            if self._win is None:
                break
            if self.visible and self.hwnd:
                self._ensure_topmost(self.hwnd)
            if self._foreground_fullscreen():
                if self.visible:
                    self.hide(manual=False)
            elif self._should_auto_show():
                self.show()
            # 收起态也要刷：角标停在启动那一眼，网络恢复后也永远不会自己回来。
            if time.monotonic() >= self._next_refresh:
                self._next_refresh = time.monotonic() + REFRESH_INTERVAL
                self._refresh_async()

    def _should_auto_show(self) -> bool:
        return bool(self.hwnd) and not self.visible and not self._manual_hidden

    def show(self) -> None:
        if self._win is None:
            return
        self._manual_hidden = False
        try:
            self._win.show(*self._anchor_position())
        except Exception as exc:
            self._log("显示浮球失败：%s" % exc)
            return
        self.visible = True

    def hide(self, manual: bool = True) -> None:
        if manual:
            self._manual_hidden = True
        if self._win is None:
            return
        try:
            self._win.hide()
        except Exception as exc:
            self._log("隐藏浮球失败：%s" % exc)
            return
        self.visible = False

    def _anchor_position(self) -> tuple[int, int]:
        width, height = self._frame_size
        if self._anchor is not None:
            return (self._anchor[0] - width, self._anchor[1] - height)
        work = _work_area(self.hwnd or 0)
        return clamp_position(
            work[2] - width - CORNER_GAP,
            work[3] - height - CORNER_GAP,
            width,
            height,
            work,
        )

    # ---- 鼠标 ----
    def _on_wheel(self, notches: int) -> None:
        """滚轮一档一行；到顶/到底就不动，值没变不重画。

        notches 正数=向上（同 WM_MOUSEWHEEL）；_scroll 是内容往上推的偏移，
        所以向上滚要减。
        """
        if not self.expanded or not notches:
            return
        limit = self._max_scroll()
        value = max(0, min(self._scroll - int(notches) * self._scroll_step(), limit))
        if value == self._scroll:
            return
        self._scroll = value
        self._paint_current()

    def _scroll_step(self) -> int:
        """一档滚多少：正好一行（行高 + 行间距），取自渲染层的几何常量。"""
        module = self._render_module()
        try:
            return int(module.ROW_H) + int(module.ROW_GAP)
        except Exception as exc:
            _warn_once("_scroll_step", "浮球滚动步长取不到，回退 %d" % ROW_FALLBACK_STEP, exc)
            return ROW_FALLBACK_STEP

    def _max_scroll(self) -> int:
        module = self._render_module()
        try:
            return int(module.list_metrics(len(self._rows))["max_scroll"])
        except Exception as exc:
            _warn_once("_max_scroll", "浮球滚动上限取不到，回退 0", exc)
            return 0

    def _on_click(self, x: int, y: int) -> None:
        if not self.expanded:
            self.expand()
            return
        action = self._hit(x, y)
        if action is None:
            return
        name, task_id = action
        if name == "close":
            self.collapse()
            return
        if name in {"done", "snooze", "pin"} and task_id:
            self._act(task_id, name)

    def _on_drag_end(self, x: int, y: int) -> None:
        width, height = self._frame_size
        position = self._win.position() if self._win is not None else (x - width, y - height)
        work = _work_area(self.hwnd or 0)
        left, top = clamp_position(position[0], position[1], width, height, work)
        self._anchor = (left + width, top + height)
        if self._win is not None:
            try:
                self._win.show(left, top)
            except Exception as exc:
                self._log("拖动后定位失败：%s" % exc)
        self._save_state()

    def _hit(self, x: int, y: int) -> tuple[str, str] | None:
        module = self._render_module()
        view = self._panel or self._panel_view()
        pad = self._shadow()
        if module is not None:
            try:
                return module.hit_test(x - pad, y - pad, view)
            except Exception as exc:
                self._log("命中判断失败：%s" % exc)
                return None
        return self._fallback_hit(x - pad, y - pad, view)

    def _fallback_hit(self, x: int, y: int, view: Any) -> tuple[str, str] | None:
        if 290 <= x < 312 and 8 <= y < 30:
            return ("close", "")
        for index, row in enumerate(getattr(view, "rows", ())):
            top = 56 + index * 64
            if top <= y < top + 56:
                return ("row", row.task_id)
        return None

    # ---- 展开 / 收起 ----
    def expand(self) -> None:
        if self.expanded:
            return
        self._animate(True)
        self._refresh_async()

    def collapse(self) -> None:
        if not self.expanded:
            return
        self._animate(False)

    def _animate(self, expand: bool) -> None:
        with self._anim_lock:
            if self._animating:
                return
            self._animating = True
        thread = threading.Thread(
            target=self._run_animation, args=(expand,), daemon=True, name="nh-floatball-anim"
        )
        thread.start()

    def _run_animation(self, expand: bool) -> None:
        try:
            if self._win is None:
                return
            if not expand:
                # 收起后下次展开从头开始；滚动位置不是需要记住的状态。
                self._scroll = 0
            self._refresh_view()
            seconds = ANIMATION_SECONDS if expand else COLLAPSE_SECONDS
            for index in range(1, ANIMATION_STEPS + 1):
                eased = ease_out_cubic(index / ANIMATION_STEPS)
                progress = eased if expand else 1.0 - eased
                self._win.set_frame(self._transition_frame(progress, self._panel), anchor="br")
                time.sleep(seconds / ANIMATION_STEPS)
            self.expanded = expand
        except Exception as exc:
            self._log("浮球动画失败：%s" % exc)
        finally:
            with self._anim_lock:
                self._animating = False
        self._paint_current()

    def _paint_current(self) -> None:
        window = self._win
        if window is None:
            return
        with self._anim_lock:
            if self._animating:  # 动画自己的最后一帧就是终态，别插队
                return
        self._refresh_view()
        image = (
            self._panel_frame(self._panel)
            if self.expanded
            else self._ball_frame(len(self._rows))
        )
        self._frame_size = tuple(image.size)
        try:
            window.set_frame(image, anchor="br")
        except Exception as exc:
            # stop() 会把 _win 清掉；后台取数线程正好走到这里时不能把异常扔出线程。
            self._log("贴浮球帧失败：%s" % exc)

    # ---- 面板数据 ----
    def _refresh_async(self) -> None:
        threading.Thread(
            target=self._refresh_panel, daemon=True, name="nh-floatball-data"
        ).start()

    def _refresh_panel(self) -> None:
        try:
            payload = self._api_get("/api/tasks")
        except Exception as exc:
            self._status = "读取待办失败：%s" % exc
            self._status_kind = "error"
            self._load_failed = True
            self._paint_current()
            return
        try:
            rows = self._rows_from_board(payload)
        except Exception as exc:
            self._status = "待办数据看不懂：%s" % exc
            self._status_kind = "error"
            self._load_failed = True
            self._paint_current()
            return
        self._rows = rows
        self._load_failed = False
        self._status = ""
        self._status_kind = ""
        self._paint_current()

    def _rows_from_board(self, payload: dict[str, Any]) -> list[Any]:
        _, TaskRow = self._view_classes()
        rows: list[Any] = []
        for bucket in ("candidates", "today", "week", "later"):
            for item in payload.get(bucket) or ():
                if not isinstance(item, dict):
                    continue
                rows.append(self._row(TaskRow, item))
        return rows[:24]

    def _row(self, TaskRow: Any, item: dict[str, Any]) -> Any:
        # action 是提炼过的短句；summary 是原文 30~60 字，直接放会把按钮压住。
        title = str(item.get("action") or "").strip()
        if not title:
            title = str(item.get("summary") or item.get("title") or "").strip()
        if not title:
            title = "（无标题）"
        groups = item.get("groups") or ()
        source = ""
        if isinstance(groups, (list, tuple)) and groups:
            source = str(groups[0] or "").strip()
        when = format_deadline(item.get("deadline"))
        if not when:
            when = str(item.get("deadline_text") or item.get("snooze_text") or "").strip()
        return TaskRow(
            task_id=str(item.get("id") or ""),
            title=title,
            source=source,
            when=when,
            pinned=bool(item.get("pinned")),
        )

    def _view_classes(self) -> tuple[Any, Any]:
        module = self._render_module()
        if module is not None:
            panel_view = getattr(module, "PanelView", None)
            task_row = getattr(module, "TaskRow", None)
            if panel_view is not None and task_row is not None:
                return panel_view, task_row
        return _FallbackPanel, _FallbackRow

    def _panel_view(self) -> Any:
        PanelView, _ = self._view_classes()
        return PanelView(
            rows=tuple(self._rows),
            status=self._status,
            status_kind=self._status_kind,
            busy=False,
            expanded=self.expanded,
            scroll=self._scroll,
        )

    def _refresh_view(self) -> None:
        self._panel = self._panel_view()

    def _act(self, task_id: str, name: str) -> None:
        threading.Thread(
            target=self._run_action, args=(task_id, name), daemon=True,
            name="nh-floatball-action",
        ).start()

    def _run_action(self, task_id: str, name: str) -> None:
        try:
            if name == "pin":
                pinned = self._is_pinned(task_id)
                self._api_post("/api/tasks/pin", {"task_id": task_id, "pinned": not pinned})
            else:
                self._api_post("/api/tasks/%s" % urllib.parse.quote(task_id, safe=""), {"action": name})
        except Exception as exc:
            self._status = "操作没成功：%s" % exc
            self._status_kind = "error"
            self._paint_current()
            return
        self._refresh_panel()

    def _is_pinned(self, task_id: str) -> bool:
        for row in self._rows:
            if getattr(row, "task_id", None) == task_id:
                return bool(getattr(row, "pinned", False))
        return False

    # ---- HTTP ----
    def _api_get(self, path: str) -> dict[str, Any]:
        request = urllib.request.Request(self._api_base + path, method="GET")
        if self._token:
            request.add_header("X-Token", self._token)
        return self._api_call(request)

    def _api_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._api_base + path, data=body, method="POST"
        )
        request.add_header("Content-Type", "application/json; charset=utf-8")
        if self._token:
            request.add_header("X-Token", self._token)
        return self._api_call(request)

    def _api_call(self, request: urllib.request.Request) -> dict[str, Any]:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT) as response:
            raw = response.read()
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("接口返回的不是对象")
        return payload


def _parse_url(url: str) -> tuple[str, str]:
    parts = urllib.parse.urlsplit(url or "")
    base = "%s://%s" % (parts.scheme or "http", parts.netloc or "127.0.0.1")
    token = (urllib.parse.parse_qs(parts.query).get("token") or [""])[0]
    return (token, base)


if os.name == "nt":
    user32()
