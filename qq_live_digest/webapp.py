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
import stat
import secrets
import shutil
import sqlite3
import tempfile
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping

from . import announce
from . import qr as qr_encoder
from .chaoxing_receiver import ChaoxingAuthError, ChaoxingClient, ChaoxingError, chaoxing_status
from .dingtalk_receiver import DingTalkLocalReader
from .wechat_receiver import group_names as wechat_group_names
from .wechat_receiver import locate_storage as wechat_locate_storage
from .wechat_receiver import storage_unavailable_hint as wechat_storage_hint
from . import pairing
from . import selfcheck
from .config import DEFAULT_VISION_MODEL, RUNTIME_ROOT, Settings, split_list, update_env_file
from .ics import render_calendar
from .store import Store, effective_urgent
from .timeutil import iso, now_local, parse_iso
from .win_text import decode_console_output

QQ_GROUP_RETRY_DELAYS = (60, 120, 300)
DINGTALK_GROUP_LLM_TIMEOUT_SECONDS = 5

try:
    from . import catchup
except Exception:  # noqa: BLE001
    catchup = None  # type: ignore[assignment]

try:
    from . import inbox
except Exception:  # noqa: BLE001
    inbox = None  # type: ignore[assignment]

try:
    from . import group_suggest
except Exception:  # noqa: BLE001
    group_suggest = None  # type: ignore[assignment]

try:
    from . import hosting
except Exception:  # noqa: BLE001
    hosting = None  # type: ignore[assignment]

try:  # 「一键接入」实现；缺失时向导降级为手工接线而不是整站报错
    from . import napcat_admin
except Exception:  # noqa: BLE001
    napcat_admin = None  # type: ignore[assignment]

try:
    from . import browser_cookie
except Exception:  # noqa: BLE001
    browser_cookie = None  # type: ignore[assignment]

try:  # 「设置 → 版本更新」；缺失时该卡片说明情况，不能把整站拖垮
    from . import updater
except Exception:  # noqa: BLE001
    updater = None  # type: ignore[assignment]

LOGGER = logging.getLogger(__name__)

MAX_BODY_BYTES = 64 * 1024
MAX_SYNC_QR_CHARS = 512
ANNOUNCEMENT_REFRESH_SECONDS = 6 * 60 * 60
# 失败的公告结果只压 10 分钟：够挡住刷屏式重试，又不会让「公告源刚修好」或「刚联网」的用户
# 对着一条 6 小时的旧错误发呆（升级还会把 data\updates\state.json 里那条旧错误一起带过来）。
ANNOUNCEMENT_ERROR_RETRY_SECONDS = 10 * 60

_JOB_LOCK = threading.Lock()
_JOB_STATE: dict[str, Any] = {
    "active": None,
    "cancel": None,
    "history": {"running": False, "stage": "idle", "done": 0, "total": 0, "note": "", "result": None},
    "classify": {"running": False, "stage": "idle", "done": 0, "total": 0, "note": "", "result": None},
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


def _updates_state_path(server: ThreadingHTTPServer) -> Path:
    """`<data_dir>\\updates\\state.json`：公告与更新检查共用这一份（启动器也在写它）。"""
    return Path(server.settings.data_dir) / "updates" / "state.json"  # type: ignore[attr-defined]


def _read_updates_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_updates_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Use unique temp file to avoid cross-thread/cross-process collision
    temp_name = f"{path.name}.{os.getpid()}-{threading.get_ident()}.tmp"
    temporary = path.with_name(temp_name)
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    # Retry replace() on Windows PermissionError - file may be temporarily locked by another thread
    delays = (0.01, 0.02, 0.04, 0.08, 0.16)
    for attempt in range(len(delays) + 1):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == len(delays):
                LOGGER.warning("更新状态写入失败（重试 %d 次后仍被占用）：%s", attempt, path)
                raise
            time.sleep(delays[attempt])


def _normalize_announcement_response(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"ok": False, "items": [], "from_cache": False, "checked_at": "", "error": "公告暂时不可用", "current_version": _current_version()}
    items = value.get("items")
    return {
        "ok": bool(value.get("ok")),
        "items": [dict(item) for item in items if isinstance(item, dict)] if isinstance(items, list) else [],
        "from_cache": bool(value.get("from_cache")),
        "checked_at": str(value.get("checked_at") or ""),
        "error": "" if value.get("ok") else str(value.get("error") or "公告暂时不可用"),
        # 弹窗要显示「当前版本 vX」；拿不到就「未知」，不能让旧安装报 500。
        "current_version": str(value.get("current_version") or "") or _current_version(),
    }


def _announcement_is_fresh(checked_at: str, ttl_seconds: float = ANNOUNCEMENT_REFRESH_SECONDS) -> bool:
    try:
        checked = dt.datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=dt.timezone.utc)
        age = (dt.datetime.now(dt.timezone.utc) - checked).total_seconds()
        return -300 <= age < ttl_seconds
    except (TypeError, ValueError):
        return False


def _refresh_announcements(server: ThreadingHTTPServer) -> None:
    updates = Path(server.settings.data_dir) / "updates"  # type: ignore[attr-defined]
    state_path = _updates_state_path(server)
    try:
        channel = updater.resolve_channel(server.settings.update_channel) if updater is not None else None  # type: ignore[attr-defined]
        response = _normalize_announcement_response(announce.active(cache_dir=updates, channel=channel))
    except Exception:  # noqa: BLE001 - announcement failures must not break the web app
        response = {"ok": False, "items": [], "from_cache": False, "checked_at": "", "error": "公告暂时不可用"}
    if not response["checked_at"]:
        response["checked_at"] = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        with server.state_lock:  # type: ignore[attr-defined]
            state = _read_updates_state(state_path)
            state["announce_checked_at"] = response["checked_at"]
            state["announce_response"] = response
            _write_updates_state(state_path, state)
    except OSError:
        LOGGER.warning("公告状态缓存写入失败", exc_info=True)
    finally:
        with server.announce_lock:  # type: ignore[attr-defined]
            server.announce_running = False  # type: ignore[attr-defined]


def _announcement_response(server: ThreadingHTTPServer, *, force: bool = False) -> dict[str, Any]:
    state_path = _updates_state_path(server)
    started_refresh = False
    if force:
        # `?refresh=1`：这次要真结果（8 秒超时），别又给一份缓存。
        _refresh_announcements(server)
        with server.state_lock:  # type: ignore[attr-defined]
            fresh = _normalize_announcement_response(_read_updates_state(state_path).get("announce_response") or {})
        fresh["from_cache"] = False
        fresh["checking"] = False
        return fresh
    with server.state_lock:  # type: ignore[attr-defined]
        state = _read_updates_state(state_path)
        response = state.get("announce_response")
        cached = _normalize_announcement_response(response) if response is not None else None
        if cached is not None:
            cached_version = str(cached.get("current_version") or "").strip()
            running_version = _current_version().strip()
            if (
                cached_version
                and running_version
                and cached_version != "未知"
                and running_version != "未知"
                and cached_version != running_version
            ):
                cached = None
        checked_at = str(state.get("announce_checked_at") or "")
        # 缓存里的**失败**结果只算 10 分钟新鲜：公告源刚修好、或用户刚联网时，不该等满 6 小时
        # 才看到第一条公告；同时 10 分钟也足够挡住页面对失败源的重试风暴。
        ttl = ANNOUNCEMENT_REFRESH_SECONDS if (cached is None or (cached["ok"] and not cached["from_cache"])) else ANNOUNCEMENT_ERROR_RETRY_SECONDS
        if cached is not None and _announcement_is_fresh(checked_at, ttl):
            cached["from_cache"] = True
            cached["checking"] = False
            return cached
    with server.announce_lock:  # type: ignore[attr-defined]
        if not server.announce_running:  # type: ignore[attr-defined]
            server.announce_running = True  # type: ignore[attr-defined]
            started_refresh = True
            worker = threading.Thread(target=_refresh_announcements, args=(server,), name="announcement-refresh", daemon=True)
            try:
                worker.start()
            except Exception:  # noqa: BLE001
                server.announce_running = False  # type: ignore[attr-defined]
                started_refresh = False
                LOGGER.warning("公告后台检查无法启动", exc_info=True)
        checking = started_refresh or bool(server.announce_running)  # type: ignore[attr-defined]
    if cached is not None:
        cached["from_cache"] = True
        cached["checking"] = checking
        return cached
    return {"ok": False, "items": [], "from_cache": True, "checked_at": checked_at, "error": "公告正在检查", "checking": checking, "current_version": _current_version()}


# ---- 版本更新（网页「设置 → 版本更新」）----
# 查/下载/覆盖安装的实现在 qq_live_digest/updater.py（启动器也用它），这里只做缓存、
# 进度状态与「退出自己去重启」这三件网页才有的事。

UPDATE_CHECK_SECONDS = 6 * 60 * 60
_APPLYING_FLAG_NAME = "applying.flag"

_FEATURE_HEADS = {"新增功能", "新增", "new", "features", "feature"}
_FIX_HEADS = {"修复问题", "修复", "fixed", "fixes", "fix", "bugfix", "bugfixes"}


def _install_root() -> Path:
    """onedir zip 在安装根目录平铺两个 exe，更新应覆盖当前 executable 的目录。"""
    return Path(RUNTIME_ROOT)


def _current_version() -> str:
    """旧版安装没有 version 常量：这里兜成「未知」，绝不让页面 500。"""
    try:
        if updater is None:
            return "未知"
        return str(updater.current_version() or "") or "未知"
    except Exception:  # noqa: BLE001 - 兜底路径，不能因为它把设置页弄挂
        return "未知"


def _clean_chaoxing_cookie(value: Any) -> str:
    """Normalize a manually copied DevTools Cookie string without changing values."""
    cookie = str(value or "").replace("\r", "").replace("\n", "").strip()
    if cookie.startswith(("'", '"')) and cookie.endswith(cookie[0]) and len(cookie) >= 2:
        cookie = cookie[1:-1].strip()
    cookie = re.sub(r"^(?:document\.cookie|Cookie:)\s*(?:=\s*)?", "", cookie, flags=re.IGNORECASE).strip()
    if cookie.startswith(("'", '"')) and cookie.endswith(cookie[0]) and len(cookie) >= 2:
        cookie = cookie[1:-1].strip()
    return cookie


def _update_channel_name(settings: Any = None) -> str:
    try:
        configured = getattr(settings, "update_channel", None) if settings is not None else None
        return str(updater.resolve_channel(configured) or "") if updater is not None else ""
    except Exception:  # noqa: BLE001
        return ""


def _invalidate_update_cache(server: ThreadingHTTPServer) -> None:
    # updates/state.json 的读改写统一归 state_lock（公告刷新、_persist_update 都是它）；
    # 原来这里用 update_lock，两把锁各读改写整文件会互相覆盖，可能把刚失效的 update_result 写回来。
    with server.state_lock:  # type: ignore[attr-defined]
        state_path = _updates_state_path(server)
        state = _read_updates_state(state_path)
        state.pop("update_result", None)
        state.pop("update_checked_epoch", None)
        _write_updates_state(state_path, state)


def _notes_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def split_release_notes(notes: str) -> dict[str, list[str]]:
    """把 release 正文按 `## 新增功能` / `## 修复问题` 分节。

    标题匹配有意放宽（`## 新增`、`## Fixes`、大小写不限）；没分节就全归到 other，
    老 release 没有这个结构也不能报错。条目里的 `- `/`* ` 前缀在这里去掉。
    """
    sections: dict[str, list[str]] = {"features": [], "fixes": [], "other": []}
    bucket = "other"
    for raw in str(notes or "").splitlines():
        line = raw.strip()
        if line.startswith("#"):
            head = line.lstrip("#").strip().lower()
            if head in _FEATURE_HEADS:
                bucket = "features"
            elif head in _FIX_HEADS:
                bucket = "fixes"
            else:
                bucket = "other"  # 其它小标题（如「已知问题」）也算更新内容
            continue
        if not line:
            continue
        if line[:2] in {"- ", "* "}:
            line = line[2:].strip()
        sections[bucket].append(line)
    return sections


def _release_notes_inline(line: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", _notes_escape(line))


def _release_notes_html(notes: str) -> str:
    """自己渲染：**先转义再套标记**，正文里写 `<script>` 也只会被当文本显示。"""
    sections = split_release_notes(notes)
    blocks: list[str] = []
    for key, title in (("features", "新增功能"), ("fixes", "修复问题"), ("other", "更新内容")):
        lines = sections[key]
        if not lines:
            continue
        items = "".join("<li>%s</li>" % _release_notes_inline(line) for line in lines)
        blocks.append('<div class="update-notes-block"><h4>%s</h4><ul>%s</ul></div>' % (title, items))
    return "".join(blocks)


def _normalize_update(value: Any) -> dict[str, Any]:
    """裁剪成页面要用的形状：GitHub 的返回是不可信输入，缺字段一律降级。"""
    data = value if isinstance(value, dict) else {}
    latest = data.get("latest") if isinstance(data.get("latest"), dict) else {}
    current = data.get("current") if isinstance(data.get("current"), dict) else {}
    try:
        size = int(latest.get("size"))
    except (TypeError, ValueError):
        size = 0
    notes = str(latest.get("notes") or "")
    fallback_urls = latest.get("fallbacks")
    if not isinstance(fallback_urls, list):
        fallback_urls = []
    version = str(current.get("version") or "") or _current_version()
    # notice 是「不是失败」的说明（如无需更新、规则不同），error 只留给真失败；
    # 页面把两者分开渲染，避免把「无需更新」显示成「检查更新失败」。
    notice = str(data.get("notice") or "")
    result: dict[str, Any] = {
        "ok": bool(data.get("ok")),
        "checked_at": str(data.get("checked_at") or ""),
        "channel": str(data.get("channel") or ""),
        "current": {"version": version, "tag": str(current.get("tag") or "")},
        "latest": {
            "tag": str(latest.get("tag") or ""),
            "version": str(latest.get("version") or ""),
            "name": str(latest.get("name") or ""),
            "notes": notes,
            "notes_html": _release_notes_html(notes),
            "published_at": str(latest.get("published_at") or ""),
            "asset_name": str(latest.get("asset_name") or ""),
            "size": size,
            "sha256": str(latest.get("sha256") or ""),
            "download_url": str(latest.get("download_url") or ""),
            "html_url": str(latest.get("html_url") or ""),
            "fallbacks": [url for url in fallback_urls if isinstance(url, str)][:5],
        },
        "has_update": bool(data.get("has_update")),
        "source": str(data.get("source") or ""),
        "error": "" if data.get("ok") else str(data.get("error") or "检查更新失败"),
        "notice": notice,
    }
    if result["has_update"] and result["latest"]["tag"] and version.strip().lower() != "未知":
        latest_version = result["latest"]["version"]
        current_kind = _version_kind(version)
        latest_kind = _version_kind(latest_version)
        if current_kind and latest_kind and current_kind != latest_kind:
            result["has_update"] = False
            online_label = result["latest"]["tag"] or latest_version
            result["notice"] = (
                f"线上 Release 与本机版本号规则不同（本机 {version} / 线上 {online_label}），"
                "不自动判断，请到 Release 页面手工确认"
            )
        elif current_kind and current_kind == latest_kind and _is_downgrade(version, latest_version):
            # updater 只做「tag 不等于当前版本」的字符串比较，如果线上最新的 release 比手上这份还旧
            # （本地版还没发布），它会算成「有更新」——那会诱导用户把新版换回旧版。这里压掉。
            result["has_update"] = False
            result["notice"] = result["notice"] or "线上 Release 比本机版本旧（这一版还没发布），无需更新"
    return result


def _version_kind(value: str) -> str:
    """识别可比较的日期式或受限 SemVer 版本；日期式优先处理相同外观的歧义。"""
    value = value.strip()
    date = re.fullmatch(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", value)
    if date and 1 <= int(date.group(2)) <= 12 and 1 <= int(date.group(3)) <= 31:
        # YYYY.MM.DD 与 major=YYYY 的 SemVer 外观相同；本项目按日期式优先解释。
        return "date"
    if re.fullmatch(r"\d+\.\d+\.\d+(?:-(?:beta|rc)\.\d+)?", value):
        return "semver"
    return ""


def _is_downgrade(current: str, latest: str) -> bool:
    """本机版本比线上最新 release 还新？用公告模块里那份版本排序（含 prerelease 规则），不重复造。"""
    try:
        current_key = announce._ver_tuple(current)
        latest_key = announce._ver_tuple(latest)
    except Exception:  # noqa: BLE001
        return False
    if not current_key or not latest_key:
        return False
    return current_key > latest_key


def _failed_update(error: str) -> dict[str, Any]:
    return {
        "ok": False,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "channel": _update_channel_name(),
        "current": {"version": _current_version(), "tag": ""},
        "latest": {},
        "has_update": False,
        "error": error,
        "notice": "",
    }


def _persist_update(server: ThreadingHTTPServer, info: Any) -> dict[str, Any]:
    normalized = _normalize_update(info)
    state_path = _updates_state_path(server)
    try:
        with server.state_lock:  # type: ignore[attr-defined]
            state = _read_updates_state(state_path)
            state["update_checked_epoch"] = time.time()
            state["update_result"] = normalized
            _write_updates_state(state_path, state)
    except OSError:
        LOGGER.warning("更新状态缓存写入失败", exc_info=True)
    return normalized


def _refresh_update(server: ThreadingHTTPServer) -> None:
    """后台真查一次（不阻塞页面）。"""
    try:
        info = updater.check(channel=_update_channel_name(server.settings), timeout=8.0) if updater is not None else _failed_update("更新模块不可用")
    except Exception:  # noqa: BLE001 - updater 契约是绝不抛，这里再兜一层
        LOGGER.warning("检查更新失败", exc_info=True)
        info = _failed_update("检查更新失败")
    _persist_update(server, info)
    with server.update_lock:  # type: ignore[attr-defined]
        server.update_running = False  # type: ignore[attr-defined]


def _update_download_snapshot(server: ThreadingHTTPServer) -> dict[str, Any]:
    with server.update_lock:  # type: ignore[attr-defined]
        return dict(server.update_download)  # type: ignore[attr-defined]


def _applying_flag_present(server: ThreadingHTTPServer) -> bool:
    try:
        return (_updates_state_path(server).parent / _APPLYING_FLAG_NAME).is_file()
    except OSError:
        return False


def _update_payload(server: ThreadingHTTPServer, cached: dict[str, Any] | None, *, checking: bool) -> dict[str, Any]:
    cached = cached if isinstance(cached, dict) else None
    if cached is None:
        payload: dict[str, Any] = {
            "ok": False,
            "current": {"version": _current_version(), "tag": ""},
            "latest": None,
            "has_update": False,
            "channel": _update_channel_name(),
            "checking": bool(checking),
            "checked_at": "",
            "error": "" if checking else "",
            "notice": "" if checking else "还没有检查过更新",
        }
    else:
        payload = dict(cached)
        payload["checking"] = bool(checking)
    payload["download"] = _update_download_snapshot(server)
    payload["applying"] = _applying_flag_present(server)
    pending = None
    pending_update = getattr(updater, "pending_update", None)
    if callable(pending_update):
        try:
            record = pending_update(Path(server.settings.data_dir))  # type: ignore[attr-defined]
            if isinstance(record, dict):
                version = record.get("version")
                at = record.get("at")
                if isinstance(version, str) and version.strip() and isinstance(at, str):
                    pending = {"version": version, "at": at}
        except Exception:  # noqa: BLE001
            LOGGER.warning("读取待安装更新失败", exc_info=True)
    payload["pending"] = pending
    return payload


def _update_cache_version_changed(cached: Any) -> bool:
    current = cached.get("current") if isinstance(cached, dict) else None
    cached_version = str(current.get("version") or "").strip() if isinstance(current, dict) else ""
    running_version = _current_version().strip()
    if not cached_version or not running_version or cached_version == "未知" or running_version == "未知":
        return False
    return cached_version != running_version


def _update_response(server: ThreadingHTTPServer, *, force: bool = False) -> dict[str, Any]:
    state_path = _updates_state_path(server)
    if force:
        # 用户点了「检查更新」：这次要真结果，等它（8 秒超时）比再给一个缓存好。
        try:
            info = updater.check(channel=_update_channel_name(server.settings), timeout=8.0) if updater is not None else _failed_update("更新模块不可用")
        except Exception:  # noqa: BLE001
            LOGGER.warning("检查更新失败", exc_info=True)
            info = _failed_update("检查更新失败")
        return _update_payload(server, _persist_update(server, info), checking=False)
    with server.state_lock:  # type: ignore[attr-defined]
        state = _read_updates_state(state_path)
        cached = state.get("update_result") if isinstance(state.get("update_result"), dict) else None
        if _update_cache_version_changed(cached):
            state.pop("update_result", None)
            state.pop("update_checked_epoch", None)
            try:
                _write_updates_state(state_path, state)
            except OSError:
                LOGGER.warning("版本变化后的更新缓存清理失败", exc_info=True)
            cached = None
        try:
            checked_epoch = float(state.get("update_checked_epoch") or 0)
        except (TypeError, ValueError):
            checked_epoch = 0.0
        stale = cached is None or (time.time() - checked_epoch) >= UPDATE_CHECK_SECONDS
    with server.update_lock:  # type: ignore[attr-defined]
        checking = False
        if stale:
            if not server.update_running:  # type: ignore[attr-defined]
                server.update_running = True  # type: ignore[attr-defined]
                worker = threading.Thread(target=_refresh_update, args=(server,), name="update-check", daemon=True)
                try:
                    worker.start()
                    checking = True
                except Exception:  # noqa: BLE001
                    server.update_running = False  # type: ignore[attr-defined]
                    LOGGER.warning("更新检查线程无法启动", exc_info=True)
            else:
                checking = True
    return _update_payload(server, cached, checking=checking)


def _start_update_download(server: ThreadingHTTPServer) -> dict[str, Any]:
    if updater is None:
        return {"ok": False, "started": False, "error": "更新模块不可用"}
    with server.state_lock:  # type: ignore[attr-defined]
        state = _read_updates_state(_updates_state_path(server))
        cached = state.get("update_result") if isinstance(state.get("update_result"), dict) else {}
        if _update_cache_version_changed(cached):
            return {"ok": False, "started": False, "error": "还没有可下载的新版本，请先点「检查更新」"}
        latest = cached.get("latest") if isinstance(cached.get("latest"), dict) else {}
        if not latest.get("download_url"):
            return {"ok": False, "started": False, "error": "还没有可下载的新版本，请先点「检查更新」"}
        info = dict(latest)
    with server.update_lock:  # type: ignore[attr-defined]
        if server.update_download.get("state") == "downloading":  # type: ignore[attr-defined]
            return {"ok": True, "started": False}
        server.update_download = {  # type: ignore[attr-defined]
            "state": "downloading",
            "done": 0,
            "total": int(latest.get("size") or 0),
            "path": "",
            "error": "",
            "tag": str(latest.get("tag") or ""),
        }
    try:
        worker = threading.Thread(target=_download_update, args=(server, info), name="update-download", daemon=True)
        worker.start()
    except Exception as error:  # noqa: BLE001
        with server.update_lock:  # type: ignore[attr-defined]
            server.update_download.update({"state": "error", "error": "下载线程无法启动：%s" % error})  # type: ignore[attr-defined]
        return {"ok": False, "started": False, "error": "下载线程无法启动：%s" % error}
    return {"ok": True, "started": True}


def _download_update(server: ThreadingHTTPServer, latest: dict[str, Any]) -> None:
    def on_progress(done: int, total: int) -> None:
        with server.update_lock:  # type: ignore[attr-defined]
            if server.update_download.get("state") == "downloading":  # type: ignore[attr-defined]
                server.update_download["done"] = int(done)  # type: ignore[attr-defined]
                if total:
                    server.update_download["total"] = int(total)  # type: ignore[attr-defined]

    try:
        result = updater.download(latest, _updates_state_path(server).parent, timeout=60.0, on_progress=on_progress)  # type: ignore[union-attr]
    except Exception as error:  # noqa: BLE001
        result = {"ok": False, "error": str(error)}
    if result.get("ok"):
        try:
            archive = Path(str(result.get("path") or ""))
            version = str(latest.get("version") or latest.get("tag") or "").strip()
            if not version or not archive.is_file():
                raise RuntimeError("下载完成但更新包无效")
            outcome = updater.mark_pending(Path(server.settings.data_dir), zip_path=archive, version=version)  # type: ignore[attr-defined]
            # 网页「重启并更新」按钮和托盘文案都以这条记录为准（页面 JS：applyButton.hidden = !showLatest || !!pending），
            # 落盘失败还报成功的话，两个入口会一起静默消失，所以这里当失败处理、让用户重试。
            if isinstance(outcome, dict) and not outcome.get("ok"):
                raise RuntimeError("下载完成但待安装记录写入失败：%s" % (outcome.get("error") or "未知原因"))
        except Exception as error:  # noqa: BLE001
            result = {"ok": False, "error": str(error)}
    with server.update_lock:  # type: ignore[attr-defined]
        if result.get("ok"):
            archive = Path(str(result.get("path") or ""))
            server.update_download.update({"state": "done", "path": str(archive), "error": ""})  # type: ignore[attr-defined]
            if result.get("bytes"):
                server.update_download["done"] = int(result["bytes"])  # type: ignore[attr-defined]
        else:
            server.update_download.update({"state": "error", "path": "", "error": str(result.get("error") or "下载失败")})  # type: ignore[attr-defined]


def _apply_update(server: ThreadingHTTPServer) -> dict[str, Any]:
    if updater is None:
        return {"ok": False, "restarting": False, "error": "更新模块不可用"}
    snapshot = _update_download_snapshot(server)
    if snapshot.get("state") != "done" or not snapshot.get("path"):
        return {"ok": False, "restarting": False, "error": "还没下载好更新包，请先点「一键更新」等它下载完"}
    archive = Path(str(snapshot["path"]))
    try:
        exists = archive.is_file()
    except OSError:
        exists = False
    if not exists:
        return {"ok": False, "restarting": False, "error": "更新包不见了（%s），请重新点「一键更新」" % archive.name}
    result = updater.apply_async(_install_root(), archive, os.getpid(), data_dir=Path(server.settings.data_dir))  # type: ignore[attr-defined]
    if not result.get("ok"):
        return {"ok": False, "restarting": False, "error": str(result.get("error") or "启动更新脚本失败")}
    # 脚本几秒内就会杀掉本安装目录的进程；先把响应发出去，再自己退（看门狗已被 applying.flag 压住）。
    threading.Timer(2.0, lambda: os._exit(0)).start()
    return {"ok": True, "restarting": True, "script": str(result.get("script") or "")}


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
ADAPTER_CACHE_SECONDS = 60.0
_adapter_lock = threading.Lock()
_adapter_cache: tuple[float, tuple[str, ...]] | None = None


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


def _adapter_ipv4s() -> list[str]:
    """真实网卡的 IPv4，WLAN / 以太网排前面；取不到清单时返回空表，交给调用方回落。"""
    global _adapter_cache
    if os.name != "nt":
        return []
    with _adapter_lock:
        if _adapter_cache is not None and time.monotonic() - _adapter_cache[0] < ADAPTER_CACHE_SECONDS:
            return list(_adapter_cache[1])
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
    with _adapter_lock:
        _adapter_cache = (time.monotonic(), result)
    return list(result)


def _reset_adapter_cache() -> None:
    """测试用：丢掉网卡清单缓存。"""
    global _adapter_cache
    with _adapter_lock:
        _adapter_cache = None


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

    直接读 tailscale serve 的配置，确认确实有一条把 /xxx.ics 反代到本机这个端口的公网规则；
    没有装 Tailscale 或没有这条规则时返回空串，调用方退回局域网地址。
    """
    executable = shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"
    if not executable or not Path(executable).is_file():
        return ""
    try:
        completed = subprocess.run([executable, "serve", "status", "--json"], capture_output=True, timeout=8, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        config = json.loads(decode_console_output(completed.stdout) or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    for target, entry in (config.get("Web") or {}).items():
        if not str(target).endswith(":443"):
            continue
        for route, handler in ((entry or {}).get("Handlers") or {}).items():
            proxy = str((handler or {}).get("Proxy") or "")
            # Proxy 是完整 URL（如 http://127.0.0.1:8766/calendar.ics），必须用 urlsplit 取端口：
            # 直接按 ":" 切最后一段会拿到 "8766/calendar.ics"，端口永远比不上，公网地址就消失了。
            try:
                proxy_port = urllib.parse.urlsplit(proxy).port
            except ValueError:
                continue
            if proxy_port != port or not str(route).lower().endswith(".ics"):
                continue
            return f"https://{str(target).rsplit(':', 1)[0]}{route}"
    return ""


TAILSCALE_FUNNEL_PORTS = (443, 8443, 10000)


def _tailscale_executable() -> str:
    executable = shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"
    return executable if executable and Path(executable).is_file() else ""


def _tailscale_serve_config(executable: str) -> dict[str, Any]:
    try:
        completed = subprocess.run([executable, "serve", "status", "--json"], capture_output=True, timeout=8, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        config = json.loads(decode_console_output(completed.stdout) or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    return config if isinstance(config, dict) else {}


def _tailscale_public_app_url(port: int) -> str:
    """手机 App 用的公网地址：整站（页面 + /api/*）都反代到本机这个端口的那条 Funnel 规则。

    和只认 .ics 的日历地址不同，这里必须有一条 "/" 路由，而且那个端口得在 AllowFunnel 里。
    """
    executable = _tailscale_executable()
    if not executable:
        return ""
    config = _tailscale_serve_config(executable)
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


def _tailscale_status_hint(executable: str) -> str:
    """Tailscale 没登录 / 没运行时给一句能看懂的原因；状态正常或查不到时返回空串。"""
    try:
        completed = subprocess.run([executable, "status", "--json"], capture_output=True, timeout=8, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        status = json.loads(decode_console_output(completed.stdout) or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return "没连上 Tailscale（命令跑不起来）：请先打开 Tailscale 并登录，再点一次「开通公网入口」。"
    state = str((status or {}).get("BackendState") or "") if isinstance(status, dict) else ""
    if state == "Running":
        return ""
    if state in {"", "NeedsLogin", "NoState", "Stopped"}:
        return "Tailscale 还没登录：请先打开 Tailscale 用同一个账号登录，再点一次「开通公网入口」。"
    return f"Tailscale 现在不可用（状态 {state}）：请先打开 Tailscale 登录并确认已连接，再点一次。"


def _enable_tailscale_funnel(port: int, public_port: int = 8443) -> dict[str, Any]:
    """电脑上点一下就整站开到公网（Tailscale Funnel，免费，不用买服务器）。"""
    executable = _tailscale_executable()
    if not executable:
        return {"ok": False, "error": "这台电脑上没找到 Tailscale，装了以后再点一次。"}
    if public_port not in TAILSCALE_FUNNEL_PORTS:
        return {"ok": False, "error": "Tailscale 只允许 443、8443、10000 这三个公网端口。"}
    hint = _tailscale_status_hint(executable)
    if hint:
        return {"ok": False, "error": hint}
    command = [executable, "funnel", "--bg", f"--https={public_port}", f"http://127.0.0.1:{port}"]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=30, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    except (OSError, subprocess.SubprocessError) as error:
        return {"ok": False, "error": f"执行 Tailscale 命令失败：{error}"}
    url = _tailscale_public_app_url(port)
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



FIREWALL_LOOKUP_TIMEOUT = 3.0
FIREWALL_CACHE_SECONDS = 60.0
_firewall_lock = threading.Lock()
# 入站规则清单与端口无关，按整个清单缓存一次（netsh 枚举一次约 0.8 秒、几千行）。
_firewall_cache: tuple[float, tuple[dict[str, str], ...]] | None = None


def _firewall_rule_command(port: int) -> str:
    return f'netsh advfirewall firewall add rule name="群务台 {port}" dir=in action=allow protocol=TCP localport={port}'


def _firewall_inbound_rules() -> tuple[dict[str, str], ...]:
    """读 netsh 的入站规则（只读、不提权）；读不到返回空元组，调用方当作「没查成」。"""
    global _firewall_cache
    if os.name != "nt":
        return ()
    with _firewall_lock:
        cached = _firewall_cache
        if cached and time.monotonic() - cached[0] < FIREWALL_CACHE_SECONDS:
            return cached[1]
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
    rules = tuple(blocks)
    if rules:
        with _firewall_lock:
            _firewall_cache = (time.monotonic(), rules)
    return rules


def _firewall_allows(port: int) -> dict[str, Any]:
    """查本机有没有放行该 TCP 端口入站的防火墙规则。只读、不提权、不改设置。"""
    result: dict[str, Any] = {"checked": False, "allowed": False, "port": port, "command": _firewall_rule_command(port)}
    rules = _firewall_inbound_rules()
    if not rules:
        return result
    result["checked"] = True
    wanted = str(port)
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


def _reset_firewall_cache() -> None:
    """测试用：丢掉防火墙检测缓存。"""
    global _firewall_cache
    with _firewall_lock:
        _firewall_cache = None


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
        errors.append("服务只监听本机回环地址，手机无法访问当前广告地址。请将 QQ_DIGEST_WEB_HOST 改为 0.0.0.0 并重启服务。")
    elif listener is not None and not listener.is_unspecified and address is not None and listener != address:
        errors.append("手机地址与服务监听地址不一致。请检查 QQ_DIGEST_WEB_HOST 和手机要用的地址。")
    return errors


def _calendar_suffix(token: str) -> str:
    return "?token=" + urllib.parse.quote(token, safe="") if token else ""


def _advertised_calendars(port: int, token: str) -> list[dict[str, Any]]:
    """本服务对外公布的日历订阅地址：公网（Tailscale Funnel）优先，其余是各网卡局域网地址。

    公网地址是唯一「手机用流量也能订阅」的路径；局域网地址排后面，供用户按自己网络情况挑选。
    """
    suffix = _calendar_suffix(token)
    calendars: list[dict[str, Any]] = []
    public = _tailscale_calendar_url(port)
    if public:
        calendars.append({"name": "公网地址", "kind": "public", "url": public + suffix, "hint": "手机用流量或任意 Wi-Fi 都能订阅"})
    for host in _lan_hosts():
        calendars.append({"name": f"局域网 {host}", "kind": "lan", "url": f"http://{host}:{port}/calendar.ics{suffix}", "hint": "手机与电脑在同一个网络时可用"})
    return calendars


def _job_snapshot(kind: str) -> dict[str, Any]:
    with _JOB_LOCK:
        return {key: _JOB_STATE[kind][key] for key in ("running", "stage", "done", "total", "note", "result")}


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
    if not isinstance(value, list) or not value or len(value) > 500:
        raise ValueError("请至少选择一个群，最多可选择 500 个")
    groups = []
    for item in value:
        group_id = str(item or "").strip()
        if not group_id or len(group_id) > 128:
            raise ValueError("群号无效")
        if group_id not in groups:
            groups.append(group_id)
    return groups


def _parse_day(value: Any, *, end: bool = False) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    day = dt.date.fromisoformat(text)
    if day.isoformat() != text:
        raise ValueError("日期格式应为 YYYY-MM-DD")
    return dt.datetime.combine(day, dt.time(23, 59, 59) if end else dt.time.min)


MANIFEST_JSON = json.dumps(
    {
        "name": "群消息待办",
        "short_name": "群待办",
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
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>月历 - 群消息待办</title>
<style>:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;padding:16px;background:#0a0c12;color:#f3f5fa;font:15px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}main{max-width:680px;margin:auto}header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}h1{font-size:20px;margin:0}.nav{display:flex;gap:6px}button{border:1px solid #303747;background:#171c29;color:#f3f5fa;border-radius:6px;padding:7px 11px;font:inherit;cursor:pointer}.grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:4px}.weekday{text-align:center;color:#9aa4b5;font-size:12px;padding:4px}.day{min-height:76px;padding:6px;background:#141925;border:1px solid #252c3b;border-radius:6px;overflow:hidden}.day.muted{opacity:.42}.day.today{border-color:#5b8cff}.num{font-size:12px;color:#b9c4d8}.event{display:block;margin-top:4px;padding:3px 4px;background:#26385e;color:#dbe7ff;border-radius:4px;font-size:11px;line-height:1.3;overflow-wrap:anywhere}@media(max-width:420px){body{padding:10px}.day{min-height:62px;padding:4px}.event{font-size:10px}.num{font-size:11px}}</style></head>
<body><main><header><h1 id="title">月历</h1><div class="nav"><button id="prev" type="button">上一月</button><button id="next" type="button">下一月</button></div></header><div id="calendar" class="grid"></div></main>
<script>(function(){var KEY='qq_digest_token';var params=new URLSearchParams(location.search);if(params.get('token'))localStorage.setItem(KEY,params.get('token'));var token=localStorage.getItem(KEY)||'';var cursor=new Date();cursor.setDate(1);var tasks=[];var names=['日','一','二','三','四','五','六'];function esc(s){return String(s||'').replace(/[&<>]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c]});}function render(){var y=cursor.getFullYear(),m=cursor.getMonth(),root=document.getElementById('calendar');document.getElementById('title').textContent=y+'年'+(m+1)+'月';root.innerHTML='';names.forEach(function(n){var h=document.createElement('div');h.className='weekday';h.textContent=n;root.appendChild(h);});var first=new Date(y,m,1).getDay(),count=new Date(y,m+1,0).getDate(),prevCount=new Date(y,m,0).getDate();for(var i=0;i<42;i++){var d=i-first+1, date=new Date(y,m,d), cell=document.createElement('div');cell.className='day'+(date.getMonth()!==m?' muted':'');if(date.toDateString()===new Date().toDateString())cell.className+=' today';cell.innerHTML='<div class="num">'+date.getDate()+'</div>';tasks.forEach(function(t){if(!t.deadline)return;var raw=String(t.deadline), key=raw.slice(0,10);if(key===date.getFullYear()+'-'+String(date.getMonth()+1).padStart(2,'0')+'-'+String(date.getDate()).padStart(2,'0')){var e=document.createElement('div');e.className='event';e.textContent=t.summary||'未命名任务';cell.appendChild(e);}});root.appendChild(cell);}}document.getElementById('prev').onclick=function(){cursor.setMonth(cursor.getMonth()-1);render();};document.getElementById('next').onclick=function(){cursor.setMonth(cursor.getMonth()+1);render();};fetch('/api/tasks',{headers:{'X-Token':token}}).then(function(r){return r.json();}).then(function(data){tasks=[].concat(data.today||[],data.week||[],data.later||[],data.done||[],data.candidates||[]);render();}).catch(function(){render();});})();</script></body></html>"""
SETUP_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>接入向导</title><style>body{margin:0;padding:18px;background:#0a0c12;color:#f3f5fa;font:15px sans-serif}main{max-width:600px;margin:auto}.card{padding:16px;margin:12px 0;background:#171c29;border:1px solid #303747;border-radius:10px}img{display:block;width:240px;height:240px;object-fit:contain;margin:auto;background:#fff}button{padding:8px 12px;margin:4px;border-radius:7px;border:1px solid #475569;background:#26385e;color:white}label{display:block;padding:10px;border-bottom:1px solid #303747}#groups{margin-top:10px}#go{padding:12px 22px;font-size:16px;background:#2563eb;border:0}#steps div{padding:4px 0;font-size:13px;line-height:1.5}</style></head><body><main><h1>QQ 接入向导</h1><div class="card"><button id="go" type="button">一键接入</button><div id="steps"></div></div><div id="status" class="card">正在检查 QQ 登录状态…</div><div id="qrbox" class="card"><p>用 QQ 主号扫码</p><img id="qr" alt="登录二维码"></div><div id="groupbox" class="card" hidden><div><button id="all" type="button">全选</button><button id="none" type="button">全不选</button></div><div id="groups"></div><button id="save" type="button">保存订阅</button><p id="result"></p><a href="/calendar">打开月历</a></div></main><script>(function(){var selected={},names={},loaded=false;var token=new URLSearchParams(location.search).get('token')||localStorage.getItem('qq_digest_token')||'';function api(path,opt){opt=opt||{};opt.headers=Object.assign({'X-Token':token},opt.headers||{});return fetch(path,opt).then(function(r){return r.json().then(function(x){if(!r.ok)throw Error(x.error||'请求失败');return x;});});}function qrSrc(){return '/api/napcat/qrcode?token='+encodeURIComponent(token)+'&t='+Date.now();}document.getElementById('qr').src=qrSrc();function run(){var go=document.getElementById('go'),box=document.getElementById('steps');go.disabled=true;go.textContent='正在接入…';box.innerHTML='';api('/api/napcat/autosetup',{method:'POST'}).then(function(d){(d.steps||[]).forEach(function(s){var p=document.createElement('div');p.textContent=(s.ok?'✓ ':'✗ ')+s.name+'：'+s.detail;box.appendChild(p);});if(d.qrcode_path){document.getElementById('qr').src=qrSrc();}if(!d.ok){var e=document.createElement('div');e.textContent='未完成：'+(d.error||'');box.appendChild(e);}check();}).catch(function(e){box.textContent='接入失败：'+e.message;}).then(function(){go.disabled=false;go.textContent='一键接入';});}document.getElementById('go').addEventListener('click',run);var lastQrStamp=null;function check(){api('/api/napcat/status').then(function(s){var ok=s.ok&&s.online;document.getElementById('status').textContent=ok?'已登录：'+(s.nickname||'')+'（'+(s.user_id||'')+'）':'未登录，请扫码';document.getElementById('qrbox').hidden=ok;document.getElementById('groupbox').hidden=!ok;if(!ok){var st=String(s.qr_stamp||'');if(st!==lastQrStamp){lastQrStamp=st;document.getElementById('qr').src=qrSrc();}}if(ok&&!loaded)loadGroups();}).catch(function(e){document.getElementById('status').textContent='连接 NapCat 失败：'+e.message;});}function loadGroups(){api('/api/napcat/groups').then(function(d){var root=document.getElementById('groups');root.innerHTML='';d.groups.forEach(function(g){var id=String(g.group_id);names[id]=g.name||'';selected[id]=!!g.selected;var l=document.createElement('label');var c=document.createElement('input');c.type='checkbox';c.value=id;c.checked=!!selected[id];c.onchange=function(){selected[id]=c.checked;};l.appendChild(c);l.appendChild(document.createTextNode(' '+g.name+'（'+id+'）'));root.appendChild(l);});loaded=true;});}document.getElementById('all').onclick=function(){document.querySelectorAll('#groups input').forEach(function(c){c.checked=true;selected[c.value]=true;});};document.getElementById('none').onclick=function(){document.querySelectorAll('#groups input').forEach(function(c){c.checked=false;selected[c.value]=false;});};/* api('/api/subscriptions', ...).catch(function(e){ result.textContent='保存失败'; }) */
document.getElementById('save').onclick=function(){var groups=Object.keys(selected).filter(function(id){return selected[id];});api('/api/subscriptions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({groups:groups,aliases:groups.reduce(function(a,id){a[id]=names[id]||'';return a;},{})})}).then(function(){document.getElementById('result').textContent='已保存并立即生效';}).catch(function(e){document.getElementById('result').textContent='保存失败：'+e.message;});};setInterval(function(){check();},5000);check();})();</script></body></html>"""
PAGE_HTML = (Path(__file__).parent / "web_static" / "page.html").read_text(encoding="utf-8")


def _deadline_label(value: dt.datetime | None, now: dt.datetime) -> tuple[str, bool]:
    if not isinstance(value, dt.datetime):
        return "", False
    overdue = value < now
    if value.date() == now.date():
        return (f"今天 {value:%H:%M} 已过" if overdue else f"今天 {value:%H:%M} 截止"), overdue
    if value.date() == now.date() + dt.timedelta(days=1):
        return f"明天 {value:%H:%M} 截止", False
    return f"{value:%m-%d %H:%M} 截止", overdue and value.date() < now.date()


def _confidence_text(task: dict[str, Any]) -> str:
    confidence = float(task.get("confidence") or 0.0)
    if confidence >= 0.75:
        level = "把握较高"
    elif confidence >= 0.55:
        level = "把握中等"
    else:
        level = "把握较低"
    return f"{level} {round(confidence * 100)}%"


def snooze_default_until(settings: Settings, now: dt.datetime) -> str:
    """稍后提醒的默认时间：次日早晨的截止提醒时刻，否则次日同一时间。"""
    target = now + dt.timedelta(days=1)
    if settings.deadline_reminders_enabled:
        try:
            hour, minute = (int(part) for part in str(settings.deadline_morning).split(":", 1))
        except (TypeError, ValueError):
            hour, minute = 7, 30
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            target = target.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return iso(target)


def group_tasks(tasks: list[dict[str, Any]], now: dt.datetime) -> dict[str, Any]:
    candidate: list[dict[str, Any]] = []
    today: list[dict[str, Any]] = []
    week: list[dict[str, Any]] = []
    later: list[dict[str, Any]] = []
    done: list[dict[str, Any]] = []
    horizon = now + dt.timedelta(days=7)
    for task in sorted(tasks, key=lambda item: not effective_urgent(item)):
        status = str(task.get("status") or "open")
        if status in {"dismissed", "expired"}:
            continue
        deadline = parse_iso(task.get("deadline"))
        payload = dict(task)
        payload["effective_urgent"] = effective_urgent(task)
        payload["deadline_text"], payload["overdue"] = _deadline_label(deadline, now)
        snooze_until = parse_iso(payload.get("snooze_until"))
        payload["snoozed"] = bool(isinstance(snooze_until, dt.datetime) and snooze_until > now)
        payload["snooze_text"] = (
            snooze_until.strftime("%m-%d %H:%M") if payload["snoozed"] and snooze_until else ""
        )
        payload["done"] = status == "done"
        if status == "candidate":
            payload["confidence_text"] = _confidence_text(payload)
            detail = str(payload.get("candidate_detail") or "")
            try:
                parsed = json.loads(detail) if detail else {}
            except json.JSONDecodeError:
                parsed = {}
            payload["confidence_reason"] = str(parsed.get("reason") or "需要你确认后再进入正式待办。")
            candidate.append(payload)
            continue
        if payload["done"]:
            done.append(payload)
            continue
        if deadline is None:
            later.append(payload)
        elif deadline.date() <= now.date():
            today.append(payload)
        elif deadline <= horizon:
            week.append(payload)
        else:
            later.append(payload)
    done.sort(key=lambda item: str(item.get("done_at") or ""), reverse=True)
    return {
        "candidates": candidate[:40],
        "today": today[:40],
        "week": week[:40],
        "later": later[:40],
        "done": done[:20],
    }


def overview(tasks: list[dict[str, Any]], grouped: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    open_tasks = [task for task in tasks if str(task.get("status") or "open") == "open"]
    candidates = grouped.get("candidates") or []
    done_count = sum(1 for task in tasks if str(task.get("status") or "") == "done")
    today = grouped["today"]
    if today:
        nearest = min(
            (parse_iso(task.get("deadline")) for task in today if parse_iso(task.get("deadline"))),
            default=None,
        )
        overdue = sum(1 for task in today if task.get("overdue"))
        if overdue:
            headline = f"今天 {len(today)} 件 · 逾期 {overdue} 件"
        elif nearest:
            headline = f"今天 {len(today)} 件，最近一件 {nearest:%H:%M} 截止"
        else:
            headline = f"今天 {len(today)} 件"
    elif candidates:
        headline = f"{len(candidates)} 条通知待确认"
    elif open_tasks:
        headline = "今天没有到期的事"
    else:
        headline = "所有待办都清空了"
    if any(task.get("overdue") for task in today):
        subline = "逾期事项已收起，可展开后查看"
    elif today:
        subline = "按截止时间从上到下处理"
    elif candidates:
        subline = "确认后才会进入正式待办"
    elif open_tasks:
        subline = "当前没有到期事项，可按自己的节奏推进"
    else:
        subline = "可以休息一下，新的通知会自动汇总"
    actionable = len(open_tasks) + len(candidates) + done_count
    progress = round(done_count * 100 / actionable) if actionable else 0
    return {"headline": headline, "subline": subline, "progress": progress}


# 设置页可写的推送通道键（前端字段名 → .env 变量名）。
_PUSH_SETTING_ENV_KEYS = {
    "wxpusher_app_token": "WXPUSHER_APP_TOKEN",
    "wxpusher_uids": "WXPUSHER_UIDS",
    "wxpusher_topic_ids": "WXPUSHER_TOPIC_IDS",
    "serverchan_keys": "SERVERCHAN_KEYS",
    "pushplus_tokens": "PUSHPLUS_TOKENS",
    "webhook_urls": "QQ_DIGEST_WEBHOOKS",
}
_PUSH_SETTING_LABELS = {
    "wxpusher_app_token": "WxPusher App Token",
    "wxpusher_uids": "WxPusher UID",
    "wxpusher_topic_ids": "WxPusher Topic ID",
    "serverchan_keys": "Server 酱 SendKey",
    "pushplus_tokens": "PushPlus Token",
    "webhook_urls": "Webhook 地址",
}
# 每个通道「去哪里拿值」的官网入口。链接由页面用 JS 填进 <a>，
# 页面本身不写外链（保持离线可用、也避免把地址写死在 HTML 里）。
PUSH_HELP_LINKS = {
    "wxpusher": "https://wxpusher.zjiecode.com/admin/",
    "serverchan": "https://sct.ftqq.com/sendkey",
    "pushplus": "https://www.pushplus.plus/",
}


def _mask_push_secret(value: str) -> str:
    """只暴露尾 4 位，其余打码（短值全码）。"""
    text = str(value)
    if len(text) <= 4:
        return "*" * len(text)
    return "*" * (len(text) - 4) + text[-4:]


def _push_setting_values(payload: dict[str, Any], keys: set[str]) -> tuple[dict[str, str], dict[str, Any]]:
    """校验设置页提交的推送通道配置。

    返回 (写进 .env 的键值, 写回内存 Settings 的字段值)。任何一项非法就整体拒绝，
    调用方拿到 ValueError 后原样返回给页面，不做部分写入。
    """
    updates: dict[str, str] = {}
    applied: dict[str, Any] = {}
    for name in sorted(keys):
        raw = payload.get(name)
        if raw is None or isinstance(raw, (dict, bool, int, float)):
            raise ValueError(f"{_PUSH_SETTING_LABELS[name]}格式不正确")
        items = split_list(raw)
        if name == "wxpusher_app_token":
            if len(items) > 1:
                raise ValueError("WxPusher App Token 只能填一个，不要用逗号或空格分隔")
            value: Any = items[0] if items else ""
        elif name == "wxpusher_topic_ids":
            if any(not item.isdigit() or int(item) <= 0 for item in items):
                raise ValueError("WxPusher Topic ID 只能是正整数")
            value = tuple(int(item) for item in items)
        elif name == "webhook_urls":
            for item in items:
                if not item.lower().startswith(("http://", "https://")):
                    raise ValueError("Webhook 地址必须以 http:// 或 https:// 开头")
            value = items
        else:
            value = items
        if isinstance(value, str):
            updates[_PUSH_SETTING_ENV_KEYS[name]] = value
        else:
            updates[_PUSH_SETTING_ENV_KEYS[name]] = ",".join(str(item) for item in value)
        applied[name] = value
    return updates, applied


def _push_setting_state(settings: Settings) -> dict[str, Any]:
    """回给页面的脱敏状态：只给是否配置、数量与尾 4 位。"""
    state: dict[str, Any] = {}
    for name in _PUSH_SETTING_ENV_KEYS:
        value = getattr(settings, name, None)
        items = [str(item) for item in value] if isinstance(value, (tuple, list)) else ([str(value)] if value else [])
        state[name] = {
            "set": bool(items),
            "count": len(items),
            "masked": [_mask_push_secret(item) for item in items[:3]],
        }
    return state


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


def _llm_setting_state(settings: Settings) -> dict[str, Any]:
    """回给页面的脱敏状态：当前选的哪家、模型名、key 只给尾 4 位。"""
    key = str(settings.dashscope_api_key or "")
    return {
        "enabled": bool(settings.llm_enabled),
        "active": bool(settings.llm_active),
        "provider": _llm_provider_id(settings),
        "endpoint": str(settings.dashscope_endpoint or ""),
        "model": str(settings.dashscope_model or ""),
        "vision": bool(settings.vision_active),
        "vision_model": str(settings.vision_model or ""),
        "key_set": bool(key),
        "key_masked": _mask_push_secret(key) if key else "",
        "timeout": int(settings.llm_timeout or 60),
    }


def _llm_setting_updates(payload: dict[str, Any], settings: Settings) -> tuple[dict[str, str], dict[str, Any]]:
    """校验设置页提交的 AI 接入配置，返回 (.env 更新, 写回内存的字段)。"""
    provider_id = str(payload.get("llm_provider") or "").strip()
    provider = _llm_provider(provider_id)
    if provider is None:
        raise ValueError("请先选择一个 AI 服务商")
    raw_key = payload.get("llm_api_key")
    if raw_key is not None and isinstance(raw_key, (dict, bool, int, float)):
        raise ValueError("API Key 格式不正确")
    api_key = str(raw_key or "").strip()
    if provider_id == "off":
        return {"QQ_DIGEST_LLM": "0"}, {"llm_enabled": False}
    if provider.get("needs_key") and not (api_key or str(settings.dashscope_api_key or "").strip()):
        raise ValueError("还没填 API Key：点上面的链接拿到以后粘进来就行")
    endpoint = str(payload.get("llm_endpoint") or provider.get("endpoint") or "").strip()
    if not endpoint.lower().startswith(("http://", "https://")):
        raise ValueError("接口地址要以 http:// 或 https:// 开头")
    model = str(payload.get("llm_model") or provider.get("model") or "").strip()
    if not model:
        raise ValueError("还没填模型名：选好服务商以后它应该自动填上")
    vision_model = str(provider.get("vision_model") or "")
    updates = {
        "QQ_DIGEST_LLM": "1",
        "QQ_DIGEST_LLM_ENDPOINT": endpoint,
        "QQ_DIGEST_LLM_MODEL": model,
        "QQ_DIGEST_VL_MODEL": vision_model,
    }
    applied: dict[str, Any] = {
        "llm_enabled": True,
        "dashscope_endpoint": endpoint,
        "dashscope_model": model,
        "vision_model": vision_model or DEFAULT_VISION_MODEL,
    }
    if api_key:
        updates["QQ_DIGEST_LLM_API_KEY"] = api_key
        applied["dashscope_api_key"] = api_key
    return updates, applied


def _llm_test_error(error: Exception) -> str:
    """把各家服务商的报错翻译成用户看得懂的一句话。"""
    text = str(error) or error.__class__.__name__
    low = text.lower()
    if "dashscope_api_key" in text or "未配置" in text:
        return "还没有填钥匙：把服务商给你的那串 API Key 粘进去再试。"
    if "401" in low or "unauthorized" in low or "invalid api key" in low or "authentication" in low:
        return "钥匙不对或已经失效：回到拿钥匙的那个页面重新复制一次。"
    if "403" in low or "forbidden" in low:
        return "服务商拒绝了这次请求：确认钥匙有没有被停用，或者账号还没实名。"
    if "404" in low or ("model" in low and "not found" in low):
        return "服务商说不认识这个模型名：把模型名改回默认的试试。"
    if "429" in low or "rate limit" in low or "quota" in low or "insufficient" in low:
        return "额度用完或被限流了：换一个钥匙，或者过一会儿再试。"
    if "timed out" in low or "timeout" in low:
        return "等太久没回应：检查一下网络，本机 Ollama 的话确认模型已经下载完。"
    if "connection refused" in low or "10061" in text:
        return "连不上这个地址：本机 Ollama 要先启动，换服务商的话确认地址有没有写对。"
    return text[:200]


# 微信（实验性）：只在这里读群名。密钥、salt、内存字节一律不进日志与响应。
_WECHAT_READ_STATUS: dict[str, Any] = {"at": "", "seconds": 0.0, "error": "", "groups": 0}

# WEB-4: pairing issues a 6-digit code and secrets.token_urlsafe(24) (32 chars). Anything
# longer on the unauthenticated /api/pair/status route is junk, so cap it before comparison.
_PAIR_CODE_MAX = 16
_PAIR_SECRET_MAX = 128


def wechat_group_label(
    group_id: str,
    names: Mapping[str, str] | None,
    aliases: Mapping[str, str] | None = None,
) -> str:
    """微信群显示名：别名 > 真实群名 > 群 ID 前 8 位。"""
    key = str(group_id or "")
    if not key:
        return ""
    alias = str((aliases or {}).get(key) or "").strip()
    if alias:
        return alias
    name = str((names or {}).get(key) or "").strip()
    if name and name != key:
        return name
    return key[:8]


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
    """
    parsed = urllib.parse.urlparse(url)
    if not parsed.hostname or not parsed.port:
        return True  # 拆不出主机/端口就照旧交给 urllib
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout):
            return True
    except OSError:
        return False


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


class _Handler(BaseHTTPRequestHandler):
    server_version = "qq-tasks/1.0"

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
            self.end_headers()
            self.wfile.write(raw)
        except ConnectionError:
            self.close_connection = True

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self._reply(status, json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8", "no-store")

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
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query, max_num_fields=8)
        for candidate in params.get("token", []):
            if hmac.compare_digest(str(candidate), token):
                return True
        return hmac.compare_digest(str(self.headers.get("X-Token", "")), token)

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
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValueError("请求内容为空或过大")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise ValueError("JSON 格式无效") from error
        if not isinstance(payload, dict):
            raise ValueError("请求内容必须是 JSON 对象")
        return payload

    def _napcat(self, action: str) -> dict[str, Any]:
        try:
            data = _request_napcat_action(self.server.settings, action)  # type: ignore[attr-defined]
            return {"ok": True, "data": data}
        except Exception as error:  # noqa: BLE001
            return {"ok": False, "error": str(error)}

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
        boot = napcat_admin.detect_boot(settings.data_dir) if napcat_admin is not None else None
        if not status.get("ok") or not login.get("ok"):
            error = status.get("error") or login.get("error") or "NapCat error"
            running = bool(napcat_admin.running_pids(settings)) if napcat_admin is not None else False
            return {"ok": False, "online": False, "good": False, "user_id": "", "nickname": "", "napcat_installed": bool(boot), "napcat_root": (boot or {}).get("data_dir"), "qr_stamp": stamp, "napcat_running": running, "error": error}
        sd, ld = status["data"], login["data"]
        return {"ok": True, "online": bool(sd.get("online")), "good": bool(sd.get("good")), "user_id": sd.get("user_id") or ld.get("user_id", ""), "nickname": ld.get("nickname", ""), "napcat_installed": bool(boot), "napcat_root": (boot or {}).get("data_dir"), "qr_stamp": stamp, "napcat_running": True, "error": ""}

    def _napcat_doctor_payload(self) -> dict[str, Any]:
        if napcat_admin is None:
            return {"ok": False, "steps": [], "broken": "", "hint": "", "error": "一键接入模块不可用"}
        try:
            return napcat_admin.diagnose(self.server.settings)  # type: ignore[attr-defined]
        except Exception as error:  # noqa: BLE001
            LOGGER.warning("NapCat 自检失败：%s", error)
            return {"ok": False, "steps": [], "broken": "", "hint": "", "error": str(error)}

    def _sync_info_response(self) -> tuple[int, dict[str, Any]]:
        try:
            port = int(self.server.server_address[1])
            token = str(getattr(self.server, "token", "") or "")
            events = sum(1 for line in render_calendar(self.store.list_tasks()).splitlines() if line == "BEGIN:VEVENT")
            calendars = _advertised_calendars(port, token)
        except OSError:
            LOGGER.warning("无法确定 iPhone 日历订阅地址")
            return 503, {"ok": False, "error": "无法确定可供手机访问的局域网地址"}
        except Exception as error:  # noqa: BLE001
            LOGGER.exception("读取日历订阅信息失败")
            return 500, {"ok": False, "error": str(error) or "读取日历订阅信息失败"}
        if not calendars:
            LOGGER.warning("无法确定 iPhone 日历订阅地址")
            return 503, {"ok": False, "error": "无法确定可供手机访问的局域网地址"}
        for item in calendars:
            item["events"] = events
        lan_base = next((item["url"][: item["url"].index("/calendar.ics")] for item in calendars if item["kind"] == "lan"), "")
        return 200, {"ok": True, "lan_base": lan_base, "port": port, "events": events, "calendars": calendars}

    def _dingtalk_status_payload(self) -> dict[str, Any]:
        settings = self.server.settings  # type: ignore[attr-defined]
        provider = getattr(self.server, "dingtalk_reader_provider", None)
        reader = provider() if callable(provider) else getattr(self.server, "dingtalk_reader", None)
        enabled = bool(settings.dingtalk_enabled)
        running = bool(reader and reader.is_alive)
        if not enabled:
            state = "未启用"
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
        available = wechat_locate_storage() is not None
        if not enabled:
            state = "未启用"
        elif not available:
            state = "未找到微信（本机微信可能未安装或未登录过）"
        elif running:
            state = f"已连接 {len(settings.wechat_group_whitelist)} 个群"
        else:
            state = "微信已启用，扫描未启动"
        status = dict(_WECHAT_READ_STATUS)
        scan = dict(getattr(settings, "wechat_status", {}) or {})
        scan_state = str(scan.get("scan_state") or ("idle" if enabled else "disabled"))
        elapsed = max(0.0, round(time.monotonic() - float(scan.get("scan_started_monotonic") or 0.0), 1)) if scan_state == "running" else 0.0
        return {
            "ok": True,
            "enabled": enabled,
            "running": running,
            "available": available,
            "groups": len(settings.wechat_group_whitelist),
            "interval_seconds": int(settings.wechat_scan_interval_seconds),
            "last_read_at": str(status.get("at") or ""),
            "last_read_seconds": float(status.get("seconds") or 0.0),
            "last_error": str(status.get("error") or ""),
            "state": state,
            "hint": "" if available else wechat_storage_hint(),
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

    def _setup_status_payload(self) -> dict[str, Any]:
        settings = self.server.settings  # type: ignore[attr-defined]
        qq_online = bool(self._napcat_status_payload().get("online"))
        cx = self._chaoxing_cookie_validation_payload()
        channels = list(settings.push_channels())
        platforms = {
            "qq": {"configured": qq_online, "status": "online" if qq_online else "offline"},
            "wechat": {
                "configured": bool(settings.wechat_enabled and settings.wechat_group_whitelist),
                "status": "configured" if settings.wechat_enabled and settings.wechat_group_whitelist else "incomplete",
            },
            "dingtalk": {"configured": None, "status": "pending"},
            "chaoxing": {"configured": cx["result"] == "有效", "status": cx["result"]},
            "push": {"configured": bool(channels), "channels": channels},
            "llm": {"configured": bool(settings.llm_active), "provider": str(settings.llm_backend)},
        }
        return {"ok": True, "platforms": platforms}

    def _hosting_status_payload(self) -> dict[str, Any]:
        if hosting is None:
            return {"ok": False, "error": "托管模块不可用"}
        try:
            return hosting.hosting_status(self.server.settings)  # type: ignore[attr-defined]
        except Exception as error:  # noqa: BLE001
            return {"ok": False, "error": str(error)}

    def do_GET(self) -> None:  # noqa: N802 - 基类命名
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/health", "/api/health"):
            channels = self.server.settings.push_channels()  # type: ignore[attr-defined]
            self._json(200, {"ok": True, "service": "qq-tasks", "channels": channels})
            return
        if path == "/manifest.webmanifest":
            self._asset(MANIFEST_JSON, "application/manifest+json; charset=utf-8")
            return
        if path == "/icon.svg":
            self._asset(ICON_SVG, "image/svg+xml; charset=utf-8", cache="public, max-age=86400")
            return
        if path == "/sw.js":
            self._asset(SERVICE_WORKER_JS, "text/javascript; charset=utf-8")
            return
        if path == "/api/donate/qr.png":
            image = RUNTIME_ROOT / "assets" / "donate-wechat.jpg"
            if not image.is_file():
                self._json(404, {"ok": False, "error": "donation QR not found"})
                return
            try:
                self._jpeg(image.read_bytes())
            except OSError:
                self._json(404, {"ok": False, "error": "donation QR not found"})
            return
        if path == "/api/onboarding":
            accepted_at = self.store.meta_get("onboarding_accepted_at", "")
            self._json(200, {"required": not bool(accepted_at), "accepted_at": accepted_at})
            return
        if path == "/api/pair/status":
            # 手机在配对批准之前还没有令牌，这个接口必须免鉴权；它只认 code + secret，
            # 别人的码问不出任何东西，令牌也只会发给拿着同一个 secret 的那台手机。
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
        if not self._authorized():
            # 浏览器直接打开/刷新/走书签进来时，要么没有令牌、要么带着**另一份安装**
            # （同一个端口）留下的令牌。回一坨 JSON 只会让人以为程序坏了 —— 这时给个
            # 说人话的页面，并告诉他怎么正确地重开。
            if "text/html" in str(self.headers.get("Accept", "")):
                self._html(UNAUTHORIZED_HTML, status=401)
                return
            self._json(401, {"ok": False, "error": "invalid token"})
            return
        if path == "/api/backup":
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
        if path == "/api/qq/groups/status":
            owner = self.server.qq_group_refresh_owner  # type: ignore[attr-defined]
            self._json(200, owner.qq_group_status())
            return
        if path == "/api/chaoxing/cookie/status":
            self._json(200, {"ok": True, **self._chaoxing_cookie_validation_payload()})
            return
        if path == "/api/setup/status":
            self._json(200, self._setup_status_payload())
            return
        if path == "/api/inbox/image":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query, max_num_fields=8)
            message_ids = query.get("msg_id", [])
            msg_id = message_ids[0] if len(message_ids) == 1 else ""
            match = re.fullmatch(r"onebot:(.+):attach:([0-9a-f]{16})", msg_id) if len(msg_id) <= 256 else None
            if not match:
                self._json(404, {"ok": False, "error": "image not found"})
                return
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
                self._json(404, {"ok": False, "error": "image not found"})
                return
            files_dir = Path(self.server.settings.data_dir) / "files"  # type: ignore[attr-defined]
            try:
                if files_dir.is_symlink() or not files_dir.is_dir():
                    raise OSError("attachment directory unavailable")
                candidates = []
                for candidate in files_dir.glob(f"{key}_*"):
                    if stat.S_ISREG(candidate.lstat().st_mode):
                        candidates.append(candidate)
                if len(candidates) != 1:
                    raise OSError("attachment is missing or ambiguous")
                image = candidates[0]
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
        if path == "/api/announcements":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query, max_num_fields=8)
            force = str((query.get("refresh") or [""])[0]).strip().lower() in {"1", "true", "yes"}
            self._json(200, _announcement_response(self.server, force=force))
            return
        if path == "/api/update":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query, max_num_fields=8)
            force = str((query.get("check") or [""])[0]).strip().lower() in {"1", "true", "yes"}
            self._json(200, _update_response(self.server, force=force))
            return
        if path in ("/floatball", "/floatball.html"):
            # 桌面浮窗用的极简页面：同源加载，只读任务与收件箱，不改任何服务端状态。
            from .floatball_page import FLOATBALL_PAGE_HTML

            self._reply(200, FLOATBALL_PAGE_HTML.encode("utf-8"), "text/html; charset=utf-8", "no-store")
            return
        if path in ("/", "/index.html"):
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
        if path == "/setup":
            self._redirect_home()
            return
        if path == "/api/napcat/status":
            self._json(200, self._napcat_status_payload())
            return
        if path == "/api/selfcheck":
            doctor = self._napcat_doctor_payload()
            napcat = self._napcat_status_payload()
            hosting_status = self._hosting_status_payload()
            sync_code, sync_status = self._sync_info_response()
            report = selfcheck.check(
                self.server.settings,  # type: ignore[attr-defined]
                doctor=doctor,
                napcat=napcat,
                hosting=hosting_status,
                dingtalk=self._dingtalk_status_payload(),
                wechat=self._wechat_status_payload(),
                chaoxing=self._chaoxing_status_payload(),
                sync=sync_status if sync_code == 200 else {},
                push_channels=self.server.settings.push_channels(),  # type: ignore[attr-defined]
            )
            service = getattr(self.server, "service", None)
            stamp = getattr(service, "last_push_at", None)
            report["last_push_at"] = stamp.strftime("%Y-%m-%dT%H:%M:%S") if stamp else ""
            self._json(200, report)
            return
        if path == "/api/napcat/doctor":
            self._json(200, self._napcat_doctor_payload())
            return
        if path == "/api/groups/suggest":
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
        if path == "/api/napcat/groups":
            result = self._napcat("get_group_list")
            self._remember_group_names(result)
            if not result.get("ok"):
                self._json(200, {"ok": False, "groups": [], "error": result.get("error", "NapCat error")})
            else:
                self._json(200, {"ok": True, "groups": [{"group_id": str(g.get("group_id", "")), "name": str(g.get("group_name", "")), "member_count": g.get("member_count", 0), "selected": str(g.get("group_id", "")) in self.server.settings.group_whitelist} for g in result["data"]]})
            return
        if path == "/api/napcat/qrcode":
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
        if path == "/api/sync/info":
            status, payload = self._sync_info_response()
            self._json(status, payload)
            return
        if path == "/api/sync/test":
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
        if path == "/api/sync/qr.png":
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
        if path == "/api/app/qr.png":
            # 手机相机扫一下就能直接打开 App 并联好，用户不用输地址、也不用抄令牌。
            # 这条地址里带着本机令牌，所以它必须在本页登录之后才拿得到。
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
        if path == "/calendar":
            self._redirect_home()
            return
        if path == "/calendar.ics":
            self._ics(render_calendar(self.store.list_tasks()))
            return
        if path == "/api/history/floor":
            group_id = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query, max_num_fields=8).get("group_id") or [""])[0].strip()
            if not group_id or len(group_id) > 128:
                self._json(400, {"ok": False, "floor_ts": None, "total_seen": 0, "error": "群号无效"}); return
            if catchup is None:
                self._json(503, {"ok": False, "floor_ts": None, "total_seen": 0, "error": "回溯模块暂不可用"}); return
            try:
                result = catchup.available_floor(self.server.settings, group_id)  # type: ignore[attr-defined]
                error = str(result.get("error") or "")
                self._json(200, {"ok": not error, "floor_ts": result.get("floor_ts"), "total_seen": int(result.get("total_seen") or 0), "error": error})
            except Exception as error:  # noqa: BLE001
                LOGGER.exception("探测 NapCat 缓存底线失败")
                self._json(200, {"ok": False, "floor_ts": None, "total_seen": 0, "error": str(error) or "探测失败"})
            return
        if path == "/api/history/fetch/status":
            self._json(200, _job_snapshot("history")); return
        if path == "/api/inbox/classify/status":
            self._json(200, _job_snapshot("classify")); return
        if path == "/api/inbox":
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
        if path == "/api/dingtalk/status":
            self._json(200, self._dingtalk_status_payload())
            return
        if path == "/api/dingtalk/groups":
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
        if path == "/api/wechat/status":
            self._json(200, self._wechat_status_payload())
            return
        if path == "/api/wechat/groups":
            settings = self.server.settings  # type: ignore[attr-defined]
            if wechat_locate_storage() is None:
                self._json(200, {"ok": False, "groups": [], "error": wechat_storage_hint() or "未找到微信本地数据库；请确认本机微信已登录过"})
                return
            whitelist = set(settings.wechat_group_whitelist or ())
            aliases = dict(settings.wechat_group_aliases or {})
            LOGGER.info("开始读取微信群名（首次需扫描微信内存，可能耗时数分钟）")
            started = time.monotonic()
            # group_names 自身吞掉异常并返回 {}，密钥/内存字节不会外泄。
            names = wechat_group_names(None)
            elapsed = time.monotonic() - started
            _WECHAT_READ_STATUS.update({
                "at": now_local().strftime("%Y-%m-%d %H:%M:%S"),
                "seconds": round(elapsed, 1),
                "error": "" if names else "未解析到群名；微信可能未登录或数据库结构有变",
                "groups": len(names),
            })
            LOGGER.info("微信群名读取完成：%d 个群，耗时 %.1f 秒", len(names), elapsed)
            groups = [
                {"id": gid, "name": name, "alias": aliases.get(gid, ""), "selected": gid in whitelist}
                for gid, name in sorted(names.items(), key=lambda item: str(item[1]))
            ]
            groups, suggest_source, suggest_error = _suggest_external_groups(settings, self.server.store, "wechat", groups)
            self._json(200, {"ok": True, "groups": groups, "suggest_source": suggest_source, "suggest_error": suggest_error})
            return
        if path == "/api/chaoxing/courses":
            cookie = str(self.server.settings.chaoxing_cookie or "")  # type: ignore[attr-defined]
            if not cookie:
                self._json(400, {"ok": False, "error": "请先配置学习通 Cookie"})
                return
            try:
                courses = ChaoxingClient(cookie, timeout=self.server.settings.http_timeout, logger=LOGGER).my_courses()  # type: ignore[attr-defined]
                self._json(200, {"ok": True, "courses": courses})
            except ChaoxingAuthError:
                self._json(502, {"ok": False, "error": "学习通登录状态无效或已过期：请在浏览器重新登录学习通，再用托盘『读取学习通 Cookie』（手工复制的 cookie 读不到 HttpOnly 项，可能不完整）"})
            except ChaoxingError as error:
                self._json(502, {"ok": False, "error": str(error)})
            return
        if path == "/api/chaoxing/status":
            self._json(200, self._chaoxing_status_payload())
            return
        if path == "/api/settings":
            if hosting is None:
                self._json(200, {"ok": False, "error": "托管模块不可用"}); return
            try:
                prefs = hosting.prefs(self.server.settings)  # type: ignore[attr-defined]
                with self.server.catchup_preferences_lock:  # type: ignore[attr-defined]
                    prefs.update(self.server.catchup_preferences)  # type: ignore[attr-defined]
                settings = self.server.settings  # type: ignore[attr-defined]
                cookie = str(settings.chaoxing_cookie or "")
                cx_status = chaoxing_status(settings, self.store)
                prefs.update({"dingtalk_enabled": bool(settings.dingtalk_enabled), "dingtalk_group_whitelist": list(settings.dingtalk_group_whitelist), "chaoxing_enabled": bool(settings.chaoxing_enabled), "chaoxing_group_whitelist": list(settings.chaoxing_group_whitelist), "chaoxing_scan_interval_seconds": int(settings.chaoxing_scan_interval_seconds), "chaoxing_cookie": {"has_cookie": bool(cookie)}, "chaoxing_status": cx_status, "last_sync_at": cx_status["last_sync_at"], "expired": cx_status["expired"], "has_cookie": bool(cookie), "wechat_enabled": bool(settings.wechat_enabled), "wechat_group_whitelist": list(settings.wechat_group_whitelist), "wechat_group_aliases": dict(settings.wechat_group_aliases), "wechat_scan_interval_seconds": int(settings.wechat_scan_interval_seconds)})
                self._json(200, prefs)
            except Exception as error:  # noqa: BLE001
                self._json(200, {"ok": False, "error": str(error)})
            return
        if path == "/api/hosting/status":
            self._json(200, self._hosting_status_payload())
            return
        if path == "/api/pair/info":
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
            firewall = _firewall_allows(settings.web_port)
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
        if path == "/api/pair/pending":
            self._json(200, {"ok": True, "pending": pairing.pending()})
            return
        if path == "/api/tasks":
            now = now_local()
            tasks = self.store.list_tasks()
            # 待办里存的是群号（入库时就存成 messages.group_name，取不到名字时就是群号本身），
            # 界面上要显示群名，所以在出口这一层统一换成名字。
            names = self.store.group_names()
            settings = self.server.settings  # type: ignore[attr-defined]
            aliases = settings.group_aliases
            wechat_aliases = settings.wechat_group_aliases
            for task in tasks:
                is_wechat = str(task.get("source") or "") == "wechat"
                task["groups"] = [
                    wechat_group_label(group_id, names, wechat_aliases)
                    if is_wechat
                    else self.store.group_label(group_id, names, aliases)
                    for group_id in (task.get("groups") or [])
                ]
            grouped = group_tasks(tasks, now)
            payload = overview(tasks, grouped, now)
            payload["stats"] = self.store.task_stats()
            payload.update(grouped)
            payload["pinned"] = sorted(
                (task for task in tasks if task.get("pinned")),
                key=lambda task: (
                    str(task.get("deadline") or "9999-99-99"),
                    -int(task.get("importance") or 0),
                    -int(task.get("id") or 0),
                ),
            )
            self._json(200, payload)
            return
        if path == "/api/notices":
            items = []
            for digest in self.store.recent_digests(limit=8):
                items.append(
                    {
                        "id": digest.get("id"),
                        "kind": digest.get("kind"),
                        "created_at": digest.get("created_at"),
                        "summary": f"{digest.get('item_count') or 0} 条重点",
                        "body": str(digest.get("body") or "")[:600],
                    }
                )
            self._json(200, {"items": items})
            return
        if path == "/api/insights":
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
        if path == "/api/meta":
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
                    "push": _push_setting_state(settings),
                    "push_help": PUSH_HELP_LINKS,
                    "llm": _llm_setting_state(settings),
                    "providers": LLM_PROVIDERS,
                    "stats": self.store.task_stats(),
                },
            )
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - 基类命名
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path == "/api/onboarding/accept":
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > MAX_BODY_BYTES:
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
        if path == "/api/pair/start":
            # 手机第一次进门时还没有令牌，这条必须免鉴权。它只发一个 6 位码和 secret，
            # 真正的授权动作是用户在电脑设置页核对数字后点「允许」。
            try:
                result = pairing.start(device=self._device_label(), source=self._client_address())
            except ValueError as error:
                self._json(400, {"ok": False, "error": str(error)})
                return
            self._json(200, {"ok": True, **result})
            return
        if not self._authorized():
            self._json(401, {"ok": False, "error": "invalid token"})
            return
        if path == "/api/chaoxing/cookie/validate":
            self._json(200, self._validate_chaoxing_cookie())
            return
        if path == "/api/chaoxing/auto_cookie":
            if browser_cookie is None:
                self._json(200, {
                    "ok": False,
                    "source": "",
                    "category": "browser_unavailable",
                    "error": "自动读取浏览器 Cookie 功能不可用",
                    "detail": "浏览器 Cookie 读取模块未加载",
                    "next_step": "请在托盘菜单里选择「读取学习通 Cookie」，或在浏览器里登录学习通后重新打开设置页。",
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
                    "error": "浏览器 Cookie 读取模块运行失败",
                    "detail": type(error).__name__,
                    "next_step": "请改用另一个浏览器重试，或右键托盘图标选「读取学习通 Cookie」。",
                    "attempts": [],
                }
            if not isinstance(result, dict):
                result = {
                    "ok": False,
                    "cookie": "",
                    "source": "",
                    "category": "browser_unavailable",
                    "error": "浏览器 Cookie 读取结果无效",
                    "detail": "返回值不是对象",
                    "next_step": "请右键托盘图标选「读取学习通 Cookie」。",
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
                    "error": str(result.get("error") or "没有读取到学习通 Cookie"),
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
                    "error": "Cookie 保存失败",
                    "detail": type(error).__name__,
                    "next_step": "请检查群务台数据目录是否可写，然后重试。",
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
        if path == "/api/dingtalk/scan" or path == "/api/wechat/scan":
            source = "dingtalk" if path.startswith("/api/dingtalk/") else "wechat"
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
                if source == "wechat":
                    self._json(200, {"ok": False, "error": "扫描启动失败"})
                else:
                    detail = _redact_secrets(str(error) or error.__class__.__name__)[:240]
                    LOGGER.error("钉钉扫描启动失败（%s）：%s", type(error).__name__, detail)
                    self._json(200, {"ok": False, "error": "钉钉扫描启动失败", "category": "scan_start_failed", "detail": detail})
                return
            scan = dict(getattr(settings, f"{source}_status", {}) or {})
            scan_state = str(scan.get("scan_state") or ("running" if started else "idle"))
            elapsed = max(0.0, round(time.monotonic() - float(scan.get("scan_started_monotonic") or 0.0), 1)) if scan_state == "running" else 0.0
            if source == "dingtalk" and not started:
                if not settings.dingtalk_enabled:
                    category, error_text = "disabled", "请先启用钉钉接入"
                elif not settings.dingtalk_group_whitelist:
                    category, error_text = "no_groups_selected", "请先选择并保存至少一个钉钉群"
                else:
                    category, error_text = "scanner_unavailable", "钉钉扫描器未运行，请重新启用钉钉接入"
                self._json(200, {"ok": False, "started": False, "source": source, "scan_state": scan_state, "scan_stage": str(scan.get("scan_stage") or ""), "scan_elapsed_seconds": elapsed, "category": category, "error": error_text})
                return
            self._json(200, {"ok": True, "started": started, "source": source, "scan_state": scan_state, "scan_stage": str(scan.get("scan_stage") or ""), "scan_elapsed_seconds": elapsed})
            return
        if path == "/api/pair/approve":
            self._pair_decision(True)
            return
        if path == "/api/pair/deny":
            self._pair_decision(False)
            return
        if path == "/api/pair/public":
            settings = self.server.settings  # type: ignore[attr-defined]
            # SEC: Funnel 把整站反代到公网，请求经本机 tailscaled 转发后来源地址是 127.0.0.1，
            # 正好命中 _authorized 里「空 token 只放行环回」的分支 —— 等于把整站无鉴权公开。
            # 所以没有令牌时不许开公网，先让用户设一个。
            if not str(getattr(self.server, "token", "") or ""):
                self._json(400, {"ok": False, "error": "请先设置访问令牌再开到公网：在 .env 里填 QQ_DIGEST_WEB_TOKEN 后重启，再来点这里。"})
                return
            result = _enable_tailscale_funnel(settings.web_port)
            if result.get("ok"):
                self._json(200, {"ok": True, "url": result.get("url", "")})
            else:
                self._json(400, {"ok": False, "error": str(result.get("error") or "开通公网入口失败")})
            return
        if path == "/api/tasks/urgent":
            try:
                payload = self._json_body()
                task_id = payload.get("task_id")
                value = payload.get("urgent")
                if not isinstance(task_id, str) or not task_id.isascii() or not task_id.isdigit() or len(task_id) > 18 or int(task_id) < 1:
                    raise ValueError("task_id 必须是正整数文本")
                if "urgent" not in payload or (value is not None and type(value) is not bool):
                    raise ValueError("urgent 必须是 true、false 或 null")
            except ValueError as error:
                self._json(400, {"ok": False, "error": str(error)})
                return
            try:
                result = self.store.set_task_urgent(int(task_id), value)
            except Exception as error:  # noqa: BLE001
                LOGGER.exception("保存任务紧急覆盖失败")
                self._json(500, {"ok": False, "error": str(error) or "紧急设置保存失败"})
                return
            self._json(200 if result.get("ok") else 404, result)
            return
        if path == "/api/tasks/pin":
            try:
                payload = self._json_body()
                task_id = payload.get("task_id")
                value = payload.get("pinned")
                if not isinstance(task_id, str) or not task_id.isascii() or not task_id.isdigit() or len(task_id) > 18 or int(task_id) < 1:
                    raise ValueError("task_id 必须是正整数文本")
                if type(value) is not bool:
                    raise ValueError("pinned 必须是 true 或 false")
            except ValueError as error:
                self._json(400, {"ok": False, "error": str(error)})
                return
            try:
                result = self.store.set_task_pinned(int(task_id), value)
            except ValueError as error:
                self._json(400, {"ok": False, "error": str(error)})
                return
            except Exception as error:  # noqa: BLE001
                LOGGER.exception("保存任务置顶失败")
                self._json(500, {"ok": False, "error": str(error) or "置顶设置保存失败"})
                return
            self._json(200 if result.get("ok") else 404, result)
            return
        if path == "/api/tasks/create":
            try:
                payload = self._json_body()
                summary = payload.get("summary")
                deadline = payload.get("deadline") or ""
                if not isinstance(summary, str):
                    raise ValueError("summary 必须是文本")
                summary = " ".join(summary.split())
                if not summary:
                    raise ValueError("请填写待办内容")
                if len(summary) > 200:
                    raise ValueError("待办内容请控制在 200 字以内")
                if deadline and not isinstance(deadline, str):
                    raise ValueError("deadline 必须是文本")
                stamp = ""
                if deadline:
                    parsed = parse_iso(deadline)
                    if parsed is None:
                        raise ValueError("截止时间格式不对，请重新选择")
                    stamp = iso(parsed)
            except ValueError as error:
                self._json(400, {"ok": False, "error": str(error)})
                return
            try:
                # 手动新建的待办用一次性 task_key，避免和 QQ 消息里抽出来的任务互相覆盖。
                task_id = self.store.upsert_task(
                    task_key=f"manual:{secrets.token_hex(8)}",
                    summary=summary,
                    deadline=stamp,
                    source="manual",
                    confidence=1.0,
                    classification_reason="页面手动新建",
                )
            except Exception as error:  # noqa: BLE001
                LOGGER.exception("手动新建待办失败")
                self._json(500, {"ok": False, "error": str(error) or "新建待办失败"})
                return
            if not task_id:
                self._json(500, {"ok": False, "error": "新建待办失败"})
                return
            self._json(200, {"ok": True, "task_id": task_id, "summary": summary, "deadline": stamp})
            return
        if path == "/api/settings/test-llm":
            settings = self.server.settings  # type: ignore[attr-defined]
            started = dt.datetime.now()
            model = str(settings.dashscope_model or "")
            try:
                from .attachments import chat_completion

                reply = chat_completion(
                    settings,
                    [{"role": "user", "content": "只回复三个字：已连通"}],
                    model=model,
                    timeout=min(30, max(10, int(settings.llm_timeout or 60))),
                    max_tokens=16,
                )
            except Exception as error:  # noqa: BLE001 - 任何失败都原样翻译给页面
                self._json(200, {
                    "ok": False,
                    "error": _llm_test_error(error),
                    "model": model,
                    "endpoint": str(settings.dashscope_endpoint or ""),
                })
                return
            self._json(200, {
                "ok": True,
                "model": model,
                "reply": " ".join(str(reply or "").split())[:120],
                "seconds": round((dt.datetime.now() - started).total_seconds(), 1),
            })
            return
        if path == "/api/settings/test-push":
            settings = self.server.settings  # type: ignore[attr-defined]
            try:
                from .push import build_pushers
            except Exception as error:  # noqa: BLE001
                self._json(200, {"ok": False, "error": f"推送模块加载失败：{error}", "results": []})
                return
            try:
                pushers = build_pushers(settings)
            except Exception as error:  # noqa: BLE001
                self._json(200, {"ok": False, "error": f"推送通道初始化失败：{error}", "results": []})
                return
            if not pushers:
                self._json(200, {"ok": False, "error": "还没有配置任何推送通道", "results": []})
                return
            results: list[dict[str, Any]] = []
            for pusher in pushers:
                try:
                    pusher.send("QQ 群消息台 · 测试消息", "看到这条消息，说明提醒已经能送到你手机上了。")
                except Exception as error:  # noqa: BLE001
                    results.append({
                        "channel": str(getattr(pusher, "name", "通道")),
                        "target": str(getattr(pusher, "target", "")),
                        "ok": False,
                        "error": str(error)[:200] or "发送失败",
                    })
                    continue
                results.append({
                    "channel": str(getattr(pusher, "name", "通道")),
                    "target": str(getattr(pusher, "target", "")),
                    "ok": True,
                    "error": "",
                })
            self._json(200, {
                "ok": all(item["ok"] for item in results),
                "results": results,
            })
            return
        if path == "/api/settings":
            length = int(self.headers.get("Content-Length") or 0)
            LOGGER.info("settings 保存请求到达，body=%d bytes", length)
            if length < 0 or length > MAX_BODY_BYTES:
                self._json(400, {"ok": False, "error": "bad body"}); return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("settings must be a JSON object")
            except (ValueError, TypeError, UnicodeDecodeError):
                self._json(400, {"ok": False, "error": "bad json"}); return
            catchup_keys = {key for key in ("catchup_enabled", "catchup_hours") if key in payload}
            host_keys = {"quit_qq", "restore_qq", "quit_restore_qq", "auto_on_start", "autostart"}.intersection(payload)
            push_keys = set(_PUSH_SETTING_ENV_KEYS).intersection(payload)
            llm_keys = {"llm_provider", "llm_api_key", "llm_model", "llm_endpoint"}.intersection(payload)
            chaoxing_keys = {"chaoxing_enabled", "chaoxing_cookie", "chaoxing_group_whitelist", "chaoxing_clear_cookie"}.intersection(payload)
            dingtalk_keys = {"dingtalk_enabled", "dingtalk_group_whitelist"}.intersection(payload)
            wechat_keys = {"wechat_enabled", "wechat_group_whitelist", "wechat_group_aliases", "wechat_scan_interval_seconds"}.intersection(payload)
            napcat_keys = {"napcat_dir"}.intersection(payload)
            update_channel_keys = {"update_channel"}.intersection(payload)
            if update_channel_keys and any((catchup_keys, host_keys, push_keys, llm_keys, chaoxing_keys, dingtalk_keys, wechat_keys, napcat_keys)):
                self._json(400, {"ok": False, "error": "更新通道设置请单独保存"}); return
            if catchup_keys and host_keys:
                self._json(400, {"ok": False, "error": "托管设置与回溯设置请分开保存"}); return
            if push_keys and (catchup_keys or host_keys):
                self._json(400, {"ok": False, "error": "推送通道设置请单独保存"}); return
            if llm_keys and (push_keys or catchup_keys or host_keys or chaoxing_keys or wechat_keys):
                self._json(400, {"ok": False, "error": "AI 接入设置请单独保存"}); return
            if dingtalk_keys and (push_keys or catchup_keys or host_keys or llm_keys or chaoxing_keys or wechat_keys):
                self._json(400, {"ok": False, "error": "钉钉设置请单独保存"}); return
            if chaoxing_keys and (push_keys or catchup_keys or host_keys or dingtalk_keys or wechat_keys):
                self._json(400, {"ok": False, "error": "学习通设置请单独保存"}); return
            if wechat_keys and (push_keys or catchup_keys or host_keys or llm_keys or dingtalk_keys or chaoxing_keys):
                self._json(400, {"ok": False, "error": "微信设置请单独保存"}); return
            if napcat_keys and (push_keys or catchup_keys or host_keys or llm_keys or dingtalk_keys or chaoxing_keys or wechat_keys):
                self._json(400, {"ok": False, "error": "NapCat 目录设置请单独保存"}); return
            try:
                if update_channel_keys:
                    settings = self.server.settings  # type: ignore[attr-defined]
                    if not str(settings.env_file or ""):
                        raise ValueError("找不到 .env 路径")
                    raw_channel = payload["update_channel"]
                    if not isinstance(raw_channel, str):
                        raise ValueError("update_channel 必须是文本")
                    channel = raw_channel.strip().lower()
                    if channel not in {"stable", "beta"}:
                        channel = ""
                    result = update_env_file(Path(settings.env_file), {"QQ_DIGEST_UPDATE_CHANNEL": channel})
                    if not result.get("ok"):
                        raise ValueError(result.get("error") or ".env 写入失败")
                    settings.update_channel = channel
                    os.environ["QQ_DIGEST_UPDATE_CHANNEL"] = channel
                    _invalidate_update_cache(self.server)
                    self._json(200, {"ok": True, "update_channel": channel})
                    return
                if napcat_keys:
                    settings = self.server.settings  # type: ignore[attr-defined]
                    if not str(settings.env_file or ""):
                        raise ValueError("找不到 .env 路径")
                    value = payload["napcat_dir"]
                    if not isinstance(value, str):
                        raise ValueError("napcat_dir 必须是文本")
                    value = value.strip()
                    if any(char in value for char in "\r\n\0"):
                        raise ValueError("NapCat 目录无效")
                    updates = {"QQ_DIGEST_NAPCAT_DIR": value} if value else {}
                    env_path = Path(settings.env_file)
                    if value:
                        result = update_env_file(env_path, updates)
                    else:
                        if not env_path.is_file():
                            raise ValueError(".env not found")
                        raw = env_path.read_bytes()
                        kept = []
                        for line in raw.splitlines(keepends=True):
                            text = line.decode("utf-8")
                            if text.split("=", 1)[0].strip() == "QQ_DIGEST_NAPCAT_DIR":
                                continue
                            kept.append(line)
                        env_path.write_bytes(b"".join(kept))
                        result = {"ok": True}
                    if not result.get("ok"):
                        raise ValueError(result.get("error") or ".env 写入失败")
                    if value:
                        os.environ["QQ_DIGEST_NAPCAT_DIR"] = value
                    else:
                        os.environ.pop("QQ_DIGEST_NAPCAT_DIR", None)
                    self._json(200, {"ok": True, "napcat_dir": value})
                    return
                if wechat_keys:
                    settings = self.server.settings  # type: ignore[attr-defined]
                    if not str(settings.env_file or ""):
                        raise ValueError("找不到 .env 路径")
                    if "wechat_enabled" in payload and type(payload["wechat_enabled"]) is not bool:
                        raise ValueError("wechat_enabled 必须是布尔值")
                    groups = settings.wechat_group_whitelist
                    if "wechat_group_whitelist" in payload:
                        raw_groups = payload["wechat_group_whitelist"]
                        if not isinstance(raw_groups, list) or any(not isinstance(x, str) or not x.strip() for x in raw_groups):
                            raise ValueError("wechat_group_whitelist 必须是文本列表")
                        groups = tuple(dict.fromkeys(x.strip() for x in raw_groups))
                    aliases = dict(settings.wechat_group_aliases or {})
                    if "wechat_group_aliases" in payload:
                        raw_aliases = payload["wechat_group_aliases"]
                        if not isinstance(raw_aliases, dict) or any(
                            not isinstance(key, str) or not isinstance(value, str)
                            for key, value in raw_aliases.items()
                        ):
                            raise ValueError("wechat_group_aliases 必须是「群ID: 别名」的对象")
                        aliases = {key.strip(): value.strip() for key, value in raw_aliases.items() if key.strip() and value.strip()}
                    interval = int(settings.wechat_scan_interval_seconds)
                    if "wechat_scan_interval_seconds" in payload:
                        try:
                            interval = max(30, int(payload["wechat_scan_interval_seconds"]))
                        except (TypeError, ValueError):
                            raise ValueError("wechat_scan_interval_seconds 必须是整数")
                    enabled = payload.get("wechat_enabled", settings.wechat_enabled)
                    updates = {}
                    if "wechat_enabled" in payload:
                        updates["QQ_DIGEST_WECHAT_ENABLED"] = "1" if enabled else "0"
                    if "wechat_group_whitelist" in payload:
                        updates["QQ_DIGEST_WECHAT_GROUP_WHITELIST"] = ",".join(groups)
                    if "wechat_group_aliases" in payload:
                        updates["QQ_DIGEST_WECHAT_GROUP_ALIASES"] = ",".join(f"{key}={value}" for key, value in aliases.items())
                    if "wechat_scan_interval_seconds" in payload:
                        updates["QQ_DIGEST_WECHAT_SCAN_INTERVAL_SECONDS"] = str(interval)
                    result = update_env_file(Path(settings.env_file), updates)
                    if not result.get("ok"):
                        raise ValueError(result.get("error") or ".env 写入失败")
                    settings.wechat_enabled = enabled
                    settings.wechat_group_whitelist = groups
                    settings.wechat_group_aliases = aliases
                    settings.wechat_scan_interval_seconds = interval
                    reload_reader = getattr(self.server, "reload_wechat", None)
                    if callable(reload_reader): reload_reader()
                    self._json(200, {"ok": True, "wechat_enabled": bool(settings.wechat_enabled), "wechat_group_whitelist": list(settings.wechat_group_whitelist), "wechat_group_aliases": dict(settings.wechat_group_aliases), "wechat_scan_interval_seconds": int(settings.wechat_scan_interval_seconds)})
                    return
                if dingtalk_keys:
                    settings = self.server.settings  # type: ignore[attr-defined]
                    if not str(settings.env_file or ""):
                        raise ValueError("找不到 .env 路径")
                    if "dingtalk_enabled" in payload and type(payload["dingtalk_enabled"]) is not bool:
                        raise ValueError("dingtalk_enabled 必须是布尔值")
                    groups = settings.dingtalk_group_whitelist
                    if "dingtalk_group_whitelist" in payload:
                        raw_groups = payload["dingtalk_group_whitelist"]
                        if not isinstance(raw_groups, list) or any(not isinstance(x, str) or not x.strip() for x in raw_groups):
                            raise ValueError("dingtalk_group_whitelist 必须是文本列表")
                        groups = tuple(dict.fromkeys(x.strip() for x in raw_groups))
                    enabled = payload.get("dingtalk_enabled", settings.dingtalk_enabled)
                    updates = {}
                    if "dingtalk_enabled" in payload:
                        updates["QQ_DIGEST_DINGTALK_ENABLED"] = "1" if enabled else "0"
                    if "dingtalk_group_whitelist" in payload:
                        updates["QQ_DIGEST_DINGTALK_GROUP_WHITELIST"] = ",".join(groups)
                    result = update_env_file(Path(settings.env_file), updates)
                    if not result.get("ok"):
                        raise ValueError(result.get("error") or ".env 写入失败")
                    settings.dingtalk_enabled = enabled
                    settings.dingtalk_group_whitelist = groups
                    reload_reader = getattr(self.server, "reload_dingtalk", None)
                    if callable(reload_reader): reload_reader()
                    self._json(200, {"ok": True, "dingtalk_enabled": bool(settings.dingtalk_enabled), "dingtalk_group_whitelist": list(settings.dingtalk_group_whitelist)})
                    return
                if chaoxing_keys:
                    settings = self.server.settings  # type: ignore[attr-defined]
                    if "chaoxing_enabled" in payload and type(payload["chaoxing_enabled"]) is not bool:
                        raise ValueError("chaoxing_enabled 必须是布尔值")
                    if "chaoxing_group_whitelist" in payload:
                        groups = payload["chaoxing_group_whitelist"]
                        if not isinstance(groups, list) or any(not isinstance(x, str) or not x.strip() for x in groups):
                            raise ValueError("chaoxing_group_whitelist 必须是文本列表")
                        settings.chaoxing_group_whitelist = tuple(x.strip() for x in groups)
                    if "chaoxing_enabled" in payload: settings.chaoxing_enabled = payload["chaoxing_enabled"]
                    cookie_path = Path(settings.data_dir) / "chaoxing.cookie"
                    if payload.get("chaoxing_clear_cookie"):
                        cookie_path.unlink(missing_ok=True); settings.chaoxing_cookie = ""
                    elif "chaoxing_cookie" in payload:
                        cleaned_cookie = _clean_chaoxing_cookie(payload["chaoxing_cookie"])
                        if cleaned_cookie:
                            cookie_path.parent.mkdir(parents=True, exist_ok=True); cookie_path.write_text(cleaned_cookie, encoding="utf-8"); settings.chaoxing_cookie = cleaned_cookie
                    cookie = str(settings.chaoxing_cookie or "")
                    if payload.get("chaoxing_clear_cookie") or ("chaoxing_cookie" in payload and _clean_chaoxing_cookie(payload["chaoxing_cookie"])):
                        lock = getattr(settings, "chaoxing_status_lock", None)
                        if lock:
                            with lock:
                                status = dict(getattr(settings, "chaoxing_status", {}) or {})
                                status.update({"ok": False, "expired": False, "last_error": ""})
                                settings.chaoxing_status = status
                        else:
                            status = {"ok": False, "expired": False, "last_sync_at": "", "last_error": ""}
                            settings.chaoxing_status = status
                        self.store.meta_set("chaoxing_status", json.dumps(status, ensure_ascii=False))
                    reload_reader = getattr(self.server, "reload_chaoxing", None)
                    if callable(reload_reader): reload_reader()
                    self._json(200, {"ok": True, "chaoxing_enabled": bool(settings.chaoxing_enabled), "chaoxing_cookie": {"has_cookie": bool(cookie)}, "chaoxing_status": chaoxing_status(settings, self.store), "chaoxing_group_whitelist": list(settings.chaoxing_group_whitelist)})
                    return
                if llm_keys:
                    settings = self.server.settings  # type: ignore[attr-defined]
                    if not str(settings.env_file or ""):
                        raise ValueError("找不到 .env 路径")
                    updates, applied = _llm_setting_updates(payload, settings)
                    result = update_env_file(Path(settings.env_file), updates)
                    if not result.get("ok"):
                        raise ValueError(result.get("error") or ".env 写入失败")
                    # 热生效：保存后立刻用新服务商，不必重启服务。
                    for field_name, field_value in applied.items():
                        setattr(settings, field_name, field_value)
                    self._json(200, {"ok": True, "llm": _llm_setting_state(settings)})
                    return
                if push_keys:
                    settings = self.server.settings  # type: ignore[attr-defined]
                    if not str(settings.env_file or ""):
                        raise ValueError("找不到 .env 路径")
                    updates, applied = _push_setting_values(payload, push_keys)
                    result = update_env_file(Path(settings.env_file), updates)
                    if not result.get("ok"):
                        raise ValueError(result.get("error") or ".env 写入失败")
                    # 热生效：同一进程内的 Settings 立刻反映新通道，不必重启。
                    for field_name, field_value in applied.items():
                        setattr(settings, field_name, field_value)
                    self._json(200, {
                        "ok": True,
                        "channels": settings.push_channels(),
                        "push": _push_setting_state(settings),
                    })
                    return
                if catchup_keys:
                    settings = self.server.settings  # type: ignore[attr-defined]
                    if "catchup_enabled" in payload and not isinstance(payload["catchup_enabled"], bool):
                        raise ValueError("catchup_enabled 必须是布尔值")
                    with self.server.catchup_preferences_lock:  # type: ignore[attr-defined]
                        current_prefs = dict(self.server.catchup_preferences)  # type: ignore[attr-defined]
                    hours = payload.get("catchup_hours", current_prefs["catchup_hours"])
                    if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 8760:
                        raise ValueError("回溯小时数必须在 1 到 8760 之间")
                    env_path = Path(settings.env_file or "")
                    if not str(settings.env_file or ""):
                        raise ValueError("找不到 .env 路径")
                    updates = {}
                    if "catchup_enabled" in payload:
                        updates["QQ_DIGEST_CATCHUP_ENABLED"] = "1" if payload["catchup_enabled"] else "0"
                    if "catchup_hours" in payload:
                        updates["QQ_DIGEST_CATCHUP_HOURS"] = str(hours)
                    result = update_env_file(env_path, updates)
                    if not result.get("ok"):
                        self._json(400, {"ok": False, "error": result.get("error") or ".env 写入失败"}); return
                    with self.server.catchup_preferences_lock:  # type: ignore[attr-defined]
                        saved_prefs = dict(self.server.catchup_preferences)  # type: ignore[attr-defined]
                        if "catchup_enabled" in payload:
                            saved_prefs["catchup_enabled"] = payload["catchup_enabled"]
                        if "catchup_hours" in payload:
                            saved_prefs["catchup_hours"] = hours
                        self.server.catchup_preferences = saved_prefs  # type: ignore[attr-defined]
                    self._json(200, {"ok": True, "updated": result.get("updated"), "added": result.get("added"), **saved_prefs})
                    return
                if hosting is None:
                    self._json(200, {"ok": False, "error": "托管模块不可用"}); return
                result = hosting.save_prefs(self.server.settings, payload)  # type: ignore[attr-defined]
                self._json(200 if result.get("ok") else 400, result)
            except (ValueError, TypeError) as error:
                self._json(400, {"ok": False, "error": str(error)})
            except Exception as error:  # noqa: BLE001
                self._json(200, {"ok": False, "error": str(error) or "设置保存失败"})
            return
        if path == "/api/history/fetch/cancel":
            with _JOB_LOCK:
                cancel_event = _JOB_STATE["cancel"]
                active = _JOB_STATE["active"]
                running = _JOB_STATE["history"]["running"]
                if active == "history" and running and isinstance(cancel_event, threading.Event):
                    cancel_event.set()
                    cancelled = True
                else:
                    cancelled = False
            if not cancelled:
                self._json(409, {"ok": False, "error": "当前没有正在运行的回溯任务"}); return
            self._json(200, {"ok": True, "cancelled": True}); return
        if path == "/api/history/fetch":
            try:
                payload = self._json_body()
                groups = _parse_groups(payload.get("groups"))
                since = _parse_day(payload.get("since"))
                until = _parse_day(payload.get("until"), end=True)
                if since and until and since > until:
                    raise ValueError("开始日期不能晚于结束日期")
            except ValueError as error:
                self._json(400, {"ok": False, "error": str(error)}); return
            if catchup is None:
                self._json(503, {"ok": False, "error": "回溯模块暂不可用"}); return
            settings = self.server.settings  # type: ignore[attr-defined]
            started, active = _start_job("history", lambda progress, cancel: catchup.backfill_range(settings, self.store, groups=groups, since=since, until=until, progress=progress, cancel=cancel))
            if not started:
                self._json(409, {"ok": False, "error": "已有回溯任务在进行" if active == "history" else "已有通知提取任务在进行"}); return
            self._json(200, {"ok": True, "started": True}); return
        if path == "/api/inbox/classify":
            try:
                payload = self._json_body()
                groups = _parse_groups(payload.get("groups"))
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
            settings = self.server.settings  # type: ignore[attr-defined]
            started, active = _start_job("classify", lambda progress, cancel: inbox.classify_messages(settings, self.store, since=since, until=until, groups=groups, limit=limit, progress=progress, cancel=cancel))
            if not started:
                self._json(409, {"ok": False, "error": "已有通知提取任务在进行" if active == "classify" else "已有回溯任务在进行"}); return
            self._json(200, {"ok": True, "started": True}); return
        if path == "/api/inbox/promote":
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
                result = inbox.promote(self.server.settings, self.store, msg_id, title=title, due=due)  # type: ignore[attr-defined]
                self._json(200 if result.get("ok") else 400, {"ok": bool(result.get("ok")), "task_id": result.get("task_id"), "error": str(result.get("error") or "")})
            except Exception as error:  # noqa: BLE001
                LOGGER.exception("通知转待办失败")
                self._json(200, {"ok": False, "task_id": None, "error": str(error) or "转换失败"})
            return
        if path in ("/api/hosting/start", "/api/hosting/stop"):
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > MAX_BODY_BYTES:
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
        if path == "/api/subscriptions":
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY_BYTES:
                self._json(400, {"ok": False, "error": "bad body"}); return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                groups = [str(x).strip() for x in payload.get("groups", []) if str(x).strip()]
                aliases = {str(k): str(v) for k, v in (payload.get("aliases") or {}).items() if str(k).strip()}
            except (ValueError, TypeError, AttributeError):
                self._json(400, {"ok": False, "error": "bad json"}); return
            settings = self.server.settings  # type: ignore[attr-defined]
            # 用共用的 update_env_file：它就地改写已有键，并把缺失的键追加到文件末尾。
            # 这里以前是手写的一份拷贝，缺键时直接回 "required .env keys missing"；而全新安装的
            # .env（packaging/launcher.py 的 ENV_TEMPLATE）根本没有 QQ_DIGEST_GROUPS 和
            # QQ_DIGEST_GROUP_ALIASES 这两行 —— 于是「第一次保存订阅」对任何测试者都必定失败。
            env_path = Path(settings.env_file or Path.cwd() / ".env")
            updates = {"QQ_DIGEST_GROUPS": ",".join(groups), "QQ_DIGEST_GROUP_ALIASES": ",".join(k + "=" + v for k, v in aliases.items())}
            result = update_env_file(env_path, updates)
            if not result.get("ok"):
                self._json(400, {"ok": False, "error": result.get("error") or ".env 写入失败"}); return
            settings.group_whitelist = tuple(groups); settings.group_aliases = aliases
            self._json(200, {"ok": True, "groups": groups, "applied": True}); return
        if path == "/api/update/download":
            self._json(200, _start_update_download(self.server))
            return
        if path == "/api/update/apply":
            self._json(200, _apply_update(self.server))
            return
        if path == "/api/napcat/install":
            if napcat_admin is None:
                self._json(200, {"ok": False, "installed": False, "already_installed": False, "root": None, "version": None, "bytes": 0, "error": "一键接入模块不可用"}); return
            try:
                result = napcat_admin.install(self.server.settings)  # type: ignore[attr-defined]
            except Exception as error:  # noqa: BLE001
                result = {"ok": False, "installed": False, "already_installed": False, "root": None, "version": None, "bytes": 0, "error": str(error)}
            self._json(200, result)
            return
        if path == "/api/napcat/launch":
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY_BYTES:
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
            self._json(200, result)
            return
        if path == "/api/napcat/restart":
            # 同一个 profile 只允许一个 NapCat，所以「卡住了」时唯一的出路是显式重启：
            # 先停掉本安装的实例，再拉一个干净的（旧的二维码随之作废）。
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY_BYTES:
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
            self._json(200, result)
            return
        if path == "/api/napcat/autosetup":
            if napcat_admin is None:
                self._json(200, {"ok": False, "steps": [], "qrcode_path": None, "uin": None, "applied_via": None, "restart_required": False, "error": "一键接入模块不可用"})
                return
            try:
                result = napcat_admin.auto_setup(self.server.settings)  # type: ignore[attr-defined]
            except Exception as error:  # noqa: BLE001
                LOGGER.warning("一键接入失败：%s", error)
                result = {"ok": False, "steps": [], "qrcode_path": None, "uin": None, "applied_via": None, "restart_required": False, "error": str(error)}
            self._json(200, result)
            return
        if not path.startswith("/api/tasks/"):
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            task_id = int(path.rsplit("/", 1)[-1])
        except ValueError:
            self._json(400, {"ok": False, "error": "bad task id"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > MAX_BODY_BYTES:
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
        if not action and "done" in payload:
            action = "done" if bool(payload.get("done")) else "reopen"
        if action == "correct":
            try:
                changed = self.store.apply_task_correction(
                    task_id,
                    str(payload.get("correction") or ""),
                    value=payload.get("value", ""),
                )
            except ValueError as error:
                self._json(400, {"ok": False, "error": str(error)})
                return
        else:
            if action not in {"confirm", "dismiss", "done", "reopen", "snooze"}:
                self._json(400, {"ok": False, "error": "unsupported action"})
                return
            detail: dict[str, Any] = {}
            if action == "snooze":
                settings = self.server.settings  # type: ignore[attr-defined]
                detail = {"until": snooze_default_until(settings, now_local())}
            changed = self.store.apply_task_action(task_id, action, detail=detail)
        task = self.store.get_task(task_id) if changed else None
        self._json(
            200 if changed else 404,
            {
                "ok": changed,
                "id": task_id,
                "action": action,
                "done": bool(task and task.get("status") == "done"),
                "status": (task or {}).get("status", ""),
            },
        )


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
            server = ThreadingHTTPServer((self.settings.web_host, self.settings.web_port), _Handler)
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
