"""Edge App based floating-ball host with Win32 window styling."""
from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

EDGE_CANDIDATES = (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")
STATE_NAME = "floatball-state.json"
CLOSED_SIZE = (72, 72)
OPEN_SIZE = (380, 600)
DRAG_STRIP = 14
TITLE_OPEN = "nh-floatball:open"
TITLE_CLOSED = "nh-floatball:closed"

GWL_STYLE = -16
GWL_EXSTYLE = -20
GWLP_WNDPROC = -4
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_POPUP = 0x80000000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SW_SHOWNOACTIVATE = 4
SW_HIDE = 0
WM_CLOSE = 0x0010
WM_NCHITTEST = 0x0084
HTCLIENT = 1
HTCAPTION = 2
MONITOR_DEFAULTTONEAREST = 2


def clamp_position(x: int, y: int, width: int, height: int, work: tuple[int, int, int, int]) -> tuple[int, int]:
    left, top, right, bottom = work
    return max(left, min(int(x), right - width)), max(top, min(int(y), bottom - height))


def snap_position(x: int, y: int, width: int, height: int, work: tuple[int, int, int, int], margin: int = 8) -> tuple[int, int]:
    left, top, right, bottom = work
    x, y = clamp_position(x, y, width, height, work)
    candidates = [
        (abs(x - left), (left + margin, y)),
        (abs(right - width - x), (right - width - margin, y)),
        (abs(y - top), (x, top + margin)),
        (abs(bottom - height - y), (x, bottom - height - margin)),
    ]
    return min(candidates, key=lambda item: item[0])[1]


def process_matches_commandline(command_line: str, profile: Path) -> bool:
    return str(profile.resolve()).lower() in str(command_line or "").lower() and "msedge" in str(command_line or "").lower()


def _work_area(hwnd: int) -> tuple[int, int, int, int]:
    """Return the work area (screen minus taskbar) of the monitor holding hwnd."""
    user32 = ctypes.windll.user32

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]

    monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        width = user32.GetSystemMetrics(0)
        height = user32.GetSystemMetrics(1)
        return 0, 0, width, height - 48
    return info.rcWork.left, info.rcWork.top, info.rcWork.right, info.rcWork.bottom


def write_state(path: Path, x: int, y: int, expanded: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"x": int(x), "y": int(y), "expanded": bool(expanded)}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class FloatBallHost:
    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.state_path = self.data_dir / STATE_NAME
        self.process: subprocess.Popen | None = None
        self.hwnd: int | None = None
        self.visible = False
        self.expanded = False
        self.width, self.height = CLOSED_SIZE
        self._old_proc = None
        self._proc_ref = None
        self._stop_event = threading.Event()
        self._watcher: threading.Thread | None = None

    # --- Edge process -------------------------------------------------
    def _edge_path(self) -> str:
        for candidate in EDGE_CANDIDATES:
            if Path(candidate).is_file():
                return candidate
        raise RuntimeError("找不到 Microsoft Edge：请确认 msedge.exe 已安装")

    def _load_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        if not self.hwnd:
            return
        try:
            rect = self._rect()
            if rect is None:
                return
            write_state(self.state_path, rect[0], rect[1], self.expanded)
        except OSError:
            pass

    # --- window helpers ----------------------------------------------
    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    def _rect(self) -> tuple[int, int, int, int] | None:
        if not self.hwnd:
            return None
        rect = self.RECT()
        if not ctypes.windll.user32.GetWindowRect(self.hwnd, ctypes.byref(rect)):
            return None
        return rect.left, rect.top, rect.right, rect.bottom

    def _apply_styles(self, hwnd: int) -> None:
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
        style = (style & ~(WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_MINIMIZEBOX | WS_MAXIMIZEBOX)) | WS_POPUP
        user32.SetWindowLongW(hwnd, GWL_STYLE, style)
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE) | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW | 0x0020)
        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)

    def _install_drag_strip(self, hwnd: int) -> None:
        user32 = ctypes.windll.user32
        setter = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
        setter.restype = ctypes.c_void_p
        setter.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        host = self

        def handler(h, msg, wparam, lparam):
            if msg == WM_NCHITTEST:
                y = ctypes.c_short(lparam & 0xFFFF).value
                rect = host.RECT()
                if user32.GetWindowRect(h, ctypes.byref(rect)):
                    if y - rect.top <= DRAG_STRIP:
                        return HTCAPTION
                return HTCLIENT
            return user32.CallWindowProcW(host._old_proc, h, msg, wparam, lparam)

        self._proc_ref = self.WNDPROC(handler)
        self._old_proc = setter(hwnd, GWLP_WNDPROC, ctypes.cast(self._proc_ref, ctypes.c_void_p))

    def _place_initial(self, hwnd: int) -> None:
        state = self._load_state()
        work = _work_area(hwnd)
        x = int(state.get("x", work[2] - self.width - 24))
        y = int(state.get("y", work[3] - self.height - 24))
        x, y = clamp_position(x, y, self.width, self.height, work)
        ctypes.windll.user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, self.width, self.height, SWP_NOACTIVATE | SWP_SHOWWINDOW)

    # --- lifecycle ----------------------------------------------------
    def start(self, url: str) -> None:
        if os.name != "nt":
            raise RuntimeError("浮球宿主仅支持 Windows")
        user32 = ctypes.windll.user32
        profile = self.data_dir / "floatball-profile"
        profile.mkdir(parents=True, exist_ok=True)
        edge = self._edge_path()
        args = [
            edge,
            "--app=" + url,
            "--window-size=%d,%d" % (self.width, self.height),
            "--user-data-dir=" + str(profile),
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--disable-extensions",
        ]
        self.process = subprocess.Popen(args, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for _ in range(75):
            found: list[int] = []

            @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_ssize_t)
            def enum_window(handle, _param):
                pid = ctypes.c_uint()
                user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
                if pid.value == self.process.pid and user32.IsWindowVisible(handle):
                    found.append(int(handle))
                return True

            user32.EnumWindows(enum_window, 0)
            if found:
                self.hwnd = found[0]
                break
            if self.process.poll() is not None:
                break
            time.sleep(0.2)
        if not self.hwnd:
            raise RuntimeError("Edge 浮球窗口在 15 秒内未出现")
        self._apply_styles(self.hwnd)
        self._install_drag_strip(self.hwnd)
        self._place_initial(self.hwnd)
        self.visible = True
        self._stop_event.clear()
        self._watcher = threading.Thread(target=self._watch, name="floatball-watch", daemon=True)
        self._watcher.start()

    def _watch(self) -> None:
        """Poll the page title for open/closed and hide while a fullscreen app runs."""
        user32 = ctypes.windll.user32
        last_title = ""
        last_pos: tuple[int, int] | None = None
        while not self._stop_event.wait(0.25):
            if not self.hwnd or self.process is None or self.process.poll() is not None:
                break
            buffer = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(self.hwnd, buffer, 256)
            title = buffer.value
            if title and title != last_title:
                last_title = title
                if title == TITLE_OPEN and not self.expanded:
                    self.expanded = True
                    self.resize(*OPEN_SIZE)
                elif title == TITLE_CLOSED and self.expanded:
                    self.expanded = False
                    self.resize(*CLOSED_SIZE)
            if self._foreground_fullscreen():
                if self.visible:
                    self.hide()
            elif not self.visible and self.hwnd:
                self.show()
            rect = self._rect()
            if rect and (rect[0], rect[1]) != last_pos:
                last_pos = (rect[0], rect[1])
                self._save_state()

    def _foreground_fullscreen(self) -> bool:
        user32 = ctypes.windll.user32
        foreground = user32.GetForegroundWindow()
        if not foreground or foreground == self.hwnd:
            return False
        rect = self.RECT()
        if not user32.GetWindowRect(foreground, ctypes.byref(rect)):
            return False
        monitor = 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        return (rect.left <= monitor[0] and rect.top <= monitor[1]
                and rect.right >= monitor[2] and rect.bottom >= monitor[3])

    def resize(self, width: int, height: int) -> None:
        self.width, self.height = int(width), int(height)
        if not self.hwnd:
            return
        rect = self._rect()
        work = _work_area(self.hwnd)
        x, y = (rect[0], rect[1]) if rect else (work[2] - self.width - 24, work[3] - self.height - 24)
        x, y = snap_position(x, y, self.width, self.height, work)
        ctypes.windll.user32.SetWindowPos(
            self.hwnd, HWND_TOPMOST, x, y, self.width, self.height,
            SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )

    def show(self) -> None:
        if self.hwnd:
            ctypes.windll.user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
            self.visible = True

    def hide(self) -> None:
        if self.hwnd:
            ctypes.windll.user32.ShowWindow(self.hwnd, SW_HIDE)
            self.visible = False

    def stop(self) -> None:
        self._stop_event.set()
        self._save_state()
        if self.hwnd:
            ctypes.windll.user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        if self.process and self.process.poll() is None:
            self.process.terminate()
        self.process = None
        self.hwnd = None
        self.visible = False
