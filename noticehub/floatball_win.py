"""桌面浮球：Python 自绘的逐像素透明窗口（ctypes + Pillow 图像数据）。

浏览器窗口做不出逐像素透明（透明处会被画成黑块），所以浮球改由本进程直接开一个
分层窗口（WS_EX_LAYERED）用 UpdateLayeredWindow 贴 32bpp 带 alpha 的位图。

几条硬约束：
- 不抢焦点：只用 SW_SHOWNA + WM_MOUSEACTIVATE 回 MA_NOACTIVATE，绝不激活窗口。
- 命中穿透：WM_NCHITTEST 按当前帧的 alpha 判，透明像素返回 HTTRANSPARENT。
- 跨线程安全：窗口与消息循环在自建 daemon 线程里；set_frame 只入队 + 唤醒。
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from ctypes import wintypes
from typing import Callable

from .floatball_host import ensure_dpi_aware

__all__ = ["FloatBallWindow", "alpha_hit", "anchor_xy"]

log = logging.getLogger(__name__)

LRESULT = ctypes.c_ssize_t

# 消息与常量
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_MOUSEACTIVATE = 0x0021
WM_NCHITTEST = 0x0084
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_MOUSEWHEEL = 0x020A
WM_QUIT = 0x0012

WHEEL_DELTA = 120  # 滚轮一档的标准增量

HTTRANSPARENT = -1
HTCLIENT = 1
MA_NOACTIVATE = 3

SW_HIDE = 0
SW_SHOWNA = 8
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
ULW_ALPHA = 2
AC_SRC_OVER = 0
AC_SRC_ALPHA = 1
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_POPUP = 0x80000000
PM_REMOVE = 0x0001
ERROR_CLASS_ALREADY_EXISTS = 1410

DRAG_THRESHOLD = 4  # 移动超过这么多像素才算拖动，否则算点击

_WIN = os.name == "nt"

if _WIN:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    WNDPROC = ctypes.WINFUNCTYPE(
        LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )

    class BLENDFUNCTION(ctypes.Structure):
        _fields_ = [
            ("BlendOp", wintypes.BYTE),
            ("BlendFlags", wintypes.BYTE),
            ("SourceConstantAlpha", wintypes.BYTE),
            ("AlphaFormat", wintypes.BYTE),
        ]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    # 这些 argtypes/restype 一个都不能漏：漏了 ctypes 会把指针当 int 传，窗口静默失败。
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.UpdateLayeredWindow.argtypes = [
        wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
        ctypes.POINTER(wintypes.SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT),
        wintypes.DWORD, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD,
    ]
    user32.UpdateLayeredWindow.restype = wintypes.BOOL
    user32.PeekMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT,
    ]
    user32.PeekMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = LRESULT
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.SetCapture.argtypes = [wintypes.HWND]
    user32.ReleaseCapture.argtypes = []
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = wintypes.LONG
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
    user32.SetWindowLongW.restype = wintypes.LONG

    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.CreateDIBSection.argtypes = [
        wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
    ]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]

    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
else:  # 非 Windows 只保证可导入：纯几何/命中测试仍可跑
    user32 = gdi32 = kernel32 = None
    WNDPROC = None
    BLENDFUNCTION = BITMAPINFOHEADER = BITMAPINFO = WNDCLASSW = None


# ---------------------------------------------------------------- 纯函数（可测）

def alpha_hit(alpha: bytes, width: int, height: int, x: int, y: int) -> bool:
    """窗口内 (x, y) 是否落在不透明像素上。alpha 是 RGBA 逐行字节。"""
    if width <= 0 or height <= 0:
        return False
    if x < 0 or y < 0 or x >= width or y >= height:
        return False
    offset = (y * width + x) * 4
    if offset + 3 >= len(alpha):
        return False
    return alpha[offset + 3] > 0


def anchor_xy(
    position: tuple[int, int],
    size: tuple[int, int],
    frame_size: tuple[int, int],
    anchor: str = "br",
) -> tuple[int, int]:
    """换帧后的左上角坐标。

    anchor="br"：右下角不动，帧变大就往左上长；"tl"：左上角不动。
    """
    x, y = int(position[0]), int(position[1])
    width, height = int(size[0]), int(size[1])
    frame_width, frame_height = int(frame_size[0]), int(frame_size[1])
    if anchor == "tl":
        return x, y
    if anchor == "br":
        return x + width - frame_width, y + height - frame_height
    raise ValueError("anchor 只支持 'br' 或 'tl'，收到 %r" % (anchor,))


def _unpack_lparam(value: int) -> tuple[int, int]:
    """WM_* 鼠标消息里 lParam 的低/高 16 位（有符号）。"""
    value = int(value)
    low = value & 0xFFFF
    high = (value >> 16) & 0xFFFF
    if low >= 0x8000:
        low -= 0x10000
    if high >= 0x8000:
        high -= 0x10000
    return low, high


def wheel_notches(wparam: int) -> int:
    """WM_MOUSEWHEEL 的 wParam 高 16 位是有符号的滚轮增量，这里换算成档数。

    一档是 WHEEL_DELTA(120)。触控板会发小于 120 的零头，此时按方向算一档，
    免得小幅滑动被整除成 0、滚不动。
    """
    delta = (int(wparam) >> 16) & 0xFFFF
    if delta >= 0x8000:
        delta -= 0x10000
    notches = int(delta / WHEEL_DELTA) if WHEEL_DELTA else delta
    if notches == 0 and delta:
        notches = 1 if delta > 0 else -1
    return notches


class FloatBallWindow:
    """自绘透明浮球窗口。窗口与消息循环都在自己的 daemon 线程里。"""

    _CLASS_NAME = "NHFloatBallWindow"
    _class_atom = 0
    _class_lock = threading.Lock()

    def __init__(
        self,
        on_click: Callable[[int, int], None],
        on_drag_end: Callable[[int, int], None],
        on_mouse_move: Callable[[int, int], None] | None = None,
        on_wheel: Callable[[int], None] | None = None,
    ) -> None:
        if not _WIN:
            raise RuntimeError("FloatBallWindow 只支持 Windows")
        ensure_dpi_aware()
        self._on_click = on_click
        self._on_drag_end = on_drag_end
        self._on_mouse_move = on_mouse_move
        self._on_wheel = on_wheel
        self._lock = threading.RLock()
        self._hwnd = 0
        self._visible = False
        self._position = (0, 0)
        self._frame_size = (0, 0)
        self._alpha = b""
        self._pending: tuple[bytes, tuple[int, int], tuple[int, int]] | None = None
        self._wake = threading.Event()
        self._ready = threading.Event()
        self._closing = False
        self._thread: threading.Thread | None = None
        self._wndproc = WNDPROC(self._window_proc)
        # GDI 资源
        self._hdc_screen = None
        self._hdc_mem = None
        self._hbmp = None
        self._dib_size = (0, 0)
        self._bits = None
        self._blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        # 拖动状态
        self._press_screen = (0, 0)
        self._drag_origin = (0, 0)
        self._dragging = False
        self._captured = False

    # -------------------------------------------------------------- 对外接口

    @property
    def visible(self) -> bool:
        return self._visible

    @property
    def hwnd(self) -> int:
        return self._hwnd

    def position(self) -> tuple[int, int]:
        with self._lock:
            return self._position

    def show(self, x: int | None = None, y: int | None = None) -> None:
        self._ensure_thread()
        with self._lock:
            if x is not None or y is not None:
                self._position = (
                    int(x) if x is not None else self._position[0],
                    int(y) if y is not None else self._position[1],
                )
            position = self._position
            hwnd = self._hwnd
            # 分层窗口的真实位置由 UpdateLayeredWindow 决定，光 SetWindowPos 不会挪。
            # 已经有帧就排一次重绘，让窗口线程按新坐标 UpdateLayeredWindow。
            if self._alpha:
                self._pending = (self._alpha, self._frame_size, position)
        if hwnd:
            user32.SetWindowPos(
                hwnd, None, position[0], position[1],
                self._frame_size[0] or 1, self._frame_size[1] or 1,
                SWP_NOZORDER | SWP_NOACTIVATE,
            )
            user32.ShowWindow(hwnd, SW_SHOWNA)  # 硬红线：不抢焦点
        self._wake.set()
        self._visible = True

    def hide(self) -> None:
        hwnd = self._hwnd
        self._visible = False
        if hwnd:
            user32.ShowWindow(hwnd, SW_HIDE)

    def set_frame(self, image, anchor: str = "br") -> None:
        """换一帧。尺寸即窗口尺寸；跨线程调用安全。"""
        frame = image.convert("RGBA") if image.mode != "RGBA" else image
        width, height = frame.size
        # 32bpp BI_RGB 的 DIB 字节序是 B,G,R,A；Pillow 原生按 BGRA 导出，
        # 直接 tobytes() 会把红蓝通道互换（实机探针贴纯红出来是纯蓝）。
        data = frame.tobytes("raw", "BGRA")
        with self._lock:
            old_size = self._frame_size
            position = anchor_xy(self._position, old_size, (width, height), anchor) if old_size[0] else self._position
            self._frame_size = (width, height)
            self._alpha = data
            self._pending = (data, (width, height), position)
            # 乐观更新：_paint 成功才回写 _position 的话，宿主在 16ms 内连推两帧时
            # 第二帧会拿旧基准算锚点，右下角逐帧平移。
            self._position = position
        self._wake.set()

    def close(self) -> None:
        """销毁窗口并停掉消息循环线程。"""
        self._closing = True
        self._visible = False
        hwnd = self._hwnd
        if hwnd:
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(3.0)

    # -------------------------------------------------------------- 线程与窗口

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="nh-floatball-win", daemon=True)
        self._thread.start()
        if not self._ready.wait(2.0):
            raise RuntimeError("浮球窗口没有在 2 秒内建好")
        if not self._hwnd:
            raise RuntimeError("浮球窗口创建失败")

    def _run(self) -> None:
        msg = wintypes.MSG()
        try:
            self._create_window()
            self._ready.set()
            while not self._closing:
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    if msg.message == WM_QUIT:
                        self._closing = True
                        break
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                pending = self._take_pending()
                if pending is not None:
                    self._paint(*pending)
                    continue
                self._wake.wait(0.004)
                self._wake.clear()
        finally:
            self._closing = True
            self._destroy_gdi()
            self._ready.set()

    def _take_pending(self):
        with self._lock:
            pending = self._pending
            self._pending = None
        return pending

    def _create_window(self) -> None:
        self._register_class()
        with self._lock:
            width, height = self._frame_size
            position = self._position
        width = max(1, width)
        height = max(1, height)
        hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST,
            self._CLASS_NAME, "nh-floatball", WS_POPUP,
            position[0], position[1], width, height,
            None, None, kernel32.GetModuleHandleW(None), None,
        )
        if not hwnd:
            raise RuntimeError("CreateWindowExW 失败，错误码 %s" % ctypes.get_last_error())
        self._hwnd = hwnd
        self._hdc_screen = user32.GetDC(None)
        self._hdc_mem = gdi32.CreateCompatibleDC(self._hdc_screen)
        self._create_dib(width, height)

    def _register_class(self) -> None:
        with FloatBallWindow._class_lock:
            if FloatBallWindow._class_atom:
                return
            wc = WNDCLASSW()
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = kernel32.GetModuleHandleW(None)
            wc.lpszClassName = self._CLASS_NAME
            atom = user32.RegisterClassW(ctypes.byref(wc))
            if not atom:
                error = ctypes.get_last_error()
                if error != ERROR_CLASS_ALREADY_EXISTS:
                    raise RuntimeError("RegisterClassW 失败，错误码 %s" % error)
            FloatBallWindow._class_atom = atom or 1

    def _create_dib(self, width: int, height: int) -> None:
        if self._hbmp:
            gdi32.DeleteObject(self._hbmp)
            self._hbmp = None
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height  # 负值 = 自上而下，和 PIL 的行序一致
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bits = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(
            self._hdc_screen, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0
        )
        if not hbmp:
            raise RuntimeError("CreateDIBSection 失败，错误码 %s" % ctypes.get_last_error())
        gdi32.SelectObject(self._hdc_mem, hbmp)
        self._hbmp = hbmp
        self._bits = bits
        self._dib_size = (width, height)

    def _paint(self, data: bytes, size: tuple[int, int], position: tuple[int, int]) -> None:
        hwnd = self._hwnd
        if not hwnd:
            return
        width, height = size
        resized = self._dib_size != (width, height)
        if resized:
            self._create_dib(width, height)
        ctypes.memmove(self._bits.value, data, width * height * 4)
        point_dst = wintypes.POINT(position[0], position[1])
        point_src = wintypes.POINT(0, 0)
        size_value = wintypes.SIZE(width, height)
        ok = user32.UpdateLayeredWindow(
            hwnd, self._hdc_screen, ctypes.byref(point_dst), ctypes.byref(size_value),
            self._hdc_mem, ctypes.byref(point_src), 0, ctypes.byref(self._blend), ULW_ALPHA,
        )
        if ok:
            if resized:
                # UpdateLayeredWindow 换了表面尺寸，但窗口矩形还留在旧尺寸上
                # （GetWindowRect / 命中测试 / 拖动都按窗口矩形算），这里对齐一次。
                user32.SetWindowPos(
                    hwnd, None, position[0], position[1], width, height,
                    SWP_NOZORDER | SWP_NOACTIVATE,
                )
            with self._lock:
                self._position = position

    def _destroy_gdi(self) -> None:
        hwnd = self._hwnd
        self._hwnd = 0
        if hwnd:
            user32.DestroyWindow(hwnd)
        if self._hbmp:
            gdi32.DeleteObject(self._hbmp)
            self._hbmp = None
        if self._hdc_mem:
            gdi32.DeleteDC(self._hdc_mem)
            self._hdc_mem = None
        if self._hdc_screen:
            user32.ReleaseDC(None, self._hdc_screen)
            self._hdc_screen = None

    # -------------------------------------------------------------- 窗口过程

    def _window_proc(self, hwnd, message, wparam, lparam):
        if message == WM_NCHITTEST:
            screen_x, screen_y = _unpack_lparam(lparam)
            with self._lock:
                width, height = self._frame_size
                position = self._position
                alpha = self._alpha
            hit = alpha_hit(alpha, width, height, screen_x - position[0], screen_y - position[1])
            return HTCLIENT if hit else HTTRANSPARENT
        if message == WM_MOUSEACTIVATE:
            return MA_NOACTIVATE
        if message == WM_LBUTTONDOWN:
            self._press_screen = self._cursor_screen()
            with self._lock:
                self._drag_origin = self._position
            self._dragging = False
            self._captured = True
            user32.SetCapture(hwnd)
            return 0
        if message == WM_MOUSEMOVE:
            client_x, client_y = _unpack_lparam(lparam)
            if self._on_mouse_move is not None and self._captured:
                self._safe_call(self._on_mouse_move, client_x, client_y)
            if self._captured:
                screen_x, screen_y = self._cursor_screen()
                dx = screen_x - self._press_screen[0]
                dy = screen_y - self._press_screen[1]
                if not self._dragging and (abs(dx) > DRAG_THRESHOLD or abs(dy) > DRAG_THRESHOLD):
                    self._dragging = True
                if self._dragging:
                    self._move_to(self._drag_origin[0] + dx, self._drag_origin[1] + dy)
            return 0
        if message == WM_LBUTTONUP:
            client_x, client_y = _unpack_lparam(lparam)
            was_dragging = self._dragging
            self._dragging = False
            if self._captured:
                self._captured = False
                user32.ReleaseCapture()
            if was_dragging:
                screen_x, screen_y = self._cursor_screen()
                self._safe_call(self._on_drag_end, screen_x, screen_y)
            else:
                self._safe_call(self._on_click, client_x, client_y)
            return 0
        if message == WM_MOUSEWHEEL:
            # wParam 高 16 位是滚轮档数（有符号），正数=向上。lParam 是屏幕坐标，用不上。
            notches = wheel_notches(wparam)
            if notches and self._on_wheel is not None:
                self._safe_call(self._on_wheel, notches)
            return 0
        if message == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if message == WM_DESTROY:
            self._hwnd = 0
            self._visible = False
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    # -------------------------------------------------------------- 小工具

    def _cursor_screen(self) -> tuple[int, int]:
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        return point.x, point.y

    def _move_to(self, x: int, y: int) -> None:
        hwnd = self._hwnd
        if not hwnd:
            return
        with self._lock:
            width, height = self._frame_size
            # 分层窗口的画面由 UpdateLayeredWindow 的 pptDst 决定，只挪窗口矩形
            # 会让「框」动而「球」留在原处，所以拖动也要排一次重绘。
            if self._alpha:
                self._pending = (self._alpha, (width, height), (int(x), int(y)))
        user32.SetWindowPos(
            hwnd, None, int(x), int(y), max(1, width), max(1, height),
            SWP_NOZORDER | SWP_NOACTIVATE,
        )
        with self._lock:
            self._position = (int(x), int(y))
        self._wake.set()

    def _safe_call(self, callback, *args) -> None:
        try:
            callback(*args)
        except Exception:  # 回调出错不能把消息循环带走，但必须留下痕迹
            log.exception("浮球回调失败：%s", callback)
