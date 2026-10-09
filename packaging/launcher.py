"""notice-hub 启动器：常驻托盘宿主。

- 首次运行生成 `.env`（随机 token）并创建桌面快捷方式，之后复用。
- 拉起 `qq-live-digest.exe --env <env> run` 作为子进程（无控制台窗口）。
- 托盘菜单：开始托管 / 结束托管 / 打开控制台 / 设置 / 退出。
- **从托盘退出 = 结束托管（停 NapCat、按设置恢复电脑版 QQ）+ 关服务子进程 + 关程序**。

托管本身实现在 `qq_live_digest/hosting.py`（与网页设置共用同一份逻辑，避免两套真相源）。
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from qq_live_digest import hosting
from qq_live_digest.config import Settings

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SERVICE_EXE = "qq-live-digest.exe"
LAUNCHER_EXE = "QQ-Notice-Hub.exe"
SHORTCUT_NAME = "notice-hub.lnk"
WATCHDOG_POLL_SECONDS = 2.0
WATCHDOG_BACKOFF_SECONDS = (5.0, 15.0, 45.0)

ENV_TEMPLATE = (
    "QQ_DIGEST_WEB=1\n"
    "QQ_DIGEST_WEB_HOST=127.0.0.1\n"
    "QQ_DIGEST_WEB_PORT={port}\n"
    "QQ_DIGEST_WEB_TOKEN={token}\n"
    "QQ_DIGEST_ONEBOT_ENABLED=1\n"
    "QQ_DIGEST_ONEBOT_TOKEN={onebot_token}\n"
    "QQ_DIGEST_NAPCAT_API_TOKEN=\n"
    "QQ_DIGEST_NAPCAT_API_URL=http://127.0.0.1:3001\n"
    "# 二维码路径留空：向导页点「一键接入」会自动找到 NapCat 的 cache\\qrcode.png 并写在这里\n"
    "QQ_DIGEST_NAPCAT_QR_PATH=\n"
    "# 默认 auto：优先用本机 CodeBuddy/WorkBuddy CLI（消耗账号额度），没有 CLI 或调用失败会自动回退本机 Ollama，无需干预\n"
    "QQ_DIGEST_LLM=1\n"
    "QQ_DIGEST_LLM_BACKEND=auto\n"
    "QQ_DIGEST_LLM_MODEL=qwen3.6:35b-a3b-q4_k_m-gpu20\n"
    "QQ_DIGEST_LLM_ENDPOINT=http://127.0.0.1:11434/v1/chat/completions\n"
    "QQ_DIGEST_LLM_TIMEOUT=600\n"
    "QQ_DIGEST_VISION=0\n"
    "DASHSCOPE_API_KEY=ollama-local\n"
    "# 「托管」：借用这个 QQ 号挂机收消息的那段时间。开始托管时自动退出电脑版 QQ，结束托管时自动开回来。\n"
    "QQ_DIGEST_HOSTING_QUIT_QQ=1\n"
    "QQ_DIGEST_HOSTING_RESTORE_QQ=1\n"
    "QQ_DIGEST_HOSTING_AUTO_ON_START=0\n"
    "QQ_DIGEST_AUTOSTART=0\n"
    "QQ_DIGEST_DATA_DIR={data}\n"
    "QQ_DIGEST_LOG_DIR={logs}\n"
)


def _read_env_value(env_file: Path, key: str) -> str:
    try:
        text = env_file.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""


def _ensure_env_file(root: Path, env_file: Path) -> None:
    if env_file.exists():
        return
    env_file.write_text(
        ENV_TEMPLATE.format(
            port=8766,
            token=secrets.token_urlsafe(32),
            onebot_token=secrets.token_urlsafe(32),
            data=root / "data",
            logs=root / "logs",
        ),
        encoding="utf-8",
    )


def _create_desktop_shortcut(root: Path) -> str | None:
    """首次运行时创建桌面快捷方式。目标写**绝对路径**，所以解压到哪都成立。"""
    target = root / LAUNCHER_EXE
    if not target.is_file():
        return None
    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        "$lnk = $ws.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) $env:NH_LNK)); "
        "$lnk.TargetPath = $env:NH_TARGET; "
        "$lnk.WorkingDirectory = $env:NH_ROOT; "
        "$lnk.IconLocation = $env:NH_ICON; "
        "$lnk.Description = 'notice-hub'; "
        "$lnk.Save()"
    )
    env = os.environ.copy()
    env.update({"NH_LNK": SHORTCUT_NAME, "NH_TARGET": str(target), "NH_ROOT": str(root), "NH_ICON": str(target) + ",0"})
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "创建失败：" + str(exc)
    return None if completed.returncode == 0 else "创建失败：" + (completed.stderr or "").strip()


_INSTANCE_MUTEX = None


def _acquire_single_instance() -> bool:
    """Windows 命名互斥体。已经在运行就返回 False，避免出现两个托盘图标。

    句柄存在模块级变量里，进程活着就一直持有，退出时由系统释放。
    """
    global _INSTANCE_MUTEX
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        _INSTANCE_MUTEX = kernel32.CreateMutexW(None, False, "Local\\notice-hub-launcher")
        return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return True  # 判断不了就别拦，宁可多开也别打不开


def _console_url(env_file: Path, port: int) -> str:
    token = _read_env_value(env_file, "QQ_DIGEST_WEB_TOKEN")
    return "http://127.0.0.1:%d/?token=%s" % (port, urllib.parse.quote(token, safe=""))


def _launch_service(root: Path, env_file: Path) -> subprocess.Popen:
    # --env 是 main.py 的顶层参数，必须放在子命令 run 之前，否则 argparse 报
    # "unrecognized arguments: --env ..."。CREATE_NO_WINDOW 避免再弹一个控制台窗口，
    # 也避免用户误关那个窗口把服务一起关掉。
    return subprocess.Popen(
        [str(root / SERVICE_EXE), "--env", str(env_file), "run"],
        cwd=root,
        creationflags=CREATE_NO_WINDOW,
    )


def _wait_health(port: int, process: subprocess.Popen, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port, timeout=1) as response:
                if response.status == 200:
                    return True
        except Exception:
            if process.poll() is not None:
                return False
        time.sleep(0.25)
    return False


def _icon_image(root: Path | None = None) -> object | None:
    """托盘图标：优先读 exe 同级 `assets\\icon-256.png`（或 `icon.ico`）。

    解压目录里没有 assets（或图坏了、没装 Pillow）时，回退到下面这段内联绘制 ——
    **没有 assets 也绝不让托盘起不来**。
    """
    base = Path(root) if root is not None else Path(sys.executable).resolve().parent
    for name in ("icon-256.png", "icon.ico"):
        try:
            if (base / "assets" / name).is_file():
                from PIL import Image

                with Image.open(base / "assets" / name) as loaded:
                    return loaded.convert("RGBA")
        except Exception:
            continue
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, size - 4, size - 4), radius=14, fill=(23, 59, 52, 255))
    draw.line((19, 33, 28, 43, 45, 21), fill=(255, 255, 255, 255), width=6, joint="curve")
    return image


class TrayHost:
    """托盘宿主：托管开关都走 `qq_live_digest.hosting`。"""

    def __init__(self, root: Path, env_file: Path, settings: Settings, process: subprocess.Popen, url: str) -> None:
        self.root = root
        self.env_file = env_file
        self.settings = settings
        self.process = process
        self.url = url
        self.icon = None
        self.busy = False
        self.active = bool(hosting.hosting_status(settings).get("hosting_active"))
        self._watchdog_stop = threading.Event()
        self._watchdog_thread = None
        self._restart_lock = threading.Lock()
        self._restart_pending = False
        self.restart_failures = 0
        self.manual_restart_required = False

    # ---- 服务看门狗 ----
    def _log_watchdog(self, message: str) -> None:
        try:
            log_file = self.root / "logs" / "launcher.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with log_file.open("a", encoding="utf-8") as stream:
                stream.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
        except OSError:
            pass

    def _restart_service(self, automatic: bool = False) -> bool:
        with self._restart_lock:
            old_process = self.process
            try:
                if old_process.poll() is None:
                    old_process.terminate()
                    old_process.wait(timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    old_process.kill()
                except OSError:
                    pass
            try:
                self.process = _launch_service(self.root, self.env_file)
            except OSError as exc:
                self._log_watchdog("服务启动失败：%s" % exc)
                return False
            return self.process.poll() is None

    def _restart_after_delay(self, delay: float) -> None:
        if self._watchdog_stop.wait(delay):
            with self._restart_lock:
                self._restart_pending = False
            return
        ok = self._restart_service(automatic=True)
        with self._restart_lock:
            self._restart_pending = False
        if not ok:
            if self.restart_failures >= len(WATCHDOG_BACKOFF_SECONDS):
                self.manual_restart_required = True
                self._notify("服务反复退出，需要人工处理")
            else:
                self._notify("服务重启失败，正在继续重试…")
        else:
            self._notify("服务已恢复。")

    def watchdog_tick(self) -> bool:
        """检查一次服务；返回 True 表示服务仍在运行。"""
        if self._watchdog_stop.is_set():
            return True
        if self.process.poll() is None:
            return True
        code = self.process.returncode
        pid = getattr(self.process, "pid", "?")
        self._log_watchdog("服务退出：pid=%s code=%s" % (pid, code))
        with self._restart_lock:
            if self._restart_pending or self.manual_restart_required:
                return False
            attempt = self.restart_failures
            if attempt >= len(WATCHDOG_BACKOFF_SECONDS):
                self.manual_restart_required = True
                self._notify("服务反复退出，需要人工处理")
                return False
            self.restart_failures += 1
            self._restart_pending = True
        self._notify("服务已退出（code=%s），正在重启…" % code)
        threading.Thread(target=self._restart_after_delay, args=(WATCHDOG_BACKOFF_SECONDS[attempt],), daemon=True).start()
        return False

    def _watchdog_loop(self) -> None:
        while not self._watchdog_stop.wait(WATCHDOG_POLL_SECONDS):
            self.watchdog_tick()

    def _start_watchdog(self) -> None:
        self._watchdog_stop.clear()
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, name="notice-hub-watchdog", daemon=True)
        self._watchdog_thread.start()

    def _stop_watchdog(self) -> None:
        self._watchdog_stop.set()

    # ---- 托盘动作 ----
    def _status_line(self) -> str:
        status = hosting.hosting_status(self.settings)
        qq = "电脑版 QQ 运行中" if status["user_qq_running"] else "电脑版 QQ 未运行"
        napcat = "NapCat 在线" if status["napcat_online"] else ("NapCat 已启动" if status["napcat_running"] else "NapCat 未启动")
        return "%s · %s · %s" % ("托管中" if status["hosting_active"] else "未托管", napcat, qq)

    def _notify(self, message: str) -> None:
        if self.icon is not None:
            try:
                self.icon.notify(message, "notice-hub")
            except Exception:
                pass

    def _start(self) -> None:
        self.busy = True
        try:
            self._notify("正在开始托管…")
            result = hosting.start(self.settings)
            self.active = bool(result.get("ok"))
            if not result.get("ok"):
                self._notify("开始托管失败：" + str(result.get("error") or "未知错误"))
            else:
                self._notify("已开始托管。" + self._status_line())
        finally:
            self.busy = False

    def _stop(self) -> None:
        self.busy = True
        try:
            self._notify("正在结束托管…")
            result = hosting.stop(self.settings)
            self.active = False
            if not result.get("ok"):
                self._notify("结束托管有问题：" + str(result.get("error") or "未知错误"))
            else:
                self._notify("已结束托管，电脑版 QQ 已恢复。")
        finally:
            self.busy = False

    def on_start(self, icon: object = None, item: object = None) -> None:
        if self.busy:
            return
        threading.Thread(target=self._start, daemon=True).start()

    def on_stop(self, icon: object = None, item: object = None) -> None:
        if self.busy:
            return
        threading.Thread(target=self._stop, daemon=True).start()

    def on_open(self, icon: object = None, item: object = None) -> None:
        webbrowser.open(self.url)

    def on_restart(self, icon: object = None, item: object = None) -> None:
        if self.busy:
            return

        def restart() -> None:
            self.busy = True
            try:
                self._notify("正在重启服务…")
                self._stop_watchdog()
                ok = self._restart_service()
                self.manual_restart_required = False
                self.restart_failures = 0
                if ok:
                    self._notify("服务已重启。")
                else:
                    self._notify("服务重启失败，需要人工处理")
            finally:
                self._start_watchdog()
                self.busy = False

        threading.Thread(target=restart, daemon=True).start()

    def on_quit(self, icon: object = None, item: object = None) -> None:
        def shutdown() -> None:
            self._stop_watchdog()
            if hosting.hosting_status(self.settings).get("hosting_active"):
                hosting.stop(self.settings)
            if self.icon is not None:
                self.icon.stop()

        self.busy = True
        threading.Thread(target=shutdown, daemon=True).start()

    def build_menu(self, pystray: object) -> object:
        return pystray.Menu(
            pystray.MenuItem("开始托管", self.on_start, enabled=lambda item: not self.busy),
            pystray.MenuItem("结束托管", self.on_stop, enabled=lambda item: not self.busy),
            pystray.MenuItem("重启服务", self.on_restart, enabled=lambda item: not self.busy),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("打开控制台", self.on_open, default=True),
            pystray.MenuItem("设置", self.on_open),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出（结束托管并关闭）", self.on_quit),
        )

    def run(self) -> bool:
        """起托盘并阻塞到用户选「退出」。没有托盘能力时返回 False。"""
        try:
            import pystray
        except Exception:
            return False
        image = _icon_image(self.root)
        if image is None:
            return False
        try:
            self.icon = pystray.Icon("notice-hub", image, "notice-hub · " + self._status_line(), self.build_menu(pystray))
            self._start_watchdog()
            if bool(getattr(self.settings, "hosting_auto_on_start", False)):
                threading.Thread(target=self._start, daemon=True).start()
            self.icon.run()
            self._stop_watchdog()
        except Exception:
            return False
        return True


def main() -> int:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    root = Path(sys.executable).resolve().parent
    data = root / "data"
    logs = root / "logs"
    data.mkdir(exist_ok=True)
    logs.mkdir(exist_ok=True)
    env_file = root / ".env"
    first_run = not env_file.exists()
    _ensure_env_file(root, env_file)
    if first_run:
        _create_desktop_shortcut(root)
    settings = Settings.from_env(env_file=env_file)
    port = int(getattr(settings, "web_port", 8766) or 8766)
    url = _console_url(env_file, port)
    if not _acquire_single_instance():
        # 已经在运行：把已经开着的那份控制台再打开一次，然后安静退出。
        webbrowser.open(url)
        return 0
    process = _launch_service(root, env_file)
    if not _wait_health(port, process):
        process.terminate()
        return 1
    webbrowser.open(url)
    host = TrayHost(root, env_file, settings, process, url)
    if host.run():
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
        return 0
    # 没有托盘（远程会话 / 缺依赖）时降级成老行为：等子进程结束。
    try:
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
