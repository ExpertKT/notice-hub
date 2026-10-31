"""手机端待办台：一个极小的 HTTP 服务，读取 tasks 表并支持勾选完成。"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import announce
from . import community
from . import qr as qr_encoder
from .chaoxing_receiver import ChaoxingAuthError, ChaoxingClient, ChaoxingError, chaoxing_status
from .dingtalk_receiver import DingTalkLocalReader
from .wechat_receiver import group_names as wechat_group_names
from .wechat_receiver import locate_storage as wechat_locate_storage
from .wechat_receiver import storage_unavailable_hint as wechat_storage_hint
from .wechat_receiver import scan_error_hint as wechat_scan_error_hint
from .wechat_receiver import last_key_failure as wechat_last_key_failure
from .wechat_receiver import clear_storage_cache as wechat_clear_storage_cache
# 配置审计只读微信定位的**缓存**：缓存键与 TTL 跟 locate_storage 用同一份，这样在不触发
# 探测（不起 PowerShell）的前提下也能拿到「上次定位到的目录」。
from .wechat_receiver import LOCATE_CACHE_SECONDS as WECHAT_LOCATE_CACHE_SECONDS
from .wechat_receiver import _LOCATE_CACHE_KEY as WECHAT_LOCATE_CACHE_KEY
from .config_audit import audit_config
from . import lab
from .settings_api import MAX_BODY_BYTES, PUSH_HELP_LINKS, SettingsPostMixin, _PUSH_SETTING_ENV_KEYS, _clean_chaoxing_cookie, _invalidate_update_cache, _llm_setting_state, _llm_setting_updates, _llm_test_error, _push_setting_state, _push_setting_values
from . import routes
from . import pairing
from . import selfcheck
from . import tunnel
from . import ttl_cache
from . import update_sources
from .attachments import delete_images, image_key, pick_image
from .config import DEFAULT_VISION_MODEL, RUNTIME_ROOT, Settings, split_list, update_env_file, web_port_source
from .messages import FIREWALL_NEEDS_TRAY
from .store import Store
from .timeutil import iso, now_local, parse_iso
from . import host_actions
from .win_text import decode_console_output

QQ_GROUP_RETRY_DELAYS = (60, 120, 300)
DINGTALK_GROUP_LLM_TIMEOUT_SECONDS = 5

# 红队 S1：令牌不再只靠 URL。会话 Cookie 的值是服务端可重算的 HMAC（无状态、重启不失效），
# 断言比对的仍是「谁知道令牌」，但页面/历史记录里不会再留下明文令牌。
SESSION_COOKIE_NAME = "nh_session"
SESSION_COOKIE_MAX_AGE = 2592000  # 30 天
SESSION_HMAC_LABEL = b"nh-session-v1"
# 红队 S12：同一来源 1 分钟内 401 超过 30 次，先锁 60 秒再做比对（省 CPU、也挡住在线爆破）。
AUTH_FAILURE_LIMIT = 30
AUTH_FAILURE_WINDOW_SECONDS = 60.0
AUTH_FAILURE_BLOCK_SECONDS = 60.0
# 红队 S21：慢客户端不许把线程池占满。
MAX_ACTIVE_CONNECTIONS = 64
REQUEST_TIMEOUT_SECONDS = 30

_AUTH_LOCK = threading.Lock()
_AUTH_FAILURES: dict[str, list[float]] = {}
_AUTH_BLOCKED_UNTIL: dict[str, float] = {}


def _session_cookie_value(token: str) -> str:
    """令牌 → 会话 Cookie 值。确定性 HMAC：重启后旧 Cookie 依然有效。"""
    return hmac.new(str(token).encode("utf-8"), SESSION_HMAC_LABEL, hashlib.sha256).hexdigest()


def _session_cookie_header(token: str) -> str:
    # HttpOnly 让页面脚本偷不到；SameSite=Lax 挡住跨站表单提交；不设 Secure 是因为本机
    # 常见部署是 http://127.0.0.1，加了 Secure 浏览器会直接丢掉这个 Cookie。
    return "%s=%s; HttpOnly; SameSite=Lax; Path=/; Max-Age=%d" % (
        SESSION_COOKIE_NAME, _session_cookie_value(token), SESSION_COOKIE_MAX_AGE,
    )


def _auth_prune(source: str, now: float) -> None:
    moments = _AUTH_FAILURES.get(source)
    if not moments:
        _AUTH_FAILURES.pop(source, None)
        return
    kept = [moment for moment in moments if now - moment < AUTH_FAILURE_WINDOW_SECONDS]
    if kept:
        _AUTH_FAILURES[source] = kept
    else:
        _AUTH_FAILURES.pop(source, None)


def _auth_blocked(source: str, now: float | None = None) -> bool:
    moment = time.monotonic() if now is None else float(now)
    with _AUTH_LOCK:
        until = _AUTH_BLOCKED_UNTIL.get(source)
        if until is None:
            return False
        if moment >= until:
            _AUTH_BLOCKED_UNTIL.pop(source, None)
            return False
        return True


def _record_auth_failure(source: str, now: float | None = None) -> None:
    moment = time.monotonic() if now is None else float(now)
    with _AUTH_LOCK:
        _auth_prune(source, moment)
        moments = _AUTH_FAILURES.setdefault(source, [])
        moments.append(moment)
        if len(moments) > AUTH_FAILURE_LIMIT:
            _AUTH_BLOCKED_UNTIL[source] = moment + AUTH_FAILURE_BLOCK_SECONDS
            _AUTH_FAILURES.pop(source, None)


def _clear_auth_failures(source: str) -> None:
    with _AUTH_LOCK:
        _AUTH_FAILURES.pop(source, None)
        _AUTH_BLOCKED_UNTIL.pop(source, None)


def _reset_auth_limits() -> None:
    """测试用：清空限流状态。"""
    with _AUTH_LOCK:
        _AUTH_FAILURES.clear()
        _AUTH_BLOCKED_UNTIL.clear()


try:
    from . import catchup
except Exception as exc:  # noqa: BLE001
    catchup = None  # type: ignore[assignment]
    logging.getLogger(__name__).warning("模块 %s 不可用：%s", "catchup", exc)

try:
    from . import inbox
except Exception as exc:  # noqa: BLE001
    inbox = None  # type: ignore[assignment]
    logging.getLogger(__name__).warning("模块 %s 不可用：%s", "inbox", exc)

try:
    from . import group_suggest
except Exception as exc:  # noqa: BLE001
    group_suggest = None  # type: ignore[assignment]
    logging.getLogger(__name__).warning("模块 %s 不可用：%s", "group_suggest", exc)

try:
    from . import hosting
except Exception as exc:  # noqa: BLE001
    hosting = None  # type: ignore[assignment]
    logging.getLogger(__name__).warning("模块 %s 不可用：%s", "hosting", exc)

try:  # 「一键接入」实现；缺失时向导降级为手工接线而不是整站报错
    from . import napcat_admin
except Exception as exc:  # noqa: BLE001
    napcat_admin = None  # type: ignore[assignment]
    logging.getLogger(__name__).warning("模块 %s 不可用：%s", "napcat_admin", exc)

try:
    from . import browser_cookie
except Exception as exc:  # noqa: BLE001
    browser_cookie = None  # type: ignore[assignment]
    logging.getLogger(__name__).warning("模块 %s 不可用：%s", "browser_cookie", exc)

try:  # 「设置 → 版本更新」；缺失时该卡片说明情况，不能把整站拖垮
    from . import updater
except Exception as exc:  # noqa: BLE001
    updater = None  # type: ignore[assignment]
    logging.getLogger(__name__).warning("模块 %s 不可用：%s", "updater", exc)

LOGGER = logging.getLogger(__name__)



MAX_SYNC_QR_CHARS = 512
# 历史缓存底线探测：每页 1000 条、单页超时 15s，2 页封顶把同步等待压到 ~30s。
HISTORY_FLOOR_MAX_PAGES = 2
# 对外公告：改动要尽快到达用户，15 分钟是「够快」与 GitHub raw 限速的折中。
ANNOUNCEMENT_REFRESH_SECONDS = 15 * 60
_JOB_LOCK = threading.Lock()
# /api/selfcheck 单飞：页面首次加载会同时发两个自检请求（健康面板 + 星标提示），
# 一个请求要探 NapCat/微信/Tailscale/网卡，跑两遍就是双倍等待。第二次请求等第一次算完复用结果。
SELFCHECK_WAIT_SECONDS = 30.0
# 自检 7 个外部探测（NapCat 端口 / 微信目录 / Tailscale 子进程 / 托管进程）并发跑，
# 每一项都自带这个预算：超时只把那一行降成 warn，不让整块自检等下去。
SELFCHECK_ITEM_TIMEOUT_SECONDS = 15.0
# 并发探测结束后，总览与组装这一段（含推送/ Funnel 的轻量查询）单独给的收尾预算：
# 整条 /api/selfcheck 的上限就是 15 + 2 秒，超了不再补算。
SELFCHECK_TAIL_SECONDS = 2.0
# 探测名 → 自检面板里的行（doctor 与 napcat 都只影响「QQ 登录」这一行）。
_SELFCHECK_PROBE_ROWS = {
    "doctor": "qq",
    "napcat": "qq",
    "hosting": "hosting",
    "dingtalk": "dingtalk",
    "wechat": "wechat",
    "chaoxing": "chaoxing",
    "sync": "calendar",
}
_SELFCHECK_LOCK = threading.Lock()
_SELFCHECK_RUNNING = False
_SELFCHECK_DONE = threading.Event()
_SELFCHECK_REPORT: dict[str, Any] | None = None
_JOB_STATE: dict[str, Any] = {
    "active": None,
    "cancel": None,
    "history": {"running": False, "stage": "idle", "done": 0, "total": 0, "note": "", "result": None},
    "classify": {"running": False, "stage": "idle", "done": 0, "total": 0, "note": "", "result": None},
    # NapCat 一键安装：下载/解压是分钟级操作，必须能报「已下载多少字节 / 解压到第几条」，
    # 否则前端只能显示一句「请稍候」，用户以为卡死就去点重启（审查 P1）。
    "napcat_install": {"running": False, "stage": "idle", "done": 0, "total": 0, "note": "", "result": None, "detail": {}},
}


UNAUTHORIZED_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>群务台 · 需要重新打开</title>
<style>body{margin:0;padding:24px;background:#0a0c12;color:#f3f5fa;font:16px/1.6 system-ui,sans-serif}
main{max-width:560px;margin:8vh auto}h1{font-size:20px}ol{padding-left:1.2em}li{margin:6px 0}
code{background:#1b2231;padding:2px 6px;border-radius:5px}small{color:#9aa7bd}</style></head>
<body><main><h1>这个页面没有访问权限</h1>
<p>多半是：你打开的是<b>另一份安装</b>留下的旧标签页（两份都用同一个端口，浏览器里存的令牌是上一份的）。</p>
<p>这样重新打开就好：</p>
<ol><li>关掉这个标签页</li><li>双击安装目录里的 <code>QQ-Notice-Hub.exe</code>，或右键托盘图标选「打开控制台」</li></ol>
<p><small>别用刷新或历史记录打开 —— 那两个都不会带上正确的令牌。</small></p>
</main></body></html>"""



# ---- 版本更新 / 公告：实现搬到 updates_api.py（薄委托，patch 点仍在本模块）----
from .updates_api import (  # noqa: F401 - 迁出后仍从 webapp 暴露给测试与路由
  # noqa: F401 - 迁出后仍从 webapp 暴露给测试与路由
    _updates_state_path,
    _read_updates_state,
    _write_updates_state,
    _normalize_announcement_response,
    _announcement_is_fresh,
    _refresh_announcements,
    _announcement_cache_is_stale,
    _start_announcement_refresh,
    _announcement_response,
    _install_root,
    _current_version,
    _update_channel_name,
    _notes_escape,
    split_release_notes,
    _release_notes_inline,
    _release_notes_html,
    _normalize_update,
    _version_kind,
    _is_downgrade,
    _failed_update,
    _persist_update,
    _refresh_update,
    _update_download_snapshot,
    _applying_flag_present,
    _update_payload,
    _update_cache_version_changed,
    _update_response,
    _start_update_download,
    _download_update,
    _apply_update,
    UPDATE_CHECK_SECONDS,
    _APPLYING_FLAG_NAME,
    _FEATURE_HEADS,
    _FIX_HEADS,
    ANNOUNCEMENT_REFRESH_SECONDS,
    ANNOUNCEMENT_ERROR_RETRY_SECONDS,
    LOGGER,
)



def _lan_ipv4() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("8.8.8.8", 80))
        address = str(probe.getsockname()[0])
    if not address or address == "0.0.0.0" or address.startswith("127."):
        raise OSError("无法确定可供手机访问的局域网地址")
    return address


def _is_phone_reachable(address: str) -> bool:
    """环回、链路本地、通配地址手机一定连不上。"""
    return bool(address) and not (address.startswith("127.") or address.startswith("169.254.") or address == "0.0.0.0")


# 现场「手机连不上局域网」的第二个来源：默认路由探测 + hostname 解析会把 Hyper-V / WSL /
# VMware / VirtualBox / Tailscale 这些虚拟网卡的地址当成局域网地址展示。按网卡别名枚举并
# 过滤掉虚拟网卡；拿不到清单（非 Windows、没有 PowerShell）才回落到旧探测逻辑。
_VIRTUAL_ADAPTER_HINTS = (
    "vethernet",
    "hyper-v",
    "wsl",
    "vmware",
    "virtualbox",
    "vbox",
    "tailscale",
    "zerotier",
    "docker",
    "vmnet",
    "npcap",
    "bluetooth",
    "loopback",
    "虚拟",
)
_PHYSICAL_ADAPTER_HINTS = ("wlan", "wi-fi", "wifi", "wireless", "无线", "ethernet", "以太网")
ADAPTER_LOOKUP_TIMEOUT = 3.0
ADAPTER_FIRST_WAIT = 0.4
ADAPTER_CACHE_SECONDS = 60.0
_adapter_lock = threading.Lock()
_adapter_cache: tuple[float, tuple[str, ...]] | None = None
_adapter_thread: threading.Thread | None = None
_adapter_waited = False


def _order_adapter_hosts(rows: Any) -> list[str]:
    """把 Get-NetIPAddress 的结果排成「先真实网卡、后其它」，并丢掉虚拟网卡。"""
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return []
    preferred: list[str] = []
    others: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        address = str(row.get("IPAddress") or "").strip()
        alias = str(row.get("InterfaceAlias") or "").strip().casefold()
        if not _is_phone_reachable(address) or address in seen:
            continue
        if any(hint in alias for hint in _VIRTUAL_ADAPTER_HINTS):
            continue
        if _pair_host_kind(address) == "virtual":
            # VirtualBox 的 host-only 网卡常被改名成「以太网 4」，只能按地址段拦。
            continue
        seen.add(address)
        target = preferred if any(hint in alias for hint in _PHYSICAL_ADAPTER_HINTS) else others
        target.append(address)
    return preferred + others


def _refresh_adapter_cache() -> None:
    """后台跑 Get-NetIPAddress 填缓存；失败也写空表，避免每个请求都重试。"""
    if os.name != "nt":
        return
    command = [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "Get-NetIPAddress -AddressFamily IPv4 | Select-Object IPAddress,InterfaceAlias | ConvertTo-Json -Compress",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=ADAPTER_LOOKUP_TIMEOUT,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        )
        rows = json.loads(decode_console_output(completed.stdout) or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        rows = []
    result = tuple(_order_adapter_hosts(rows))
    global _adapter_cache
    with _adapter_lock:
        _adapter_cache = (time.monotonic(), result)


def _adapter_ipv4s() -> list[str]:
    """真实网卡的 IPv4，WLAN / 以太网排前面；取不到清单时返回空表，交给调用方回落。

    枚举挪到后台线程（与 hostname 那套一致）：CI 冷启动时 Get-NetIPAddress 能跑好几秒，
    在请求线程里同步等会把 /api/pair/info 拖过客户端超时。首次调用最多等
    ADAPTER_FIRST_WAIT 秒，之后只读缓存；缓存过期时先给旧值，后台再刷新。
    """
    global _adapter_thread, _adapter_waited
    if os.name != "nt":
        return []
    with _adapter_lock:
        cached = _adapter_cache
        if cached is not None and time.monotonic() - cached[0] < ADAPTER_CACHE_SECONDS:
            return list(cached[1])
        stale = list(cached[1]) if cached is not None else []
        if _adapter_thread is None or not _adapter_thread.is_alive():
            _adapter_thread = threading.Thread(target=_refresh_adapter_cache, name="lan-adapters", daemon=True)
            _adapter_thread.start()
        thread = _adapter_thread
        wait = not _adapter_waited
        _adapter_waited = True
    if wait:
        thread.join(ADAPTER_FIRST_WAIT)
    with _adapter_lock:
        if _adapter_cache is not None:
            return list(_adapter_cache[1])
    return stale


def _reset_adapter_cache() -> None:
    """测试用：丢掉网卡清单缓存，让下一次 _lan_hosts() 重新枚举。"""
    global _adapter_cache, _adapter_thread, _adapter_waited
    with _adapter_lock:
        _adapter_cache = None
        _adapter_thread = None
        _adapter_waited = False


# hostname 解析走后台线程：macOS / 开了 mDNS 搜索域的机器上 socket.getaddrinfo 可能卡住好几秒，
# 而它是在请求线程里同步调的，卡住就等于前端按钮卡住。只解析一次，结果缓存。
HOSTNAME_LOOKUP_TIMEOUT = 1.0
_hostname_lock = threading.Lock()
_hostname_thread: threading.Thread | None = None
_hostname_waited = False
_hostname_result: tuple[str, ...] = ()


def _resolve_hostname() -> None:
    global _hostname_result
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return
    _hostname_result = tuple(str(info[4][0]) for info in infos)


def _hostname_ipv4s() -> list[str]:
    """本机 hostname 解析出的 IPv4；解析慢时最多等 HOSTNAME_LOOKUP_TIMEOUT 秒，之后不再等。"""
    global _hostname_thread, _hostname_waited
    with _hostname_lock:
        if _hostname_thread is None:
            _hostname_thread = threading.Thread(target=_resolve_hostname, name="lan-hostname", daemon=True)
            _hostname_thread.start()
        thread = _hostname_thread
        wait = not _hostname_waited
        _hostname_waited = True
    if wait:
        thread.join(HOSTNAME_LOOKUP_TIMEOUT)
    if thread.is_alive():
        return []
    return list(_hostname_result)


def _reset_hostname_cache() -> None:
    """测试用：丢掉缓存，让下一次 _lan_hosts() 重新解析。"""
    global _hostname_thread, _hostname_waited, _hostname_result
    with _hostname_lock:
        _hostname_thread = None
        _hostname_waited = False
        _hostname_result = ()


def _lan_hosts() -> list[str]:
    """本机所有可能被手机直连的 IPv4，按更可能是真实局域网的地址排序。"""
    hosts: list[str] = _adapter_ipv4s()
    if not hosts:
        # 枚举不到真实网卡（非 Windows、PowerShell 被挡）时沿用旧的探测逻辑。
        try:
            hosts.append(_lan_ipv4())
        except OSError:
            pass
        hosts.extend(_hostname_ipv4s())
    unique: list[str] = []
    for host in hosts:
        if _is_phone_reachable(host) and host not in unique:
            unique.append(host)
    return unique


def _pair_host_kind(address: str) -> str:
    """给手机配对候选地址分类；这里只按地址段判断，不探测网卡归属。"""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "other"
    if ip.version != 4:
        return "other"
    if ip in ipaddress.ip_network("100.64.0.0/10"):
        return "tailscale"
    if ip in ipaddress.ip_network("192.168.56.0/24") or ip in ipaddress.ip_network("192.168.99.0/24") or ip in ipaddress.ip_network("192.168.122.0/24"):
        return "virtual"
    if ip in ipaddress.ip_network("172.20.10.0/24"):
        return "cellular"
    if ip.is_private and (ip in ipaddress.ip_network("10.0.0.0/8") or ip in ipaddress.ip_network("172.16.0.0/12") or ip in ipaddress.ip_network("192.168.0.0/16")):
        return "lan"
    return "other"


def _pair_candidates(hosts: list[str], port: int, public: str) -> list[dict[str, Any]]:
    """同网 LAN 优先；异地连接依次尝试 Tailscale VPN 和公网 Funnel。"""
    candidates = [{"url": f"http://{host}:{port}", "kind": _pair_host_kind(host), "recommended": False} for host in hosts]
    if public:
        candidates.append({"url": public, "kind": "tailscale", "recommended": False})
    candidates.sort(key=lambda item: (
        0 if item["kind"] == "lan" else
        1 if item["kind"] == "tailscale" and item["url"].startswith("http://") else
        2 if item["url"].startswith("https://") else 3
    ))
    recommended = next((item for item in candidates if item["kind"] == "lan"), None)
    if recommended is None:
        recommended = next((item for item in candidates if item["url"] == public), candidates[0] if candidates else None)
    if recommended:
        recommended["recommended"] = True
    return candidates


def _tailscale_calendar_url(port: int) -> str:
    """Tailscale Funnel 暴露到公网的日历地址（手机在流量或任意 Wi-Fi 下都能订阅）。

    实现只有一份（``tunnel.public_calendar_url``）：整站 "/" 规则与专门的 "/xxx.ics"
    规则都认；没装 Tailscale / 没开 Funnel / 端口对不上时返回空串，调用方退回局域网地址。
    """
    return tunnel.public_calendar_url(port)


TAILSCALE_FUNNEL_PORTS = tunnel.TAILSCALE_FUNNEL_PORTS


# Tailscale 的检测/安装/登录/开通道全在 tunnel.py 里，这里保留同名薄封装：
# 既有的调用点和测试都按 webapp 的名字找它们，实现只有一份。
def _tailscale_executable() -> str:
    """找本机 tailscale.exe；没装返回空串。"""
    return tunnel.tailscale_executable()


def _tailscale_serve_config(executable: str) -> dict[str, Any]:
    """读 `tailscale serve status --json`；跑不起来或不是 JSON 时返回空字典。"""
    return tunnel.serve_config(executable)


def _tailscale_public_app_url(port: int) -> str:
    """手机 App 用的公网地址：整站（页面 + /api/*）都反代到本机这个端口的那条 Funnel 规则。

    和只认 .ics 的日历地址不同，这里必须有一条 "/" 路由，而且那个端口得在 AllowFunnel 里。

    查一次要起 tailscale CLI（本机 2.4 秒），缓存 30 秒；用户刚开通 Funnel 时由
    ``_enable_tailscale_funnel`` 主动失效，不会让他看到旧状态。
    """
    return ttl_cache.get(
        TAILSCALE_URL_CACHE_KEY,
        TAILSCALE_URL_CACHE_SECONDS,
        lambda: tunnel.public_app_url(port),
        cache_empty=True,
    )


def _tailscale_status_hint(executable: str) -> str:
    """Tailscale 没登录 / 没运行时给一句能看懂的原因；状态正常或查不到时返回空串。

    同样要起 CLI（本机 2.4 秒），跟 ``_tailscale_public_app_url`` 一起缓存 30 秒——
    总览一次请求里这两句都要问，不缓存就是两次 2.4 秒。
    """
    return ttl_cache.get(
        TAILSCALE_HINT_CACHE_KEY,
        TAILSCALE_URL_CACHE_SECONDS,
        lambda: tunnel.status_hint(executable),
        cache_empty=True,
    )


def _enable_tailscale_funnel(port: int, public_port: int = 8443) -> dict[str, Any]:
    """电脑上点一下就整站开到公网（Tailscale Funnel，免费，不用买服务器）。"""
    try:
        return tunnel.enable_funnel(port, public_port)
    finally:
        # 刚开通就别让页面读到 30 秒前的「未开通」。
        _clear_tailscale_cache()


_funnel_setup_lock = threading.Lock()
_funnel_setups: dict[int, tunnel.FunnelSetup] = {}


def _funnel_setup(port: int) -> tunnel.FunnelSetup:
    """同一端口只保留一个开通任务，页面轮询它的 snapshot()。"""
    with _funnel_setup_lock:
        setup = _funnel_setups.get(port)
        if setup is None:
            setup = tunnel.FunnelSetup(port)
            _funnel_setups[port] = setup
        return setup


# ---- 四道门向导（/api/onboarding/next|run|status）----
# 步骤顺序固定：qq → napcat → tailscale → firewall → push（推送可跳过）。
ONBOARDING_ORDER = ("qq", "napcat", "tailscale", "firewall", "push")
# key → (title, body, reason, action, action_label, blocked, skippable)
ONBOARDING_STEPS: dict[str, tuple[str, str, str, str, str, bool, bool]] = {
    "qq": ("连上 QQ", "先登录 QQ，后面的群消息才有来源", "还没检测到电脑版 QQ 在线", "connect_qq", "去扫码登录", True, False),
    "napcat": ("装 QQ 引擎", "NapCat 负责把 QQ 消息转成本机接口", "QQ 引擎还没装好，或者装了没在跑", "install_napcat", "一键安装", False, False),
    "tailscale": ("开通公网入口", "手机用流量时也能连上来（同一 Wi-Fi 可以不装）", "公网入口还没开通，手机换了网络就连不上", "open_funnel", "一键开通", False, True),
    "firewall": ("放行防火墙", "Windows 防火墙默认会挡住手机访问这个端口", "这个端口还没在 Windows 防火墙放行", "allow_firewall", "一键放行", False, False),
    "push": ("配推送通道", "截止提醒要发到手机；这一步可以跳过", "还没配推送通道，提醒只在网页里看得到", "configure_push", "去设置（可跳过）", True, True),
}
# 同一个动作的 running 超过这么久就当它卡死了，允许再点一次（否则出错后永远点不动）。
ONBOARDING_RUNNING_TIMEOUT_SECONDS = 600.0
_onboarding_lock = threading.Lock()
_onboarding_run: dict[str, Any] = {"state": "idle", "action": "", "message": "", "at": ""}


def _onboarding_skip_keys(values: list[str] | None = None) -> set[str]:
    """把 ?skip=tailscale,push（也可以重复给多次）解析成步骤集合。

    只认 ONBOARDING_ORDER 里的 key：跳过是前端记在地址里的，手改地址塞别的 key 不生效。
    """
    keys: set[str] = set()
    for raw in values or []:
        for part in str(raw).split(","):
            key = part.strip().lower()
            if key in ONBOARDING_ORDER:
                keys.add(key)
    return keys


def _onboarding_set(state: str, action: str, message: str = "") -> None:
    with _onboarding_lock:
        _onboarding_run.update(
            {
                "state": str(state),
                "action": str(action),
                "message": str(message or ""),
                "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
        )


def _onboarding_begin(action: str, message: str) -> dict[str, Any] | None:
    """原子地开始一个动作：同一动作已在跑就返回它的当前状态，让调用方直接回给前端。

    不做这件事的话，用户连点两下会起两个下载线程，两个都写同一份四周状态。
    """
    now = time.monotonic()
    with _onboarding_lock:
        if (
            _onboarding_run.get("state") == "running"
            and _onboarding_run.get("action") == action
            and now - float(_onboarding_run.get("started") or 0.0) < ONBOARDING_RUNNING_TIMEOUT_SECONDS
        ):
            return dict(_onboarding_run)
        _onboarding_run.update(
            {
                "state": "running",
                "action": str(action),
                "message": str(message or ""),
                "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "started": now,
            }
        )
        return None


def _run_onboarding_napcat(settings: Any) -> None:
    """后台装 NapCat：安装要下载几十 MB，绝不能挂在这次请求里。"""
    try:
        result = napcat_admin.install(settings) if napcat_admin is not None else {"ok": False, "error": "一键接入模块不可用"}
    except Exception as error:  # noqa: BLE001
        result = {"ok": False, "error": str(error)}
    if result.get("ok"):
        _onboarding_set("done", "install_napcat", str(result.get("version") or result.get("root") or "已装好"))
    else:
        _onboarding_set("failed", "install_napcat", str(result.get("error") or "安装失败"))


def _tunnel_status_payload(port: int) -> dict[str, Any]:
    """公网入口的当前状态：已开通就给出地址，没开通就报状态机走到哪一步。"""
    url = _tailscale_public_app_url(port)
    if url:
        return {
            "ok": True,
            "state": "ready",
            "message": "公网入口已开通，手机用移动网络也能连。",
            "url": url,
            "auth_url": "",
            "retryable": False,
            "installed": True,
        }
    payload = _funnel_setup(port).snapshot()
    if str(payload.get("state") or "") not in {"idle", "installing", "running"}:
        # 开通任务刚有结果（ready/failed），别让页面继续读缓存的旧地址。
        _clear_tailscale_cache()
    payload["ok"] = True
    payload["installed"] = bool(_tailscale_executable())
    return payload


FIREWALL_REQUEST_NAME = "firewall.request"
FIREWALL_RESULT_NAME = "firewall.result"
# 托盘每一次 tick 都会看请求文件；超过这个时间还没人消费，就当托盘没在跑。
FIREWALL_REQUEST_TIMEOUT_SECONDS = 90.0


def _firewall_paths(settings: Any) -> tuple[Path, Path]:
    data_dir = Path(getattr(settings, "data_dir", ".") or Path("."))
    return data_dir / FIREWALL_REQUEST_NAME, data_dir / FIREWALL_RESULT_NAME


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_firewall_request(settings: Any, port: int) -> str:
    """把「放行端口」的请求交给托盘执行（后台服务自己弹 UAC 会挂住请求线程）。

    原子写：先写临时文件再 replace，托盘不会读到半截 JSON。返回 request_id。
    """
    request_path, _ = _firewall_paths(settings)
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_id = f"{int(port)}-{int(time.time() * 1000)}"
    payload = {
        "port": int(port),
        "requested_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "request_id": request_id,
    }
    # 临时名带上 pid+线程：两个标签页同时点，不能撞同一个 .tmp 让 os.replace 抛 FileNotFoundError。
    tmp_path = request_path.with_name(f"{request_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, request_path)
    return request_id


def _firewall_status_payload(settings: Any) -> dict[str, Any]:
    """页面轮询用：unknown / pending / running / granted / failed / unavailable。

    ``checked`` 表示「本机到底读没读到防火墙规则清单」：读不到时不能把「没查成」
    说成「没放行」。非提权进程读不到 netsh 输出，要等托盘点过「一键放行」后
    提权探测写下 ``data/firewall.rules.txt`` 才读得到。
    """
    port = int(getattr(settings, "web_port", 0) or 0)
    request_path, result_path = _firewall_paths(settings)
    probe = _firewall_allows(port, getattr(settings, "data_dir", None))
    checked = bool(probe.get("checked"))
    base: dict[str, Any] = {"ok": True, "port": port, "state": "unknown", "message": "", "at": "", "checked": checked}
    if probe.get("allowed"):
        return {**base, "state": "granted", "message": "端口已放行"}
    request = _read_json_file(request_path)
    try:
        request_mtime = request_path.stat().st_mtime if request else 0.0
    except OSError:
        request_mtime = 0.0
    result = _read_json_file(result_path)
    if result and int(result.get("port") or 0) == port:
        try:
            result_mtime = result_path.stat().st_mtime
        except OSError:
            result_mtime = 0.0
        # 早于本次请求的结果属于上一次点击（典型：上次失败后 result 常驻），不能拿它回答这一次。
        if not request or result_mtime >= request_mtime:
            raw_state = str(result.get("state") or "")
            if raw_state == "running":
                state = "running"
            elif bool(result.get("ok")):
                state = "granted"
            else:
                state = "failed"
            if "checked" in result:
                checked = bool(result.get("checked"))
            return {
                **base,
                "checked": checked,
                "state": state,
                "message": str(result.get("message") or ""),
                "at": str(result.get("at") or ""),
            }
    if request:
        age = time.time() - request_mtime
        if age <= FIREWALL_REQUEST_TIMEOUT_SECONDS:
            return {**base, "state": "pending", "message": "正在请求管理员权限，请看托盘弹窗"}
        return {**base, "state": "unavailable", "message": FIREWALL_NEEDS_TRAY}
    if not checked:
        return {
            **base,
            "message": "有没有放行要管理员权限才查得到：没提权的进程读不到本机防火墙规则，点「一键放行」会顺便查出来",
        }
    return {**base, "message": "本机防火墙还没放行端口 %d" % port}



def _host_actions_disabled() -> bool:
    """会改本机网络/系统的写操作是否被整体关掉（测试或只读部署用）。"""
    return (
        os.environ.get("NH_TEST_MODE", "").strip() in {"1", "true", "yes"}
        or os.environ.get("QQ_DIGEST_HOST_ACTIONS", "").strip().casefold() in {"off", "0", "false", "no"}
    )


def _host_action_audit(settings: Any, action: str, port: int, client: str, result: str) -> None:
    """记一行宿主操作审计（谁、什么时候、做了什么）。写日志失败绝不能影响接口结果。"""
    try:
        log_dir = Path(getattr(settings, "log_dir", "") or (Path(getattr(settings, "data_dir", ".")) / "logs"))
        log_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "action": action,
                "port": int(port or 0),
                "client": str(client or ""),
                "result": str(result or ""),
            },
            ensure_ascii=False,
        )
        with (log_dir / "host-actions.log").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:  # noqa: BLE001 - 审计失败不许影响业务返回
        LOGGER.warning("写宿主操作审计失败：%s", action, exc_info=True)



FIREWALL_LOOKUP_TIMEOUT = 3.0
FIREWALL_CACHE_SECONDS = 60.0


def _firewall_rule_command(port: int) -> str:
    """薄封装：命令生成统一在 host_actions（与旧实现逐字一致）。"""
    return host_actions.firewall_allow_command(port)


def _firewall_inbound_rules(data_dir: Any = None) -> tuple[dict[str, str], ...]:
    """薄封装：netsh 查询与解析统一在 host_actions（只读、不提权）。"""
    return host_actions.firewall_inbound_rules(data_dir)


def _firewall_allows(port: int, data_dir: Any = None) -> dict[str, Any]:
    """查本机有没有放行该 TCP 端口入站的防火墙规则。只读、不提权、不改设置。"""
    return host_actions.firewall_allows(port, data_dir)


def _reset_firewall_cache() -> None:
    """丢掉防火墙检测缓存（规则刚被改过时调用）。"""
    host_actions.reset_firewall_cache()


def _pair_help(base: str) -> dict[str, Any]:
    return {
        "base": base,
        "steps": [
            "同一个 Wi-Fi 下优先用局域网地址；不需要开通公网入口。",
            "异地连接可让电脑和手机加入同一个 Tailscale VPN，再选择 Tailscale 地址；也可开通公网入口后选择 Funnel 地址。",
            "手机装好群务台 App，打开后它自己会显示 6 位数字；在电脑上核对并点「允许」。",
        ],
        "no_public_hint": "同一 Wi-Fi 下直接用局域网地址。异地可让电脑和手机加入同一个 Tailscale VPN；或安装 Tailscale 后开通公网 Funnel（会让服务可从公网访问）。",
    }


def _pair_qr_errors(settings: Any, token: str, base: str, public_url: str = "") -> list[str]:
    errors: list[str] = []
    if not token:
        errors.append("本机还没有访问令牌。请设置 QQ_DIGEST_WEB_TOKEN 并重启服务，再扫码配对。")
    if not base:
        errors.append("没有检测到可供手机访问的地址。")
        return errors
    if public_url and base.rstrip("/") == public_url.rstrip("/"):
        return errors

    host = urllib.parse.urlsplit(base).hostname or ""
    if host.lower().rstrip(".") == "localhost":
        errors.append("二维码地址指向本机回环地址，手机无法访问。")
        return errors
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and address.is_loopback:
        errors.append("二维码地址指向本机回环地址，手机无法访问。")
        return errors

    # A public DNS name may be a Funnel or reverse proxy; only compare literal IPs.
    bind_host = str(getattr(settings, "web_host", "") or "").lower().rstrip(".")
    try:
        listener = ipaddress.ip_address(bind_host)
    except ValueError:
        listener = None
    listener_is_loopback = bind_host == "localhost" or bool(listener is not None and listener.is_loopback)
    if listener_is_loopback and address is not None:
        errors.append("服务只监听本机回环地址，手机连不上你给它的这个地址。把 QQ_DIGEST_WEB_HOST 改成 0.0.0.0 再重启服务。")
    elif listener is not None and not listener.is_unspecified and address is not None and listener != address:
        errors.append("手机里的地址和服务实际监听的地址对不上。核对 QQ_DIGEST_WEB_HOST 和手机上填的地址。")
    return errors


def _is_virtual_host(host: str) -> bool:
    from noticehub.application.calendar import is_virtual_host

    return is_virtual_host(host)


def _calendar_host_priority(host: str) -> tuple[int, str]:
    from noticehub.application.calendar import host_priority

    return host_priority(host)


def _primary_calendar_url(calendars: list[dict[str, Any]]) -> str:
    from noticehub.application.calendar import primary_calendar_url

    return primary_calendar_url(calendars)


def _advertised_calendars(port: int, token: str) -> list[dict[str, Any]]:
    """探测仍在本模块；排序和推荐在用例里。"""
    from noticehub.application.calendar import assemble_calendars

    return assemble_calendars(_lan_hosts(), _tailscale_calendar_url(port), port=port, token=token)


def _job_snapshot(kind: str) -> dict[str, Any]:
    with _JOB_LOCK:
        snapshot = {key: _JOB_STATE[kind][key] for key in ("running", "stage", "done", "total", "note", "result")}
        snapshot["detail"] = dict(_JOB_STATE[kind].get("detail") or {})
        return snapshot


def _install_note(stage: str, detail: dict[str, Any]) -> str:
    """把安装阶段翻成人话；数字拿不到就不显示，别编。"""
    def mb(value: Any) -> str:
        try:
            return "%.1f MB" % (int(value) / (1024 * 1024))
        except (TypeError, ValueError):
            return ""

    if stage == "download":
        got = mb(detail.get("downloaded_bytes"))
        whole = mb(detail.get("total_bytes"))
        if got and whole:
            return f"正在下载 NapCat：{got} / {whole}"
        if got:
            return f"正在下载 NapCat：已下载 {got}"
        return "正在下载 NapCat"
    if stage == "verify":
        return "正在校验安装包指纹"
    if stage == "extract":
        done = detail.get("extracted")
        whole = detail.get("entries")
        if isinstance(done, int) and isinstance(whole, int) and whole > 0:
            return f"正在解压 NapCat：{done}/{whole} 个文件"
        return "正在解压 NapCat"
    if stage == "install":
        return "正在安装到数据目录"
    if stage == "complete":
        return "安装完成"
    if stage == "error":
        return "安装失败"
    return "正在准备安装"


def _job_progress_detail(kind: str, **fields: Any) -> None:
    """安装类任务专用：除了阶段，还要记已下载字节 / 已解压条目这些数字。"""
    with _JOB_LOCK:
        state = _JOB_STATE[kind]
        if not state["running"]:
            return
        detail = state.setdefault("detail", {})
        for key in ("downloaded_bytes", "total_bytes", "extracted", "entries"):
            if key in fields:
                detail[key] = fields[key]
        stage = str(fields.get("stage") or "") or str(state["stage"])
        state["stage"] = stage
        if detail.get("downloaded_bytes") is not None:
            state["done"] = max(0, int(detail["downloaded_bytes"] or 0))
            state["total"] = max(0, int(detail.get("total_bytes") or 0))
        elif detail.get("extracted") is not None:
            state["done"] = max(0, int(detail["extracted"] or 0))
            state["total"] = max(0, int(detail.get("entries") or 0))
        state["note"] = _install_note(stage, detail)


def _job_progress(kind: str, *values: Any) -> None:
    if len(values) == 4:
        stage, done, total, note = values
    elif len(values) == 2:
        done, total = values
        stage, note = "classifying", "正在判定已回溯消息"
    else:
        stage, done, total, note = "working", 0, 0, "正在处理"
    with _JOB_LOCK:
        state = _JOB_STATE[kind]
        if state["running"]:
            state.update(stage=str(stage or "working"), done=max(0, int(done or 0)), total=max(0, int(total or 0)), note=str(note or ""))


def _run_job(kind: str, worker: Any, cancel_event: threading.Event) -> None:
    try:
        result = worker(lambda *values: _job_progress(kind, *values), cancel_event.is_set)
        if not isinstance(result, dict):
            result = {"ok": False, "error": "后台任务返回格式无效"}
    except Exception as error:  # noqa: BLE001
        LOGGER.exception("后台%s任务失败", kind)
        result = {"ok": False, "error": str(error) or "后台任务失败"}
    with _JOB_LOCK:
        state = _JOB_STATE[kind]
        cancelled = cancel_event.is_set() or bool(result.get("cancelled"))
        if cancelled:
            result = {"ok": False, "cancelled": True, "error": "已取消"}
        state.update(
            running=False,
            stage="cancelled" if cancelled else ("complete" if result.get("ok") else "error"),
            note="已取消" if cancelled else ("处理完成" if result.get("ok") else str(result.get("error") or "处理失败")),
            result=result,
        )
        if _JOB_STATE["active"] == kind:
            _JOB_STATE["active"] = None
            _JOB_STATE["cancel"] = None


def _start_job(kind: str, worker: Any) -> tuple[bool, str]:
    with _JOB_LOCK:
        active = _JOB_STATE["active"]
        if active:
            return False, str(active)
        cancel_event = threading.Event()
        _JOB_STATE["active"] = kind
        _JOB_STATE["cancel"] = cancel_event
        _JOB_STATE[kind].update(running=True, stage="starting", done=0, total=0, note="正在启动", result=None)
        thread = threading.Thread(target=_run_job, args=(kind, worker, cancel_event), name="web-" + kind, daemon=True)
        try:
            thread.start()
        except Exception as error:  # noqa: BLE001
            _JOB_STATE["active"] = None
            _JOB_STATE["cancel"] = None
            _JOB_STATE[kind].update(running=False, stage="error", note="无法启动后台任务", result={"ok": False, "error": str(error)})
            return False, "无法启动后台任务"
        return True, ""


def _parse_groups(value: Any) -> list[str]:
    from noticehub.application.jobs import parse_groups

    return parse_groups(value)


def _parse_day(value: Any, *, end: bool = False) -> dt.datetime | None:
    from noticehub.application.jobs import parse_day

    return parse_day(value, end=end)


MANIFEST_JSON = json.dumps(
    {
        "name": "群务台",
        "short_name": "群务台",
        "description": "QQ 群通知摘要与个人待办台",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#0b0d12",
        "theme_color": "#0b0d12",
        "orientation": "portrait",
        "icons": [
            {
                "src": "/icon.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any maskable",
            }
        ],
    },
    ensure_ascii=False,
)

ICON_SVG = """<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 512 512\">
  <defs>
    <linearGradient id=\"g\" x1=\"0\" y1=\"0\" x2=\"1\" y2=\"1\">
      <stop offset=\"0\" stop-color=\"#12695b\"/>
      <stop offset=\"1\" stop-color=\"#173b34\"/>
    </linearGradient>
  </defs>
  <rect width=\"512\" height=\"512\" rx=\"112\" fill=\"#0f2a24\"/>
  <rect x=\"64\" y=\"64\" width=\"384\" height=\"384\" rx=\"96\" fill=\"url(#g)\"/>
  <path d=\"M168 264l58 58 122-142\" fill=\"none\" stroke=\"#fff\" stroke-width=\"36\" stroke-linecap=\"round\" stroke-linejoin=\"round\"/>
</svg>"""

SERVICE_WORKER_JS = """const CACHE = 'qq-digest-shell-v6';
const ASSETS = ['/manifest.webmanifest', '/icon.svg'];
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return;
  event.respondWith(fetch(event.request).then(response => {
    if (response.ok && (url.pathname === '/' || ASSETS.includes(url.pathname))) {
      const copy = response.clone();
      caches.open(CACHE).then(cache => cache.put(event.request, copy));
    }
    return response;
  }).catch(() => caches.match(event.request).then(cached => cached || caches.match('/'))));
});
"""
CALENDAR_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>群务台 · 月历</title>
<style>:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;padding:16px;background:#0a0c12;color:#f3f5fa;font:15px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}main{max-width:680px;margin:auto}header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}h1{font-size:20px;margin:0}.nav{display:flex;gap:6px}button{border:1px solid #303747;background:#171c29;color:#f3f5fa;border-radius:6px;padding:7px 11px;font:inherit;cursor:pointer}.grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:4px}.weekday{text-align:center;color:#9aa4b5;font-size:12px;padding:4px}.day{min-height:76px;padding:6px;background:#141925;border:1px solid #252c3b;border-radius:6px;overflow:hidden}.day.muted{opacity:.42}.day.today{border-color:#5b8cff}.num{font-size:12px;color:#b9c4d8}.event{display:block;margin-top:4px;padding:3px 4px;background:#26385e;color:#dbe7ff;border-radius:4px;font-size:11px;line-height:1.3;overflow-wrap:anywhere}@media(max-width:420px){body{padding:10px}.day{min-height:62px;padding:4px}.event{font-size:10px}.num{font-size:11px}}</style></head>
<body><main><header><h1 id="title">月历</h1><div class="nav"><button id="prev" type="button">上一月</button><button id="next" type="button">下一月</button></div></header><div id="calendar" class="grid"></div></main>
<script>(function(){var KEY='qq_digest_token';var params=new URLSearchParams(location.search);if(params.get('token'))localStorage.setItem(KEY,params.get('token'));var token=localStorage.getItem(KEY)||'';var cursor=new Date();cursor.setDate(1);var tasks=[];var names=['日','一','二','三','四','五','六'];function esc(s){return String(s||'').replace(/[&<>]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c]});}function render(){var y=cursor.getFullYear(),m=cursor.getMonth(),root=document.getElementById('calendar');document.getElementById('title').textContent=y+'年'+(m+1)+'月';root.innerHTML='';names.forEach(function(n){var h=document.createElement('div');h.className='weekday';h.textContent=n;root.appendChild(h);});var first=new Date(y,m,1).getDay(),count=new Date(y,m+1,0).getDate(),prevCount=new Date(y,m,0).getDate();for(var i=0;i<42;i++){var d=i-first+1, date=new Date(y,m,d), cell=document.createElement('div');cell.className='day'+(date.getMonth()!==m?' muted':'');if(date.toDateString()===new Date().toDateString())cell.className+=' today';cell.innerHTML='<div class="num">'+date.getDate()+'</div>';tasks.forEach(function(t){if(!t.deadline)return;var raw=String(t.deadline), key=raw.slice(0,10);if(key===date.getFullYear()+'-'+String(date.getMonth()+1).padStart(2,'0')+'-'+String(date.getDate()).padStart(2,'0')){var e=document.createElement('div');e.className='event';e.textContent=t.summary||'未命名任务';cell.appendChild(e);}});root.appendChild(cell);}}document.getElementById('prev').onclick=function(){cursor.setMonth(cursor.getMonth()-1);render();};document.getElementById('next').onclick=function(){cursor.setMonth(cursor.getMonth()+1);render();};fetch('/api/tasks',{headers:{'X-Token':token}}).then(function(r){return r.json();}).then(function(data){tasks=[].concat(data.today||[],data.week||[],data.later||[],data.done||[],data.candidates||[]);render();}).catch(function(){render();});})();</script></body></html>"""
SETUP_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>群务台 · 接入向导</title><style>body{margin:0;padding:18px;background:#0a0c12;color:#f3f5fa;font:15px sans-serif}main{max-width:600px;margin:auto}.card{padding:16px;margin:12px 0;background:#171c29;border:1px solid #303747;border-radius:10px}img{display:block;width:240px;height:240px;object-fit:contain;margin:auto;background:#fff}button{padding:8px 12px;margin:4px;border-radius:7px;border:1px solid #475569;background:#26385e;color:white}label{display:block;padding:10px;border-bottom:1px solid #303747}#groups{margin-top:10px}#go{padding:12px 22px;font-size:16px;background:#2563eb;border:0}#steps div{padding:4px 0;font-size:13px;line-height:1.5}</style></head><body><main><h1>QQ 接入向导</h1><div class="card"><button id="go" type="button">一键接入</button><div id="steps"></div></div><div id="status" class="card">正在检查 QQ 登录状态…</div><div id="qrbox" class="card"><p>用 QQ 主号扫码</p><img id="qr" alt="登录二维码"></div><div id="groupbox" class="card" hidden><div><button id="all" type="button">全选</button><button id="none" type="button">全不选</button></div><div id="groups"></div><button id="save" type="button">保存订阅</button><p id="result"></p><a href="/calendar">打开月历</a></div></main><script>(function(){var selected={},names={},loaded=false;var token=new URLSearchParams(location.search).get('token')||localStorage.getItem('qq_digest_token')||'';function api(path,opt){opt=opt||{};opt.headers=Object.assign({'X-Token':token},opt.headers||{});return fetch(path,opt).then(function(r){return r.json().then(function(x){if(!r.ok)throw Error(x.error||'请求失败');return x;});});}function qrSrc(){return '/api/napcat/qrcode?token='+encodeURIComponent(token)+'&t='+Date.now();}document.getElementById('qr').src=qrSrc();function run(){var go=document.getElementById('go'),box=document.getElementById('steps');go.disabled=true;go.textContent='正在接入…';box.innerHTML='';api('/api/napcat/autosetup',{method:'POST'}).then(function(d){(d.steps||[]).forEach(function(s){var p=document.createElement('div');p.textContent=(s.ok?'✓ ':'✗ ')+s.name+'：'+s.detail;box.appendChild(p);});if(d.qrcode_path){document.getElementById('qr').src=qrSrc();}if(!d.ok){var e=document.createElement('div');e.textContent='未完成：'+(d.error||'');box.appendChild(e);}check();}).catch(function(e){box.textContent='接入失败：'+e.message;}).then(function(){go.disabled=false;go.textContent='一键接入';});}document.getElementById('go').addEventListener('click',run);var lastQrStamp=null;function check(){api('/api/napcat/status').then(function(s){var ok=s.ok&&s.online;document.getElementById('status').textContent=ok?'已登录：'+(s.nickname||'')+'（'+(s.user_id||'')+'）':'未登录，请扫码';document.getElementById('qrbox').hidden=ok;document.getElementById('groupbox').hidden=!ok;if(!ok){var st=String(s.qr_stamp||'');if(st!==lastQrStamp){lastQrStamp=st;document.getElementById('qr').src=qrSrc();}}if(ok&&!loaded)loadGroups();}).catch(function(e){document.getElementById('status').textContent='连接 NapCat 失败：'+e.message;});}function loadGroups(){api('/api/napcat/groups').then(function(d){var root=document.getElementById('groups');root.innerHTML='';d.groups.forEach(function(g){var id=String(g.group_id);names[id]=g.name||'';selected[id]=!!g.selected;var l=document.createElement('label');var c=document.createElement('input');c.type='checkbox';c.value=id;c.checked=!!selected[id];c.onchange=function(){selected[id]=c.checked;};l.appendChild(c);l.appendChild(document.createTextNode(' '+g.name+'（'+id+'）'));root.appendChild(l);});loaded=true;});}document.getElementById('all').onclick=function(){document.querySelectorAll('#groups input').forEach(function(c){c.checked=true;selected[c.value]=true;});};document.getElementById('none').onclick=function(){document.querySelectorAll('#groups input').forEach(function(c){c.checked=false;selected[c.value]=false;});};/* api('/api/subscriptions', ...).catch(function(e){ result.textContent='保存失败'; }) */
document.getElementById('save').onclick=function(){var groups=Object.keys(selected).filter(function(id){return selected[id];});api('/api/subscriptions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({groups:groups,aliases:groups.reduce(function(a,id){a[id]=names[id]||'';return a;},{})})}).then(function(){document.getElementById('result').textContent='已保存并立即生效';}).catch(function(e){document.getElementById('result').textContent='保存失败：'+e.message;});};setInterval(function(){check();},5000);check();})();</script></body></html>"""
PAGE_HTML = (Path(__file__).parent / "web_static" / "page.html").read_text(encoding="utf-8")


from noticehub.application.tasks import group_tasks, list_task_board, overview, task_stats, wechat_group_label


def snooze_default_until(settings: Settings, now: dt.datetime) -> str:
    from noticehub.application.tasks import snooze_default_until as _snooze_default_until

    return _snooze_default_until(settings, now)


# 设置页可写的推送通道键（前端字段名 → .env 变量名）。




# 设置页可写的「AI 摘要」键（前端字段名 → .env 变量名）。
_LLM_SETTING_ENV_KEYS = {
    "api_key": "QQ_DIGEST_LLM_API_KEY",
    "endpoint": "QQ_DIGEST_LLM_ENDPOINT",
    "model": "QQ_DIGEST_LLM_MODEL",
}

# 预设服务商：用户只选名字，接口地址与模型名由这里替他填好。
# needs_key 为假表示不用钥匙（本机模型）；vision 为真表示这家能看图片。
LLM_PROVIDERS: list[dict[str, Any]] = [
    {
        "id": "deepseek",
        "name": "DeepSeek（推荐，最便宜）",
        "hint": "按量付费：输入约 ¥1、输出约 ¥4 每百万字，一个月通常不到一元钱；能读图片。",
        "endpoint": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-flash",
        "vision_model": "deepseek-flash",
        "key_url": "https://platform.deepseek.com/api_keys",
        "key_steps": [
            "打开 platform.deepseek.com，用手机号注册并登录",
            "左侧点「API keys」，再点「创建 API key」",
            "复制弹出的那一串 sk- 开头的字符，粘到下面的「钥匙」里",
        ],
        "needs_key": True,
    },
    {
        "id": "zhipu",
        "name": "智谱 GLM（免费）",
        "hint": "glm-4.7-flash 和能看图的 glm-4.6v-flash 都是 ¥0，长期免费，注册就能用。",
        "endpoint": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "model": "glm-4.7-flash",
        "vision_model": "glm-4.6v-flash",
        "key_url": "https://open.bigmodel.cn/usercenter/apikeys",
        "key_steps": [
            "打开 open.bigmodel.cn，用手机号注册并登录",
            "进「API Keys」页面，复制默认那串钥匙",
            "粘到下面；模型名已经替你填好了",
        ],
        "needs_key": True,
    },
    {
        "id": "aliyun_bailian",
        "name": "阿里云百炼（通义千问，送 100 万字）",
        "hint": "新人送 100 万字额度（90 天内有效，仅北京地域），之后约 ¥0.8/百万字；读图能力最好。",
        "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "model": "qwen3.8-flash",
        "vision_model": "qwen3.8-flash",
        "key_url": "https://bailian.console.aliyun.com/?apiKey=1",
        "key_steps": [
            "打开阿里云百炼控制台，用支付宝或淘宝账号登录",
            "点右上角「API-KEY」→「创建我的 API-KEY」",
            "复制 sk- 开头的字符，粘到下面",
        ],
        "needs_key": True,
    },
    {
        "id": "siliconflow",
        "name": "硅基流动（免费模型）",
        "hint": "模型广场里有标价 ¥0 的模型（GLM-5.3-Flash），免费还能读图片。",
        "endpoint": "https://api.siliconflow.cn/v1/chat/completions",
        "model": "zai-org/GLM-5.3-Flash",
        "vision_model": "zai-org/GLM-5.3-Flash",
        "key_url": "https://cloud.siliconflow.cn/account/ak",
        "key_steps": [
            "打开 cloud.siliconflow.cn，注册并登录",
            "左侧「API 密钥」→「新建 API 密钥」",
            "复制 sk- 开头的字符，粘到下面",
        ],
        "needs_key": True,
    },
    {
        "id": "moonshot",
        "name": "月之暗面 Kimi",
        "hint": "按量付费（约 ¥1.1/百万字输入），中文长文本理解好。",
        "endpoint": "https://api.moonshot.cn/v1/chat/completions",
        "model": "kimi-k2.6",
        "vision_model": "kimi-k2.6",
        "key_url": "https://platform.kimi.com/",
        "key_steps": [
            "打开 platform.kimi.com，注册并登录",
            "进「API Key 管理」，新建一个",
            "复制 sk- 开头的字符，粘到下面",
        ],
        "needs_key": True,
    },
    {
        "id": "ollama",
        "name": "本机跑的 Ollama（免费，但要显卡）",
        "hint": "完全免费、断网也能用，只花电费；要有显卡，速度比云服务慢。",
        "endpoint": "http://127.0.0.1:11434/v1/chat/completions",
        "model": "qwen3:8b",
        "vision_model": "",
        "key_url": "https://ollama.com/download",
        "key_steps": [
            "先装 Ollama（点下面链接），装完在命令行执行 ollama pull qwen3:8b",
            "本机模型不用钥匙，直接点「保存并测试」",
        ],
        "needs_key": False,
    },
    {
        "id": "custom",
        "name": "其它（自己填接口地址）",
        "hint": "任何大模型服务都行，只要它给你的地址以 /v1/chat/completions 结尾。",
        "endpoint": "",
        "model": "",
        "vision_model": "",
        "key_url": "",
        "key_steps": [],
        "needs_key": True,
    },
    {
        "id": "off",
        "name": "不接 AI（只记录原始消息）",
        "hint": "不调用任何模型，群里消息只归档、不生成摘要。",
        "endpoint": "",
        "model": "",
        "vision_model": "",
        "key_url": "",
        "key_steps": [],
        "needs_key": False,
    },
]
_LLM_PROVIDER_BY_ID = {str(item["id"]): item for item in LLM_PROVIDERS}


def _llm_provider(provider_id: str) -> dict[str, Any] | None:
    return _LLM_PROVIDER_BY_ID.get(str(provider_id))


def _llm_provider_id(settings: Settings) -> str:
    """按当前接口地址反推用户选的是哪一家；认不出来就是 custom。"""
    endpoint = str(settings.dashscope_endpoint or "").strip().lower().rstrip("/")
    for item in LLM_PROVIDERS:
        preset = str(item.get("endpoint") or "").strip().lower().rstrip("/")
        if preset and preset == endpoint:
            return str(item["id"])
    return "custom"






# 微信（实验性）：只在这里读群名。密钥、salt、内存字节一律不进日志与响应。
_WECHAT_READ_STATUS: dict[str, Any] = {"at": "", "seconds": 0.0, "error": "", "groups": 0}

# WEB-4: pairing issues a 6-digit code and secrets.token_urlsafe(24) (32 chars). Anything
# longer on the unauthenticated /api/pair/status route is junk, so cap it before comparison.
_PAIR_CODE_MAX = 16
_PAIR_SECRET_MAX = 128


def _redact_secrets(text: str) -> str:
    """把请求行/查询串里的 token=…、secret=…、password=… 换成 ***（WEB-2）。

    日志会被人贴到 issue、聊天窗口或工单里；令牌一旦泄漏，别人就能直接接管待办台。
    保留键名和非敏感参数（不打印长度），只把值抹掉。
    """
    return re.sub(
        r"((?:token|secret|password|passwd|pwd|apikey|api_key)=)([^&\s\"]+)",
        r"\1***",
        str(text),
        flags=re.IGNORECASE,
    )


def _message_samples(store: Store, source: str, group_ids: list[str], *, limit: int = 3) -> dict[str, list[str]]:
    ids = list(dict.fromkeys(str(group_id) for group_id in group_ids if str(group_id)))
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    query = f"""
        SELECT group_id, content FROM (
            SELECT group_id, content,
                   ROW_NUMBER() OVER (PARTITION BY group_id ORDER BY received_at DESC, msg_id DESC) AS position
            FROM messages WHERE source=? AND group_id IN ({placeholders})
        ) WHERE position <= ? ORDER BY group_id, position
    """
    uri = store.path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(query, (source, *ids, max(1, int(limit)))).fetchall()
    finally:
        connection.close()
    samples: dict[str, list[str]] = {}
    for row in rows:
        content = str(row["content"] or "").strip()
        if content:
            samples.setdefault(str(row["group_id"]), []).append(content[:500])
    return samples


def _suggest_external_groups(settings: Settings, store: Store, source: str, groups: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str, str]:
    if group_suggest is None:
        result = {"groups": [], "source": "heuristic"}
        error = "推荐模块不可用，已按关键词分类"
    else:
        try:
            ids = [str((row.get("cid") if source == "dingtalk" else row.get("id")) or "") for row in groups]
            samples = _message_samples(store, source, ids)
            suggest_settings = settings
            if source == "dingtalk" and settings.llm_active:
                suggest_settings = copy.copy(settings)
                suggest_settings.llm_timeout = min(
                    int(settings.llm_timeout), DINGTALK_GROUP_LLM_TIMEOUT_SECONDS
                )
            result = group_suggest.suggest_for_source(suggest_settings, source, groups, sample=samples)
            error = "AI 分类暂不可用，已按关键词分类" if result.get("source") == "heuristic" and settings.llm_active and result.get("error") else ""
        except Exception:
            result = group_suggest.heuristic_for_source(source, groups)
            error = "推荐暂不可用，已按关键词分类"
    by_id = {str(item.get("group_id") or ""): item for item in result.get("groups", [])}
    merged = []
    for row in groups:
        item = dict(row)
        group_id = str((row.get("cid") if source == "dingtalk" else row.get("id")) or "")
        suggestion = by_id.get(group_id, {})
        item.update({
            "category": str(suggestion.get("category") or "other"),
            "suggested": bool(suggestion.get("suggested")),
            "reason": str(suggestion.get("reason") or "未识别"),
            "source": str(suggestion.get("source") or result.get("source") or "heuristic"),
        })
        merged.append(item)
    return merged, str(result.get("source") or "heuristic"), error


def _napcat_port_accepts(url: str, timeout: float = 0.3) -> bool:
    """NapCat 没起来时，先花 0.3 秒探一下它的 API 端口。

    有的机器（本机就是）回环 connect 被拒也要等 2 秒；装了丢包型防火墙的机器更狠，要等满
    http_timeout。`/api/napcat/status` 会调两次 NapCat 接口，状态面板就会一直转圈。探不通就
    直接报错，别再等 urllib 的超时。

    结果缓存 3 秒：一次 status 会连问两遍，而 0.3 秒在「每次 stat 都要 0.35 秒」的机器上是
    真金白银。启动/重启/安装之后由 ``_clear_napcat_live_cache()`` 立刻失效。
    """
    key = f"{NAPCAT_PORT_CACHE_KEY}:{url}"
    _NAPCAT_PORT_KEYS.add(key)
    return ttl_cache.get(
        key,
        NAPCAT_PORT_CACHE_SECONDS,
        lambda: _napcat_port_accepts_uncached(url, timeout),
        cache_empty=True,
    )


def _napcat_port_accepts_uncached(url: str, timeout: float = 0.3) -> bool:
    parsed = urllib.parse.urlparse(url)
    if not parsed.hostname or not parsed.port:
        return True  # 拆不出主机/端口就照旧交给 urllib
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout):
            return True
    except OSError:
        return False


# NapCat 端口探测与 Tailscale 公网地址都会真的碰系统，缓存几秒；动作之后主动失效。
NAPCAT_PORT_CACHE_KEY = "webapp:napcat-port"
NAPCAT_PORT_CACHE_SECONDS = 3.0
_NAPCAT_PORT_KEYS: set[str] = set()
TAILSCALE_URL_CACHE_KEY = "webapp:tailscale-url"
TAILSCALE_HINT_CACHE_KEY = "webapp:tailscale-hint"
TAILSCALE_URL_CACHE_SECONDS = 30.0


def _clear_napcat_live_cache() -> None:
    """NapCat 状态类缓存全清：装完/启动/重启之后，下一次 status 必须是真实结果。

    端口探测的缓存键带 URL（同一进程里改过端口不能串味），用一个小集合记住用过哪些键，
    只清这些——别把微信定位、Tailscale 那些无关缓存一起清掉。
    """
    for key in list(_NAPCAT_PORT_KEYS):
        ttl_cache.clear(key)
    _NAPCAT_PORT_KEYS.clear()


def _clear_tailscale_cache() -> None:
    """公网地址/状态提示都缓存 30 秒，开通动作有结果后立刻失效。"""
    ttl_cache.clear(TAILSCALE_URL_CACHE_KEY)
    ttl_cache.clear(TAILSCALE_HINT_CACHE_KEY)


def _napcat_wait_seconds(server: Any, stage: str, stamp: str) -> float:
    """本进程第一次看到当前阶段起算的秒数，让页面能显示「已等待 N 秒」。

    起点存在 server 上：扫码登录要等用户掏手机，每请求重算成 0 就永远显示 0 秒。
    ``waiting_qr`` 阶段换过二维码（stamp 变了）要重新起算——那是重新扫的一张码。
    """
    now = time.monotonic()
    state = getattr(server, "napcat_stage_state", None)
    if not isinstance(state, dict) or state.get("stage") != stage or state.get("stamp") != stamp:
        state = {"stage": stage, "stamp": stamp, "since": now}
        server.napcat_stage_state = state
    return round(max(0.0, now - float(state.get("since") or now)), 1)


def _napcat_connect_hints(settings: Settings, stage: str) -> dict[str, Any]:
    """「帮我连上它」按钮要的数据：现在能不能连、当前地址、会按顺序试哪些地址。

    按钮由前端画，文案归前端；后端只保证字段齐、按候选顺序真的能连上。
    """
    candidates: list[str] = []
    can_connect = stage in ("running_no_api", "waiting_qr")
    # 只有真能连的时候才算候选地址：算一次要读注册表 + 递归 glob 引擎目录，
    # 登录页隔几秒轮询一次状态，不能每次都扫盘。
    if can_connect and napcat_admin is not None:
        try:
            candidates = napcat_admin.api_candidates(settings)
        except Exception:  # noqa: BLE001 - 状态接口不能因为探测失败而崩
            LOGGER.warning("列出 NapCat 候选地址失败", exc_info=True)
            candidates = []
    return {
        "can_connect": can_connect,
        "connect_url": "/api/napcat/connect",
        "napcat_api_url": str(getattr(settings, "napcat_api_url", "") or ""),
        "napcat_api_candidates": candidates,
    }


def _setup_advice(
    *,
    qq_stage: str,
    engine_state: str,
    login_state: str,
    wechat_enabled: bool,
    wechat_groups: bool,
    dingtalk_configured: bool,
    dingtalk_ready: bool,
    chaoxing_result: str,
    chaoxing_has_cookie: bool,
    channels: list[str],
    llm_active: bool,
) -> dict[str, dict[str, Any]]:
    """每个平台一句人话「卡在哪」+ 一个下一步动作，全部是静态映射。

    只读已经算好的状态，**不在这里做任何探测**：这个接口在设置页首屏被拉，加一次
    子进程或网络请求就等于把首屏拖慢好几秒。

    动作三选一：``panel``（前端 openPlatformSetupTarget 跳到的卡片）、``api``（前端直接
    POST 的接口，必须是 routes.py 里真实存在的路径）、``manual``（修不了，``hint`` 写清
    具体到哪个界面点哪个按钮）。已配好的平台 ``action`` 为 ``None``。
    """
    panels = {
        "qq": "qq",
        "wechat": "wechat-settings-card",
        "dingtalk": "dingtalk-settings-card",
        "chaoxing": "chaoxing-settings-card",
        "push": "push-settings",
        "llm": "llm-settings",
    }

    def panel(name: str, label: str) -> dict[str, Any]:
        return {"kind": "panel", "label": label, "target": panels[name], "hint": ""}

    def manual(label: str, hint: str) -> dict[str, Any]:
        return {"kind": "manual", "label": label, "target": "", "hint": hint}

    advice: dict[str, dict[str, Any]] = {}

    # ---------------- QQ：装没装 / 跑没跑 / 能不能扫 / 接口通不通
    # headline 里不写平台名（行左边已经有名字），也不写 NapCat / OneBot / token 这类内部术语。
    if login_state == "online" and engine_state == "running":
        advice["qq"] = {"headline": "已连上，消息能进来了", "action": None}
    elif qq_stage == "running_no_api":
        advice["qq"] = {
            "headline": "引擎在跑，但程序还连不上它",
            "action": {"kind": "api", "label": "帮我连上它", "target": "/api/napcat/connect", "hint": ""},
        }
    elif engine_state == "engine_missing":
        advice["qq"] = {
            "headline": "这版缺一键接入模块，装不了引擎",
            "action": manual("看怎么修", "用最新的安装包覆盖安装一次群务台：双击安装包，一路点「下一步」到完成。这版缺一键接入模块，没法自动装引擎"),
        }
    elif engine_state == "installed_not_running":
        advice["qq"] = {"headline": "引擎装好了，但没启动", "action": panel("qq", "去启动引擎")}
    elif engine_state == "not_installed":
        advice["qq"] = {"headline": "还没装引擎，装了才能接消息", "action": panel("qq", "去装引擎")}
    elif login_state == "waiting_scan":
        advice["qq"] = {"headline": "引擎在跑，等你扫码登录", "action": panel("qq", "去扫码登录")}
    else:
        advice["qq"] = {"headline": "引擎在跑，还没登录", "action": panel("qq", "去登录")}

    # ---------------- 微信
    if wechat_enabled and wechat_groups:
        advice["wechat"] = {"headline": "已接上，消息能进来了", "action": None}
    elif not wechat_enabled:
        advice["wechat"] = {"headline": "还没开，这部分消息收不到", "action": panel("wechat", "去开微信")}
    else:
        advice["wechat"] = {"headline": "开着，但没选群，等于没收", "action": panel("wechat", "去选群")}

    # ---------------- 钉钉
    if dingtalk_configured and dingtalk_ready:
        advice["dingtalk"] = {"headline": "已接上，消息能进来了", "action": None}
    elif dingtalk_configured:
        advice["dingtalk"] = {"headline": "群填好了，但连不上服务", "action": panel("dingtalk", "去查连接")}
    else:
        advice["dingtalk"] = {"headline": "还没填群，这部分消息进不来", "action": panel("dingtalk", "去填群")}

    # ---------------- 学习通
    if chaoxing_result == "有效":
        advice["chaoxing"] = {"headline": "已登录，课程能抓了", "action": None}
    elif chaoxing_result == "失效":
        advice["chaoxing"] = {
            "headline": "登录过期了，课程抓不到",
            "action": manual("重新读一次登录信息", "在「学习通」卡片里点「立即校验登录凭证」；自动获取不行就展开「高级：手动粘贴登录凭证」"),
        }
    elif chaoxing_has_cookie:
        advice["chaoxing"] = {"headline": "填过登录信息，但还没验证", "action": panel("chaoxing", "去验证")}
    else:
        advice["chaoxing"] = {"headline": "还没登录，课程抓不到", "action": panel("chaoxing", "去登录")}

    # ---------------- 推送
    if channels:
        advice["push"] = {"headline": "配好了，提醒能发到手机", "action": None}
    else:
        advice["push"] = {"headline": "没配，手机收不到提醒", "action": panel("push", "去配推送")}

    # ---------------- 模型
    if llm_active:
        advice["llm"] = {"headline": "配好了，摘要会按内容挑重点", "action": None}
    else:
        advice["llm"] = {"headline": "没配，通知先按关键词挑", "action": panel("llm", "去配模型")}

    return advice


def _request_napcat_action(settings: Settings, action: str) -> Any:
    if not _napcat_port_accepts(settings.napcat_api_url):
        raise ConnectionError("NapCat 没在运行（连不上 %s）" % settings.napcat_api_url)
    request = urllib.request.Request(
        settings.napcat_api_url.rstrip("/") + "/" + action,
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + settings.napcat_api_token},
    )
    with urllib.request.urlopen(request, timeout=settings.http_timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("status") != "ok" or payload.get("retcode", 0) != 0:
        raise RuntimeError(str(payload.get("message") or "NapCat request failed"))
    return payload.get("data") or {}


class _Handler(SettingsPostMixin, BaseHTTPRequestHandler):
    server_version = "qq-tasks/1.0"
    # 红队 S21：慢客户端不许把连接挂到天荒地老（socket 超时，单位秒）。
    timeout = REQUEST_TIMEOUT_SECONDS

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 与基类签名一致
        # WEB-2: the request line carries the access token (`/api/x?token=...`), so logging
        # it verbatim writes the token into the log file. Redact before it ever lands there.
        LOGGER.debug("web %s", _redact_secrets(format % args))

    def _reply(self, status: int, raw: bytes, content_type: str, cache: str | None = None, extra_headers: list[tuple[str, str]] | None = None) -> None:
        """发一个响应体。

        客户端中途关页面或刷新时 write 会抛 ConnectionAbortedError(10053)/10054/EPIPE。
        对方已经不在了，这里必须吞掉：再抛出去只会让路由的 except 对着同一条断开的连接
        再写一次响应、再抛一次（日志里那串 WinError 10053 就是这么来的）。
        """
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            if cache:
                self.send_header("Cache-Control", cache)
            for name, value in (extra_headers or ()):
                self.send_header(name, value)
            take_cookie = getattr(self, "_take_session_cookie", None)
            cookie = take_cookie() if callable(take_cookie) else ""
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(raw)
        except ConnectionError:
            self.close_connection = True

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self._reply(status, json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8", "no-store")

    def _lab_reply(self, result: dict[str, Any]) -> None:
        # 实验室门面统一返回 {"ok": ..., "error": ...}；开关关→403，模块没落地/内部失败→503。
        # 用户自己填错的东西（规则非法、消息缺字段）→400，别让他以为是服务端坏了。
        # 门面只包一层：底层函数自己也可能返回 {"ok": False, "error": ...}（比如规则校验失败），
        # 这时外层 ok 仍是 True。不摊平就会把「第 2 条规则非法」当成保存成功回给页面。
        inner = result.get("result")
        if result.get("ok") and isinstance(inner, dict) and "ok" in inner:
            result = inner
        error = str(result.get("error") or "")
        if result.get("ok"):
            self._json(200, result)
        elif "未开启" in error:
            self._json(403, {"ok": False, "error": error})
        elif any(mark in error for mark in ("非法", "必须是一个数组", "缺少", "过长", "必须是对象")):
            self._json(400, {"ok": False, "error": error})
        else:
            self._json(503, {"ok": False, "error": error})

    def _redirect_home(self) -> None:
        query = urllib.parse.urlparse(self.path).query
        location = "/" + ("?" + query if query else "")
        self._reply(302, b"", "text/plain; charset=utf-8", None, [("Location", location)])

    def _html(self, body: str, status: int = 200) -> None:
        self._reply(status, body.encode("utf-8"), "text/html; charset=utf-8")

    def _ics(self, body: str) -> None:
        # 页面里的「测试地址」按钮要从浏览器直接 fetch 这个订阅地址（跨源），
        # 没有这个头浏览器会直接拦掉并报 Failed to fetch，让人误以为订阅地址坏了。
        # 地址本身自带令牌，放开读取范围不会多给出任何东西。
        self._reply(200, body.encode("utf-8"), "text/calendar; charset=utf-8", "no-store", [("Access-Control-Allow-Origin", "*")])

    def _png(self, raw: bytes) -> None:
        self._reply(200, raw, "image/png", "no-store")

    def _jpeg(self, raw: bytes) -> None:
        self._reply(200, raw, "image/jpeg", "max-age=3600")

    def _asset(self, body: str, content_type: str, *, cache: str = "no-store") -> None:
        extra = [("Service-Worker-Allowed", "/")] if content_type.startswith("text/javascript") else None
        self._reply(200, body.encode("utf-8"), content_type, cache, extra)

    def _take_session_cookie(self) -> str:
        """把「这一条响应要下发会话 Cookie」的标记消费掉（一次性，keep-alive 不重复发）。"""
        if not getattr(self, "_issue_session_cookie", False):
            return ""
        self._issue_session_cookie = False
        token = str(getattr(self.server, "token", "") or "")
        return _session_cookie_header(token) if token else ""

    def _session_cookie_token(self) -> str:
        raw = str(self.headers.get("Cookie", "") or "")
        if not raw:
            return ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE_NAME:
                return value.strip()
        return ""

    def _respond_if_throttled(self) -> bool:
        """红队 S12：爆破期间连比对都不做，直接 429。"""
        if not _auth_blocked(self._client_address()):
            return False
        self._json(429, {"ok": False, "error": "尝试过于频繁，请稍后再试"})
        return True

    def _authorized(self) -> bool:
        token = str(getattr(self.server, "token", "") or "")
        if not token:
            # 空 token 只可能出现在绑定环回的部署（WebApp.__init__ 对非环回必然生成 token）。
            # 这里再按来源兜底：非本机来源一律拒绝，防止误配置把无鉴权端口暴露到局域网。
            try:
                peer = self.client_address[0] if self.client_address else ""
            except (IndexError, TypeError, ValueError):
                peer = ""
            return peer in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
        # WEB-1: `in` / `==` on secrets leaks their prefix through timing (an attacker can
        # recover a token byte by byte by measuring how long the comparison takes).
        # hmac.compare_digest is constant-time. Both sides go through str() because
        # compare_digest rejects str containing non-ASCII characters.
        # 红队 S1：判定顺序 = 会话 Cookie → X-Token → ?token=（并顺手换成 Cookie）。
        cookie = self._session_cookie_token()
        if cookie and hmac.compare_digest(cookie, _session_cookie_value(token)):
            return True
        if hmac.compare_digest(str(self.headers.get("X-Token", "")), token):
            return True
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query, max_num_fields=8)
        for candidate in params.get("token", []):
            if hmac.compare_digest(str(candidate), token):
                self._issue_session_cookie = True
                return True
        _record_auth_failure(self._client_address())
        return False

    def _client_address(self) -> str:
        return str(self.client_address[0] if self.client_address else "")

    def _device_label(self) -> str:
        """给电脑设置页看的一句话设备描述，好让用户核对是不是自己的手机。"""
        agent = str(self.headers.get("User-Agent", "") or "")
        if "Android" in agent:
            name = "安卓手机"
        elif "iPhone" in agent or "iPad" in agent:
            name = "苹果手机"
        elif not agent:
            name = "未知设备"
        else:
            name = agent[:20]
        address = self._client_address()
        return f"{name}（{address}）" if address else name

    def _pair_decision(self, approved: bool) -> None:
        try:
            payload = self._json_body()
        except ValueError as error:
            self._json(400, {"ok": False, "error": str(error)})
            return
        code = str(payload.get("code") or "").strip()
        if not code.isdigit() or len(code) != 6:
            self._json(400, {"ok": False, "error": "配对码必须是 6 位数字"})
            return
        result = pairing.decide(code, approved)
        self._json(200 if result.get("ok") else 400, result)

    def _json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as error:
            raise ValueError("请求长度无效") from error
        if length > MAX_BODY_BYTES:
            # 这里只排空再抛，由调用方回 400；不能调 _reject_oversized_body，否则会回两条响应。
            self._drain_oversized_body(length)
            raise ValueError("请求内容为空或过大")
        if length <= 0:
            raise ValueError("请求内容为空或过大")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise ValueError("JSON 格式无效") from error
        if not isinstance(payload, dict):
            raise ValueError("请求内容必须是 JSON 对象")
        return payload

    def _drain_oversized_body(self, length: int) -> None:
        """把已声明的 body 读掉一小段：不读就走，内核会发 RST，客户端拿到连接重置而不是 400。

        最多读 1 MB（够排空内核缓冲区），绝不去读一个 100 MB 的 body；先用 0.2 秒的超时，
        声明了超大 body 却根本没发的客户端不会把请求线程挂在这里。
        """
        try:
            self.connection.settimeout(0.2)
        except OSError:
            pass
        try:
            self.rfile.read(min(int(length or 0), 1 << 20))
        except (OSError, ValueError):
            pass  # 超时/连接已断都正常：照样回 400，不往上抛
        finally:
            try:
                self.connection.settimeout(getattr(self, "timeout", None))
            except OSError:
                pass

    def _reject_oversized_body(self, length: int, error: str = "bad body") -> None:
        self._drain_oversized_body(length)
        self._json(400, {"ok": False, "error": error})

    def _confirmed_host_action(self) -> bool:
        """请求体里是否显式同意执行「会改本机设置」的写操作（`{"confirm": true}`）。

        没有 body、body 不是 JSON 对象、或 confirm 不是真正的布尔 true，一律算没确认——
        绝不因为「参数写错」就放行一次装软件/弹 UAC/开公网。
        """
        try:
            payload = self._json_body()
        except ValueError:
            return False
        return payload.get("confirm") is True

    def _napcat(self, action: str) -> dict[str, Any]:
        try:
            data = _request_napcat_action(self.server.settings, action)  # type: ignore[attr-defined]
            return {"ok": True, "data": data}
        except Exception as error:  # noqa: BLE001
            # 连不上不能说「WinError 10061 积极拒绝」——那句话用户没法照做（issue #4）。
            hint = napcat_admin.probe_failure_hint(error) if napcat_admin is not None else str(error)
            return {"ok": False, "error": hint}

    @property
    def store(self) -> Store:
        return self.server.store  # type: ignore[attr-defined]

    def _remember_group_names(self, result: dict[str, Any]) -> None:
        """把 NapCat 报回来的群名存进数据库：界面显示群名靠它，NapCat 掉线时也得显示得出来。"""
        rows = result.get("data") or []
        names = {}
        for row in rows if isinstance(rows, list) else []:
            group_id = str(row.get("group_id", "")).strip()
            name = str(row.get("group_name", "")).strip()
            if group_id and name:
                names[group_id] = name
        if names:
            try:
                self.store.remember_group_names(names)
            except Exception:  # noqa: BLE001
                LOGGER.warning("群名缓存写入失败", exc_info=True)

    def _napcat_status_payload(self) -> dict[str, Any]:
        status = self._napcat("get_status")
        login = self._napcat("get_login_info")
        settings = self.server.settings  # type: ignore[attr-defined]
        stamp = napcat_admin.qr_stamp(settings) if napcat_admin is not None else ""
        qr_age = napcat_admin.qr_age_seconds(settings) if napcat_admin is not None else None
        qr_age = None if qr_age is None else round(qr_age, 1)
        qr_stale = napcat_admin.qr_stale(settings) if napcat_admin is not None else False
        boot = napcat_admin.detect_boot(settings.data_dir) if napcat_admin is not None else None
        if not status.get("ok") or not login.get("ok"):
            error = status.get("error") or login.get("error") or "NapCat error"
            pids = napcat_admin.running_pids(settings) if napcat_admin is not None else []
            running = bool(pids)
            # OneBot 的 HTTP 端口要等扫码登录成功才监听，所以「端口不通」不等于坏了：
            # 引擎进程在、只是还没登录，就是 waiting_qr。反过来 running_pids 在有的机器上
            # 会假阴性（查进程失败返回空），所以端口有响应时绝不能判成 stopped。
            port_open = _napcat_port_accepts(settings.napcat_api_url)
            if port_open:
                stage = "error"  # 端口在监听、接口却失败：配置/token/OneBot 服务本身的问题，不该再叫用户去扫码
            elif running:
                stage = "waiting_qr"
                error = ("已经收到二维码，正在等手机确认；确认后 OneBot 会自己起来，不用刷新页面"
                         if stamp else
                         "还没生成二维码：引擎刚启动时二维码要等几秒才出现，一直不出现就点「重启引擎」再试")
            elif boot:
                stage = "stopped"
                # 用户可能自己手动起了一份 NapCat（issue #4）：进程在、登录了，但我们连不上它的
                # OneBot 接口。这时说「未登录、去扫码」是错的，得告诉他两条真实可走的路。
                others: list[int] = []
                if napcat_admin is not None:
                    try:
                        mine = set(napcat_admin.running_pids(settings))
                        others = [pid for pid in napcat_admin.all_napcat_pids() if pid not in mine]
                    except (OSError, ValueError):
                        others = []
                if others:
                    # 用户自己手动起的那份 NapCat：别让他去翻端口。程序读它的配置、
                    # 按它实际开的端口接上（POST /api/napcat/connect 干这事）。
                    stage = "running_no_api"
                    error = ("检测到这台机器上有 NapCat 在跑（像你手动启动的那份），但本程序连不上它的 OneBot 接口。"
                             "点「帮我连上它」：程序会去读 NapCat 自己的配置，按它实际开的端口接上，不用你自己找端口。")
                else:
                    # 刚点过启动、进程查询假阴性、端口还没起来的窗口期：别让用户以为装了却没跑
                    error = f"{error}；刚点过启动就等十几秒再刷新"
            else:
                stage = "not_installed"
            payload = {"ok": False, "online": False, "good": False, "user_id": "", "nickname": "", "napcat_installed": bool(boot), "napcat_root": (boot or {}).get("data_dir"), "qr_stamp": stamp, "qr_age_seconds": qr_age, "qr_stale": qr_stale, "napcat_running": running, "stage": stage, "waiting_seconds": _napcat_wait_seconds(self.server, stage, stamp), "error": error}
            # 「帮我连上它」按钮的判据：引擎在场、接口没通，就有活可干。
            payload.update(_napcat_connect_hints(settings, stage))
            return payload
        sd, ld = status["data"], login["data"]
        # OneBot 接口通了不等于 QQ 已登录：NapCat 起来但还没扫码/没确认时 online 仍是 false。
        # 这时必须报 waiting_login，否则页面会显示「已登录」——又是一个假状态。
        qq_online = bool(sd.get("online"))
        payload = {"ok": True, "online": qq_online, "good": bool(sd.get("good")), "user_id": sd.get("user_id") or ld.get("user_id", ""), "nickname": ld.get("nickname", ""), "napcat_installed": bool(boot), "napcat_root": (boot or {}).get("data_dir"), "qr_stamp": stamp, "qr_age_seconds": qr_age, "qr_stale": qr_stale, "napcat_running": True, "stage": "online" if qq_online else "waiting_login", "waiting_seconds": _napcat_wait_seconds(self.server, "online" if qq_online else "waiting_login", stamp), "error": ""}
        payload.update(_napcat_connect_hints(settings, payload["stage"]))
        return payload

    def _napcat_doctor_payload(self) -> dict[str, Any]:
        if napcat_admin is None:
            return {"ok": False, "steps": [], "broken": "", "hint": "", "error": "一键接入模块不可用"}
        try:
            return napcat_admin.diagnose(self.server.settings)  # type: ignore[attr-defined]
        except Exception as error:  # noqa: BLE001
            LOGGER.warning("NapCat 自检失败：%s", error)
            return {"ok": False, "steps": [], "broken": "", "hint": "", "error": str(error)}

    def _sync_info_response(self) -> tuple[int, dict[str, Any]]:
        from noticehub.application.calendar import describe_calendar_subscription

        try:
            port = int(self.server.server_address[1])
            token = str(getattr(self.server, "token", "") or "")
            from noticehub.application.calendar import render_calendar_ics

            events = sum(
                1 for line in render_calendar_ics(self.store).splitlines() if line == "BEGIN:VEVENT"
            )
            payload = describe_calendar_subscription(
                _lan_hosts(),
                _tailscale_calendar_url(port),
                port=port,
                token=token,
                events=events,
            )
        except OSError:
            LOGGER.warning("无法确定 iPhone 日历订阅地址")
            return 503, {"ok": False, "error": "无法确定可供手机访问的局域网地址"}
        except Exception as error:  # noqa: BLE001
            LOGGER.exception("读取日历订阅信息失败")
            return 500, {"ok": False, "error": str(error) or "读取日历订阅信息失败"}
        if not payload["calendars"]:
            LOGGER.warning("无法确定 iPhone 日历订阅地址")
            return 503, {"ok": False, "error": "无法确定可供手机访问的局域网地址"}
        return 200, payload

    def _dingtalk_status_payload(self) -> dict[str, Any]:
        settings = self.server.settings  # type: ignore[attr-defined]
        provider = getattr(self.server, "dingtalk_reader_provider", None)
        reader = provider() if callable(provider) else getattr(self.server, "dingtalk_reader", None)
        enabled = bool(settings.dingtalk_enabled)
        running = bool(reader and reader.is_alive)
        if not enabled:
            state = "未启用"
        elif not settings.dingtalk_group_whitelist:
            state = "已启用，但还没勾群"
        elif running:
            state = f"已连接实时监听 {len(settings.dingtalk_group_whitelist)} 个群"
        else:
            state = "钉钉已启用，监听未启动"
        scan = dict(getattr(settings, "dingtalk_status", {}) or {})
        started_at = str(scan.get("scan_started_at") or "")
        elapsed = max(0.0, round(time.monotonic() - float(scan.get("scan_started_monotonic") or 0.0), 1)) if scan.get("scan_state") == "running" else 0.0
        return {"ok": True, "enabled": enabled, "running": running, "groups": len(settings.dingtalk_group_whitelist), "state": state, "scan_state": str(scan.get("scan_state") or ("idle" if enabled else "disabled")), "scan_stage": str(scan.get("scan_stage") or ""), "scan_started_at": started_at, "scan_elapsed_seconds": elapsed}

    def _wechat_status_payload(self) -> dict[str, Any]:
        settings = self.server.settings  # type: ignore[attr-defined]
        provider = getattr(self.server, "wechat_reader_provider", None)
        reader = provider() if callable(provider) else None
        enabled = bool(settings.wechat_enabled)
        running = bool(reader is not None and reader.is_alive)
        available_dir = wechat_locate_storage(str(getattr(settings, "wechat_dir", "") or "").strip() or None)
        available = available_dir is not None
        if not enabled:
            state = "未启用"
        elif not available:
            # 不是「没装/没登录」：改过微信存储位置的人也能对上号，并指向下一步动作。
            state = "未找到微信数据目录（可能未安装，或微信的存储位置改过）"
        elif running:
            state = f"已连接 {len(settings.wechat_group_whitelist)} 个群"
        else:
            state = "微信已启用，扫描未启动"
        status = dict(_WECHAT_READ_STATUS)
        scan = dict(getattr(settings, "wechat_status", {}) or {})
        scan_state = str(scan.get("scan_state") or ("idle" if enabled else "disabled"))
        elapsed = max(0.0, round(time.monotonic() - float(scan.get("scan_started_monotonic") or 0.0), 1)) if scan_state == "running" else 0.0
        error_category = str(scan.get("error_category") or status.get("error_category") or "")
        return {
            "ok": True,
            "enabled": enabled,
            "running": running,
            "available": available,
            "groups": len(settings.wechat_group_whitelist),
            "interval_seconds": int(settings.wechat_scan_interval_seconds),
            "last_read_at": str(status.get("at") or ""),
            "last_read_seconds": float(status.get("seconds") or 0.0),
            "last_error": str(scan.get("last_error") or status.get("error") or ""),
            "error_category": error_category,
            "error_hint": wechat_scan_error_hint(error_category),
            "manual_key": bool(str(getattr(settings, "wechat_key", "") or "").strip()),
            "state": state,
            "hint": "" if available else wechat_storage_hint(),
            # 让用户能对照微信「文件管理」里的路径，判断程序读的是不是同一个目录。
            "storage_dir": str(available_dir) if available_dir is not None else "",
            "scan_state": scan_state,
            "scan_stage": str(scan.get("scan_stage") or ""),
            "scan_started_at": str(scan.get("scan_started_at") or ""),
            "scan_elapsed_seconds": elapsed,
        }

    def _chaoxing_status_payload(self) -> dict[str, Any]:
        return chaoxing_status(self.server.settings, self.store)  # type: ignore[attr-defined]

    def _chaoxing_cookie_validation_payload(self) -> dict[str, Any]:
        cookie = str(self.server.settings.chaoxing_cookie or "")  # type: ignore[attr-defined]
        try:
            saved = json.loads(self.store.meta_get("chaoxing_cookie_validation", "{}"))
        except (TypeError, ValueError):
            saved = {}
        fingerprint = hashlib.sha256(cookie.encode("utf-8")).hexdigest() if cookie else ""
        matches = isinstance(saved, dict) and saved.get("cookie_sha256") == fingerprint and bool(fingerprint)
        if not cookie:
            result, checked_at, error = "未配置", str((saved or {}).get("last_checked_at") or ""), ""
        elif not matches:
            result, checked_at, error = "未校验", "", ""
        else:
            result = str(saved.get("result") or "未校验")
            checked_at = str(saved.get("last_checked_at") or "")
            error = str(saved.get("error") or "")
        return {"has_cookie": bool(cookie), "last_checked_at": checked_at, "result": result, "error": error}

    def _validate_chaoxing_cookie(self) -> dict[str, Any]:
        cookie = str(self.server.settings.chaoxing_cookie or "")  # type: ignore[attr-defined]
        checked_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        error_text = ""
        if not cookie:
            result = "未配置"
            error_text = "学习通 Cookie 未配置"
        else:
            try:
                ChaoxingClient(cookie, timeout=min(float(self.server.settings.http_timeout), 10.0)).validate_login()  # type: ignore[attr-defined]
                result = "有效"
            except Exception as error:  # noqa: BLE001 - 原因由 Cookie 自检接口原样返回
                result = "失效"
                error_text = str(error) or error.__class__.__name__
                if cookie in error_text:
                    error_text = error_text.replace(cookie, "<redacted>")
        payload = {
            "cookie_sha256": hashlib.sha256(cookie.encode("utf-8")).hexdigest() if cookie else "",
            "last_checked_at": checked_at,
            "result": result,
            "error": error_text,
        }
        self.store.meta_set("chaoxing_cookie_validation", json.dumps(payload, ensure_ascii=False))
        return {"ok": result == "有效", **self._chaoxing_cookie_validation_payload()}

    def _overview_payload(self) -> dict[str, Any]:
        """总览 / 合并向导用：每个通道只回四种 state（off / needs_setup / ready / error）。

        判定全部复用现有函数（napcat / wechat / dingtalk / chaoxing 的 status payload、
        推送与 Funnel 的现有辅助），不另造一套。

        自检那条路径已经算过一遍通道状态，会把它挂在 ``self._precomputed_status`` 上让这里
        复用：每个 status payload 都要探一次外部状态，一次请求算两遍会让页面多等十几秒。
        """
        given = getattr(self, "_precomputed_status", None) or {}

        def reuse(key: str, probe):
            """自检已经算过的通道状态就用它——哪怕值是空的（空 = 这次没取到）。

            绝不在同一个请求里重探：超时的探测如果在这里再跑一遍，就是 15 秒预算之后
            又串行 15 秒，冷检直接冲到 30 多秒（实测 37.89 s 就是这么来的）。
            """
            if key in given:
                return given.get(key) or {}
            return probe() or {}
        settings = self.server.settings  # type: ignore[attr-defined]
        manager = getattr(self.server, "meta_provider", None)
        info: dict[str, Any] = {}
        if callable(manager):
            try:
                info = manager() or {}
            except Exception:  # noqa: BLE001 - 元信息失败不影响总览
                info = {}
        groups = list(info.get("groups") or [])

        try:
            napcat = reuse("napcat", self._napcat_status_payload)
        except Exception as error:  # noqa: BLE001 - 单个通道挂了绝不能把整个总览拖成 500
            LOGGER.warning("总览：读取 NapCat 状态失败：%s", error)
            napcat = {"_overview_error": str(error)[:120]}
        online = bool(napcat.get("online"))
        napcat_map = {
            "online": ("ready", "在线", ""),
            "waiting_qr": ("needs_setup", "等待扫码登录", "扫码登录 QQ"),
            "waiting_login": ("needs_setup", "已扫码，等待手机确认", "在手机上点确认"),
            "error": ("error", str(napcat.get("error") or "接口调不通"), "去检查引擎的接口设置"),
            "not_installed": ("off", "还没安装 NapCat 引擎", "安装 NapCat"),
            "stopped": ("off", "引擎没在运行", "启动并登录"),
        }
        if napcat.get("_overview_error"):
            napcat_state, napcat_detail, napcat_action = ("error", str(napcat["_overview_error"]), "重启程序后重试")
        else:
            napcat_state, napcat_detail, napcat_action = napcat_map.get(
                str(napcat.get("stage") or ""), ("off", "引擎状态未知", "启动并登录")
            )

        if napcat.get("_overview_error"):
            qq = ("error", str(napcat["_overview_error"]), "重启程序后重试")
        elif not online:
            qq = ("off", "QQ 未登录", "去扫码登录 QQ")
        elif not groups:
            qq = ("needs_setup", "还没选监控群", "去设置里选群")
        else:
            qq = ("ready", "监控 %d 个群" % len(groups), "")

        try:
            wechat = reuse("wechat", self._wechat_status_payload)
        except Exception as error:  # noqa: BLE001
            LOGGER.warning("总览：读取微信状态失败：%s", error)
            wechat = {"_overview_error": str(error)[:120]}
        if wechat.get("_overview_error"):
            wechat_state = ("error", str(wechat["_overview_error"]), "重启程序后重试")
        elif not wechat.get("enabled"):
            wechat_state = ("off", "", "去设置页「微信」里启用微信")
        elif not wechat.get("available"):
            wechat_state = ("needs_setup", "没找到微信数据目录", "去设置页「微信」看数据目录提示")
        elif wechat.get("last_error"):
            # 四分类的下一步文案由 wechat_receiver 出（error_hint），总览只搬，别再只丢技术原文。
            wechat_actions = {
                "not_logged_in": "去电脑上登录微信",
                "key_timeout": "等几分钟再扫描",
                "version_unsupported": "升级微信到 4.0",
            }
            wechat_state = (
                "error",
                str(wechat.get("error_hint") or wechat["last_error"]),
                wechat_actions.get(str(wechat.get("error_category") or ""), "去设置页点「立即扫描一次」"),
            )
        elif wechat.get("running"):
            wechat_state = ("ready", "已连接 %d 个群" % int(wechat.get("groups") or 0), "")
        else:
            no_groups = int(wechat.get("groups") or 0) == 0
            wechat_state = (
                "needs_setup",
                str(wechat.get("state") or "扫描未启动"),
                "去设置页勾选「我的群」并保存" if no_groups else "去设置页点「立即扫描一次」",
            )

        try:
            dingtalk = reuse("dingtalk", self._dingtalk_status_payload)
        except Exception as error:  # noqa: BLE001
            LOGGER.warning("总览：读取钉钉状态失败：%s", error)
            dingtalk = {"_overview_error": str(error)[:120]}
        if dingtalk.get("_overview_error"):
            dingtalk_state = ("error", str(dingtalk["_overview_error"]), "重启程序后重试")
        elif not dingtalk.get("enabled"):
            dingtalk_state = ("off", "", "去设置里开启钉钉")
        elif dingtalk.get("running"):
            dingtalk_state = ("ready", "已连接实时监听 %d 个群" % int(dingtalk.get("groups") or 0), "")
        elif not dingtalk.get("groups"):
            dingtalk_state = ("needs_setup", "还没选监听群", "去设置里选群")
        else:
            dingtalk_state = ("needs_setup", str(dingtalk.get("state") or "监听未启动"), "去设置页点「立即扫描一次」")

        try:
            cx = reuse("chaoxing", self._chaoxing_status_payload)
        except Exception as error:  # noqa: BLE001
            LOGGER.warning("总览：读取学习通状态失败：%s", error)
            cx = {"_overview_error": str(error)[:120]}
        if cx.get("_overview_error"):
            chaoxing_state = ("error", str(cx["_overview_error"]), "重启程序后重试")
        elif not cx.get("has_cookie"):
            chaoxing_state = ("off", "未配置 Cookie", "去设置里读取学习通 Cookie")
        elif cx.get("expired"):
            # Cookie 过期 = 同步必然失败，不能按提醒（黄）算，否则总览比自检轻一档。
            chaoxing_state = ("error", "登录已过期，需重新读取", "重新读取学习通 Cookie")
        elif cx.get("last_error") and not cx.get("ok"):
            chaoxing_state = ("error", str(cx["last_error"]), "检查 Cookie 后重试")
        elif cx.get("ok"):
            chaoxing_state = ("ready", "上次同步 %s" % cx.get("last_sync_at") if cx.get("last_sync_at") else "Cookie 有效", "")
        else:
            chaoxing_state = ("needs_setup", "还没成功同步过", "点「立即校验」")

        try:
            executable = tunnel.tailscale_executable()
            public_url = _tailscale_public_app_url(settings.web_port)
            if not executable:
                funnel_state = ("off", "未安装 Tailscale", "安装 Tailscale")
            elif public_url:
                funnel_state = ("ready", public_url, "")
            else:
                funnel_state = (
                    "needs_setup",
                    _tailscale_status_hint(executable) or "已安装，未开通公网入口",
                    "开通公网入口",
                )
        except Exception as error:  # noqa: BLE001 - 查不到就当作没开通
            funnel_state = ("off", str(error), "开通公网入口")

        try:
            channels = list(settings.push_channels())
            credentials = _push_setting_state(settings)
            push_error = ""
        except Exception as error:  # noqa: BLE001 - 单源出错不能把整个总览拖成 500
            LOGGER.warning("总览：读取推送状态失败：%s", error)
            channels, credentials, push_error = [], {}, str(error)[:120]
        if push_error:
            push_state = ("error", push_error, "重启程序后重试")
        elif channels:
            push_state = ("ready", "已配置 %s" % "、".join(channels), "")
        elif any(bool(item.get("set")) for item in credentials.values()):
            push_state = ("needs_setup", "通道凭据不完整", "补全推送凭据")
        else:
            push_state = ("off", "没有推送通道", "去设置里选推送方式")

        sources = {}
        for key, label, (state, detail, action) in (
            ("qq", "QQ 群", qq),
            ("napcat", "QQ 引擎", (napcat_state, napcat_detail, napcat_action)),
            ("wechat", "微信", wechat_state),
            ("dingtalk", "钉钉", dingtalk_state),
            ("chaoxing", "学习通", chaoxing_state),
            ("funnel", "公网入口", funnel_state),
            ("push", "推送", push_state),
        ):
            sources[key] = {"state": state, "label": label, "detail": str(detail or ""), "action": str(action or "")}
        return {"ok": True, "sources": sources}

    def _onboarding_gates(self) -> dict[str, dict[str, Any]]:
        """四道门 + 推送的当前状态：渠道判定全部来自 _overview_payload()。"""
        settings = self.server.settings  # type: ignore[attr-defined]
        sources = self._overview_payload().get("sources") or {}
        fw = _firewall_allows(settings.web_port, getattr(settings, "data_dir", None))
        if fw.get("allowed"):
            firewall = {"state": "ready", "detail": "端口已放行", "action": "一键放行"}
        elif not fw.get("checked"):
            # 读不到规则清单时不能断言「没放行」：点「一键放行」会在提权进程里顺手把清单写回来。
            firewall = {
                "state": "needs_setup",
                "detail": "有没有放行要管理员权限才查得到（点「一键放行」会顺便查出来）",
                "action": "一键放行",
            }
        else:
            firewall = {"state": "needs_setup", "detail": "本机防火墙还没放行这个端口", "action": "一键放行"}
        return {
            "qq": sources.get("qq") or {},
            "napcat": sources.get("napcat") or {},
            "tailscale": sources.get("funnel") or {},
            "firewall": firewall,
            "push": sources.get("push") or {},
        }

    def _onboarding_next_payload(self, skip: set[str] | None = None) -> dict[str, Any]:
        gates = self._onboarding_gates()
        done: list[str] = []
        skipped: list[str] = []
        step = "done"
        # 「跳过这一步」只对 skippable 的步骤生效：qq/napcat/firewall 是硬前置，
        # 手改地址塞进来也不认，免得向导被糊过去。
        skip = skip or set()
        for key in ONBOARDING_ORDER:
            if str(gates[key].get("state") or "") == "ready":
                done.append(key)
                continue
            if key in skip and ONBOARDING_STEPS[key][6]:
                skipped.append(key)
                continue
            step = key
            break
        total = len(ONBOARDING_ORDER)
        if step == "done":
            names = "、".join(ONBOARDING_STEPS[key][0] for key in skipped)
            return {
                "ok": True,
                "step": "done",
                "title": "剩下的都跳过了" if skipped else "四道门都通了",
                "body": (
                    "「%s」这几步先跳过了，之后照页面里的提示补上就行。" % names
                    if skipped
                    else "接下来等第一条待办进来就行。"
                ),
                "reason": "",
                "state": "ready",
                "action": "",
                "action_label": "",
                "blocked": False,
                "skippable": False,
                "done": done,
                "detail": "",
                "skipped": skipped,
                "index": total,
                "total": total,
            }
        title, body, reason, action, action_label, blocked, skippable = ONBOARDING_STEPS[step]
        source = gates.get(step) or {}
        state = str(source.get("state") or "off")
        # error 时把具体的错顶到 reason 前面：静态文案说不清 token 错了还是端口被占。
        detail = str(source.get("detail") or "")
        return {
            "ok": True,
            "step": step,
            "title": title,
            "body": body,
            "reason": (detail if state == "error" and detail else reason),
            "state": state,
            "action": action,
            "action_label": action_label,
            "blocked": blocked,
            "skippable": skippable,
            "done": done,
            "detail": detail,
            "skipped": skipped,
            "index": ONBOARDING_ORDER.index(step) + 1,
            "total": total,
        }

    def _onboarding_run_payload(self) -> tuple[int, dict[str, Any]]:
        settings = self.server.settings  # type: ignore[attr-defined]
        try:
            payload = self._json_body()
        except ValueError as error:
            return 400, {"ok": False, "error": str(error)}
        if payload.get("confirm") is not True:
            return 400, {
                "ok": False,
                "needs_confirm": True,
                "error": "这是会装软件/改防火墙/开公网的写操作，请带 confirm:true 再调用。",
            }
        action = str(payload.get("action") or "").strip()
        if action in {"connect_qq", "configure_push"}:
            message = (
                "用手机 QQ 扫二维码登录——这一步必须人工完成，向导点不了你的手机。"
                if action == "connect_qq"
                else "推送通道要在「设置 → 提醒怎么送到手机」里填；这一步可以跳过。"
            )
            _onboarding_set("blocked", action, message)
            return 200, {"ok": True, "state": "blocked", "blocked": True, "action": action, "message": message}
        running_messages = {
            "install_napcat": "正在下载并安装 QQ 引擎",
            "install_tailscale": "正在下载/安装 Tailscale",
            "open_funnel": "正在开通公网入口",
            "allow_firewall": "已把放行请求交给托盘",
        }
        if action not in running_messages:
            return 400, {"ok": False, "error": "未知的向导动作：%s" % (action or "（空）")}
        # 同一个动作已经在跑就回它自己的状态，不再起一个线程：连点两下不该下两份。
        existing = _onboarding_begin(action, running_messages[action])
        if existing is not None:
            return 200, {
                "ok": True,
                "state": "running",
                "action": action,
                "deduped": True,
                "message": str(existing.get("message") or running_messages[action]),
            }
        if action == "install_napcat":
            threading.Thread(target=_run_onboarding_napcat, args=(settings,), name="onboarding-napcat", daemon=True).start()
            return 200, {"ok": True, "state": "running", "action": action, "message": "正在下载并安装 QQ 引擎，进度会显示在页面上"}
        if action in {"install_tailscale", "open_funnel"}:
            started = _funnel_setup(settings.web_port).start()
            _onboarding_set("running", action, str(started.get("message") or "已开始下载/安装 Tailscale"))
            return 200, {"ok": True, "state": "running", "action": action, "message": str(started.get("message") or "已开始安装 Tailscale")}
        request_id = _write_firewall_request(settings, settings.web_port)
        _onboarding_set("running", action, "已把放行请求交给托盘")
        return 200, {"ok": True, "state": "running", "action": action, "request_id": request_id, "message": "已把放行请求交给托盘，请看托盘弹窗"}

    def _onboarding_status_payload(self) -> dict[str, Any]:
        settings = self.server.settings  # type: ignore[attr-defined]
        with _onboarding_lock:
            state = dict(_onboarding_run)
        action = str(state.get("action") or "")
        stored_message = str(state.get("message") or "")
        if action in {"install_tailscale", "open_funnel"}:
            try:
                snapshot = _funnel_setup(settings.web_port).snapshot()
            except Exception as exc:  # noqa: BLE001 - 探测失败 ≠ 还在跑，否则进度条永远不消失
                LOGGER.warning("后台任务状态未知（%s）：%s", action, exc)
                return {
                    "ok": False,
                    "state": "error",
                    "action": action,
                    "message": stored_message,
                    "error": "后台任务状态未知，可能已中断，请取消后重试",
                }
            if not isinstance(snapshot, dict):
                # 记录还没写 = 任务可能刚起步，保持 running；探测抛异常才是真异常（上面已处理）。
                snapshot = {}
            raw = str(snapshot.get("state") or "")
            mapped = {"ready": "done", "error": "failed"}.get(raw, "running")
            message = str(snapshot.get("message") or stored_message or "")
            return {
                "ok": True,
                "state": mapped,
                "action": action,
                "message": message,
                "error": str(snapshot.get("error") or (message if mapped == "failed" else "")),
            }
        if action == "allow_firewall":
            fw = _firewall_status_payload(settings)
            mapped = {"granted": "done", "failed": "failed", "unavailable": "failed", "pending": "running"}.get(
                str(fw.get("state") or ""), "running"
            )
            message = str(fw.get("message") or stored_message or "")
            return {
                "ok": True,
                "state": mapped,
                "action": action,
                "message": message,
                "error": str(fw.get("error") or (message if mapped == "failed" else "")),
            }
        stored_state = str(state.get("state") or "idle")
        return {
            "ok": True,
            "state": stored_state,
            "action": action,
            "message": stored_message,
            "error": stored_message if stored_state == "failed" else "",
        }

    def _community_counts_payload(self) -> dict[str, Any]:
        """社区计数：读不到的那项是 None，但接口本身永远是 200，别拿它当健康检查。"""
        return {"ok": True, "counts": community.fetch_counts()}

    def _setup_status_payload(self) -> dict[str, Any]:
        settings = self.server.settings  # type: ignore[attr-defined]
        qq = self._napcat_status_payload()
        qq_online = bool(qq.get("online"))
        qq_running = bool(qq.get("napcat_running"))
        qq_installed = bool(qq.get("napcat_installed"))
        # 「引擎在跑」和「已登录」是两件事：混成一个 configured 会让顶部说在运行、
        # 平台卡又说没配置，用户不知道该扫码还是该装引擎。拆成两个状态字段。
        if qq_running:
            engine_state = "running"
        elif qq_installed:
            engine_state = "installed_not_running"
        elif napcat_admin is None:
            engine_state = "engine_missing"
        else:
            engine_state = "not_installed"
        login_state = "online" if qq_online else ("waiting_scan" if engine_state == "running" else "not_logged_in")
        cx = self._chaoxing_cookie_validation_payload()
        channels = list(settings.push_channels())
        dingtalk_ready = bool(self._dingtalk_status_payload().get("running"))
        dingtalk_configured = bool(settings.dingtalk_enabled and settings.dingtalk_group_whitelist)
        platforms = {
            "qq": {
                "configured": engine_state == "running" and login_state == "online",
                "status": "online" if qq_online else "offline",
                "engine_state": engine_state,
                "login_state": login_state,
            },
            "wechat": {
                "configured": bool(settings.wechat_enabled and settings.wechat_group_whitelist),
                "status": "configured" if settings.wechat_enabled and settings.wechat_group_whitelist else "incomplete",
            },
            "dingtalk": {
                "configured": dingtalk_configured,
                "status": "ready" if dingtalk_ready else ("needs_setup" if dingtalk_configured else "off"),
            },
            "chaoxing": {"configured": cx["result"] == "有效", "status": cx["result"]},
            "push": {"configured": bool(channels), "channels": channels},
            "llm": {"configured": bool(settings.llm_active), "provider": str(settings.llm_backend)},
        }
        # 只增不改：configured/status/engine_state/login_state 的语义一个字不动，
        # 前端旧代码照旧可用；新增的 headline/action 是「卡在哪 + 下一步做什么」。
        advice = _setup_advice(
            qq_stage=str(qq.get("stage") or ""),
            engine_state=engine_state,
            login_state=login_state,
            wechat_enabled=bool(settings.wechat_enabled),
            wechat_groups=bool(settings.wechat_group_whitelist),
            dingtalk_configured=dingtalk_configured,
            dingtalk_ready=dingtalk_ready,
            chaoxing_result=str(cx.get("result") or ""),
            chaoxing_has_cookie=bool(cx.get("has_cookie")),
            channels=channels,
            llm_active=bool(settings.llm_active),
        )
        for name, entry in advice.items():
            if name in platforms:
                platforms[name]["headline"] = entry["headline"]
                platforms[name]["action"] = entry["action"]
        return {"ok": True, "platforms": platforms}

    def _hosting_status_payload(self) -> dict[str, Any]:
        if hosting is None:
            return {"ok": False, "error": "托管模块不可用"}
        try:
            return hosting.hosting_status(self.server.settings)  # type: ignore[attr-defined]
        except Exception as error:  # noqa: BLE001
            return {"ok": False, "error": str(error)}

    # GET /health / /api/health
    def _route_get_health(self, path: str) -> None:  # noqa: N802 - 路由处理
        # 红队 S20：以前这里无条件 200，进程活着但库读不了也报健康。现在真的探一下，
        # 但只回粗粒度状态：这是公网可达的接口，不暴露路径、令牌、群 ID。
        checks: dict[str, Any] = {}
        healthy = True
        try:
            with self.server.store._connect() as connection:  # type: ignore[attr-defined]
                connection.execute("SELECT 1").fetchone()
            checks["database"] = "ok"
        except Exception as error:  # noqa: BLE001 - 健康接口本身绝不能抛
            checks["database"] = "down"
            healthy = False
            LOGGER.warning("健康检查：数据库不可用：%s", error)

        receivers: dict[str, str] = {}
        for name in ("dingtalk", "wechat", "chaoxing"):
            status = getattr(self.server.settings, name + "_status", None)  # type: ignore[attr-defined]
            state = str((status or {}).get("scan_state") or "") if isinstance(status, dict) else ""
            # 只看已有状态：没启用/没跑过就是 unknown，不猜它「应该没问题」。
            # idle = 一次都没跑过（未验证），不能当 ok 给绿点。
            receivers[name] = "ok" if state in {"ok", "running"} else ("down" if state == "error" else "unknown")
        checks["receivers"] = receivers

        service = getattr(self.server, "service", None)
        stamp = getattr(service, "last_push_at", None)
        if stamp is not None:
            last_push = stamp.strftime("%Y-%m-%dT%H:%M:%S")
        else:
            try:
                last_push = str(self.server.store.meta_get("last_push_at", "") or "")  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - 健康接口不能因为读不到历史就整条连接崩掉
                last_push = ""
        checks["last_push_at"] = last_push
        try:
            checks["update_source"] = ",".join(item["id"] for item in update_sources.sources(self.server.settings))
        except Exception:  # noqa: BLE001
            checks["update_source"] = "unknown"
        checks["channels"] = self.server.settings.push_channels()  # type: ignore[attr-defined]
        self._json(200 if healthy else 503, {"ok": healthy, "service": "qq-tasks", **checks})
        return

    # GET /manifest.webmanifest
    def _route_get_manifest_webmanifest(self, path: str) -> None:  # noqa: N802 - 路由处理
        self._asset(MANIFEST_JSON, "application/manifest+json; charset=utf-8")
        return

    # GET /icon.svg
    def _route_get_icon_svg(self, path: str) -> None:  # noqa: N802 - 路由处理
        self._asset(ICON_SVG, "image/svg+xml; charset=utf-8", cache="public, max-age=86400")
        return

    # GET /sw.js
    def _route_get_sw_js(self, path: str) -> None:  # noqa: N802 - 路由处理
        self._asset(SERVICE_WORKER_JS, "text/javascript; charset=utf-8")
        return

    # GET /api/donate/qr.png
    def _route_get_api_donate_qr_png(self, path: str) -> None:  # noqa: N802 - 路由处理
        image = RUNTIME_ROOT / "assets" / "donate-wechat.jpg"
        if not image.is_file():
            self._json(404, {"ok": False, "error": "donation QR not found"})
            return
        try:
            self._jpeg(image.read_bytes())
        except OSError:
            self._json(404, {"ok": False, "error": "donation QR not found"})
        return

    # GET /api/onboarding
    def _route_get_api_onboarding(self, path: str) -> None:  # noqa: N802 - 路由处理
        accepted_at = self.store.meta_get("onboarding_accepted_at", "")
        self._json(200, {"required": not bool(accepted_at), "accepted_at": accepted_at})
        return

    # GET /api/onboarding/next
    def _route_get_api_onboarding_next(self, path: str) -> None:  # noqa: N802 - 路由处理
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query, max_num_fields=4)
        self._json(200, self._onboarding_next_payload(_onboarding_skip_keys(query.get("skip") or [])))
        return

    # GET /api/onboarding/status
    def _route_get_api_onboarding_status(self, path: str) -> None:  # noqa: N802 - 路由处理
        self._json(200, self._onboarding_status_payload())
        return

    # GET /api/pair/status
    def _route_get_api_pair_status(self, path: str) -> None:  # noqa: N802 - 路由处理
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query, max_num_fields=8)
        # WEB-4: these two are unauthenticated. pairing issues a 6-digit code and a
        # 32-char secret, so anything longer is junk; reject it before it reaches the
        # compare_digest call instead of hashing megabytes of attacker-chosen string.
        code = str((query.get("code") or [""])[0])
        secret = str((query.get("secret") or [""])[0])
        if len(code) > _PAIR_CODE_MAX or len(secret) > _PAIR_SECRET_MAX:
            self._json(404, {"ok": False, "error": "配对码无效或已过期"})
            return
        result = pairing.status(code, secret)
        if result is None:
            self._json(404, {"ok": False, "error": "配对码无效或已过期"})
            return
        payload: dict[str, Any] = {"ok": True, "status": result["status"]}
        if result["status"] == "approved":
            payload["token"] = str(getattr(self.server, "token", "") or "")
        self._json(200, payload)
        return

    # GET /api/community/counts
    def _route_get_api_community_counts(self, path: str) -> None:  # noqa: N802 - 路由处理
        # 页面每次打开都会拉这个接口，正好当「今日打开 / 当前在线」的计数信号。
        # 计数只是锦上添花：放后台线程发，第三方服务慢/不通时绝不能让页面跟着等。
        # timeout 必须给足 3 秒：abacus 端点实测要 0.9–1.3 秒才回，给 1 秒会必然超时，
        # 上报永远写不进去，页面就永远显示「暂不可用」。
        # 关掉遥测就完全不发请求、不建线程；守卫放在调用点，别让每页都白起一个线程。
        if getattr(self.server.settings, "community_telemetry", False):
            try:
                threading.Thread(
                    target=community.ping, kwargs={"timeout": 3.0},
                    name="notice-hub-community-ping", daemon=True,
                ).start()
            except Exception:  # noqa: BLE001
                pass
        self._json(200, self._community_counts_payload())
        return

    # GET /api/lab/status
    def _route_get_api_lab_status(self, path: str) -> None:  # noqa: N802 - 路由处理
        settings = self.server.settings  # type: ignore[attr-defined]
        available = lab.available(settings)
        # available 是「开关开 且 模块就绪」；设置页回显勾选态要看开关本身。
        self._json(200, {"ok": True, "result": available, "enabled": {name: settings.lab_enabled(name) for name in available}})
        return

    # GET /api/lab/rules
    def _route_get_api_lab_rules(self, path: str) -> None:  # noqa: N802 - 路由处理
        self._lab_reply(lab.list_rules(self.server.settings))  # type: ignore[attr-defined]
        return

    # GET /api/lab/search
    def _route_get_api_lab_search(self, path: str) -> None:  # noqa: N802 - 路由处理
        settings = self.server.settings  # type: ignore[attr-defined]
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query, max_num_fields=8)
        q = str((params.get("q") or [""])[0]).strip()
        if not q:
            self._json(400, {"ok": False, "error": "缺少 q"})
            return
        if len(q) > 2000:
            self._json(400, {"ok": False, "error": "q 过长"})
            return
        filters: dict[str, Any] = {}
        for key in ("group_id", "since", "until"):
            value = str((params.get(key) or [""])[0]).strip()
            if not value:
                continue
            if len(value) > 2000:
                self._json(400, {"ok": False, "error": f"{key} 过长"})
                return
            filters[key] = value
        limit_raw = str((params.get("limit") or [""])[0]).strip()
        if limit_raw:
            try:
                limit = int(limit_raw)
            except ValueError:
                self._json(400, {"ok": False, "error": "limit 参数无效"})
                return
            if not 1 <= limit <= 500:
                self._json(400, {"ok": False, "error": "limit 参数无效"})
                return
            filters["limit"] = limit
        self._lab_reply(lab.search(settings, q, **filters))
        return

    # GET /api/lab/replies
    def _route_get_api_lab_replies(self, path: str) -> None:  # noqa: N802 - 路由处理
        settings = self.server.settings  # type: ignore[attr-defined]
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query, max_num_fields=8)
        filters = {}
        limit_raw = str((params.get("limit") or [""])[0]).strip()
        if limit_raw:
            try:
                limit = int(limit_raw)
            except ValueError:
                self._json(400, {"ok": False, "error": "limit 参数无效"})
                return
            if not 1 <= limit <= 500:
                self._json(400, {"ok": False, "error": "limit 参数无效"})
                return
            filters["limit"] = limit
        self._lab_reply(lab.list_replies(settings, **filters))
        return

    # GET /api/backup
    def _route_get_api_backup(self, path: str) -> None:  # noqa: N802 - 路由处理
        database_path = Path(self.store.path)
        if not database_path.is_file():
            self._json(404, {"ok": False, "code": "database_missing", "error": "数据库文件不存在，无法备份"})
            return
        source = snapshot = None
        try:
            source_uri = database_path.resolve().as_uri() + "?mode=ro"
            source = sqlite3.connect(source_uri, uri=True, timeout=15)
            with tempfile.TemporaryDirectory(prefix="notice-hub-backup-") as snapshot_dir:
                snapshot_path = Path(snapshot_dir) / "snapshot.sqlite3"
                snapshot = sqlite3.connect(snapshot_path)
                source.backup(snapshot)
                snapshot.execute("PRAGMA journal_mode=DELETE")
                database_bytes = snapshot.serialize()
                snapshot.close()
                snapshot = None
                output = io.BytesIO()
                with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr("data/digest.sqlite3", database_bytes)
                filename = "notice-hub-backup-%s.zip" % dt.datetime.now().strftime("%Y-%m-%d")
                self._reply(
                    200,
                    output.getvalue(),
                    "application/zip",
                    "no-store",
                    [("Content-Disposition", 'attachment; filename="%s"' % filename), ("X-Content-Type-Options", "nosniff")],
                )
        except (OSError, sqlite3.Error, ValueError, zipfile.BadZipFile) as error:
            LOGGER.warning("数据库备份生成失败: %s", type(error).__name__)
            self._json(500, {"ok": False, "code": "backup_failed", "error": "数据库备份生成失败，请稍后重试"})
        finally:
            if source is not None:
                source.close()
            if snapshot is not None:
                snapshot.close()
        return

    # GET /api/qq/groups/status
    def _route_get_api_qq_groups_status(self, path: str) -> None:  # noqa: N802 - 路由处理
        owner = self.server.qq_group_refresh_owner  # type: ignore[attr-defined]
        self._json(200, owner.qq_group_status())
        return

    # GET /api/chaoxing/cookie/status
    def _route_get_api_chaoxing_cookie_status(self, path: str) -> None:  # noqa: N802 - 路由处理
        self._json(200, {"ok": True, **self._chaoxing_cookie_validation_payload()})
        return

    # GET /api/setup/status
    def _route_get_api_setup_status(self, path: str) -> None:  # noqa: N802 - 路由处理
        self._json(200, self._setup_status_payload())
        return

    # GET /api/inbox/image
    def _route_get_api_inbox_image(self, path: str) -> None:  # noqa: N802 - 路由处理
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query, max_num_fields=8)
        message_ids = query.get("msg_id", [])
        msg_id = message_ids[0] if len(message_ids) == 1 else ""
        if (
            not msg_id
            or len(msg_id) > 256
            or ".." in msg_id
            or "/" in msg_id
            or "\\" in msg_id
        ):
            self._json(400, {"ok": False, "error": "bad msg_id"})
            return
        files_dir = Path(self.server.settings.data_dir) / "files"  # type: ignore[attr-defined]
        try:
            if files_dir.is_symlink() or not files_dir.is_dir():
                raise OSError("attachment directory unavailable")
            image = self._inbox_image_of(files_dir, msg_id)
            content_type, _ = mimetypes.guess_type(image.name)
            if not content_type or not content_type.startswith("image/") or content_type == "image/svg+xml":
                raise OSError("unsupported attachment type")
            raw = image.read_bytes()
        except OSError:
            self._json(404, {"ok": False, "error": "image not found"})
            return
        self._reply(200, raw, content_type, "private, no-store", [
            ("Content-Disposition", "inline"),
            ("X-Content-Type-Options", "nosniff"),
        ])
        return

    def _inbox_image_of(self, files_dir: Path, msg_id: str) -> Path:
        """按 msg_id 找到要显示的那张原图；找不到（或附件事件重复）抛 OSError。

        判定只有一处（``attachments.pick_image``）；``_inbox_has_image`` 也走它，
        所以「列表里的 has_image」和「这个接口回 200」永远是同一个结论。

        两条来源：
        - 群附件事件（onebot-attachment）：``attach:<key>`` 是事件级 key，必须唯一，
          还得和库里那条记录对得上，沿用老规则；
        - 普通消息 / 历史回溯：图片按 ``files/<image_key(msg_id)>_<n>.<ext>`` 留存，
          同一条消息有多张图时取序号最小的那张（``_0``）——多张不是歧义。
        """
        match = re.fullmatch(r"onebot:(.+):attach:([0-9a-f]{16})", msg_id)
        if match:
            group_id, key = match.groups()
            with self.store._connect() as connection:
                message = connection.execute(
                    "SELECT msg_id, source, event, group_id FROM messages WHERE msg_id = ?",
                    (msg_id,),
                ).fetchone()
            if (message is None or message["source"] != "onebot-attachment"
                    or message["event"] != "GROUP_ATTACHMENT"
                    or message["group_id"] != group_id
                    or msg_id != f"onebot:{message['group_id']}:attach:{key}"):
                raise OSError("image not found")
            image = pick_image(files_dir.parent, msg_id, prefix=key, unique=True)
        else:
            image = pick_image(files_dir.parent, msg_id)
        if image is None:
            raise OSError("attachment is missing or ambiguous")
        return image

    def _inbox_has_image(self, msg_id: str) -> bool:
        """收件箱列表的 has_image：和 ``GET /api/inbox/image`` 用同一个判定函数。"""
        files_dir = Path(self.server.settings.data_dir) / "files"  # type: ignore[attr-defined]
        if files_dir.is_symlink():
            return False
        try:
            self._inbox_image_of(files_dir, msg_id)
        except OSError:
            return False
        return True

    # GET /api/announcements
    def _route_get_api_announcements(self, path: str) -> None:  # noqa: N802 - 路由处理
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query, max_num_fields=8)
        force = str((query.get("refresh") or [""])[0]).strip().lower() in {"1", "true", "yes"}
        self._json(200, _announcement_response(self.server, force=force))
        return

    # GET /api/update
    def _route_get_api_update(self, path: str) -> None:  # noqa: N802 - 路由处理
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query, max_num_fields=8)
        force = str((query.get("check") or [""])[0]).strip().lower() in {"1", "true", "yes"}
        self._json(200, _update_response(self.server, force=force))
        return

    # GET /floatball / /floatball.html
    def _route_get_floatball(self, path: str) -> None:  # noqa: N802 - 路由处理
        from .floatball_page import FLOATBALL_PAGE_HTML

        self._reply(200, FLOATBALL_PAGE_HTML.encode("utf-8"), "text/html; charset=utf-8", "no-store")
        return

    # GET / / /index.html
    def _route_get_(self, path: str) -> None:  # noqa: N802 - 路由处理
        channel = _update_channel_name(self.server.settings)
        if channel == "off": channel = ""
        version = _current_version()
        version_badge = "<span class=\"channel-badge-version\"> · %s</span>" % version if version and version != "未知" else ""
        badge = {"beta": "<span class=\"channel-badge channel-badge-beta\">BETA 测试版%s</span>" % version_badge, "stable": "<span class=\"channel-badge channel-badge-stable\">正式版%s</span>" % version_badge}.get(channel, "")
        # beta 的页头 logo 换成测试版配色（和 assets/icon-256-beta.png 同一个琥珀 #B5481E），正式版不注入变量
        brand_vars = "html{--brand-ink:#B5481E;--brand-accent:#B5481E}" if channel == "beta" else ""
        resolved_channel = _update_channel_name(self.server.settings)
        resolved_channel = resolved_channel if resolved_channel in {"stable", "beta"} else ""
        page = (PAGE_HTML.replace("<!--CHANNEL_BADGE-->", badge)
                .replace("<!--CHANNEL_LOGO-->", brand_vars)
                .replace("<!--UPDATE_CHANNEL-->", resolved_channel))
        self._html(page)
        return

    # GET /setup
    def _route_get_setup(self, path: str) -> None:  # noqa: N802 - 路由处理
        self._redirect_home()
        return

    # GET /api/napcat/status
    def _route_get_api_napcat_status(self, path: str) -> None:  # noqa: N802 - 路由处理
        self._json(200, self._napcat_status_payload())
        return

    # GET /api/selfcheck
    def _route_get_api_selfcheck(self, path: str) -> None:  # noqa: N802 - 路由处理
        global _SELFCHECK_RUNNING, _SELFCHECK_DONE, _SELFCHECK_REPORT
        # 页面首次加载会并发打两次（健康面板 + 星标提示）。每次自检都要探 NapCat 端口、
        # 微信目录、Tailscale 子进程、网卡清单，跑两遍等于把首屏等待翻倍；这里做单飞：
        # 第二次请求等正在跑的那次算完、直接复用结果，不再另起一遍。
        with _SELFCHECK_LOCK:
            running = _SELFCHECK_RUNNING
            done_event = _SELFCHECK_DONE
        if running:
            finished = done_event.wait(SELFCHECK_WAIT_SECONDS)
            with _SELFCHECK_LOCK:
                report = _SELFCHECK_REPORT
                still_running = _SELFCHECK_RUNNING
            if report is not None:
                payload = dict(report)
                payload["checking"] = bool(still_running)
                payload["cached"] = True
                self._json(200, payload)
                return
            self._json(200, {"ok": False, "checking": True, "cached": False, "items": [], "error": "正在检测…" if not finished else "自检没有返回结果，请再点一次「重新检测」"})
            return
        with _SELFCHECK_LOCK:
            _SELFCHECK_RUNNING = True
            _SELFCHECK_DONE = threading.Event()
            done_event = _SELFCHECK_DONE
        try:
            report = self._compute_selfcheck_report()
        except Exception:
            with _SELFCHECK_LOCK:
                _SELFCHECK_RUNNING = False
                done_event.set()
            raise
        with _SELFCHECK_LOCK:
            _SELFCHECK_REPORT = report
            _SELFCHECK_RUNNING = False
            done_event.set()
        payload = dict(report)
        payload["checking"] = False
        payload["cached"] = False
        self._json(200, payload)
        return

    def _compute_selfcheck_report(self) -> dict[str, Any]:
        # 8 项里 7 项是各自独立的外部探测（NapCat 端口、微信目录、Tailscale 子进程、
        # 托管进程），串行相加就是十几二十秒，首屏等待全卡在这一段。改成并发跑：
        # 只影响耗时，行的条数、顺序、字段结构一个字都不变。每项还背着真实探测，不读缓存顶替。
        probes = {
            "doctor": self._napcat_doctor_payload,
            "napcat": self._napcat_status_payload,
            "hosting": self._hosting_status_payload,
            "sync": self._sync_info_response,
            "dingtalk": self._dingtalk_status_payload,
            "wechat": self._wechat_status_payload,
            "chaoxing": self._chaoxing_status_payload,
        }
        results: dict[str, Any] = {}
        timed_out: list[str] = []
        overview_sources: dict[str, Any] = {}
        started = time.monotonic()
        deadline = started + SELFCHECK_ITEM_TIMEOUT_SECONDS
        # ponytail: 超时的探测线程不会被杀，后台自行结束，结果丢弃；若将来某项可能永久卡死，改用 daemon 线程 + 队列。
        pool = ThreadPoolExecutor(max_workers=len(probes) + 1, thread_name_prefix="selfcheck")
        try:
            futures = {key: pool.submit(probe) for key, probe in probes.items()}
            for key, future in futures.items():
                try:
                    results[key] = future.result(timeout=max(0.0, deadline - time.monotonic()))
                except FuturesTimeout:
                    results[key] = None
                    timed_out.append(key)

            doctor = results.get("doctor") or {}
            napcat = results.get("napcat") or {}
            hosting_status = results.get("hosting") or {}
            sync_code, sync_status = results.get("sync") or (0, {})
            dingtalk = results.get("dingtalk") or {}
            wechat = results.get("wechat") or {}
            chaoxing = results.get("chaoxing") or {}
            # 每个通道的 status payload 都要探一次外部状态（NapCat 端口、微信目录、Tailscale
            # 子进程），一次请求里算两遍会让自检多等十几秒，所以先算一次再传给总览复用。
            # 空值也要放进去：库里有这个键 = 这次已经探过了（哪怕没取到），总览不许再探。
            self._precomputed_status = {
                "napcat": napcat, "dingtalk": dingtalk, "wechat": wechat, "chaoxing": chaoxing,
                # doctor 也带上：总览若要用自检里的断点结论，别再跑一遍探针。
                "doctor": doctor,
            }
            # 收尾（总览）也放进同一个池、同一份预算里算：它自己会探推送与 Funnel，
            # 慢起来一两秒；超了就用空 sources，绝不为补数据再串行重探一遍。
            try:
                overview = pool.submit(self._overview_payload).result(
                    timeout=max(0.0, deadline + SELFCHECK_TAIL_SECONDS - time.monotonic())
                )
                overview_sources = (overview or {}).get("sources") or {}
            except FuturesTimeout:
                overview_sources = {}
        finally:
            self._precomputed_status = None
            # 卡住的探测线程收不回来（Python 线程不能杀），但请求不能跟着干等。
            pool.shutdown(wait=False, cancel_futures=True)
        report = selfcheck.check(
            self.server.settings,  # type: ignore[attr-defined]
            overview=overview_sources,
            doctor=doctor,
            napcat=napcat,
            hosting=hosting_status,
            dingtalk=dingtalk,
            wechat=wechat,
            chaoxing=chaoxing,
            sync=sync_status if sync_code == 200 else {},
            push_channels=self.server.settings.push_channels(),  # type: ignore[attr-defined]
        )
        if timed_out:
            self._mark_selfcheck_timeouts(report, timed_out)
        service = getattr(self.server, "service", None)
        stamp = getattr(service, "last_push_at", None)
        report["last_push_at"] = stamp.strftime("%Y-%m-%dT%H:%M:%S") if stamp else ""
        return report

    def _mark_selfcheck_timeouts(self, report: dict[str, Any], timed_out: list[str]) -> None:
        """超时的探测只把自己那一行降成 warn，其余行照常给结果。

        一行可能由两个探测共同决定（QQ 登录 = doctor + napcat），两个都超时才动这一行。
        """
        rows = {_SELFCHECK_PROBE_ROWS[key] for key in timed_out if key in _SELFCHECK_PROBE_ROWS}
        for row_key in list(rows):
            owners = [probe for probe, row in _SELFCHECK_PROBE_ROWS.items() if row == row_key]
            if not all(probe in timed_out for probe in owners):
                rows.discard(row_key)
        if not rows:
            return
        # 没查完的行说「正在检测」，不说「检测超时」：冷启动第一次打开设置页时这项可能还在跑，
        # 把「慢」写成「坏」会让用户以为自检失败。没等到的结果会在后台跑完并被丢弃。
        hint = "这一项还没查完（超过 %d 秒），先在后台继续；稍后点「重新检测」看结果。" % int(
            SELFCHECK_ITEM_TIMEOUT_SECONDS
        )
        for row in report.get("items") or []:
            if row.get("key") in rows:
                row["state"] = "warn"
                row["value"] = "正在检测"
                row["hint"] = hint

    # GET /api/napcat/doctor
    def _route_get_api_napcat_doctor(self, path: str) -> None:  # noqa: N802 - 路由处理
        self._json(200, self._napcat_doctor_payload())
        return

    # GET /api/groups/suggest
    def _route_get_api_groups_suggest(self, path: str) -> None:  # noqa: N802 - 路由处理
        if group_suggest is None:
            self._json(200, {"ok": False, "source": "heuristic", "groups": [], "counts": {}, "error": "群组建议模块不可用"}); return
        try:
            raw = self._napcat("get_group_list")
            self._remember_group_names(raw)
            groups = raw.get("data", []) if raw.get("ok") else []
            if not raw.get("ok"):
                self._json(200, {"ok": False, "source": "heuristic", "groups": [], "counts": {}, "error": raw.get("error", "NapCat error")}); return
            self._json(200, group_suggest.suggest(self.server.settings, groups))  # type: ignore[attr-defined]
        except Exception as error:  # noqa: BLE001
            self._json(200, {"ok": False, "source": "heuristic", "groups": [], "counts": {}, "error": str(error)})
        return

    # GET /api/napcat/groups
    def _route_get_api_napcat_groups(self, path: str) -> None:  # noqa: N802 - 路由处理
        result = self._napcat("get_group_list")
        self._remember_group_names(result)
        if not result.get("ok"):
            self._json(200, {"ok": False, "groups": [], "error": result.get("error", "NapCat error")})
        else:
            self._json(200, {"ok": True, "groups": [{"group_id": str(g.get("group_id", "")), "name": str(g.get("group_name", "")), "member_count": g.get("member_count", 0), "selected": str(g.get("group_id", "")) in self.server.settings.group_whitelist} for g in result["data"]]})
        return

    # GET /api/napcat/qrcode
    def _route_get_api_napcat_qrcode(self, path: str) -> None:  # noqa: N802 - 路由处理
        raw = None
        if napcat_admin is not None:
            try:
                raw = napcat_admin.qrcode_bytes(self.server.settings)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                raw = None
        if not raw:
            qr = Path(self.server.settings.napcat_qr_path)  # type: ignore[attr-defined]
            raw = qr.read_bytes() if qr.is_file() else None
        if not raw:
            self._json(404, {"ok": False, "error": "QR code not found"})
            return
        self._png(raw)
        return

    # GET /api/sync/info
    def _route_get_api_sync_info(self, path: str) -> None:  # noqa: N802 - 路由处理
        status, payload = self._sync_info_response()
        self._json(status, payload)
        return

    # GET /api/sync/test
    def _route_get_api_sync_test(self, path: str) -> None:  # noqa: N802 - 路由处理
        port = int(self.server.server_address[1])
        token = str(getattr(self.server, "token", "") or "")
        requested = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query, max_num_fields=8).get("url", [""])[0].strip()
        if requested.lower().startswith("webcal:"):
            requested = "http:" + requested[len("webcal:"):]
        parts = urllib.parse.urlsplit(requested)
        allowed = {urllib.parse.urlsplit(item["url"])[:3] for item in _advertised_calendars(port, token)}
        allowed |= {("http", f"127.0.0.1:{port}", "/calendar.ics"), ("http", f"localhost:{port}", "/calendar.ics")}
        if parts[:3] not in allowed:
            self._json(400, {"ok": False, "error": "只能测试本服务公布的日历地址"})
            return
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(urllib.request.Request(requested, headers={"User-Agent": "qq-notice-hub-selftest"}), timeout=10) as response:
                body = response.read().decode("utf-8", "replace")
        except Exception as error:  # noqa: BLE001
            LOGGER.info("自测日历地址失败：%s", error)
            self._json(502, {"ok": False, "error": f"服务器读取该地址失败：{error}"})
            return
        if not body.startswith("BEGIN:VCALENDAR"):
            self._json(502, {"ok": False, "error": "地址返回的内容不是 iCalendar"})
            return
        count = sum(1 for line in body.splitlines() if line.strip() == "BEGIN:VEVENT")
        self._json(200, {"ok": True, "events": count, "url": requested})
        return

    # GET /api/sync/qr.png
    def _route_get_api_sync_qr_png(self, path: str) -> None:  # noqa: N802 - 路由处理
        try:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query, keep_blank_values=True, max_num_fields=4)
            values = query.get("text", [])
            if len(values) != 1:
                raise ValueError("二维码内容无效")
            text = values[0]
            if not text or len(text) > MAX_SYNC_QR_CHARS or len(text.encode("utf-8")) > MAX_SYNC_QR_CHARS or any(not char.isprintable() or char.isspace() for char in text):
                raise ValueError("二维码内容为空、过长或包含无效字符")
            parsed = urllib.parse.urlsplit(text)
            if parsed.scheme.lower() not in {"webcal", "http", "https"} or not parsed.netloc or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("二维码内容必须是带主机名且不含凭据的日历 URL")
            _ = parsed.port
            raw = qr_encoder.png_bytes(text, scale=10, border=4)
        except (ValueError, UnicodeEncodeError) as error:
            self._json(400, {"ok": False, "error": str(error) or "二维码内容无效"})
            return
        except Exception as error:  # noqa: BLE001
            LOGGER.exception("生成日历订阅二维码失败")
            self._json(500, {"ok": False, "error": str(error) or "二维码生成失败"})
            return
        self._png(raw)
        return

    # GET /api/app/qr.png
    def _route_get_api_app_qr_png(self, path: str) -> None:  # noqa: N802 - 路由处理
        try:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query, keep_blank_values=True, max_num_fields=4)
            values = query.get("base", [])
            if len(values) != 1:
                raise ValueError("手机地址无效")
            base = values[0].strip().rstrip("/")
            parsed = urllib.parse.urlsplit(base)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("手机地址必须是带主机名、不带凭据的 http 或 https 地址")
            _ = parsed.port
            token = str(getattr(self.server, "token", "") or "")
            errors = _pair_qr_errors(self.server.settings, token, base)  # type: ignore[attr-defined]
            if errors:
                raise ValueError("；".join(errors))
            text = "noticehub://connect?" + urllib.parse.urlencode({"base": base, "token": token})
            if len(text) > MAX_SYNC_QR_CHARS:
                raise ValueError("地址太长，生成不了二维码")
            raw = qr_encoder.png_bytes(text, scale=8, border=4)
        except (ValueError, UnicodeEncodeError) as error:
            self._json(400, {"ok": False, "error": str(error) or "二维码内容无效"})
            return
        except Exception as error:  # noqa: BLE001
            LOGGER.exception("生成手机配对二维码失败")
            self._json(500, {"ok": False, "error": str(error) or "二维码生成失败"})
            return
        self._png(raw)
        return

    # GET /calendar
    def _route_get_calendar(self, path: str) -> None:  # noqa: N802 - 路由处理
        self._redirect_home()
        return

    # GET /calendar.ics
    def _route_get_calendar_ics(self, path: str) -> None:  # noqa: N802 - 路由处理
        from noticehub.application.calendar import render_calendar_ics

        self._ics(render_calendar_ics(self.store))
        return

    # GET /api/history/floor
    def _route_get_api_history_floor(self, path: str) -> None:  # noqa: N802 - 路由处理
        from noticehub.application.jobs import probe_history_floor

        group_id = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query, max_num_fields=8).get("group_id") or [""])[0]
        text = str(group_id or "").strip()
        if not text or len(text) > 128:
            self._json(400, {"ok": False, "floor_ts": None, "total_seen": 0, "error": "群号无效"}); return
        if catchup is None:
            self._json(503, {"ok": False, "floor_ts": None, "total_seen": 0, "error": "回溯模块暂不可用"}); return
        try:
            # 次数上限压到 2 页（每页 1000、单页超时 15s）：最坏 ~30s，不再让浏览器干等 300s。
            result = probe_history_floor(
                group_id,
                probe=lambda text, max_pages: catchup.available_floor(self.server.settings, text, max_pages=max_pages),  # type: ignore[attr-defined]
                max_pages=HISTORY_FLOOR_MAX_PAGES,
            )
        except ValueError as error:
            self._json(400, {"ok": False, "floor_ts": None, "total_seen": 0, "error": str(error)}); return
        except Exception as error:  # noqa: BLE001
            LOGGER.exception("探测 NapCat 缓存底线失败")
            self._json(200, {"ok": False, "floor_ts": None, "total_seen": 0, "pages": 0, "partial": False, "note": "", "error": str(error) or "探测失败"}); return
        self._json(200, result)
        return

    # GET /api/history/fetch/status
    def _route_get_api_history_fetch_status(self, path: str) -> None:  # noqa: N802 - 路由处理
        from noticehub.application.jobs import poll_job

        self._json(200, poll_job("history", snapshot=_job_snapshot)); return

    # GET /api/inbox/classify/status
    def _route_get_api_inbox_classify_status(self, path: str) -> None:  # noqa: N802 - 路由处理
        from noticehub.application.jobs import poll_job

        snapshot = poll_job("classify", snapshot=_job_snapshot)
        # 取消后前端要能读到 state=cancelled（stage 已有，state 是给取消按钮看的别名）。
        snapshot["state"] = snapshot.get("stage")
        self._json(200, snapshot); return

    # GET /api/inbox
    def _route_get_api_inbox(self, path: str) -> None:  # noqa: N802 - 路由处理
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query, max_num_fields=8)
        verdict = (query.get("verdict") or [""])[0].strip()
        if verdict not in {"", "notice", "suspect", "noise", "promoted"}:
            self._json(400, {"ok": False, "error": "判定类别无效"}); return
        group_id = (query.get("group_id") or [""])[0].strip()
        q = (query.get("q") or [""])[0].strip()
        try:
            limit = int((query.get("limit") or ["50"])[0])
            offset = int((query.get("offset") or ["0"])[0])
        except ValueError:
            self._json(400, {"ok": False, "error": "分页参数无效"}); return
        if not 1 <= limit <= 100 or not 0 <= offset <= 10000000 or len(group_id) > 128 or len(q) > 200:
            self._json(400, {"ok": False, "error": "筛选或分页参数超出范围"}); return
        try:
            counts = self.store.inbox_counts()
            promoted = True if verdict == "promoted" else None
            selected_verdict = None if verdict in {"", "promoted"} else verdict
            filters = {"verdict": selected_verdict, "group_id": group_id or None, "q": q or None, "promoted": promoted}
            items = self.store.inbox_items(**filters, limit=limit, offset=offset)
            for item in items:
                item["has_image"] = self._inbox_has_image(str(item.get("msg_id") or ""))
            if group_id or q:
                total = 0
                count_offset = 0
                while True:
                    batch = self.store.inbox_items(**filters, limit=1000, offset=count_offset)
                    total += len(batch)
                    if len(batch) < 1000:
                        break
                    count_offset += len(batch)
            elif verdict == "promoted":
                total = int(counts.get("promoted", 0))
            elif verdict:
                total = int(counts.get(verdict, 0))
            else:
                total = sum(int(counts.get(key, 0)) for key in ("notice", "suspect", "noise"))
            self._json(200, {"ok": True, "items": items, "counts": counts, "total": total})
        except Exception as error:  # noqa: BLE001
            LOGGER.exception("读取收件箱失败")
            self._json(200, {"ok": False, "items": [], "counts": {}, "total": 0, "error": str(error) or "收件箱暂不可用"})
        return

    # GET /api/dingtalk/status
    def _route_get_api_dingtalk_status(self, path: str) -> None:  # noqa: N802 - 路由处理
        self._json(200, self._dingtalk_status_payload())
        return

    # GET /api/dingtalk/groups
    def _route_get_api_dingtalk_groups(self, path: str) -> None:  # noqa: N802 - 路由处理
        try:
            settings = self.server.settings  # type: ignore[attr-defined]
            reader = DingTalkLocalReader(settings, lambda _record: True, logger=LOGGER)
            groups = reader.list_groups()
            groups, suggest_source, suggest_error = _suggest_external_groups(settings, self.server.store, "dingtalk", groups)
            self._json(200, {"ok": True, "groups": groups, "suggest_source": suggest_source, "suggest_error": suggest_error})
        except RuntimeError as error:
            cause = error.__cause__ or error
            detail = _redact_secrets(str(cause) or cause.__class__.__name__)[:240]
            self._json(200, {
                "ok": False,
                "groups": [],
                "category": "database_unavailable" if "未找到" in str(error) else "read_failed",
                "error": str(error),
                "detail": detail,
            })
        except Exception as error:  # noqa: BLE001
            detail = _redact_secrets(str(error) or error.__class__.__name__)[:240]
            LOGGER.error("钉钉群列表读取失败（%s）：%s", type(error).__name__, detail)
            self._json(200, {
                "ok": False,
                "groups": [],
                "category": "read_failed",
                "error": "钉钉群读取失败",
                "detail": detail,
            })
        return

    # GET /api/wechat/status
    def _route_get_api_wechat_status(self, path: str) -> None:  # noqa: N802 - 路由处理
        self._json(200, self._wechat_status_payload())
        return

    # GET /api/wechat/groups
    def _route_get_api_wechat_groups(self, path: str) -> None:  # noqa: N802 - 路由处理
        settings = self.server.settings  # type: ignore[attr-defined]
        # 用户主动点「拉取我的群」：可能要重读微信存储位置（刚在设置页改过），先让探测缓存失效。
        wechat_clear_storage_cache()
        if wechat_locate_storage(str(getattr(settings, "wechat_dir", "") or "").strip() or None) is None:
            fallback = (
                "未找到微信本地数据库。如果微信改过存储位置，到设置页「微信数据目录」填一下微信文件目录"
                "（例如 D:\\Wechat files）；也可以在这台机器的 .env 里加 QQ_DIGEST_WECHAT_DIR=同样路径，"
                "保存即生效，不用重启。"
            )
            self._json(200, {"ok": False, "groups": [], "error": wechat_storage_hint() or fallback})
            return
        whitelist = set(settings.wechat_group_whitelist or ())
        aliases = dict(settings.wechat_group_aliases or {})
        LOGGER.info("开始读取微信群名（首次需扫描微信内存，可能耗时数分钟）")
        started = time.monotonic()
        # group_names 自身吞掉异常并返回 {}，密钥/内存字节不会外泄。
        names = wechat_group_names(None)
        elapsed = time.monotonic() - started
        # 没拿到群时把「为什么」说清楚：没登录 / 没候选 / 候选解不开（版本没适配）。
        failure = wechat_last_key_failure() if not names else {}
        failure_category = str(failure.get("category") or "")
        read_error = str(failure.get("message") or "") or ("读到了微信数据库，但没解析到群名；微信可能没有群，或数据库结构有变" if not names else "")
        _WECHAT_READ_STATUS.update({
            "at": now_local().strftime("%Y-%m-%d %H:%M:%S"),
            "seconds": round(elapsed, 1),
            "error": read_error,
            "error_category": str(failure.get("category") or ""),
            "groups": len(names),
        })
        LOGGER.info("微信群名读取完成：%d 个群，耗时 %.1f 秒", len(names), elapsed)
        groups = [
            {"id": gid, "name": name, "alias": aliases.get(gid, ""), "selected": gid in whitelist}
            for gid, name in sorted(names.items(), key=lambda item: str(item[1]))
        ]
        groups, suggest_source, suggest_error = _suggest_external_groups(settings, self.server.store, "wechat", groups)
        payload: dict[str, Any] = {"ok": True, "groups": groups, "suggest_source": suggest_source, "suggest_error": suggest_error}
        if not names:
            # 0 个群时把原因一起交给前端：否则页面只剩「没有解析到群。」，用户无从下手。
            payload["error"] = read_error
            payload["error_category"] = failure_category
            payload["error_hint"] = wechat_scan_error_hint(failure_category)
        self._json(200, payload)
        return

    # GET /api/chaoxing/courses
    def _route_get_api_chaoxing_courses(self, path: str) -> None:  # noqa: N802 - 路由处理
        cookie = str(self.server.settings.chaoxing_cookie or "")  # type: ignore[attr-defined]
        if not cookie:
            self._json(400, {"ok": False, "error": "请先填学习通登录信息"})
            return
        try:
            courses = ChaoxingClient(cookie, timeout=self.server.settings.http_timeout, logger=LOGGER).my_courses()  # type: ignore[attr-defined]
            self._json(200, {"ok": True, "courses": courses})
        except ChaoxingAuthError:
            self._json(502, {"ok": False, "error": "学习通登录已失效：请在浏览器里重新登录学习通，再从托盘菜单重新读取一次登录信息（手工复制的内容读不全，可能不完整）"})
        except ChaoxingError as error:
            self._json(502, {"ok": False, "error": str(error)})
        return

    # GET /api/chaoxing/status
    def _route_get_api_chaoxing_status(self, path: str) -> None:  # noqa: N802 - 路由处理
        self._json(200, self._chaoxing_status_payload())
        return

    # GET /api/settings
    def _route_get_api_settings(self, path: str) -> None:  # noqa: N802 - 路由处理
        if hosting is None:
            self._json(200, {"ok": False, "error": "托管模块不可用"}); return
        try:
            prefs = hosting.prefs(self.server.settings)  # type: ignore[attr-defined]
            with self.server.catchup_preferences_lock:  # type: ignore[attr-defined]
                prefs.update(self.server.catchup_preferences)  # type: ignore[attr-defined]
            settings = self.server.settings  # type: ignore[attr-defined]
            cookie = str(settings.chaoxing_cookie or "")
            cx_status = chaoxing_status(settings, self.store)
            prefs.update({"dingtalk_enabled": bool(settings.dingtalk_enabled), "dingtalk_group_whitelist": list(settings.dingtalk_group_whitelist), "chaoxing_enabled": bool(settings.chaoxing_enabled), "chaoxing_group_whitelist": list(settings.chaoxing_group_whitelist), "chaoxing_scan_interval_seconds": int(settings.chaoxing_scan_interval_seconds), "chaoxing_cookie": {"has_cookie": bool(cookie)}, "chaoxing_status": cx_status, "last_sync_at": cx_status["last_sync_at"], "expired": cx_status["expired"], "has_cookie": bool(cookie), "wechat_enabled": bool(settings.wechat_enabled), "wechat_group_whitelist": list(settings.wechat_group_whitelist), "wechat_group_aliases": dict(settings.wechat_group_aliases), "wechat_scan_interval_seconds": int(settings.wechat_scan_interval_seconds), "lab_search": bool(settings.lab_search), "lab_replies": bool(settings.lab_replies), "lab_events": bool(settings.lab_events), "lab_rules": bool(settings.lab_rules), "community_telemetry": bool(settings.community_telemetry), "manual_key": bool(str(getattr(settings, "wechat_key", "") or "").strip()), "wechat_dir": str(getattr(settings, "wechat_dir", "") or "")})
            self._json(200, prefs)
        except Exception as error:  # noqa: BLE001
            self._json(200, {"ok": False, "error": str(error)})
        return

    # GET /api/hosting/status
    def _route_get_api_hosting_status(self, path: str) -> None:  # noqa: N802 - 路由处理
        self._json(200, self._hosting_status_payload())
        return

    # GET /api/firewall/status
    def _route_get_api_firewall_status(self, path: str) -> None:  # noqa: N802 - 路由处理
        settings = self.server.settings  # type: ignore[attr-defined]
        self._json(200, _firewall_status_payload(settings))
        return

    # GET /api/pair/info
    def _route_get_api_pair_info(self, path: str) -> None:  # noqa: N802 - 路由处理
        settings = self.server.settings  # type: ignore[attr-defined]
        public = _tailscale_public_app_url(settings.web_port)
        hosts = _lan_hosts()
        lan = [f"http://{host}:{settings.web_port}" for host in hosts]
        candidates = _pair_candidates(hosts, settings.web_port, public)
        # Keep `base` for older clients, using the same LAN-first choice as `candidates`.
        recommended = next((item for item in candidates if item["recommended"]), None)
        base = recommended["url"] if recommended else ""
        token = str(getattr(self.server, "token", "") or "")
        qr_errors = _pair_qr_errors(settings, token, base, public)
        firewall = _firewall_allows(settings.web_port, getattr(settings, "data_dir", None))
        self._json(
            200,
            {
                "ok": True,
                "public_url": public,
                "public_enabled": bool(public),
                "tailscale_ready": bool(_tailscale_executable()),
                "base": base,
                "lan": lan,
                "candidates": candidates,
                "qr_error": "；".join(qr_errors),
                "firewall": firewall,
                "steps": _pair_help(base)["steps"],
                "no_public_hint": _pair_help(base)["no_public_hint"],
            },
        )
        return

    # GET /api/config/audit
    def _route_get_api_config_audit(self, path: str) -> None:  # noqa: N802 - 路由处理
        settings = self.server.settings  # type: ignore[attr-defined]
        # ponytail: 审计只报**已经生效**的值，探测归各自的读路径（微信定位有 45 秒缓存、NapCat
        # 状态由面板轮询维护），这里绝不重复探测——否则设置页每刷一次就多起一次 PowerShell。
        # 微信那项只读缓存（cache_empty=False + 空 producer，不写也不探测）：缓存冷了就先不报，
        # 等状态栏那次轮询把它填上，下次审计自然就有值。
        located = ttl_cache.get(
            WECHAT_LOCATE_CACHE_KEY, WECHAT_LOCATE_CACHE_SECONDS, lambda: None, cache_empty=False
        )
        snapshot = {
            "wechat_storage": str(located) if located else "",
            "napcat_api_url": str(getattr(settings, "napcat_api_url", "") or ""),
        }
        self._json(200, {"ok": True, "items": audit_config(settings, snapshot)})
        return

    # GET /api/tunnel/status
    def _route_get_api_tunnel_status(self, path: str) -> None:  # noqa: N802 - 路由处理
        self._json(200, _tunnel_status_payload(self.server.settings.web_port))  # type: ignore[attr-defined]
        return

    # GET /api/pair/pending
    def _route_get_api_pair_pending(self, path: str) -> None:  # noqa: N802 - 路由处理
        self._json(200, {"ok": True, "pending": pairing.pending()})
        return

    # GET /api/tasks
    def _route_get_api_tasks(self, path: str) -> None:  # noqa: N802 - 路由处理
        settings = self.server.settings  # type: ignore[attr-defined]
        self._json(200, list_task_board(self.store, settings, now=now_local()))
        return

    # GET /api/notices
    def _route_get_api_notices(self, path: str) -> None:  # noqa: N802 - 路由处理
        from noticehub.application.notices import list_notices

        try:
            self._json(200, list_notices(self.store))
        except Exception as error:  # noqa: BLE001 - 读库失败不能断连接，页面要能显示原因
            LOGGER.exception("读取推送记录失败")
            self._json(200, {"items": [], "error": f"读取推送记录失败：{error}"})
        return

    # GET /api/insights
    def _route_get_api_insights(self, path: str) -> None:  # noqa: N802 - 路由处理
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query, max_num_fields=8)
        try:
            days = int((query.get("days") or ["30"])[0])
        except (TypeError, ValueError):
            days = 30
        days = max(1, min(365, days))
        self._json(
            200,
            {
                "stats": self.store.correction_stats(days=days),
                "insights": self.store.correction_insights(days=days),
            },
        )
        return

    # GET /api/overview
    def _route_get_api_overview(self, path: str) -> None:  # noqa: N802 - 路由处理
        self._json(200, self._overview_payload())
        return

    # GET /api/meta
    def _route_get_api_meta(self, path: str) -> None:  # noqa: N802 - 路由处理
        settings = self.server.settings  # type: ignore[attr-defined]
        manager = getattr(self.server, "meta_provider", None)
        info: dict[str, Any] = {}
        if callable(manager):
            try:
                info = manager() or {}
            except Exception:  # noqa: BLE001 - 元信息失败不影响页面
                info = {}
        self._json(
            200,
            {
                "groups": list(info.get("groups") or []),
                "channels": list(info.get("channels") or []),
                "reminders": info.get("reminders") or "",
                "started_at": info.get("started_at") or "",
                "insights": list(info.get("insights") or []),
                "push_budget": info.get("push_budget") or "",
                "quiet_hours": info.get("quiet_hours") or "",
                "channel": settings.update_channel,
                "web_port": int(settings.web_port),
                "web_port_source": web_port_source(settings.env_file),
                "push": _push_setting_state(settings),
                "push_help": PUSH_HELP_LINKS,
                "llm": _llm_setting_state(settings),
                "providers": LLM_PROVIDERS,
                "stats": task_stats(self.store),
            },
        )
        return

    # GET /js/*（前缀模式，免鉴权；未命中返回 False，让调用方继续走鉴权与 404）
    def _route_get_js(self, path: str) -> bool:  # noqa: N802 - 路由处理
        js_root = (Path(__file__).parent / "web_static" / "js").resolve()
        script = (js_root / path[len("/js/"):]).resolve()
        if script.parent == js_root and script.suffix == ".js" and script.is_file():
            self._asset(script.read_text(encoding="utf-8"), "text/javascript; charset=utf-8")
            return True
        return False

    def do_GET(self) -> None:  # noqa: N802 - 基类命名
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if self._respond_if_throttled():
            return
        entry = routes.GET_ROUTES.get(path)
        if entry is not None and entry[1]:  # 免鉴权路由：静态资源、配对、向导
            getattr(self, entry[0])(path)
            return
        # 前缀模式路由（/js/*）：静态脚本不鉴权，未命中就继续往下走鉴权与 404。
        pattern = routes.match_pattern("GET", path)
        if pattern is not None and getattr(self, pattern[0])(path):
            return
        if not self._authorized():
            # 浏览器直接打开/刷新/走书签进来时，要么没有令牌、要么带着**另一份安装**
            # （同一个端口）留下的令牌。回一坨 JSON 只会让人以为程序坏了 —— 这时给个
            # 说人话的页面，并告诉他怎么正确地重开。
            if "text/html" in str(self.headers.get("Accept", "")):
                self._html(UNAUTHORIZED_HTML, status=401)
                return
            self._json(401, {"ok": False, "error": "invalid token"})
            return
        if entry is not None:
            getattr(self, entry[0])(path)
            return
        self._json(404, {"ok": False, "error": "not found"})

    # POST /api/onboarding/accept
    def _route_post_api_onboarding_accept(self, path: str) -> None:  # noqa: N802 - 路由处理
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._reject_oversized_body(length)
            return
        if length < 0:
            self._json(400, {"ok": False, "error": "bad body"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"ok": False, "error": "bad json"})
            return
        if not isinstance(payload, dict):
            # 合法 JSON 但不是对象（数组/字符串/null）：前端固定发 '{}'，
            # 其余形态没有意义，直接 400 而不是让下面崩掉连接。
            self._json(400, {"ok": False, "error": "bad json"})
            return
        accepted_at = self.store.meta_get("onboarding_accepted_at", "")
        if not accepted_at:
            accepted_at = iso(now_local())
            self.store.meta_set("onboarding_accepted_at", accepted_at)
        self._json(200, {"ok": True, "accepted_at": accepted_at})
        return

    # POST /api/pair/start
    def _route_post_api_pair_start(self, path: str) -> None:  # noqa: N802 - 路由处理
        try:
            result = pairing.start(device=self._device_label(), source=self._client_address())
        except pairing.PairingRateLimited as error:
            # 红队 S7：同一来源 10 分钟内最多发起 3 次，挡住拿「发起配对」当探针的脚本。
            self._json(429, {"ok": False, "error": str(error)})
            return
        except ValueError as error:
            self._json(400, {"ok": False, "error": str(error)})
            return
        self._json(200, {"ok": True, **result})
        return

    # POST /api/pair/pending/clear（机主一键清空挂起的配对请求）
    def _route_post_api_pair_pending_clear(self, path: str) -> None:  # noqa: N802 - 路由处理
        cleared = pairing.clear_pending()
        self._json(200, {"ok": True, "cleared": cleared})
        return

    # POST /api/session/exchange（公开路由：令牌换 HttpOnly 会话 Cookie）
    def _route_post_api_session_exchange(self, path: str) -> None:  # noqa: N802 - 路由处理
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._reject_oversized_body(length)
            return
        if length < 0:
            self._json(400, {"ok": False, "error": "bad body"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"ok": False, "error": "bad json"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"ok": False, "error": "bad json"})
            return
        token = str(getattr(self.server, "token", "") or "")
        if not token:
            self._json(400, {"ok": False, "error": "本实例没有配置令牌，无需换取会话"})
            return
        offered = payload.get("token")
        if not isinstance(offered, str) or not hmac.compare_digest(offered, token):
            _record_auth_failure(self._client_address())
            self._json(401, {"ok": False, "error": "令牌不正确"})
            return
        _clear_auth_failures(self._client_address())
        # 不让明文令牌出现在响应正文里：浏览器只需要 Set-Cookie。
        self._issue_session_cookie = True
        self._json(200, {"ok": True})
        return

    # POST /api/lab/replies/ack / /api/lab/events
    def _route_post_api_lab_replies_ack(self, path: str) -> None:  # noqa: N802 - 路由处理
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._reject_oversized_body(length)
            return
        if length < 0:
            self._json(400, {"ok": False, "error": "bad body"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"ok": False, "error": "bad json"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"ok": False, "error": "bad json"})
            return
        if path == "/api/lab/replies/ack":
            msg_id = str(payload.get("msg_id") or "").strip()
            if not msg_id:
                self._json(400, {"ok": False, "error": "缺少 msg_id"})
                return
            self._lab_reply(lab.ack_reply(self.server.settings, msg_id))  # type: ignore[attr-defined]
        else:
            text = str(payload.get("text") or "").strip()
            if not text:
                self._json(400, {"ok": False, "error": "缺少 text"})
                return
            if len(text) > 2000:
                self._json(400, {"ok": False, "error": "text 过长"})
                return
            self._lab_reply(lab.extract_events(self.server.settings, text))  # type: ignore[attr-defined]
        return

    # POST /api/lab/rules
    def _route_post_api_lab_rules(self, path: str) -> None:  # noqa: N802 - 路由处理
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._reject_oversized_body(length)
            return
        if length < 0:
            self._json(400, {"ok": False, "error": "bad body"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"ok": False, "error": "bad json"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"ok": False, "error": "bad json"})
            return
        self._lab_reply(lab.save_rules(self.server.settings, payload.get("rules")))  # type: ignore[attr-defined]
        return

    # POST /api/lab/rules/test
    def _route_post_api_lab_rules_test(self, path: str) -> None:  # noqa: N802 - 路由处理
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._reject_oversized_body(length)
            return
        if length < 0:
            self._json(400, {"ok": False, "error": "bad body"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"ok": False, "error": "bad json"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"ok": False, "error": "bad json"})
            return
        self._lab_reply(lab.test_rules(self.server.settings, payload.get("message")))  # type: ignore[attr-defined]
        return

    # POST /api/chaoxing/cookie/validate
    def _route_post_api_chaoxing_cookie_validate(self, path: str) -> None:  # noqa: N802 - 路由处理
        self._json(200, self._validate_chaoxing_cookie())
        return

    # POST /api/chaoxing/auto_cookie
    def _route_post_api_chaoxing_auto_cookie(self, path: str) -> None:  # noqa: N802 - 路由处理
        if browser_cookie is None:
            self._json(200, {
                "ok": False,
                "source": "",
                "category": "browser_unavailable",
                "error": "自动读取浏览器登录信息的功能不可用",
                "detail": "浏览器登录信息读取模块未加载",
                "next_step": "请在托盘菜单里重新读取一次学习通登录信息，或在浏览器里登录学习通后重新打开设置页。",
                "attempts": [],
            })
            return
        try:
            result = browser_cookie.read_chaoxing_cookie()
        except Exception as error:  # noqa: BLE001
            result = {
                "ok": False,
                "cookie": "",
                "source": "",
                "category": "decrypt_failed",
                "error": "浏览器登录信息读取模块运行失败",
                "detail": type(error).__name__,
                "next_step": "请改用另一个浏览器重试，或右键托盘图标重新读取一次学习通登录信息。",
                "attempts": [],
            }
        if not isinstance(result, dict):
            result = {
                "ok": False,
                "cookie": "",
                "source": "",
                "category": "browser_unavailable",
                "error": "浏览器登录信息读取结果无效",
                "detail": "返回值不是对象",
                "next_step": "请右键托盘图标重新读取一次学习通登录信息。",
                "attempts": [],
            }
        attempts = result.get("attempts") if isinstance(result.get("attempts"), list) else []
        if not result.get("ok") or not str(result.get("cookie") or "").strip():
            category = str(result.get("category") or "browser_unavailable")
            if category not in {"browser_unavailable", "browser_locked", "no_chaoxing_cookie", "decrypt_failed"}:
                category = "browser_unavailable"
            self._json(200, {
                "ok": False,
                "source": str(result.get("source") or ""),
                "category": category,
                "error": str(result.get("error") or "没有读取到学习通登录信息"),
                "detail": str(result.get("detail") or ""),
                "next_step": str(result.get("next_step") or "请按页面三步引导完成后重试。"),
                "attempts": attempts,
            })
            return
        cookie = str(result.get("cookie") or "").strip()
        source = str(result.get("source") or "")
        try:
            courses = ChaoxingClient(cookie, timeout=self.server.settings.http_timeout, logger=LOGGER).my_courses()  # type: ignore[attr-defined]
        except Exception as error:  # noqa: BLE001
            self._json(200, {
                "ok": False,
                "source": source,
                "category": "no_chaoxing_cookie",
                "error": "浏览器中的学习通登录态已失效",
                "detail": type(error).__name__,
                "next_step": "请重新登录 chaoxing.com，完全退出浏览器后再重试。",
                "attempts": attempts,
            })
            return
        settings = self.server.settings  # type: ignore[attr-defined]
        cookie_path = Path(settings.data_dir) / "chaoxing.cookie"
        try:
            cookie_path.parent.mkdir(parents=True, exist_ok=True)
            cookie_path.write_text(cookie, encoding="utf-8")
            settings.chaoxing_cookie = cookie
            settings.chaoxing_enabled = True
            reload_reader = getattr(self.server, "reload_chaoxing", None)
            if callable(reload_reader): reload_reader()
        except OSError as error:
            self._json(200, {
                "ok": False,
                "source": source,
                "category": "browser_unavailable",
                "error": "登录信息保存失败",
                "detail": type(error).__name__,
                "next_step": "看下群务台的数据目录能不能写，再重试一次。",
                "attempts": attempts,
            })
            return
        self._json(200, {
            "ok": True,
            "saved": True,
            "source": source,
            "courses": len(courses),
            "attempts": attempts,
        })
        return

    # POST /api/dingtalk/scan / /api/wechat/scan
    def _route_post_api_dingtalk_scan(self, path: str) -> None:  # noqa: N802 - 路由处理
        source = "dingtalk" if path.startswith("/api/dingtalk/") else "wechat"
        if source == "wechat":
            # 用户主动点「立即扫描一次」：先让存储位置探测缓存失效，改完位置马上生效。
            wechat_clear_storage_cache()
        start_scan = getattr(self.server, "start_scan", None)
        if not callable(start_scan):
            self._json(200, {"ok": False, "error": "扫描接口暂不可用"})
            return
        settings = self.server.settings  # type: ignore[attr-defined]
        try:
            start_result = start_scan(source)
            if start_result is None:
                self._json(200, {"ok": False, "error": "扫描接口暂不可用"})
                return
            started = bool(start_result)
        except Exception as error:  # noqa: BLE001
            # 两个来源走同一套：脱敏后记日志 + 回可诊断的分类，不再只说一句「扫描启动失败」。
            detail = _redact_secrets(str(error) or error.__class__.__name__)[:240]
            label = "钉钉" if source == "dingtalk" else "微信"
            LOGGER.error("%s扫描启动失败（%s）：%s", label, type(error).__name__, detail)
            self._json(200, {"ok": False, "error": f"{label}扫描启动失败", "category": "scan_start_failed", "detail": detail})
            return
        scan = dict(getattr(settings, f"{source}_status", {}) or {})
        scan_state = str(scan.get("scan_state") or ("running" if started else "idle"))
        elapsed = max(0.0, round(time.monotonic() - float(scan.get("scan_started_monotonic") or 0.0), 1)) if scan_state == "running" else 0.0
        if not started:
            # 已有扫描持锁时 start_scan() 返回 False（wechat_receiver.start_scan 的既有语义），
            # 这不是故障：告诉前端「正在跑，等这一轮结束」，别报「扫描器未运行」。
            if scan_state == "running":
                self._json(200, {
                    "ok": True,
                    "started": False,
                    "already_running": True,
                    "source": source,
                    "scan_state": scan_state,
                    "scan_stage": str(scan.get("scan_stage") or ""),
                    "scan_elapsed_seconds": elapsed,
                    "message": "扫描正在跑，这一轮跑完会自动刷新。",
                })
                return
            if source == "dingtalk":
                if not settings.dingtalk_enabled:
                    category, error_text = "disabled", "请先启用钉钉接入"
                elif not settings.dingtalk_group_whitelist:
                    category, error_text = "no_groups_selected", "请先选择并保存至少一个钉钉群"
                else:
                    category, error_text = "scanner_unavailable", "钉钉扫描器未运行，请重新启用钉钉接入"
            else:
                if not settings.wechat_enabled:
                    category, error_text = "disabled", "请先启用微信接入"
                elif not settings.wechat_group_whitelist:
                    category, error_text = "no_groups_selected", "请先选择并保存至少一个微信群"
                else:
                    category, error_text = "scanner_unavailable", "微信扫描器未运行，请重新启用微信接入"
            # 未启用 / 没选群属于「还没配好」，不是故障：ok=True，error 只当提示文案；
            # 扫描器真没起来才是失败。
            self._json(200, {
                "ok": category != "scanner_unavailable",
                "started": False,
                "source": source,
                "scan_state": scan_state,
                "scan_stage": str(scan.get("scan_stage") or ""),
                "scan_elapsed_seconds": elapsed,
                "category": category,
                "error": error_text,
            })
            return
        self._json(200, {"ok": True, "started": started, "source": source, "scan_state": scan_state, "scan_stage": str(scan.get("scan_stage") or ""), "scan_elapsed_seconds": elapsed})
        return

    # POST /api/pair/approve
    def _route_post_api_pair_approve(self, path: str) -> None:  # noqa: N802 - 路由处理
        self._pair_decision(True)
        return

    # POST /api/pair/deny
    def _route_post_api_pair_deny(self, path: str) -> None:  # noqa: N802 - 路由处理
        self._pair_decision(False)
        return

    # POST /api/onboarding/run
    def _route_post_api_onboarding_run(self, path: str) -> None:  # noqa: N802 - 路由处理
        settings = self.server.settings  # type: ignore[attr-defined]
        if _host_actions_disabled():
            self._json(403, {"ok": False, "error": "当前是测试/只读模式，改本机网络设置的操作已禁用。"})
            return
        code, payload = self._onboarding_run_payload()
        _host_action_audit(settings, "onboarding.%s" % str(payload.get("action") or ""), settings.web_port, self.client_address[0] if getattr(self, "client_address", None) else "", str(payload.get("state") or payload.get("error") or ""))
        self._json(code, payload)
        return

    # POST /api/tunnel/start / /api/pair/public
    def _route_post_api_tunnel_start(self, path: str) -> None:  # noqa: N802 - 路由处理
        settings = self.server.settings  # type: ignore[attr-defined]
        # SEC: Funnel 把整站反代到公网，请求经本机 tailscaled 转发后来源地址是 127.0.0.1，
        # 正好命中 _authorized 里「空 token 只放行环回」的分支 —— 等于把整站无鉴权公开。
        # 所以没有令牌时不许开公网，先让用户设一个。
        if not str(getattr(self.server, "token", "") or ""):
            self._json(400, {"ok": False, "error": "请先设置访问令牌再开到公网：打开安装目录的 .env，把访问令牌那一行填好，重启后再来点这里。"})
            return
        if _host_actions_disabled():
            self._json(403, {"ok": False, "error": "当前是测试/只读模式，改本机网络设置的操作已禁用。"})
            return
        # 会真的装软件、开公网：新接口必须显式确认（旧 /api/pair/public 保留旧行为兼容老页面，
        # 但它内部走的是同一套执行路径，所以审计一样记）。
        if path == "/api/tunnel/start" and not self._confirmed_host_action():
            self._json(400, {"ok": False, "needs_confirm": True, "error": "这是会改本机网络设置的写操作，请带 confirm:true 再调用。"})
            return
        # 开通要下载/安装 Tailscale、等用户登录，几十秒到几分钟，绝不能挂在这次请求里
        # （旧实现同步跑，页面会卡到超时）。这里只把后台任务起起来，页面轮询 /api/tunnel/status。
        started = _funnel_setup(settings.web_port).start()
        _host_action_audit(settings, "tunnel.start", settings.web_port, str(self.client_address[0]), str(started.get("state") or ""))
        started["ok"] = True
        self._json(200, started)
        return

    # POST /api/firewall/allow
    def _route_post_api_firewall_allow(self, path: str) -> None:  # noqa: N802 - 路由处理
        settings = self.server.settings  # type: ignore[attr-defined]
        if _host_actions_disabled():
            self._json(403, {"ok": False, "error": "当前是测试/只读模式，改本机网络设置的操作已禁用。"})
            return
        if not self._confirmed_host_action():
            self._json(400, {"ok": False, "needs_confirm": True, "error": "这是会弹管理员权限、改防火墙的写操作，请带 confirm:true 再调用。"})
            return
        # 不在服务进程里弹 UAC（会挂住请求线程、用户也不知道该点哪个框）：
        # 写请求文件交给托盘执行，页面轮询 /api/firewall/status 看结果。
        request_id = _write_firewall_request(settings, settings.web_port)
        _host_action_audit(
            settings,
            "firewall.allow",
            settings.web_port,
            str(self.client_address[0]),
            "delegated",
        )
        self._json(200, {"ok": True, "state": "pending", "request_id": request_id, "port": int(settings.web_port)})
        return

    # POST /api/tasks/urgent
    def _route_post_api_tasks_urgent(self, path: str) -> None:  # noqa: N802 - 路由处理
        from noticehub.application.tasks import set_task_urgent

        try:
            payload = self._json_body()
            if "urgent" not in payload:
                raise ValueError("urgent 必须是 true、false 或 null")
            result = set_task_urgent(self.store, payload.get("task_id"), payload.get("urgent"))
        except ValueError as error:
            self._json(400, {"ok": False, "error": str(error)})
            return
        except Exception as error:  # noqa: BLE001
            LOGGER.exception("保存任务紧急覆盖失败")
            self._json(500, {"ok": False, "error": str(error) or "紧急设置保存失败"})
            return
        # 用例返回 ok:false。HTTP 仍按现有测试把「任务不存在」映射成 404。
        self._json(200 if result.get("ok") else 404, result)
        return

    # POST /api/tasks/pin
    def _route_post_api_tasks_pin(self, path: str) -> None:  # noqa: N802 - 路由处理
        from noticehub.application.tasks import set_task_pinned

        try:
            payload = self._json_body()
            if "pinned" not in payload:
                raise ValueError("pinned 必须是 true 或 false")
            result = set_task_pinned(self.store, payload.get("task_id"), payload.get("pinned"))
        except ValueError as error:
            self._json(400, {"ok": False, "error": str(error)})
            return
        except Exception as error:  # noqa: BLE001
            LOGGER.exception("保存任务置顶失败")
            self._json(500, {"ok": False, "error": str(error) or "置顶设置保存失败"})
            return
        self._json(200 if result.get("ok") else 404, result)
        return

    # POST /api/tasks/create
    def _route_post_api_tasks_create(self, path: str) -> None:  # noqa: N802 - 路由处理
        from noticehub.application.tasks import create_task

        try:
            payload = self._json_body()
            result = create_task(self.store, payload.get("summary"), payload.get("deadline") or "")
        except ValueError as error:
            self._json(400, {"ok": False, "error": str(error)})
            return
        except Exception as error:  # noqa: BLE001
            LOGGER.exception("手动新建待办失败")
            self._json(500, {"ok": False, "error": str(error) or "新建待办失败"})
            return
        if not result.get("task_id"):
            self._json(500, {"ok": False, "error": "新建待办失败"})
            return
        self._json(200, result)
        return

    # POST /api/tasks/clear-finished
    def _route_post_api_tasks_clear_finished(self, path: str) -> None:  # noqa: N802 - 路由处理
        # 只清已完成/已忽略/已过期；open/candidate 一条不动（不是 clear_tasks 那种清库）。
        try:
            removed = self.store.clear_finished_tasks()
        except Exception as error:  # noqa: BLE001
            LOGGER.exception("清空已完成任务失败")
            self._json(200, {"ok": False, "error": str(error) or "清空失败"})
            return
        self._json(200, {"ok": True, "removed": int(removed)})
        return


    # POST /api/inbox/classify/cancel
    def _route_post_api_inbox_classify_cancel(self, path: str) -> None:  # noqa: N802 - 路由处理
        with _JOB_LOCK:
            cancel_event = _JOB_STATE["cancel"]
            active = _JOB_STATE["active"]
            running = _JOB_STATE["classify"]["running"]
            cancelled = bool(active == "classify" and running and isinstance(cancel_event, threading.Event))
            if cancelled:
                cancel_event.set()
        # 没在跑也算「请求已处理」：前端取消一个刚结束的任务不该看到 500/409。
        self._json(200, {"ok": True, "cancelled": cancelled}); return

    # POST /api/history/fetch/cancel
    def _route_post_api_history_fetch_cancel(self, path: str) -> None:  # noqa: N802 - 路由处理
        from noticehub.application.jobs import cancel_job

        result = cancel_job(_JOB_STATE, _JOB_LOCK)
        if not result["cancelled"]:
            self._json(409, {"ok": False, "error": result["error"]}); return
        self._json(200, {"ok": True, "cancelled": True}); return

    # POST /api/history/fetch
    def _route_post_api_history_fetch(self, path: str) -> None:  # noqa: N802 - 路由处理
        from noticehub.application.jobs import start_history_backfill

        try:
            payload = self._json_body()
            _parse_groups(payload.get("groups"))
            since = _parse_day(payload.get("since"))
            until = _parse_day(payload.get("until"), end=True)
            if since and until and since > until:
                raise ValueError("开始日期不能晚于结束日期")
        except ValueError as error:
            self._json(400, {"ok": False, "error": str(error)}); return
        if catchup is None:
            self._json(503, {"ok": False, "error": "回溯模块暂不可用"}); return
        try:
            settings = self.server.settings  # type: ignore[attr-defined]
            result = start_history_backfill(
                payload.get("groups"),
                payload.get("since"),
                payload.get("until"),
                start=_start_job,
                run=lambda **kwargs: catchup.backfill_range(settings, self.store, **kwargs),  # type: ignore[attr-defined]
            )
        except ValueError as error:
            self._json(400, {"ok": False, "error": str(error)}); return
        if not result["started"]:
            active = result["active"]
            self._json(409, {"ok": False, "error": "已有回溯任务在进行" if active == "history" else "已有通知提取任务在进行"}); return
        self._json(200, {"ok": True, "started": True}); return

    # POST /api/inbox/classify
    def _route_post_api_inbox_classify(self, path: str) -> None:  # noqa: N802 - 路由处理
        from noticehub.application.jobs import start_inbox_classify

        try:
            payload = self._json_body()
            _parse_groups(payload.get("groups"))
            since = _parse_day(payload.get("since"))
            until = _parse_day(payload.get("until"), end=True)
            limit = payload.get("limit", 1000)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20000:
                raise ValueError("提取条数必须在 1 到 20000 之间")
            if since and until and since > until:
                raise ValueError("开始日期不能晚于结束日期")
        except ValueError as error:
            self._json(400, {"ok": False, "error": str(error)}); return
        if inbox is None:
            self._json(503, {"ok": False, "error": "通知判定模块暂不可用"}); return
        try:
            settings = self.server.settings  # type: ignore[attr-defined]
            result = start_inbox_classify(
                payload.get("groups"),
                payload.get("since"),
                payload.get("until"),
                payload.get("limit", 1000),
                start=_start_job,
                run=lambda **kwargs: inbox.classify_messages(settings, self.store, **kwargs),  # type: ignore[attr-defined]
            )
        except ValueError as error:
            self._json(400, {"ok": False, "error": str(error)}); return
        if not result["started"]:
            active = result["active"]
            self._json(409, {"ok": False, "error": "已有通知提取任务在进行" if active == "classify" else "已有回溯任务在进行"}); return
        self._json(200, {"ok": True, "started": True}); return

    # POST /api/inbox/promote
    def _route_post_api_inbox_promote(self, path: str) -> None:  # noqa: N802 - 路由处理
        from noticehub.application.inbox_item import promote_inbox_item

        try:
            payload = self._json_body()
            msg_id = str(payload.get("msg_id") or "").strip()
            title = payload.get("title")
            due = payload.get("due")
            if not msg_id or len(msg_id) > 256:
                raise ValueError("消息编号无效")
            if title is not None and (not isinstance(title, str) or len(title) > 500):
                raise ValueError("待办标题最多 500 个字符")
            if due is not None and due != "" and not isinstance(due, str):
                raise ValueError("截止时间格式无效")
            if due and parse_iso(due) is None:
                raise ValueError("截止时间格式无效")
        except ValueError as error:
            self._json(400, {"ok": False, "error": str(error)}); return
        if inbox is None:
            self._json(503, {"ok": False, "task_id": None, "error": "通知判定模块暂不可用"}); return
        try:
            settings = self.server.settings  # type: ignore[attr-defined]
            result = promote_inbox_item(
                payload.get("msg_id"),
                payload.get("title"),
                payload.get("due"),
                promote=lambda msg_id, title, due: inbox.promote(settings, self.store, msg_id, title=title, due=due),  # type: ignore[attr-defined]
            )
        except ValueError as error:
            self._json(400, {"ok": False, "error": str(error)}); return
        except Exception as error:  # noqa: BLE001
            LOGGER.exception("通知转待办失败")
            self._json(200, {"ok": False, "task_id": None, "error": str(error) or "转换失败"}); return
        self._json(200 if result.get("ok") else 400, result)
        return

    # POST /api/inbox/delete
    def _route_post_api_inbox_delete(self, path: str) -> None:  # noqa: N802 - 路由处理
        try:
            payload = self._json_body()
            raw_ids = payload.get("msg_ids")
            if raw_ids is None:
                raw_ids = [payload.get("msg_id")] if "msg_id" in payload else []
            if not isinstance(raw_ids, list):
                raise ValueError("msg_ids 必须是消息编号数组")
            if not raw_ids:
                raise ValueError("msg_id 无效：至少要给一个消息编号")
            if len(raw_ids) > 200:
                raise ValueError("一次最多删 200 条")
            msg_ids: list[str] = []
            for item in raw_ids:
                if not isinstance(item, str) or not item.strip():
                    raise ValueError("msg_id 无效：消息编号必须是非空字符串")
                if len(item) > 200:
                    raise ValueError("msg_id 无效：单个消息编号不能超过 200 个字符")
                msg_ids.append(item.strip())
        except ValueError as error:
            self._json(400, {"ok": False, "error": str(error)}); return
        try:
            deleted = self.store.delete_inbox_items(msg_ids)  # type: ignore[attr-defined]
        except Exception as error:  # noqa: BLE001
            LOGGER.exception("删除通知失败")
            self._json(200, {"ok": False, "error": str(error) or "删除失败"}); return
        # 原图跟着条目一起删，别在 files/ 里留孤儿。
        delete_images(self.server.settings.data_dir, msg_ids)  # type: ignore[attr-defined]
        self._json(200, {"ok": True, "deleted": int(deleted)})
        return

    # POST /api/hosting/start / /api/hosting/stop
    def _route_post_api_hosting_start(self, path: str) -> None:  # noqa: N802 - 路由处理
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._reject_oversized_body(length); return
        if length < 0:
            self._json(400, {"ok": False, "error": "bad body"}); return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, TypeError):
            self._json(400, {"ok": False, "error": "bad json"}); return
        if hosting is None:
            self._json(200, {"ok": False, "error": "托管模块不可用"}); return
        try:
            result = (hosting.start(self.server.settings, uin=str(payload.get("uin") or "").strip() or None)
                      if path.endswith("/start") else hosting.stop(self.server.settings))  # type: ignore[attr-defined]
            self._json(200, result)
        except Exception as error:  # noqa: BLE001
            self._json(200, {"ok": False, "error": str(error)})
        return

    # POST /api/subscriptions
    def _route_post_api_subscriptions(self, path: str) -> None:  # noqa: N802 - 路由处理
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._reject_oversized_body(length); return
        if length <= 0:
            self._json(400, {"ok": False, "error": "bad body"}); return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            groups = [str(x).strip() for x in payload.get("groups", []) if str(x).strip()]
            aliases = {str(k): str(v) for k, v in (payload.get("aliases") or {}).items() if str(k).strip()}
        except (ValueError, TypeError, AttributeError):
            self._json(400, {"ok": False, "error": "bad json"}); return
        settings = self.server.settings  # type: ignore[attr-defined]
        from noticehub.application.subscriptions import save_subscriptions

        # 缺键由 update_env_file 追加。全新安装的 .env 可能没有这两行，不能因此失败。
        result = save_subscriptions(settings, groups, aliases, write=update_env_file)
        if not result.get("ok"):
            self._json(400, {"ok": False, "error": result.get("error") or ".env 写入失败"}); return
        self._json(200, {"ok": True, "groups": result["groups"], "applied": True}); return

    # POST /api/update/download
    def _route_post_api_update_download(self, path: str) -> None:  # noqa: N802 - 路由处理
        self._json(200, _start_update_download(self.server))
        return

    # POST /api/update/apply
    def _route_post_api_update_apply(self, path: str) -> None:  # noqa: N802 - 路由处理
        self._json(200, _apply_update(self.server))
        return

    # POST /api/napcat/install
    def _route_post_api_napcat_install(self, path: str) -> None:  # noqa: N802 - 路由处理
        if napcat_admin is None:
            self._json(200, {"ok": False, "installed": False, "already_installed": False, "root": None, "version": None, "bytes": 0, "error": "一键接入模块不可用"}); return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = -1
        accept_sha = ""
        if length > MAX_BODY_BYTES:
            self._reject_oversized_body(length); return
        if length > 0:
            try:
                payload = self._json_body()
            except ValueError as error:
                self._json(400, {"ok": False, "error": str(error)}); return
            if isinstance(payload, dict):
                accept_sha = str(payload.get("accept_sha256") or "")
        # 安装可能要几分钟：POST 仍然同步返回最终结果（前端改异步前保持兼容），
        # 但期间把进度写进 job 快照，前端可以并行轮询 /api/napcat/install/status 显示百分比。
        with _JOB_LOCK:
            _JOB_STATE["napcat_install"].update(
                running=True, stage="download", done=0, total=0, note="正在准备安装", result=None, detail={},
            )
        result: dict[str, Any] = {"ok": False, "installed": False, "already_installed": False, "root": None, "version": None, "bytes": 0, "error": "安装未完成"}
        try:
            result = napcat_admin.install(  # type: ignore[attr-defined]
                self.server.settings,
                accept_sha256=accept_sha,
                on_progress=lambda fields: _job_progress_detail("napcat_install", **fields),
            )
        except Exception as error:  # noqa: BLE001
            result = {"ok": False, "installed": False, "already_installed": False, "root": None, "version": None, "bytes": 0, "error": str(error)}
        finally:
            with _JOB_LOCK:
                state = _JOB_STATE["napcat_install"]
                stage = "complete" if result.get("ok") else "error"
                state.update(running=False, stage=stage, result=result, note=_install_note(stage, state.get("detail") or {}))
                if not result.get("ok") and result.get("error"):
                    state["note"] = str(result.get("error"))[:200]
        _clear_napcat_live_cache()
        self._json(200, result)
        return

    # GET /api/napcat/install/status
    def _route_get_api_napcat_install_status(self, path: str) -> None:  # noqa: N802 - 路由处理
        snapshot = _job_snapshot("napcat_install")
        detail = snapshot.pop("detail", {}) or {}
        # 缺的字段给 null，不编数字：前端据此显示「已下载 12.3 MB」或只显示转圈。
        self._json(200, {
            "ok": True,
            **snapshot,
            "downloaded_bytes": detail.get("downloaded_bytes"),
            "total_bytes": detail.get("total_bytes"),
            "extracted": detail.get("extracted"),
            "entries": detail.get("entries"),
        })
        return

    # POST /api/napcat/launch
    def _route_post_api_napcat_launch(self, path: str) -> None:  # noqa: N802 - 路由处理
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._reject_oversized_body(length); return
        if length <= 0:
            self._json(400, {"ok": False, "error": "bad body"}); return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            uin = str(payload.get("uin") or "").strip() or None
        except (ValueError, TypeError, AttributeError):
            self._json(400, {"ok": False, "error": "bad json"}); return
        if napcat_admin is None:
            self._json(200, {"ok": False, "already_running": False, "pid": None, "command": [], "profile_dir": "", "boot": None, "error": "一键接入模块不可用"}); return
        try:
            result = napcat_admin.launch(self.server.settings, uin=uin)  # type: ignore[attr-defined]
        except Exception as error:  # noqa: BLE001
            LOGGER.warning("启动 NapCat 失败：%s", error)
            result = {"ok": False, "already_running": False, "pid": None, "command": [], "profile_dir": "", "boot": None, "error": str(error)}
        _clear_napcat_live_cache()
        self._json(200, result)
        return

    # POST /api/napcat/connect
    def _route_post_api_napcat_connect(self, path: str) -> None:  # noqa: N802 - 路由处理
        """「帮我连上它」：读 NapCat 自己的 OneBot 配置 → 探测 → 写回我们自己的设置。

        只读对方的配置，绝不改；写回的是 ``QQ_DIGEST_NAPCAT_API_URL``，下次启动直接用。
        整段探测 3 秒内结束，接口不会把页面挂住。
        """
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._reject_oversized_body(length); return
        if length > 0:
            try:
                self.rfile.read(length)
            except OSError:  # noqa: BLE001 - 请求体只是占位，读不动也不影响
                pass
        settings = self.server.settings  # type: ignore[attr-defined]
        if napcat_admin is None:
            self._json(200, {"ok": False, "api_url": "", "tried": [], "error": "一键接入模块不可用"}); return
        try:
            found = napcat_admin.discover_api_url(settings)
        except Exception as error:  # noqa: BLE001 - 探测失败不能把接口打成 500
            LOGGER.warning("自动连接 OneBot 失败：%s", error)
            found = {"ok": False, "api_url": "", "tried": [], "error": str(error)}
        if not found.get("ok"):
            result = dict(found)
            result["stage"] = "running_no_api"
            result["webui_url"] = napcat_admin.webui_url(settings)
            self._json(200, result); return
        url = str(found["api_url"])
        env_path = Path(settings.env_file or Path.cwd() / ".env")
        written = update_env_file(env_path, {"QQ_DIGEST_NAPCAT_API_URL": url})
        settings.napcat_api_url = url
        # 与 settings_api 保持同一口径：进程内的 os.environ 也同步，免得两边打架。
        os.environ["QQ_DIGEST_NAPCAT_API_URL"] = url
        _clear_napcat_live_cache()
        persist_error = "" if written.get("ok") else str(written.get("error") or ".env 没写成")
        if persist_error:
            # 写得进去才算「下次启动直接用」。写不进去必须让用户知道：重启就回到旧地址。
            LOGGER.warning("已连上 %s，但写不回 .env（%s）：重启后会回到旧地址", url, persist_error)
        self._json(200, {
            "ok": True,
            "api_url": url,
            "tried": found.get("tried") or [],
            "persisted": bool(written.get("ok")),
            "persist_error": persist_error,
            "status": self._napcat_status_payload(),
        })
        return

    # POST /api/napcat/restart
    def _route_post_api_napcat_restart(self, path: str) -> None:  # noqa: N802 - 路由处理
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._reject_oversized_body(length); return
        if length <= 0:
            self._json(400, {"ok": False, "error": "bad body"}); return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            uin = str(payload.get("uin") or "").strip() or None
        except (ValueError, TypeError, AttributeError):
            self._json(400, {"ok": False, "error": "bad json"}); return
        if napcat_admin is None:
            self._json(200, {"ok": False, "stopped": [], "already_stopped": False, "pid": None, "api_up": False, "waiting_for_login": False, "error": "一键接入模块不可用"}); return
        try:
            result = napcat_admin.restart(self.server.settings, uin=uin)  # type: ignore[attr-defined]
        except Exception as error:  # noqa: BLE001
            LOGGER.warning("重启 NapCat 失败：%s", error)
            result = {"ok": False, "stopped": [], "already_stopped": False, "pid": None, "api_up": False, "waiting_for_login": False, "error": str(error)}
        _clear_napcat_live_cache()
        self._json(200, result)
        return

    # POST /api/napcat/autosetup
    def _route_post_api_napcat_autosetup(self, path: str) -> None:  # noqa: N802 - 路由处理
        if napcat_admin is None:
            self._json(200, {"ok": False, "steps": [], "qrcode_path": None, "uin": None, "applied_via": None, "restart_required": False, "stage": "not_installed", "error": "一键接入模块不可用"})
            return
        # 「一键接入」现在可能在里面对 NapCat 下载安装（几分钟）：进度写进 job 快照，
        # 前端并行轮询 /api/napcat/install/status 就能看到已下载 MB 与解压条目数。
        with _JOB_LOCK:
            _JOB_STATE["napcat_install"].update(
                running=True, stage="download", done=0, total=0, note="一键接入：正在检查并安装 NapCat", result=None, detail={},
            )
        # 用户在「确认安装」里确认过指纹时会把 accept_sha256 带回来：透传给 install() 校验，
        # 这样「一键接入」也能走完首次安装，而不是永远停在等确认。
        accept_sha = ""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = -1
        if length > MAX_BODY_BYTES:
            self._reject_oversized_body(length)
            return
        if length > 0:
            try:
                payload = self._json_body()
            except ValueError as error:
                self._json(400, {"ok": False, "error": str(error)})
                return
            if isinstance(payload, dict):
                accept_sha = str(payload.get("accept_sha256") or "").strip().lower()[:64]
        result: dict[str, Any] = {}
        try:
            result = napcat_admin.auto_setup(  # type: ignore[attr-defined]
                self.server.settings,
                on_progress=lambda fields: _job_progress_detail("napcat_install", **fields),
                accept_sha256=accept_sha,
            )
        except Exception as error:  # noqa: BLE001
            LOGGER.warning("一键接入失败：%s", error)
            result = {"ok": False, "steps": [], "qrcode_path": None, "uin": None, "applied_via": None,
                      "restart_required": False, "stage": "launch_failed",
                      "error": "一键接入失败：%s" % (_redact_secrets(str(error) or error.__class__.__name__)[:240])}
        finally:
            with _JOB_LOCK:
                state = _JOB_STATE["napcat_install"]
                stage = "complete" if result.get("ok") else "error"
                state.update(running=False, stage=stage, result=result, note=_install_note(stage, state.get("detail") or {}))
                if not result.get("ok") and result.get("error"):
                    state["note"] = str(result.get("error"))[:200]
        _clear_napcat_live_cache()
        self._json(200, result)
        return

    # POST /api/tasks/<id>（前缀模式：任务详情上的 confirm/dismiss/done/snooze/correct）
    def _route_post_tasks(self, path: str) -> None:  # noqa: N802 - 路由处理
        try:
            task_id = int(path.rsplit("/", 1)[-1])
        except ValueError:
            self._json(400, {"ok": False, "error": "bad task id"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._reject_oversized_body(length)
            return
        if length < 0:
            self._json(400, {"ok": False, "error": "bad body"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"ok": False, "error": "bad json"})
            return
        if not isinstance(payload, dict):
            # 合法 JSON 但不是对象（数组/字符串/null）：下面要 .get()，先挡住，
            # 否则 AttributeError 会直接把连接摔掉、前端拿不到任何响应。
            self._json(400, {"ok": False, "error": "bad json"})
            return
        action = str(payload.get("action") or "").strip().lower()
        if action == "correct":
            from noticehub.application.tasks import correct_task

            try:
                result = correct_task(
                    self.store,
                    task_id,
                    str(payload.get("correction") or ""),
                    payload.get("value", ""),
                )
            except ValueError as error:
                self._json(400, {"ok": False, "error": str(error)})
                return
            self._json(200 if result["ok"] else 404, result)
            return
        from noticehub.application.tasks import apply_task_action

        try:
            present: dict[str, Any] = {}
            if "done" in payload:
                present["done"] = payload.get("done")
            result = apply_task_action(
                self.store,
                task_id,
                action,
                settings=self.server.settings,  # type: ignore[attr-defined]
                now=now_local,
                **present,
            )
        except ValueError as error:
            self._json(400, {"ok": False, "error": str(error)})
            return
        self._json(200 if result["ok"] else 404, result)

    def do_POST(self) -> None:  # noqa: N802 - 基类命名
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if self._respond_if_throttled():
            return
        entry = routes.POST_ROUTES.get(path)
        if entry is not None and entry[1]:  # 免鉴权路由：静态资源、配对、向导
            getattr(self, entry[0])(path)
            return
        if not self._authorized():
            self._json(401, {"ok": False, "error": "invalid token"})
            return
        if entry is not None:
            getattr(self, entry[0])(path)
            return
        # 前缀模式路由（/api/tasks/<id>）：尾部兜底，未命中才是 404。
        pattern = routes.match_pattern("POST", path)
        if pattern is not None:
            getattr(self, pattern[0])(path)
            return
        self._json(404, {"ok": False, "error": "not found"})


class _BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """并发上限：同时处理的连接最多 MAX_ACTIVE_CONNECTIONS 个。

    红队 S21：ThreadingHTTPServer 默认来一个开一个线程，慢客户端能把线程数顶到几千。
    超出的连接直接告警并关闭，不排队（排队等于把线程留给死连接）。
    """

    daemon_threads = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._active_lock = threading.Lock()
        self._active_connections = 0

    def process_request(self, request: Any, client_address: Any) -> None:  # noqa: N802 - 基类命名
        with self._active_lock:
            if self._active_connections >= MAX_ACTIVE_CONNECTIONS:
                LOGGER.warning("并发连接已达上限 %d，拒绝来自 %s 的连接", MAX_ACTIVE_CONNECTIONS, client_address[0] if client_address else "")
                self.shutdown_request(request)
                return
            self._active_connections += 1
        super().process_request(request, client_address)

    def process_request_thread(self, request: Any, client_address: Any) -> None:  # noqa: N802 - 基类命名
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._active_lock:
                self._active_connections = max(0, self._active_connections - 1)


class TaskWebServer:
    """待办台的 HTTP 服务，绑定在独立端口上。"""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        *,
        logger: logging.Logger | None = None,
        meta_provider: Any = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.logger = logger or LOGGER
        self.meta_provider = meta_provider
        self.reload_chaoxing = None
        self.reload_dingtalk = None
        self.reload_wechat = None
        self.start_scan = None
        self.dingtalk_reader_provider = None
        self.wechat_reader_provider = None
        self.start_scan = None
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.qq_group_refresh_lock = threading.Lock()
        self.qq_group_refresh_stop = threading.Event()
        self.qq_group_refresh_thread: threading.Thread | None = None
        try:
            saved_group_status = json.loads(store.meta_get("qq_group_refresh_status", "{}"))
        except (TypeError, ValueError):
            saved_group_status = {}
        self.qq_group_refresh_status = {
            "last_attempt_at": "",
            "last_success_at": "",
            "group_count": 0,
            "last_error": "",
            "is_fetching": False,
            "next_run_at": "",
            "failure_count": 0,
            **(saved_group_status if isinstance(saved_group_status, dict) else {}),
        }
        self.qq_group_refresh_status["is_fetching"] = False
        self.token = settings.web_token or store.meta_get("web_token", "")
        if not self.token and settings.web_host not in {"127.0.0.1", "localhost", "::1"}:
            self.token = secrets.token_urlsafe(12)
            store.meta_set("web_token", self.token)

    @property
    def is_alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def _save_qq_group_status(self) -> None:
        with self.qq_group_refresh_lock:
            snapshot = dict(self.qq_group_refresh_status)
        try:
            self.store.meta_set("qq_group_refresh_status", json.dumps(snapshot, ensure_ascii=False))
        except Exception:
            self.logger.warning("QQ 群列表状态持久化失败", exc_info=True)

    def qq_group_status(self) -> dict[str, Any]:
        with self.qq_group_refresh_lock:
            state = dict(self.qq_group_refresh_status)
        return {
            "ok": True,
            "last_attempt_at": str(state.get("last_attempt_at") or ""),
            "last_success_at": str(state.get("last_success_at") or ""),
            "group_count": max(0, int(state.get("group_count") or 0)),
            "last_error": str(state.get("last_error") or ""),
            "is_fetching": bool(state.get("is_fetching")),
            "next_run_at": str(state.get("next_run_at") or ""),
        }

    def refresh_qq_groups_once(self) -> bool:
        attempted_at = dt.datetime.now().astimezone()
        with self.qq_group_refresh_lock:
            if self.qq_group_refresh_status.get("is_fetching"):
                return False
            self.qq_group_refresh_status.update({
                "last_attempt_at": attempted_at.isoformat(timespec="seconds"),
                "is_fetching": True,
                "next_run_at": (attempted_at + dt.timedelta(seconds=int(self.settings.qq_group_refresh_interval_seconds))).isoformat(timespec="seconds"),
            })
        self._save_qq_group_status()
        succeeded = False
        groups: list[dict[str, Any]] = []
        error_text = ""
        try:
            rows = _request_napcat_action(self.settings, "get_group_list")
            if not isinstance(rows, list):
                raise ValueError("NapCat 群列表响应不是数组")
            groups = [dict(row) for row in rows if isinstance(row, dict) and str(row.get("group_id") or "").strip()]
            names = {
                str(row["group_id"]).strip(): str(row.get("group_name") or "").strip()
                for row in groups
                if str(row.get("group_name") or "").strip()
            }
            self.store.remember_group_names(names)
            self.store.meta_set("qq_group_list", json.dumps(groups, ensure_ascii=False))
            succeeded = True
        except Exception as error:  # noqa: BLE001
            error_text = str(error) or error.__class__.__name__

        completed_at = dt.datetime.now().astimezone()
        with self.qq_group_refresh_lock:
            state = self.qq_group_refresh_status
            state["is_fetching"] = False
            if succeeded:
                state.update({
                    "last_success_at": completed_at.isoformat(timespec="seconds"),
                    "group_count": len(groups),
                    "last_error": "",
                    "failure_count": 0,
                    "next_run_at": (completed_at + dt.timedelta(seconds=int(self.settings.qq_group_refresh_interval_seconds))).isoformat(timespec="seconds"),
                })
            else:
                failures = int(state.get("failure_count") or 0) + 1
                delay = QQ_GROUP_RETRY_DELAYS[min(failures - 1, len(QQ_GROUP_RETRY_DELAYS) - 1)]
                state.update({
                    "last_error": error_text,
                    "failure_count": failures,
                    "next_run_at": (completed_at + dt.timedelta(seconds=delay)).isoformat(timespec="seconds"),
                })
        self._save_qq_group_status()
        return succeeded

    def _qq_group_refresh_loop(self) -> None:
        if self.qq_group_refresh_stop.wait(1.0):
            return
        while not self.qq_group_refresh_stop.is_set():
            self.refresh_qq_groups_once()
            with self.qq_group_refresh_lock:
                next_run = str(self.qq_group_refresh_status.get("next_run_at") or "")
            try:
                delay = max(0.0, dt.datetime.fromisoformat(next_run).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                delay = max(30, int(self.settings.qq_group_refresh_interval_seconds))
            if self.qq_group_refresh_stop.wait(delay):
                break

    def start(self) -> bool:
        if self.is_alive:
            return True
        try:
            server = _BoundedThreadingHTTPServer((self.settings.web_host, self.settings.web_port), _Handler)
        except OSError as error:
            self.logger.error("待办台启动失败（%s:%d）：%s", self.settings.web_host, self.settings.web_port, error)
            return False
        server.store = self.store  # type: ignore[attr-defined]
        server.settings = self.settings  # type: ignore[attr-defined]
        server.catchup_preferences = {"catchup_enabled": bool(self.settings.catchup_enabled), "catchup_hours": int(self.settings.catchup_hours)}  # type: ignore[attr-defined]
        server.catchup_preferences_lock = threading.Lock()  # type: ignore[attr-defined]
        server.state_lock = threading.Lock()  # type: ignore[attr-defined]
        server.announce_lock = threading.Lock()  # type: ignore[attr-defined]
        server.announce_running = False  # type: ignore[attr-defined]
        server.update_lock = threading.Lock()  # type: ignore[attr-defined]
        server.update_running = False  # type: ignore[attr-defined]
        server.update_download = {"state": "idle", "done": 0, "total": 0, "path": "", "error": "", "tag": ""}  # type: ignore[attr-defined]
        server.token = self.token  # type: ignore[attr-defined]
        server.meta_provider = self.meta_provider  # type: ignore[attr-defined]
        server.reload_chaoxing = lambda: self.reload_chaoxing() if callable(self.reload_chaoxing) else None  # type: ignore[attr-defined]
        server.reload_dingtalk = lambda: self.reload_dingtalk() if callable(self.reload_dingtalk) else None  # type: ignore[attr-defined]
        server.reload_wechat = lambda: self.reload_wechat() if callable(self.reload_wechat) else None  # type: ignore[attr-defined]
        server.dingtalk_reader_provider = lambda: self.dingtalk_reader_provider() if callable(self.dingtalk_reader_provider) else None  # type: ignore[attr-defined]
        server.wechat_reader_provider = lambda: self.wechat_reader_provider() if callable(self.wechat_reader_provider) else None  # type: ignore[attr-defined]
        server.start_scan = lambda source: self.start_scan(source) if callable(self.start_scan) else None  # type: ignore[attr-defined]
        server.qq_group_refresh_owner = self  # type: ignore[attr-defined]
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever, name="tasks-web", daemon=True)
        self.thread.start()
        self.qq_group_refresh_stop.clear()
        self.qq_group_refresh_thread = threading.Thread(
            target=self._qq_group_refresh_loop, name="qq-group-refresh", daemon=True
        )
        self.qq_group_refresh_thread.start()
        self.logger.info(
            "待办台已启动：http://%s:%d/ （%s）",
            self.settings.web_host,
            self.settings.web_port,
            "需要 token" if self.token else "未设 token",
        )
        return True

    def stop(self) -> None:
        self.qq_group_refresh_stop.set()
        if self.qq_group_refresh_thread is not None and self.qq_group_refresh_thread.is_alive():
            self.qq_group_refresh_thread.join(timeout=max(1, int(self.settings.http_timeout) + 1))
        self.qq_group_refresh_thread = None
        if self.server is not None:
            try:
                self.server.shutdown()
                if self.thread is not None:
                    self.thread.join(timeout=5)
                self.server.server_close()
            except Exception:  # noqa: BLE001 - 关闭失败不应阻塞退出
                self.logger.exception("待办台关闭失败")
        self.server = None
        self.thread = None

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.settings.web_enabled),
            "alive": self.is_alive,
            "host": self.settings.web_host,
            "port": self.settings.web_port,
            "auth": bool(self.token),
        }
