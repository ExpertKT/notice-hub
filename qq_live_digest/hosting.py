"""「托管」：让 NapCat 独占某个 QQ 号时，临时退出/恢复用户自己的电脑版 QQ。

用户明确要求（改动前先读这几条）：
- **绝不修改、移动、删除用户 QQ 的任何文件**；这里只做「关进程 / 重开进程」。
- 只认注册表登记的那份 QQ（本机是 ``D:\\QQ\\QQ.exe``），绝不把 NapCat 自带的壳
  ``QQ.exe``（``F:\\napcat-dl\\...\\NapCat.*.Shell\\QQ.exe``）当成用户的 QQ。
- 托盘不可用、注册表读不到、进程查不到时，一律降级返回错误字典，不抛异常、不崩。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config import update_env_file

AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "notice-hub"
QQ_EXE_NAME = "QQ.exe"
# 32 位视图与 64 位视图都试；QQ 的卸载信息在 WOW6432Node 下。
QQ_UNINSTALL_KEYS = (
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ",
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ",
)
NAPCAT_ONEBOT_PORT = 3001
NAPCAT_WEBUI_PORT = 6099

HOSTING_ENV_KEYS = {
    "quit_qq": "QQ_DIGEST_HOSTING_QUIT_QQ",
    "restore_qq": "QQ_DIGEST_HOSTING_RESTORE_QQ",
    "auto_on_start": "QQ_DIGEST_HOSTING_AUTO_ON_START",
    "autostart": "QQ_DIGEST_AUTOSTART",
}

_PROCESS_QUERY = (
    "$ErrorActionPreference='SilentlyContinue';"
    "Get-CimInstance Win32_Process -Filter \"Name='QQ.exe'\" |"
    "Select-Object ProcessId,ExecutablePath,ParentProcessId | ConvertTo-Json -Compress"
)


def _winreg() -> Any:
    import winreg  # 仅 Windows 有；放在函数里便于测试替换

    return winreg


def _exec_from_command(text: str) -> str:
    """从注册表命令串里取出可执行文件路径（兼容带引号与不带引号）。"""
    text = (text or "").strip()
    if not text:
        return ""
    if text.startswith('"'):
        end = text.find('"', 1)
        return text[1:end] if end > 0 else text[1:]
    match = re.match(r"(?i)(.*?\.exe)", text)
    return match.group(1) if match else text.split(" ")[0]


def _qq_exe_candidates() -> list[Path]:
    """按注册表登记的卸载路径推出电脑版 QQ 的安装目录，再拼出 QQ.exe。"""
    out: list[Path] = []
    try:
        winreg = _winreg()
    except ImportError:
        return out
    for key_path in QQ_UNINSTALL_KEYS:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
                raw, _ = winreg.QueryValueEx(key, "UninstallString")
        except OSError:
            continue
        exe = _exec_from_command(str(raw or ""))
        if exe:
            out.append(Path(exe).parent / QQ_EXE_NAME)
    return out


def user_qq_path() -> Path | None:
    """电脑版 QQ 的 ``QQ.exe`` 绝对路径；找不到就返回 None。"""
    for candidate in _qq_exe_candidates():
        text = str(candidate).lower()
        if "napcat" in text:
            continue  # NapCat 的壳 QQ 绝不能当作用户的 QQ
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _processes() -> list[dict[str, Any]]:
    """列出名为 QQ.exe 的进程（含完整路径），失败时返回空列表。"""
    if os.name != "nt":
        return []
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _PROCESS_QUERY],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    text = (completed.stdout or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    return [item for item in data if isinstance(item, dict)]


def user_qq_pids() -> list[int]:
    """正在运行的**用户自己的**电脑版 QQ 进程号。"""
    target = user_qq_path()
    if target is None:
        return []
    wanted = str(target).lower()
    pids: list[int] = []
    for item in _processes():
        path = str(item.get("ExecutablePath") or "").lower()
        if path and path == wanted:
            try:
                pids.append(int(item.get("ProcessId") or 0))
            except (TypeError, ValueError):
                continue
    return [pid for pid in pids if pid]


def _taskkill(pid: int, *, force: bool) -> bool:
    args = ["taskkill.exe", "/PID", str(pid)]
    if force:
        args.append("/F")
    try:
        completed = subprocess.run(
            args,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return False
    return completed.returncode == 0


def quit_user_qq(timeout: float = 12.0) -> dict[str, Any]:
    """先请电脑版 QQ 正常退出，超时仍未退出才强制结束。"""
    pids = user_qq_pids()
    if not pids:
        return {"ok": True, "pids": [], "already_closed": True, "forced": False, "remaining": [], "error": None}
    for pid in pids:
        _taskkill(pid, force=False)
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if not user_qq_pids():
            return {"ok": True, "pids": pids, "already_closed": False, "forced": False, "remaining": [], "error": None}
        time.sleep(0.5)
    remaining = user_qq_pids()
    forced = False
    for pid in remaining:
        forced = _taskkill(pid, force=True) or forced
    time.sleep(1.0)
    still = user_qq_pids()
    return {
        "ok": not still,
        "pids": pids,
        "already_closed": False,
        "forced": forced,
        "remaining": still,
        "error": "" if not still else "电脑版 QQ 未能退出（可能停在确认对话框）",
    }


def start_user_qq() -> dict[str, Any]:
    """把电脑版 QQ 重新打开。已经在跑就不重复启动。"""
    path = user_qq_path()
    if path is None:
        return {"ok": False, "pid": None, "already_running": False, "error": "找不到电脑版 QQ"}
    if user_qq_pids():
        return {"ok": True, "pid": None, "already_running": True, "error": None}
    flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
    )
    try:
        child = subprocess.Popen([str(path)], cwd=str(path.parent), creationflags=flags)
    except OSError as exc:
        return {"ok": False, "pid": None, "already_running": False, "error": str(exc)}
    return {"ok": True, "pid": child.pid, "already_running": False, "error": None}


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    with socket.socket() as sock:
        sock.settimeout(timeout)
        try:
            return sock.connect_ex((host, port)) == 0
        except OSError:
            return False


def _state_path(settings: Any) -> Path:
    return Path(getattr(settings, "data_dir", ".")) / "hosting.json"


def _read_state(settings: Any) -> dict[str, Any]:
    try:
        data = json.loads(_state_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(settings: Any, state: dict[str, Any]) -> None:
    path = _state_path(settings)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def hosting_status(settings: Any | None = None) -> dict[str, Any]:
    """当前状态。``hosting_active`` 只由我们自己写在 data_dir/hosting.json 的状态决定。"""
    path = user_qq_path()
    pids = user_qq_pids()
    state = _read_state(settings) if settings is not None else {}
    whitelist = getattr(settings, "group_whitelist", ()) or ()
    return {
        "ok": True,
        "user_qq_path": str(path) if path else None,
        "user_qq_pids": pids,
        "user_qq_running": bool(pids),
        "napcat_running": _port_open(NAPCAT_WEBUI_PORT) or _port_open(NAPCAT_ONEBOT_PORT),
        "napcat_online": _port_open(NAPCAT_ONEBOT_PORT),
        "hosting_active": bool(state.get("active")),
        "hosting_uin": state.get("uin"),
        "hosting_since": state.get("since"),
        "groups_selected": len(tuple(whitelist)),
        "error": None,
    }


def _napcat_admin() -> Any | None:
    try:
        from . import napcat_admin
    except Exception:  # pragma: no cover - 只在下游模块损坏时触发
        return None
    return napcat_admin


def _bridge(name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    module = _napcat_admin()
    func = getattr(module, name, None) if module is not None else None
    if func is None:
        return {"ok": False, "error": "NapCat 管理模块不可用（缺少 %s）" % name}
    try:
        result = func(*args, **kwargs)
    except Exception as exc:  # NapCat 侧任何异常都不该把托盘拖崩
        return {"ok": False, "error": str(exc)}
    return result if isinstance(result, dict) else {"ok": False, "error": "unexpected result"}


def start(settings: Any, *, uin: str | None = None) -> dict[str, Any]:
    """开始托管：按设置退出电脑版 QQ，再启动 NapCat。"""
    # 空的 uin 一律归一成 None：NapCat 收到 -q 时会去"快速登录"那个号，
    # 传字符串 "None" 或者空串都会变成一次注定失败的快速登录，而不是扫码。
    clean_uin = (uin or "").strip() or None
    steps: list[dict[str, Any]] = []
    quit_result = {"ok": True, "skipped": True}
    if bool(getattr(settings, "hosting_quit_qq", True)):
        quit_result = quit_user_qq()
        steps.append({"step": "quit-user-qq", **quit_result})
    napcat = _bridge("launch", settings, uin=clean_uin)
    steps.append({"step": "start-napcat", **napcat})
    ok = bool(quit_result.get("ok")) and bool(napcat.get("ok"))
    if ok:
        _write_state(
            settings,
            {"active": True, "uin": clean_uin, "since": dt.datetime.now().isoformat(timespec="seconds")},
        )
    return {"ok": ok, "steps": steps, "napcat": napcat, "error": napcat.get("error") or quit_result.get("error")}


def stop(settings: Any) -> dict[str, Any]:
    """结束托管：停掉 NapCat，再按设置把电脑版 QQ 开回来。"""
    steps: list[dict[str, Any]] = []
    napcat = _bridge("stop", settings)
    steps.append({"step": "stop-napcat", **napcat})
    restore_result = {"ok": True, "skipped": True}
    if bool(getattr(settings, "hosting_restore_qq", True)):
        restore_result = start_user_qq()
        steps.append({"step": "restore-user-qq", **restore_result})
    ok = bool(napcat.get("ok")) and bool(restore_result.get("ok"))
    _write_state(settings, {"active": False})
    return {"ok": ok, "steps": steps, "napcat": napcat, "error": napcat.get("error") or restore_result.get("error")}


def autostart_enabled() -> bool:
    try:
        winreg = _winreg()
    except ImportError:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, AUTOSTART_NAME)
    except OSError:
        return False
    return True


def set_autostart(enabled: bool, *, target: Path | str | None = None) -> dict[str, Any]:
    """写/删 HKCU\\...\\Run 下的开机启动项。只动我们自己那一个值，别的项一个不碰。"""
    if os.name != "nt":
        return {"ok": False, "enabled": bool(enabled), "target": None, "error": "仅支持 Windows"}
    try:
        winreg = _winreg()
    except ImportError as exc:
        return {"ok": False, "enabled": bool(enabled), "target": None, "error": str(exc)}
    exe = str(Path(target or sys.executable))
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, '"' + exe + '"')
            else:
                try:
                    winreg.DeleteValue(key, AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
    except OSError as exc:
        return {"ok": False, "enabled": bool(enabled), "target": exe, "error": str(exc)}
    return {"ok": True, "enabled": bool(enabled), "target": exe, "error": None}


def prefs(settings: Any) -> dict[str, bool]:
    return {
        "quit_qq": bool(getattr(settings, "hosting_quit_qq", True)),
        "restore_qq": bool(getattr(settings, "hosting_restore_qq", True)),
        "auto_on_start": bool(getattr(settings, "hosting_auto_on_start", False)),
        "autostart": bool(getattr(settings, "autostart", False)),
    }


def save_prefs(settings: Any, values: dict[str, Any]) -> dict[str, Any]:
    """把托管的四个开关写回 .env（只重写这四行），并同步内存里的 settings。"""
    wanted = {key: values[key] for key in HOSTING_ENV_KEYS if key in values}
    if not wanted:
        return {"ok": False, "error": "没有可保存的键", "env": None}
    env_file = getattr(settings, "env_file", None)
    if not env_file:
        return {"ok": False, "error": "找不到 .env 路径", "env": None}
    updates = {HOSTING_ENV_KEYS[key]: ("1" if bool(value) else "0") for key, value in wanted.items()}
    result = update_env_file(env_file, updates)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error"), "env": str(env_file)}
    attrs = {
        "quit_qq": "hosting_quit_qq",
        "restore_qq": "hosting_restore_qq",
        "auto_on_start": "hosting_auto_on_start",
        "autostart": "autostart",
    }
    for key, value in wanted.items():
        setattr(settings, attrs[key], bool(value))
    autostart_result = set_autostart(bool(wanted["autostart"])) if "autostart" in wanted else None
    return {
        "ok": True,
        "error": None,
        "env": str(env_file),
        "updated": result.get("updated"),
        "added": result.get("added"),
        "autostart": autostart_result,
    }
