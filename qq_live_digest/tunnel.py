"""一键开通公网入口（Tailscale Funnel）的检测 / 安装 / 登录 / 开通道状态机。

目标是「用户只看页面、只点一次按钮」：没装 Tailscale 就自动下载官方安装包并提权安装，
没登录就触发登录并等用户完成，登录后开 Funnel，最后把公网地址返回给页面。
每个阶段都有超时，失败可重试，失败信息写成用户能看懂的中文。

命令与安装参数按 Tailscale 1.102.4 核对（本机 `C:\Program Files\Tailscale\tailscale.exe`）：
  - `tailscale status --json` 的 `BackendState` 为 `Running` 表示已登录；`AuthURL` 是待登录链接。
  - `tailscale up` 触发登录（Windows 版会让 tailscaled 打开浏览器登录页）；它**没有**
    `--funnel` 参数，社区教程里的 `tailscale up --funnel` 在当前版本会直接报错。
  - `tailscale funnel --bg --yes --https=8443 http://127.0.0.1:<port>` 开公网；
    另可用 `tailscale funnel status --json` / `serve status --json` 读回已生效的规则。
  - Windows 安装包 `https://pkgs.tailscale.com/stable/tailscale-setup-latest.exe` 是 WiX
    安装器，支持 `/quiet /norestart`；Tailscale 自己的自动更新用的也是
    `msiexec /i ... /quiet /norestart /qn`（clientupdate/clientupdate_windows.go）。
  - 提权走 `ShellExecuteW(..., "runas", ...)`：UAC 只弹一次；返回码 <= 32 表示没启动
    （1223 = 用户点了「否」）。
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from .win_text import decode_console_output

LOGGER = logging.getLogger(__name__)

TAILSCALE_INSTALLER_URL = "https://pkgs.tailscale.com/stable/tailscale-setup-latest.exe"
TAILSCALE_FUNNEL_PORTS = (443, 8443, 10000)
PUBLIC_PORT = 8443
INSTALL_TIMEOUT_SECONDS = 5 * 60
LOGIN_TIMEOUT_SECONDS = 10 * 60
FUNNEL_TIMEOUT_SECONDS = 30
POLL_SECONDS = 2.0
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def tailscale_executable() -> str:
    """找到本机 tailscale.exe；没装返回空串。"""
    executable = shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"
    return executable if executable and Path(executable).is_file() else ""


def serve_config(executable: str) -> dict[str, Any]:
    """读 `tailscale serve status --json`；命令跑不起来或输出不是 JSON 时返回空字典。"""
    try:
        completed = subprocess.run(
            [executable, "serve", "status", "--json"],
            capture_output=True,
            timeout=8,
            check=False,
            creationflags=_CREATE_NO_WINDOW,
        )
        config = json.loads(decode_console_output(completed.stdout) or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    return config if isinstance(config, dict) else {}


def public_app_url(port: int) -> str:
    """手机 App 用的公网地址：整站（页面 + /api/*）都反代到本机这个端口的那条 Funnel 规则。

    和只认 .ics 的日历地址不同，这里必须有一条 "/" 路由，而且那个端口得在 AllowFunnel 里。
    """
    executable = tailscale_executable()
    if not executable:
        return ""
    config = serve_config(executable)
    funnel = config.get("AllowFunnel") or {}
    for target, entry in (config.get("Web") or {}).items():
        target = str(target)
        if not funnel.get(target):
            continue
        host, _, port_text = target.rpartition(":")
        if port_text not in {str(value) for value in TAILSCALE_FUNNEL_PORTS}:
            continue
        proxy = str((((entry or {}).get("Handlers") or {}).get("/") or {}).get("Proxy") or "")
        try:
            if urllib.parse.urlsplit(proxy).port != port:
                continue
        except ValueError:
            continue
        return f"https://{host}" + ("" if port_text == "443" else f":{port_text}")
    return ""


def status_hint(executable: str) -> str:
    """Tailscale 没登录 / 没运行时给一句能看懂的原因；状态正常或查不到时返回空串。"""
    status = read_status(executable)
    if not status:
        return "没连上 Tailscale（命令跑不起来）：请先打开 Tailscale 并登录，再点一次「开通公网入口」。"
    state = str(status.get("BackendState") or "")
    if state == "Running":
        return ""
    if state in {"", "NeedsLogin", "NoState", "Stopped"}:
        return "Tailscale 还没登录：请先打开 Tailscale 用同一个账号登录，再点一次「开通公网入口」。"
    return f"Tailscale 现在不可用（状态 {state}）：请先打开 Tailscale 登录并确认已连接，再点一次。"


def read_status(executable: str) -> dict[str, Any]:
    """`tailscale status --json` 的结果；命令失败返回空字典。"""
    try:
        completed = subprocess.run(
            [executable, "status", "--json"],
            capture_output=True,
            timeout=8,
            check=False,
            creationflags=_CREATE_NO_WINDOW,
        )
        status = json.loads(decode_console_output(completed.stdout) or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    return status if isinstance(status, dict) else {}


def enable_funnel(port: int, public_port: int = PUBLIC_PORT) -> dict[str, Any]:
    """电脑上点一下就整站开到公网（Tailscale Funnel，免费，不用买服务器）。"""
    executable = tailscale_executable()
    if not executable:
        return {"ok": False, "error": "这台电脑上没找到 Tailscale，装了以后再点一次。"}
    if public_port not in TAILSCALE_FUNNEL_PORTS:
        return {"ok": False, "error": "Tailscale 只允许 443、8443、10000 这三个公网端口。"}
    hint = status_hint(executable)
    if hint:
        return {"ok": False, "error": hint}
    command = [executable, "funnel", "--bg", "--yes", f"--https={public_port}", f"http://127.0.0.1:{port}"]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=FUNNEL_TIMEOUT_SECONDS,
            check=False,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {"ok": False, "error": f"执行 Tailscale 命令失败：{error}"}
    url = public_app_url(port)
    if url:
        return {"ok": True, "url": url}
    # tailscale prints its error in the console code page; decode defensively so a
    # Chinese/GBK message reaches the user instead of raising UnicodeDecodeError.
    detail = decode_console_output(completed.stderr or completed.stdout).strip()
    lowered = detail.casefold()
    if "funnel" in lowered and "not enabled" in lowered:
        return {
            "ok": False,
            "error": "Tailscale 账户还没开启 Funnel：去 Tailscale 管理后台的 Access Controls 里允许 Funnel（免费版也支持），保存后再点一次。",
        }
    return {"ok": False, "error": detail[:300] or "Tailscale 没有接受这条公网规则。"}


def download_installer(directory: str) -> str:
    """下载官方 Windows 安装包到指定目录，返回路径。"""
    target = Path(directory) / "tailscale-setup-latest.exe"
    request = urllib.request.Request(TAILSCALE_INSTALLER_URL, headers={"User-Agent": "notice-hub"})
    with urllib.request.urlopen(request, timeout=120) as response, target.open("wb") as handle:
        shutil.copyfileobj(response, handle)
    if target.stat().st_size < 1_000_000:
        target.unlink(missing_ok=True)
        raise OSError("安装包不完整")
    return str(target)


def run_installer(path: str) -> None:
    """提权静默安装：ShellExecuteW(runas) 只弹一次 UAC。"""
    if sys.platform != "win32":
        raise OSError("自动安装只支持 Windows")
    import ctypes

    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", str(path), "/quiet /norestart", None, 1)
    if result <= 32:
        raise OSError("管理员授权被取消" if result == 1223 else f"安装程序没能启动（{result}）")


def start_login(executable: str) -> subprocess.Popen[bytes]:
    """触发登录：Windows 版 tailscaled 会自动打开浏览器登录页。"""
    return subprocess.Popen(
        [executable, "up"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_CREATE_NO_WINDOW,
    )


class FunnelSetup:
    """一次「开通公网入口」的后台任务；页面轮询 snapshot() 看进度。

    state: idle / installing / waiting_login / enabling / ready / error
    """

    def __init__(self, port: int, public_port: int = PUBLIC_PORT) -> None:
        self.port = port
        self.public_port = public_port
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._snapshot: dict[str, Any] = {
            "state": "idle",
            "message": "",
            "url": "",
            "auth_url": "",
            "retryable": True,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._snapshot)

    def _set(self, **fields: Any) -> None:
        with self._lock:
            self._snapshot.update(fields)

    def start(self) -> dict[str, Any]:
        """开始（或继续）开通流程；已经在跑或已开通时原样返回当前状态。"""
        with self._lock:
            state = str(self._snapshot.get("state") or "idle")
            if state in {"installing", "waiting_login", "enabling"}:
                return dict(self._snapshot)
            if state == "ready" and self._snapshot.get("url"):
                return dict(self._snapshot)
            self._snapshot = {
                "state": "installing",
                "message": "正在检查这台电脑上的 Tailscale…",
                "url": "",
                "auth_url": "",
                "retryable": True,
            }
            self._thread = threading.Thread(target=self.run, name="tailscale-funnel-setup", daemon=True)
            self._thread.start()
            return dict(self._snapshot)

    def _fail(self, message: str) -> None:
        self._set(state="error", message=message, url="", auth_url="", retryable=True)

    def run(self) -> None:
        """同步跑完整个流程；start() 在后台线程里调它，测试可直接调。"""
        try:
            self._run()
        except Exception as error:  # noqa: BLE001 - 后台线程要把任何异常变成页面能看懂的提示
            LOGGER.warning("开通公网入口失败", exc_info=True)
            self._fail(f"开通公网入口时出错：{error}。可以点「重试」。")

    def _run(self) -> None:
        executable = tailscale_executable()
        if not executable:
            self._set(state="installing", message="正在下载 Tailscale 安装包（约 40 MB）…")
            try:
                installer = download_installer(tempfile.gettempdir())
            except (OSError, ValueError) as error:
                self._fail(f"下载 Tailscale 安装包失败：{error}。请检查网络后点「重试」。")
                return
            self._set(message="安装包下载好了，正在请求管理员权限。请在弹窗里点「是」。")
            try:
                run_installer(installer)
            except OSError as error:
                self._fail(f"启动 Tailscale 安装程序失败：{error}。再点一次「重试」即可。")
                return
            executable = self._wait_installed()
            if not executable:
                self._fail("安装 Tailscale 超时（等了 5 分钟还没装好）。可以再点「重试」。")
                return

        status = read_status(executable)
        if str(status.get("BackendState") or "") != "Running":
            self._set(
                state="waiting_login",
                message="请在打开的浏览器里登录 Tailscale（微信 / Google 账号都行），登录完会自动继续。",
                auth_url=str(status.get("AuthURL") or ""),
            )
            try:
                process = start_login(executable)
            except OSError as error:
                self._fail(f"启动 Tailscale 登录失败：{error}。再点一次「重试」即可。")
                return
            try:
                logged_in = self._wait_login(executable)
            finally:
                if process.poll() is None:
                    process.terminate()
            if not logged_in:
                return

        self._set(state="enabling", message="已登录，正在开启公网入口…", auth_url="")
        result = enable_funnel(self.port, self.public_port)
        if not result.get("ok"):
            self._fail(str(result.get("error") or "开通公网入口失败"))
            return
        self._set(
            state="ready",
            message="公网入口已开通，手机用移动网络也能连。",
            url=str(result.get("url") or ""),
            retryable=False,
        )

    def _wait_installed(self) -> str:
        deadline = time.monotonic() + INSTALL_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            executable = tailscale_executable()
            if executable:
                return executable
            time.sleep(POLL_SECONDS)
        return ""

    def _wait_login(self, executable: str) -> bool:
        """等登录完成；超时或需要管理后台批准时写好错误信息并返回 False。"""
        deadline = time.monotonic() + LOGIN_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            status = read_status(executable)
            state = str(status.get("BackendState") or "")
            if state == "Running":
                return True
            if state == "NeedsMachineAuth":
                self._fail("这台电脑还要在 Tailscale 管理后台批准一下，批准后点「重试」。")
                return False
            self._set(auth_url=str(status.get("AuthURL") or ""))
            time.sleep(POLL_SECONDS)
        self._fail("等待 Tailscale 登录超时（10 分钟）。请在电脑上打开 Tailscale 登录，再点「重试」。")
        return False
