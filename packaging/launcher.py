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
        "$lnk.Description = 'notice-hub'; "
        "$lnk.Save()"
    )
    env = os.environ.copy()
    env.update({"NH_LNK": SHORTCUT_NAME, "NH_TARGET": str(target), "NH_ROOT": str(root)})
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


def _icon_image() -> object | None:
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, size - 4, size - 4), radius=14, fill=(59, 91, 219, 255))
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

    def on_quit(self, icon: object = None, item: object = None) -> None:
        def shutdown() -> None:
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
        image = _icon_image()
        if image is None:
            return False
        try:
            self.icon = pystray.Icon("notice-hub", image, "notice-hub · " + self._status_line(), self.build_menu(pystray))
            if bool(getattr(self.settings, "hosting_auto_on_start", False)):
                threading.Thread(target=self._start, daemon=True).start()
            self.icon.run()
        except Exception:
            return False
        return True


def main() -> int:
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
