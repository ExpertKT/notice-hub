"""notice-hub 启动器：常驻托盘宿主。

- 首次运行生成 `.env`（随机 token）并创建桌面快捷方式，之后复用。
- 拉起 `qq-live-digest.exe --env <env> run` 作为子进程（无控制台窗口）。
- 托盘菜单：开始托管 / 结束托管 / 打开控制台 / 退出（浮窗启用时才多一项「显示/隐藏浮窗」）。
- **从托盘退出 = 结束托管（停 NapCat、按设置恢复电脑版 QQ）+ 关服务子进程 + 关程序**。

托管本身实现在 `noticehub/hosting.py`（与网页设置共用同一份逻辑，避免两套真相源）。
"""

from __future__ import annotations

import os
import re
import json
import ctypes
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

from noticehub import host_actions, hosting
from noticehub.config import Settings, parse_env_file
try:  # 浮球模块不在发布范围内：缺函数时托盘仍要能起来
    from noticehub.floatball_host import ensure_dpi_aware
except Exception:  # pragma: no cover - 只影响 DPI 校准，不影响主程序
    ensure_dpi_aware = None  # type: ignore[assignment]
try:  # 浮球模块暂未随本版发布，缺文件时托盘仍要能起来
    from noticehub.floatball_window import FloatBallWindow
except Exception:  # pragma: no cover - 只影响浮球，不影响主程序
    FloatBallWindow = None  # type: ignore[assignment]

FLOATBALL_ENABLED = False  # 浮球暂停（面板尺寸受 DPI 影响未收敛），见 floatball_window.py
BROWSER_SUPPRESSED = False


def _open_browser(url: str) -> bool:
    if BROWSER_SUPPRESSED:
        return False
    return bool(webbrowser.open(url))


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SERVICE_EXE = "qq-live-digest.exe"
LAUNCHER_EXE = "QQ-Notice-Hub.exe"
SHORTCUT_NAME = "notice-hub.lnk"
WATCHDOG_POLL_SECONDS = 2.0
WATCHDOG_BACKOFF_SECONDS = (5.0, 15.0, 45.0)
APPLYING_FLAG_MAX_AGE_SECONDS = 600
# 托盘「退出」的硬兜底：托管要动 NapCat 与电脑版 QQ，慢也得有个上限。
QUIT_HARD_DEADLINE_SECONDS = 30.0
# 退出时「托管收尾」这一步自己的预算：停 NapCat、按设置开回 QQ。超了就放弃，
# 不让它把整个退出拖到硬兜底那 30 秒（用户 2026-10-31 06:19 等了 40 秒还被强杀）。
QUIT_TEARDOWN_BUDGET_SECONDS = 5.0
# 托盘「忙」的上限：hosting.start/stop 要等 NapCat 起来，可能长时间不返回；超过这个
# 时间就自愈复位，否则菜单会一直全灰、用户连「重启并更新」都点不了（2026-10-24 现场）。
BUSY_TIMEOUT_SECONDS = 90.0
# `--quit` 与托盘之间的信号文件：托盘没有可投递的窗口消息，看门狗每
# WATCHDOG_POLL_SECONDS 查一次这个文件，见到就执行退出（见 watchdog_tick）。
QUIT_REQUEST_NAME = "launcher.quit"

ENV_TEMPLATE = (
    "QQ_DIGEST_WEB=1\n"
    # 必须绑 0.0.0.0（与 noticehub/config.py 的默认值一致），不能写 127.0.0.1：
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
    # （noticehub.config.update_env_file），老版本在这里手写了一份拷贝、缺键就报
    # "required .env keys missing"，导致全新安装第一次保存订阅必定失败。
    "QQ_DIGEST_GROUPS=\n"
    "QQ_DIGEST_GROUP_ALIASES=\n"
    "QQ_DIGEST_DATA_DIR={data}\n"
    "QQ_DIGEST_LOG_DIR={logs}\n"
)


def _read_env_value(env_file: Path, key: str) -> str:
    # 与主程序共用同一个 .env 解析（noticehub.config.parse_env_file），避免两套规则漂移
    return parse_env_file(env_file).get(key, "")


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


def _icon_names(channel: str | None) -> tuple[str, ...]:
    if channel == "beta":
        return ("icon-256-beta.png", "icon-beta.ico", "icon-256.png", "icon.ico")
    return ("icon-256.png", "icon.ico")


def _shortcut_icon_location(root: Path, channel: str | None) -> str:
    target = Path(root) / LAUNCHER_EXE
    beta_icon = Path(root) / "assets" / "icon-beta.ico"
    if channel == "beta" and beta_icon.is_file():
        return str(beta_icon.resolve())
    return str(target) + ",0"


def _create_desktop_shortcut(root: Path, channel: str | None = None) -> str | None:
    """首次运行时创建桌面快捷方式。目标写**绝对路径**，所以解压到哪都成立。"""
    target = root / LAUNCHER_EXE
    if not target.is_file():
        return None
    script = (
        "$desktop = [Environment]::GetFolderPath('Desktop'); "
        "$shortcut = Join-Path $desktop $env:NH_LNK; "
        "if (Test-Path -LiteralPath $shortcut) { exit 0 }; "
        "$ws = New-Object -ComObject WScript.Shell; "
        "$lnk = $ws.CreateShortcut($shortcut); "
        "$lnk.TargetPath = $env:NH_TARGET; "
        "$lnk.WorkingDirectory = $env:NH_ROOT; "
        "$lnk.IconLocation = $env:NH_ICON; "
        "$lnk.Description = 'notice-hub'; "
        "$lnk.Save()"
    )
    env = os.environ.copy()
    env.update({"NH_LNK": SHORTCUT_NAME, "NH_TARGET": str(target), "NH_ROOT": str(root), "NH_ICON": _shortcut_icon_location(root, channel)})
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


LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 2  # launcher.log + .1 + .2


def _rotate_launcher_log(log_file: Path) -> None:
    """按大小轮转：超过 2 MB 就 launcher.log → .1 → .2，最旧的 .2 删掉。

    手写而不是 RotatingFileHandler：launcher 是裸脚本，尽量少依赖标准库之外的东西
    （这里连 logging 模块都不用）。只有 launcher 自己写这个文件，不用加锁。
    任何一步失败都只是「这次不轮转」，不能让日志把启动搞崩。
    """
    try:
        if log_file.stat().st_size < LOG_MAX_BYTES:
            return
    except OSError:
        return
    oldest = log_file.with_name(log_file.name + ".%d" % LOG_BACKUPS)
    first = log_file.with_name(log_file.name + ".1")
    try:
        oldest.unlink()
    except OSError:
        pass
    for index in range(LOG_BACKUPS - 1, 0, -1):
        source = log_file.with_name(log_file.name + ".%d" % index)
        target = log_file.with_name(log_file.name + ".%d" % (index + 1))
        try:
            source.replace(target)
        except OSError:
            pass
    try:
        log_file.replace(first)
    except OSError:
        pass


def _log_launcher(root: Path, message: str) -> None:
    """给「用户看不见反馈」的路径留一条能查的线索。

    启动器是 console=False 打包的：健康检查失败时既没有托盘也没有控制台窗口，
    双击后用户什么都看不到（bug-hunt-packaging C-08）。写进 logs\\launcher.log，
    用户下次回来问「为什么双击没反应」，这个文件就是答案。按 2 MB 轮转，防止无限增长。
    """
    try:
        log_file = root / "logs" / "launcher.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        _rotate_launcher_log(log_file)
        with log_file.open("a", encoding="utf-8") as stream:
            stream.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except OSError:
        pass


def _console_window_open() -> bool:
    """Every launch used to open another console tab; one visible page is enough."""
    if os.name != "nt":
        return False
    found: list[int] = []
    user32 = ctypes.windll.user32

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_ssize_t)
    def enum_window(hwnd, _param):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        if any(word in title.value for word in ("\u7fa4\u52a1\u53f0", "\u7fa4\u6d88\u606f\u5f85\u529e")):
            found.append(1)
        return True

    user32.EnumWindows(enum_window, 0)
    return bool(found)


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
        from noticehub.updater import APPLYING_FLAG

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


# launcher 自己负责的 state.json 字段。webapp 也往同一个文件里写（下载进度、
# applying 标志等），所以写回时只能动这几个键，别人的原样留着。
LAUNCHER_STATE_KEYS = ("last_check", "last_result", "notified_tag")
_UPDATE_STATE_LOCK = threading.Lock()


def _write_update_state(data_dir: Path, state: dict) -> None:
    """合并写 state.json：写前重读最新，只覆盖 launcher 自己的字段，原子替换。

    以前和 webapp 都是「读全量 → 改 → 写全量」，谁后写谁把对方刚写进去的字段抹掉。
    两个进程没法共用一把锁，但重读-合并-原子写把窗口压到了最小。
    """
    if not isinstance(state, dict) or not state:
        return
    path = _update_state_path(data_dir)
    with _UPDATE_STATE_LOCK:
        merged = _read_update_state(data_dir)
        for key in LAUNCHER_STATE_KEYS:
            if key in state:
                merged[key] = state[key]
        temporary = path.with_name("%s.%d-%d.tmp" % (path.name, os.getpid(), threading.get_ident()))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, path)
        except OSError as exc:
            # 失败不静默：用户「为什么没提示新版本」时，日志里得有一条。
            _log_launcher(data_dir.parent, "更新状态写入失败：%s（%s）" % (path, exc))
            try:
                temporary.unlink()
            except OSError:
                pass


def _update_channel(env_file: Path) -> str | None:
    """.env 里的 QQ_DIGEST_UPDATE_CHANNEL：stable/beta/off 显式指定，其它值交给 updater 默认。"""
    value = _read_env_value(env_file, "QQ_DIGEST_UPDATE_CHANNEL").strip().lower()
    return value if value in {"stable", "beta", "off"} else None


def _update_check_enabled(env_file: Path) -> bool:
    return _update_channel(env_file) != "off"


# ---- Windows 防火墙自动放行 ----
# 手机扫码配对、日历订阅都要从局域网连进来；Windows 防火墙默认会为一个陌生程序
# 弹「允许访问」对话框。用户点了「取消」、或者压根没看见，手机就永远连不上，
# 而症状只是「网络错误」。这里启动时只读检测一次，缺规则就弹一次 UAC 交给
# netsh 自己加，用户不用去翻控制面板。
FIREWALL_STATE_NAME = "launcher-firewall.json"
# 设置页「修复防火墙」由 webapp 写这个空文件；看门狗见到就删掉并强制重查一次。
FIREWALL_REQUEST_NAME = "firewall.request"
FIREWALL_RESULT_NAME = "firewall.result"
# 同一台机器 24 小时内最多弹一次 UAC：用户拒绝过也是他的决定，别每次启动都骚扰。
FIREWALL_RETRY_SECONDS = 24 * 60 * 60
FIREWALL_LOOKUP_TIMEOUT = 3.0


def _firewall_rule_command(port: int) -> str:
    # 与 webapp._firewall_rule_command 同名同格式：设置页「复制命令」手工执行与自动添加
    # 产生同一条规则，重复执行幂等。命令生成统一在 host_actions。
    return host_actions.firewall_allow_command(port)


def _firewall_request_path(data_dir: Path) -> Path:
    return Path(data_dir) / FIREWALL_REQUEST_NAME


def _firewall_result_path(data_dir: Path) -> Path:
    return Path(data_dir) / FIREWALL_RESULT_NAME


def _firewall_allows(port: int, data_dir: Path | None = None) -> dict:
    """只读查本机有没有放行该 TCP 端口入站的规则。查不到时 checked=False，别据此弹 UAC。

    查询与解析统一在 noticehub.host_actions（启动器不能 import webapp，它依赖 Flask）。
    缓存也统一在那里（60 秒，键 host_actions.FIREWALL_CACHE_KEY）：启动器是独立进程，
    启动那一次缓存本来就是空的、照样实跑 netsh；缓存只省掉同一进程内 60 秒里的重复探测
    （每次 netsh 枚举约 0.8 秒）。用户明确要「立刻重查」时由 ensure_firewall_rule(force=True) 清缓存。

    ``data_dir`` 用于在非提权 netsh 读不到时回退到提权探测写下的快照
    （``data/firewall.rules.txt``）——不传就只能靠直接调用 netsh。
    """
    return host_actions.firewall_allows(port, data_dir)


def _firewall_state_path(data_dir: Path) -> Path:
    return Path(data_dir) / FIREWALL_STATE_NAME


def _read_firewall_state(data_dir: Path) -> dict:
    try:
        path = _firewall_state_path(data_dir)
        if not path.is_file():
            return {}
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _write_firewall_state(data_dir: Path, state: dict) -> None:
    path = _firewall_state_path(data_dir)
    temporary = path.with_name("%s.%d-%d.tmp" % (path.name, os.getpid(), threading.get_ident()))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        _log_launcher(Path(data_dir).parent, "防火墙状态写入失败：%s（%s）" % (path, exc))
        try:
            temporary.unlink()
        except OSError:
            pass


def _clear_firewall_state(data_dir: Path) -> None:
    try:
        _firewall_state_path(data_dir).unlink()
    except OSError:
        pass


def _request_firewall_rule(port: int, command: str) -> str | None:
    """弹一次 UAC 让系统自己加规则；返回 None 表示已发起，否则返回失败原因。

    提权统一走 host_actions.run_elevated（整条 netsh 命令原样传下去）。
    ``command`` 只用于展示/日志；真正执行的是「先删同名旧规则再加新规则」的替换命令——
    旧版本加的规则不限 profile，单独 add 不会覆盖它，必须显式删。
    ``ok`` 只说明「已拉起 netsh」，加没加成功由下次启动的只读检测确认；
    不等待 netsh 结束，否则托盘会卡在 UAC 上。
    """
    replace_command = host_actions.firewall_replace_command(port)
    state, message = host_actions.run_elevated(replace_command, reason="自动放行 TCP %d 端口" % int(port))
    return None if state == "ok" else message


def ensure_firewall_rule(root: Path, data_dir: Path, port: int, force: bool = False, now: float | None = None) -> dict:
    """检测入站规则，缺了就弹一次 UAC 自动添加。绝不抛异常、绝不重复弹。

    去重规则：最近 FIREWALL_RETRY_SECONDS 内尝试过（同意或拒绝都算）就不再弹，
    状态记在 data/launcher-firewall.json。`force=True` 来自设置页「修复防火墙」，
    跳过冷却立即重试。规则已在时清掉状态，方便规则被删后立刻再提示。
    """
    result: dict = {"checked": False, "allowed": False, "prompted": False, "port": int(port or 0), "error": ""}
    try:
        port_number = int(port or 0)
    except (TypeError, ValueError):
        return result
    if not port_number:
        return result
    if force:
        # 「修复防火墙」是用户明确要求立刻重查：只有这里需要绕过 60 秒缓存。
        host_actions.reset_firewall_cache()
    try:
        probe = _firewall_allows(port_number, data_dir)
    except Exception as exc:
        probe = {"checked": False, "allowed": False, "command": _firewall_rule_command(port_number)}
        result["error"] = str(exc)
    result["checked"] = bool(probe.get("checked"))
    result["allowed"] = bool(probe.get("allowed"))
    result["command"] = probe.get("command") or _firewall_rule_command(port_number)
    if not result["checked"]:
        # 查不到（netsh 失败/非 Windows）就别弹：不知道缺不缺，弹了只是骚扰。
        return result
    if result["allowed"]:
        _clear_firewall_state(data_dir)
        return result
    moment = time.time() if now is None else float(now)
    state = _read_firewall_state(data_dir)
    try:
        last_attempt = float(state.get("last_attempt") or 0)
    except (TypeError, ValueError):
        last_attempt = 0.0
    if not force and moment - last_attempt < FIREWALL_RETRY_SECONDS:
        result["error"] = "冷却中，%d 小时内不重复提示" % int(FIREWALL_RETRY_SECONDS // 3600)
        return result
    # 先记时间再弹：万一进程正好在这时被杀，下次启动也不会又弹一次。
    _write_firewall_state(data_dir, {"last_attempt": moment, "port": port_number, "result": "requested"})
    error = _request_firewall_rule(port_number, result["command"])
    if error:
        result["error"] = error
        _write_firewall_state(data_dir, {"last_attempt": moment, "port": port_number, "result": "failed", "error": error[:200]})
        _log_launcher(root, "自动添加防火墙规则失败：%s" % error)
    else:
        result["prompted"] = True
        _log_launcher(root, "已请求 UAC 自动放行 TCP %d 端口（用户确认后由 netsh 添加）" % port_number)
    return result


def _update_check_worker(root: Path, data_dir: Path, channel: str | None = None) -> None:
    """后台查一次更新：写 state.json，有新版本且没通知过就弹一次信息框。

    跑在守护线程里，而且自己把异常全吃掉：检查更新失败绝不能影响服务启动。
    """
    try:
        from noticehub import updater

        info = updater.check(channel=channel)
        latest = info.get("latest") if isinstance(info.get("latest"), dict) else {}
        _write_update_state(
            data_dir,
            {
                "last_check": str(info.get("checked_at") or time.strftime("%Y-%m-%d %H:%M:%S")),
                "last_result": {
                    "ok": bool(info.get("ok")),
                    "error": str(info.get("error") or ""),
                    "latest": str(latest.get("tag") or ""),
                    "has_update": bool(info.get("has_update")),
                },
            },
        )

        tag = str(latest.get("tag") or "")
        if not info.get("ok") or not info.get("has_update") or not tag:
            return
        if tag == str(_read_update_state(data_dir).get("notified_tag") or ""):
            return  # 弹过一次就够了，别每次启动都打扰
        name = str(latest.get("name") or "").strip()
        _notify_info(
            root,
            "发现新版本 %s：%s\n\n打开网页 →「设置 → 版本更新」可以一键更新" % (tag, name),
            title="群务台 有新版本",
        )
        _write_update_state(data_dir, {"notified_tag": tag})
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
            if isinstance(payload, dict) and payload.get("ok") is True and str(payload.get("service") or "") == "qq-tasks":
                # 只认这两个字段，**不要**用 == 整字典比：真实的 /api/health 还带
                # `channels`（webapp.py 的 `{"ok": True, "service": "qq-tasks", "channels": channels}`），
                # 整字典比会永远不相等 → _wait_health 必失败 → 启动器把自己刚拉起来的服务杀掉。
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


def _last_service_error_message(root: Path, limit: int = 60) -> str:
    """从服务自己的日志里抠出最后一条 ERROR，让失败框说人话而不是猜原因。"""
    log = root / "logs" / "qq-live-digest.log"
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except OSError:
        return ""
    for line in reversed(lines):
        if " ERROR " in line:
            # 形如 "2026-10-13 01:23:30,304 ERROR noticehub.service 缺少 AppID…"
            return line.split(" ERROR ", 1)[1].strip()[:300]
    return ""


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


def _icon_image(root: Path | None = None, channel: str | None = None) -> object | None:
    """托盘图标：优先读 exe 同级 `assets\\icon-256.png`（或 `icon.ico`）。

    解压目录里没有 assets（或图坏了、没装 Pillow）时，回退到下面这段内联绘制 ——
    **没有 assets 也绝不让托盘起不来**。
    """
    base = Path(root) if root is not None else Path(sys.executable).resolve().parent
    for name in _icon_names(channel):
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
    """托盘宿主：托管开关都走 `noticehub.hosting`。"""

    def __init__(self, root: Path, env_file: Path, settings: Settings, process: subprocess.Popen, url: str, port: int | None = None) -> None:
        self.root = root
        self.env_file = env_file
        self.settings = settings
        self.process = process
        self.url = url
        # 实际在用的端口（端口回退时跟 settings.web_port 不一致），防火墙必须放行这个。
        try:
            self.port = int(port or getattr(settings, "web_port", 0) or 0)
        except (TypeError, ValueError):
            self.port = 0
        self.icon = None
        self._busy = False
        self._busy_since = 0.0
        self.active = bool(hosting.hosting_status(settings).get("hosting_active"))
        self._watchdog_stop = threading.Event()
        self._watchdog_thread = None
        self._restart_lock = threading.Lock()
        self._restart_pending = False
        self.restart_failures = 0
        self.manual_restart_required = False
        self._cookie_read_busy = False
        self._clipboard_cookie_busy = False
        self._firewall_busy = False
        # 测试里的 Settings 替身可能没有 data_dir；退回安装目录下的 data，别让托盘整体起不来。
        self.floatball = FloatBallWindow(Path(getattr(settings, "data_dir", "") or (self.root / "data"))) if FloatBallWindow else None

    # ---- 忙碌标志 ----
    @property
    def busy(self) -> bool:
        return self._busy

    @busy.setter
    def busy(self, value: bool) -> None:
        # 所有置忙/复位都走这里，顺手记下时刻，is_busy() 才有据可查。
        if value and not self._busy:
            self._busy_since = time.monotonic()
        elif not value:
            self._busy_since = 0.0
        self._busy = bool(value)

    def is_busy(self) -> bool:
        """菜单是否该禁用。忙太久就自愈复位，避免 permanent 灰菜单把人锁死。"""
        if not self._busy:
            return False
        if self._busy_since and time.monotonic() - self._busy_since >= BUSY_TIMEOUT_SECONDS:
            self._log_watchdog("托管操作超过 %d 秒仍未返回，已恢复菜单" % int(BUSY_TIMEOUT_SECONDS))
            self.busy = False
            return False
        return True

    def _hosting_call(self, label: str, func) -> dict | None:
        """跑一个可能长阻塞的托管操作；超时就放弃等待，先把菜单还回去。"""
        box: dict = {}
        done = threading.Event()

        def work() -> None:
            try:
                box["value"] = func()
            except Exception as exc:  # 托管层理论上不抛；抛了也不能把托盘线程带走
                box["error"] = exc
            finally:
                done.set()

        threading.Thread(target=work, daemon=True).start()
        if not done.wait(BUSY_TIMEOUT_SECONDS):
            self._log_watchdog("%s超时（>%ds），已恢复菜单" % (label, int(BUSY_TIMEOUT_SECONDS)))
            self._notify("%s超时，已恢复菜单" % label)
            return None
        if "error" in box:
            raise box["error"]
        value = box.get("value")
        return value if isinstance(value, dict) else None

    # ---- 服务看门狗 ----
    def _log_watchdog(self, message: str) -> None:
        _log_launcher(self.root, message)

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

    # ---- 防火墙 ----
    def _firewall_check(self, force: bool = False) -> None:
        """检测并（必要时）弹一次 UAC。放后台线程跑：UAC 会一直等到用户点按钮。"""
        if self._firewall_busy:
            return
        self._firewall_busy = True
        try:
            ensure_firewall_rule(self.root, _data_dir_for(self.root, self.settings), self.port, force=force)
        except Exception as exc:  # 防火墙只是锦上添花，绝不能拖垮托盘
            self._log_watchdog("防火墙检查失败：%s" % exc)
        finally:
            self._firewall_busy = False

    def _grant_firewall(self, port: int) -> None:
        """执行页面转来的「放行端口」请求：提权跑 netsh，结果写 firewall.result。"""
        data_dir = _data_dir_for(self.root, self.settings)
        result_path = _firewall_result_path(data_dir)
        port_number = int(port or 0)

        def write_result(payload: dict) -> None:
            try:
                data_dir.mkdir(parents=True, exist_ok=True)
                tmp_path = result_path.with_name(f"{result_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
                tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                tmp_path.replace(result_path)
            except OSError as exc:
                self._log_watchdog("写防火墙结果失败：%s" % exc)

        # 先落一条 running：request 已被消费删掉、UAC 还没返回的这几秒，页面不能掉回 unknown。
        write_result(
            {
                "port": port_number,
                "ok": False,
                "state": "running",
                "message": "正在请求管理员权限，请看托盘弹窗",
                "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
        )
        self._firewall_busy = True
        try:
            self._notify("正在请求管理员权限以放行端口 %d…" % port_number)
            # 加规则和「把 netsh 的规则清单导出到快照」合成一次提权：非提权进程在某些
            # 策略下读不到 netsh 输出（请求的操作需要提升），以前就是因此永远判失败。
            state, message = host_actions.run_elevated(
                host_actions.firewall_allow_and_probe_command(port_number, data_dir),
                reason="放行 TCP %d 端口" % port_number,
                log_dir=data_dir / "logs",
            )
        except Exception as exc:  # noqa: BLE001 - 提权失败也要把原因写回去
            state, message = "failed", "执行失败：%s" % exc
        finally:
            self._firewall_busy = False
        ok = state == "ok"
        probe: dict = {"checked": False, "allowed": False}
        if ok:
            # ShellExecuteW 成功只代表命令起来了，规则不一定已生效；先真探到规则才算放行，
            # 否则 netsh 失败时页面会先绿一下（审查：防火墙绿得太早）。
            ok = False
            try:
                for _ in range(3):
                    time.sleep(2.0)
                    host_actions.reset_firewall_cache()
                    probe = host_actions.firewall_allows(port_number, data_dir)
                    if probe.get("allowed"):
                        ok = True
                        break
            except Exception as exc:  # noqa: BLE001 - 探测本身失败也算没确认，别让线程死掉
                self._log_watchdog("探测防火墙规则失败：%s" % exc)
            if not ok:
                state = "failed"
                if probe.get("checked"):
                    # 读得到规则清单、但里面没有这条：netsh 真的没执行成功。
                    message = "管理员权限已确认，但防火墙里还没看到这条放行规则（netsh 可能没执行成功）；稍后点「刷新状态」再看"
                else:
                    # 读不到规则清单（需要管理员才能确认），不能说成「规则没生效」。
                    message = (
                        "已请求管理员权限放行端口 %d，但本机探测读不到防火墙规则"
                        "（需要管理员才能确认）；稍后点「刷新状态」再看" % port_number
                    )
        write_result(
            {
                "port": port_number,
                "ok": ok,
                "state": state,
                "checked": bool(probe.get("checked")),
                "message": str(message or ""),
                "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
        )
        # 审计：事后要查得出这次是成功/失败/取消（只写 result 文件查不到）。
        host_actions.audit_host_action(
            "firewall_allow",
            {"port": port_number, "by": "launcher", "reason": "页面一键放行"},
            state,
            log_dir=data_dir / "logs",
        )
        self._log_watchdog("防火墙放行端口 %d：%s（%s）" % (port_number, state, message))
        if ok:
            self._notify("已请求管理员权限放行端口 %d" % port_number)
        elif state == "cancelled":
            self._notify("你取消了管理员权限，端口 %d 还没放行；可以再点一次" % port_number)
        else:
            self._notify("放行端口 %d 失败：%s" % (port_number, message))

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
        firewall_request = _firewall_request_path(_data_dir_for(self.root, self.settings))
        try:
            requested = firewall_request.exists()
        except OSError:
            requested = False
        if requested and not self._firewall_busy:
            # 页面「一键放行防火墙」写入请求文件（JSON 带端口）；托盘负责提权，服务进程不弹 UAC。
            # 正在执行时不消费请求文件，留给 2 秒后的下一次 tick，免得这次点击被吞掉。
            try:
                payload = json.loads(firewall_request.read_text(encoding="utf-8") or "{}")
            except (OSError, ValueError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            try:
                firewall_request.unlink()
            except OSError:
                pass
            try:
                requested_port = int(payload.get("port") or self.port or 0)
            except (TypeError, ValueError):
                requested_port = int(self.port or 0)
            self._log_watchdog("收到放行防火墙请求：TCP %d" % requested_port)
            threading.Thread(target=self._grant_firewall, args=(requested_port,), name="notice-hub-firewall", daemon=True).start()
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
            result = self._hosting_call("托管操作", lambda: hosting.start(self.settings))
            if result is None:
                return
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
            result = self._hosting_call("托管操作", lambda: hosting.stop(self.settings))
            if result is None:
                return
            self.active = False
            if not result.get("ok"):
                self._notify("结束托管有问题：" + str(result.get("error") or "未知错误"))
            else:
                self._notify("已结束托管，电脑版 QQ 已恢复。")
        finally:
            self.busy = False

    def on_start(self, icon: object = None, item: object = None) -> None:
        if self.is_busy():
            return
        threading.Thread(target=self._start, daemon=True).start()

    def on_stop(self, icon: object = None, item: object = None) -> None:
        if self.is_busy():
            return
        threading.Thread(target=self._stop, daemon=True).start()

    def on_open(self, icon: object = None, item: object = None) -> None:
        _open_browser(self.url)

    def _pending_update(self) -> dict | None:
        try:
            from noticehub import updater

            return updater.pending_update(_data_dir_for(self.root, self.settings))
        except Exception:
            return None

    def _restart_title(self, item: object = None) -> str:
        return "重启并更新" if self._pending_update() else "重启服务"

    def _cookie_read_url(self) -> str:
        port = int(getattr(self.settings, "web_port", 0) or 0)
        try:
            parsed = urllib.parse.urlsplit(self.url)
            port = int(parsed.port or port)
        except (ValueError, TypeError):
            pass
        return "http://127.0.0.1:%d/api/chaoxing/auto_cookie" % port

    def _read_chaoxing_cookie(self) -> None:
        self._cookie_read_busy = True
        try:
            token = _read_env_value(self.env_file, "QQ_DIGEST_WEB_TOKEN") or _read_meta_token(self.env_file, self.root)
            request = urllib.request.Request(
                self._cookie_read_url(),
                data=b"{}",
                headers={"Content-Type": "application/json", "X-Token": token},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=45) as response:
                    result = json.loads(response.read().decode("utf-8"))
            except (OSError, ValueError):
                self._notify("群务台服务没有响应，请确认它在运行")
                return
            if not isinstance(result, dict):
                self._notify("群务台服务没有响应，请确认它在运行")
                return
            if result.get("ok") is True:
                self._notify("已读取学习通 Cookie，并自动启用学习通")
            elif result.get("category") == "browser_locked":
                self._notify("浏览器还在运行：请完全退出 Edge/Chrome（含托盘图标和后台进程）后再点一次")
            else:
                self._notify("自动读取学习通 Cookie 失败：%s" % str(result.get("error") or "请稍后重试"))
        finally:
            self._cookie_read_busy = False

    def on_read_chaoxing_cookie(self, icon: object = None, item: object = None) -> None:
        if self.is_busy() or self._cookie_read_busy:
            return
        threading.Thread(target=self._read_chaoxing_cookie, daemon=True).start()

    def _clipboard_text(self) -> str | None:
        """Read CF_UNICODETEXT through Win32 without extra bundled GUI dependencies."""
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        # 64 位下默认 restype 是 c_int，句柄会被截断成 32 位后读非法内存；必须显式声明。
        user32.GetClipboardData.restype = ctypes.c_void_p
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        for attempt in range(3):
            if not user32.OpenClipboard(None):
                if attempt < 2:
                    time.sleep(0.05)
                    continue
                return None
            try:
                if not user32.IsClipboardFormatAvailable(13):
                    return ""
                handle = user32.GetClipboardData(13)
                if not handle:
                    return None
                pointer = kernel32.GlobalLock(handle)
                if not pointer:
                    return None
                try:
                    return ctypes.wstring_at(pointer)
                finally:
                    kernel32.GlobalUnlock(handle)
            finally:
                user32.CloseClipboard()
        return None

    def _settings_url(self) -> str:
        port = int(getattr(self.settings, "web_port", 0) or 0)
        try:
            parsed = urllib.parse.urlsplit(self.url)
            port = int(parsed.port or port)
        except (ValueError, TypeError):
            pass
        return "http://127.0.0.1:%d/api/settings" % port

    def _import_clipboard_cookie(self) -> None:
        self._clipboard_cookie_busy = True
        try:
            clipboard = self._clipboard_text()
            if clipboard is None:
                self._notify("读取剪贴板失败，请稍后重试")
                return
            cookie = clipboard.strip()
            if not cookie:
                self._notify("剪贴板里没有内容")
                return
            token = _read_env_value(self.env_file, "QQ_DIGEST_WEB_TOKEN") or _read_meta_token(self.env_file, self.root)
            request = urllib.request.Request(
                self._settings_url(),
                data=json.dumps({"chaoxing_cookie": cookie}, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json", "X-Token": token},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    result = json.loads(response.read().decode("utf-8"))
            except (OSError, ValueError):
                self._notify("群务台服务没有响应，请确认它在运行")
                return
            if not isinstance(result, dict):
                self._notify("群务台服务没有响应，请确认它在运行")
            elif result.get("ok") is True:
                self._notify("已从剪贴板导入学习通 Cookie")
            else:
                self._notify(str(result.get("error") or "导入学习通 Cookie 失败"))
        finally:
            self._clipboard_cookie_busy = False

    def on_import_clipboard_cookie(self, icon: object = None, item: object = None) -> None:
        if self.is_busy() or self._cookie_read_busy or self._clipboard_cookie_busy:
            return
        threading.Thread(target=self._import_clipboard_cookie, daemon=True).start()

    def on_restart(self, icon: object = None, item: object = None) -> None:
        if self.is_busy():
            return

        def restart() -> None:
            self.busy = True
            try:
                pending = self._pending_update()
                if pending:
                    self._notify("正在安装更新并退出…")
                    self._stop_watchdog()
                    from noticehub import updater

                    result = updater.apply_async(
                        self.root,
                        Path(pending["zip"]),
                        os.getpid(),
                        data_dir=_data_dir_for(self.root, self.settings),
                    )
                    if not result.get("ok"):
                        self._notify("更新启动失败：%s" % (result.get("error") or "未知错误"))
                        self._start_watchdog()
                        return
                    updater.clear_pending(_data_dir_for(self.root, self.settings))
                    self._quit_steps()
                    os._exit(0)

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
                if self._watchdog_stop.is_set() and self.busy:
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
        """退出流程：每一步都各自兜住异常，任何一步炸了都不许拦住后面的退出。

        顺序是「先断服务与图标，再清理外部进程」：服务子进程一停、托盘图标一关，
        用户立刻看到程序关了；托管收尾（停 NapCat、按设置开回电脑版 QQ）属于清理
        外部进程，慢一点也不能拖住整个退出——2026-10-31 06:19 就是它吃掉 30 秒，
        用户点了退出等 40 秒还被强杀。托管收尾只在本地做（读 data/hosting.json、
        调 napcat_admin），不依赖服务子进程，所以提前关服务不会让它拿不到状态。
        """
        try:
            self._stop_watchdog()
        except Exception:
            pass
        # ① 先断服务子进程与托盘图标：这是「已经关了」的信号。
        self._stop_service_child()
        try:
            if self.icon is not None:
                self.icon.stop()
        except Exception:
            self._log_watchdog("退出时关托盘图标出错（继续退出）")
        # ② 关浮窗（外部 UI 进程，同样是清理）。
        try:
            self.floatball.stop()
        except Exception:
            self._log_watchdog("退出时关闭浮窗出错（继续退出）")
        # ③ 托管收尾放最后，并给总预算：超时就放弃、记一条 warning 继续退。
        try:
            if hosting.hosting_status(self.settings).get("hosting_active"):
                hosting.stop(self.settings, quitting=True, budget=QUIT_TEARDOWN_BUDGET_SECONDS)
        except Exception:
            # 结束托管失败也得退：留着托盘不让用户走，比「电脑版 QQ 没自动恢复」更糟。
            self._log_watchdog("退出时结束托管出错（继续退出）")
        # 退出时开回 QQ 是 opt-in：默认只安静退出，不替用户多开一个进程。
        # 用户 2026-10-12：「托盘右键退出会自动开 qq，我希望是设置项里的选项，默认关闭」。
        if hosting.restore_qq_on_quit(self.settings):
            try:
                hosting.start_user_qq(self.settings, verify=False)
            except Exception:
                self._log_watchdog("退出时恢复电脑版 QQ 出错（继续退出）")

    def _toggle_floatball(self, icon: object = None, item: object = None) -> None:
        if self.floatball.visible:
            self.floatball.hide()
            return
        if getattr(self.floatball, "root", None) is not None:
            self.floatball.show()
        else:
            threading.Thread(target=self._start_floatball, daemon=True).start()

    def _start_floatball(self) -> None:
        if not FLOATBALL_ENABLED:
            return
        try:
            # self.url 本身可能已经带 ?token=…，先拆开再拼 /floatball，别把路径拼进查询串。
            base, sep, query = str(self.url).partition("?")
            url = base.rstrip("/") + "/floatball" + (("?" + query) if sep else "")
            if "token=" not in query:
                token = _read_env_value(self.env_file, "QQ_DIGEST_WEB_TOKEN") or _read_meta_token(self.env_file, self.root)
                if token:
                    url += ("&" if "?" in url else "?") + "token=" + urllib.parse.quote(str(token), safe="")
            self.floatball.start(url)
        except Exception as exc:
            self._log_watchdog("浮窗启动失败：%s" % exc)

    def _force_quit(self) -> None:
        self._log_watchdog("退出超时（>%ds），强制结束" % int(QUIT_HARD_DEADLINE_SECONDS))
        self._stop_service_child()
        os._exit(0)

    def build_menu(self, pystray: object) -> object:
        items = [
            pystray.MenuItem("开始托管", self.on_start, enabled=lambda item: not self.is_busy()),
            pystray.MenuItem("结束托管", self.on_stop, enabled=lambda item: not self.is_busy()),
            # pystray 会在菜单显示时调用 callable 文案；此时读取 pending.json，
            # 让启动后下载的更新也能显示「重启并更新」，同时保留 enabled 动态判断。
            pystray.MenuItem(self._restart_title, self.on_restart, enabled=lambda item: not self.is_busy()),
            pystray.MenuItem("读取学习通 Cookie", self.on_read_chaoxing_cookie, enabled=lambda item: not self.is_busy() and not self._cookie_read_busy and not self._clipboard_cookie_busy),
            pystray.MenuItem("从剪贴板导入学习通 Cookie", self.on_import_clipboard_cookie, enabled=lambda item: not self.is_busy() and not self._cookie_read_busy and not self._clipboard_cookie_busy),
        ]
        # 浮球暂停时这一项点了什么都不发生，干脆不摆出来（置灰也还是名不副实）。
        if FLOATBALL_ENABLED:
            items.append(pystray.MenuItem("显示/隐藏浮窗", self._toggle_floatball))
        items += [
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("打开控制台", self.on_open, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出（结束托管并关闭）", self.on_quit),
        ]
        return pystray.Menu(*items)

    def run(self) -> bool:
        """起托盘并阻塞到用户选「退出」。没有托盘能力时返回 False。"""
        try:
            import pystray
        except Exception:
            return False
        image = _icon_image(self.root, _update_channel(self.env_file))
        if image is None:
            return False
        try:
            self.icon = pystray.Icon("notice-hub", image, "notice-hub · " + self._status_line(), self.build_menu(pystray))
            self._start_watchdog()
            # 启动时只读查一次防火墙；缺规则就弹一次 UAC（24 小时内不重复）。
            # 放后台线程：UAC 会一直等到用户点按钮，不能挡住托盘和浮球。
            threading.Thread(target=self._firewall_check, name="notice-hub-firewall", daemon=True).start()
            threading.Thread(target=self._start_floatball, daemon=True).start()
            if bool(getattr(self.settings, "hosting_auto_on_start", False)):
                threading.Thread(target=self._start, daemon=True).start()
            self.icon.run()
            self._stop_watchdog()
        except Exception:
            return False
        return True


def main() -> int:
    global BROWSER_SUPPRESSED
    BROWSER_SUPPRESSED = "--no-browser" in sys.argv[1:]
    if ensure_dpi_aware is not None:
        ensure_dpi_aware()
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
        _create_desktop_shortcut(root, _update_channel(env_file))
    settings = Settings.from_env(env_file=env_file)
    port = int(getattr(settings, "web_port", 8766) or 8766)
    url = _console_url(env_file, port)
    if not _acquire_single_instance(root):
        # 同一个安装目录已经在跑：把已经开着的那份控制台再打开一次，然后安静退出。
        # 注意这里打开的是**本安装目录自己的** .env 拼出来的 url，所以另一个安装目录
        # （生产 / beta）不会走到这个分支——它们的互斥体名不同。
        _open_browser(url)
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
        # 不再把"端口被占用"列为常见原因（真被占会在上面走回退），也不再一上来就
        # 建议重新解压——先给服务自己报的原因。
        detail = _last_service_error_message(root)
        reason = (
            "服务没能启动（端口 %d，退出码 %s）。\n\n"
            "%s"
            "下一步：看安装目录下 logs\\qq-live-digest.log 与 logs\\launcher.log 的最后几行；\n"
            "两个日志里都没有错误，就再启动一次；仍不行，才把安装包重新解压到一个新目录试。"
        ) % (
            selected_port,
            exit_code,
            ("服务自己报的原因：%s\n\n" % detail) if detail else "",
        )
        _log_launcher(root, "健康检查失败：port=%d exit=%s" % (port, exit_code))
        _notify_user(root, reason)
        return 1
    if not _console_window_open():
        _open_browser(url)
    if _update_check_enabled(env_file):
        # 后台查一次更新（守护线程、不阻塞启动）；QQ_DIGEST_UPDATE_CHANNEL=off 时整块跳过。
        threading.Thread(
            target=_update_check_worker,
            args=(root, _data_dir_for(root, settings), _update_channel(env_file)),
            name="notice-hub-update-check",
            daemon=True,
        ).start()
    host = TrayHost(root, env_file, settings, process, url, selected_port)
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
