"""notice-hub 启动器：常驻托盘宿主。

- 首次运行生成 `.env`（随机 token）并创建桌面快捷方式，之后复用。
- 拉起 `qq-live-digest.exe --env <env> run` 作为子进程（无控制台窗口）。
- 托盘菜单：开始托管 / 结束托管 / 打开控制台 / 设置 / 退出。
- **从托盘退出 = 结束托管（停 NapCat、按设置恢复电脑版 QQ）+ 关服务子进程 + 关程序**。

托管本身实现在 `qq_live_digest/hosting.py`（与网页设置共用同一份逻辑，避免两套真相源）。
"""

from __future__ import annotations

import os
import json
import sqlite3
import secrets
import hashlib
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
APPLYING_FLAG_MAX_AGE_SECONDS = 600
# 托盘「退出」的硬兜底：托管要动 NapCat 与电脑版 QQ，慢也得有个上限。
QUIT_HARD_DEADLINE_SECONDS = 30.0
# `--quit` 与托盘之间的信号文件：托盘没有可投递的窗口消息，看门狗每
# WATCHDOG_POLL_SECONDS 查一次这个文件，见到就执行退出（见 watchdog_tick）。
QUIT_REQUEST_NAME = "launcher.quit"

ENV_TEMPLATE = (
    "QQ_DIGEST_WEB=1\n"
    # 必须绑 0.0.0.0（与 qq_live_digest/config.py 的默认值一致），不能写 127.0.0.1：
    # ① webapp.py 只在 web_host 不是回环地址时才生成并持久化访问令牌，写死回环 ⇒
    #    首装实例永远没有 token，/api/app/qr.png 永远 400「本机还没有访问令牌」，配对二维码加载不出来；
    # ② 只绑回环时 /api/pair/info 广告的是局域网/tailscale 地址，手机根本连不上。
    # 首次启动 Windows 防火墙可能弹窗，要允许「专用网络」访问（手机扫码配对 + 日历订阅需要）。
    "QQ_DIGEST_WEB_HOST=0.0.0.0\n"
    "QQ_DIGEST_WEB_PORT={port}\n"
    "QQ_DIGEST_WEB_TOKEN={token}\n"
    "QQ_DIGEST_ONEBOT_ENABLED=1\n"
    "QQ_DIGEST_ONEBOT_TOKEN={onebot_token}\n"
    "QQ_DIGEST_NAPCAT_API_TOKEN=\n"
    "QQ_DIGEST_NAPCAT_API_URL=http://127.0.0.1:3001\n"
    "# 二维码路径留空：向导页点「一键接入」会自动找到 NapCat 的 cache\\qrcode.png 并写在这里\n"
    "QQ_DIGEST_NAPCAT_QR_PATH=\n"
    # AI 默认「不接」：首装不许假设本机有 Ollama 或 CodeBuddy CLI。以前这里写死了开发机上的
    # 本地模型名、本机推理端点和一个假钥匙，别人机器上没有 Ollama，摘要就静默失效、用户还
    # 以为开着 —— 这就是「AI 摘要默认落本地模型」那个 bug。现在交给「设置 → AI 摘要」引导
    # 用户自己选一家（预设里 DeepSeek 最便宜、智谱 GLM 免费、本机 Ollama 也在列表里，谁都
    # 不用改 .env）；没选之前只做本地规则摘要，不会假装有模型。模型名/端点留空表示还没选，
    # 选了之后由设置页把这三行写实。
    "QQ_DIGEST_LLM=0\n"
    "QQ_DIGEST_LLM_BACKEND=auto\n"
    "QQ_DIGEST_LLM_MODEL=\n"
    "QQ_DIGEST_LLM_ENDPOINT=\n"
    "QQ_DIGEST_LLM_API_KEY=\n"
    "QQ_DIGEST_LLM_TIMEOUT=120\n"
    "QQ_DIGEST_VISION=0\n"
    "# 「托管」：借用这个 QQ 号挂机收消息的那段时间。开始托管时自动退出电脑版 QQ，结束托管时自动开回来。\n"
    "QQ_DIGEST_HOSTING_QUIT_QQ=1\n"
    "QQ_DIGEST_HOSTING_RESTORE_QQ=1\n"
    "# 托盘「退出」时要不要顺手把电脑版 QQ 开回来。默认 0（安静退出）；\n"
    "# 托管自己关掉的 QQ 由上一行在结束托管时恢复，与这里无关。\n"
    "QQ_DIGEST_QUIT_RESTORE_QQ=0\n"
    "QQ_DIGEST_HOSTING_AUTO_ON_START=0\n"
    "QQ_DIGEST_AUTOSTART=0\n"
    # 订阅的群与群名别名。这两行必须预先存在：向导页「保存订阅」是就地改写它们
    # （qq_live_digest.config.update_env_file），老版本在这里手写了一份拷贝、缺键就报
    # "required .env keys missing"，导致全新安装第一次保存订阅必定失败。
    "QQ_DIGEST_GROUPS=\n"
    "QQ_DIGEST_GROUP_ALIASES=\n"
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


def _instance_mutex_name(root: Path) -> str:
    """Per-install-directory mutex name.

    The old name was the global "Local\\notice-hub-launcher", so **any** copy of the
    launcher blocked **every** other copy: with production's tray already running,
    double-clicking the beta copy silently exited (exit 0) without ever starting the
    beta service, while still opening the browser to production's own console URL —
    so the user believed it had started. See 04-task-B-mediums.md B-10.

    Keying on the install root lets production and a beta/second install run side by
    side, while still allowing two launches of the *same* directory to be de-duplicated
    (the original intent: one tray icon per install).

    The path is hashed rather than embedded verbatim: a Windows object name may not
    contain backslashes, and hashing keeps the name short and case-normalised.
    """
    try:
        resolved = str(root.resolve())
    except OSError:
        resolved = str(root)
    digest = hashlib.sha256(resolved.lower().encode("utf-8", "replace")).hexdigest()[:16]
    return "Local\\notice-hub-launcher-%s" % digest


def _acquire_single_instance(root: Path | None = None) -> bool:
    """Windows 命名互斥体。已经在运行就返回 False，避免同一安装目录出现两个托盘图标。

    互斥体名按安装目录区分（见 _instance_mutex_name），所以生产与 beta/第二个安装
    可以同时运行；同名同目录才判重复。

    句柄存在模块级变量里，进程活着就一直持有，退出时由系统释放。
    """
    global _INSTANCE_MUTEX
    try:
        import ctypes

        if root is None:
            root = Path(sys.executable).resolve().parent
        kernel32 = ctypes.windll.kernel32
        _INSTANCE_MUTEX = kernel32.CreateMutexW(None, False, _instance_mutex_name(root))
        return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return True  # 判断不了就别拦，宁可多开也别打不开


def _quit_request_path(root: Path) -> Path:
    return Path(root) / QUIT_REQUEST_NAME


def _other_instance_running(root: Path) -> bool:
    """只探测、不持有：这个安装目录里是不是已经有托盘在跑。"""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, _instance_mutex_name(root))
        running = kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
        if handle:
            kernel32.CloseHandle(handle)
        return bool(running)
    except Exception:
        return False  # 判断不了就当没在跑：请求文件照写，退了更好，没退也不误报


def _request_quit(root: Path, wait_seconds: float = 40.0) -> int:
    """`QQ-Notice-Hub.exe --quit`：让这份安装里正跑着的托盘退出。

    等价于右键托盘 →「退出」，但可以从命令行/脚本触发 —— 自动化验收托盘退出
    只能靠它（托盘点不动，进程也不能靠 taskkill 证明退出流程是对的）。返回 0
    表示「确认已退出」，1 表示「本来就没在跑」或「等了 wait_seconds 还没退」。
    """
    if not _other_instance_running(root):
        _log_launcher(root, "--quit：这个目录没有正在运行的实例")
        return 1
    try:
        _quit_request_path(root).write_text(str(os.getpid()), encoding="utf-8")
    except OSError as exc:
        _log_launcher(root, "--quit：写退出请求失败：%s" % exc)
        return 1
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if not _other_instance_running(root):
            _log_launcher(root, "--quit：实例已退出")
            return 0
        time.sleep(0.5)
    _log_launcher(root, "--quit：等待 %d 秒仍未退出" % int(wait_seconds))
    return 1


def _log_launcher(root: Path, message: str) -> None:
    """给「用户看不见反馈」的路径留一条能查的线索。

    启动器是 console=False 打包的：健康检查失败时既没有托盘也没有控制台窗口，
    双击后用户什么都看不到（bug-hunt-packaging C-08）。写进 logs\\launcher.log，
    用户下次回来问「为什么双击没反应」，这个文件就是答案。
    """
    try:
        log_file = root / "logs" / "launcher.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as stream:
            stream.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except OSError:
        pass


def _notify_user(root: Path, message: str) -> None:
    """没有托盘时也要让用户知道出事了：响一声 + 弹窗，失败则退回日志。

    启动器是 console=False 打包的，失败时既没有托盘也没有控制台窗口。
    系统通知可能被「专注助手/勿扰」静音，所以弹窗是保底；弹窗也弹不出来
    （例如远程会话）就只剩日志了。
    """
    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.MessageBeep(0x40)  # MB_ICONEXCLAMATION
        user32.MessageBoxW(None, message, "群务台 启动失败", 0x10)  # MB_ICONERROR
        return
    except Exception:
        pass
    _log_launcher(root, "弹窗也弹不出来，请看 logs\\launcher.log：" + message)


def _notify_info(root: Path, message: str, title: str = "群务台 有新版本") -> None:
    """好消息用信息框（0x40 图标），别跟「启动失败」的红色错误框混在一起。"""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, title, 0x40)
        return
    except Exception:
        pass
    _log_launcher(root, "信息框弹不出来：\n" + message)


# ---- 版本更新 ----
UPDATES_DIR_NAME = "updates"
UPDATE_STATE_NAME = "state.json"


def _data_dir_for(root: Path, settings: object) -> Path:
    """用户数据目录：跟着 .env 的 QQ_DIGEST_DATA_DIR 走，缺省是安装目录下的 data。"""
    try:
        configured = getattr(settings, "data_dir", None)
        if configured:
            return Path(configured)
    except Exception:
        pass
    return Path(root) / "data"


def _applying_flag_path(data_dir: Path) -> Path:
    try:
        from qq_live_digest.updater import APPLYING_FLAG

        name = APPLYING_FLAG
    except Exception:
        name = "applying.flag"
    return Path(data_dir) / UPDATES_DIR_NAME / name


def _update_state_path(data_dir: Path) -> Path:
    return Path(data_dir) / UPDATES_DIR_NAME / UPDATE_STATE_NAME


def _read_update_state(data_dir: Path) -> dict:
    try:
        path = _update_state_path(data_dir)
        if not path.is_file():
            return {}
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _write_update_state(data_dir: Path, state: dict) -> None:
    try:
        path = _update_state_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _update_channel(env_file: Path) -> str | None:
    """.env 里的 QQ_DIGEST_UPDATE_CHANNEL：stable/beta/off 显式指定，其它值交给 updater 默认。"""
    value = _read_env_value(env_file, "QQ_DIGEST_UPDATE_CHANNEL").strip().lower()
    return value if value in {"stable", "beta", "off"} else None


def _update_check_enabled(env_file: Path) -> bool:
    return _update_channel(env_file) != "off"


def _update_check_worker(root: Path, data_dir: Path, channel: str | None = None) -> None:
    """后台查一次更新：写 state.json，有新版本且没通知过就弹一次信息框。

    跑在守护线程里，而且自己把异常全吃掉：检查更新失败绝不能影响服务启动。
    """
    try:
        from qq_live_digest import updater

        info = updater.check(channel=channel)
        state = _read_update_state(data_dir)
        state["last_check"] = str(info.get("checked_at") or time.strftime("%Y-%m-%d %H:%M:%S"))
        latest = info.get("latest") if isinstance(info.get("latest"), dict) else {}
        state["last_result"] = {
            "ok": bool(info.get("ok")),
            "error": str(info.get("error") or ""),
            "latest": str(latest.get("tag") or ""),
            "has_update": bool(info.get("has_update")),
        }
        _write_update_state(data_dir, state)

        tag = str(latest.get("tag") or "")
        if not info.get("ok") or not info.get("has_update") or not tag:
            return
        if tag == str(state.get("notified_tag") or ""):
            return  # 弹过一次就够了，别每次启动都打扰
        name = str(latest.get("name") or "").strip()
        _notify_info(
            root,
            "发现新版本 %s：%s\n\n打开网页 →「设置 → 版本更新」可以一键更新" % (tag, name),
            title="群务台 有新版本",
        )
        state["notified_tag"] = tag
        _write_update_state(data_dir, state)
    except Exception:
        pass


def _read_meta_token(env_file: Path, root: Path) -> str:
    """B-12 兜底：.env 没写 token 时，webapp 会把生成的令牌存进 sqlite meta。

    启动器只读 .env 会拿到空 token ⇒ 打开的控制台 URL 缺 token（401 空白页）。
    这里用标准库 sqlite3 只读回退一次 meta 里的 web_token（无新依赖，失败即空串）。
    """
    try:
        data_dir_text = _read_env_value(env_file, "QQ_DIGEST_DATA_DIR").strip()
        if data_dir_text and Path(data_dir_text).is_absolute():
            data_dir = Path(data_dir_text)
        else:
            data_dir = root / "data"
        db_path = data_dir / "digest.sqlite3"
        if not db_path.is_file():
            return ""
        connection = sqlite3.connect("file:%s?mode=ro" % db_path.as_posix(), uri=True, timeout=2)
        try:
            row = connection.execute("SELECT value FROM meta WHERE key='web_token'").fetchone()
        finally:
            connection.close()
        return row[0] if row else ""
    except Exception:
        return ""


def _console_url(env_file: Path, port: int) -> str:
    root = Path(env_file).parent
    token = _read_env_value(env_file, "QQ_DIGEST_WEB_TOKEN") or _read_meta_token(env_file, root)
    return "http://127.0.0.1:%d/?token=%s" % (port, urllib.parse.quote(token, safe=""))


def _launch_service(root: Path, env_file: Path, port: int | None = None) -> subprocess.Popen:
    # --env 是 main.py 的顶层参数，必须放在子命令 run 之前，否则 argparse 报
    # "unrecognized arguments: --env ..."。CREATE_NO_WINDOW 避免再弹一个控制台窗口，
    # 也避免用户误关那个窗口把服务一起关掉。
    child_env = os.environ.copy()
    if port is not None:
        # 仅给本次子进程覆盖端口，绝不改用户 .env。
        child_env["QQ_DIGEST_WEB_PORT"] = str(port)
    return subprocess.Popen(
        [str(root / SERVICE_EXE), "--env", str(env_file), "run"],
        cwd=root,
        env=child_env,
        creationflags=CREATE_NO_WINDOW,
    )


def _port_probe(port: int) -> str:
    """Return ``qq-tasks``, ``occupied`` or ``free`` for a candidate port."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port, timeout=0.6) as response:
            if response.status != 200:
                return "occupied"
            try:
                payload = json.loads(response.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return "occupied"
            if payload == {"ok": True, "service": "qq-tasks"}:
                return "qq-tasks"
            return "occupied"
    except Exception:
        pass
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        return "occupied"
    finally:
        probe.close()
    return "free"


def _port_has_health_response(port: int) -> bool:
    """Compatibility helper: true only for a real qq-tasks health response."""
    return _port_probe(port) == "qq-tasks"


def _find_available_port(start: int, limit: int = 100) -> int | None:
    for candidate in range(start + 1, start + limit + 1):
        if _port_probe(candidate) == "free":
            return candidate
    return None


def _wait_health(port: int, process: subprocess.Popen, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if _port_probe(port) == "qq-tasks":
                if process.poll() is not None:
                    return False
                time.sleep(min(0.6, max(0.0, deadline - time.monotonic())))
                if process.poll() is not None:
                    return False
                return _port_probe(port) == "qq-tasks" and process.poll() is None
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
        quit_request = _quit_request_path(self.root)
        if quit_request.exists():
            # 程序化的「退出」（`--quit` / 自动化验收）。先删文件再退：留着的话
            # 下次启动会被立刻顶掉。
            try:
                quit_request.unlink()
            except OSError:
                pass
            self._log_watchdog("收到退出请求，正在退出")
            self.on_quit()
            return False
        flag = _applying_flag_path(_data_dir_for(self.root, self.settings))
        try:
            flag_stat = flag.stat()
        except FileNotFoundError:
            flag_stat = None
        except OSError as exc:
            self._log_watchdog("无法检查更新标记，暂不拉起服务：%s" % exc)
            self._stop_watchdog()
            return False

        if flag_stat is not None:
            try:
                marker = next(
                    (line[3:].strip() for line in flag.read_text(encoding="utf-8").splitlines() if line.startswith("at=")),
                    "",
                )
                created_at = time.mktime(time.strptime(marker, "%Y-%m-%d %H:%M:%S")) if marker else flag_stat.st_mtime
            except (OSError, ValueError, OverflowError):
                created_at = flag_stat.st_mtime
            age = time.time() - created_at
            if age > APPLYING_FLAG_MAX_AGE_SECONDS:
                try:
                    flag.unlink()
                except OSError as exc:
                    self._log_watchdog("更新标记已过期但无法清除，暂不拉起服务：%s" % exc)
                    self._stop_watchdog()
                    return False
                self._log_watchdog("更新标记超过 %d 秒，已清除陈旧标记" % APPLYING_FLAG_MAX_AGE_SECONDS)
            else:
                # 正在覆盖安装：这时候服务本来就该退出，拉回来只会干扰解压覆盖，
                # 所以看门狗自己停掉，等新版本的启动器接管。
                self._log_watchdog("正在覆盖安装更新，看门狗停止拉起服务")
                self._stop_watchdog()
                return False
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
            # 到点没退干净就硬退。托盘「退出」是用户唯一的出口：一旦里面哪步卡住，
            # 图标就黏在托盘点不掉，只能去任务管理器强杀（用户 2026-10-11 就是这么撞上的）。
            threading.Timer(QUIT_HARD_DEADLINE_SECONDS, self._force_quit).start()
            self._quit_steps()
            os._exit(0)

        self.busy = True
        threading.Thread(target=shutdown, daemon=True).start()

    def _stop_service_child(self) -> None:
        """退出前一定把服务子进程带走，否则端口还占着、下次双击直接报「端口被占用」。"""
        try:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
        except Exception:
            self._log_watchdog("退出时结束服务子进程出错（继续退出）")

    def _quit_steps(self) -> None:
        """退出流程：每一步都各自兜住异常，任何一步炸了都不许拦住后面的退出。"""
        try:
            self._stop_watchdog()
        except Exception:
            pass
        try:
            if hosting.hosting_status(self.settings).get("hosting_active"):
                hosting.stop(self.settings)
        except Exception:
            # 结束托管失败也得退：留着托盘不让用户走，比「电脑版 QQ 没自动恢复」更糟。
            self._log_watchdog("退出时结束托管出错（继续退出）")
        # 退出时开回 QQ 是 opt-in：默认只安静退出，不替用户多开一个进程。
        # 用户 2026-10-12：「托盘右键退出会自动开 qq，我希望是设置项里的选项，默认关闭」。
        if hosting.restore_qq_on_quit(self.settings):
            try:
                hosting.start_user_qq(self.settings)
            except Exception:
                self._log_watchdog("退出时恢复电脑版 QQ 出错（继续退出）")
        try:
            if self.icon is not None:
                self.icon.stop()
        except Exception:
            self._log_watchdog("退出时关托盘图标出错（继续退出）")
        self._stop_service_child()

    def _force_quit(self) -> None:
        self._log_watchdog("退出超时（>%ds），强制结束" % int(QUIT_HARD_DEADLINE_SECONDS))
        self._stop_service_child()
        os._exit(0)

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
    if "--quit" in sys.argv[1:]:
        # 让本目录里正跑着的托盘退出；日志与 data 目录要先生成，_request_quit 才写得进去。
        return _request_quit(root)
    env_file = root / ".env"
    first_run = not env_file.exists()
    _ensure_env_file(root, env_file)
    if first_run:
        _create_desktop_shortcut(root)
    settings = Settings.from_env(env_file=env_file)
    port = int(getattr(settings, "web_port", 8766) or 8766)
    url = _console_url(env_file, port)
    if not _acquire_single_instance(root):
        # 同一个安装目录已经在跑：把已经开着的那份控制台再打开一次，然后安静退出。
        # 注意这里打开的是**本安装目录自己的** .env 拼出来的 url，所以另一个安装目录
        # （生产 / beta）不会走到这个分支——它们的互斥体名不同。
        webbrowser.open(url)
        return 0
    selected_port = port
    port_state = _port_probe(port)
    if port_state != "free":
        selected_port = _find_available_port(port)
        if selected_port is None:
            reason = (
                "端口 %d 已被占用，附近没有可用端口。\n"
                "请关闭占用端口的程序后，再启动群务台；用户 .env 没有被修改。"
            ) % port
            _log_launcher(root, "启动前探测发现端口 %d 已被占用，未找到可用端口" % port)
            _notify_user(root, reason)
            return 1
        message = "端口 %d 被占用，本次改用 %d。控制台地址：http://127.0.0.1:%d/" % (
            port,
            selected_port,
            selected_port,
        )
        # 启动路径不能调用模态 MessageBox：用户不点确定时会挡住服务进程。
        # 日志保留完整事实；服务启动后由托盘状态承载后续反馈。
        _log_launcher(root, message)
    url = _console_url(env_file, selected_port)
    try:
        # 上一次退出中途崩了可能留下请求文件；这次刚起来就被它顶掉会很莫名其妙。
        _quit_request_path(root).unlink()
    except OSError:
        pass
    process = _launch_service(root, env_file, selected_port)
    if not _wait_health(selected_port, process):
        exit_code = process.poll()
        process.terminate()
        # terminate 只是"发出"终止请求；不 wait 的话服务可能还占着端口。
        try:
            process.wait(timeout=10)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=5)
            except Exception:
                pass
        # C-08: console=False + 无托盘 ⇒ 双击后用户什么都没看见。至少告诉他一声。
        reason = (
            "服务没能启动（端口 %d，退出码 %s）。\n\n"
            "常见原因：\n"
            "· 端口 %d 已被别的程序占用\n"
            "· 安装目录里的 qq-live-digest.exe 缺失或损坏（建议重新解压一次）\n\n"
            "详细信息见 logs\\launcher.log。"
        ) % (selected_port, exit_code, selected_port)
        _log_launcher(root, "健康检查失败：port=%d exit=%s" % (port, exit_code))
        _notify_user(root, reason)
        return 1
    webbrowser.open(url)
    if _update_check_enabled(env_file):
        # 后台查一次更新（守护线程、不阻塞启动）；QQ_DIGEST_UPDATE_CHANNEL=off 时整块跳过。
        threading.Thread(
            target=_update_check_worker,
            args=(root, _data_dir_for(root, settings), _update_channel(env_file)),
            name="notice-hub-update-check",
            daemon=True,
        ).start()
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
