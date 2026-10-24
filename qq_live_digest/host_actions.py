"""宿主机操作的唯一入口：提权执行、防火墙规则、宿主操作审计。

为什么单独一个模块：防火墙放行、装 Tailscale、往 QQ 目录拷 NapCat 加载器都需要管理员
权限，之前 webapp / packaging.launcher / tunnel 各写一套 ``ShellExecuteW(runas)``，
netsh 命令也抄了三份。这里只做集中，命令字符串与旧实现逐字一致（先照抄，不改行为）。

本模块纯标准库，且必须能在非 Windows 上 ``import``：Windows-only 的 ctypes 调用都在
函数内部，模块顶层不碰 ``winreg`` / ``ctypes`` 之外的平台模块。
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from . import ttl_cache
from .messages import ADMIN_CANCELLED, ADMIN_FAILED
from .win_text import decode_console_output

LOGGER = logging.getLogger("qq_live_digest.host_actions")

AUDIT_FILE_NAME = "host-actions.log"
FIREWALL_LOOKUP_TIMEOUT = 3.0
FIREWALL_CACHE_SECONDS = 60.0
FIREWALL_CACHE_KEY = "host_actions.firewall_rules"
RULE_NAME_TEMPLATE = "群务台 {port}"


# --------------------------------------------------------------------------
# 平台 / 权限
# --------------------------------------------------------------------------
def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_admin() -> bool:
    """当前进程是否已经提权；非 Windows 永远 False。"""
    if not is_windows():
        return False
    try:
        import ctypes  # noqa: PLC0415 - Windows-only

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 - 查不到就当没有，别让调用方因此崩
        return False


# --------------------------------------------------------------------------
# 审计
# --------------------------------------------------------------------------
def _default_log_dir() -> Path:
    data_dir = os.environ.get("QQ_DIGEST_DATA_DIR", "").strip()
    base = Path(data_dir) if data_dir else Path("data")
    return base / "logs"


def audit_host_action(action: str, detail: Any, result: str, log_dir: Any = None) -> None:
    """写一行宿主机操作审计（at / action / detail / result）。失败只 warn，绝不抛。"""
    try:
        directory = Path(log_dir) if log_dir else _default_log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "action": str(action or ""),
                "detail": detail if isinstance(detail, (str, int, float, bool, list, dict)) else str(detail),
                "result": str(result or ""),
            },
            ensure_ascii=False,
        )
        with (directory / AUDIT_FILE_NAME).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:  # noqa: BLE001 - 审计失败不许影响业务
        LOGGER.warning("写宿主操作审计失败：%s", action, exc_info=True)


# --------------------------------------------------------------------------
# 提权执行：唯一入口
# --------------------------------------------------------------------------
def run_elevated(
    args: Sequence[str] | str,
    *,
    reason: str = "",
    timeout: float = 0.0,
    log_dir: Any = None,
) -> tuple[str, str]:
    """用 ``ShellExecuteW(None, "runas", ...)`` 拉起一条需要管理员权限的命令。

    ``args`` 可以是参数列表，也可以是**整条命令行字符串**（例如 netsh 那条完整命令：
    此时 exe 与参数按原样传给 ShellExecuteW，不重新拼引号，与旧实现逐字一致）。

    返回 ``(state, message)``，``state`` 取值：
    * ``ok``          —— UAC 通过、进程已拉起（拿不到退出码，实际结果由调用方复查）
    * ``cancelled``   —— 用户在 UAC 里点「否」/ 取消（ShellExecuteW 返回 5 或 1223）
    * ``failed``      —— 其他 ``<= 32`` 的失败（找不到程序、策略拒绝等）
    * ``unsupported`` —— 非 Windows

    ponytail: ``timeout`` 目前不使用 —— 等提权进程结束会把托盘/请求线程卡在 UAC 上，
    旧实现一致不等待；保留参数是为了调用方接口统一，真需要等待时再实现。
    """
    listed = [str(item) for item in args] if not isinstance(args, str) else None
    text = args if isinstance(args, str) else subprocess.list2cmdline(listed or [])
    detail = {"reason": str(reason or ""), "command": text}
    if not text.strip():
        audit_host_action("run_elevated", detail, "failed：没有可执行的命令", log_dir)
        return "failed", "没有可执行的命令。"
    if not is_windows():
        audit_host_action("run_elevated", detail, "unsupported", log_dir)
        return "unsupported", "提权执行只在 Windows 上可用。"
    if isinstance(args, str):
        executable, _, parameters = text.strip().partition(" ")
        if not executable:
            audit_host_action("run_elevated", detail, "failed：没有可执行的命令", log_dir)
            return "failed", "没有可执行的命令。"
    else:
        executable = listed[0]
        parameters = subprocess.list2cmdline(listed[1:])
    try:
        import ctypes  # noqa: PLC0415 - Windows-only

        code = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, parameters, None, 0)
    except Exception as error:  # noqa: BLE001 - 提权失败一律转成文案
        message = ADMIN_FAILED.format(detail=error)
        audit_host_action("run_elevated", detail, f"failed：{message}", log_dir)
        return "failed", message
    code = int(code or 0)
    # 5 = UAC 里点「否」/策略拒绝，1223 = 用户取消；这两个值可能大于 32，要先判。
    if code in {5, 1223}:
        message = ADMIN_CANCELLED
        audit_host_action("run_elevated", detail, "cancelled", log_dir)
        return "cancelled", message
    if code <= 32:
        message = ADMIN_FAILED.format(detail=f"返回码 {code}")
        audit_host_action("run_elevated", detail, f"failed：{message}", log_dir)
        return "failed", message
    message = "已请求管理员权限，确认后稍等几秒。"
    audit_host_action("run_elevated", detail, "ok", log_dir)
    return "ok", message


# --------------------------------------------------------------------------
# 防火墙（netsh）
# --------------------------------------------------------------------------
def firewall_rule_name(port: int) -> str:
    return RULE_NAME_TEMPLATE.format(port=int(port))


def firewall_allow_command(port: int) -> str:
    """与 webapp._firewall_rule_command / launcher._firewall_rule_command 逐字一致。"""
    return (
        f'netsh advfirewall firewall add rule name="群务台 {int(port)}" '
        f"dir=in action=allow protocol=TCP localport={int(port)}"
    )


def firewall_inbound_rules() -> tuple[dict[str, str], ...]:
    """读 netsh 的入站规则（只读、不提权）；读不到返回空元组，调用方当作「没查成」。"""
    if not is_windows():
        return ()
    return ttl_cache.get(
        FIREWALL_CACHE_KEY,
        FIREWALL_CACHE_SECONDS,
        _read_firewall_rules,
        cache_empty=False,
    )


def _read_firewall_rules() -> tuple[dict[str, str], ...]:
    try:
        completed = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=all", "dir=in"],
            capture_output=True,
            timeout=FIREWALL_LOOKUP_TIMEOUT,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    text = decode_console_output(completed.stdout) if completed.returncode == 0 else ""
    blocks: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            if current:
                blocks.append(current)
                current = {}
            continue
        key, sep, value = line.partition(":")
        if sep:
            current[key.strip().casefold()] = value.strip()
    if current:
        blocks.append(current)
    return tuple(blocks)


def firewall_allows(port: int) -> dict[str, Any]:
    """查本机有没有放行该 TCP 端口入站的规则。只读、不提权、不改设置。"""
    result: dict[str, Any] = {"checked": False, "allowed": False, "port": port, "command": firewall_allow_command(port)}
    rules = firewall_inbound_rules()
    if not rules:
        return result
    result["checked"] = True
    wanted = str(int(port))
    for block in rules:
        if block.get("enabled", "").casefold() != "yes" or block.get("action", "").casefold() != "allow":
            continue
        if block.get("direction", "").casefold() != "in":
            continue
        ports = {item.casefold() for item in re.split(r"[,\s]+", block.get("localport", "")) if item}
        if "any" in ports or wanted in ports:
            result["allowed"] = True
            result["rule"] = block.get("rule name", "")
            break
    return result


def reset_firewall_cache() -> None:
    """丢掉防火墙检测缓存（规则刚被改过时调用）。"""
    ttl_cache.clear(FIREWALL_CACHE_KEY)
