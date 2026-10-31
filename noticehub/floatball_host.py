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
PID_NAME = "floatball.pid"
WINDOW_CLASS = "Chrome_WidgetWin_1"
CLOSED_SIZE = (72, 72)
OPEN_SIZE = (380, 600)
DRAG_STRIP = 14
TITLE_OPEN = "nh-floatball:open"
TITLE_CLOSED = "nh-floatball:closed"
TITLE_SIZES = {TITLE_OPEN: OPEN_SIZE, TITLE_CLOSED: CLOSED_SIZE}
TITLE_PATTERN = re.compile(r"^nh-floatball:(open|closed)(?::(\d+)x(\d+))?$")

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
WS_EX_TOPMOST = 0x00000008
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


def parse_title(title: str) -> tuple[str, int, int] | None:
    """Return (state, viewport width, viewport height); viewport is 0 when the page cannot report it."""
    match = TITLE_PATTERN.match(str(title or ""))
    if not match:
        return None
    state = match.group(1)
    width = int(match.group(2) or 0)
    height = int(match.group(3) or 0)
    return state, width, height


_DPI_READY = False


def ensure_dpi_aware() -> None:
    """Real pixels everywhere: otherwise GetWindowRect is virtualized while the DPI scale is not."""
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
    buffer = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def _kill_tree(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def pids_using_profile(profile: Path) -> list[int]:
    """Edge keeps using an old window when its profile dir is still open, so new launches must clear it."""
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
        "Where-Object { $_.CommandLine -like '*%s*' } | "
        "ForEach-Object { $_.ProcessId }" % str(profile).replace("'", "''")
    )
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in (done.stdout or "").splitlines():
        line = line.strip()
        if line.isdigit():
            found.append(int(line))
    return found


def size_for_title(title: str) -> tuple[int, int] | None:
    """Return a trusted layout size; browser-loading titles are ignored."""
    value = str(title or "")
    if not value.startswith("nh-floatball:"):
        return None
    return TITLE_SIZES.get(value)


def region_radius(expanded: bool, height: int) -> int:
    """Collapsed card is a circle; the open panel gets a 16px corner radius."""
    return int(height) // 2 if not expanded else 16


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


_U32_READY = False


def user32():
    """ctypes defaults treat pointers as 32-bit; declare signatures once so 64-bit hwnds work."""
    global _U32_READY
    lib = ctypes.windll.user32
    if not _U32_READY:
        lib.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        lib.SetWindowPos.restype = ctypes.c_bool
        lib.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(RECT)]
        lib.GetWindowRect.restype = ctypes.c_bool
        lib.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.GetWindowLongW.restype = ctypes.c_long
        lib.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
        lib.SetWindowLongW.restype = ctypes.c_long
        lib.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        lib.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        lib.GetForegroundWindow.restype = ctypes.c_void_p
        lib.MonitorFromWindow.restype = ctypes.c_void_p
        lib.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        lib.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        lib.GetMonitorInfoW.restype = ctypes.c_bool
        lib.IsIconic.argtypes = [ctypes.c_void_p]
        lib.IsWindowVisible.argtypes = [ctypes.c_void_p]
        lib.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
        _U32_READY = True
    return lib


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
        self.url = ""
        self.visible = False
        self.expanded = False
        self.width, self.height = CLOSED_SIZE
        self.viewport: tuple[int, int] | None = None
        self._fitted = False
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
            x, y = self._visible_position()
            write_state(self.state_path, x, y, self.expanded)
        except OSError:
            pass

    # --- window helpers ----------------------------------------------
    def _rect(self) -> tuple[int, int, int, int] | None:
        if not self.hwnd:
            return None
        rect = RECT()
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
                rect = RECT()
                if user32.GetWindowRect(h, ctypes.byref(rect)):
                    box = host._client_box()
                    offset = y - (box[5] if box else rect.top)
                    if 0 <= offset <= DRAG_STRIP:
                        return HTCAPTION
                return HTCLIENT
            return user32.CallWindowProcW(host._old_proc, h, msg, wparam, lparam)

        self._proc_ref = self.WNDPROC(handler)
        self._old_proc = setter(hwnd, GWLP_WNDPROC, ctypes.cast(self._proc_ref, ctypes.c_void_p))

    def _dpi_scale(self) -> float:
        if not self.hwnd:
            return 1.0
        try:
            dpi = ctypes.windll.user32.GetDpiForWindow(self.hwnd)
        except AttributeError:
            dpi = 96
        return (dpi or 96) / 96.0

    def _card_px(self) -> tuple[int, int]:
        scale = self._dpi_scale()
        width, height = OPEN_SIZE if self.expanded else CLOSED_SIZE
        return max(1, round(width * scale)), max(1, round(height * scale))

    def _chrome(self) -> tuple[int, int, int, int]:
        """Page area inside the window: Edge paints its own title strip on top of it."""
        rect = self._rect()
        if rect is None or not self.viewport:
            return 0, 0, 0, 0
        scale = self._dpi_scale()
        content_w = round(self.viewport[0] * scale)
        content_h = round(self.viewport[1] * scale)
        win_w = rect[2] - rect[0]
        win_h = rect[3] - rect[1]
        return 0, max(0, win_h - content_h), max(0, win_w - content_w), 0

    def _region_box(self) -> tuple[int, int, int, int]:
        """The card sits in the window's bottom-right page corner; the rest is clipped away."""
        rect = self._rect()
        if rect is None:
            return 0, 0, *self._card_px()
        _, _, right, bottom = self._chrome()
        card_w, card_h = self._card_px()
        win_w = rect[2] - rect[0]
        win_h = rect[3] - rect[1]
        return win_w - right - card_w, win_h - bottom - card_h, card_w, card_h

    def _visible_position(self) -> tuple[int, int]:
        rect = self._rect()
        if rect is None:
            return 0, 0
        left, top, _, _ = self._region_box()
        return rect[0] + left, rect[1] + top

    def _place_card(self, x: int, y: int) -> None:
        """(x, y) is the card's screen top-left; the window is larger and clipped down to it."""
        rect = self._rect()
        if rect is None:
            return
        left, top, _, _ = self._region_box()
        ctypes.windll.user32.SetWindowPos(
            self.hwnd, HWND_TOPMOST, int(x) - left, int(y) - top, 0, 0,
            SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
        self._ensure_topmost(self.hwnd)

    def _apply_region(self, hwnd: int) -> None:
        """Clip the window to the card so Edge's title strip and side borders stay invisible."""
        left, top, width, height = self._region_box()
        radius = region_radius(self.expanded, height)
        gdi = ctypes.windll.gdi32
        region = gdi.CreateRoundRectRgn(left, top, left + width + 1, top + height + 1,
                                        radius * 2, radius * 2)
        if region:
            ctypes.windll.user32.SetWindowRgn(hwnd, region, True)

    def _place_initial(self, hwnd: int) -> None:
        work = _work_area(hwnd)
        width, height = self._card_px()
        x, y = work[2] - width - 8, work[3] - height - 8
        self._apply_region(hwnd)
        self._place_card(x, y)

    def _ensure_topmost(self, hwnd: int) -> None:
        user32 = ctypes.windll.user32
        if not user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST:
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    # --- lifecycle ----------------------------------------------------
    def _log(self, message: str) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with (self.data_dir / "floatball.log").open("a", encoding="utf-8") as handle:
                handle.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
        except OSError:
            pass

    def start(self, url: str) -> None:
        if os.name != "nt":
            raise RuntimeError("浮球宿主仅支持 Windows")
        self.url = url
        ensure_dpi_aware()
        profile = self.data_dir / "floatball-profile"
        profile.mkdir(parents=True, exist_ok=True)
        self._clear_stale(profile)
        edge = self._edge_path()
        args = [
            edge,
            "--app=" + url,
            "--window-size=%d,%d" % OPEN_SIZE,
            "--user-data-dir=" + str(profile),
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--disable-extensions",
        ]
        self.process = subprocess.Popen(args, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            (self.data_dir / PID_NAME).write_text(str(self.process.pid), encoding="utf-8")
        except OSError:
            pass
        self.hwnd = self._find_window()
        if not self.hwnd:
            _kill_tree(self.process.pid)
            self.process = None
            raise RuntimeError("Edge 浮球窗口在 15 秒内未出现")
        self._style_window(self.hwnd)
        self._place_initial(self.hwnd)
        self.visible = True
        self._stop_event.clear()
        self._watcher = threading.Thread(target=self._watch, name="floatball-watch", daemon=True)
        self._watcher.start()

    def _clear_stale(self, profile: Path) -> None:
        """A leftover Edge on the same profile swallows `--app` and no new window ever shows up."""
        stale: set[int] = set()
        try:
            pid = int((self.data_dir / PID_NAME).read_text(encoding="utf-8").strip())
            stale.add(pid)
        except (OSError, ValueError):
            pass
        stale.update(pids_using_profile(profile))
        for pid in stale:
            if pid <= 0 or (self.process and pid == self.process.pid):
                continue
            self._log("清理残留浮球 Edge pid=%s" % pid)
            _kill_tree(pid)
        if stale:
            time.sleep(0.6)

    def _find_window(self) -> int | None:
        """Pick the real app window: Edge also owns helper windows that are visible but not it."""
        user32 = ctypes.windll.user32
        process = self.process
        if process is None:
            return None
        stable: int | None = None
        stable_hits = 0
        for _ in range(75):
            found: list[int] = []

            @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_ssize_t)
            def enum_window(handle, _param):
                pid = ctypes.c_uint()
                user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
                if pid.value != process.pid or not user32.IsWindowVisible(handle):
                    return True
                if window_class(handle) != WINDOW_CLASS:
                    return True
                rect = RECT()
                if not user32.GetWindowRect(handle, ctypes.byref(rect)):
                    return True
                if rect.right - rect.left < 20 or rect.bottom - rect.top < 20:
                    return True
                found.append(int(handle))
                return True

            user32.EnumWindows(enum_window, 0)
            candidate = found[0] if found else None
            if candidate and candidate == stable:
                stable_hits += 1
                if stable_hits >= 3:
                    return candidate
            else:
                stable = candidate
                stable_hits = 1 if candidate else 0
            if process.poll() is not None:
                return None
            time.sleep(0.2)
        return stable

    def _style_window(self, hwnd: int) -> None:
        self._apply_styles(hwnd)
        self._install_drag_strip(hwnd)
        user32 = ctypes.windll.user32
        for _ in range(3):
            style = user32.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF
            if not style & WS_CAPTION:
                break
            self._apply_styles(hwnd)
            time.sleep(0.1)
        self._log("浮球窗口就绪 hwnd=%s style=0x%08x" % (hwnd, user32.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF))

    def _watch(self) -> None:
        """Poll the page title for open/closed, hide while a fullscreen app runs, revive a dead window."""
        user32 = ctypes.windll.user32
        last_title = ""
        last_pos: tuple[int, int] | None = None
        revivals = 0
        while not self._stop_event.wait(0.2):
            if not self.hwnd or self.process is None:
                break
            code = self.process.poll()
            if code is not None:
                self._log("浮球 Edge 进程退出，code=%s" % code)
                self.hwnd = None
                self.visible = False
                if self._revive(revivals):
                    revivals += 1
                    continue
                break
            if not user32.IsWindow(self.hwnd):
                self._log("浮球窗口已消失（Edge 进程仍在）")
                self.hwnd = None
                self.visible = False
                if self._revive(revivals):
                    revivals += 1
                    continue
                break
            if user32.GetWindowLongW(self.hwnd, GWL_STYLE) & WS_CAPTION:
                self._log("浮球窗口被重新加回系统边框，重新去框")
                self._apply_styles(self.hwnd)
                self._install_drag_strip(self.hwnd)
            if self.visible:
                self._ensure_topmost(self.hwnd)
            buffer = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(self.hwnd, buffer, 256)
            title = buffer.value
            if title and title != last_title:
                last_title = title
                self._apply_title(title)
            if self._foreground_fullscreen():
                if self.visible:
                    self.hide()
            elif not self.visible and self.hwnd:
                self.show()
            visible = self._visible_position()
            if visible != last_pos:
                last_pos = visible
                self._save_state()

    def _revive(self, revivals: int) -> bool:
        """Windows the user closed or Edge killed itself: bring the float back instead of going silent."""
        if not self.url or revivals >= 3:
            return False
        if self.process is not None:
            _kill_tree(self.process.pid)
            self.process = None
        time.sleep(0.8)
        try:
            self.start(self.url)
        except (OSError, RuntimeError) as exc:
            self._log("浮球重启失败：%s" % exc)
            return False
        self._log("浮球已自动重启")
        return True

    def _foreground_fullscreen(self) -> bool:
        """True only when the foreground window covers its whole monitor (not just maximized)."""
        user32 = ctypes.windll.user32
        foreground = user32.GetForegroundWindow()
        if not foreground or foreground == self.hwnd or user32.IsIconic(foreground):
            return False
        rect = RECT()
        if not user32.GetWindowRect(foreground, ctypes.byref(rect)):
            return False

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]

        monitor = user32.MonitorFromWindow(foreground, MONITOR_DEFAULTTONEAREST)
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return False
        m = info.rcMonitor
        return (abs(rect.left - m.left) <= 2 and abs(rect.top - m.top) <= 2
                and abs(rect.right - m.right) <= 2 and abs(rect.bottom - m.bottom) <= 2)

    def resize(self, width: int, height: int) -> None:
        self.width, self.height = int(width), int(height)
        if not self.hwnd:
            return
        self._apply_region(self.hwnd)

    def _fit_window(self) -> None:
        """One-off: make the page viewport exactly the open-panel size, then never resize again."""
        rect = self._rect()
        if rect is None or not self.viewport or self._fitted:
            return
        self._fitted = True
        scale = self._dpi_scale()
        chrome_w = (rect[2] - rect[0]) - round(self.viewport[0] * scale)
        chrome_h = (rect[3] - rect[1]) - round(self.viewport[1] * scale)
        target_w = round(OPEN_SIZE[0] * scale) + max(0, chrome_w)
        target_h = round(OPEN_SIZE[1] * scale) + max(0, chrome_h)
        ctypes.windll.user32.SetWindowPos(
            self.hwnd, HWND_TOPMOST, 0, 0, target_w, target_h,
            SWP_NOMOVE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
        self._log("窗口定尺 %sx%s chrome=%sx%s" % (target_w, target_h, chrome_w, chrome_h))
        self._place_initial(self.hwnd)

    def _apply_title(self, title: str) -> None:
        """The page reports its viewport; open/closed only changes which part of it stays visible."""
        parsed = parse_title(title)
        if parsed is None:
            return
        name, viewport_width, viewport_height = parsed
        if viewport_width and viewport_height:
            changed = (self.viewport is None
                       or abs(viewport_width - self.viewport[0]) > 2
                       or abs(viewport_height - self.viewport[1]) > 2)
            if changed:
                self.viewport = (viewport_width, viewport_height)
                self._fit_window()
        expanded = name == "open"
        if expanded != self.expanded:
            self.expanded = expanded
            if self.hwnd:
                self._apply_region(self.hwnd)

    def show(self) -> None:
        if self.hwnd:
            ctypes.windll.user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
            self.visible = True

    def hide(self) -> None:
        if self.hwnd:
            ctypes.windll.user32.ShowWindow(self.hwnd, SW_HIDE)
            self.visible = False

    def stop(self) -> None:
        self._log("stop() 主动关闭浮球")
        self._stop_event.set()
        self._save_state()
        if self.hwnd:
            ctypes.windll.user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        if self.process and self.process.poll() is None:
            _kill_tree(self.process.pid)
        try:
            (self.data_dir / PID_NAME).unlink()
        except OSError:
            pass
        self.process = None
        self.hwnd = None
        self.visible = False


if os.name == "nt":
    user32()
